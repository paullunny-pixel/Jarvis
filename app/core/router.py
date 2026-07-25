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
from app.heartbeat.gates import GateKeeper, mentions_meds
from app.heartbeat.streaks import STREAK_LABELS, Streaks, detect_activities, looks_negated
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

        # 2c. Task talk → the Daily 12 + Trello write-back (Milestone 3).
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
        system = build_system_prompt(
            timezone=timezone,
            memory_context=memory_context,
            system_status=self._integration_status(),
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
            f"- Trello / Daily 12: {'CONNECTED' if self.daily12 is not None else 'not connected yet'}",
            f"- Memory search: {'Voyage embeddings' if s.voyage_api_key else 'local fallback (Voyage key pending)'}",
            f"- Calendar: {'connected (read-only)' if s.calendar_ics_url else 'not connected yet'}",
            f"- Kiefer nightly email: {'configured' if (s.gmail_address and s.kiefer_email) else 'not configured yet'}",
            f"- Apple Health webhook: {'configured' if s.apple_health_webhook_secret else 'not configured yet'}",
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
                lines.append(
                    f"✅ Trello: connected — board '{health['board_name']}', "
                    f"{health['cached_cards']} cards cached, last sync {health['last_sync'][:16] or 'never'}"
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
        lines.append(
            "✅ Kiefer email: configured" if (s.gmail_address and s.kiefer_email)
            else "▫️ Kiefer email: not configured"
        )
        lines.append(
            "✅ Apple Health: webhook ready" if s.apple_health_webhook_secret
            else "▫️ Apple Health: not configured"
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

    async def _handle_life_signals(self, message: IncomingMessage, transcript: str) -> bool:
        import re

        lowered = transcript.lower()

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

    ACTIVITY_CONFIRM_SYSTEM = (
        'Paul mentioned daily activities. For each of run, workout, meals, medication decide from '
        'his message alone: "done" (he clearly states he completed/took it today), "not_done" (he '
        'states he has NOT / missed it / will do it later), or "na" (not mentioned or unclear). '
        'Negations like "haven\'t", "not done", "yet", "missed" mean not_done. Reply ONLY JSON: '
        '{"run":"done|not_done|na","workout":"done|not_done|na","meals":"done|not_done|na",'
        '"medication":"done|not_done|na"}'
    )

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
