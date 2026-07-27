"""Backend tools the live voice agent calls mid-conversation.

Each returns a short plain-text result the agent speaks from. The private
wall holds here exactly as in chat: memory recall excludes the private room.
Action tools mutate real state, so the agent is instructed (engine prompt)
to call them only on Paul's explicit say-so — the live-call equivalent of
the negation-aware logging invariant.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.memory.writer import format_memory_context

logger = logging.getLogger(__name__)


class VoiceTools:
    def __init__(self, db, memory=None, living=None, daily12=None, mail=None, jobs=None,
                 timezone_default: str = "Europe/London") -> None:
        self._db = db
        self._memory = memory
        self._living = living
        self._daily12 = daily12
        self._mail = mail
        self._jobs = jobs
        self._tz_default = timezone_default

    async def _today(self):
        from app.core.store import SettingsStore

        tz = await SettingsStore(self._db).get("current_timezone", self._tz_default)
        return datetime.now(ZoneInfo(tz)).date()

    async def dispatch(self, name: str, args: dict) -> str:
        try:
            handler = getattr(self, f"tool_{name}", None)
            if handler is None:
                return f"Unknown tool '{name}'."
            return await handler(args or {})
        except Exception:
            logger.exception("Voice tool %s failed", name)
            return "That tool hit a snag — carry on without it and mention it to Paul."

    async def tool_recall_memory(self, args: dict) -> str:
        if self._memory is None or self._living is None:
            return "Memory isn't connected on this deployment."
        query = str(args.get("query", "")).strip()
        if not query:
            return "Give me a query to search."
        chunks = await self._memory.search(query, k=6, min_score=0.15)
        living = await self._living.all_current(exclude_private=True)
        context = format_memory_context(chunks, living)
        return context or "Nothing relevant in memory for that."

    async def tool_todays_focus(self, args: dict) -> str:
        if self._daily12 is None:
            return "Trello isn't connected."
        return await self._daily12.format_plan()

    async def tool_mark_done(self, args: dict) -> str:
        if self._daily12 is None:
            return "Trello isn't connected."
        return await self._daily12.mark_done(str(args.get("reference", "")))

    async def tool_create_task(self, args: dict) -> str:
        if self._daily12 is None:
            return "Trello isn't connected."
        return await self._daily12.create(
            str(args.get("title", "New task")), assignee=str(args.get("assignee", "") or "")
        )

    async def tool_log_water(self, args: dict) -> str:
        if self._jobs is None:
            return "Day-rhythm tracking isn't connected."
        ml = int(args.get("ml") or 300)
        total = await self._jobs.log_water(await self._today(), ml)
        return f"Logged — {total / 1000:.1f}L today."

    async def tool_log_movement(self, args: dict) -> str:
        if self._jobs is None:
            return "Day-rhythm tracking isn't connected."
        count = await self._jobs.log_movement(await self._today())
        return f"Movement logged — {count} today."

    async def tool_inbox_overview(self, args: dict) -> str:
        if self._mail is None:
            return "Email isn't connected."
        return await self._mail.overview()
