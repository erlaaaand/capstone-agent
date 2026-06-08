# app/api.py
#
# Endpoints:
#   POST /api/v1/fetch/trigger  — picu fetch manual (background)
#   GET  /api/v1/runs           — daftar semua run
#   GET  /api/v1/runs/latest    — data run terbaru (full JSON)
#   GET  /api/v1/runs/{dir_name} — data run spesifik
#   GET  /api/v1/health         — status service
#   GET  /api/v1/scheduler      — info scheduler

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from core import config
from core.logger import get_logger
from core.security import verify_api_key
from agent import storage, task
from agent.scheduler import get_scheduler

logger = get_logger("app.api")

router = APIRouter(prefix="/api/v1")


# ══════════════════════════════════════════════════════════════════════════════
# Request / Response Schemas
# ══════════════════════════════════════════════════════════════════════════════

class TriggerResponse(BaseModel):
    triggered:  bool
    message:    str
    run_active: bool  # True jika ada run yang sedang berjalan


class RunListItem(BaseModel):
    dir_name:    str
    summary:     Optional[Dict[str, Any]] = None


class RunListResponse(BaseModel):
    total: int
    runs:  List[RunListItem]


class HealthResponse(BaseModel):
    status:           str
    app_name:         str
    app_version:      str
    serpapi_key_set:  bool
    latest_run:       Optional[Dict[str, Any]] = None


class SchedulerResponse(BaseModel):
    running:       bool
    disabled:      bool
    next_run_time: Optional[str]
    schedule:      str


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/fetch/trigger",
    response_model=TriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Picu fetch manual",
    description=(
        "Jalankan pipeline fetch Google Shopping di background. "
        "Hasil disimpan ke `data/runs/<timestamp>/`. "
        "Gunakan `GET /api/v1/runs/latest` untuk melihat hasilnya."
    ),
    dependencies=[Depends(verify_api_key)],
)
async def trigger_fetch() -> TriggerResponse:
    run_active = task._run_lock.locked()

    if run_active:
        return TriggerResponse(
            triggered  = False,
            message    = "Pipeline sedang berjalan. Tunggu hingga selesai.",
            run_active = True,
        )

    asyncio.create_task(task.run_once(), name="manual_run_api")
    logger.info("[API] Manual fetch trigger diterima.")

    return TriggerResponse(
        triggered  = True,
        message    = "Fetch dijalankan di background. Cek /api/v1/runs/latest untuk hasilnya.",
        run_active = False,
    )


@router.post(
    "/fetch/run",
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
    summary="Jalankan fetch dan tunggu hasilnya",
    description=(
        "Jalankan pipeline fetch dan **tunggu hingga selesai** sebelum response dikembalikan. "
        "Cocok untuk testing. Bisa memakan waktu 30–120 detik."
    ),
    dependencies=[Depends(verify_api_key)],
)
async def run_fetch_sync() -> Dict[str, Any]:
    if task._run_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pipeline sedang berjalan. Gunakan /fetch/trigger untuk mode background.",
        )

    logger.info("[API] Sync run dimulai (menunggu hasil)...")
    result = await task.run_once()

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pipeline gagal atau timeout.",
        )

    return result


@router.get(
    "/runs",
    response_model=RunListResponse,
    summary="Daftar semua run",
    description="Kembalikan daftar semua run yang tersimpan, terbaru di atas.",
    dependencies=[Depends(verify_api_key)],
)
async def list_runs() -> RunListResponse:
    runs = storage.list_runs()
    return RunListResponse(
        total=len(runs),
        runs=[RunListItem(dir_name=r["dir_name"], summary=r.get("summary")) for r in runs],
    )


@router.get(
    "/runs/latest",
    response_model=Dict[str, Any],
    summary="Data run terbaru (full JSON)",
    description=(
        "Kembalikan seluruh data run terakhir, termasuk raw response SerpApi "
        "per varietas. Response bisa besar (ratusan KB)."
    ),
    dependencies=[Depends(verify_api_key)],
)
async def get_latest_run() -> Dict[str, Any]:
    run = storage.get_latest_run()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Belum ada data. Jalankan fetch terlebih dahulu via POST /fetch/trigger.",
        )
    return run


@router.get(
    "/runs/{dir_name}",
    response_model=Dict[str, Any],
    summary="Data run spesifik",
    description="Kembalikan data run berdasarkan nama direktori (dari GET /runs).",
    dependencies=[Depends(verify_api_key)],
)
async def get_run(dir_name: str) -> Dict[str, Any]:
    # Sanitasi: cegah path traversal
    if ".." in dir_name or "/" in dir_name or "\\" in dir_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="dir_name tidak valid.")

    run = storage.get_run(dir_name)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{dir_name}' tidak ditemukan.",
        )
    return run


