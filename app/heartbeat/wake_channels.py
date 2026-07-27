"""Wake-up escalation channels (Master Update §4).

The sequence logic in jobs.py talks to a Channel; Telegram is the only
implementation today. A Twilio voice-call channel plugs in here later as the
primary escalation WITHOUT refactoring the sequence — config placeholders
(TWILIO_*) already exist in Settings. Deliberately not built yet.

Wake messages bypass the heartbeat's identical-message dedupe on purpose —
an escalating loop until the mirror selfie arrives IS the feature — so each
channel receives pre-varied text and sends directly.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class WakeChannel(Protocol):
    name: str

    async def escalate(self, step: int, text: str) -> None: ...


class PhoneCallWakeChannel:
    """The Twilio escalation, live: Jarvis literally rings Paul and holds a
    realtime conversation until he's demonstrably conscious. The mirror
    selfie in Telegram remains the confirm step. Activates only when the
    Twilio number is registered in ElevenLabs and both numbers configured."""

    name = "phone"

    def __init__(self, engine, phone_number_id: str, to_number: str) -> None:
        self._engine = engine
        self._phone_number_id = phone_number_id
        self._to = to_number

    async def escalate(self, step: int, text: str) -> None:
        called = await self._engine.outbound_call(self._phone_number_id, self._to)
        if not called:
            logger.warning("Wake call failed at step %d — Telegram carries on", step)


class TelegramWakeChannel:
    """Escalating Telegram messages; a voice note every few steps."""

    name = "telegram"

    def __init__(self, telegram, elevenlabs, owner_chat, message_log) -> None:
        self._telegram = telegram
        self._elevenlabs = elevenlabs
        self._owner_chat = owner_chat        # async () -> int
        self._log = message_log

    async def escalate(self, step: int, text: str) -> None:
        chat_id = await self._owner_chat()
        if not chat_id:
            return
        await self._log.log("out", text, chat_id=chat_id, meta={"wake": True, "step": step})
        if self._elevenlabs is not None and step % 4 == 2:
            try:
                audio = await self._elevenlabs.synthesize(text)
                await self._telegram.send_voice(chat_id, audio, transcript=text)
                return
            except Exception:
                logger.exception("Wake voice failed — text instead")
        await self._telegram.send_text(chat_id, text)
