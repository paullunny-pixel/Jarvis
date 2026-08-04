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
        self._audio: dict[str, tuple[float, bytes]] = {}
        self._greetings: dict[str, str] = {}

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

    # ------------------------------------------------------------ TwiML

    @staticmethod
    def _document(inner: str) -> str:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'

    async def _voiced(self, text: str) -> str:
        """<Play> in Jarvis's own voice, or <Say> if ElevenLabs is down.
        Flash model + low bitrate: still his voice, a fraction of the
        synthesis time, and a phone line can't hear the difference."""
        try:
            audio = await self._elevenlabs.synthesize(
                text, model="eleven_flash_v2_5", output_format="mp3_22050_32"
            )
            return f"<Play>{escape(self._audio_url(self.stash_audio(audio)))}</Play>"
        except Exception:
            logger.exception("Phone TTS failed — Twilio <Say> carries the line")
            return f'<Say voice="Polly.Brian">{escape(text)}</Say>'

    async def _speak_and_listen(self, text: str) -> str:
        """Say `text`, then listen for Paul's next utterance. If he stays
        silent past the Gather, the call ends politely instead of hanging
        (plain <Say> there — not worth an extra TTS round-trip per turn)."""
        voiced = await self._voiced(text)
        return self._document(
            f'<Gather input="speech" language="en-GB" speechTimeout="auto" '
            f'speechModel="experimental_conversations" profanityFilter="false" '
            f'action="{escape(self._turn_url())}" method="POST">{voiced}</Gather>'
            f'<Say voice="Polly.Brian">{escape(FAREWELL)}</Say><Hangup/>'
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

    async def handle_answer(self, params: dict[str, str]) -> str:
        """First TwiML of a call — outbound (greeting token in `g`) or inbound
        (Paul rang the Twilio number)."""
        greeting = self._greetings.pop(params.get("g", ""), "") or DEFAULT_GREETING
        return await self._speak_and_listen(greeting)

    async def handle_turn(self, params: dict[str, str]) -> str:
        """One conversational turn: Paul's utterance in, Jarvis's reply out."""
        speech = (params.get("SpeechResult") or "").strip()
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
        return await self._speak_and_listen(reply)
