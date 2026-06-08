# agent/queries.py
#
# Definisi target pencarian Google Shopping per varietas durian.
# Query dirancang untuk mendapatkan BUAH UTUH BERKULIT saja,
# bukan daging, bukan bibit, bukan olahan.
#
# Strategi query:
# - Sertakan kata kunci positif yang kuat: "buah utuh", "segar berkulit"
# - Sertakan satuan berat per buah: "per buah", "per biji", "per kg"
# - Hindari query yang terlalu pendek → Google akan memperluas ke produk lain

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
        search_queries : Daftar query yang dicoba berurutan. Query pertama
                         adalah utama; berikutnya adalah fallback jika hasil
                         kurang dari min_results.
        min_results    : Batas minimum item lolos filter agar query dianggap cukup.
        num_results    : Jumlah hasil yang diminta ke SerpApi (max 100).
                         Sengaja dinaikkan ke 40 untuk kompensasi setelah filtering.
        gl             : Google country code.
        hl             : Google language code.
    """
    variety_code:   str
    variety_name:   str
    search_queries: List[str]
    min_results:    int = 3
    num_results:    int = 40   # Minta lebih banyak karena sebagian akan dibuang saat filter
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
#
# PRINSIP QUERY:
# "jual durian [nama] buah utuh segar"  → sinyal kuat ke Google Shopping
# "per buah" / "per biji" / "per kg"   → disambiguasi satuan
# Tanpa kata: daging, kupas, frozen, bibit, olahan

DURIAN_QUERIES: List[DurianQuery] = [

    DurianQuery(
        variety_code = "D197",
        variety_name = "Musang King / Raja Kunyit / Mao Shan Wang",
        search_queries = [
            # Query utama: paling spesifik
            "jual durian musang king buah utuh segar per buah",
            # Fallback 1: nama Malaysia + satuan
            "jual durian mao shan wang buah utuh berkulit",
            # Fallback 2: nama populer Indonesia
            "jual durian raja kunyit buah utuh segar berkulit",
            # Fallback 3: lebih longgar tapi tetap ada "buah utuh"
            "durian musang king buah utuh berkulit segar",
        ],
        min_results = 3,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D13",
        variety_name = "Golden Bun",
        search_queries = [
            "jual durian golden bun buah utuh segar per buah",
            "jual durian D13 golden bun buah utuh berkulit",
            "durian golden bun buah utuh segar berkulit",
        ],
        min_results = 2,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D24",
        variety_name = "Sultan / Bukit Merah",
        search_queries = [
            "jual durian sultan D24 buah utuh segar per buah",
            "jual durian bukit merah D24 buah utuh berkulit",
            "durian sultan D24 buah utuh segar berkulit",
        ],
        min_results = 2,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D2",
        variety_name = "Dato Nina",
        search_queries = [
            "jual durian dato nina buah utuh segar per buah",
            "jual durian D2 dato nina buah utuh berkulit",
            "durian dato nina buah utuh segar berkulit",
        ],
        min_results = 1,   # D2 lebih langka di pasaran online
        num_results = 40,
    ),

]
