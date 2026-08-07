"""The Twilio phone channel — Jarvis on a real phone call (Build brief, 4 Aug).

A custom pipeline, deliberately NOT ElevenLabs Agents: Twilio rings (or
answers) the call and speaks TwiML we generate; Paul's speech is captured by
<Gather input="speech"> (Twilio's own STT — turn-based v1, Media Streams
realtime is a later upgrade); each utterance runs through the router's full
brain (memory, tools, board truth, persona) via the injected `brain` callable;
the reply comes back in Jarvis's own ElevenLabs voice, served as short-lived
MP3s from our own audio endpoint. Every layer degrades gracefully: TTS failure
falls back to Twilio's <Say>, a brain failure gets an honest spoken line —
the call always answers (invariant 5).
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

AUDIO_TTL_SECONDS = 15 * 60          # a call's audio never needs to outlive it
DEFAULT_GREETING = "Paul. Jarvis here. What do you need?"
FAREWELL = "Speak soon, sir."
BRAIN_STUMBLE = "I lost my thread there — give me that once more?"

PIPELINE_ERROR_LINE = (
    "Something choked in my pipeline there, sir. Give me a minute and try "
    "again — or message me on Telegram, I'm fine over there."
)
# Distinct by ear from the pipeline line: if Paul hears THIS, the request
# reached us but failed Twilio's signature check — a config problem, not a
# crash — and the Render logs will name the mismatch.
SIGNATURE_ERROR_LINE = (
    "That request didn't carry a valid Twilio signature, so I'm not taking "
    "instructions from it. If that was really you calling, the logs will "
    "say why, sir."
)


def error_twiml(line: str = PIPELINE_ERROR_LINE) -> str:
    """The last-resort TwiML: whatever broke, Jarvis owns it out loud instead
    of Twilio's stock 'an application error has occurred' (invariant 5)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Brian">'
        f"{escape(line)}</Say><Hangup/></Response>"
    )


GOODBYE = re.compile(
    r"\b(good\s*bye|bye|hang\s+up|that('|’)?s\s+(all|everything)|good\s*night|"
    r"cheers\s+jarvis|thanks\s+jarvis|nothing\s+else)\b",
    re.IGNORECASE,
)


