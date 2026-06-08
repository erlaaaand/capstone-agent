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
# PRINSIP QUERY BARU:
# - Gunakan kata kunci yang natural digunakan oleh SELLER (singkat, padat, menggunakan kode).
# - Hindari over-filtering di tingkat Search Engine (jangan gunakan "segar per buah berkulit").
# - Biarkan SerpApi menarik data yang luas (bibit/kupas mungkin masuk), lalu biarkan 
#   Ollama LLM yang memfilter is_whole_fruit di tahap normalisasi untuk mendapatkan harga wajar.

DURIAN_QUERIES: List[DurianQuery] = [

    DurianQuery(
        variety_code = "D197",
        variety_name = "Musang King / Raja Kunyit / Mao Shan Wang",
        search_queries = [
            # Utama: Sangat umum dipakai seller
            "durian musang king utuh",
            # Fallback 1: Menggunakan kode
            "durian D197 utuh",
            # Fallback 2: Kata kunci alternatif populer
            "durian mao shan wang utuh",
            # Fallback 3: Sangat broad jika hasil masih kurang
            "durian musang king fresh",
        ],
        min_results = 3,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D13",
        variety_name = "Golden Bun",
        search_queries = [
            # Utama: Kombinasi kode dan utuh (Seller sering pakai D13)
            "durian D13 utuh",
            # Fallback 1: Menggunakan nama komersial
            "durian golden bun utuh",
            # Fallback 2: Broad query untuk D13
            "durian D13 fresh",
            # Fallback 3: Tanpa kata "utuh" (Ollama yang akan memfilter nanti)
            "durian D13 asli",
        ],
        min_results = 2,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D24",
        variety_name = "Sultan / Bukit Merah",
        search_queries = [
            # Utama: Kode sangat dominan untuk D24
            "durian D24 utuh",
            # Fallback 1: Kombinasi nama dan kode
            "durian sultan D24",
            # Fallback 2: Varian nama lokal
            "durian bukit merah utuh",
            # Fallback 3: Broad query
            "durian D24 fresh",
        ],
        min_results = 2,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D2",
        variety_name = "Dato Nina",
        search_queries = [
            # Utama: Sangat langka, langsung tembak kode
            "durian D2 utuh",
            # Fallback 1: Menggunakan nama komersial
            "durian dato nina utuh",
            # Fallback 2: Broad query
            "durian D2 dato nina",
            # Fallback 3: Pencarian paling umum untuk varietas ini
            "durian D2 asli",
        ],
        min_results = 1,   # D2 lebih langka di pasaran online
        num_results = 40,
    ),

]