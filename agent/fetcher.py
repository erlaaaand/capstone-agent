# agent/fetcher.py
#
# Fetch → filter → ekstrak field bersih, siap masuk price_extractor.
#
# v3 Changes (tanpa Ollama):
#   - Filter _is_valid_item diperkuat: relaxed mode WAJIB ada keyword varietas
#     dari variety_keyword_extras (tidak cukup "durian" saja).
#   - D2 sekarang non-relaxed: nama varietas WAJIB ada di judul.
#   - Output item sudah bersih, langsung diproses price_extractor (tanpa LLM).

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional, Tuple

import httpx

from core import config
from core.logger import get_logger
from agent.queries import DURIAN_QUERIES, DurianQuery

logger = get_logger("agent.fetcher")

_ENDPOINT = "/search"


# ══════════════════════════════════════════════════════════════════════════════
# Kata kunci filter
# ══════════════════════════════════════════════════════════════════════════════

_REJECT_KEYWORDS: FrozenSet[str] = frozenset({
    "kupas", "dikupas", "flesh", "pulp", "daging",
    "frozen", "beku", "freezer", "vacum", "vacuum", "nitro",
    "pancake", "biskuit", "kue", "cake", "pudding",
    "jelly", "extract", "sari", "minuman", "juice", "sirup",
    "lempok", "dodol", "es krim", "ice cream",
    "bibit", "benih", "seedling", "pohon", "tanam",
    "entres", "okulasi", "cangkok", "sambung",
    "sabun", "parfum", "lotion", "kosmetik",
    "buku", "kaos", "souvenir",
    "100gr", "100g", "200gr", "200g", "250gr", "250g",
    "400gr", "400g", "500gr", "500g",
    "100 gr", "200 gr", "250 gr", "400 gr", "500 gr",
})

_WHOLE_FRUIT_SIGNALS: FrozenSet[str] = frozenset({
    "utuh", "berkulit", "segar", "bulat",
    "per buah", "per biji", "1 buah", "1buah",
    "whole", "fresh",
    "import", "impor", "malaysia", "imported",
    "premium", "original", "asli", "ori",
    "buah utuh", "segar berkulit", "utuh segar",
    "buah segar", "fresh import",
    "(l)", "(m)", "(s)", "(xl)", " l ", " m ", " s ",
})

_VARIETY_KEYWORDS: Dict[str, FrozenSet[str]] = {
    "D197": frozenset({
        "musang king", "mao shan wang", "raja kunyit",
        "msw", "d197", "musangking",
    }),
    "D13": frozenset({
        "golden bun", "d13", "goldenbun", "golden-bun",
    }),
    "D24": frozenset({
        "sultan", "bukit merah", "d24", "malayd24", "malay d24",
    }),
    "D2": frozenset({
        # "d2" standalone dihapus — terlalu ambigu
        "dato nina", "datuk nina", "dato nena",
        "durian d2",
    }),
}

_MIN_PRICE_IDR: float = 100_000.0
_MAX_PRICE_IDR: float = 10_000_000.0

_WEIGHT_PATTERNS = [
    re.compile(r"(\d+[,.]?\d*)\s*[-–]\s*(\d+[,.]?\d*)\s*kg", re.I),
    re.compile(r"~?\s*(\d+[,.]?\d+)\s*kg", re.I),
    re.compile(r"\b(\d)\s*kg\b", re.I),
]

_SMALL_GRAM_RE  = re.compile(r"\b\d+\+*\s*gr(am)?\b", re.I)
_MAX_SMALL_GRAM = 900


def _extract_weight_kg(title: str) -> Optional[float]:
    for pat in _WEIGHT_PATTERNS:
        m = pat.search(title)
        if m:
            try:
                if len(m.groups()) == 2:
                    lo = float(m.group(1).replace(",", "."))
                    hi = float(m.group(2).replace(",", "."))
                    return round((lo + hi) / 2, 2)
                else:
                    return round(float(m.group(1).replace(",", ".")), 2)
            except ValueError:
                continue
    return None


