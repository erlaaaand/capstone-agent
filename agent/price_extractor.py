# agent/price_extractor.py
#
# Modul ekstraksi & normalisasi harga berbasis regex — pengganti Ollama.
#
# Tugas utama:
#   1. Deteksi satuan harga (per_kg / per_buah / unknown)
#   2. Ekstraksi berat buah dari judul
#   3. Normalisasi ke price_per_kg_* dan price_per_unit_*
#   4. Mengisi semua field wajib NestJS ValidationPipe
#   5. Memberi variety_alias yang bersih (nama standar, bukan judul listing)
#
# Tidak ada dependensi eksternal selain standard library.

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger("agent.price_extractor")

# ══════════════════════════════════════════════════════════════════════════════
# Konstanta nama varietas standar
# ══════════════════════════════════════════════════════════════════════════════

VARIETY_ALIAS: Dict[str, str] = {
    "D197": "Musang King",
    "D13":  "Golden Bun",
    "D24":  "Sultan / D24",
    "D2":   "Dato Nina",
}

# Estimasi berat buah utuh per varietas (kg) — dipakai saat info berat tidak ada
VARIETY_WEIGHT_ESTIMATE: Dict[str, float] = {
    "D197": 2.0,   # Musang King: 1.5–2.5 kg, pakai 2.0
    "D13":  2.5,   # Golden Bun:  2–3 kg, pakai 2.5
    "D24":  2.0,   # Sultan D24:  1.5–2.5 kg, pakai 2.0
    "D2":   2.0,   # Dato Nina:   1.5–2.5 kg, pakai 2.0
}

# Batas harga masuk akal (IDR)
_MIN_PRICE: float = 50_000.0
_MAX_PRICE: float = 15_000_000.0

# ══════════════════════════════════════════════════════════════════════════════
# Sinyal produk olahan (→ is_whole_fruit = False)
# ══════════════════════════════════════════════════════════════════════════════

_PROCESSED_SIGNALS: frozenset = frozenset({
    "kupas", "dikupas", "flesh", "pulp", "daging", "frozen", "beku",
    "pancake", "biskuit", "kue", "cake", "pudding", "jelly", "extract",
    "juice", "dodol", "lempok", "bibit", "benih", "seedling",
    "sabun", "parfum", "lotion",
})

# ══════════════════════════════════════════════════════════════════════════════
# Regex pola berat
# ══════════════════════════════════════════════════════════════════════════════

_WEIGHT_PATTERNS: List[re.Pattern] = [
    # "2,2-2,3 kg" atau "2.0-2.1kg" — range, ambil rata-rata
    re.compile(r"(\d+[,.]?\d*)\s*[-–]\s*(\d+[,.]?\d*)\s*kg", re.I),
    # "~1,5kg" atau "2Kg" atau "1.5 kg"
    re.compile(r"~?\s*(\d+[,.]?\d+)\s*kg", re.I),
    # "2 kg" (angka bulat)
    re.compile(r"\b(\d)\s*kg\b", re.I),
    # "(2kg)" atau "[2kg]"
    re.compile(r"[(\[]\s*(\d+[,.]?\d*)\s*kg\s*[)\]]", re.I),
]


def _extract_weight_kg(text: str) -> Optional[float]:
    """Ekstrak berat dari teks. Kembalikan float kg atau None."""
    for pat in _WEIGHT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                groups = m.groups()
                if len(groups) == 2 and groups[1] is not None:
                    lo = float(groups[0].replace(",", "."))
                    hi = float(groups[1].replace(",", "."))
                    return round((lo + hi) / 2, 2)
                else:
                    return round(float(groups[0].replace(",", ".")), 2)
            except (ValueError, TypeError):
                continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Deteksi satuan harga
# ══════════════════════════════════════════════════════════════════════════════

_KG_SIGNALS    = ["per kg", "per-kg", "/kg", "harga kg", "1 kg", "1kg", "per kilo"]
_BUAH_SIGNALS  = [
    "per buah", "per biji", "1 buah", "1buah", "satu buah",
    "(l)", "(m)", "(s)", "(xl)", "pcs", "per pcs",
]


def _detect_price_unit(title: str) -> str:
    t = title.lower()
    if any(kw in t for kw in _KG_SIGNALS):
        return "per_kg"
    if any(kw in t for kw in _BUAH_SIGNALS):
        return "per_buah"
    if _extract_weight_kg(title) is not None:
        return "per_buah"
    return "unknown"


# ══════════════════════════════════════════════════════════════════════════════
# Deteksi is_whole_fruit
# ══════════════════════════════════════════════════════════════════════════════

def _detect_is_whole_fruit(title: str) -> bool:
    """
    True jika buah utuh berkulit. False jika ada sinyal produk olahan/kupas.
    Fetcher sudah memfilter, tapi kita double-check di sini.
    """
    t = title.lower()
    return not any(sig in t for sig in _PROCESSED_SIGNALS)


