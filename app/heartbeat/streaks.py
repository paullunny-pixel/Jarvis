"""The four streaks (run · workout · the 12 · meals) and the activity phrases
that feed them. No points system — the reward is watching the numbers climb."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.db.base import Database

STREAK_TYPES = ("run", "workout", "twelve", "meals")
STREAK_LABELS = {"run": "Run", "workout": "Workout", "twelve": "Daily 12", "meals": "Meals on-plan"}

RUN_DONE = re.compile(
    r"\b(run|5k|5 ?km)\b.{0,20}\b(done|smashed|in the bag|finished|complete)\b"
    r"|\b(did|done|been on|just did|finished)\b.{0,16}\b(my |the |a )?(run|5k|5 ?km)\b"
    r"|\bjust ran\b",
    re.IGNORECASE,
)
WORKOUT_DONE = re.compile(
    r"\b(workout|gym|session|push day|pull day|leg day|legs day)\b.{0,20}\b(done|smashed|finished|complete)\b"
    r"|\b(did|done|finished|smashed)\b.{0,16}\b(my |the |a )?(workout|gym|session|push|pull|legs)\b",
    re.IGNORECASE,
)
MEALS_DONE = re.compile(
    r"\bmeals?\b.{0,26}\b(on plan|on-plan|done|all in|logged|prepped and eaten|hit)\b"
    r"|\b(ate|hit)\b.{0,16}\b(clean|my macros|on plan)\b",
    re.IGNORECASE,
)


def detect_activities(text: str) -> list[str]:
    found = []
    if RUN_DONE.search(text):
        found.append("run")
    if WORKOUT_DONE.search(text):
        found.append("workout")
    if MEALS_DONE.search(text):
        found.append("meals")
    return found


class Streaks:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, streak_type: str, on_date: date) -> dict[str, Any]:
        """Count today's activity. Consecutive days extend the streak; a gap
        resets it (yesterday's count is what was lost). Same-day repeats no-op."""
        assert streak_type in STREAK_TYPES
        row = await self._db.fetch_one("SELECT * FROM streaks WHERE type = ?", (streak_type,))
        today_iso = on_date.isoformat()
        yesterday_iso = (on_date - timedelta(days=1)).isoformat()
        if row is None:
            current, best = 1, 1
            await self._db.execute(
                "INSERT INTO streaks (type, current_count, best_count, last_date) VALUES (?, 1, 1, ?)",
                (streak_type, today_iso),
            )
        else:
            if row["last_date"] == today_iso:
                return {"type": streak_type, "current": row["current_count"], "best": row["best_count"], "changed": False}
            current = row["current_count"] + 1 if row["last_date"] == yesterday_iso else 1
            best = max(current, row["best_count"])
            await self._db.execute(
                "UPDATE streaks SET current_count = ?, best_count = ?, last_date = ? WHERE type = ?",
                (current, best, today_iso, streak_type),
            )
        return {"type": streak_type, "current": current, "best": best, "changed": True}

    async def snapshot(self, today: date) -> dict[str, dict[str, Any]]:
        """Current view of all four streaks; a streak whose last_date is older
        than yesterday shows as broken (0) without being rewritten."""
        result: dict[str, dict[str, Any]] = {}
        yesterday_iso = (today - timedelta(days=1)).isoformat()
        for streak_type in STREAK_TYPES:
            row = await self._db.fetch_one("SELECT * FROM streaks WHERE type = ?", (streak_type,))
            if row is None:
                result[streak_type] = {"current": 0, "best": 0, "done_today": False}
            else:
                alive = row["last_date"] >= yesterday_iso
                result[streak_type] = {
                    "current": row["current_count"] if alive else 0,
                    "best": row["best_count"],
                    "done_today": row["last_date"] == today.isoformat(),
                }
        return result

    async def done_today(self, streak_type: str, today: date) -> bool:
        row = await self._db.fetch_one("SELECT last_date FROM streaks WHERE type = ?", (streak_type,))
        return bool(row and row["last_date"] == today.isoformat())
