# core/config.py
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env dari root project
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_ROOT / ".env", override=False)


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("true", "1", "yes")


# ── App ──────────────────────────────────────────────────────────────────────
APP_NAME:    str  = os.getenv("APP_NAME", "Market Intelligence Agent")
APP_VERSION: str  = os.getenv("APP_VERSION", "1.0.0")
DEBUG:       bool = _bool("DEBUG", False)

# ── Security ─────────────────────────────────────────────────────────────────
API_KEY: str = os.getenv("API_KEY", "")

# ── SerpApi ──────────────────────────────────────────────────────────────────
SERPAPI_KEY:            str   = os.getenv("SERPAPI_KEY", "")
SERPAPI_BASE_URL:       str   = "https://serpapi.com"
SERPAPI_TIMEOUT_SEC:    float = _float("SERPAPI_TIMEOUT_SEC", 30.0)
SERPAPI_MAX_RETRIES:    int   = _int("SERPAPI_MAX_RETRIES", 2)
SERPAPI_RETRY_DELAY:    float = _float("SERPAPI_RETRY_DELAY_SEC", 2.0)
SERPAPI_CONCURRENT:     int   = _int("SERPAPI_CONCURRENT_LIMIT", 2)

# ── Storage ──────────────────────────────────────────────────────────────────
DATA_DIR:       Path = _ROOT / os.getenv("DATA_DIR", "data/runs")
MAX_RUNS_KEPT:  int  = _int("DATA_MAX_RUNS_KEPT", 30)

# ── Scheduler ────────────────────────────────────────────────────────────────
CRON_HOUR:          int  = _int("CRON_HOUR", 19)
CRON_MINUTE:        int  = _int("CRON_MINUTE", 30)
TIMEZONE:           str  = os.getenv("TIMEZONE", "Asia/Jakarta")
SCHEDULER_DISABLED: bool = _bool("SCHEDULER_DISABLED", False)

# ── Database ─────────────────────────────────────────────────────────────────
DB_BACKEND:    str  = os.getenv("DB_BACKEND", "sqlite").lower()
DATABASE_URL:  str  = os.getenv("DATABASE_URL", "")
SQLITE_PATH:   Path = _ROOT / os.getenv("SQLITE_PATH", "data/market.db")
