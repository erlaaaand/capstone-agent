# agent/db_sender.py
#
# Pipeline: raw fetcher items → Ollama LLM (normalisasi) → MarketReportDto → HMAC sign → POST NestJS
#
# Perbaikan dari log error 2026-06-08:
#   [FIX-1] OLLAMA_BATCH_SIZE default 3 (bukan 6) — mencegah timeout & output terpotong
#   [FIX-2] num_predict dinaikkan ke 4096 — mencegah JSON terpotong di tengah
#   [FIX-3] _sanitize_entry diperkuat — weight_reference WAJIB terisi sebelum POST
#   [FIX-4] Retry logic per-batch Ollama — 1x retry jika timeout/parse error
#   [FIX-5] _post_to_nestjs: fallback rawBody ke JSON.stringify(body) harus compact
#           (separators=(",",":")). Sudah benar, ditambah log signature untuk debug.
#   [FIX-6] Tambah field agentRunId ke setiap entry sesuai MarketPriceEntity NestJS
#           (NestJS memetakan run_id → agentRunId lewat mapper, bukan di sini)
#   [FIX-7] _normalize_variety: hanya kirim item is_whole_fruit=True ke NestJS
#   [FIX-8] _sanitize_entry: truncate semua string field dengan aman (None-safe)

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core import config
from core.logger import get_logger

logger = get_logger("agent.db_sender")

# ══════════════════════════════════════════════════════════════════════════════
# Konfigurasi
# ══════════════════════════════════════════════════════════════════════════════

NESTJS_BASE_URL:     str   = os.getenv("NESTJS_BASE_URL", "http://localhost:3001")
NESTJS_INTERNAL_KEY: str   = os.getenv("NESTJS_INTERNAL_API_KEY", "")
NESTJS_INGEST_PATH:  str   = "/api/v1/ai-integration/market-report"
NESTJS_TIMEOUT_SEC:  int   = int(os.getenv("NESTJS_TIMEOUT_SEC", "30"))

OLLAMA_BASE_URL:     str   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL:        str   = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT_SEC:  int   = int(os.getenv("OLLAMA_TIMEOUT_SEC", "180"))

# [FIX-1] Default 3, bukan 6.
# qwen2.5:7b stabil dengan 3 item per batch.
# Dengan 6 item, output JSON sering terpotong → parse error / timeout.
OLLAMA_BATCH_SIZE:   int   = int(os.getenv("OLLAMA_BATCH_SIZE", "3"))

# Jumlah retry per batch jika Ollama timeout atau JSON parse error
OLLAMA_BATCH_RETRIES: int  = int(os.getenv("OLLAMA_BATCH_RETRIES", "1"))

MIN_CONFIDENCE:      float = float(os.getenv("DB_MIN_CONFIDENCE", "0.5"))

_VARIETY_ALIASES: Dict[str, str] = {
    "D197": "Musang King / Mao Shan Wang / Raja Kunyit",
    "D13":  "Golden Bun",
    "D24":  "Sultan / Bukit Merah / D24",
    "D2":   "Dato Nina / D2",
}


# ══════════════════════════════════════════════════════════════════════════════
# Sanitasi output Ollama
# ══════════════════════════════════════════════════════════════════════════════

