# app/main.py
from __future__ import annotations

import asyncio
import sys

# Windows: set ProactorEventLoop sebelum apapun
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core import config
from core.logger import get_logger
from agent.scheduler import get_scheduler
from app.api import router

logger = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(
        f"\n{'═'*50}\n"
        f"  {config.APP_NAME} v{config.APP_VERSION} — START\n"
        f"  SERPAPI_KEY : {'✓ diset' if config.SERPAPI_KEY else '✗ BELUM DISET'}\n"
        f"  Data dir    : {config.DATA_DIR}\n"
        f"{'═'*50}"
    )

    if not config.SERPAPI_KEY:
        logger.warning(
            "⚠ SERPAPI_KEY belum diset! "
            "Tambahkan ke .env: SERPAPI_KEY=your_key_here"
        )

    if not config.API_KEY:
        logger.warning(
            "⚠ API_KEY belum diset! "
            "Endpoint yang dilindungi tidak dapat diakses. "
            "Tambahkan ke .env: API_KEY=random-secret-key"
        )

    # Pastikan direktori data ada
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Mulai scheduler
    scheduler = get_scheduler()
    await scheduler.start()

    yield

    await scheduler.stop()
    logger.info(f"{'═'*50}\n  {config.APP_NAME} — SHUTDOWN\n{'═'*50}")


app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    description=(
        "**Market Intelligence Agent** — Fetch harga durian premium dari "
        "Google Shopping via SerpApi dan simpan sebagai JSON mentah.\n\n"
        "**Pipeline:**\n"
        "1. SerpApi Google Shopping fetch\n"
        "2. Simpan raw JSON ke `data/runs/<timestamp>/`\n\n"
        "**Output per-run:**\n"
        "- `run_summary.json` — metadata run\n"
        "- `D197_musang_king.json` — raw SerpApi response per varietas\n"
        "- `D13_golden_bun.json`, `D24_sultan.json`, `D2_dato_nina.json`\n\n"
        "Semua endpoint kecuali `/health` dan `/api/v1/health` memerlukan "
        "header `X-API-Key`."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", tags=["Root"])
async def root() -> JSONResponse:
    return JSONResponse({
        "service": config.APP_NAME,
        "version": config.APP_VERSION,
        "status":  "running",
        "docs":    "/docs",
    })


@app.get("/health", tags=["Root"])
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
