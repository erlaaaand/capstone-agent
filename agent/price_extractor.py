# agent/price_extractor.py
#
# Perubahan dari versi sebelumnya:
#   - _detect_price_unit() SELALU dijalankan ulang dari judul — tidak percaya
#     nilai price_unit dari fetcher, karena fetcher bisa salah (kasus D13 "pcs"
#     yang tidak terdeteksi karena ada separator pipe sebelumnya).
#   - Tambah logika inferensi "whole_fruit_per_buah": jika judul mengandung
#     sinyal buah utuh ("utuh", "bulat", "fresh", "berkulit") DAN harga masuk
#     rentang wajar per-buah varietas → asumsikan per_buah daripada dibuang.
#     Ini menyelamatkan listing D24 yang valid tapi tidak punya kata "per buah".
#   - Confidence sedikit dikurangi untuk inferensi whole_fruit (0.70, bukan 0.80)
#     agar IQR di NestJS masih bisa menangkap jika ada outlier masuk.

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger("agent.price_extractor")

# ── Regex word boundary untuk kode varietas yang ambigu ──────────────────────
# "d2" sebagai substring cocok dengan "d214", "d24", "d2000", dll.
# Harus pakai \b agar hanya cocok dengan "d2" yang berdiri sendiri.
_D2_WORD_BOUNDARY = re.compile(r'\bd2\b', re.I)

# ── Nama standar varietas ─────────────────────────────────────────────────────
VARIETY_ALIAS: Dict[str, str] = {
    "D197": "Musang King",
    "D13":  "Golden Bun",
    "D24":  "Sultan / D24",
    "D2":   "Dato Nina",
}

# ── Estimasi berat buah utuh (kg) ─────────────────────────────────────────────
VARIETY_WEIGHT_ESTIMATE: Dict[str, float] = {
    "D197": 2.0,
    "D13":  2.5,
    "D24":  1.5,   # D24 lebih kecil dari Musang King
    "D2":   2.0,
}

# ── Batas harga per BUAH yang masuk akal (IDR) ───────────────────────────────
VARIETY_UNIT_PRICE_BOUNDS: Dict[str, Tuple[float, float]] = {
    "D197": (350_000, 6_000_000),
    "D13":  (250_000, 5_000_000),
    "D24":  (100_000, 4_000_000),  # D24 bisa sangat murah untuk buah kecil
    "D2":   (200_000, 5_000_000),
}

# Confidence minimum — entry di bawah ini dibuang
MIN_CONFIDENCE = 0.70

# ── Sinyal produk olahan ──────────────────────────────────────────────────────
_PROCESSED_SIGNALS: frozenset = frozenset({
    "kupas", "dikupas", "flesh", "pulp", "daging", "frozen", "beku",
    "pancake", "biskuit", "kue", "cake", "pudding", "jelly", "extract",
    "juice", "dodol", "lempok", "bibit", "benih", "seedling",
    "sabun", "parfum", "lotion",
})

# ── Regex berat ───────────────────────────────────────────────────────────────
_WEIGHT_PATTERNS: List[re.Pattern] = [
    re.compile(r"(\d+[,.]?\d*)\s*[-–]\s*(\d+[,.]?\d*)\s*kg", re.I),
    re.compile(r"~?\s*(\d+[,.]?\d+)\s*kg", re.I),
    re.compile(r"\b(\d)\s*kg\b", re.I),
    re.compile(r"[(\[]\s*(\d+[,.]?\d*)\s*kg\s*[)\]]", re.I),
]

# ── Sinyal satuan per-kg ──────────────────────────────────────────────────────
_KG_SIGNALS = ["per kg", "per-kg", "/kg", "harga kg", "1 kg", "1kg", "per kilo"]

# ── Sinyal eksplisit per-buah ─────────────────────────────────────────────────
_BUAH_SIGNALS = [
    "per buah", "per biji", "1 buah", "1buah", "satu buah",
    "(l)", "(m)", "(s)", "(xl)", "pcs", "per pcs", "/pcs",
]

