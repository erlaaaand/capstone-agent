# agent/queries.py
#
# Definisi target pencarian Google Shopping per varietas durian.
# Query dirancang untuk mendapatkan BUAH UTUH BERKULIT saja.
#
# Perbaikan dari log error 2026-06-08:
#   [FIX-Q1] Query diselaraskan kembali dengan _WHOLE_FRUIT_SIGNALS di fetcher.py
#            Log lama memakai query "jual durian musang king buah utuh segar per buah"
#            yang masih menghasilkan 21 item valid → query baru harus setidaknya
#            mengandung sinyal yang sama.
#   [FIX-Q2] D13 (Golden Bun): query lama 100% reject (40/40).
#            Root cause: "golden bun" dan "D13" hampir tidak ada di Google Shopping ID.
#            Query baru mencoba kata kunci yang lebih umum dipakai seller lokal.
#   [FIX-Q3] D2 (Dato Nina): query lama 100% reject — sama kasusnya dengan D13.
#            Query baru pakai kata yang lebih ditemukan di marketplace.
#   [FIX-Q4] min_results D197 naik ke 5 karena variasinya banyak di pasaran.
#
# PRINSIP QUERY:
#   - Kata kunci harus cocok dengan _WHOLE_FRUIT_SIGNALS di fetcher.py:
#     {"utuh", "berkulit", "segar", "bulat", "per buah", "per biji",
#      "1 buah", "1buah", "whole", "fresh", "import"}
#   - Jangan terlalu panjang (>6 kata) — Google Shopping cenderung memperluas terlalu jauh.
#   - Biarkan filter fetcher._is_valid_item() yang menyaring produk non-utuh.

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class DurianQuery:
    """
    Satu target pencarian Google Shopping untuk satu varietas durian.

    Attributes:
        variety_code   : Kode identifikasi varietas (D2 / D13 / D24 / D197).
        variety_name   : Nama lengkap untuk logging dan output JSON.
        search_queries : Daftar query dicoba berurutan. Pertama = utama; berikutnya = fallback.
        min_results    : Batas minimum item lolos filter agar query dianggap cukup.
        num_results    : Jumlah hasil yang diminta ke SerpApi (max 100).
        gl             : Google country code.
        hl             : Google language code.
    """
    variety_code:   str
    variety_name:   str
    search_queries: List[str]
    min_results:    int = 3
    num_results:    int = 40
    gl:             str = "id"
    hl:             str = "id"

    def __post_init__(self) -> None:
        if not self.variety_code.strip():
            raise ValueError("variety_code tidak boleh kosong.")
        if not self.search_queries:
            raise ValueError("search_queries tidak boleh kosong.")
        if self.min_results < 1:
            raise ValueError("min_results harus >= 1.")
        if not 1 <= self.num_results <= 100:
            raise ValueError("num_results harus 1–100.")


# ── 4 Varietas Target ─────────────────────────────────────────────────────────

DURIAN_QUERIES: List[DurianQuery] = [

    # [FIX-Q1] D197 Musang King
    # Log lama: "jual durian musang king buah utuh segar per buah" → 21/40 valid ✓
    # Query baru mempertahankan kata "utuh" + "segar" yang jadi sinyal _WHOLE_FRUIT_SIGNALS.
    DurianQuery(
        variety_code = "D197",
        variety_name = "Musang King / Raja Kunyit / Mao Shan Wang",
        search_queries = [
            "durian musang king utuh segar",    # Utama: dua sinyal whole_fruit
            "durian musang king buah utuh",     # Fallback 1: sinyal "utuh"
            "durian mao shan wang utuh",        # Fallback 2: nama Malaysia
            "durian musangking fresh",          # Fallback 3: sinyal "fresh"
        ],
        min_results = 5,    # [FIX-Q4] naik dari 3 → 5 karena D197 melimpah
        num_results = 40,
    ),

    # [FIX-Q2] D13 Golden Bun
    # Log lama: 3 query semua 0/40 valid — "golden bun" + "D13" tidak ada di Google Shopping ID.
    # Strategi baru: pakai nama yang lebih dikenal penjual lokal + sinyal whole_fruit.
    DurianQuery(
        variety_code = "D13",
        variety_name = "Golden Bun",
        search_queries = [
            "durian golden bun utuh segar",     # Utama: nama + sinyal utuh + segar
            "durian D13 buah utuh",             # Fallback 1: kode + sinyal
            "durian golden bun fresh",          # Fallback 2: nama + sinyal "fresh"
            "durian D13 segar berkulit",        # Fallback 3: kode + berkulit
        ],
        min_results = 2,
        num_results = 40,
    ),

    # D24 Sultan / Bukit Merah
    # Log lama: "jual durian sultan D24 buah utuh segar per buah" → 2/40 valid ✓
    # Query baru: pertahankan pola yang sama.
    DurianQuery(
        variety_code = "D24",
        variety_name = "Sultan / Bukit Merah",
        search_queries = [
            "durian D24 buah utuh segar",       # Utama: kode + sinyal ganda
            "durian sultan D24 utuh",           # Fallback 1: nama + kode
            "durian bukit merah utuh segar",    # Fallback 2: nama alternatif
            "durian D24 fresh berkulit",        # Fallback 3: sinyal alternatif
        ],
        min_results = 2,
        num_results = 40,
    ),

    # [FIX-Q3] D2 Dato Nina
    # Log lama: 3 query semua 0/40 valid — "dato nina" sangat langka di Google Shopping ID.
    # Strategi baru: query lebih broad dengan sinyal yang kuat,
    # min_results=1 karena memang sangat langka di pasaran online.
    DurianQuery(
        variety_code = "D2",
        variety_name = "Dato Nina",
        search_queries = [
            "durian dato nina utuh segar",      # Utama: nama + sinyal ganda
            "durian D2 buah utuh",              # Fallback 1: kode + sinyal
            "durian dato nina fresh",           # Fallback 2: nama + "fresh"
            "durian D2 segar berkulit",         # Fallback 3: kode + "berkulit"
        ],
        min_results = 1,    # D2 sangat langka di pasaran online
        num_results = 40,
    ),

]