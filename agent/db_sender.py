# agent/db_sender.py
#
# Pipeline: raw fetcher items → Ollama LLM (normalisasi) → MarketReportDto → HMAC sign → POST NestJS
#
# Flow per run:
#   send_run_to_db(summary, results)
#     └─ untuk setiap varietas yang success:
#          └─ _normalize_variety(items, variety_code) via Ollama (dibatch per BATCH_SIZE item)
#               └─ _post_to_nestjs(market_report_dto) → POST /api/v1/ai-integration/market-report

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import httpx

from core import config
from core.logger import get_logger

logger = get_logger("agent.db_sender")

# ══════════════════════════════════════════════════════════════════════════════
# Konfigurasi (baca dari env, dengan fallback yang aman)
# ══════════════════════════════════════════════════════════════════════════════

NESTJS_BASE_URL:     str   = os.getenv("NESTJS_BASE_URL", "http://localhost:3001")
NESTJS_INTERNAL_KEY: str   = os.getenv("NESTJS_INTERNAL_API_KEY", "")
NESTJS_INGEST_PATH:  str   = "/api/v1/ai-integration/market-report"
NESTJS_TIMEOUT_SEC:  int   = int(os.getenv("NESTJS_TIMEOUT_SEC", "30"))

OLLAMA_BASE_URL:     str   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL:        str   = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT_SEC:  int   = int(os.getenv("OLLAMA_TIMEOUT_SEC", "180"))

# qwen2.5:7b stabil dengan 3 item per batch.
# Lebih dari itu berisiko timeout atau output terpotong.
OLLAMA_BATCH_SIZE:   int   = int(os.getenv("OLLAMA_BATCH_SIZE", "3"))

# Confidence minimum dari Ollama agar entri diteruskan ke NestJS
MIN_CONFIDENCE:      float = float(os.getenv("DB_MIN_CONFIDENCE", "0.5"))

# ── Mapping variety_code → nama panjang untuk context LLM ───────────────────
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
    Pastikan semua field wajib NestJS terisi dengan nilai default yang aman,
    supaya ValidationPipe tidak reject karena field kosong atau tipe salah.
    Dipanggil sekali per entry, di dalam _normalize_variety.
    """
    # weight_reference: wajib non-empty string (max 200 char)
    weight_ref = entry.get("weight_reference")
    if not weight_ref or not isinstance(weight_ref, str) or not weight_ref.strip():
        # Rekonstruksi dari field harga yang tersedia
        if entry.get("price_per_kg_avg") or entry.get("price_per_kg_min") or entry.get("price_per_kg_max"):
            weight_ref = "per kg"
        elif entry.get("price_per_unit_min") or entry.get("price_per_unit_max"):
            weight_ref = "per buah"
        else:
            weight_ref = "tidak diketahui"
    entry["weight_reference"] = weight_ref[:200]

    # variety_alias: wajib non-empty string (max 100 char)
    alias = entry.get("variety_alias")
    if not alias or not isinstance(alias, str) or not alias.strip():
        entry["variety_alias"] = entry.get("variety_code", "unknown")
    entry["variety_alias"] = entry["variety_alias"][:100]

    # confidence: harus float 0.0–1.0
    conf = entry.get("confidence", 0.5)
    if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
        entry["confidence"] = 0.5

    # notes: opsional, tapi jika ada max 500 char
    notes = entry.get("notes")
    if notes and isinstance(notes, str) and len(notes) > 500:
        entry["notes"] = notes[:500]

    # raw_text_snippet: opsional, tapi jika ada max 500 char
    snippet = entry.get("raw_text_snippet")
    if snippet and isinstance(snippet, str) and len(snippet) > 500:
        entry["raw_text_snippet"] = snippet[:500]

    return entry


# ══════════════════════════════════════════════════════════════════════════════
# Ollama — normalisasi harga
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
Kamu adalah asisten ekstraksi data harga produk. Tugasmu memproses listing produk \
durian dari Google Shopping dan menghasilkan data terstruktur dalam format JSON.

ATURAN WAJIB:
- Respons HANYA berupa JSON array. Tidak ada teks, tidak ada markdown, tidak ada komentar.
- Setiap elemen array sesuai SATU item input (urutan harus sama persis).
- Semua nilai harga dalam IDR (Rupiah), bilangan bulat tanpa titik/koma.
- is_whole_fruit = true HANYA jika produk adalah buah utuh dengan kulit \
(bukan daging/kupas/beku/pancake/bibit/olahan apapun).
- Normalisasi ke harga per-kg menggunakan weight_kg_hint jika tersedia; \
tulis rumus matematisnya di field notes.
- Jika nilai tidak bisa ditentukan, gunakan null.
- confidence (0.0–1.0): seberapa yakin kamu terhadap akurasi entri ini secara keseluruhan.
"""

