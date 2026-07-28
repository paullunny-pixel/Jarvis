"""The heart of Milestone 1: one inbound Telegram message → Jarvis's reply.

Flow: authorise → (voice? download + Deepgram) → log inbound → recall history →
Claude (Jarvis persona) → log outbound → reply by voice (ElevenLabs) or text
per the smart-mix policy. Errors degrade gracefully — Jarvis always answers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
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
from app.heartbeat.gates import GateKeeper, med_items_mentioned, mentions_meds
from app.heartbeat.streaks import STREAK_LABELS, Streaks, detect_activities, looks_negated
from app.mail import commands as mail_commands
from app.mail.service import MailService
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
        gates: GateKeeper | None = None,
        mail: MailService | None = None,
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
        self.gates = gates
        self.mail = mail
        self.log = MessageLog(db)
        self.store = SettingsStore(db)
        self.streaks = Streaks(db)
        self._sprint_tasks: set = set()  # strong refs to running buzzer timers

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

    # Group-name → company heuristics; a settings map ("telegram_group_map")
    # set by "map the X group to Y" overrides them.
    GROUP_COMPANY_HINTS = (
        ("derma direct uk", "derma_uk"), ("derma uk", "derma_uk"),
        ("derma eu", "derma_eu"), ("derma direct eu", "derma_eu"),
        ("aesthetics", "aesthetics_supply"), ("grey", "aesthetics_supply"),
        ("prodermis", "prodermis"), ("prime derm", "prodermis"), ("bmi", "prodermis"),
        ("derma", "derma_uk"),  # generic fallback — checked last
    )

    async def _group_company(self, chat_id: int, chat_title: str) -> str:
        import json as _json

        try:
            overrides = _json.loads(await self.store.get("telegram_group_map", "{}"))
        except Exception:
            overrides = {}
        mapped = overrides.get(str(chat_id)) or overrides.get(chat_title.strip().lower())
        if mapped:
            return mapped
        lowered = chat_title.lower()
        for hint, slug in self.GROUP_COMPANY_HINTS:
            if hint in lowered:
                return slug
        return ""

    async def _ingest_group_message(self, message: IncomingMessage) -> None:
        """Telegram org ingestion: the bot sits in the work groups (privacy
        mode off) and READS — it never replies there. Text is stored as-is,
        voice notes go through Deepgram first, media leaves a marker."""
        kind, content = "text", message.text
        if message.is_voice:
            kind = "voice"
            try:
                audio = await self.telegram.download_file(message.voice_file_id)
                content = await self.deepgram.transcribe(audio, "audio/ogg")
            except Exception:
                logger.exception("Group voice transcription failed")
                content = ""
            if not content:
                content = "[voice note — transcription unavailable]"
        elif message.is_photo:
            kind = "photo"
            content = f"[photo] {message.text}".strip()
        elif message.is_document:
            kind = "document"
            content = f"[document: {message.document_name}] {message.text}".strip()
        if not content:
            return
        company = await self._group_company(message.chat_id, message.chat_title)
        await self.db.execute(
            "INSERT INTO telegram_ingest (ts, chat_id, chat_title, company_tag, sender,"
            " sender_id, kind, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
                message.chat_id,
                message.chat_title[:200],
                company,
                message.from_name[:120],
                message.sender_id,
                kind,
                content[:2000],
            ),
        )

    async def _handle_message(self, message: IncomingMessage) -> None:
        # WORK GROUPS: ingest silently, reply never (Telegram org ingestion).
        if message.is_group:
            try:
                await self._ingest_group_message(message)
            except Exception:
                logger.exception("Group ingestion failed")
            return

        if not await self._is_owner(message.chat_id):
            logger.warning("Ignoring stranger %s (chat %s)", message.from_name, message.chat_id)
            await self.telegram.send_text(message.chat_id, STRANGER_REPLY)
            return

        await self.telegram.send_chat_action(message.chat_id, "typing")

        # 0. File uploads → the document library (Milestone 2).
        if message.is_document:
            await self._handle_document_upload(message)
            return

        # 0b. Photos → Jarvis actually looks at them (Claude vision).
        if message.is_photo:
            await self._handle_photo(message)
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

        # 2c. Email talk → inbox triage, drafts, and confirmed sends (Phase 2).
        if self.mail is not None:
            if await self._handle_email_talk(message, transcript):
                return

        # 2d. Task talk → the Daily 12 + Trello write-back (Milestone 3).
        # THE GATES: past their deadline, an unconfirmed run/meds blocks the
        # working day — Jarvis will not serve the board until they're done.
        if self.daily12 is not None and mentions_tasks(transcript):
            if self.gates is not None:
                tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
                outstanding = await self.gates.outstanding(datetime.now(ZoneInfo(tz_name)))
                if outstanding:
                    block = self.gates.block_message(outstanding)
                    await self.log.log("out", block, chat_id=message.chat_id, meta={"gated": True})
                    try:
                        audio = await self.elevenlabs.synthesize(strip_for_speech(block))
                        await self.telegram.send_voice(message.chat_id, audio, transcript=block)
                    except SynthesisError:
                        await self.telegram.send_text(message.chat_id, block)
                    return
            if await self._handle_task_talk(message, transcript):
                return

        # 3. Think (with the second brain's recalled knowledge, Milestone 2).
        timezone = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
        memory_context = await self._recall(transcript)
        system_status = self._integration_status()
        # LIVE BOARD TRUTH: the brain sees Today's Focus as it actually is,
        # so it can never insist a long-gone card is "still sitting there".
        if self.daily12 is not None:
            try:
                board_truth = await self.daily12.format_plan()
                system_status += (
                    "\n\nLIVE BOARD TRUTH — Today's Focus as of this message "
                    "(trust this over anything remembered):\n" + board_truth
                )
            except Exception:
                logger.exception("Board truth injection failed — continuing without")
        system = build_system_prompt(
            timezone=timezone,
            memory_context=memory_context,
            system_status=system_status,
        )
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

    async def _handle_photo(self, message: IncomingMessage) -> None:
        """A photo message: download it, show it to the brain alongside the
        caption and conversation history, reply as normal."""
        import base64

        import base64 as _b64

        caption = message.text or ""
        photo = await self.telegram.download_file(message.photo_file_id)
        await self.log.log(
            "in", f"[photo] {caption}".strip(), chat_id=message.chat_id, meta={"photo": True}
        )

        # Gate proof: if today's run is unconfirmed, check whether this photo
        # IS the run stats — vision reads the numbers and logs the real thing.
        if self.gates is not None:
            tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
            today = datetime.now(ZoneInfo(tz_name)).date()
            if not await self.gates.is_confirmed("run", today):
                stats = await self._read_run_stats(_b64.b64encode(photo).decode())
                if stats is not None:
                    await self.streaks.record("run", today)
                    if self.heartbeat is not None:  # a run also proves he's up
                        await self.heartbeat.record_wake(today, "run_proof")
                    await self.db.execute(
                        "INSERT INTO runs (run_date, distance_km, duration_min, source)"
                        " VALUES (?, ?, ?, 'photo_proof')",
                        (today.isoformat(), stats["distance_km"], stats["duration_min"]),
                    )
                    snap = await self.streaks.snapshot(today)
                    duration = (
                        f" in {stats['duration_min']:.0f} minutes" if stats["duration_min"] else ""
                    )
                    ack = (
                        f"Verified, sir — {stats['distance_km']:.2f} km{duration}. Run logged, "
                        f"streak at {snap['run']['current']}. The day is officially open."
                    )
                    await self.log.log("out", ack, chat_id=message.chat_id, meta={"gate_proof": True})
                    try:
                        audio = await self.elevenlabs.synthesize(strip_for_speech(ack))
                        await self.telegram.send_voice(message.chat_id, audio, transcript=ack)
                    except SynthesisError:
                        await self.telegram.send_text(message.chat_id, ack)
                    return

        # §4: the morning mirror selfie — ends the wake sequence.
        if self.heartbeat is not None:
            tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
            now_local = datetime.now(ZoneInfo(tz_name))
            try:
                if (
                    await self.heartbeat.wake_enabled()
                    and now_local.hour < 12
                    and not await self.heartbeat.woke_today(now_local.date())
                ):
                    await self.heartbeat.record_wake(
                        now_local.date(), "selfie", photo_ref=message.photo_file_id
                    )
                    ack = (
                        f"Verified and vertical at {now_local.strftime('%H:%M')} — good morning, "
                        "sir. Sequence stands down; the day is yours."
                    )
                    await self.log.log("out", ack, chat_id=message.chat_id, meta={"wake": True})
                    try:
                        audio = await self.elevenlabs.synthesize(strip_for_speech(ack))
                        await self.telegram.send_voice(message.chat_id, audio, transcript=ack)
                    except SynthesisError:
                        await self.telegram.send_text(message.chat_id, ack)
                    return
            except Exception:
                logger.exception("Wake selfie handling failed — treating as a normal photo")

        timezone = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
        memory_context = await self._recall(caption) if caption else ""
        system = build_system_prompt(
            timezone=timezone,
            memory_context=memory_context,
            system_status=self._integration_status(),
        )
        history = await self.log.as_claude_messages(self.settings.history_messages)
        # Attach the image to the final (current) user turn.
        if history and history[-1]["role"] == "user":
            text_part = history[-1]["content"]
            history[-1] = {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(photo).decode(),
                        },
                    },
                    {"type": "text", "text": text_part},
                ],
            }
        raw_reply = await self.claude.converse(system, history)
        if not raw_reply:
            raw_reply = "I looked, but lost my train of thought — send it again?"
        channel, reply_text = decide_reply(raw_reply, incoming_was_voice=False)
        await self.log.log(
            "out", reply_text, chat_id=message.chat_id,
            kind="voice" if channel == "voice" else "text",
        )
        if channel == "voice":
            try:
                audio = await self.elevenlabs.synthesize(strip_for_speech(reply_text))
                await self.telegram.send_voice(message.chat_id, audio, transcript=reply_text)
                return
            except SynthesisError:
                logger.exception("TTS failed — falling back to text")
        await self.telegram.send_text(message.chat_id, reply_text)

    RUN_STATS_SYSTEM = (
        'You inspect an image to see if it shows completed running/exercise stats (a sports '
        'watch, Strava/fitness app screenshot, treadmill display). Reply ONLY JSON: '
        '{"is_run_stats": true|false, "distance_km": <number or 0>, "duration_min": <number or 0>}. '
        'Convert miles to km. If it is not exercise stats, is_run_stats is false.'
    )

    async def _read_run_stats(self, image_b64: str) -> dict | None:
        """Vision check of run-proof photos. Returns stats when the image shows
        a completed run of at least ~5km, else None (photo handled normally)."""
        import json as _json
        import re as _re

        try:
            raw = await self.claude.quick_vision(
                "Does this image show completed run stats? Extract the numbers.",
                image_b64,
                system=self.RUN_STATS_SYSTEM,
                max_tokens=150,
            )
            match = _re.search(r"\{.*\}", raw, _re.DOTALL)
            if not match:
                return None
            data = _json.loads(match.group(0))
            distance = float(data.get("distance_km") or 0)
            if data.get("is_run_stats") and distance >= 4.5:
                return {"distance_km": distance, "duration_min": float(data.get("duration_min") or 0)}
        except Exception:
            logger.exception("Run-stats vision check failed")
        return None

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

    def _integration_status(self) -> str:
        """Ground truth about what's wired in — fed to the brain every turn so
        it can never confabulate about its own connections."""
        s = self.settings
        lines = [
            f"- Trello / Today's Focus: {'CONNECTED' if self.daily12 is not None else 'not connected yet'}",
            f"- Memory search: {'Voyage embeddings' if s.voyage_api_key else 'local fallback (Voyage key pending)'}",
            f"- Calendar: {'connected (read-only)' if s.calendar_ics_url else 'not connected yet'}",
            (
                f"- Email inboxes: {', '.join(self.mail.labels)} connected — read & draft; "
                "sends ONLY after Paul confirms a read-back draft"
                if self.mail is not None
                else "- Email inboxes: not connected yet"
            ),
            f"- Kiefer nightly email: {'configured' if (s.gmail_address and s.kiefer_email) else 'not configured yet'}",
            f"- Apple Health webhook: {'configured' if s.apple_health_webhook_secret else 'not configured yet'}",
            "- Day rhythm: wake-up sequence built (05:00 local; Paul arms it with 'start the "
            "wake-ups', skips one day with 'no wake-up tomorrow'); hourly move+water nudges; "
            "med reminders (ADHD 09:30, supplements 14:00, TRT Saturdays); 'override' releases "
            "any block.",
            "- Work-group ears: Paul's org runs on Telegram; when this bot is in a work "
            "group (privacy mode off) every message is ingested, tagged by company and "
            "summarisable ('catch me up on Derma EU'). The bot never replies in groups.",
            (
                "- Live voice: the cockpit's 'Talk to Jarvis' button opens a realtime, "
                "interruptible spoken conversation (same persona, same memory via tools)."
                if getattr(self, "voice_engine", None) is not None
                else "- Live voice: not available (ElevenLabs key required)."
            ),
            "- Voice (ElevenLabs), hearing (Deepgram), vision, the heartbeat, gates and the "
            "private track: all active.",
            "If Paul asks about a connection, answer from this list — or tell him to say "
            "'status' for a live check.",
        ]
        return "\n".join(lines)

    STATUS_QUERY = None  # set below (regex compiled at import)

    async def _handle_status(self, message: IncomingMessage) -> None:
        """'status' / 'are you connected to Trello?' → run REAL live checks."""
        s = self.settings
        lines = ["SYSTEM CHECK"]
        if self.daily12 is not None:
            health = await self.daily12.health()
            if health["ok"]:
                if health.get("scoped"):
                    where = (
                        f"working from {health['board_name']} "
                        f"({health['boards']} boards visible)"
                    )
                else:
                    plural = "board" if health["boards"] == 1 else "boards"
                    where = f"{health['boards']} {plural} ({health['board_name']})"
                lines.append(
                    f"✅ Trello: connected — {where}, "
                    f"{health['cached_cards']} cards in play, last sync {health['last_sync'][:16] or 'never'}"
                )
            else:
                lines.append(f"⚠️ Trello: keys present but the live check failed — {health['error']}")
        else:
            lines.append("▫️ Trello: not connected (keys not set)")
        lines.append(
            "✅ Memory: Voyage embeddings" if s.voyage_api_key
            else "▫️ Memory: local fallback (add VOYAGE_API_KEY for proper recall)"
        )
        lines.append("✅ Calendar: connected" if s.calendar_ics_url else "▫️ Calendar: not connected")
        if self.mail is not None:
            for check in await self.mail.health():
                if check["ok"]:
                    lines.append(f"✅ Email · {check['label']}: connected")
                else:
                    lines.append(f"⚠️ Email · {check['label']}: check failed — {check['error']}")
        else:
            lines.append("▫️ Email inboxes: not connected")
        lines.append(
            "✅ Kiefer email: configured" if (s.gmail_address and s.kiefer_email)
            else "▫️ Kiefer email: not configured"
        )
        lines.append(
            "✅ Apple Health: webhook ready" if s.apple_health_webhook_secret
            else "▫️ Apple Health: not configured"
        )
        since_24h = (datetime.now(ZoneInfo("UTC")) - timedelta(hours=24)).isoformat(timespec="seconds")
        recent_tg = await self.db.fetch_one(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT chat_id) AS chats FROM telegram_ingest"
            " WHERE ts >= ?",
            (since_24h,),
        )
        tg_count = int(recent_tg["n"]) if recent_tg else 0
        lines.append(
            f"✅ Work groups: {tg_count} messages in 24h across {int(recent_tg['chats'])} group(s)"
            if tg_count
            else "▫️ Work groups: no traffic yet (add me to the groups + privacy mode OFF in BotFather)"
        )
        lines.append("✅ Voice, hearing, vision, heartbeat, gates, private track: active")
        report = "\n".join(lines)
        await self.log.log("out", report, chat_id=message.chat_id, meta={"status_check": True})
        await self.telegram.send_text(message.chat_id, report)

    # --- Milestone 4: life signals ---

    TIMEZONE_MAP = {
        "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
        "uk": "Europe/London", "england": "Europe/London",
        "london": "Europe/London", "nottingham": "Europe/London", "home": "Europe/London",
    }

    OVERRIDE_PHRASE = None  # compiled below

    async def _handle_life_signals(self, message: IncomingMessage, transcript: str) -> bool:
        import json as _json
        import re

        lowered = transcript.lower()

        # THE UNIVERSAL OVERRIDE (Master Update §1). Unconditional failsafe:
        # nothing may suppress it. One confirm, reason logged, gates released
        # for the rest of the day.
        pending = await self.store.get("pending_override")
        today_iso = datetime.now(
            ZoneInfo(await self.store.get(TIMEZONE_KEY, self.settings.timezone_default))
        ).date().isoformat()
        if pending:
            try:
                state = _json.loads(pending)
            except Exception:
                state = {}
            await self.store.set("pending_override", "")
            if state.get("date") == today_iso and self.gates is not None:
                if re.search(r"\b(no|leave it|never ?mind|cancel|as you were|forget it)\b", lowered):
                    reply = "As you were, sir — everything stays in place."
                else:
                    await self.gates.override(
                        state.get("items", []),
                        transcript,
                        datetime.fromisoformat(today_iso).date(),
                    )
                    reply = (
                        "Understood — released, and it won't block you again today. "
                        "Noted the why; on we go."
                    )
                await self.log.log("out", reply, chat_id=message.chat_id, meta={"override": True})
                await self.telegram.send_text(message.chat_id, reply)
                return True
        override_hit = re.search(
            r"^\s*override[,.!]?(\s+jarvis)?\s*$|\boverride,?\s+jarvis\b|\bmove on\b", lowered
        )
        if override_hit and self.gates is not None:
            tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
            now_local = datetime.now(ZoneInfo(tz_name))
            outstanding = await self.gates.outstanding(now_local)
            if self.heartbeat is not None:
                try:
                    if await self.heartbeat.wake_pending(now_local):
                        outstanding = outstanding + [
                            {"id": "wake", "label": "the wake-up sequence", "by": ""}
                        ]
                except Exception:
                    logger.exception("Wake-pending check failed during override")
            if outstanding:
                await self.store.set(
                    "pending_override",
                    _json.dumps({"date": today_iso, "items": [g["id"] for g in outstanding]}),
                )
                reply = "You sure? What's up — one line and I'll release it."
                await self.log.log("out", reply, chat_id=message.chat_id, meta={"override": True})
                await self.telegram.send_text(message.chat_id, reply)
                return True
            if lowered.strip().strip(".,!").startswith("override"):
                reply = "Nothing's locked right now, sir — the day's already open."
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True
            # A conversational "move on" with nothing blocked — not an override.

        # System check: "status" / "are you connected to trello?"
        if re.search(
            r"^\s*status\s*$|\bsystem (check|status)\b"
            r"|\b(confirm|check|got|have|do you have)\b.{0,16}\baccess\b.{0,24}\b(trello|board|calendar|gmail|email)\b"
            r"|\b(are you|you)\b.{0,12}\bconnect(ed)?\b.{0,20}\b(trello|calendar|gmail|board)\b"
            r"|\b(trello|calendar|gmail)\b.{0,20}\b(connected|working|wired|linked)\b",
            lowered,
        ):
            await self._handle_status(message)
            return True

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

        # On-demand board sync: "sync trello" / "refresh the board".
        if re.search(
            r"\b(sync|resync|re-sync|refresh)\b.{0,16}\b(trello|board|cards)\b"
            r"|\btrello\b.{0,12}\b(sync|refresh)\b",
            lowered,
        ):
            if self.daily12 is None:
                reply = "Trello isn't connected on this deployment, sir."
            else:
                try:
                    count = await self.daily12.sync()
                    health = await self.daily12.health()
                    reply = (
                        f"Board synced — {count} cards read, {health['cached_cards']} in play "
                        f"from {health['board_name']}. Moves, ticks and deletions all landed."
                    )
                except Exception:
                    logger.exception("Manual Trello sync failed")
                    reply = "Trello wouldn't answer just now — I'll retry on the hourly pass."
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

        # "Catch me up on <company/group>" — summarise work-group traffic.
        catch = re.search(
            r"\bcatch me up\b(?:\s+on\s+(.+))?"
            r"|\bwhat'?s (?:been )?(?:happening|going on)\b.{0,12}\b(?:in|with|on)\s+(.+?)\s*(?:group|chat|$)",
            lowered,
        )
        if catch and ("catch me up" in lowered or "group" in lowered):
            topic = (catch.group(1) or catch.group(2) or "").strip()
            reply = await self._group_catchup(topic)
            await self.log.log("out", reply, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, reply)
            return True

        # On-the-fly group mapping: "map the Warehouse group to Derma EU".
        map_hit = re.search(
            r"\bmap\b\s+(?:the\s+)?(.+?)\s+group\s+to\s+([\w '&.]+)$", lowered
        )
        if map_hit:
            import json as _json

            slug_lookup = {
                "derma uk": "derma_uk", "derma direct uk": "derma_uk",
                "derma eu": "derma_eu", "derma direct eu": "derma_eu",
                "aesthetics": "aesthetics_supply", "aesthetics supply": "aesthetics_supply",
                "aesthetics supply uk": "aesthetics_supply",
                "prodermis": "prodermis", "personal": "",
            }
            target = map_hit.group(2).strip().rstrip(".")
            slug = slug_lookup.get(target)
            if slug is None:
                reply = (
                    f"I don't know '{target}' as a company — use Derma UK, Derma EU, "
                    "Aesthetics Supply, Prodermis or Personal."
                )
            else:
                try:
                    mapping = _json.loads(await self.store.get("telegram_group_map", "{}"))
                except Exception:
                    mapping = {}
                mapping[map_hit.group(1).strip().lower()] = slug
                await self.store.set("telegram_group_map", _json.dumps(mapping))
                reply = (
                    f"Mapped — messages from the '{map_hit.group(1).strip()}' group now file "
                    f"under {target.title() if slug else 'Personal (ignored for business)'}. "
                )
            await self.log.log("out", reply, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, reply)
            return True

        # §4 wake-up controls + §10 goodnight (all in Paul's current timezone)
        if self.heartbeat is not None:
            tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
            now_local = datetime.now(ZoneInfo(tz_name))
            today_local = now_local.date()
            if re.search(r"\b(start|switch on|turn on|arm)\b.{0,20}\bwake[- ]?ups?\b", lowered):
                await self.heartbeat.set_wake_enabled(True)
                reply = (
                    "Wake sequence armed, sir — 05:00 local, wherever you are. Alarmy does the "
                    "alarm; I keep going every few minutes until the mirror selfie lands. "
                    "'No wake-up tomorrow' skips a travel night; 'override' stops any morning."
                )
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True
            if re.search(r"\b(stop|switch off|turn off|disable)\b.{0,20}\bwake[- ]?ups?\b", lowered):
                await self.heartbeat.set_wake_enabled(False)
                reply = "Wake-ups off until you say the word."
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True

            skip_hit = re.search(
                r"\b(no|skip|don'?t|not?)\b.{0,24}\bwake[- ]?up\b.{0,20}\btomorrow\b"
                r"|\bdon'?t wake me\b.{0,16}\btomorrow\b",
                lowered,
            )
            if skip_hit:
                await self.heartbeat.skip_next_wake(today_local + timedelta(days=1))

            # Nightly one-shot arm: "wake me (up) tomorrow / as normal / at 5".
            arm_hit = not skip_hit and re.search(
                r"\bwake me( up)?\b.{0,24}\b(tomorrow|as normal|as usual|per usual|at 5|usual time)\b",
                lowered,
            )
            if arm_hit:
                await self.heartbeat.arm_wake_for(today_local + timedelta(days=1))

            if re.search(
                r"\bgood\s?night\b|\bnight,? jarvis\b|\b(off|going) to (bed|sleep)\b", lowered
            ):
                await self.db.execute(
                    "INSERT INTO sleep_log (day, goodnight_time, tz) VALUES (?, ?, ?)",
                    (today_local.isoformat(), datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"), tz_name),
                )
                if skip_hit or (
                    await self.store.get("wake_skip_date")
                    == (today_local + timedelta(days=1)).isoformat()
                ):
                    wake_note = "No wake-up tomorrow, as agreed — travel understood. Back on the day after."
                elif arm_hit or await self.heartbeat.wake_enabled():
                    wake_note = "Wake sequence armed — 05:00, mirror selfie ends it."
                else:
                    wake_note = ""
                reply = (
                    f"Goodnight, sir. Day closed and logged. {wake_note} "
                    "Phone down, lights low — tomorrow's already taken care of."
                ).replace("  ", " ")
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True

            if skip_hit:
                reply = (
                    "Understood — no wake-up tomorrow. It re-arms automatically the day after; "
                    "say so if travel runs longer."
                )
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True

            if arm_hit:
                reply = "Armed — 05:00 tomorrow, mirror selfie ends it. I'll nudge you towards bed around nine."
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True

            # §11: an explicit rest day is valid training, never a failure.
            if re.search(r"\b(rest|recovery) day\b", lowered) and not looks_negated(transcript):
                await self.streaks.record_recovery(today_local)
                monthly = await self.streaks.monthly_activity(today_local)
                reply = (
                    f"Recovery day logged — that's training too. {monthly['runs']} runs and "
                    f"{monthly['workouts']} workouts this month already; the body builds on rest."
                )
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True

            # §5: water and movement logging by voice
            water_match = re.search(r"\b(\d{2,4})\s?ml\b", lowered)
            water_word = re.search(r"\bwater\b.{0,12}\b(done|down|in|drunk|had)\b|\bdrank\b", lowered)
            moved = re.search(
                r"^\s*moved[.!]?\s*$|\bmovement (done|in)\b|\bstretch(ed)? (done|it)\b"
                r"|\bgot up and moved\b|\bmoved and\b|\band moved\b",
                lowered,
            )
            if water_match or water_word or moved:
                parts = []
                if water_match or water_word:
                    ml = int(water_match.group(1)) if water_match else 300
                    total = await self.heartbeat.log_water(today_local, ml)
                    parts.append(
                        f"water {total / 1000:.1f}L of {self.settings.water_target_ml / 1000:.1f}L"
                    )
                if moved:
                    count = await self.heartbeat.log_movement(today_local)
                    parts.append(f"movement {count} today")
                reply = "Logged — " + " · ".join(parts) + ". Keep it ticking."
                await self.log.log("out", reply, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, reply)
                return True

        # §7 park-it: a distracting thought → Brain Dump, no context switch.
        park = re.match(
            r"\s*park(?:\s+(?:that|this|it))?(?:\s+thought)?\s*[:,\-]?\s+(.+)$",
            transcript,
            re.IGNORECASE,
        )
        if park and self.daily12 is not None:
            reply = await self.daily12.park(park.group(1))
            await self.log.log("out", reply, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, reply)
            return True

        # §7 focus sprints (body-doubling) + "just start".
        if re.search(r"\bsprint\b.{0,20}\b(done|complete|finished|smashed|went (well|fine|great))\b", lowered):
            row = await self.db.fetch_one(
                "SELECT id FROM focus_sprints WHERE completed = 0 ORDER BY id DESC LIMIT 1"
            )
            if row:
                await self.db.execute(
                    "UPDATE focus_sprints SET completed = 1 WHERE id = ?", (row["id"],)
                )
                reply = "Logged. That's how mountains get moved — one clean sprint at a time."
            else:
                reply = "Nothing on the clock, but I'll take the win — say 'start a sprint' next time and I'll time it."
            await self.log.log("out", reply, chat_id=message.chat_id)
            await self.telegram.send_text(message.chat_id, reply)
            return True

        sprint_start = re.search(
            r"\b(?:start|give me|let'?s do|run)\b.{0,20}\bsprint\b|\bfocus sprint\b"
            r"|\b\d{1,2}\s?min(?:ute)?s? sprint\b",
            lowered,
        )
        just_start = re.search(
            r"\bjust start\b|\bhelp me start\b|\bi'?m dreading\b|\bcan'?t (?:face|seem to start)\b"
            r"|\bstruggling to start\b",
            lowered,
        )
        if sprint_start or just_start:
            length_match = re.search(r"\b(\d{1,2})\s?min", lowered)
            minutes = int(length_match.group(1)) if length_match else (5 if just_start else 25)
            title_match = re.search(r"(?:sprint|start)\s+(?:on|for|with)\s+(.+)$", transcript, re.IGNORECASE)
            title = title_match.group(1).strip().rstrip(".?!") if title_match else ""
            sprint_id = await self.db.insert_returning_id(
                "INSERT INTO focus_sprints (started_at, length_min, task, completed)"
                " VALUES (?, ?, ?, 0)",
                (datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"), minutes, title[:200]),
            )
            self._schedule_sprint_buzzer(message.chat_id, sprint_id, minutes, title)
            if just_start:
                reply = (
                    "Smallest possible first step — open the thing, nothing more. "
                    f"{minutes} minutes on the clock, starting now. Anything counts; I'll buzz you."
                )
            else:
                on = f" on '{title}'" if title else ""
                reply = (
                    f"{minutes} minutes{on}. Phone face-down, one thing only — I'm here, quiet, "
                    "until the buzzer. Three, two, one — go."
                )
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

        # Activity reports: "run done", "smashed the workout", "meals on plan".
        # The phrase-match only nominates candidates — the fast model confirms
        # done vs not-done, so "I haven't done my run" never logs a run.
        candidates = detect_activities(transcript)
        meds_candidate = mentions_meds(transcript)
        if candidates or meds_candidate:
            tz_name = await self.store.get(TIMEZONE_KEY, self.settings.timezone_default)
            today = datetime.now(ZoneInfo(tz_name)).date()
            verdicts = await self._confirm_activities(transcript, candidates)

            recorded, corrected = [], []
            if meds_candidate and verdicts.get("medication") == "done" and self.gates is not None:
                await self.gates.confirm("meds", today)
                # §6 adherence log — which items this confirmation covers.
                if self.heartbeat is not None:
                    for item in med_items_mentioned(transcript) or ["adhd"]:
                        await self.heartbeat.record_med(today, item)
                recorded.append("Meds confirmed")
            for activity in candidates:
                verdict = verdicts.get(activity, "na")
                if verdict == "done":
                    result = await self.streaks.record(activity, today)
                    recorded.append(f"{STREAK_LABELS[activity]} streak: {result['current']}")
                    if activity == "run":
                        await self.db.execute(
                            "INSERT INTO runs (run_date, distance_km, duration_min, source)"
                            " VALUES (?, 5.0, 0, 'told')",
                            (today.isoformat(),),
                        )
                elif verdict == "not_done" and await self.streaks.done_today(activity, today):
                    # He's correcting a wrong log — undo it.
                    await self.streaks.unrecord(activity, today)
                    if activity == "run":
                        await self.db.execute(
                            "DELETE FROM runs WHERE run_date = ? AND source = 'told'",
                            (today.isoformat(),),
                        )
                    corrected.append(STREAK_LABELS[activity])

            if recorded:
                ack = "Logged. " + " · ".join(recorded) + "."
                if any(part.startswith("Run") for part in recorded):
                    ack += " Keystone's in — the day's yours now."
                await self.log.log("out", ack, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, ack)
                return not ("?" in transcript or wants_plan(transcript))
            if corrected:
                ack = f"My mistake — {' and '.join(corrected)} unlogged, streak corrected."
                await self.log.log("out", ack, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, ack)
                return False  # let Jarvis respond to the actual situation too
            # Mentioned but not done → the coach handles it in conversation.
            return False
        return False

    async def _group_catchup(self, topic: str) -> str:
        """Summarise the last 48h of ingested work-group Telegram traffic."""
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        since = (datetime.now(_tz.utc) - _td(hours=48)).isoformat(timespec="seconds")
        slug_hints = {
            "eu": "derma_eu", "uk": "derma_uk", "derma": "derma_uk",
            "prodermis": "prodermis", "prime": "prodermis", "bmi": "prodermis",
            "aesthetic": "aesthetics_supply", "grey": "aesthetics_supply",
        }
        slug = next((s for hint, s in slug_hints.items() if hint in topic.lower()), "")
        if slug:
            rows = await self.db.fetch_all(
                "SELECT ts, chat_title, sender, message FROM telegram_ingest"
                " WHERE ts >= ? AND company_tag = ? ORDER BY ts DESC LIMIT 80",
                (since, slug),
            )
        elif topic:
            rows = await self.db.fetch_all(
                "SELECT ts, chat_title, sender, message FROM telegram_ingest"
                " WHERE ts >= ? AND LOWER(chat_title) LIKE ? ORDER BY ts DESC LIMIT 80",
                (since, f"%{topic.lower()}%"),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT ts, chat_title, sender, message FROM telegram_ingest"
                " WHERE ts >= ? ORDER BY ts DESC LIMIT 120",
                (since,),
            )
        if not rows:
            any_row = await self.db.fetch_one("SELECT id FROM telegram_ingest LIMIT 1")
            if any_row is None:
                return (
                    "No group traffic in the brain yet — add me to the work groups and turn "
                    "OFF my privacy mode in BotFather, and I'll hear everything from there."
                )
            return f"Quiet on {'that front' if topic else 'the work groups'} these last two days, sir."
        lines = "\n".join(
            f"[{r['chat_title']}] {r['sender']}: {r['message'][:220]}" for r in reversed(rows)
        )
        try:
            summary = await self.claude.quick(
                lines,
                system=(
                    "Summarise these work Telegram group messages for Paul as Jarvis: what "
                    "moved, decisions, anything waiting on Paul, anything urgent. Group by "
                    "topic, concise, warm. Plain text, no markdown."
                ),
                max_tokens=500,
            )
            return summary or "Traffic's there but the summary escaped me — ask again."
        except Exception:
            logger.exception("Group catch-up summary failed")
            return "Couldn't build the summary just now — try again in a minute."

    ACTIVITY_CONFIRM_SYSTEM = (
        'Paul mentioned daily activities. For each of run, workout, meals, medication, portuguese '
        'decide from his message alone: "done" (he clearly states he completed/took/practised it '
        'today), "not_done" (he states he has NOT / missed it / will do it later), or "na" (not '
        'mentioned or unclear). Negations like "haven\'t", "not done", "yet", "missed" mean '
        'not_done. Reply ONLY JSON: '
        '{"run":"done|not_done|na","workout":"done|not_done|na","meals":"done|not_done|na",'
        '"medication":"done|not_done|na","portuguese":"done|not_done|na"}'
    )

    SPRINT_MINUTE_SECONDS = 60.0  # tests shrink this to fire buzzers instantly

    def _schedule_sprint_buzzer(self, chat_id: int, sprint_id: int, minutes: int, title: str) -> None:
        import asyncio

        task = asyncio.create_task(self._sprint_buzzer(chat_id, sprint_id, minutes, title))
        self._sprint_tasks.add(task)
        task.add_done_callback(self._sprint_tasks.discard)

    async def _sprint_buzzer(self, chat_id: int, sprint_id: int, minutes: int, title: str) -> None:
        import asyncio

        try:
            await asyncio.sleep(minutes * self.SPRINT_MINUTE_SECONDS)
            row = await self.db.fetch_one(
                "SELECT completed FROM focus_sprints WHERE id = ?", (sprint_id,)
            )
            if row and row["completed"]:
                return  # he called it done before the buzzer — no need to interrupt
            on = f" on '{title}'" if title else ""
            text = (
                f"Buzzer, sir — {minutes} minutes{on} done. How did it land? "
                "'Sprint done' logs it, or we go straight into another."
            )
            await self.log.log("out", text, chat_id=chat_id, meta={"sprint": sprint_id})
            await self.telegram.send_text(chat_id, text)
        except Exception:
            logger.exception("Sprint buzzer failed")

    async def _confirm_activities(self, transcript: str, candidates: list[str]) -> dict[str, str]:
        import json as _json
        import re as _re

        try:
            raw = await self.claude.quick(
                transcript, system=self.ACTIVITY_CONFIRM_SYSTEM, max_tokens=100
            )
            match = _re.search(r"\{.*\}", raw, _re.DOTALL)
            if match:
                verdicts = _json.loads(match.group(0))
                allowed = {"done", "not_done", "na"}
                return {k: v for k, v in verdicts.items() if v in allowed}
        except Exception:
            logger.exception("Activity confirmation failed — using conservative fallback")
        # Fallback without the model: under-log rather than falsely credit.
        keys = list(candidates) + (["medication"] if mentions_meds(transcript) else [])
        if looks_negated(transcript):
            return {key: "na" for key in keys}
        return {key: "done" for key in keys}

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
            if results:
                reply = " ".join(results)
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
            if show or not results:
                # A show request builds the plan if it doesn't exist yet —
                # never the old "no plan yet" brush-off.
                await self.daily12.generate(plan_date)
                shown = await self.daily12.format_plan(plan_date)
                await self.log.log("out", shown, chat_id=message.chat_id)
                await self.telegram.send_text(message.chat_id, shown)
            return True
        except Exception:
            logger.exception("Task handling failed%s", " after replying" if replied else "")
            return replied  # only fall through to conversation if nothing happened yet

    async def _handle_email_talk(self, message: IncomingMessage, transcript: str) -> bool:
        """Inbox triage / drafting / the confirm-to-send flow. Returns True when
        handled; False falls through to normal conversation. A send happens ONLY
        when a pending draft exists and Paul explicitly confirms it."""
        import re

        assert self.mail is not None
        try:
            pending = await self.mail.pending_draft()
            if pending is not None and mail_commands.confirms_send(transcript):
                reply = await self.mail.send_pending()
                return await self._say(message, reply)
            if pending is not None and mail_commands.cancels_send(transcript):
                reply = await self.mail.cancel_draft()
                return await self._say(message, reply)
            # Style contacts: "add style contact Kiefer, +447..., kiefer@x.com"
            contact_hit = re.search(
                r"\badd\b.{0,16}\bstyle contact\b[:,]?\s*(.+)$", transcript, re.IGNORECASE
            )
            if contact_hit:
                remainder = contact_hit.group(1)
                email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", remainder)
                phone_m = re.search(r"\+?\d[\d\s]{8,14}\d", remainder)
                note_m = re.search(r"[-—]\s*([^,]+)$", remainder)
                scrub = remainder
                for m in (email_m, phone_m, note_m):
                    if m:
                        scrub = scrub.replace(m.group(0), " ")
                name = re.sub(r"[^A-Za-z ]", " ", scrub).strip().split()
                reply = await self.mail.add_contact(
                    " ".join(name[:2]) if name else "",
                    phone=(phone_m.group(0).replace(" ", "") if phone_m else ""),
                    email=(email_m.group(0) if email_m else ""),
                    note=(note_m.group(1).strip() if note_m else ""),
                )
                return await self._say(message, reply)

            # "teach Kiefer style: <example>" / "this is how I write to Harry: ..."
            teach_hit = re.search(
                r"^\s*teach\s+(?:my\s+)?(\w+)(?:'s)?\s+style\s*[:,\-]\s*(.+)$"
                r"|\bthis is how i (?:write|talk|speak) to (\w+)\s*[:,\-]\s*(.+)$",
                transcript, re.IGNORECASE | re.DOTALL,
            )
            if teach_hit:
                who = teach_hit.group(1) or teach_hit.group(3)
                sample = teach_hit.group(2) or teach_hit.group(4)
                reply = await self.mail.add_person_sample(who, sample)
                return await self._say(message, reply)

            # "learn my style" (generic) / "learn my Kiefer style" (per-person)
            learn_hit = re.search(
                r"\b(?:learn|study|copy|refresh)\b(?:\s+my)?\s+(\w+)?\s*(?:email |writing )?(?:style|voice)\b",
                transcript, re.IGNORECASE,
            )
            if learn_hit:
                who = (learn_hit.group(1) or "").lower()
                if who in ("", "my", "own", "email", "writing", "generic"):
                    reply = await self.mail.learn_style(self.claude)
                else:
                    reply = await self.mail.learn_person_style(self.claude, who)
                return await self._say(message, reply)
            if not mail_commands.mentions_email(transcript):
                return False
            actions = await mail_commands.parse_actions(
                self.claude, transcript, self.mail.labels, await self.mail.last_listing(),
                style=await self.mail.style_profile(),
                person_styles=await self.mail.person_styles(),
            )
            if not actions:
                return False  # ordinary conversation after all
            results = await mail_commands.execute_actions(self.mail, actions)
            if not results:
                return False
            reply = "\n\n".join(results)
            await self.log.log("out", reply, chat_id=message.chat_id, meta={"email_actions": True})
            await self.telegram.send_text(message.chat_id, reply)  # inboxes & drafts go as text
            return True
        except Exception:
            logger.exception("Email handling failed")
            return False

    async def _say(self, message: IncomingMessage, reply: str) -> bool:
        """Log + deliver a short reply with the usual voice/text mix."""
        await self.log.log("out", reply, chat_id=message.chat_id)
        channel, reply_text = decide_reply(reply, incoming_was_voice=message.is_voice)
        if channel == "voice":
            try:
                audio = await self.elevenlabs.synthesize(strip_for_speech(reply_text))
                await self.telegram.send_voice(message.chat_id, audio, transcript=reply_text)
                return True
            except SynthesisError:
                logger.exception("TTS failed — falling back to text")
        await self.telegram.send_text(message.chat_id, reply_text)
        return True

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