# ══════════════════════════════════════════════════════════════════════════════
# Deteksi seller_type dari nama toko/sumber
# ══════════════════════════════════════════════════════════════════════════════

_RESELLER_SIGNALS  = ["shopee", "tokopedia", "lazada", "bukalapak", "blibli"]
_IMPORTIR_SIGNALS  = ["importir", "import", "impor"]
_KEBUN_SIGNALS     = ["kebun", "farm", "petani", "tani"]


def _detect_seller_type(source: str, title: str) -> Optional[str]:
    s = (source + " " + title).lower()
    if any(k in s for k in _KEBUN_SIGNALS):
        return "kebun"
    if any(k in s for k in _IMPORTIR_SIGNALS):
        return "importir"
    if any(k in s for k in _RESELLER_SIGNALS):
        return "toko online"
    return "reseller"


# ══════════════════════════════════════════════════════════════════════════════
# Normalisasi harga utama
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_price(
    price_idr:  float,
    price_unit: str,
    weight_kg:  Optional[float],
    variety_code: str,
) -> Tuple[
    Optional[float],  # price_per_kg_min
    Optional[float],  # price_per_kg_max
    Optional[float],  # price_per_kg_avg
    Optional[float],  # price_per_unit_min
    Optional[float],  # price_per_unit_max
    str,              # weight_reference
    Optional[str],    # notes
]:
    """
    Normalisasi harga ke per-kg dan/atau per-buah.

    Aturan:
    - per_kg   → price_per_kg_avg = price_idr, unit = null
    - per_buah + weight → price_per_kg_avg = round(price / weight), unit = price_idr
    - per_buah + no weight → pakai estimasi berat varietas
    - unknown  → coba tebak dari nilai harga:
                 jika < 200k → kemungkinan per_kg
                 jika > 200k → kemungkinan per_buah
    """
    notes: Optional[str] = None

    if price_unit == "per_kg":
        return (
            price_idr, price_idr, price_idr,
            None, None,
            "per kg",
            None,
        )

    if price_unit == "per_buah":
        weight = weight_kg
        if weight is None:
            weight = VARIETY_WEIGHT_ESTIMATE.get(variety_code, 2.0)
            notes = (
                f"Berat tidak tersedia di judul. "
                f"Pakai estimasi {weight} kg untuk {variety_code}."
            )
        kg_price = round(price_idr / weight)
        weight_ref = f"per buah ~{weight} kg" if weight_kg is None else f"per buah {weight_kg} kg"
        calc_note = f"Rp{price_idr:,.0f}/buah ÷ {weight}kg = Rp{kg_price:,}/kg"
        if notes:
            notes = notes + " | " + calc_note
        else:
            notes = calc_note
        return (
            None, None, kg_price,
            price_idr, price_idr,
            weight_ref,
            notes,
        )

    # unknown — tebak dari nilai harga
    if price_idr <= 200_000:
        # Kemungkinan per kg
        notes = f"Satuan tidak diketahui. Diasumsikan per kg karena harga Rp{price_idr:,.0f} ≤ 200k."
        return (
            price_idr, price_idr, price_idr,
            None, None,
            "per kg (estimasi)",
            notes,
        )
    else:
        # Kemungkinan per buah
        weight = weight_kg or VARIETY_WEIGHT_ESTIMATE.get(variety_code, 2.0)
        kg_price = round(price_idr / weight)
        weight_ref = f"per buah ~{weight} kg (estimasi)"
        notes = (
            f"Satuan tidak diketahui. Diasumsikan per buah. "
            f"Rp{price_idr:,.0f} ÷ {weight}kg = Rp{kg_price:,}/kg."
        )
        return (
            None, None, kg_price,
            price_idr, price_idr,
            weight_ref,
            notes,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Public API — proses satu item
# ══════════════════════════════════════════════════════════════════════════════

def extract_entry(
    item:         Dict[str, Any],
    variety_code: str,
) -> Optional[Dict[str, Any]]:
    """
    Konversi satu raw item fetcher → dict siap kirim ke NestJS.

    Returns None jika harga tidak valid atau item tidak layak.
    """
    title      = item.get("title", "").strip()
    price_idr  = item.get("price_idr")
    price_str  = item.get("price_str", "")
    source     = item.get("source", "Unknown")
    source_url = item.get("product_link", "")

    if not title or price_idr is None:
        logger.debug(f"[Extractor] SKIP: judul kosong atau harga None. title='{title[:60]}'")
        return None

    try:
        price_idr = float(price_idr)
    except (TypeError, ValueError):
        logger.debug(f"[Extractor] SKIP: harga tidak bisa dikonversi: {price_idr}")
        return None

    if not (_MIN_PRICE <= price_idr <= _MAX_PRICE):
        logger.debug(
            f"[Extractor] SKIP: harga di luar range: Rp{price_idr:,.0f} "
            f"('{title[:60]}')"
        )
        return None

    # Deteksi properti
    weight_kg   = item.get("weight_kg_hint") or _extract_weight_kg(title)
    price_unit  = item.get("price_unit") or _detect_price_unit(title)
    is_whole    = _detect_is_whole_fruit(title)
    seller_type = _detect_seller_type(source, title)

    # Normalisasi harga
    (
        price_per_kg_min,
        price_per_kg_max,
        price_per_kg_avg,
        price_per_unit_min,
        price_per_unit_max,
        weight_reference,
        notes,
    ) = _normalize_price(price_idr, price_unit, weight_kg, variety_code)

    # Snippet ringkas untuk raw_text_snippet
    snippet = f"{title} | {price_str}"[:500]

    # Confidence: heuristik sederhana
    confidence = _compute_confidence(
        variety_code  = variety_code,
        title         = title,
        price_unit    = price_unit,
        weight_kg     = weight_kg,
        is_whole      = is_whole,
    )

    entry = {
        "variety_code":      variety_code,
        "variety_alias":     VARIETY_ALIAS.get(variety_code, variety_code),
        "is_whole_fruit":    is_whole,
        "weight_reference":  weight_reference,
        "notes":             notes,
        "price_per_kg_min":  price_per_kg_min,
        "price_per_kg_max":  price_per_kg_max,
        "price_per_kg_avg":  price_per_kg_avg,
        "price_per_unit_min": price_per_unit_min,
        "price_per_unit_max": price_per_unit_max,
        "location_hint":     _extract_location(title),
        "seller_type":       seller_type,
        "confidence":        confidence,
        "raw_text_snippet":  snippet,
        "source_name":       source,
        "source_url":        source_url or "",
    }

    logger.debug(
        f"[Extractor] OK {variety_code}: '{title[:60]}' | "
        f"Rp{price_idr:,.0f} [{price_unit}] → "
        f"kg_avg={price_per_kg_avg} unit_min={price_per_unit_min} "
        f"conf={confidence:.2f}"
    )

    return entry


# ══════════════════════════════════════════════════════════════════════════════
# Heuristik confidence
# ══════════════════════════════════════════════════════════════════════════════

def _compute_confidence(
    variety_code: str,
    title:        str,
    price_unit:   str,
    weight_kg:    Optional[float],
    is_whole:     bool,
) -> float:
    """
    Hitung skor kepercayaan 0.0–1.0 berdasarkan kelengkapan data.
    """
    score = 0.5  # base

    t = title.lower()

    # Satuan jelas → +0.2
    if price_unit in ("per_kg", "per_buah"):
        score += 0.2

    # Ada info berat → +0.1
    if weight_kg is not None:
        score += 0.1

    # Buah utuh → +0.1
    if is_whole:
        score += 0.1

    # Nama varietas ada di judul → +0.1
    variety_mentions = {
        "D197": ["musang king", "mao shan wang", "msw", "raja kunyit", "musangking"],
        "D13":  ["golden bun", "d13"],
        "D24":  ["d24", "sultan", "bukit merah"],
        "D2":   ["dato nina", "datuk nina", "d2"],
    }
    kws = variety_mentions.get(variety_code, [])
    if any(kw in t for kw in kws):
        score += 0.1
    else:
        # Varietas tidak tersebut di judul → kurangi confidence
        score -= 0.15

    return round(max(0.0, min(1.0, score)), 2)


# ══════════════════════════════════════════════════════════════════════════════
# Ekstraksi lokasi dari judul (best-effort)
# ══════════════════════════════════════════════════════════════════════════════

_KOTA_INDONESIA = [
    "jakarta", "surabaya", "bandung", "medan", "semarang", "makassar",
    "palembang", "depok", "tangerang", "bekasi", "bogor", "batam",
    "pekanbaru", "denpasar", "yogyakarta", "malang", "solo", "banjarmasin",
    "padang", "pontianak", "samarinda", "balikpapan", "manado",
]


def _extract_location(title: str) -> Optional[str]:
    t = title.lower()
    for kota in _KOTA_INDONESIA:
        if kota in t:
            return kota.title()
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Public API — proses satu varietas (list of items)
# ══════════════════════════════════════════════════════════════════════════════

def process_variety_items(
    items:        List[Dict[str, Any]],
    variety_code: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Proses semua item satu varietas.

    Returns:
        (entries, error_count)
        - entries: list dict siap kirim ke NestJS
        - error_count: jumlah item yang gagal diproses
    """
    entries: List[Dict[str, Any]] = []
    errors  = 0

    for item in items:
        try:
            entry = extract_entry(item, variety_code)
            if entry is not None:
                entries.append(entry)
            else:
                errors += 1
        except Exception as exc:
            logger.error(
                f"[Extractor] Error proses item '{item.get('title', '')[:60]}': {exc}",
                exc_info=True,
            )
            errors += 1

    logger.info(
        f"[Extractor] {variety_code}: "
        f"{len(entries)} entri valid, {errors} gagal dari {len(items)} item"
    )
    return entries, errors