_USER_PROMPT_TEMPLATE = """\
Proses {count} listing durian varietas {variety_name} berikut ini.
Kembalikan JSON array dengan tepat {count} elemen.

Schema tiap elemen:
{{
  "variety_code":       "{variety_code}",
  "variety_alias":      "<nama produk dari judul listing, max 100 karakter>",
  "is_whole_fruit":     <true|false>,
  "weight_reference":   "<deskripsi berat dari listing, contoh: 'per buah 2-3 kg', WAJIB DIISI, max 200 karakter>",
  "notes":              "<chain-of-thought normalisasi harga, atau null>",
  "price_per_kg_min":   <IDR integer atau null>,
  "price_per_kg_max":   <IDR integer atau null>,
  "price_per_kg_avg":   <IDR integer atau null>,
  "price_per_unit_min": <IDR integer atau null>,
  "price_per_unit_max": <IDR integer atau null>,
  "location_hint":      "<kota/wilayah dari listing atau null>",
  "seller_type":        "<kebun|reseller|importir|toko online|null>",
  "confidence":         <float 0.0–1.0>,
  "raw_text_snippet":   "<gabungan title + harga, max 500 karakter>"
}}

PETUNJUK NORMALISASI HARGA:
- price_unit="per_kg"   → price_per_kg_avg = price_idr. Set price_per_unit_* = null.
- price_unit="per_buah" DAN weight_kg_hint ada  → price_per_kg_avg = round(price_idr / weight_kg_hint). Tulis rumusnya di notes.
- price_unit="per_buah" DAN weight_kg_hint null → isi price_per_unit_min/max saja, biarkan price_per_kg_* null.
- price_unit="unknown"  → gunakan konteks judul. Durian premium utuh umumnya 1.5–3 kg per buah.

PENTING: field weight_reference WAJIB diisi, jangan pernah null. \
Gunakan informasi berat dari judul listing, atau tulis "per buah" / "per kg" sesuai konteks.

INPUT:
{items_json}
"""


