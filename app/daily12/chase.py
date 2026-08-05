"""Phase 2 — the Chase Engine (Paul's spec, 5 Aug).

The loop: each person's evening sweep OBSERVES what didn't close (it never
sends), next morning ONE numbered nudge asks about the carry-overs, their
reply closes the card without opening Trello, and at 22:00 Dubai Paul gets
one digest separating what closed / what's open-and-engaged / what's silent.

The rules that outrank the code:
- Every nudge is a QUESTION, never an accusation — the most common reason a
  card is open is that the work is done and nobody ticked it.
- One message per person, never one per card. Nudge fatigue kills the system.
- nudge:false (Kiefer) means NEVER messaged, asserted at the send boundary —
  but always visible in Paul's digest.
- Working days, not calendar days: Friday's card is day 2 on Monday.
- No stale nudges after downtime; silence outside the nudge window.
- Dry-run mode (chase_dry_run, default ON) logs instead of sending.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PARTICIPANTS: list[dict] = [
    {"key": "harry", "name": "Harry", "board": "harry", "todoList": "Harry Today",
     "timezone": "Europe/London", "workingDays": [0, 1, 2, 3, 4],  # Mon-Fri
     "sweepTime": "17:30", "nudgeTime": "08:30", "nudge": True},
    {"key": "kiefer", "name": "Kiefer Brindle", "board": "master",
     "todoList": "Kiefer Today", "timezone": "Asia/Dubai",
     "workingDays": [0, 1, 2, 3, 4], "nudge": False},   # NEVER messaged
]

CHAT_IDS_KEY = "chase_chat_ids"          # {participant_key: chat_id}
SWEEP_KEY = "chase_sweep"                # {participant_key: {...last sweep}}
LAST_NUDGE_KEY = "chase_last_nudge"      # {participant_key: {date, items:[card ids]}}
HOLIDAY_KEY = "chase_holidays"           # {participant_key: "YYYY-MM-DD" until}
PENDING_LINK_KEY = "chase_pending_link"  # last unknown chat that said hello


class NudgeForbidden(RuntimeError):
    """The send boundary caught an attempt to message a nudge:false person."""


def working_days_between(entered: datetime, now: datetime, working: list[int]) -> int:
    """Day count in the person's working days; day 1 = the entry day.
    Friday-entered, Monday-asked = day 2 (§4 — fair, not stupid)."""
    if entered > now:
        return 1
    days = 0
    cursor = entered.date()
    while cursor <= now.date():
        if cursor.weekday() in working:
            days += 1
        cursor += timedelta(days=1)
    return max(1, days)


class ChaseEngine:
    """Rides the Phase 1 TrelloLayer for reads/writes and the jobs plumbing
    (store, telegram, settings) for everything else."""

    def __init__(self, jobs, layer_factory) -> None:
        self.jobs = jobs
        self.store = jobs.store
        self._layer_factory = layer_factory   # async () -> TrelloLayer
        self.participants = {p["key"]: p for p in PARTICIPANTS}

    async def _layer(self):
        return await self._layer_factory()

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.jobs.settings, "chase_dry_run", True))

    # ------------------------------------------------------------ store bits

    async def _get_json(self, key: str, default: str = "{}") -> Any:
        try:
            return json.loads(await self.store.get(key, default) or default)
        except Exception:
            return json.loads(default)

    async def chat_id_for(self, key: str) -> int:
        ids = await self._get_json(CHAT_IDS_KEY)
        return int(ids.get(key) or 0)

    async def link_chat(self, key: str, chat_id: int) -> str:
        if key not in self.participants:
            return f"UNKNOWN PARTICIPANT '{key}' — known: {', '.join(self.participants)}"
        ids = await self._get_json(CHAT_IDS_KEY)
        ids[key] = chat_id
        await self.store.set(CHAT_IDS_KEY, json.dumps(ids))
        return f"Linked — {self.participants[key]['name']} can now be messaged."

    async def participant_for_chat(self, chat_id: int) -> dict | None:
        ids = await self._get_json(CHAT_IDS_KEY)
        for key, stored in ids.items():
            if int(stored) == int(chat_id) and key in self.participants:
                return self.participants[key]
        return None

    async def on_holiday(self, key: str, today: datetime) -> bool:
        until = (await self._get_json(HOLIDAY_KEY)).get(key, "")
        return bool(until) and today.date().isoformat() <= until

    async def set_holiday(self, key: str, until_iso: str) -> None:
        holidays = await self._get_json(HOLIDAY_KEY)
        holidays[key] = until_iso
        await self.store.set(HOLIDAY_KEY, json.dumps(holidays))

    # ------------------------------------------------------------ the send boundary

    async def _send_to(self, participant: dict, text: str) -> bool:
        """EVERY outbound chase message passes here — the nudge:false assert
        lives at the boundary so it cannot be forgotten (§13)."""
        if not participant.get("nudge"):
            raise NudgeForbidden(
                f"{participant['name']} is nudge:false — never messaged, ever."
            )
        if self.dry_run:
            logger.info("CHASE DRY-RUN → %s: %s", participant["name"], text[:400])
            await self.jobs._send_text(
                f"🧪 CHASE DRY-RUN — would send to {participant['name']}:\n\n{text}"
            )
            return True
        chat_id = await self.chat_id_for(participant["key"])
        if not chat_id:
            await self.jobs._send_text(
                f"⚠️ Can't message {participant['name']} — they haven't started the "
                "bot yet. Send them the bot link and have them tap Start, then tell "
                f"me 'that's {participant['name'].split()[0]}'."
            )
            return False
        try:
            await self.jobs.telegram.send_text(chat_id, text)
            return True
        except Exception:
            logger.exception("Chase send to %s failed", participant["name"])
            await self.jobs._send_text(
                f"⚠️ Telegram refused the message to {participant['name']} — if they "
                "never tapped Start on the bot, that's why."
            )
            return False

    # ------------------------------------------------------------ §6 the sweep

    async def sweep(self, key: str, now: datetime | None = None) -> dict:
        """Observe one person's lists — record, never send. Re-runnable."""
        participant = self.participants[key]
        tz = ZoneInfo(participant["timezone"])
        now = now or datetime.now(tz)
        layer = await self._layer()
        cards = []
        for card in await layer.list_snapshot(participant["board"], participant["todoList"]):
            entered = await layer.entered_current_list_at(card["id"])
            cards.append({
                "id": card["id"], "name": card["name"], "due": card.get("due"),
                "days": working_days_between(
                    entered.astimezone(tz), now, participant["workingDays"]
                ),
            })
        sweeps = await self._get_json(SWEEP_KEY)
        sweeps[key] = {
            "date": now.date().isoformat(), "cards": cards,
            "swept_at": now.isoformat(timespec="seconds"),
        }
        await self.store.set(SWEEP_KEY, json.dumps(sweeps))
        logger.info("Chase sweep %s: %d carry-overs", participant["name"], len(cards))
        return sweeps[key]

    # ------------------------------------------------------------ §7 the nudge

    def _compose_nudge(self, participant: dict, cards: list[dict]) -> str:
        lines = [
            f"Morning {participant['name'].split()[0]} — "
            + (f"{len(cards)} things still on your Today list:"
               if len(cards) > 1 else "one thing still on your Today list:"),
            "",
        ]
        for n, card in enumerate(cards, 1):
            day_bit = f"day {card['days']}"
            help_bit = (
                " — this has been a few days now: need a hand, or should we move "
                "it out of Today?" if card["days"] >= 3 else ""
            )
            lines.append(f"{n}. {card['name']} — {day_bit}{help_bit}")
        lines += [
            "",
            'Reply like "1 done" to close one off, or tell me what\'s blocking '
            "and I'll flag it to Paul.",
        ]
        return "\n".join(lines)

    async def nudge(self, key: str, now: datetime | None = None) -> bool:
        """One message, numbered, working days only, once per day, no stale
        sends: the sweep must be from the LAST working day or today (§14 —
        never fire a backlog after downtime)."""
        participant = self.participants[key]
        if not participant.get("nudge"):
            return False   # Kiefer: the job itself never composes a message
        tz = ZoneInfo(participant["timezone"])
        now = now or datetime.now(tz)
        if now.weekday() not in participant["workingDays"]:
            return False
        if await self.on_holiday(key, now):
            return False
        last = (await self._get_json(LAST_NUDGE_KEY)).get(key, {})
        if last.get("date") == now.date().isoformat():
            return False   # never twice in one day
        sweep = (await self._get_json(SWEEP_KEY)).get(key)
        if not sweep or not sweep.get("cards"):
            return False   # clear list — silence is the reward
        swept = datetime.fromisoformat(sweep["swept_at"])
        if (now - swept) > timedelta(hours=20):
            logger.info("Chase nudge %s skipped — sweep is stale (offline window)", key)
            return False
        sent = await self._send_to(participant, self._compose_nudge(participant, sweep["cards"]))
        if not sent:
            return False
        nudges = await self._get_json(LAST_NUDGE_KEY)
        nudges[key] = {
            "date": now.date().isoformat(),
            "items": [c["id"] for c in sweep["cards"]],
        }
        await self.store.set(LAST_NUDGE_KEY, json.dumps(nudges))
        layer = await self._layer()
        bmap = layer.board(participant["board"])
        stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for card in sweep["cards"]:
            try:
                if "Last Nudged On" in bmap.fields:
                    await layer.client.set_custom_field(
                        card["id"], bmap.fields["Last Nudged On"], {"value": {"date": stamp}}
                    )
                await layer.client.comment(
                    card["id"], f"Jarvis nudged {participant['name']} ({now.date().isoformat()})"
                )
            except Exception:
                logger.exception("Nudge stamp failed for %s", card["name"])
        return True

    # ------------------------------------------------------------ §8 replies

    async def handle_reply(self, key: str, text: str) -> str:
        """A participant's free-text reply → card action → confirmation.
        Numeric first, name-match second, ask third — never guess (§13)."""
        participant = self.participants[key]
        layer = await self._layer()
        last = (await self._get_json(LAST_NUDGE_KEY)).get(key, {})
        item_ids = last.get("items", [])
        lowered = text.lower().strip()

        import re as _re

        target_id = None
        num_hit = _re.match(r"^\s*(?:number\s+)?(\d{1,2})\b", lowered)
        if num_hit and item_ids:
            index = int(num_hit.group(1)) - 1
            if 0 <= index < len(item_ids):
                target_id = item_ids[index]
        sweep_cards = ((await self._get_json(SWEEP_KEY)).get(key) or {}).get("cards", [])
        if target_id is None:
            matches = [
                c for c in sweep_cards
                if any(word in c["name"].lower() for word in lowered.split() if len(word) > 3)
            ]
            if len(matches) == 1:
                target_id = matches[0]["id"]
            elif len(matches) > 1 and not _re.search(r"\bholiday\b", lowered):
                names = " or ".join(c["name"] for c in matches[:3])
                return f"Which one — {names}?"
        card_name = next((c["name"] for c in sweep_cards if c["id"] == target_id), "that one")

        holiday_hit = _re.search(
            r"\b(?:holiday|leave|off)\b.*?\buntil\b\s*(\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+\w+)",
            lowered,
        )
        if holiday_hit or "on holiday" in lowered:
            until = datetime.now(ZoneInfo(participant["timezone"])) + timedelta(days=7)
            raw = holiday_hit.group(1) if holiday_hit else ""
            if _re.match(r"^\d{4}-\d{2}-\d{2}$", raw or ""):
                until_iso = raw
            else:
                until_iso = until.date().isoformat()
            await self.set_holiday(key, until_iso)
            await self.jobs._send_text(
                f"{participant['name']} is on holiday until {until_iso} — nudges paused."
            )
            return f"Enjoy it — nudges paused until {until_iso}. I'll tell Paul."

        if target_id is None:
            if item_ids:
                return (
                    "Which one do you mean? Reply with the number from my message "
                    '— like "1 done" or "2 blocked".'
                )
            return "Nothing's open on my list for you right now — all clear."

        done_words = _re.search(r"\b(done|finished|closed|completed|sorted|shipped)\b", lowered)
        blocked_words = _re.search(r"\b(blocked|waiting|stuck|can'?t)\b", lowered)
        help_words = _re.search(r"\b(help|hand|struggling|not sure how)\b", lowered)
        defer_words = _re.search(r"\b(not today|tomorrow|later|defer|push)\b", lowered)

        stamp = datetime.now(ZoneInfo(participant["timezone"])).strftime("%d %b %H:%M")
        if done_words and not blocked_words:
            layer_board = participant["board"]
            done_list = await layer.done_week_list(
                layer_board, datetime.now(ZoneInfo(participant["timezone"]))
            )
            await layer.client.update_card(target_id, idList=done_list)
            await layer.client.comment(
                target_id, f"Closed via Telegram by {participant['name']}, {stamp}"
            )
            return f"Done — {card_name} is closed. 👌"
        if help_words:
            await layer.client.comment(
                target_id, f"{participant['name']} asked for help via Telegram, {stamp}"
            )
            await self.jobs._send_text(
                f"🔺 {participant['name']} needs a hand with '{card_name}': “{text.strip()}”",
                essential=True,
            )
            return "Flagged to Paul right now — he'll come back to you."
        if blocked_words:
            try:
                await layer.move_card(participant["board"], target_id, "Blocked / Waiting On")
            except Exception:
                logger.exception("Blocked move failed — comment still lands")
            await layer.client.comment(
                target_id, f"Blocked ({participant['name']} via Telegram, {stamp}): {text.strip()}"
            )
            return f"Noted — {card_name} moved to Blocked and Paul will see why tonight."
        if defer_words:
            try:
                await layer.move_card(participant["board"], target_id, "This Week")
            except Exception:
                logger.exception("Defer move failed")
            await layer.client.comment(
                target_id, f"Deferred by {participant['name']} via Telegram, {stamp}"
            )
            return f"Fair enough — {card_name} moved to This Week."
        await layer.client.comment(
            target_id, f"{participant['name']} via Telegram, {stamp}: {text.strip()}"
        )
        return (
            f"Noted on {card_name}. Say \"done\", \"blocked — why\", or "
            '"not today" and I\'ll move it for you.'
        )

    # ------------------------------------------------------------ §10 digest

    async def digest(self, now: datetime | None = None) -> str:
        """Paul's 22:00 view: closed vs open, engaged vs silent, Kiefer
        included, stalled flagged. Composed from the last sweep per person."""
        now = now or datetime.now(ZoneInfo("Asia/Dubai"))
        sweeps = await self._get_json(SWEEP_KEY)
        nudges = await self._get_json(LAST_NUDGE_KEY)
        lines = [now.strftime("%A %-d %B").upper() if hasattr(now, "strftime") else ""]
        for key, participant in self.participants.items():
            sweep = sweeps.get(key)
            lines.append("")
            lines.append(participant["name"].split()[0].upper())
            if not sweep:
                lines.append("  no sweep yet — nothing to report")
                continue
            cards = sweep.get("cards", [])
            if not cards:
                lines.append("  Today list clear ✅")
                continue
            nudged_today = nudges.get(key, {}).get("date") == now.date().isoformat()
            lines.append(f"  Still open ({len(cards)})")
            for card in cards:
                flag = ""
                if card["days"] >= 5:
                    flag = " ⚠ BLOCKER — re-scope, reassign or move to Blocked?"
                elif card["days"] >= 3:
                    flag = " ⚠ stalled"
                engaged = "" if not participant.get("nudge") else (
                    "" if not nudged_today else " · nudged, no reply yet"
                )
                lines.append(f"    · {card['name']} — day {card['days']}{flag}{engaged}")
        stalled = sum(
            1 for s in sweeps.values() for c in s.get("cards", []) if c["days"] >= 3
        )
        if stalled:
            lines += ["", f"⚠ {stalled} item(s) aging past 3 working days"]
        return "\n".join(lines)

    async def send_digest(self, now: datetime | None = None) -> None:
        text = await self.digest(now)
        await self.jobs._send_text("CHASE DIGEST — " + text)