# ── Sinyal buah utuh (inferensi per-buah jika tidak ada sinyal eksplisit) ─────
# Jika judul mengandung salah satu sinyal ini DAN harga masuk range per-buah
# varietas → asumsikan per_buah dengan confidence lebih rendah.
_WHOLE_FRUIT_INFERENCE_SIGNALS = [
    "utuh", "bulat", "berkulit", "segar", "fresh", "whole",
    "impor", "import", "malaysia", "imported",
]

_RESELLER_SIGNALS = ["shopee", "tokopedia", "lazada", "bukalapak", "blibli"]
_IMPORTIR_SIGNALS = ["importir", "import", "impor"]
_KEBUN_SIGNALS    = ["kebun", "farm", "petani", "tani"]

_KOTA_INDONESIA = [
    "jakarta", "surabaya", "bandung", "medan", "semarang", "makassar",
    "palembang", "depok", "tangerang", "bekasi", "bogor", "batam",
    "pekanbaru", "denpasar", "yogyakarta", "malang", "solo", "banjarmasin",
    "padang", "pontianak", "samarinda", "balikpapan", "manado",
]


def _extract_weight_kg(text: str) -> Optional[float]:
    for pat in _WEIGHT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                groups = m.groups()
                if len(groups) == 2 and groups[1] is not None:
                    lo = float(groups[0].replace(",", "."))
                    hi = float(groups[1].replace(",", "."))
                    return round((lo + hi) / 2, 2)
                return round(float(groups[0].replace(",", ".")), 2)
            except (ValueError, TypeError):
                continue
    return None


def _detect_price_unit(title: str) -> str:
    """
    Deteksi satuan harga dari judul listing.

    Urutan prioritas:
    1. Sinyal per-kg eksplisit
    2. Sinyal per-buah eksplisit (termasuk "pcs", "per biji", dll)
    3. Ada info berat dalam kg → per_buah
    4. Semua gagal → "unknown"

    CATATAN: fungsi ini dipanggil FRESH dari judul di extract_entry(),
    tidak menggunakan nilai price_unit dari fetcher. Ini sengaja untuk
    menghindari false "unknown" dari fetcher (kasus D13 "pcs" tidak terdeteksi
    karena ada pipe separator sebelum kata "pcs").
    """
    t = title.lower()
    if any(kw in t for kw in _KG_SIGNALS):
        return "per_kg"
    if any(kw in t for kw in _BUAH_SIGNALS):
        return "per_buah"
    if _extract_weight_kg(title) is not None:
        return "per_buah"
    return "unknown"


def _infer_per_buah_from_whole_signals(
    title:        str,
    price_idr:    float,
    variety_code: str,
) -> bool:
    """
    Jika price_unit = "unknown" tapi judul mengandung sinyal buah utuh
    DAN harga berada dalam rentang wajar per-buah varietas,
    maka kita bisa inferensikan ini adalah harga per-buah.

    Ini menyelamatkan listing seperti:
      "Durian Malayd24 Super Premium Fresh Utuh Bulat" Rp250.000
    yang jelas buah utuh tapi tidak punya kata "per buah" atau "pcs".
    """
    t = title.lower()
    has_whole_signal = any(sig in t for sig in _WHOLE_FRUIT_INFERENCE_SIGNALS)
    if not has_whole_signal:
        return False

    lo, hi = VARIETY_UNIT_PRICE_BOUNDS.get(variety_code, (100_000, 10_000_000))
    return lo <= price_idr <= hi


def _detect_is_whole_fruit(title: str) -> bool:
    t = title.lower()
    return not any(sig in t for sig in _PROCESSED_SIGNALS)


def _detect_seller_type(source: str, title: str) -> Optional[str]:
    s = (source + " " + title).lower()
    if any(k in s for k in _KEBUN_SIGNALS):    return "kebun"
    if any(k in s for k in _IMPORTIR_SIGNALS): return "importir"
    if any(k in s for k in _RESELLER_SIGNALS): return "toko online"
    return "reseller"


def _extract_location(title: str) -> Optional[str]:
    t = title.lower()
    for kota in _KOTA_INDONESIA:
        if kota in t:
            return kota.title()
    return None


