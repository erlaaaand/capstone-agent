# agent/scheduler.py
from __future__ import annotations

import asyncio
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent

from core import config
from core.logger import get_logger
from agent.task import run_once

logger   = get_logger("agent.scheduler")
_JOB_ID  = "market_intelligence"


def _on_executed(event: JobExecutionEvent) -> None:
    if event.job_id != _JOB_ID:
        return
    rv = event.retval
    if rv is None:
        logger.warning("[Scheduler] Job selesai — di-skip (run aktif atau timeout).")
    else:
        logger.info(
            f"[Scheduler] Job selesai — "
            f"status={rv.get('status')} | "
            f"items={rv.get('total_items')} | "
            f"durasi={rv.get('duration_sec')}s"
        )


def _on_error(event: JobExecutionEvent) -> None:
    if event.job_id != _JOB_ID:
        return
    logger.error(f"[Scheduler] Job error: {event.exception}", exc_info=event.traceback)


class MarketScheduler:

    def __init__(self) -> None:
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._started   = False

    async def start(self) -> None:
        if config.SCHEDULER_DISABLED:
            logger.warning("[Scheduler] SCHEDULER_DISABLED=true — tidak dijadwalkan.")
            return

        if self._started:
            return

        sched = AsyncIOScheduler(
            timezone=config.TIMEZONE,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
        )
        sched.add_listener(_on_executed, EVENT_JOB_EXECUTED)
        sched.add_listener(_on_error,    EVENT_JOB_ERROR)
        sched.add_job(
            run_once,
            CronTrigger(hour=config.CRON_HOUR, minute=config.CRON_MINUTE, timezone=config.TIMEZONE),
            id=_JOB_ID,
            name="Market Intelligence Fetch",
        )
        sched.start()
        self._scheduler = sched
        self._started   = True

        next_run = sched.get_job(_JOB_ID).next_run_time
        logger.info(
            f"[Scheduler] Dijadwalkan pukul "
            f"{config.CRON_HOUR:02d}:{config.CRON_MINUTE:02d} {config.TIMEZONE}. "
            f"Run berikutnya: {next_run}"
        )

    async def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("[Scheduler] Scheduler dihentikan.")

    async def trigger_now(self) -> None:
        logger.info("[Scheduler] Manual trigger dijalankan di background.")
        asyncio.create_task(run_once(), name="manual_run")

    @property
    def is_running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    def next_run_time(self) -> Optional[str]:
        if not self.is_running:
            return None
        job = self._scheduler.get_job(_JOB_ID)
        return job.next_run_time.isoformat() if job and job.next_run_time else None


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: Optional[MarketScheduler] = None


def get_scheduler() -> MarketScheduler:
    global _instance
    if _instance is None:
        _instance = MarketScheduler()
    return _instance