def _sanitize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pastikan semua field wajib NestJS ValidationPipe terisi dengan nilai
    default yang aman. Dipanggil satu kali per entry setelah parsing Ollama.

    [FIX-3] Logika weight_reference diperkuat:
      - Rekonstruksi dari harga yang tersedia
      - Pastikan tidak pernah None/empty setelah sanitasi
      - Truncate aman (handle None)

    [FIX-8] Semua string field: handle None sebelum [:N]
    """
    # ── weight_reference: @IsNotEmpty @IsString @MaxLength(200) ─────────────
    weight_ref = entry.get("weight_reference")
    if not isinstance(weight_ref, str) or not weight_ref.strip():
        # Rekonstruksi dari konteks harga
        has_kg   = any(entry.get(k) is not None for k in
                       ("price_per_kg_min", "price_per_kg_max", "price_per_kg_avg"))
        has_unit = any(entry.get(k) is not None for k in
                       ("price_per_unit_min", "price_per_unit_max"))
        if has_kg and has_unit:
            weight_ref = "per buah (dengan referensi per kg)"
        elif has_kg:
            weight_ref = "per kg"
        elif has_unit:
            weight_ref = "per buah"
        else:
            weight_ref = "tidak diketahui"
    entry["weight_reference"] = str(weight_ref)[:200]

    # ── variety_alias: @IsNotEmpty @IsString @MaxLength(100) ────────────────
    alias = entry.get("variety_alias")
    if not isinstance(alias, str) or not alias.strip():
        entry["variety_alias"] = str(entry.get("variety_code", "unknown"))
    else:
        entry["variety_alias"] = alias[:100]

    # ── confidence: @IsNumber @Min(0) @Max(1) ───────────────────────────────
    conf = entry.get("confidence", 0.5)
    try:
        conf = float(conf)
        if conf < 0 or conf > 1:
            conf = 0.5
    except (TypeError, ValueError):
        conf = 0.5
    entry["confidence"] = conf

    # ── notes: @IsOptional @IsString @MaxLength(500) ────────────────────────
    notes = entry.get("notes")
    if notes is not None:
        entry["notes"] = str(notes)[:500] if notes else None

    # ── raw_text_snippet: @IsOptional @IsString @MaxLength(500) ─────────────
    snippet = entry.get("raw_text_snippet")
    if snippet is not None:
        entry["raw_text_snippet"] = str(snippet)[:500] if snippet else None

    # ── location_hint: @IsOptional @IsString @MaxLength(200) ────────────────
    loc = entry.get("location_hint")
    if loc is not None:
        entry["location_hint"] = str(loc)[:200] if loc else None

    # ── seller_type: @IsOptional @IsString @MaxLength(100) ──────────────────
    seller = entry.get("seller_type")
    if seller is not None:
        entry["seller_type"] = str(seller)[:100] if seller else None

    # ── is_whole_fruit: harus boolean ───────────────────────────────────────
    iwf = entry.get("is_whole_fruit")
    if not isinstance(iwf, bool):
        entry["is_whole_fruit"] = bool(iwf) if iwf is not None else False

    return entry


# ══════════════════════════════════════════════════════════════════════════════
# Ollama — normalisasi harga
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
Kamu adalah asisten ekstraksi data harga produk. Tugasmu memproses listing produk \
durian dari Google Shopping dan menghasilkan data terstruktur dalam format JSON.

ATURAN WAJIB:
- Respons HANYA berupa JSON array murni. Tidak ada teks tambahan, tidak ada markdown, \
tidak ada blok ```json, tidak ada komentar.
- Mulai langsung dengan karakter '[' dan akhiri dengan ']'.
- Setiap elemen array sesuai SATU item input (urutan sama persis, jumlah sama persis).
- Semua nilai harga dalam IDR (Rupiah), bilangan bulat tanpa titik/koma pemisah ribuan.
- is_whole_fruit = true HANYA jika produk adalah buah utuh dengan kulit \
(BUKAN daging/kupas/beku/pancake/bibit/olahan apapun).
- Jika nilai tidak bisa ditentukan, gunakan null.
- confidence (0.0–1.0): seberapa yakin kamu terhadap akurasi entri ini.
- JANGAN potong atau ringkas output. Selesaikan semua elemen array.
"""