@router.get(
    "/runs/{dir_name}/{variety_code}",
    response_model=Dict[str, Any],
    summary="Data satu varietas dari run tertentu",
    description="Kembalikan raw JSON SerpApi untuk satu varietas dari run tertentu.",
    dependencies=[Depends(verify_api_key)],
)
async def get_run_variety(dir_name: str, variety_code: str) -> Dict[str, Any]:
    if ".." in dir_name or "/" in dir_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="dir_name tidak valid.")

    run = storage.get_run(dir_name)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{dir_name}' tidak ditemukan.")

    varieties = run.get("varieties", [])
    for v in varieties:
        if v.get("variety_code", "").upper() == variety_code.upper():
            return v

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Varietas '{variety_code}' tidak ditemukan di run '{dir_name}'.",
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
)
async def health() -> HealthResponse:
    latest = storage.get_latest_run()
    latest_summary = latest.get("summary") if latest else None

    return HealthResponse(
        status           = "ok",
        app_name         = config.APP_NAME,
        app_version      = config.APP_VERSION,
        serpapi_key_set  = bool(config.SERPAPI_KEY),
        latest_run       = latest_summary,
    )


@router.get(
    "/scheduler",
    response_model=SchedulerResponse,
    summary="Status scheduler",
)
async def scheduler_status() -> SchedulerResponse:
    sched = get_scheduler()
    return SchedulerResponse(
        running       = sched.is_running,
        disabled      = config.SCHEDULER_DISABLED,
        next_run_time = sched.next_run_time(),
        schedule      = f"{config.CRON_HOUR:02d}:{config.CRON_MINUTE:02d} {config.TIMEZONE}",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints: query database
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/db/prices",
    response_model=Dict[str, Any],
    summary="Ringkasan harga dari database",
    description=(
        "Query harga dari database. Kembalikan semua listing price_per_kg yang "
        "bisa dihitung, dikelompokkan per varietas.\n\n"
        "Parameter opsional:\n"
        "- `variety_code`: filter ke satu varietas (D197, D13, D24, D2)\n"
        "- `limit`: maksimum listing per varietas (default 50)\n"
        "- `run_id`: filter ke run tertentu (default: run terbaru)"
    ),
    dependencies=[Depends(verify_api_key)],
)
async def get_prices_from_db(
    variety_code: Optional[str] = None,
    limit:        int           = 50,
    run_id:       Optional[str] = None,
) -> Dict[str, Any]:
    from agent.db_sender import _sqlite_init, _SQLITE_PATH, _BACKEND, _DATABASE_URL

    if _BACKEND == "sqlite":
        def _query() -> dict:
            conn = _sqlite_init(_SQLITE_PATH)
            try:
                # Ambil run_id terbaru jika tidak dispesifikasi
                target_run = run_id
                if not target_run:
                    row = conn.execute(
                        "SELECT run_id FROM fetch_runs ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                    if not row:
                        return {"run_id": None, "varieties": {}, "message": "Belum ada data di database."}
                    target_run = row["run_id"]

                # Query listings per varietas
                where = ["run_id = ?", "price_per_kg_calc IS NOT NULL", "is_outlier = 0"]
                params: list = [target_run]
                if variety_code:
                    where.append("variety_code = ?")
                    params.append(variety_code.upper())

                rows = conn.execute(
                    f"""SELECT variety_code, title, price_idr, price_str,
                               price_unit, weight_kg_hint, price_per_kg_calc,
                               calc_notes, source, rating, reviews, fetched_at
                        FROM price_listings
                        WHERE {' AND '.join(where)}
                        ORDER BY variety_code, price_per_kg_calc
                        LIMIT {limit * 10}""",
                    params,
                ).fetchall()

                # Kelompokkan per varietas
                varieties: Dict[str, Any] = {}
                for r in rows:
                    vc = r["variety_code"]
                    if vc not in varieties:
                        varieties[vc] = {"listings": [], "summary": {}}
                    if len(varieties[vc]["listings"]) < limit:
                        varieties[vc]["listings"].append(dict(r))

                # Hitung summary per varietas
                for vc, data in varieties.items():
                    prices = [l["price_per_kg_calc"] for l in data["listings"] if l["price_per_kg_calc"]]
                    if prices:
                        data["summary"] = {
                            "count":      len(prices),
                            "min_idr":    round(min(prices)),
                            "max_idr":    round(max(prices)),
                            "avg_idr":    round(sum(prices) / len(prices)),
                        }

                return {"run_id": target_run, "varieties": varieties}
            finally:
                conn.close()

        result = await asyncio.get_event_loop().run_in_executor(None, _query)
        return result
    else:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Endpoint /db/prices untuk PostgreSQL belum diimplementasikan. Gunakan SQL client langsung.",
        )


@router.get(
    "/db/runs",
    response_model=Dict[str, Any],
    summary="Daftar run dari database",
    dependencies=[Depends(verify_api_key)],
)
async def get_db_runs(limit: int = 20) -> Dict[str, Any]:
    from agent.db_sender import _sqlite_init, _SQLITE_PATH, _BACKEND

    if _BACKEND != "sqlite":
        raise HTTPException(status_code=501, detail="Hanya tersedia untuk SQLite backend.")

    def _query() -> list:
        conn = _sqlite_init(_SQLITE_PATH)
        try:
            rows = conn.execute(
                """SELECT run_id, started_at, ended_at, duration_sec, status,
                          varieties_ok, varieties_failed, total_items
                   FROM fetch_runs
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    rows = await asyncio.get_event_loop().run_in_executor(None, _query)
    return {"total": len(rows), "runs": rows}
