"""APScheduler wiring — cron jobs in Paul's *current* timezone, rescheduled on
the spot when he says "I'm in Dubai". Locked times (Plan §15): brief 07:00,
nudge 13:30, review + Kiefer 21:00; plus 06:30 run protection and hound slots.
"""
from __future__ import annotations

import logging

from app.heartbeat.jobs import HeartbeatJobs

logger = logging.getLogger(__name__)

JOB_TIMES = {
    "run_protect": (6, 30),
    "morning_brief": (7, 0),
    "midday_nudge": (13, 30),
    "hound_1530": (15, 30),
    "hound_1730": (17, 30),
    "hound_1900": (19, 0),
    "evening_review": (21, 0),
    "kiefer_note": (21, 0),
}


class Heartbeat:
    def __init__(self, jobs: HeartbeatJobs) -> None:
        self.jobs = jobs
        self._scheduler = None

    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler()
        await self._register_all()
        self._scheduler.start()
        logger.info("Heartbeat started")

    async def _register_all(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        timezone = await self.jobs.store.get(
            "current_timezone", self.jobs.settings.timezone_default
        )
        callables = {
            "run_protect": self.jobs.run_protect,
            "morning_brief": self.jobs.morning_brief,
            "midday_nudge": self.jobs.midday_nudge,
            "hound_1530": self.jobs.hound_ping,
            "hound_1730": self.jobs.hound_ping,
            "hound_1900": self.jobs.hound_ping,
            "evening_review": self.jobs.evening_review,
            "kiefer_note": self.jobs.kiefer_note,
        }
        for job_id, (hour, minute) in JOB_TIMES.items():
            self._scheduler.add_job(
                callables[job_id],
                CronTrigger(hour=hour, minute=minute, timezone=timezone),
                id=job_id,
                replace_existing=True,
                misfire_grace_time=1800,
            )
        logger.info("Heartbeat jobs registered in %s", timezone)

    async def reschedule(self) -> None:
        """Called after a timezone change — jobs move to the new local times."""
        if self._scheduler is not None:
            await self._register_all()

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
