"""The four streaks (run · workout · the 12 · meals) and the activity phrases
that feed them. No points system — the reward is watching the numbers climb."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.db.base import Database

STREAK_TYPES = ("run", "workout", "twelve", "meals", "portuguese")
# "twelve" keeps its DB key for continuity; the habit is now "Today's Focus".
# Run/workout still track internally (they power the gates), but they're
# REPORTED as recovery-aware monthly counts (§11), never as broken streaks.
STREAK_LABELS = {
    "run": "Run",
    "workout": "Workout",
    "twelve": "Today's Focus",
    "meals": "Meals on-plan",
    "portuguese": "Portuguese",
}
# The daily-doable habits shown as streaks (§11); physical training is monthly.
DAILY_STREAKS = ("twelve", "meals", "portuguese")

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
PORTUGUESE_DONE = re.compile(
    r"\bportuguese\b.{0,26}\b(done|practi[cs]ed|finished|in|lesson done)\b"
    r"|\b(did|finished|practi[cs]ed)\b.{0,20}\bportuguese\b"
    r"|\bduolingo\b.{0,20}\b(done|kept|finished)\b",
    re.IGNORECASE,
)


NEGATION = re.compile(
    r"\bhaven'?t\b|\bhasn'?t\b|\bdidn'?t\b|\bnot (?:done|yet|managed|been)\b|\bno run\b"
    r"|\byet to\b|\bstill (?:need|got|haven'?t|to do)\b|\bmissed\b|\bskipped\b|\bcouldn'?t\b"
    r"|\bgoing to\b|\bgonna\b|\babout to\b|\bwill do\b|\blater\b",
    re.IGNORECASE,
)


def looks_negated(text: str) -> bool:
    """Cheap guard: the message contains not-done/future language. Used as the
    fallback when the model confirmation is unavailable — prefer under-logging
    (Jarvis nags) to falsely crediting an activity."""
    return bool(NEGATION.search(text))


def detect_activities(text: str) -> list[str]:
    found = []
    if RUN_DONE.search(text):
        found.append("run")
    if WORKOUT_DONE.search(text):
        found.append("workout")
    if MEALS_DONE.search(text):
        found.append("meals")
    if PORTUGUESE_DONE.search(text):
        found.append("portuguese")
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
        if streak_type in ("run", "workout"):
            await self._log_activity(streak_type, on_date)
        return {"type": streak_type, "current": current, "best": best, "changed": True}

    # --- Recovery-aware physical activity (§11): dated events → monthly counts.

    async def _log_activity(self, kind: str, on_date: date) -> None:
        exists = await self._db.fetch_one(
            "SELECT id FROM activity_log WHERE kind = ? AND day = ?",
            (kind, on_date.isoformat()),
        )
        if not exists:
            await self._db.execute(
                "INSERT INTO activity_log (day, kind) VALUES (?, ?)",
                (on_date.isoformat(), kind),
            )

    async def record_recovery(self, on_date: date) -> None:
        """An explicit rest day — valid training, never a broken anything."""
        await self._log_activity("recovery", on_date)

    async def monthly_activity(self, today: date) -> dict[str, int]:
        month = today.isoformat()[:7]
        result = {}
        for kind in ("run", "workout", "recovery"):
            row = await self._db.fetch_one(
                "SELECT COUNT(DISTINCT day) AS n FROM activity_log WHERE kind = ? AND day LIKE ?",
                (kind, f"{month}%"),
            )
            result[kind + "s" if kind != "recovery" else "recovery_days"] = (
                int(row["n"]) if row else 0
            )
        return result

    async def unrecord(self, streak_type: str, on_date: date) -> bool:
        """Undo a same-day record (Paul corrects a wrong log). Returns whether
        anything was undone."""
        assert streak_type in STREAK_TYPES
        row = await self._db.fetch_one("SELECT * FROM streaks WHERE type = ?", (streak_type,))
        if row is None or row["last_date"] != on_date.isoformat():
            return False
        current = max(0, row["current_count"] - 1)
        best = row["best_count"]
        if best == row["current_count"]:
            best = max(current, best - 1)
        last = (on_date - timedelta(days=1)).isoformat() if current > 0 else ""
        await self._db.execute(
            "UPDATE streaks SET current_count = ?, best_count = ?, last_date = ? WHERE type = ?",
            (current, best, last, streak_type),
        )
        if streak_type in ("run", "workout"):
            await self._db.execute(
                "DELETE FROM activity_log WHERE kind = ? AND day = ?",
                (streak_type, on_date.isoformat()),
            )
        return True

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