def _detect_price_unit(title: str) -> str:
    t = title.lower()
    if any(kw in t for kw in ["per kg", "per-kg", "/kg", "harga kg", "1 kg", "1kg"]):
        return "per_kg"
    if any(kw in t for kw in [
        "per buah", "per biji", "1 buah", "1buah", "satu buah",
        "(l)", "(m)", "(s)", "(xl)",
    ]):
        return "per_buah"
    if _extract_weight_kg(title) is not None:
        return "per_buah"
    return "unknown"


def _is_valid_item(
    title:        str,
    price_idr:    float,
    variety_code: str,
    dq:           Optional[DurianQuery] = None,
) -> Tuple[bool, str]:
    """
    Validasi satu item listing.

    Normal mode  : nama varietas WAJIB ada di judul (dari _VARIETY_KEYWORDS ATAU extras).
    Relaxed mode : kata "durian" WAJIB ada DAN setidaknya satu keyword dari
                   variety_keyword_extras WAJIB ada di judul.
                   (Lebih ketat dari v2 — tidak cukup sinyal buah utuh saja)
    """
    t = title.lower()

    # 1. Reject kata kunci berbahaya
    for kw in _REJECT_KEYWORDS:
        if kw in t:
            return False, f"kata reject: '{kw}'"

    # 1b. Reject pola gram kecil
    m = _SMALL_GRAM_RE.search(t)
    if m:
        gram_str = re.sub(r"[^\d]", "", m.group())
        if gram_str:
            gram_val = int(gram_str)
            if gram_val <= _MAX_SMALL_GRAM:
                return False, f"kemasan gram kecil: '{m.group()}' ({gram_val}gr)"

    # 2. Harga masuk akal
    if price_idr < _MIN_PRICE_IDR:
        return False, f"harga terlalu rendah (Rp{price_idr:,.0f})"
    if price_idr > _MAX_PRICE_IDR:
        return False, f"harga terlalu tinggi (Rp{price_idr:,.0f})"

    # 3. Kumpulkan semua keyword varietas
    primary_keywords   = _VARIETY_KEYWORDS.get(variety_code, frozenset())
    extra_keywords     = dq.variety_keyword_extras if dq else frozenset()
    all_variety_kws    = primary_keywords | extra_keywords

    has_variety_kw = any(kw in t for kw in all_variety_kws)

    # Khusus D2: "d2" boleh lolos JIKA ada kata "durian" juga
    if variety_code == "D2" and not has_variety_kw:
        if "d2" in t and "durian" in t:
            has_variety_kw = True

    relaxed = dq.relaxed_variety_check if dq else False

    if relaxed:
        # Relaxed mode: WAJIB ada kata "durian" DAN setidaknya satu keyword
        # dari variety_keyword_extras (bukan cukup sinyal buah utuh saja).
        if not ("durian" in t or "duren" in t):
            return False, "relaxed mode: kata 'durian' tidak ada di judul"
        
        extra_kws = dq.variety_keyword_extras if dq else frozenset()
        has_extra = any(kw in t for kw in extra_kws)
        
        if not has_extra and not has_variety_kw:
            return False, (
                "relaxed mode: tidak ada keyword varietas "
                f"({variety_code}) di judul"
            )
        return True, ""

    # Normal mode: nama varietas WAJIB ada
    if not has_variety_kw:
        return False, f"nama varietas {variety_code} tidak ada di judul"

    # Sinyal buah utuh
    has_signal     = any(kw in t for kw in _WHOLE_FRUIT_SIGNALS)
    has_weight     = _extract_weight_kg(title) is not None
    has_durian     = "durian" in t or "duren" in t
    price_ok_whole = price_idr >= 300_000

    if not has_signal and not has_weight:
        if not (has_durian and price_ok_whole):
            return False, (
                "tidak ada sinyal buah utuh dan harga tidak mencerminkan buah utuh"
            )

    return True, ""


