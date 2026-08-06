"""Non-skippable gates (Paul's request): the run and the meds are not
optional. Past their morning deadline, unconfirmed gates block the working
day — Jarvis will not serve the Daily 12 or press on with tasks until each is
confirmed. The run can be proven with a photo of the stats (vision-verified).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from app.core.store import SettingsStore, utc_now_iso
from app.db.base import Database
from app.heartbeat.streaks import Streaks

DEFAULT_GATES = [
    {"id": "run", "label": "the 5km run", "by": "09:30"},
    {"id": "meds", "label": "supplements & medication", "by": "09:00"},
]

MEDS_DONE = re.compile(
    r"\b(meds|medication|medications|supplements|vitamins|trt|testosterone)\b"
    r".{0,30}\b(done|taken|in|had|gone in|sorted|down)\b"
    r"|\b(taken|had|done)\b.{0,24}\b(meds|medication|supplements|vitamins|trt)\b",
    re.IGNORECASE,
)


def mentions_meds(text: str) -> bool:
    return bool(MEDS_DONE.search(text))


MED_ITEM_WORDS = {
    "trt": re.compile(r"\b(trt|testosterone)\b", re.IGNORECASE),
    "supplements": re.compile(r"\b(supplements?|vitamins?|minerals?)\b", re.IGNORECASE),
    "adhd": re.compile(r"\b(adhd|meds?|medications?)\b", re.IGNORECASE),
}


def med_items_mentioned(text: str) -> list[str]:
    """Which schedule items a meds confirmation covers (adherence log §6)."""
    return [item for item, rx in MED_ITEM_WORDS.items() if rx.search(text)]


class GateKeeper:
    def __init__(self, db: Database, streaks: Streaks, run_reminders: bool = True) -> None:
        self._db = db
        self._store = SettingsStore(db)
        self._streaks = streaks
        self._run_reminders = run_reminders

    async def config(self) -> list[dict]:
        raw = await self._store.get("gates_config")
        gates = None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    gates = parsed
            except ValueError:
                pass
        if gates is None:
            gates = list(DEFAULT_GATES)
        if not self._run_reminders:
            # Paul, 6 Aug: run reminders cancelled (blood pressure first) —
            # the run gate leaves the chase entirely; meds carry on.
            gates = [g for g in gates if g.get("id") != "run"]
        return gates

    async def confirm(self, gate_id: str, today: date) -> None:
        await self._store.set(f"gate:{gate_id}:{today.isoformat()}", utc_now_iso())

    async def is_confirmed(self, gate_id: str, today: date) -> bool:
        if gate_id == "run":
            # The run gate follows the streak — a told run, an Apple Health
            # push, or photo proof all open it. An EXPLICIT recovery day does
            # too (§11): rest is valid training, never a locked board.
            if await self._streaks.done_today("run", today):
                return True
            return await self._streaks.recovery_today(today)
        return bool(await self._store.get(f"gate:{gate_id}:{today.isoformat()}"))

    async def outstanding(self, now: datetime) -> list[dict]:
        """Gates past their deadline, unconfirmed, and not overridden today."""
        result = []
        for gate in await self.config():
            if now.strftime("%H:%M") < gate.get("by", "00:00"):
                continue
            if await self.is_overridden(gate["id"], now.date()):
                continue
            if not await self.is_confirmed(gate["id"], now.date()):
                result.append(gate)
        return result

    # --- The universal override (Master Update §1). Unconditional: once Paul
    # gives the word and a reason, the item never blocks again today.

    async def is_overridden(self, item: str, today: date) -> bool:
        return bool(await self._store.get(f"override:{item}:{today.isoformat()}"))

    async def override(self, items: list[str], reason: str, today: date) -> None:
        for item in items:
            await self._store.set(f"override:{item}:{today.isoformat()}", utc_now_iso())
            await self._db.execute(
                "INSERT INTO override_log (ts, item, reason) VALUES (?, ?, ?)",
                (utc_now_iso(), item, reason[:500]),
            )

    @staticmethod
    def block_message(outstanding: list[dict]) -> str:
        labels = " and ".join(g["label"] for g in outstanding)
        proof = " A photo of the run stats settles it instantly." if any(
            g["id"] == "run" for g in outstanding
        ) else ""
        return (
            f"Before we touch the board, sir — {labels} first. Those aren't skippable; "
            f"confirm each to me and the day opens up.{proof}"
        )