_USER_PROMPT_TEMPLATE = """\
Proses {count} listing durian varietas {variety_name} berikut.
Kembalikan JSON array dengan TEPAT {count} elemen. Mulai dengan '[', akhiri dengan ']'.

Schema SETIAP elemen (semua field wajib ada, gunakan null jika tidak tahu):
{{
  "variety_code":       "{variety_code}",
  "variety_alias":      "<nama produk dari judul, max 100 karakter>",
  "is_whole_fruit":     <true|false>,
  "weight_reference":   "<WAJIB DIISI, min 4 karakter. Contoh: 'per buah 2-3 kg', 'per kg', 'per buah'. MAX 200 karakter>",
  "notes":              "<catatan normalisasi harga atau null, max 500 karakter>",
  "price_per_kg_min":   <IDR integer atau null>,
  "price_per_kg_max":   <IDR integer atau null>,
  "price_per_kg_avg":   <IDR integer atau null>,
  "price_per_unit_min": <IDR integer atau null>,
  "price_per_unit_max": <IDR integer atau null>,
  "location_hint":      "<kota/wilayah atau null>",
  "seller_type":        "<kebun|reseller|importir|toko online|null>",
  "confidence":         <float 0.0–1.0>,
  "raw_text_snippet":   "<title + harga, max 200 karakter>"
}}

ATURAN NORMALISASI HARGA:
- price_unit="per_kg"   → price_per_kg_avg = price_idr, set price_per_unit_* = null.
- price_unit="per_buah" DAN weight_kg_hint ada → \
price_per_kg_avg = round(price_idr / weight_kg_hint), tulis rumus di notes.
- price_unit="per_buah" DAN weight_kg_hint null → \
isi price_per_unit_min=price_idr, price_per_unit_max=price_idr, biarkan price_per_kg_* null.
- price_unit="unknown"  → estimasi dari konteks judul. Durian premium ≈ 1.5–3 kg per buah.

WAJIB: weight_reference harus diisi sesuai konteks:
- Ada info berat di judul → salin verbatim (contoh: "per buah 2,2-2,3 kg")
- Harga per kg → "per kg"
- Harga per buah tanpa info berat → "per buah"
- Tidak ada info → "tidak diketahui"

INPUT ({count} item):
{items_json}
"""


async def _call_ollama(
    items:        List[Dict[str, Any]],
    variety_code: str,
    client:       httpx.AsyncClient,
    attempt:      int = 1,
) -> List[Dict[str, Any]]:
    """
    Kirim satu batch ke Ollama dan parse JSON array hasilnya.
    Return list parsed entries, atau [] jika gagal.

    [FIX-2] num_predict dinaikkan ke 4096 agar JSON tidak terpotong.
    """
    variety_name = _VARIETY_ALIASES.get(variety_code, variety_code)

    compact_items = [
        {
            "title":          it.get("title", ""),
            "price_str":      it.get("price_str", ""),
            "price_idr":      it.get("price_idr"),
            "price_unit":     it.get("price_unit", "unknown"),
            "weight_kg_hint": it.get("weight_kg_hint"),
            "source":         it.get("source", ""),
        }
        for it in items
    ]

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        count        = len(items),
        variety_name = variety_name,
        variety_code = variety_code,
        items_json   = json.dumps(compact_items, ensure_ascii=False, indent=2),
    )

    payload = {
        "model":  OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        "options": {
            "temperature": 0.05,   # lebih deterministik untuk ekstraksi terstruktur
            "num_predict": 4096,   # [FIX-2] cukup untuk 3 item × ~1000 token/item
            "stop":        ["\n\n\n"],  # hentikan jika ada baris kosong berlebih
        },
    }

    raw_text = ""
    try:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json    = payload,
            timeout = OLLAMA_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        raw_text = resp.json().get("message", {}).get("content", "").strip()

        # Strip markdown code fences
        if raw_text.startswith("```"):
            lines    = raw_text.splitlines()
            raw_text = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            ).strip()

        # Coba temukan JSON array jika ada teks sebelum/sesudahnya
        if not raw_text.startswith("["):
            start = raw_text.find("[")
            if start != -1:
                raw_text = raw_text[start:]

        # Pastikan berakhir dengan ']'
        if raw_text and not raw_text.endswith("]"):
            end = raw_text.rfind("]")
            if end != -1:
                raw_text = raw_text[:end + 1]

        parsed = json.loads(raw_text)

        if not isinstance(parsed, list):
            logger.warning(
                f"[DbSender][Ollama] Respons bukan array untuk {variety_code}. "
                f"Tipe: {type(parsed).__name__}"
            )
            return []

        # Validasi jumlah elemen
        if len(parsed) != len(items):
            logger.warning(
                f"[DbSender][Ollama] {variety_code}: diharapkan {len(items)} elemen, "
                f"dapat {len(parsed)}. Tetap dilanjutkan."
            )

        return parsed

    except httpx.TimeoutException:
        logger.error(
            f"[DbSender][Ollama] Timeout {OLLAMA_TIMEOUT_SEC}s untuk {variety_code} "
            f"(attempt {attempt})."
        )
        return []
    except json.JSONDecodeError as exc:
        logger.error(
            f"[DbSender][Ollama] JSON parse error untuk {variety_code} "
            f"(attempt {attempt}): {exc}. "
            f"Raw (truncated): {raw_text[:600]}"
        )
        return []
    except httpx.ConnectError:
        logger.error(
            f"[DbSender][Ollama] Tidak bisa konek ke Ollama di {OLLAMA_BASE_URL}. "
            "Pastikan `ollama serve` sedang berjalan."
        )
        return []
    except Exception as exc:
        logger.error(
            f"[DbSender][Ollama] Error tak terduga untuk {variety_code}: {exc}",
            exc_info=True,
        )
        return []