def _compute_confidence(
    variety_code:    str,
    title:           str,
    price_unit:      str,
    weight_kg:       Optional[float],
    is_whole:        bool,
    is_inferred:     bool,  # True jika unit di-infer dari sinyal whole_fruit
) -> float:
    score = 0.50
    t = title.lower()

    if price_unit == "per_buah" and not is_inferred:
        score += 0.20   # Satuan eksplisit
    elif price_unit == "per_buah" and is_inferred:
        score += 0.10   # Inferensi — lebih rendah
    elif price_unit == "per_kg":
        score += 0.10

    if weight_kg is not None:
        score += 0.15   # Ada berat di judul

    if is_whole:
        score += 0.10

    variety_kws = {
        "D197": ["musang king", "mao shan wang", "msw", "raja kunyit", "musangking"],
        "D13":  ["golden bun", "d13"],
        "D24":  ["d24", "sultan", "bukit merah", "malayd24", "malay d24"],
        # D2: "dato nina"/"datuk nina" aman sebagai substring.
        # "d2" HARUS pakai word boundary — "d214", "d24", dll mengandung "d2" sebagai substring.
        "D2":   ["dato nina", "datuk nina"],
    }
    has_variety_kw = any(kw in t for kw in variety_kws.get(variety_code, []))

    # Khusus D2: cek \bd2\b agar tidak false-match dengan D214, D24, dll
    if variety_code == "D2" and not has_variety_kw:
        has_variety_kw = bool(_D2_WORD_BOUNDARY.search(t))

    if has_variety_kw:
        score += 0.10
    else:
        score -= 0.15

    return round(max(0.0, min(1.0, score)), 2)


def _to_unit_price(
    price_idr:    float,
    price_unit:   str,
    weight_kg:    Optional[float],
    variety_code: str,
) -> Optional[Tuple[float, str, str]]:
    """
    Konversi harga listing ke harga per buah utuh.
    Returns (price_per_unit, weight_reference, notes) atau None.

      - per_buah              → langsung pakai price_idr
      - per_kg + ada berat    → kalkulasi price_idr × weight_kg
      - per_kg + tanpa berat  → BUANG (tidak bisa konversi akurat)
    """
    if price_unit == "per_buah":
        weight = weight_kg or VARIETY_WEIGHT_ESTIMATE.get(variety_code, 2.0)
        if weight_kg:
            return (
                price_idr,
                f"per buah {weight_kg} kg",
                f"Rp{price_idr:,.0f}/buah (berat dari judul: {weight_kg} kg)",
            )
        else:
            est = VARIETY_WEIGHT_ESTIMATE.get(variety_code, 2.0)
            return (
                price_idr,
                f"per buah ~{est} kg (estimasi)",
                f"Berat tidak ada di judul, pakai estimasi {est} kg untuk {variety_code}.",
            )

    if price_unit == "per_kg":
        if weight_kg is None:
            return None   # tidak bisa konversi tanpa berat
        unit_price = round(price_idr * weight_kg)
        return (
            unit_price,
            f"per kg × {weight_kg} kg",
            f"Rp{price_idr:,.0f}/kg × {weight_kg} kg = Rp{unit_price:,}/buah",
        )

    return None  # "unknown" tidak sampai sini, sudah di-handle di extract_entry


