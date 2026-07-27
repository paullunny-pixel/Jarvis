"""Progress Cockpit data — everything in one glance, from live backend data.

The sobriety panel gets only a day count and a supportive line — never notes,
triggers or history. Everything else follows the same privacy wall as the rest
of the system: business surfaces never read the private room beyond that count.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.store import SettingsStore
from app.daily12.scoring import COMPANIES, COMPANY_NAMES
from app.db.base import Database
from app.heartbeat.calendar_ics import IcsCalendar
from app.heartbeat.jobs import assert_no_private_content, compose_kiefer_data
from app.heartbeat.streaks import Streaks
from app.memory.store import LivingFacts

COMPANY_INITIALS = {"derma_uk": "DU", "derma_eu": "DE", "aesthetics_supply": "AS", "prodermis": "P"}
COMPANY_GRADIENTS = {
    "derma_uk": "linear-gradient(135deg,#38bdf8,#3987e5)",
    "derma_eu": "linear-gradient(135deg,#fab219,#ec833a)",
    "aesthetics_supply": "linear-gradient(135deg,#e08af0,#a855c7)",
    "prodermis": "linear-gradient(135deg,#2dd4bf,#199e70)",
}


class CockpitService:
    def __init__(
        self,
        db: Database,
        living: LivingFacts | None = None,
        calendar: IcsCalendar | None = None,
        timezone_default: str = "Europe/London",
    ) -> None:
        self._db = db
        self._living = living
        self._calendar = calendar
        self._settings = SettingsStore(db)
        self._streaks = Streaks(db)
        self._tz_default = timezone_default

    async def gather(self) -> dict[str, Any]:
        tz_name = await self._settings.get("current_timezone", self._tz_default)
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        today = now.date()
        return {
            "generated_at": now.strftime("%H:%M"),
            "location": _location_label(tz_name, now),
            "day_with_jarvis": await self._days_with_jarvis(today),
            "streaks": await self._streaks.snapshot(today),
            "twelve": await self._twelve(today),
            "body": await self._body(),
            "villa": await self._villa(),
            "today": await self._timeline(today, tz),
            "kiefer": await self._kiefer_preview(today),
            "sobriety": await self._sobriety(),
            "rhythm": await self._rhythm(today),
        }

    async def _rhythm(self, today: date) -> dict[str, Any]:
        """Master Update §§4-6: wake time, water, movement, med adherence."""
        from app.config import get_settings

        day = today.isoformat()
        water = await self._db.fetch_one("SELECT ml FROM water_log WHERE day = ?", (day,))
        moves = await self._db.fetch_one("SELECT count FROM movement_log WHERE day = ?", (day,))
        wake = await self._db.fetch_one(
            "SELECT wake_time, method FROM wake_log WHERE day = ?", (day,)
        )
        meds = {
            row["item"]: True
            for row in await self._db.fetch_all(
                "SELECT item FROM med_adherence WHERE day = ?", (day,)
            )
        }
        return {
            "water_ml": int(water["ml"]) if water else 0,
            "water_target_ml": get_settings().water_target_ml,
            "movements": int(moves["count"]) if moves else 0,
            "woke_at": wake["wake_time"][11:16] if wake else "",
            "wake_method": wake["method"] if wake else "",
            "meds": {item: meds.get(item, False) for item in ("adhd", "supplements", "trt")},
        }

    async def _days_with_jarvis(self, today: date) -> int:
        row = await self._db.fetch_one("SELECT MIN(ts) AS first FROM messages")
        if not row or not row["first"]:
            return 1
        first = datetime.fromisoformat(row["first"]).date()
        return max(1, (today - first).days + 1)

    async def _twelve(self, today: date) -> dict[str, Any]:
        rows = await self._db.fetch_all(
            "SELECT d.position, d.done, d.company_slug, t.title FROM daily_12 d"
            " JOIN tasks t ON t.id = d.task_id WHERE d.plan_date = ?"
            " ORDER BY d.position",
            (today.isoformat(),),
        )
        main = [r for r in rows if r["position"] != 0]
        bonus = next((r for r in rows if r["position"] == 0), None)
        done = sum(1 for r in main if r["done"])
        by_company = []
        for slug in COMPANIES:
            tasks = [
                {"position": r["position"], "title": r["title"], "done": bool(r["done"])}
                for r in main
                if r["company_slug"] == slug
            ]
            by_company.append(
                {
                    "slug": slug,
                    "name": COMPANY_NAMES[slug],
                    "initials": COMPANY_INITIALS[slug],
                    "gradient": COMPANY_GRADIENTS[slug],
                    "tasks": tasks,
                }
            )
        return {
            "done": done,
            "total": len(main),
            "by_company": by_company,
            "bonus": (
                {"title": bonus["title"], "done": bool(bonus["done"])}
                if bonus is not None and done >= len(main) and main
                else None
            ),
        }

    async def _body(self) -> dict[str, Any]:
        weights = await self._db.fetch_all(
            "SELECT stat_date, weight_kg FROM health_stats WHERE weight_kg > 0 ORDER BY stat_date"
        )
        runs = await self._db.fetch_all(
            "SELECT run_date, distance_km, duration_min FROM runs"
            " WHERE duration_min > 0 AND distance_km >= 4.5 ORDER BY run_date"
        )
        body: dict[str, Any] = {"weight": None, "run_pace": None}
        if weights:
            body["weight"] = {
                "first": weights[0]["weight_kg"],
                "latest": weights[-1]["weight_kg"],
                "series": [w["weight_kg"] for w in weights][-12:],
            }
        if runs:
            times = [r["duration_min"] for r in runs]
            body["run_pace"] = {
                "first_min": times[0],
                "latest_min": times[-1],
                "best_min": min(times),
                "series": times[-12:],
            }
        if self._living is not None:
            body["weight_note"] = await self._living.get("health.weight") or ""
        return body

    async def _villa(self) -> dict[str, Any]:
        if self._living is None:
            return {"available": False}
        paid = await self._living.get("villa.paid") or ""
        pct_match = re.search(r"(\d+)\s*%", paid)
        return {
            "available": bool(paid),
            "pct": int(pct_match.group(1)) if pct_match else 0,
            "paid_label": paid,
            "price_label": await self._living.get("villa.price") or "",
            "next_demand": await self._living.get("villa.next_demand") or "",
            "flags": [f for f in [(await self._living.get("villa.flags")) or ""] if f],
        }

    async def _timeline(self, today: date, tz: ZoneInfo) -> list[dict[str, Any]]:
        snapshot = await self._streaks.snapshot(today)
        slots = [
            {"time": "06:30", "title": "5km run — the keystone", "type": "run",
             "done": snapshot["run"]["done_today"]},
            {"time": "07:00", "title": "Morning brief with Jarvis", "type": "rev", "done": False},
            {"time": "09:00", "title": "Today's Focus — deep-work block", "type": "work", "done": False},
            {"time": "13:30", "title": "Midday check-in", "type": "rev", "done": False},
            {"time": "17:30", "title": "Workout (Push/Pull/Legs)", "type": "gym",
             "done": snapshot["workout"]["done_today"]},
            {"time": "21:00", "title": "Evening review + plan tomorrow", "type": "rev", "done": False},
        ]
        if self._calendar is not None:
            for event in await self._calendar.events_for(today, tz):
                slots.append(
                    {"time": event["time"] if event["time"] != "all day" else "—",
                     "title": event["title"], "type": "cal", "done": False}
                )
        slots.sort(key=lambda s: s["time"])
        return slots

    async def _kiefer_preview(self, today: date) -> dict[str, Any]:
        twelve = await self._twelve(today)
        snapshot = await self._streaks.snapshot(today)
        preview = compose_kiefer_data(today, twelve["done"], twelve["total"], snapshot)
        assert_no_private_content(preview)
        return {
            "preview": preview,
            "run": snapshot["run"]["done_today"],
            "workout": snapshot["workout"]["done_today"],
            "twelve": f"{twelve['done']} / {twelve['total'] or 12}",
        }

    async def _sobriety(self) -> dict[str, Any]:
        """Day count only — the panel is supportive, not a dossier."""
        if self._living is None:
            return {"days": None}
        raw = await self._living.get("sobriety.days")
        if raw is None:
            start = await self._living.get("sobriety.start_date")
            if start:
                try:
                    days = (date.today() - date.fromisoformat(start[:10])).days
                    return {"days": max(days, 0)}
                except ValueError:
                    return {"days": None}
            return {"days": None}
        match = re.search(r"\d+", raw)
        return {"days": int(match.group(0)) if match else None}


def _location_label(tz_name: str, now: datetime) -> str:
    offset = now.strftime("%z")
    pretty = f"{offset[:3]}:{offset[3:]}" if offset else ""
    place = "Dubai" if "Dubai" in tz_name else "UK"
    return f"{place} · UTC{pretty}"
