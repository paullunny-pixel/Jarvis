"""Meetings, Layer B (build order §6, riding the 6 Aug Zoom brief): OtterPilot
already attends and transcribes Paul's meetings on his existing Otter.ai
subscription — no new account, no bot for Jarvis to dispatch. The build
order is explicit: 'ignore §1's custom auto-join bot — Otter does that;
Jarvis just consumes Otter's output.' This module IS that consumption.

Route: the Otter public API needs its own credential Paul hasn't set up, so
this rides the brief's 'robust fallback' — the meeting notes Otter emails,
read through the mail accounts already wired for Phase 2. No new keys.

Otter emails are hunted (not marked read — Jarvis never touches Paul's
inbox state), turned into a summary + decisions + actions by the brain
model, filed as Trello cards in Brain Dump, remembered in the second brain
tagged by meeting/company/attendees, and Paul gets a short Telegram note.
Dedup rides a settings-stored hash set since IMAP UNSEEN can't be trusted
(BODY.PEEK never advances it).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

ENABLED_KEY = "meeting_notetaker_enabled"
SEEN_KEY = "meeting_notetaker_seen"
SEEN_CAP = 300

OTTER_SENDER = re.compile(r"@otter\.ai$", re.IGNORECASE)
JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

EXTRACTION_SYSTEM = """\
You are reading an Otter.ai meeting-summary email that landed in Paul's \
inbox. Extract what's real and return ONLY a JSON object:
{"title": "<short meeting title>", "company": "<best-guess company this \
relates to, or empty>", "attendees": ["..."], "summary": "<3-4 sentence \
plain-English summary>", "decisions": ["..."], "actions": [{"title": \
"<short imperative action>", "owner": "<name mentioned, or empty>", \
"due": "<natural-language date IF actually stated, else empty>"}]}