def extract_entry(
    item:         Dict[str, Any],
    variety_code: str,
) -> Optional[Dict[str, Any]]:
    """
    Konversi satu raw item fetcher → dict siap kirim ke NestJS.
    Returns None jika entry tidak layak.
    """
    title      = item.get("title", "").strip()
    price_idr  = item.get("price_idr")
    price_str  = item.get("price_str", "")
    source     = item.get("source", "Unknown")
    source_url = item.get("product_link") or item.get("source_url") or ""

    if not title or price_idr is None:
        return None

    try:
        price_idr = float(price_idr)
    except (TypeError, ValueError):
        return None

    # ── Re-detect price_unit dari judul (tidak percaya nilai dari fetcher) ───
    # Alasan: fetcher bisa menghasilkan "unknown" padahal judul punya sinyal
    # per-buah yang valid (contoh: "pcs" setelah separator pipe terlewat).
    weight_kg  = item.get("weight_kg_hint") or _extract_weight_kg(title)
    price_unit = _detect_price_unit(title)  # ← selalu fresh dari judul
    is_whole   = _detect_is_whole_fruit(title)
    is_inferred = False

    # ── Inferensi per_buah dari sinyal buah utuh ──────────────────────────────
    # Jika satuan masih "unknown" setelah deteksi normal, coba inferensi
    # dari sinyal buah utuh + rentang harga wajar.
    if price_unit == "unknown":
        if _infer_per_buah_from_whole_signals(title, price_idr, variety_code):
            price_unit  = "per_buah"
            is_inferred = True
            logger.debug(
                f"[Extractor] Inferensi per_buah dari sinyal whole_fruit: "
                f"'{title[:60]}' Rp{price_idr:,.0f}"
            )
        else:
            logger.debug(
                f"[Extractor] BUANG unknown unit (tidak ada inferensi): "
                f"'{title[:60]}' Rp{price_idr:,.0f}"
            )
            return None

    # ── Konversi ke harga per buah ────────────────────────────────────────────
    result = _to_unit_price(price_idr, price_unit, weight_kg, variety_code)
    if result is None:
        logger.debug(
            f"[Extractor] BUANG per_kg tanpa berat: '{title[:60]}'"
        )
        return None

    unit_price, weight_reference, notes = result

    if is_inferred:
        notes = f"[Inferensi per-buah dari sinyal whole_fruit] {notes}"

    # ── Filter harga per buah di luar batas wajar ─────────────────────────────
    lo, hi = VARIETY_UNIT_PRICE_BOUNDS.get(variety_code, (100_000, 10_000_000))
    if not (lo <= unit_price <= hi):
        logger.debug(
            f"[Extractor] BUANG out-of-range: {variety_code} "
            f"Rp{unit_price:,.0f}/buah (batas: Rp{lo:,.0f}–Rp{hi:,.0f})"
        )
        return None

    # ── Confidence ────────────────────────────────────────────────────────────
    confidence = _compute_confidence(
        variety_code, title, price_unit, weight_kg, is_whole, is_inferred
    )

    if confidence < MIN_CONFIDENCE:
        logger.debug(
            f"[Extractor] BUANG low confidence ({confidence:.2f}): '{title[:60]}'"
        )
        return None

    # ── Hitung price_per_kg_avg sebagai data sekunder ─────────────────────────
    if price_unit == "per_buah" and weight_kg:
        pkg_avg = round(unit_price / weight_kg)
    elif price_unit == "per_kg":
        pkg_avg = int(price_idr)
    else:
        est = VARIETY_WEIGHT_ESTIMATE.get(variety_code, 2.0)
        pkg_avg = round(unit_price / est)

    snippet = f"{title} | {price_str}"[:500]

    entry = {
        "variety_code":       variety_code,
        "variety_alias":      VARIETY_ALIAS.get(variety_code, variety_code),
        "is_whole_fruit":     is_whole,
        "weight_reference":   weight_reference,
        "notes":              notes,
        "price_per_unit_min": unit_price,
        "price_per_unit_max": unit_price,
        "price_per_kg_min":   None,
        "price_per_kg_max":   None,
        "price_per_kg_avg":   pkg_avg,
        "location_hint":      _extract_location(title),
        "seller_type":        _detect_seller_type(source, title),
        "confidence":         confidence,
        "raw_text_snippet":   snippet,
        "source_name":        source,
        "source_url":         source_url,
    }

    logger.debug(
        f"[Extractor] OK {variety_code}: '{title[:55]}' "
        f"→ Rp{unit_price:,.0f}/buah | unit={price_unit}"
        f"{'(inferred)' if is_inferred else ''} | conf={confidence:.2f}"
    )
    return entry


def process_variety_items(
    items:        List[Dict[str, Any]],
    variety_code: str,
) -> Tuple[List[Dict[str, Any]], int]:
    entries: List[Dict[str, Any]] = []
    errors = 0

    for item in items:
        try:
            entry = extract_entry(item, variety_code)
            if entry is not None:
                entries.append(entry)
            else:
                errors += 1
        except Exception as exc:
            logger.error(
                f"[Extractor] Error '{item.get('title', '')[:60]}': {exc}",
                exc_info=True,
            )
            errors += 1

    logger.info(
        f"[Extractor] {variety_code}: "
        f"{len(entries)} valid, {errors} dibuang dari {len(items)} item"
    )
    return entries, errors