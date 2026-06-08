# agent/task.py
#
# Orkestrasi pipeline: fetch dari SerpApi → simpan JSON → kirim ke DB.

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from core import config
from core.logger import get_logger
from agent import fetcher, storage, db_sender

logger   = get_logger("agent.task")
_SEP     = "═" * 60
_run_lock = asyncio.Lock()


async def _pipeline() -> dict:
    run_id     = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    logger.info(f"\n{_SEP}\n  Market Intelligence — START\n  run_id: {run_id}\n{_SEP}")

    # ── Siapkan direktori ────────────────────────────────────────────────────
    run_storage = storage.RunStorage.create(run_id)

    # ── Tahap 1: Fetch ───────────────────────────────────────────────────────
    logger.info("[Task] ── Tahap 1/3: Fetch Google Shopping via SerpApi ───────")
    results = await fetcher.fetch_all()

    succeeded    = sum(1 for r in results if r["success"])
    failed       = len(results) - succeeded
    no_results   = sum(1 for r in results if r.get("no_results"))
    total_items  = sum(r["item_count"] for r in results)

    # ── Tahap 2: Simpan JSON ─────────────────────────────────────────────────
    logger.info("[Task] ── Tahap 2/3: Simpan JSON ke disk ──────────────────────")
    saved_files = []
    for result in results:
        try:
            fp = run_storage.save_variety(result)
            saved_files.append(str(fp))
        except Exception as exc:
            logger.error(f"[Task] Gagal simpan '{result['variety_code']}': {exc}")

    # ── Susun summary ─────────────────────────────────────────────────────────
    ended_at     = datetime.now(timezone.utc)
    duration_sec = (ended_at - started_at).total_seconds()

    status = "success" if succeeded > 0 else "failed"
    if succeeded > 0 and (failed - no_results) > 0:
        status = "partial"   # ada error teknis, bukan sekadar no-results

    summary = {
        "run_id":            run_id,
        "run_dir":           str(run_storage.run_dir),
        "started_at":        started_at.isoformat(),
        "ended_at":          ended_at.isoformat(),
        "duration_sec":      round(duration_sec, 2),
        "status":            status,
        "varieties_total":   len(results),
        "varieties_ok":      succeeded,
        "varieties_failed":  failed,
        "varieties_no_data": no_results,
        "total_items":       total_items,
        "saved_files":       saved_files,
        "variety_results": [
            {
                "variety_code":   r["variety_code"],
                "variety_name":   r["variety_name"],
                "query_used":     r["query_used"],
                "success":        r["success"],
                "no_results":     r.get("no_results", False),
                "item_count":     r["item_count"],
                "raw_count":      r.get("raw_count", 0),
                "rejected_count": r.get("rejected_count", 0),
                "error":          r.get("error"),
            }
            for r in results
        ],
    }

    run_storage.save_summary(summary)

    # ── Tahap 3: Kirim ke Database ────────────────────────────────────────────
    logger.info("[Task] ── Tahap 3/3: Kirim ke Database ─────────────────────────")
    db_result = await db_sender.send_run_to_db(summary, results)
    summary["db_insert"] = db_result

    # ── Cleanup run lama ───────────────────────────────────────────────────────
    deleted = storage.cleanup_old_runs()
    if deleted:
        logger.info(f"[Task] Cleanup: {deleted} run lama dihapus.")

    logger.info(
        f"\n{_SEP}\n"
        f"  Market Intelligence — SELESAI\n"
        f"  run_id    : {run_id}\n"
        f"  status    : {status}\n"
        f"  berhasil  : {succeeded}/{len(results)} varietas\n"
        f"  no-data   : {no_results} varietas (Google tidak punya hasil)\n"
        f"  total item: {total_items} listing buah utuh\n"
        f"  DB insert : {db_result.get('listings_inserted', 0)} listing "
        f"({'OK' if db_result.get('success') else 'GAGAL'})\n"
        f"  durasi    : {duration_sec:.1f}s\n"
        f"{_SEP}"
    )

    return summary


async def run_once() -> Optional[dict]:
    """
    Jalankan pipeline sekali. Lock mencegah run paralel.
    Returns dict summary, atau None jika ada run aktif / timeout.
    """
    if _run_lock.locked():
        logger.warning("[Task] Run sebelumnya masih berjalan — skip.")
        return None

    async with _run_lock:
        try:
            return await asyncio.wait_for(_pipeline(), timeout=3600.0)
        except asyncio.TimeoutError:
            logger.error("[Task] Pipeline timeout (1 jam).")
            return None
        except Exception as exc:
            logger.critical(f"[Task] Error tidak tertangani: {exc}", exc_info=True)
            return None
