"""The heart of Milestone 1: one inbound Telegram message → Jarvis's reply.

Flow: authorise → (voice? download + Deepgram) → log inbound → recall history →
Claude (Jarvis persona) → log outbound → reply by voice (ElevenLabs) or text
per the smart-mix policy. Errors degrade gracefully — Jarvis always answers.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient, SynthesisError
from app.clients.telegram_client import IncomingMessage, TelegramClient, parse_update
from app.config import Settings
from app.core.reply_policy import decide_reply, strip_for_speech
from app.core.store import MessageLog, SettingsStore
from app.db.base import Database
from app.daily12.commands import execute_actions, mentions_tasks, parse_actions, wants_plan
from app.daily12.service import Daily12Service
from app.documents.service import DocumentLibrary, looks_like_document_request
from app.heartbeat.streaks import STREAK_LABELS, Streaks, detect_activities
from app.memory.store import LivingFacts, MemoryStore
from app.memory.writer import extract_and_file, format_memory_context
from app.persona import build_system_prompt
from app.private.service import PrivateTrack

logger = logging.getLogger(__name__)

OWNER_KEY = "owner_chat_id"
TIMEZONE_KEY = "current_timezone"

STRANGER_REPLY = "This is a private assistant. If you're looking for Jarvis, he's taken."


class JarvisRouter:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        telegram: TelegramClient,
        claude: ClaudeClient,
        deepgram: DeepgramClient,
        elevenlabs: ElevenLabsClient,
        memory: MemoryStore | None = None,
        living: LivingFacts | None = None,
        library: DocumentLibrary | None = None,
        daily12: Daily12Service | None = None,
        heartbeat=None,          # HeartbeatJobs — hound mode + timezone reschedule
        on_timezone_change=None,  # async callback after Paul moves timezone
        private_track: PrivateTrack | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.telegram = telegram
        self.claude = claude
        self.deepgram = deepgram
        self.elevenlabs = elevenlabs
        self.memory = memory
        self.living = living
        self.library = library
        self.daily12 = daily12
        self.heartbeat = heartbeat
        self.on_timezone_change = on_timezone_change
        self.private_track = private_track
        self.log = MessageLog(db)
        self.store = SettingsStore(db)
        self.streaks = Streaks(db)

    # --- Authorisation: Jarvis talks to Paul and no one else ---

    async def _is_owner(self, chat_id: int) -> bool:
        if self.settings.telegram_owner_chat_id:
            return chat_id == self.settings.telegram_owner_chat_id
        stored = await self.store.get(OWNER_KEY)
        if not stored:
            # First human to message becomes the owner (Paul, on first contact).
            await self.store.set(OWNER_KEY, str(chat_id))
            logger.info("Locked onto owner chat id %s", chat_id)
            return True
        return stored == str(chat_id)

    # --- Entry point ---

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = parse_update(update)
        if message is None:
            return
        try:
            await self._handle_message(message)
        except Exception:
            logger.exception("Failed handling message %s", message.message_id)
            try:
                await self.telegram.send_text(
                    message.chat_id,
                    "Hit a snag processing that one — say it again and I'm on it.",
                )
            except Exception:
                logger.exception("Even the apology failed")

    async def _handle_message(self, message: IncomingMessage) -> None:
        if not await self._is_owner(message.chat_id):
            logger.warning("Ignoring stranger %s (chat %s)", message.from_name, message.chat_id)
            await self.telegram.send_text(message.chat_id, STRANGER_REPLY)
            return

        await self.telegram.send_chat_action(message.chat_id, "typing")

        # 0. File uploads → the document library (Milestone 2).
        if message.is_document:
            await self._handle_document_upload(message)
            return

        # 1. Get the words (transcribe voice notes).
        if message.is_voice:
            audio = await self.telegram.download_file(message.voice_file_id)
            transcript = await self.deepgram.transcribe(audio, "audio/ogg")
            if not transcript:
                await self.log.log(
                    "in", "", chat_id=message.chat_id, kind="voice",
                    voice_duration=message.voice_duration, meta={"empty_transcript": True},
                )
                await self.telegram.send_text(
                    message.chat_id, "Couldn't make that voice note out — give me it again?"
                )
                return
        else:
            transcript = message.text
            if not transcript:
                return  # stickers, photos etc. — Phase 2

        # 1b. THE PRIVATE ROOM (Milestone 6) — checked before anything is
        # written to the general log. Private exchanges live only in the
        # encrypted sobriety store; the business brain never sees them.
        if self.private_track is not None and (
            self.private_track.is_sos(transcript) or self.private_track.is_private_topic(transcript)
        ):
            await self._handle_private(message, transcript)
            return

        # 2. Log inbound.
        await self.log.log(
            "in",
            transcript,
            chat_id=message.chat_id,
            kind="voice" if message.is_voice else "text",
            voice_duration=message.voice_duration,
        )

        # 2a. Life signals: streaks, hound mode, timezone (Milestone 4).
        if await self._handle_life_signals(message, transcript):
            return

        # 2b. "Show me the BMI contract" — fetch from the document library.
        if self.library is not None and looks_like_document_request(transcript):
            if await self._handle_document_request(message.chat_id, transcript):
                return

        # 2c. Task talk → the Daily 12 + Trello write-back (Milestone 3).
        if self.daily12 is not None and mentions_tasks(transcript):
            if await self._handle_task_talk(message, transcript):
                return

        # 3. Think (with the second brain's recalled knowledge, Milestone 2).
        timezone = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
        memory_context = await self._recall(transcript)
        system = build_system_prompt(timezone=timezone, memory_context=memory_context)
        history = await self.log.as_claude_messages(self.settings.history_messages)
        raw_reply = await self.claude.converse(system, history)
        if not raw_reply:
            raw_reply = "I lost my train of thought there — go again."

        # 4. Decide voice vs text, log outbound, deliver.
        channel, reply_text = decide_reply(raw_reply, incoming_was_voice=message.is_voice)
        await self.log.log(
            "out", reply_text, chat_id=message.chat_id,
            kind="voice" if channel == "voice" else "text", meta={"channel_decision": channel},
        )

        if channel == "voice":
            await self.telegram.send_chat_action(message.chat_id, "record_voice")
            try:
                audio_mp3 = await self.elevenlabs.synthesize(strip_for_speech(reply_text))
                await self.telegram.send_voice(message.chat_id, audio_mp3, transcript=reply_text)
            except SynthesisError:
                logger.exception("TTS failed — falling back to text")
                await self.telegram.send_text(message.chat_id, reply_text)
        else:
            await self.telegram.send_text(message.chat_id, reply_text)

        # 5. Remember anything durable from what Paul said (never blocks the reply).
        if self.memory is not None and self.living is not None:
            await extract_and_file(
                self.claude, self.memory, self.living, transcript,
                source="voice" if message.is_voice else "text",
            )

    # --- Milestone 2 helpers ---

    async def _recall(self, query: str) -> str:
        """Pull the relevant slice of the second brain for this message.
        THE WALL: business retrieval never includes the private room."""
        if self.memory is None or self.living is None:
            return ""
        try:
            chunks = await self.memory.search(query, k=8, min_score=0.15)
            living_facts = await self.living.all_current(exclude_private=True)
            return format_memory_context(chunks, living_facts)
        except Exception:
            logger.exception("Memory recall failed — continuing without")
            return ""

    async def _handle_document_upload(self, message: IncomingMessage) -> None:
        if self.library is None:
            await self.telegram.send_text(
                message.chat_id, "Document library isn't switched on yet — soon."
            )
            return
        data = await self.telegram.download_file(message.document_file_id)
        summary = await self.library.ingest(
            data, message.document_name, mime=message.document_mime
        )
        await self.log.log(
            "in", f"[uploaded document: {message.document_name}]",
            chat_id=message.chat_id, meta=summary,
        )
        if summary["chars"] > 0:
            reply = (
                f"Filed. {message.document_name} is in the library — I've read it "
                f"and I'll recall it whenever it's relevant. Ask me for it any time."
            )
        else:
            reply = (
                f"Stored {message.document_name} safely, though I couldn't read text out of it — "
                f"I can still send it back whenever you ask."
            )
        await self.log.log("out", reply, chat_id=message.chat_id)
        await self.telegram.send_text(message.chat_id, reply)

    # --- Milestone 6: the private room ---

    async def _handle_private(self, message: IncomingMessage, transcript: str) -> None:
        """Private-room exchange: warm voice reply; only redacted markers touch
        the general log (content lives encrypted in the sobriety store)."""
        assert self.private_track is not None
        tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
        today = datetime.now(ZoneInfo(tz_name)).date()
        await self.log.log(
            "in", "[private exchange]", chat_id=message.chat_id,
            kind="voice" if message.is_voice else "text", meta={"private": True},
        )
        reply = await self.private_track.respond(transcript, today)
        await self.log.log("out", "[private exchange]", chat_id=message.chat_id, meta={"private": True})
        try:
            audio = await self.elevenlabs.synthesize(strip_for_speech(reply))
            await self.telegram.send_voice(message.chat_id, audio, transcript=reply)
        except SynthesisError:
            await self.telegram.send_text(message.chat_id, reply)

    # --- Milestone 4: life signals ---

    TIMEZONE_MAP = {
        "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
        "uk": "Europe/London", "england": "Europe/London",
        "london": "Europe/London", "nottingham": "Europe/London", "home": "Europe/London",
    }

    async def _handle_life_signals(self, message: IncomingMessage, transcript: str) -> bool:
        import re

        lowered = transcript.lower()

        # Timezone: "I'm in Dubai" / "landed in the UK"
        move = re.search(r"\b(?:i'?m in|landed in|back in|arrived in)\s+(?:the\s+)?(\w+)", lowered)
        if move and move.group(1) in self.TIMEZONE_MAP:
            timezone = self.TIMEZONE_MAP[move.group(1)]
            await self.store.set(TIMEZONE_KEY, timezone)
            if self.on_timezone_change is not None:
                await self.on_timezone_change()
            place = move.group(1).upper() if move.group(1) == "uk" else move.group(1).title()
            reply = f"Clocks switched to {place} time. Briefs, nudges and the 9pm review all follow you."
            await self.log.log("out", reply, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, reply)
            return True

        # The cockpit link: "show me the cockpit / dashboard"
        if re.search(r"\b(cockpit|dashboard)\b", lowered):
            if self.settings.public_url:
                url = (
                    f"{self.settings.public_url.rstrip('/')}/cockpit/"
                    f"{self.settings.effective_cockpit_secret}"
                )
                reply = f"Your cockpit: {url}\nBookmark it — live streaks, the 12, the villa, the lot."
            else:
                reply = "The cockpit goes live once I'm deployed with a public URL — soon."
            await self.log.log("out", reply, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, reply)
            return True

        # Manual hound mode: "hound me"
        if re.search(r"\bhound me\b|\bhound mode\b", lowered) and self.heartbeat is not None:
            await self.heartbeat.set_hound(True)
            reply = (
                "Hound mode on. I'll be on you through the day until the board's clear — "
                "first move: pick one task and give me two minutes on it right now."
            )
            await self.log.log("out", reply, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, reply)
            return True

        # Activity reports: "run done", "smashed the workout", "meals on plan"
        activities = detect_activities(transcript)
        if activities:
            tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
            today = datetime.now(ZoneInfo(tz_name)).date()
            parts = []
            for activity in activities:
                result = await self.streaks.record(activity, today)
                parts.append(f"{STREAK_LABELS[activity]} streak: {result['current']}")
                if activity == "run":
                    await self.db.execute(
                        "INSERT INTO runs (run_date, distance_km, duration_min, source)"
                        " VALUES (?, 5.0, 0, 'told')",
                        (today.isoformat(),),
                    )
            ack = "Logged. " + " · ".join(parts) + "."
            if "run" in activities:
                ack += " Keystone's in — the day's yours now."
            await self.log.log("out", ack, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, ack)
            # An activity report can carry more ("run done — what's my 12?");
            # only fall through when there's clearly a question/request left.
            return not ("?" in transcript or wants_plan(transcript))
        return False

    async def _handle_task_talk(self, message: IncomingMessage, transcript: str) -> bool:
        """Daily 12 queries and voice feedback → Trello (Milestone 3). Returns
        True when handled; False falls through to normal conversation."""
        assert self.daily12 is not None
        replied = False  # once anything is sent (or the board touched), never fall through
        try:
            plan_date = await self.daily12.paul_today()
            if wants_plan(transcript):
                await self.daily12.generate(plan_date)
                plan_text = await self.daily12.format_plan(plan_date)
                await self.log.log("out", plan_text, chat_id=message.chat_id)
                replied = True
                await self.telegram.send_text(message.chat_id, plan_text)  # lists go as text
                return True

            plan_text = await self.daily12.format_plan(plan_date)
            actions = await parse_actions(self.claude, transcript, plan_text)
            if not actions:
                return False  # ordinary conversation after all
            results, show = await execute_actions(self.daily12, actions)
            replied = True  # the board may have changed — this exchange is ours now
            reply = " ".join(results) if results else "Done."
            await self.log.log("out", reply, chat_id=message.chat_id, meta={"actions": actions})
            channel, reply_text = decide_reply(reply, incoming_was_voice=message.is_voice)
            if channel == "voice":
                try:
                    audio = await self.elevenlabs.synthesize(strip_for_speech(reply_text))
                    await self.telegram.send_voice(message.chat_id, audio, transcript=reply_text)
                except SynthesisError:
                    await self.telegram.send_text(message.chat_id, reply_text)
            else:
                await self.telegram.send_text(message.chat_id, reply_text)
            if show:
                shown = await self.daily12.format_plan(plan_date)
                await self.log.log("out", shown, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, shown)
            return True
        except Exception:
            logger.exception("Task handling failed%s", " after replying" if replied else "")
            return replied  # only fall through to conversation if nothing happened yet

    async def _handle_document_request(self, chat_id: int, transcript: str) -> bool:
        """Try to serve a 'show me the X' request from the library. Returns True
        when a document was sent (the request is then fully handled)."""
        assert self.library is not None
        try:
            matches = await self.library.find(transcript, k=1)
        except Exception:
            logger.exception("Document lookup failed")
            return False
        if not matches:
            return False  # let the brain answer normally
        doc = matches[0]
        data = await self.library.fetch_bytes(doc)
        if not data:
            return False
        caption = f"Here it is — {doc['filename']}"
        await self.telegram.send_document(
            chat_id, data, doc["filename"], doc["mime"] or "application/octet-stream", caption
        )
        await self.log.log("out", f"[sent document: {doc['filename']}]", chat_id=chat_id)
        return True