async def _call_ollama(
    items:        List[Dict[str, Any]],
    variety_code: str,
    client:       httpx.AsyncClient,
) -> List[Dict[str, Any]]:
    """
    Kirim satu batch ke Ollama dan parse JSON array hasilnya.
    Return list parsed entries, atau [] jika gagal.
    """
    variety_name = _VARIETY_ALIASES.get(variety_code, variety_code)

    # Buat representasi compact — hanya field yang relevan untuk LLM
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
            "temperature": 0.1,   # rendah = deterministik, cocok untuk ekstraksi terstruktur
            "num_predict": 2048,
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

        # Strip markdown code fences jika model membungkusnya
        if raw_text.startswith("```"):
            lines    = raw_text.splitlines()
            raw_text = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            ).strip()

        parsed = json.loads(raw_text)

        if not isinstance(parsed, list):
            logger.warning(
                f"[DbSender][Ollama] Respons bukan array untuk {variety_code}. "
                f"Tipe: {type(parsed).__name__}"
            )
            return []

        return parsed

    except httpx.TimeoutException:
        logger.error(
            f"[DbSender][Ollama] Timeout {OLLAMA_TIMEOUT_SEC}s untuk {variety_code}."
        )
        return []
    except json.JSONDecodeError as exc:
        logger.error(
            f"[DbSender][Ollama] JSON parse error untuk {variety_code}: {exc}. "
            f"Raw (truncated): {raw_text[:400]}"
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
    Sanitasi dilakukan di sini, satu kali per entry.

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
        logger.debug(
            f"[DbSender][Ollama] {variety_code} batch {idx+1}/{len(chunks)} "
            f"({len(chunk)} item)..."
        )
        parsed = await _call_ollama(chunk, variety_code, client)

        if not parsed:
            parse_errors += 1
        else:
            for entry in parsed:
                confidence = entry.get("confidence", 0.0)
                if not isinstance(confidence, (int, float)):
                    confidence = 0.0
                if confidence >= MIN_CONFIDENCE:
                    # Sanitasi dipanggil sekali di sini saja
                    all_entries.append(_sanitize_entry(entry))
                else:
                    logger.debug(
                        f"[DbSender] Skip low-confidence entry "
                        f"(conf={confidence:.2f}): "
                        f"{entry.get('variety_alias', '')[:60]}"
                    )

        logger.info(
            f"[DbSender] {variety_code} batch {idx+1}/{len(chunks)}: "
            f"{len(parsed)} parsed, {len(all_entries)} diterima sejauh ini"
        )

        # Jeda ringan antar batch supaya Ollama tidak di-spam
        if idx < len(chunks) - 1:
            await asyncio.sleep(0.5)

    return all_entries, parse_errors


# ══════════════════════════════════════════════════════════════════════════════
# HMAC Signing
# ══════════════════════════════════════════════════════════════════════════════

def _sign_body(body_bytes: bytes, secret: str) -> str:
    """
    Hitung HMAC-SHA256 dan kembalikan dalam format 'sha256=<hex>'.
    Format ini sesuai dengan yang di-expected oleh HmacSignatureGuard di NestJS.
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

    PENTING: body di-serialize dengan separators=(",", ":") (tanpa spasi)
    karena NestJS membaca rawBody Buffer untuk verifikasi HMAC.
    Perbedaan whitespace sekecil apapun akan membuat signature tidak cocok.
    """
    if not NESTJS_INTERNAL_KEY:
        logger.error(
            "[DbSender] NESTJS_INTERNAL_API_KEY belum diset di .env! "
            "POST ke NestJS dibatalkan."
        )
        return {"success": False, "error": "NESTJS_INTERNAL_API_KEY tidak dikonfigurasi."}

    # Serialisasi compact — HARUS konsisten dengan apa yang NestJS terima sebagai rawBody
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

    logger.info(
        f"[DbSender] → POST {url} | "
        f"{len(body_bytes)} bytes | "
        f"{len(payload.get('entries', []))} entries"
    )

    try:
        resp = await client.post(
            url,
            content = body_bytes,   # kirim bytes langsung, bukan json= param
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
            f"[DbSender] ✗ NestJS {resp.status_code}: {resp.text[:400]}"
        )
        return {"success": False, "error": f"HTTP {resp.status_code}", "body": resp.text[:400]}

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

    # Map status task.py → AgentRunStatus NestJS
    run_status    = summary.get("status", "no_data")
    _status_map   = {"success": "success", "partial": "partial", "failed": "scraper_error"}
    nestjs_status = _status_map.get(run_status, "no_data")

    # Varietas yang punya item valid
    valid_varieties = [r for r in results if r.get("success") and r.get("items")]

    if not valid_varieties:
        logger.warning(
            f"[DbSender] run={run_id}: tidak ada varietas dengan item valid. "
            f"Kirim laporan kosong ke NestJS."
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