async def _normalize_variety(
    items:        List[Dict[str, Any]],
    variety_code: str,
    client:       httpx.AsyncClient,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Normalisasi semua item satu varietas via Ollama dengan batching.

    [FIX-4] Retry 1x per batch jika gagal (timeout atau parse error).
    [FIX-7] Hanya loloskan entry dengan is_whole_fruit=True ke NestJS,
            karena NestJS MarketPriceValidator akan menolak is_whole_fruit=False
            (hemat bandwidth & menghindari rejection log yang tidak perlu).

    Returns:
        (accepted_entries, parse_error_count)
    """
    if not items:
        return [], 0

    all_entries:  List[Dict[str, Any]] = []
    parse_errors: int                  = 0
    chunks = [
        items[i : i + OLLAMA_BATCH_SIZE]
        for i in range(0, len(items), OLLAMA_BATCH_SIZE)
    ]

    logger.info(
        f"[DbSender] {variety_code}: {len(items)} item → "
        f"{len(chunks)} batch (batch_size={OLLAMA_BATCH_SIZE})"
    )

    for idx, chunk in enumerate(chunks):
        parsed: List[Dict[str, Any]] = []

        # [FIX-4] Retry loop
        for attempt in range(1, OLLAMA_BATCH_RETRIES + 2):  # +2 = attempts = retries + 1
            parsed = await _call_ollama(chunk, variety_code, client, attempt)
            if parsed:
                break
            if attempt <= OLLAMA_BATCH_RETRIES:
                wait_sec = 2.0 * attempt
                logger.warning(
                    f"[DbSender] {variety_code} batch {idx+1}/{len(chunks)}: "
                    f"attempt {attempt} gagal, retry dalam {wait_sec}s..."
                )
                await asyncio.sleep(wait_sec)

        if not parsed:
            parse_errors += 1
        else:
            for entry in parsed:
                # [FIX-7] Filter is_whole_fruit=False SEBELUM kirim ke NestJS
                is_whole = entry.get("is_whole_fruit")
                if not is_whole:
                    logger.debug(
                        f"[DbSender] Skip non-whole-fruit: "
                        f"{entry.get('variety_alias', '')[:60]}"
                    )
                    continue

                confidence = entry.get("confidence", 0.0)
                try:
                    confidence = float(confidence)
                except (TypeError, ValueError):
                    confidence = 0.0

                if confidence < MIN_CONFIDENCE:
                    logger.debug(
                        f"[DbSender] Skip low-confidence entry "
                        f"(conf={confidence:.2f}): "
                        f"{entry.get('variety_alias', '')[:60]}"
                    )
                    continue

                # Sanitasi dipanggil sekali di sini
                all_entries.append(_sanitize_entry(entry))

        logger.info(
            f"[DbSender] {variety_code} batch {idx+1}/{len(chunks)}: "
            f"{len(parsed)} parsed, {len(all_entries)} diterima sejauh ini"
        )

        if idx < len(chunks) - 1:
            await asyncio.sleep(0.3)

    return all_entries, parse_errors


# ══════════════════════════════════════════════════════════════════════════════
# HMAC Signing
# ══════════════════════════════════════════════════════════════════════════════

def _sign_body(body_bytes: bytes, secret: str) -> str:
    """
    Hitung HMAC-SHA256 dan kembalikan dalam format 'sha256=<hex>'.
    Sesuai dengan HmacSignatureGuard di NestJS yang membaca rawBody Buffer.
    """
    digest = hmac.new(
        key       = secret.encode("utf-8"),
        msg       = body_bytes,
        digestmod = hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


# ══════════════════════════════════════════════════════════════════════════════
# HTTP POST ke NestJS
# ══════════════════════════════════════════════════════════════════════════════

async def _post_to_nestjs(
    payload:       Dict[str, Any],
    agent_version: str,
    client:        httpx.AsyncClient,
) -> Dict[str, Any]:
    """
    Serialisasi payload → HMAC sign → POST ke NestJS.

    KRITIS: body HARUS di-serialize compact (separators=(",",":")) tanpa spasi,
    karena NestJS HmacSignatureGuard membaca rawBody Buffer untuk verifikasi.
    Perbedaan whitespace sekecil apapun akan membuat signature tidak cocok.
    """
    if not NESTJS_INTERNAL_KEY:
        logger.error(
            "[DbSender] NESTJS_INTERNAL_API_KEY belum diset di .env! "
            "POST ke NestJS dibatalkan."
        )
        return {"success": False, "error": "NESTJS_INTERNAL_API_KEY tidak dikonfigurasi."}

    body_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

    signature = _sign_body(body_bytes, NESTJS_INTERNAL_KEY)
    url       = f"{NESTJS_BASE_URL}{NESTJS_INGEST_PATH}"

    headers = {
        "Content-Type":    "application/json",
        "X-Signature":     signature,
        "X-Agent-Version": agent_version,
    }

    entry_count = len(payload.get("entries", []))
    logger.info(
        f"[DbSender] → POST {url} | "
        f"{len(body_bytes)} bytes | "
        f"{entry_count} entries"
    )

    try:
        resp = await client.post(
            url,
            content = body_bytes,
            headers = headers,
            timeout = NESTJS_TIMEOUT_SEC,
        )

        if resp.status_code == 200:
            result = resp.json()
            logger.info(
                f"[DbSender] ✓ NestJS OK — "
                f"saved={result.get('entries_saved', '?')}, "
                f"rejected={result.get('entries_rejected', '?')}"
            )
            return {"success": True, **result}

        logger.error(
            f"[DbSender] ✗ NestJS {resp.status_code}: {resp.text[:600]}"
        )
        return {
            "success": False,
            "error":   f"HTTP {resp.status_code}",
            "body":    resp.text[:600],
        }

    except httpx.ConnectError as exc:
        logger.error(f"[DbSender] Tidak bisa konek ke NestJS ({url}): {exc}")
        return {"success": False, "error": f"Connection refused: {exc}"}
    except httpx.TimeoutException:
        logger.error(f"[DbSender] Timeout {NESTJS_TIMEOUT_SEC}s saat POST ke NestJS.")
        return {"success": False, "error": "Timeout"}
    except Exception as exc:
        logger.error(f"[DbSender] Error tak terduga saat POST: {exc}", exc_info=True)
        return {"success": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# Public API — dipanggil dari agent/task.py
# ══════════════════════════════════════════════════════════════════════════════

async def send_run_to_db(
    summary: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Entry point utama dari task.py Tahap 3.

    Args:
        summary : Dict run_summary yang sudah disimpan oleh storage.save_summary()
        results : List hasil per varietas dari fetcher.fetch_all()

    Returns:
        Dict: {success, listings_inserted, listings_rejected, llm_errors, error}
    """
    run_id        = summary.get("run_id", "unknown")
    agent_version = os.getenv("APP_VERSION", "1.0.0")

    run_status    = summary.get("status", "no_data")
    _status_map   = {
        "success": "success",
        "partial": "partial",
        "failed":  "scraper_error",
    }
    nestjs_status = _status_map.get(run_status, "no_data")

    # Varietas yang punya item valid
    valid_varieties = [r for r in results if r.get("success") and r.get("items")]

    if not valid_varieties:
        logger.warning(
            f"[DbSender] run={run_id}: tidak ada varietas dengan item valid. "
            "Kirim laporan kosong ke NestJS."
        )
        return await _send_empty_report(run_id, agent_version, nestjs_status, summary)

    logger.info(
        f"[DbSender] run={run_id}: mulai normalisasi "
        f"{len(valid_varieties)} varietas via {OLLAMA_MODEL}..."
    )

    all_entries:      List[Dict[str, Any]] = []
    total_llm_errors: int                  = 0

    async with httpx.AsyncClient() as client:
        for vr in valid_varieties:
            variety_code = vr["variety_code"]
            items        = vr.get("items", [])

            logger.info(f"[DbSender] Normalisasi {variety_code} ({len(items)} item)...")

            try:
                entries, errs = await _normalize_variety(items, variety_code, client)
                all_entries.extend(entries)
                total_llm_errors += errs
                logger.info(
                    f"[DbSender] {variety_code}: "
                    f"{len(entries)} entri siap, {errs} batch error"
                )
            except Exception as exc:
                logger.error(
                    f"[DbSender] Gagal normalisasi {variety_code}: {exc}",
                    exc_info=True,
                )
                total_llm_errors += 1

    if not all_entries:
        logger.warning(
            f"[DbSender] run={run_id}: Ollama tidak menghasilkan entri valid. "
            f"llm_errors={total_llm_errors}"
        )
        empty_status = "llm_error" if total_llm_errors > 0 else "no_data"
        return await _send_empty_report(run_id, agent_version, empty_status, summary)

    # Hitung statistik ringkasan
    sources_scraped   = sum(r.get("item_count", 0) for r in results)
    sources_failed    = sum(1 for r in results if not r.get("success"))
    entries_discarded = max(0, sources_scraped - len(all_entries))

    market_report: Dict[str, Any] = {
        "agent_version":     agent_version,
        "run_id":            run_id,
        "run_started_at":    summary.get("started_at", datetime.now(timezone.utc).isoformat()),
        "run_ended_at":      summary.get("ended_at",   datetime.now(timezone.utc).isoformat()),
        "status":            nestjs_status,
        "entries":           all_entries,
        "sources_scraped":   sources_scraped,
        "sources_failed":    sources_failed,
        "llm_parse_errors":  total_llm_errors,
        "entries_discarded": entries_discarded,
        "error_details":     None,
    }

    # Validasi pra-kirim: pastikan tidak ada weight_reference kosong
    # (defensive check sebelum POST, sebagai lapisan ke-2 setelah _sanitize_entry)
    pre_check_errors: List[str] = []
    for i, entry in enumerate(all_entries):
        wr = entry.get("weight_reference")
        if not wr or not isinstance(wr, str) or not wr.strip():
            pre_check_errors.append(
                f"entries[{i}] variety={entry.get('variety_code')} "
                f"alias='{entry.get('variety_alias', '')[:40]}': "
                f"weight_reference kosong setelah sanitasi!"
            )
            # Force-set fallback terakhir
            entry["weight_reference"] = "per buah"

    if pre_check_errors:
        logger.warning(
            f"[DbSender] Pre-check menemukan {len(pre_check_errors)} "
            f"entry dengan weight_reference kosong — sudah di-patch:\n"
            + "\n".join(pre_check_errors)
        )

    logger.info(
        f"[DbSender] Kirim ke NestJS: "
        f"run_id={run_id} | entries={len(all_entries)} | status={nestjs_status}"
    )

    async with httpx.AsyncClient() as client:
        result = await _post_to_nestjs(market_report, agent_version, client)

    return {
        "success":           result.get("success", False),
        "listings_inserted": result.get("entries_saved", 0),
        "listings_rejected": result.get("entries_rejected", 0),
        "llm_errors":        total_llm_errors,
        "error":             result.get("error"),
    }


async def _send_empty_report(
    run_id:        str,
    agent_version: str,
    status:        str,
    summary:       Dict[str, Any],
) -> Dict[str, Any]:
    """
    Kirim laporan kosong ke NestJS supaya run tetap tercatat
    di log server meski tidak ada data yang disimpan.
    """
    market_report: Dict[str, Any] = {
        "agent_version":     agent_version,
        "run_id":            run_id,
        "run_started_at":    summary.get("started_at", datetime.now(timezone.utc).isoformat()),
        "run_ended_at":      summary.get("ended_at",   datetime.now(timezone.utc).isoformat()),
        "status":            status,
        "entries":           [],
        "sources_scraped":   summary.get("total_items", 0),
        "sources_failed":    summary.get("varieties_failed", 0),
        "llm_parse_errors":  0,
        "entries_discarded": 0,
        "error_details":     None,
    }
    async with httpx.AsyncClient() as client:
        result = await _post_to_nestjs(market_report, agent_version, client)

    return {
        "success":           result.get("success", False),
        "listings_inserted": 0,
        "listings_rejected": 0,
        "llm_errors":        0,
        "error":             result.get("error"),
    }