class PhoneChannel:
    """Owns call placement, the TwiML conversation loop and the audio cache.
    The FastAPI endpoints in main.py are thin shims over this."""

    def __init__(
        self,
        twilio,
        elevenlabs,
        *,
        from_number: str,
        paul_number: str,
        public_url: str,
        secret: str,
    ) -> None:
        self.twilio = twilio
        self._elevenlabs = elevenlabs
        self.from_number = from_number
        self.paul_number = paul_number
        self._public_url = public_url.rstrip("/")
        self._secret = secret
        # The router injects its phone_turn here (async transcript -> reply);
        # constructor-time injection would be circular (router needs main's
        # components, main builds this before the router).
        self.brain = None
        # Realtime upgrade (7 Aug): when the live voice engine is up, INBOUND
        # calls stream straight to the ElevenLabs agent over Media Streams —
        # interruptible, no turn lag. main.py flips this on; outbound
        # greetings and the wake script keep the turn-based flow, and a
        # failed stream Redirects back here with ?fallback=1.
        self.realtime_available = False
        self.eleven_connect = None   # test seam: async factory for the agent socket
        # Wake v2: while a wake sequence runs, its scripted handler intercepts
        # every turn (instant, no LLM); returning None falls through to the
        # brain. '[[bye]]'-prefixed replies are spoken and then hung up.
        self.script_handler = None
        self.listen_timeout = None   # Gather 'timeout' override (wake waits ~12s)
        self._audio: dict[str, tuple[float, bytes]] = {}
        self._greetings: dict[str, str] = {}
        self._tts_memo: dict[str, bytes] = {}   # scripted lines synth once, replay free
        # Whitelisted-contact calls (7 Aug): a third party on the line NEVER
        # reaches Paul's brain/tools/memory — their turns route to guest_brain
        # (router injects a scoped, privacy-walled persona). Keyed by CallSid,
        # registered at placement so the status callback can report a call
        # that was never answered.
        self.guest_brain = None      # async (contact_key, speech, call_sid) -> reply
        self.on_guest_event = None   # async (event, contact, detail) -> None
        self._guest_calls: dict[str, dict] = {}

    # ------------------------------------------------------------ URLs

    @property
    def configured(self) -> bool:
        return bool(self.from_number and self.paul_number and self._public_url)

    def answer_url(self) -> str:
        return f"{self._public_url}/twilio/voice/{self._secret}/answer"

    def _turn_url(self) -> str:
        return f"{self._public_url}/twilio/voice/{self._secret}/turn"

    def _audio_url(self, audio_id: str) -> str:
        return f"{self._public_url}/twilio/audio/{self._secret}/{audio_id}.mp3"

    def status_url(self) -> str:
        return f"{self._public_url}/twilio/voice/{self._secret}/status"

    def media_ws_url(self) -> str:
        base = self._public_url
        for scheme, ws in (("https://", "wss://"), ("http://", "ws://")):
            if base.startswith(scheme):
                base = ws + base[len(scheme):]
                break
        return f"{base}/twilio/media/{self._secret}"

    # ------------------------------------------------------------ audio cache

    def stash_audio(self, data: bytes) -> str:
        now = time.monotonic()
        self._audio = {
            k: (born, blob) for k, (born, blob) in self._audio.items()
            if now - born < AUDIO_TTL_SECONDS
        }
        audio_id = uuid.uuid4().hex
        self._audio[audio_id] = (now, data)
        return audio_id

    def get_audio(self, audio_id: str) -> bytes | None:
        entry = self._audio.get(audio_id)
        if entry is None or time.monotonic() - entry[0] >= AUDIO_TTL_SECONDS:
            return None
        return entry[1]

    def prewarm(self, lines: list[str]) -> None:
        """Wake v2 (5 Aug fix): synthesize the scripted bank into the memo
        BEFORE the call needs it — standard lines then play with zero model
        latency. Background task; failures just mean the live path synthesizes
        as usual."""
        import asyncio

        async def _warm() -> None:
            for line in lines:
                if line and line not in self._tts_memo:
                    try:
                        self._tts_memo[line] = await self._elevenlabs.synthesize(
                            line, model="eleven_flash_v2_5", output_format="mp3_22050_32"
                        )
                    except Exception:
                        logger.exception("TTS prewarm stopped — live synth carries on")
                        return

        task = asyncio.create_task(_warm())
        self._warm_tasks: set = getattr(self, "_warm_tasks", set())
        self._warm_tasks.add(task)
        task.add_done_callback(self._warm_tasks.discard)

    # ------------------------------------------------------------ TwiML

    @staticmethod
    def _document(inner: str) -> str:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'

    async def _voiced(self, text: str) -> str:
        """<Play> in Jarvis's own voice, or <Say> if ElevenLabs is down.
        Flash model + low bitrate: still his voice, a fraction of the
        synthesis time, and a phone line can't hear the difference."""
        try:
            audio = self._tts_memo.get(text)
            if audio is None:
                audio = await self._elevenlabs.synthesize(
                    text, model="eleven_flash_v2_5", output_format="mp3_22050_32"
                )
                if len(self._tts_memo) >= 60:
                    self._tts_memo.clear()   # blunt cap; scripted lines re-warm fast
                self._tts_memo[text] = audio
            return f"<Play>{escape(self._audio_url(self.stash_audio(audio)))}</Play>"
        except Exception:
            logger.exception("Phone TTS failed — Twilio <Say> carries the line")
            return f'<Say voice="Polly.Brian">{escape(text)}</Say>'

    async def _speak_and_listen(self, text: str, farewell: str | None = None) -> str:
        """Say `text`, then listen for the next utterance. Silence past the
        Gather normally ends the call politely (with `farewell` — a guest call
        gets its own warm sign-off, never Paul's); while a wake script is
        driving (script_handler set), silence REDIRECTS back to the turn
        endpoint so the script can pitch — the script bounds its own silence
        loop."""
        voiced = await self._voiced(text)
        wait = f' timeout="{int(self.listen_timeout)}"' if self.listen_timeout else ""
        if self.script_handler is not None:
            after = f"<Redirect method='POST'>{escape(self._turn_url())}</Redirect>"
        else:
            after = f'<Say voice="Polly.Brian">{escape(farewell or FAREWELL)}</Say><Hangup/>'
        return self._document(
            f'<Gather input="speech" language="en-GB" speechTimeout="auto"{wait} '
            f'speechModel="experimental_conversations" profanityFilter="false" '
            f'action="{escape(self._turn_url())}" method="POST">{voiced}</Gather>{after}'
        )

    async def _speak_and_hangup(self, text: str) -> str:
        return self._document(f"{await self._voiced(text)}<Hangup/>")

    # ------------------------------------------------------------ the call

    async def call_paul(self, greeting: str | None = None) -> bool:
        """Ring Paul's phone; when he answers, `greeting` (or the default)
        opens the conversation. This is both the 'call me' path and the
        wake-up escalation channel."""
        if not self.configured:
            return False
        token = uuid.uuid4().hex
        self._greetings[token] = (greeting or DEFAULT_GREETING).strip() or DEFAULT_GREETING
        sid = await self.twilio.place_call(
            self.paul_number, self.from_number, f"{self.answer_url()}?g={token}"
        )
        if not sid:
            self._greetings.pop(token, None)
        return bool(sid)

    async def call_contact(self, contact: dict, greeting: str) -> str:
        """Ring a WHITELISTED contact (7 Aug — John Debono seeded). The caller
        (router) has already matched the name against the whitelist; this
        layer never sees an arbitrary number. Returns the CallSid ('' on
        failure). The status callback is how no-answer/busy/failed get
        reported honestly instead of Paul assuming it connected."""
        if not self.configured:
            return ""
        sid = await self.twilio.place_call(
            contact["number"], self.from_number, self.answer_url(),
            status_callback=self.status_url(),
        )
        if sid:
            self._guest_calls[sid] = {
                "contact": contact,
                "greeting": greeting,
                "answered": False,
                "farewell": (
                    f"Cheers {contact['short']} — I'll tell Paul you said "
                    "hello. Take care now."
                ),
            }
        return sid

    def _guest(self, params: dict[str, str]) -> dict | None:
        return self._guest_calls.get(params.get("CallSid", ""))

    async def _guest_event(self, event: str, contact: dict, detail: str = "") -> None:
        if self.on_guest_event is None:
            return
        try:
            await self.on_guest_event(event, contact, detail)
        except Exception:
            logger.exception("Guest-call event report failed (call carries on)")

    async def handle_status(self, params: dict[str, str]) -> None:
        """Twilio's final word on an outbound guest call. 'completed' after an
        answer needs no report here (the goodbye turn already told Paul);
        anything else means the chat never happened — say so honestly."""
        record = self._guest_calls.pop(params.get("CallSid", ""), None)
        if record is None:
            return
        status = (params.get("CallStatus") or "").lower()
        if status in ("no-answer", "busy", "failed", "canceled"):
            await self._guest_event(status, record["contact"])
        elif status == "completed" and not record["answered"]:
            # Answered by machine/voicemail-cut so fast no turn ever ran.
            await self._guest_event("no-answer", record["contact"])

    async def handle_answer(self, params: dict[str, str]) -> str:
        """First TwiML of a call — outbound (greeting token in `g`) or inbound
        (Paul rang the Twilio number). An inbound call goes REALTIME when the
        live engine is up: <Connect><Stream> into the media bridge, with a
        Redirect back here (?fallback=1) so a failed stream still gets the
        turn-based conversation, never dead air (invariant 5)."""
        guest = self._guest(params)
        if guest is not None:
            guest["answered"] = True
            await self._guest_event("answered", guest["contact"])
            return await self._speak_and_listen(guest["greeting"], farewell=guest["farewell"])
        inbound = params.get("Direction", "").startswith("inbound")
        if (
            self.realtime_available
            and self.script_handler is None
            and inbound
            and not params.get("g")
            and params.get("fallback") != "1"
        ):
            return self._document(
                f'<Connect><Stream url="{escape(self.media_ws_url())}"/></Connect>'
                f'<Redirect method="POST">{escape(self.answer_url())}?fallback=1</Redirect>'
            )
        greeting = self._greetings.pop(params.get("g", ""), "") or DEFAULT_GREETING
        return await self._speak_and_listen(greeting)

    async def handle_turn(self, params: dict[str, str]) -> str:
        """One conversational turn: Paul's utterance in, Jarvis's reply out.
        A live wake script gets first refusal — instant scripted lines, no
        LLM; its None means 'off-script, give it to the brain'."""
        speech = (params.get("SpeechResult") or "").strip()
        guest = self._guest(params)
        if guest is not None:
            # A third party on the line: their words NEVER reach Paul's brain,
            # tools or memory — only the scoped guest persona. Hard wall.
            return await self._guest_turn(guest, params.get("CallSid", ""), speech)
        if self.script_handler is not None:
            try:
                scripted = await self.script_handler(speech)
            except Exception:
                logger.exception("Wake script turn failed — falling through")
                scripted = None
            if scripted is not None:
                if scripted.startswith("[[bye]]"):
                    return await self._speak_and_hangup(scripted[len("[[bye]]"):])
                return await self._speak_and_listen(scripted)
        if not speech:
            return await self._speak_and_listen("Didn't catch that — say it once more?")
        if GOODBYE.search(speech) and len(speech) <= 60:
            return await self._speak_and_hangup(FAREWELL)
        reply = BRAIN_STUMBLE
        if self.brain is not None:
            try:
                reply = (await self.brain(speech)) or BRAIN_STUMBLE
            except Exception:
                logger.exception("Phone brain turn failed — honest stumble line")
        # The brain can end a call too (6 Aug: the master switch answers an
        # inbound call with one line and hangs up).
        if reply.startswith("[[bye]]"):
            return await self._speak_and_hangup(reply[len("[[bye]]"):])
        return await self._speak_and_listen(reply)

    async def _guest_turn(self, guest: dict, call_sid: str, speech: str) -> str:
        """One turn of a whitelisted-contact call. Chit-chat until they say
        goodbye or hang up; every path degrades to a warm sign-off, never
        dead air."""
        contact = guest["contact"]
        if not speech:
            return await self._speak_and_listen(
                f"Sorry {contact['short']}, I didn't catch that — say again?",
                farewell=guest["farewell"],
            )
        if GOODBYE.search(speech) and len(speech) <= 60:
            self._guest_calls.pop(call_sid, None)
            await self._guest_event("ended", contact, "they said goodbye")
            return await self._speak_and_hangup(guest["farewell"])
        reply = ""
        if self.guest_brain is not None:
            try:
                reply = (await self.guest_brain(contact["key"], speech, call_sid)) or ""
            except Exception:
                logger.exception("Guest brain turn failed — warm sign-off instead")
        if not reply:
            # No scoped brain (or it stumbled): never wing it with a third
            # party on the line — wind up warmly and report back.
            self._guest_calls.pop(call_sid, None)
            await self._guest_event("ended", contact, "my end stumbled, so I wrapped up early")
            return await self._speak_and_hangup(guest["farewell"])
        if reply.startswith("[[bye]]"):
            self._guest_calls.pop(call_sid, None)
            await self._guest_event("ended", contact, "wound up naturally")
            return await self._speak_and_hangup(reply[len("[[bye]]"):])
        return await self._speak_and_listen(reply, farewell=guest["farewell"])