def _extract_clean_item(
    raw_item:     dict,
    variety_code: str,
    dq:           Optional[DurianQuery] = None,
) -> Optional[dict]:
    title = raw_item.get("title", "").strip()
    if not title:
        return None

    price_idr: Optional[float] = raw_item.get("extracted_price")
    price_str: Optional[str]   = raw_item.get("price")

    if price_idr is None and price_str:
        cleaned = re.sub(r"[^\d]", "", price_str)
        if cleaned:
            try:
                price_idr = float(cleaned)
            except ValueError:
                pass

    if price_idr is None:
        return None

    is_valid, reason = _is_valid_item(title, price_idr, variety_code, dq)
    if not is_valid:
        logger.debug(f"[Fetcher][Filter] BUANG '{title[:70]}' — {reason}")
        return None

    weight_kg  = _extract_weight_kg(title)
    price_unit = _detect_price_unit(title)
    old_price  = raw_item.get("extracted_old_price")

    return {
        "position":       raw_item.get("position"),
        "title":          title,
        "price_str":      price_str,
        "price_idr":      price_idr,
        "old_price_idr":  old_price,
        "weight_kg_hint": weight_kg,
        "price_unit":     price_unit,
        "source":         raw_item.get("source"),
        "source_url":     raw_item.get("product_link", ""),
        "rating":         raw_item.get("rating"),
        "reviews":        raw_item.get("reviews"),
        "delivery":       raw_item.get("delivery"),
        "product_link":   raw_item.get("product_link"),
    }


def _process_response(
    raw_response: dict,
    variety_code: str,
    dq:           Optional[DurianQuery] = None,
) -> Tuple[List[dict], int, int]:
    all_raw: List[dict] = (
        raw_response.get("shopping_results", [])
        + raw_response.get("inline_shopping_results", [])
    )

    raw_count   = len(all_raw)
    clean_items = []
    rejected    = 0
    seen: set   = set()

    for raw_item in all_raw:
        if not isinstance(raw_item, dict):
            continue

        item = _extract_clean_item(raw_item, variety_code, dq)

        if item is None:
            rejected += 1
            continue

        key = f"{item['title'].lower()}|{item['source']}|{item['price_idr']}"
        if key in seen:
            continue
        seen.add(key)

        clean_items.append(item)

    logger.info(
        f"[Fetcher][Filter] {variety_code}: "
        f"{len(clean_items)} valid / {raw_count} raw "
        f"({rejected} ditolak)"
    )

    return clean_items, raw_count, rejected


# ══════════════════════════════════════════════════════════════════════════════
# Circuit Breaker
# ══════════════════════════════════════════════════════════════════════════════

_CIRCUIT_THRESHOLD    = 3
_CIRCUIT_COOLDOWN_SEC = 3600


class _CircuitBreaker:
    def __init__(self) -> None:
        self._lock:       Optional[asyncio.Lock] = None
        self._failures:   Dict[str, int]          = {}
        self._tripped_at: Dict[str, float]        = {}

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def is_open(self, key: str) -> bool:
        async with self.lock:
            tripped = self._tripped_at.get(key)
            if tripped is None:
                return False
            if time.time() - tripped < _CIRCUIT_COOLDOWN_SEC:
                return True
            self._failures.pop(key, None)
            self._tripped_at.pop(key, None)
            logger.info(f"[Circuit] RESET '{key}'.")
            return False

    async def record_failure(self, key: str) -> None:
        async with self.lock:
            self._failures[key] = self._failures.get(key, 0) + 1
            if (
                self._failures[key] >= _CIRCUIT_THRESHOLD
                and key not in self._tripped_at
            ):
                self._tripped_at[key] = time.time()
                logger.warning(
                    f"[Circuit] OPEN '{key}' setelah "
                    f"{self._failures[key]} kegagalan."
                )

    async def record_success(self, key: str) -> None:
        async with self.lock:
            self._failures.pop(key, None)
            self._tripped_at.pop(key, None)


_circuit = _CircuitBreaker()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP Request ke SerpApi
# ══════════════════════════════════════════════════════════════════════════════

_NO_RESULTS_PHRASES = {
    "google hasn't returned any results for this query",
    "no results for this query",
    "did not match any shopping results",
}


