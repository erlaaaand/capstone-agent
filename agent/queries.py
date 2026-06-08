# agent/queries.py
#
# Definisi target pencarian Google Shopping per varietas durian.
# Query dirancang untuk mendapatkan BUAH UTUH BERKULIT saja.
#
# Strategi filter:
#   - D197/D24: Normal mode — nama varietas WAJIB ada di judul.
#   - D13/D2  : Relaxed mode — nama varietas OR sinyal buah utuh cukup,
#               TAPI tetap harus ada kata "durian" di judul.
#               Catatan: relaxed mode menerima listing yang tidak label varietas
#               (seller lokal sering tidak tulis "D13" atau "Dato Nina").
#
# PENTING perubahan v3:
#   - D2: relaxed_variety_check=False sekarang — D2 sangat langka dan query
#     relaxed menghasilkan item salah varietas (musang king muncul).
#     Lebih baik dapat 0 item yang benar daripada data yang salah.
#   - D13: relaxed_variety_check=True dipertahankan tapi keyword_extras diperkuat.

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
        min_results           : Batas minimum item lolos filter agar query
                               dianggap cukup.
        num_results           : Jumlah hasil yang diminta ke SerpApi (max 100).
        gl                    : Google country code.
        hl                    : Google language code.
        relaxed_variety_check : Jika True, fetcher tidak wajibkan nama varietas
                               ada di judul — cukup lolos sinyal buah utuh.
                               Gunakan HANYA untuk varietas yang benar-benar langka
                               DAN query sudah sangat spesifik.
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
    DurianQuery(
        variety_code = "D197",
        variety_name = "Musang King / Raja Kunyit / Mao Shan Wang",
        search_queries = [
            "durian musang king utuh segar",
            "durian musang king buah utuh",
            "durian mao shan wang buah utuh",
            "durian raja kunyit segar berkulit",
            "durian musangking fresh impor",
        ],
        variety_keyword_extras = frozenset({"msw", "mao shan wang", "raja kunyit", "musangking"}),
        min_results = 5,
        num_results = 40,
        relaxed_variety_check = False,
    ),

    # ── D13 Golden Bun ────────────────────────────────────────────────────────
    # relaxed=True karena seller Indonesia jarang label "D13" / "Golden Bun"
    # tapi query sudah sangat spesifik sehingga hasil tetap relevan.
    DurianQuery(
        variety_code = "D13",
        variety_name = "Golden Bun",
        search_queries = [
            "durian golden bun d13 utuh",
            "durian d13 golden bun segar",
            "jual durian d13 buah utuh",
            "durian golden bun impor malaysia",
            "durian d13 impor malaysia segar",
        ],
        variety_keyword_extras = frozenset({
            "golden bun", "d13", "goldenbun", "golden-bun",
        }),
        min_results = 1,
        num_results = 60,
        relaxed_variety_check = True,
    ),

    # ── D24 Sultan ────────────────────────────────────────────────────────────
    DurianQuery(
        variety_code = "D24",
        variety_name = "Sultan / Bukit Merah",
        search_queries = [
            "durian D24 buah utuh segar",
            "durian sultan D24 utuh",
            "durian bukit merah D24 segar",
            "durian D24 fresh berkulit impor",
            "jual durian D24 asli malaysia",
        ],
        variety_keyword_extras = frozenset({"bukit merah", "sultan d24", "malayd24", "malay d24"}),
        min_results = 2,
        num_results = 40,
        relaxed_variety_check = False,
    ),

    # ── D2 Dato Nina ─────────────────────────────────────────────────────────
    # PENTING: relaxed_variety_check=False karena query relaxed menghasilkan
    # item salah varietas (misalnya Musang King muncul dengan query "durian datuk nina").
    # Lebih baik 0 item benar daripada data salah variety_code.
    # Jika ingin relaxed, ubah kembali ke True DAN tambah validasi di fetcher.
    DurianQuery(
        variety_code = "D2",
        variety_name = "Dato Nina",
        search_queries = [
            "durian dato nina utuh segar",
            "durian dato nina d2 buah",
            "durian d2 dato nina impor",
            "jual durian datuk nina segar",
            "durian dato nina fresh malaysia",
        ],
        variety_keyword_extras = frozenset({
            "dato nina", "datuk nina", "dato nena", "durian d2",
        }),
        min_results = 1,
        num_results = 60,
        relaxed_variety_check = False,  # Ketat: WAJIB ada nama varietas di judul
    ),

]