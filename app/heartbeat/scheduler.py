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
        from functools import partial

        from apscheduler.triggers.cron import CronTrigger

        timezone = await self.jobs.store.get(
            "current_timezone", self.jobs.settings.timezone_default
        )
        callables = {
            "run_protect": self.jobs.run_protect,
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
        # Master Update day-rhythm jobs — all in Paul's CURRENT timezone.
        extras = [
            # Brief lands when Paul's morning starts: hourly slots, the job
            # holds until his declared wake hour, once-guard stops doubles.
            ("morning_brief", self.jobs.morning_brief, CronTrigger(hour="7-10", minute=0, timezone=timezone), 1800),
            # §4: escalating wake loop, ~every 3 min from 05:00 (no-op until enabled)
            ("wake_tick", self.jobs.wake_tick, CronTrigger(hour="5-8", minute="*/3", timezone=timezone), 120),
            # Wake & Hydrate v2: minute beat, any hour — fires at the gospel
            # time, then drives the callback/hydration loop (cheap no-op idle)
            ("wake2_tick", self.jobs.wake2_tick, CronTrigger(minute="*", timezone=timezone), 55),
            # Timed reminder calls ('call me in 40 minutes...') — minute beat
            ("call_tick", self.jobs.scheduled_calls_tick, CronTrigger(minute="*", timezone=timezone), 55),
            # §5: hourly move + water through the waking day
            ("move_water", self.jobs.move_water_nudge, CronTrigger(hour="8-20", minute=5, timezone=timezone), 900),
            # §6: meds/supplements/TRT (TRT job self-checks for Saturday)
            ("med_adhd", partial(self.jobs.med_reminder, "adhd"), CronTrigger(hour=9, minute=30, timezone=timezone), 1800),
            ("med_supplements", partial(self.jobs.med_reminder, "supplements"), CronTrigger(hour=14, minute=0, timezone=timezone), 1800),
            ("med_trt", partial(self.jobs.med_reminder, "trt"), CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=timezone), 1800),
            # §10: wind-down check-in after the 21:00 review
            ("bedtime_nudge", self.jobs.bedtime_nudge, CronTrigger(hour=21, minute=45, timezone=timezone), 1800),
            # 3× daily work-group digest (since-last-update, watermarked)
            ("digest_am", self.jobs.group_digest, CronTrigger(hour=8, minute=30, timezone=timezone), 1800),
            ("digest_noon", self.jobs.group_digest, CronTrigger(hour=13, minute=0, timezone=timezone), 1800),
            ("digest_eve", self.jobs.group_digest, CronTrigger(hour=18, minute=0, timezone=timezone), 1800),
            # Cache honesty: hourly board re-sync through the working day
            ("trello_resync", self.jobs.trello_resync, CronTrigger(hour="7-22", minute=40, timezone=timezone), 900),
            # Gates chase, never block (Paul, 3 Aug): hourly owed-items reminder
            ("gate_chaser", self.jobs.gate_chaser, CronTrigger(hour="10-21", minute=15, timezone=timezone), 900),
            # Phase A3: the Continuous Mind — hourly thinking pass, usually silent
            ("mind_tick", self.jobs.mind_tick, CronTrigger(hour="7-22", minute=50, timezone=timezone), 900),
            # Phase A1: the living Paul Brief re-learns him nightly at 21:55
            ("paul_brief", self.jobs.refresh_paul_brief, CronTrigger(hour=21, minute=55, timezone=timezone), 1800),
            # Paul's rule: 22:30 lights out without fail (plus one 23:00 chaser)
            ("lights_out", self.jobs.lights_out, CronTrigger(hour=22, minute=30, timezone=timezone), 900),
            ("lights_out_chaser", self.jobs.lights_out_chaser, CronTrigger(hour=23, minute=0, timezone=timezone), 900),
        ]
        for job_id, func, trigger, grace in extras:
            self._scheduler.add_job(
                func, trigger, id=job_id, replace_existing=True, misfire_grace_time=grace
            )
        logger.info("Heartbeat jobs registered in %s", timezone)

    async def reschedule(self) -> None:
        """Called after a timezone change — jobs move to the new local times."""
        if self._scheduler is not None:
            await self._register_all()

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