async def _request(
    query_str: str,
    dq:        DurianQuery,
    client:    httpx.AsyncClient,
) -> Tuple[Optional[dict], Optional[str], bool]:
    params = {
        "engine":        "google_shopping",
        "q":             query_str,
        "api_key":       config.SERPAPI_KEY,
        "gl":            dq.gl,
        "hl":            dq.hl,
        "num":           str(dq.num_results),
        "google_domain": "google.co.id",
    }

    url          = f"{config.SERPAPI_BASE_URL}{_ENDPOINT}"
    max_attempts = config.SERPAPI_MAX_RETRIES + 1

    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.get(url, params=params)

            if resp.status_code == 401:
                return None, "SerpApi: API key tidak valid (HTTP 401).", False
            if resp.status_code == 403:
                return None, "SerpApi: Akses ditolak (HTTP 403).", False
            if resp.status_code == 429:
                return None, "SerpApi: Rate limit (HTTP 429).", False

            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                err_msg = data["error"]
                if any(phrase in err_msg.lower() for phrase in _NO_RESULTS_PHRASES):
                    logger.info(
                        f"[Fetcher] Query '{query_str}' → "
                        "tidak ada hasil di Google Shopping."
                    )
                    return None, err_msg, True
                return None, f"SerpApi error: {err_msg}", False

            return data, None, False

        except httpx.TimeoutException:
            err = f"Timeout {config.SERPAPI_TIMEOUT_SEC}s."
            logger.warning(f"[Fetcher] {err} (attempt={attempt})")
            if attempt < max_attempts:
                await asyncio.sleep(config.SERPAPI_RETRY_DELAY * attempt)
            else:
                return None, err, False

        except httpx.HTTPStatusError as exc:
            return None, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}", False

        except httpx.ConnectError as exc:
            return None, f"Koneksi gagal: {exc}", False

        except Exception as exc:
            return None, f"{type(exc).__name__}: {str(exc)[:300]}", False

    return None, "Semua retry habis.", False


