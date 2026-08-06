"""Group intelligence (Build Slice Part 2, 7 Aug): the read-only group ingest
becomes actions, @-mention alerts and a missed-summary. Channel-agnostic on
purpose — today the only ingest source is Telegram's telegram_ingest table
(`app/core/router.py::_ingest_group_message`); the day WhatsApp groups exist
too, `_ingest_since` grows a UNION and nothing else in this file changes.

Two watermarks, deliberately separate:
- group_intel_last_action_id — the highest ingest row already scanned for
  actions/@-mentions. Actions never expire, so this only moves forward.
- group_intel_cleared_at — when Paul last dismissed. Drives BOTH the
  missed-summary's content and each group's "since last cleared" message
  count (§1 and §5 share one clock; dismissing resets it, nothing else does).

Dismissing touches neither watermark's sibling data: group_actions (open/
trello/ignored items) are untouched by a dismiss, on purpose (§5's easy bug
to ship — this file has a test that exists purely to catch it coming back).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAST_ACTION_ID_KEY = "group_intel_last_action_id"
CLEARED_AT_KEY = "group_intel_cleared_at"
MISSED_SUMMARY_KEY = "group_intel_missed_summary"
DISMISSED_LOG_KEY = "group_intel_dismissed_log"
TAG_VARIANTS_KEY = "group_intel_tag_variants"
DISMISSED_LOG_CAP = 20

# Paul's own @-tag forms — 'if in doubt, surface it' (§3), so this errs wide.
# Overridable via settings (JSON list) the same way telegram_group_map is.
DEFAULT_TAG_VARIANTS = ["@paul", "@paullunny", "@paul lunny", "@paul.lunny"]

GIST_SYSTEM = """\
You summarise a work-group chat for a busy owner who will NOT read the raw \
messages. From the messages given (one group, most recent last), write ONE \
short plain-English sentence: what this group is about RIGHT NOW. Not a \
list of messages, not a greeting, just the gist. No preamble, no quotes.\
"""

EXTRACTION_SYSTEM = """\
Read these work-group messages. Extract things that need PAUL PERSONALLY \
to do something: direct requests to him, questions aimed at him, decisions \
waiting on him, deadlines that involve him. Ignore chit-chat and internal \
back-and-forth that doesn't need him.

Reply with ONLY a JSON array (possibly empty), each item:
{"ask": "<what's being asked, third person, self-contained>",
 "asked_by": "<the sender's name exactly as given>",
 "source_message": "<the EXACT message text, character for character — never paraphrase>",
 "ts": "<its timestamp exactly as given>"}

At most 8 items. Never invent an ask that isn't really there.\
"""

MISSED_SUMMARY_SYSTEM = """\
Write Paul a catch-up of everything in his work groups since he last \
checked. Grouped by group name, in plain prose — what happened and what it \
means for him. Not a transcript, not bullets-of-messages. Skip a group \
entirely if nothing in it actually matters. If genuinely nothing matters \
anywhere, say so in one honest line.\
"""

JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _dedupe_key(channel: str, chat_id: int, source_message: str, ts: str) -> str:
    raw = f"{channel}|{chat_id}|{_norm(source_message)[:500]}|{ts}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class GroupIntel:
    def __init__(self, db, claude, store, layer_factory=None) -> None:
        self.db = db
        self.claude = claude
        self._store = store
        self._layer_factory = layer_factory   # async () -> TrelloLayer | None

    # ------------------------------------------------------------ ingest

    async def _ingest_since(self, since_id: int) -> list[dict]:
        """Every new group message since the watermark, any channel. Only
        Telegram exists today — add a UNION here (with the SAME shape:
        id/ts/chat_id/chat_title/company_tag/sender/message/kind/channel)
        the day WhatsApp groups are real; nothing downstream changes."""
        rows = await self.db.fetch_all(
            "SELECT id, ts, chat_id, chat_title, company_tag, sender, message, kind"
            " FROM telegram_ingest WHERE id > ? ORDER BY id", (since_id,),
        )
        for row in rows:
            row["channel"] = "telegram"
        return rows

    async def _ever_ingested(self) -> bool:
        row = await self.db.fetch_one("SELECT id FROM telegram_ingest LIMIT 1")
        return row is not None

    async def _tag_variants(self) -> list[str]:
        try:
            custom = json.loads(await self._store.get(TAG_VARIANTS_KEY, "[]"))
        except Exception:
            custom = []
        return [v.lower() for v in (custom or DEFAULT_TAG_VARIANTS)]

    def _is_tagged(self, message: str, variants: list[str]) -> bool:
        lowered = f" {message.lower()} "
        return any(re.search(rf"(?<![\w@]){re.escape(v)}\b", lowered) for v in variants)

    # ------------------------------------------------------------ refresh

    async def refresh(self) -> int:
        """Scan new ingest, update per-group gists, extract actions
        (@-mentions auto-promoted, never judged), regenerate the
        missed-summary. Returns how many new messages were scanned. Never
        raises — a half-done refresh still leaves the watermark where it
        was, so the next tick picks up cleanly."""
        try:
            last_id = int(await self._store.get(LAST_ACTION_ID_KEY, "0") or "0")
        except Exception:
            last_id = 0
        rows = await self._ingest_since(last_id)
        if not rows:
            return 0
        by_chat: dict[int, list[dict]] = {}
        for row in rows:
            by_chat.setdefault(row["chat_id"], []).append(row)
        variants = await self._tag_variants()
        for chat_id, group_rows in by_chat.items():
            try:
                await self._process_group(chat_id, group_rows, variants)
            except Exception:
                logger.exception("Group intel refresh failed for chat %s", chat_id)
        max_id = max(r["id"] for r in rows)
        await self._store.set(LAST_ACTION_ID_KEY, str(max_id))
        try:
            await self._regenerate_missed_summary()
        except Exception:
            logger.exception("Missed-summary regeneration failed — cached version stands")
        return len(rows)

    async def _process_group(self, chat_id: int, rows: list[dict], variants: list[str]) -> None:
        chat_title = rows[-1]["chat_title"]
        company_tag = rows[-1]["company_tag"]
        channel = rows[-1]["channel"]

        # §3 — @-mentions are NEVER judged, they're auto-promoted, straight away.
        tagged_texts: set[str] = set()
        for row in rows:
            if not self._is_tagged(row["message"], variants):
                continue
            tagged_texts.add(_norm(row["message"]))
            await self._insert_action(
                channel=channel, chat_id=chat_id, chat_title=chat_title,
                company_tag=company_tag, asked_by=row["sender"], ask=row["message"][:300],
                source_message=row["message"], ts=row["ts"], tagged=True,
            )

        # §2 — the general extractor, for asks that were never tagged.
        transcript = "\n".join(f"{r['sender']}: {r['message']}" for r in rows)
        try:
            raw = await self.claude.quick(transcript, system=EXTRACTION_SYSTEM, max_tokens=1000)
            match = JSON_ARRAY.search(raw)
            items = json.loads(match.group(0)) if match else []
        except Exception:
            logger.exception("Action extraction failed for chat %s", chat_id)
            items = []
        for item in items[:8]:
            source = str(item.get("source_message", "")).strip()
            ask = str(item.get("ask", "")).strip()
            if not ask or not source:
                continue
            if _norm(source) in tagged_texts:
                continue   # §3 dedupe rule — the tagged copy already covers it
            await self._insert_action(
                channel=channel, chat_id=chat_id, chat_title=chat_title,
                company_tag=company_tag, asked_by=str(item.get("asked_by", "")).strip(),
                ask=ask, source_message=source, ts=str(item.get("ts", "")).strip(), tagged=False,
            )

        # §1 — the rolling one-liner, refreshed on ingest.
        try:
            gist = (await self.claude.quick(transcript, system=GIST_SYSTEM, max_tokens=120)).strip()
        except Exception:
            logger.exception("Gist refresh failed for chat %s", chat_id)
            gist = ""
        if gist:
            await self._upsert_summary(channel, chat_id, chat_title, company_tag, gist)

    async def _insert_action(
        self, *, channel, chat_id, chat_title, company_tag, asked_by, ask,
        source_message, ts, tagged,
    ) -> None:
        key = _dedupe_key(channel, chat_id, source_message, ts)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await self.db.execute(
            "INSERT INTO group_actions (created_at, channel, chat_id, chat_title, company_tag,"
            " asked_by, ask, source_message, msg_ts, tagged, status, dedupe_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)"
            " ON CONFLICT (dedupe_key) DO NOTHING",
            (now, channel, chat_id, chat_title[:200], company_tag, asked_by[:120],
             ask[:500], source_message[:2000], ts, 1 if tagged else 0, key),
        )

    async def _upsert_summary(self, channel, chat_id, chat_title, company_tag, gist) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cleared_at = await self._store.get(CLEARED_AT_KEY, "")
        count_row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM telegram_ingest WHERE chat_id = ?"
            + (" AND ts > ?" if cleared_at else ""),
            (chat_id, cleared_at) if cleared_at else (chat_id,),
        )
        count = count_row["n"] if count_row else 0
        existing = await self.db.fetch_one(
            "SELECT id FROM group_summaries WHERE channel = ? AND chat_id = ?", (channel, chat_id),
        )
        if existing:
            await self.db.execute(
                "UPDATE group_summaries SET chat_title = ?, company_tag = ?, gist = ?,"
                " message_count = ?, updated_at = ? WHERE id = ?",
                (chat_title[:200], company_tag, gist[:300], count, now, existing["id"]),
            )
        else:
            await self.db.execute(
                "INSERT INTO group_summaries (channel, chat_id, chat_title, company_tag, gist,"
                " message_count, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (channel, chat_id, chat_title[:200], company_tag, gist[:300], count, now),
            )

    # ------------------------------------------------------------ §4 missed-summary

    async def _regenerate_missed_summary(self) -> None:
        cleared_at = await self._store.get(CLEARED_AT_KEY, "")
        sql = "SELECT chat_title, sender, message FROM telegram_ingest"
        params: tuple = ()
        if cleared_at:
            sql += " WHERE ts > ?"
            params = (cleared_at,)
        sql += " ORDER BY chat_id, ts"
        rows = await self.db.fetch_all(sql, params)
        if not rows:
            await self._store.set(MISSED_SUMMARY_KEY, "")
            return
        by_group: dict[str, list[str]] = {}
        for row in rows:
            by_group.setdefault(row["chat_title"] or "Unnamed group", []).append(
                f"{row['sender']}: {row['message']}"
            )
        transcript = "\n\n".join(
            f"[{title}]\n" + "\n".join(lines[-40:]) for title, lines in by_group.items()
        )[:60000]
        try:
            summary = (
                await self.claude.quick(transcript, system=MISSED_SUMMARY_SYSTEM, max_tokens=1200)
            ).strip()
        except Exception:
            logger.exception("Missed-summary Claude call failed")
            return   # keep whatever was cached rather than blank it on a transient failure
        await self._store.set(MISSED_SUMMARY_KEY, summary)

    # ------------------------------------------------------------ §6 read endpoints

    async def status(self) -> str:
        return "connected" if await self._ever_ingested() else "not_connected"

    async def group_summaries(self) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT channel, chat_id, chat_title, company_tag, gist, message_count, updated_at"
            " FROM group_summaries WHERE message_count > 0 OR gist != '' ORDER BY updated_at DESC"
        )
        return rows

    async def open_actions(self) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT id, channel, chat_id, chat_title, company_tag, asked_by, ask,"
            " source_message, msg_ts, tagged, created_at FROM group_actions"
            " WHERE status = 'open' ORDER BY tagged DESC, created_at DESC"
        )
        for row in rows:
            row["tagged"] = bool(row["tagged"])
        return rows

    async def missed_summary(self) -> dict:
        cleared_at = await self._store.get(CLEARED_AT_KEY, "")
        text = await self._store.get(MISSED_SUMMARY_KEY, "")
        return {"since": cleared_at or None, "text": text}

    async def dismissed_history(self) -> list[dict]:
        try:
            return json.loads(await self._store.get(DISMISSED_LOG_KEY, "[]"))
        except Exception:
            return []

    async def uncleared_count(self) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM group_actions WHERE status = 'open'"
        )
        return row["n"] if row else 0

    # ------------------------------------------------------------ §5 dismiss

    async def dismiss_summary(self) -> None:
        """Clears the missed-summary and each group's 'since cleared' count
        ONLY — group_actions (open/trello/ignored) are never touched here.
        The dismissed text stays retrievable, never destroyed."""
        text = await self._store.get(MISSED_SUMMARY_KEY, "")
        cleared_at = await self._store.get(CLEARED_AT_KEY, "")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if text:
            try:
                log = json.loads(await self._store.get(DISMISSED_LOG_KEY, "[]"))
            except Exception:
                log = []
            log.append({"from": cleared_at or None, "to": now, "text": text})
            await self._store.set(DISMISSED_LOG_KEY, json.dumps(log[-DISMISSED_LOG_CAP:]))
        await self._store.set(CLEARED_AT_KEY, now)
        await self._store.set(MISSED_SUMMARY_KEY, "")
        await self.db.execute("UPDATE group_summaries SET message_count = 0")

    # ------------------------------------------------------------ §2 action → Trello / ignore

    async def action_to_trello(self, action_id: int) -> str:
        row = await self.db.fetch_one("SELECT * FROM group_actions WHERE id = ?", (action_id,))
        if row is None:
            return "That action doesn't exist any more."
        if row["status"] != "open":
            return f"That one's already {row['status']} — nothing to do."
        if self._layer_factory is None:
            return "Trello isn't wired up — can't file this one yet."
        try:
            layer = await self._layer_factory()
        except Exception:
            logger.exception("Trello layer unavailable — action not filed")
            return "Couldn't reach Trello just now — try again shortly."
        if layer is None:
            return "Trello isn't wired up — can't file this one yet."
        card = await layer.create_card_full(
            board_key="master",
            list_name="Brain Dump",
            title=row["ask"][:120],
            desc=(
                f"From {row['chat_title']} ({row['channel']}), asked by "
                f"{row['asked_by'] or 'someone'} on {row['msg_ts']}\n\n"
                f"Message: \"{row['source_message']}\"\n\n"
                "Filed by Jarvis from group intelligence."
            ),
        )
        await self.db.execute(
            "UPDATE group_actions SET status = 'trello', trello_card_id = ? WHERE id = ?",
            (str(card.get("id", "")), action_id),
        )
        return f"Filed to Brain Dump: {row['ask'][:80]}"

    async def action_ignore(self, action_id: int) -> str:
        row = await self.db.fetch_one("SELECT id, status FROM group_actions WHERE id = ?", (action_id,))
        if row is None:
            return "That action doesn't exist any more."
        if row["status"] != "open":
            return f"That one's already {row['status']} — nothing to do."
        await self.db.execute(
            "UPDATE group_actions SET status = 'ignored' WHERE id = ?", (action_id,)
        )
        return "Ignored."

    # ------------------------------------------------------------ fixtures (§7)

    @staticmethod
    def fixtures() -> dict:
        """Sample payloads (a group summary, a normal action, a tagged item,
        a missed-summary) so every surface can be built and tested end-to-end
        before real group traffic exists."""
        return {
            "group_summaries": [{
                "channel": "telegram", "chat_id": -1001, "chat_title": "Derma Direct UK",
                "company_tag": "derma_uk", "gist": "Chasing the BMI order and a website QA punch list.",
                "message_count": 7, "updated_at": "2026-08-07T09:00:00+00:00",
            }],
            "open_actions": [
                {"id": 1, "channel": "telegram", "chat_id": -1001, "chat_title": "Derma Direct UK",
                 "company_tag": "derma_uk", "asked_by": "Harry", "ask": "Confirm the BMI order quantity",
                 "source_message": "Paul can you confirm we're doing 500 units on the BMI order?",
                 "msg_ts": "2026-08-07T08:45:00+00:00", "tagged": False, "created_at": "2026-08-07T08:50:00+00:00"},
                {"id": 2, "channel": "telegram", "chat_id": -1001, "chat_title": "Derma Direct UK",
                 "company_tag": "derma_uk", "asked_by": "Adriana", "ask": "@Paul — sign off the new packaging artwork",
                 "source_message": "@Paul can you sign off the new packaging artwork today?",
                 "msg_ts": "2026-08-07T08:55:00+00:00", "tagged": True, "created_at": "2026-08-07T08:56:00+00:00"},
            ],
            "missed_summary": {
                "since": "2026-08-06T21:00:00+00:00",
                "text": (
                    "Derma Direct UK: Harry's chasing sign-off on the BMI order quantity, and "
                    "Adriana needs the new packaging artwork approved. Nothing else needs you."
                ),
            },
        }