Rules:
- Work ONLY from the text given. Never invent people, decisions or actions.
- If this doesn't look like real meeting content (an Otter marketing email, \
a billing receipt, a login alert), return {"title": "", "actions": []} and \
nothing else.
- "actions" is capped at 10, "attendees" at 8. Quality over quantity.\
"""


class MeetingNotetaker:
    """Watches configured inboxes for Otter's post-meeting emails and turns
    them into filed actions, a remembered meeting, and a Telegram note."""

    def __init__(
        self,
        mail,             # MailService | None
        claude,            # ClaudeClient
        layer_factory,     # async () -> TrelloLayer | None
        memory,             # MemoryStore | None
        store,              # SettingsStore
        send_text,          # async (str) -> None
    ) -> None:
        self.mail = mail
        self.claude = claude
        self._layer_factory = layer_factory
        self.memory = memory
        self._store = store
        self._send_text = send_text

    async def enabled(self) -> bool:
        return await self._store.get(ENABLED_KEY, "on") != "off"

    async def set_enabled(self, on: bool) -> None:
        await self._store.set(ENABLED_KEY, "on" if on else "off")

    async def _seen(self) -> list[str]:
        try:
            return json.loads(await self._store.get(SEEN_KEY, "[]"))
        except Exception:
            return []

    async def _mark_seen(self, hashes: list[str]) -> None:
        await self._store.set(SEEN_KEY, json.dumps(hashes[-SEEN_CAP:]))

    @staticmethod
    def _hash(message: dict) -> str:
        raw = f"{message.get('from_address', '')}|{message.get('subject', '')}|{message.get('date', '')}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    async def check_new(self) -> int:
        """Scan every inbox for fresh Otter mail, file what's found. Returns
        the number of meetings actually processed. Never raises — a scan
        that half-fails still marks what it saw so it doesn't loop forever."""
        if self.mail is None or not await self.enabled():
            return 0
        seen = await self._seen()
        seen_set = set(seen)
        processed = 0
        for client in self.mail.match(""):
            try:
                messages, _total = await client.search_messages("otter.ai", limit=15)
            except Exception:
                logger.exception("Otter mail search failed for %s", client.account.label)
                continue
            for message in messages:
                if not OTTER_SENDER.search(message.get("from_address", "")):
                    continue
                key = self._hash(message)
                if key in seen_set:
                    continue
                seen_set.add(key)
                seen.append(key)
                try:
                    if await self._process(message):
                        processed += 1
                except Exception:
                    logger.exception(
                        "Otter email processing failed: %s", message.get("subject")
                    )
        await self._mark_seen(seen)
        return processed

    async def _extract(self, message: dict) -> dict:
        body = (
            f"Subject: {message.get('subject', '')}\n"
            f"From: {message.get('from', '')}\n"
            f"Date: {message.get('date', '')}\n\n{message.get('snippet', '')}"
        )
        try:
            raw = await self.claude.quick(body, system=EXTRACTION_SYSTEM, max_tokens=900)
        except Exception:
            logger.exception("Otter extraction call failed")
            return {}
        match = JSON_OBJ.search(raw)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except Exception:
            return {}

    async def _process(self, message: dict) -> bool:
        parsed = await self._extract(message)
        title = str(parsed.get("title", "")).strip()
        if not title:
            return False   # not real meeting content — seen, but nothing filed
        stamp = message.get("date", "") or datetime.now().strftime("%d %b")
        actions = [a for a in (parsed.get("actions") or [])[:10] if str(a.get("title", "")).strip()]
        filed = await self._file_cards(title, stamp, parsed, actions)
        await self._remember(title, stamp, parsed)
        await self._notify(title, parsed, filed)
        return True

    async def _file_cards(self, title: str, stamp: str, parsed: dict, actions: list[dict]) -> int:
        if self._layer_factory is None or not actions:
            return 0
        try:
            layer = await self._layer_factory()
        except Exception:
            logger.exception("Trello layer unavailable — meeting actions not filed")
            return 0
        if layer is None:
            return 0
        from app.meetings import parse_when

        now = datetime.now(layer.owner_tz)
        summary = str(parsed.get("summary", "")).strip()
        filed = 0
        for action in actions:
            action_title = str(action.get("title", "")).strip()[:120]
            due_text = str(action.get("due", "")).strip()
            due_local = parse_when(due_text, now) if due_text else None
            try:
                await layer.create_card_full(
                    board_key="master",
                    list_name="Brain Dump",
                    title=action_title,
                    desc=(
                        f"From the meeting: {title} ({stamp})\n\n"
                        f"Why: {summary or 'action raised in this meeting'}\n\n"
                        "Filed by Jarvis from Otter's meeting notes."
                    ),
                    owner_name=str(action.get("owner", "")).strip(),
                    due_local=due_local,
                )
                filed += 1
            except Exception:
                logger.exception("Failed to file meeting action card: %s", action_title)
        if filed:
            try:
                decisions = [str(d) for d in (parsed.get("decisions") or [])]
                desc = summary
                if decisions:
                    desc += "\n\nDecisions:\n- " + "\n- ".join(decisions)
                desc += "\n\nFiled by Jarvis from Otter's meeting notes."
                await layer.create_card_full(
                    board_key="master",
                    list_name="Brain Dump",
                    title=f"Meeting summary — {title}, {stamp}",
                    desc=desc,
                )
            except Exception:
                logger.exception("Failed to file meeting summary card")
        return filed

    async def _remember(self, title: str, stamp: str, parsed: dict) -> None:
        if self.memory is None:
            return
        company = str(parsed.get("company", "")).strip()
        attendees = [str(a).strip() for a in (parsed.get("attendees") or [])[:8] if str(a).strip()]
        lines = [f"Meeting: {title} ({stamp})"]
        if company:
            lines.append(f"Company: {company}")
        if attendees:
            lines.append("Attendees: " + ", ".join(attendees))
        summary = str(parsed.get("summary", "")).strip()
        if summary:
            lines.append(summary)
        decisions = [str(d) for d in (parsed.get("decisions") or [])]
        if decisions:
            lines.append("Decisions: " + "; ".join(decisions))
        try:
            await self.memory.add_chunk(
                "\n".join(lines),
                room="companies",
                type_="STABLE",
                source="meeting:otter",
                tags=[t for t in ["meeting", company, *attendees] if t][:6],
            )
        except Exception:
            logger.exception("Failed to store meeting in second brain")

    async def _notify(self, title: str, parsed: dict, filed: int) -> None:
        summary = str(parsed.get("summary", "")).strip()
        text = f'\U0001F4DD Meeting notes in — "{title}".'
        if summary:
            text += f" {summary}"
        if filed:
            text += f" Added {filed} action{'s' if filed != 1 else ''} to Brain Dump."
        else:
            text += " No clear actions to file."
        try:
            await self._send_text(text)
        except Exception:
            logger.exception("Failed to notify Paul about meeting notes")