# ══════════════════════════════════════════════════════════════════════════════
# Fetch satu varietas
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_variety(
    dq:     DurianQuery,
    client: httpx.AsyncClient,
) -> dict:
    relaxed_info = " [RELAXED MODE]" if dq.relaxed_variety_check else ""
    logger.info(
        f"[Fetcher] Mulai '{dq.variety_name}' ({dq.variety_code}){relaxed_info} "
        f"| {len(dq.search_queries)} query"
    )

    best_items:     List[dict]    = []
    best_query:     str           = dq.search_queries[0]
    best_count:     int           = 0
    best_raw:       int           = 0
    best_rej:       int           = 0
    last_error:     Optional[str] = None
    all_no_results: int           = 0

    for i, query_str in enumerate(dq.search_queries):
        logger.info(f"[Fetcher] Query {i+1}/{len(dq.search_queries)}: '{query_str}'")

        raw_resp, error, is_no_results = await _request(query_str, dq, client)

        if is_no_results:
            all_no_results += 1
            continue

        if error:
            last_error = error
            logger.warning(f"[Fetcher] Query gagal: {error}")
            continue

        clean_items, raw_count, rejected = _process_response(raw_resp, dq.variety_code, dq)

        logger.info(
            f"[Fetcher] '{query_str}': "
            f"{len(clean_items)} valid dari {raw_count} item SerpApi"
        )

        if len(clean_items) > best_count:
            best_count = len(clean_items)
            best_items = clean_items
            best_query = query_str
            best_raw   = raw_count
            best_rej   = rejected

        if best_count >= dq.min_results:
            logger.info(
                f"[Fetcher] Cukup ({best_count} >= min={dq.min_results}). Stop."
            )
            break

    fetched_at        = datetime.now(timezone.utc).isoformat()
    all_queries_tried = len(dq.search_queries)

    if all_no_results == all_queries_tried and best_count == 0:
        logger.warning(
            f"[Fetcher] '{dq.variety_code}' tidak ditemukan di Google Shopping "
            f"(semua {all_queries_tried} query kembali no-results)."
        )
        return {
            "variety_code":   dq.variety_code,
            "variety_name":   dq.variety_name,
            "query_used":     best_query,
            "fetched_at":     fetched_at,
            "success":        False,
            "no_results":     True,
            "error":          "Varietas tidak ditemukan di Google Shopping.",
            "item_count":     0,
            "raw_count":      0,
            "rejected_count": 0,
            "items":          [],
        }

    if best_count == 0 and last_error:
        logger.error(f"[Fetcher] GAGAL '{dq.variety_code}': {last_error}")
        return {
            "variety_code":   dq.variety_code,
            "variety_name":   dq.variety_name,
            "query_used":     best_query,
            "fetched_at":     fetched_at,
            "success":        False,
            "no_results":     False,
            "error":          last_error,
            "item_count":     0,
            "raw_count":      0,
            "rejected_count": 0,
            "items":          [],
        }

    if best_count < dq.min_results:
        logger.warning(
            f"[Fetcher] '{dq.variety_code}' hanya {best_count} item valid "
            f"(min={dq.min_results}). Data tetap disimpan."
        )

    success = best_count > 0
    logger.info(
        f"[Fetcher] {'✓' if success else '✗'} '{dq.variety_name}': "
        f"{best_count} item valid | query='{best_query}'"
        + (f" | RELAXED" if dq.relaxed_variety_check else "")
    )

    return {
        "variety_code":   dq.variety_code,
        "variety_name":   dq.variety_name,
        "query_used":     best_query,
        "fetched_at":     fetched_at,
        "success":        success,
        "no_results":     not success and all_no_results == all_queries_tried,
        "error":          None if success else last_error,
        "item_count":     best_count,
        "raw_count":      best_raw,
        "rejected_count": best_rej,
        "items":          best_items,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_all() -> List[dict]:
    if not config.SERPAPI_KEY:
        logger.error("[Fetcher] SERPAPI_KEY belum diset di .env")
        return [
            {
                "variety_code":   dq.variety_code,
                "variety_name":   dq.variety_name,
                "query_used":     dq.search_queries[0],
                "fetched_at":     datetime.now(timezone.utc).isoformat(),
                "success":        False,
                "no_results":     False,
                "error":          "SERPAPI_KEY belum dikonfigurasi.",
                "item_count":     0,
                "raw_count":      0,
                "rejected_count": 0,
                "items":          [],
            }
            for dq in DURIAN_QUERIES
        ]

    semaphore = asyncio.Semaphore(config.SERPAPI_CONCURRENT)
    results: List[Optional[dict]] = [None] * len(DURIAN_QUERIES)

    async def _bounded(idx: int, dq: DurianQuery, client: httpx.AsyncClient) -> None:
        async with semaphore:
            if await _circuit.is_open(dq.variety_code):
                logger.warning(
                    f"[Fetcher] SKIP '{dq.variety_code}' — circuit breaker OPEN."
                )
                results[idx] = {
                    "variety_code":   dq.variety_code,
                    "variety_name":   dq.variety_name,
                    "query_used":     dq.search_queries[0],
                    "fetched_at":     datetime.now(timezone.utc).isoformat(),
                    "success":        False,
                    "no_results":     False,
                    "error":          "Circuit breaker open.",
                    "item_count":     0,
                    "raw_count":      0,
                    "rejected_count": 0,
                    "items":          [],
                }
                return

            result = await _fetch_variety(dq, client)
            results[idx] = result

            if result["success"]:
                await _circuit.record_success(dq.variety_code)
            elif not result.get("no_results", False):
                await _circuit.record_failure(dq.variety_code)

    async with httpx.AsyncClient(timeout=config.SERPAPI_TIMEOUT_SEC) as client:
        await asyncio.gather(
            *[_bounded(i, dq, client) for i, dq in enumerate(DURIAN_QUERIES)],
            return_exceptions=False,
        )

    final = [r for r in results if r is not None]

    succeeded   = sum(1 for r in final if r["success"])
    no_results  = sum(1 for r in final if r.get("no_results"))
    total_items = sum(r["item_count"] for r in final)

    logger.info(
        f"[Fetcher] Selesai: {succeeded}/{len(final)} berhasil | "
        f"{no_results} tidak ada data | "
        f"{total_items} item total."
    )

    return final