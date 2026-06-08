# agent/queries.py
#
# Definisi target pencarian Google Shopping per varietas durian.
# Query dirancang untuk mendapatkan BUAH UTUH BERKULIT saja.
#
# Perbaikan 2026-06-08 v2:
#   [FIX-Q5] D13 Golden Bun: strategi baru — karena "golden bun" dan "D13"
#            nyaris tidak ada di Google Shopping ID, kita pakai pendekatan
#            "durian impor malaysia premium" + ciri D13 (kulit tipis, biji besar)
#            dan andalkan LLM Ollama untuk memverifikasi via variety_code override.
#            Ditambah query fallback yang sangat broad agar setidaknya ada data.
#   [FIX-Q6] D2 Dato Nina: "d2" terlalu generik → semua query wajib pair
#            dengan "durian" + identifier sekunder. Tambah alias seller lokal.
#   [FIX-Q7] Tambah variety_aliases_override: dict keyword tambahan yang
#            DIIZINKAN per varietas selain _VARIETY_KEYWORDS utama.
#            Ini dibaca fetcher._is_valid_item() untuk relaksasi D13/D2.
#   [FIX-Q8] num_results D13/D2 dinaikkan ke 60 — net yang lebih lebar.
#
# STRATEGI D13 & D2:
#   Kedua varietas ini hampir tidak ada label eksplisit di marketplace Indonesia.
#   Solusi: gunakan query "durian impor malaysia premium" yang luas,
#   biarkan Ollama menentukan variety_code dari konteks + sistem kita override
#   variety_code berdasarkan query yang digunakan.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional


@dataclass(frozen=True)
class DurianQuery:
    """
    Satu target pencarian Google Shopping untuk satu varietas durian.

    Attributes:
        variety_code          : Kode identifikasi varietas (D2/D13/D24/D197).
        variety_name          : Nama lengkap untuk logging dan output JSON.
        search_queries        : Daftar query dicoba berurutan. Pertama = utama;
                               berikutnya = fallback.
        variety_keyword_extras: Keyword tambahan yang boleh lolos filter varietas
                               (selain _VARIETY_KEYWORDS utama di fetcher).
                               Digunakan untuk D13/D2 yang langka.
        min_results           : Batas minimum item lolos filter agar query
                               dianggap cukup.
        num_results           : Jumlah hasil yang diminta ke SerpApi (max 100).
        gl                    : Google country code.
        hl                    : Google language code.
        relaxed_variety_check : Jika True, fetcher tidak wajibkan nama varietas
                               ada di judul — cukup lolos sinyal buah utuh.
                               Gunakan untuk varietas yang sangat langka.
    """
    variety_code:           str
    variety_name:           str
    search_queries:         List[str]
    variety_keyword_extras: FrozenSet[str] = frozenset()
    min_results:            int  = 3
    num_results:            int  = 40
    gl:                     str  = "id"
    hl:                     str  = "id"
    relaxed_variety_check:  bool = False

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

    # ── D197 Musang King ──────────────────────────────────────────────────────
    # Varietas paling populer di pasar Indonesia — kuantitas melimpah.
    # Query fokus ke buah utuh, hindari produk olahan.
    DurianQuery(
        variety_code = "D197",
        variety_name = "Musang King / Raja Kunyit / Mao Shan Wang",
        search_queries = [
            "durian musang king utuh segar",        # Utama: dua sinyal whole_fruit
            "durian musang king buah utuh",         # Fallback 1
            "durian mao shan wang buah utuh",       # Fallback 2: nama Malaysia
            "durian raja kunyit segar berkulit",    # Fallback 3: nama lokal
            "durian musangking fresh impor",        # Fallback 4: sinyal impor
        ],
        variety_keyword_extras = frozenset({"msw", "mao shan wang", "raja kunyit"}),
        min_results = 5,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D13",
        variety_name = "Golden Bun",
        search_queries = [
            "durian golden bun d13 utuh",           # Utama: nama lengkap + kode
            "durian d13 golden bun segar",          # Fallback 1: kode di depan
            "jual durian d13 buah utuh",            # Fallback 2: kata "jual" membantu
            "durian golden bun impor malaysia",     # Fallback 3: konteks impor
            "durian d13 impor malaysia segar",      # Fallback 4: kode + impor
            "durian golden bun fresh berkulit",     # Fallback 5: sinyal ganda
            "beli durian d13",                      # Fallback 6: sangat broad
        ],
        variety_keyword_extras = frozenset({
            "golden bun", "d13", "impor malaysia", "malaysia impor",
        }),
        min_results = 1,    # Sangat langka — terima berapa pun hasilnya
        num_results = 60,   # [FIX-Q8] Net lebih lebar
        relaxed_variety_check = True,   # Izinkan match via extras saja
    ),

    DurianQuery(
        variety_code = "D24",
        variety_name = "Sultan / Bukit Merah",
        search_queries = [
            "durian D24 buah utuh segar",           # Utama: kode + sinyal ganda
            "durian sultan D24 utuh",               # Fallback 1: nama + kode
            "durian bukit merah D24 segar",         # Fallback 2: nama alternatif
            "durian D24 fresh berkulit impor",      # Fallback 3: sinyal berganda
            "jual durian D24 asli malaysia",        # Fallback 4: konteks asal
        ],
        variety_keyword_extras = frozenset({"bukit merah", "sultan d24"}),
        min_results = 2,
        num_results = 40,
    ),

    DurianQuery(
        variety_code = "D2",
        variety_name = "Dato Nina",
        search_queries = [
            "durian dato nina utuh segar",          # Utama: nama resmi
            "durian dato nina d2 buah",             # Fallback 1: nama + kode
            "durian d2 dato nina impor",            # Fallback 2: kode + nama + impor
            "jual durian datuk nina segar",         # Fallback 3: ejaan alternatif
            "durian dato nina fresh malaysia",      # Fallback 4: nama + asal
            "durian d2 impor malaysia utuh",        # Fallback 5: kode + impor
            "durian biji kecil impor premium",      # Fallback 6: karakteristik D2
            "beli durian d2 malaysia",              # Fallback 7: sangat broad
        ],
        variety_keyword_extras = frozenset({
            "dato nina", "datuk nina", "dato nena",
            "d2", "durian d2",
            "biji kecil",   # Karakteristik khas D2
        }),
        min_results = 1,    # Sangat langka
        num_results = 60,   # [FIX-Q8] Net lebih lebar
        relaxed_variety_check = True,
    ),

]