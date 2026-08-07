"""Meds refill tracking (7 Aug build slice): TRT and ADHD meds warn ahead
of running out. Paul gives either a run-out/refill date directly, or a
quantity + dosing cadence Jarvis works the date out from — either way the
service stores ONE run_out_date per item and warns warn_days_before it
arrives. Ties into the existing non-suppressible med reminder channel
(essential=True) — quiet day never silences it.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.core.store import utc_now_iso
from app.db.base import Database

TRACKED_ITEMS = ("adhd", "trt", "supplements")


class MedSupply:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def set_by_date(self, item: str, run_out_date_iso: str, warn_days_before: int = 5) -> str:
        item = item.strip().lower()
        if item not in TRACKED_ITEMS:
            return f"Don't recognise '{item}' — track adhd, trt or supplements."
        run_out_date_iso = (run_out_date_iso or "")[:10]
        try:
            date.fromisoformat(run_out_date_iso)
        except ValueError:
            return f"'{run_out_date_iso}' isn't a date I can read — give me YYYY-MM-DD."
        await self._upsert(item, run_out_date_iso, warn_days_before)
        return (
            f"{item.upper()} tracked — runs out {run_out_date_iso}, "
            f"I'll warn you {warn_days_before} days out."
        )

    async def set_by_quantity(
        self, item: str, quantity: float, doses_per_day: float,
        start_date_iso: str = "", warn_days_before: int = 5,
    ) -> str:
        item = item.strip().lower()
        if item not in TRACKED_ITEMS:
            return f"Don't recognise '{item}' — track adhd, trt or supplements."
        if quantity <= 0 or doses_per_day <= 0:
            return "NOTHING SET — need a real quantity and a dosing rate."
        start = date.fromisoformat(start_date_iso[:10]) if start_date_iso else date.today()
        days_of_supply = int(quantity / doses_per_day)
        run_out = start + timedelta(days=days_of_supply)
        await self._upsert(item, run_out.isoformat(), warn_days_before)
        return (
            f"{item.upper()} tracked — {quantity:g} at {doses_per_day:g}/day from "
            f"{start.isoformat()} runs out {run_out.isoformat()}, I'll warn you "
            f"{warn_days_before} days out."
        )

    async def _upsert(self, item: str, run_out_date: str, warn_days_before: int) -> None:
        if self._db.dialect == "postgres":
            await self._db.execute(
                "INSERT INTO med_supply (item, run_out_date, warn_days_before, updated_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (item) DO UPDATE SET"
                " run_out_date = EXCLUDED.run_out_date, warn_days_before = EXCLUDED.warn_days_before,"
                " updated_at = EXCLUDED.updated_at",
                (item, run_out_date, warn_days_before, utc_now_iso()),
            )
        else:
            await self._db.execute(
                "INSERT OR REPLACE INTO med_supply (item, run_out_date, warn_days_before, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (item, run_out_date, warn_days_before, utc_now_iso()),
            )

    async def status(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT item, run_out_date, warn_days_before FROM med_supply"
        )
        return [dict(r) for r in rows]

    async def due_warnings(self, today: date) -> list[dict[str, Any]]:
        """Items within their warn window that haven't run out yet — the
        tick's worklist. Once past run-out it stops nagging (that's now a
        'you're out' conversation Paul will have started himself)."""
        out = []
        for row in await self.status():
            try:
                run_out = date.fromisoformat(row["run_out_date"])
            except ValueError:
                continue
            days_left = (run_out - today).days
            if 0 <= days_left <= row["warn_days_before"]:
                out.append({**row, "days_left": days_left})
        return out
