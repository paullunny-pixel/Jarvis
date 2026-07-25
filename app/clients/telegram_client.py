"""Telegram Bot API client — Jarvis's front door.

Thin, fully-tested wrapper over the official Bot API (webhook mode in
production on Render; scripts/run_polling.py exists for local dev). Handles:
sending text (chunked to the 4096-char limit), sending voice notes (MP3 from
ElevenLabs is accepted directly), downloading Paul's voice notes, chat
actions, and webhook management.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096
MAX_CAPTION_LEN = 1024


class TelegramError(RuntimeError):
    pass


@dataclass
class IncomingMessage:
    """A normalised inbound Telegram message."""

    chat_id: int
    message_id: int
    from_name: str
    text: str = ""
    voice_file_id: str = ""
    voice_duration: int = 0
    document_file_id: str = ""
    document_name: str = ""
    document_mime: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_voice(self) -> bool:
        return bool(self.voice_file_id)

    @property
    def is_document(self) -> bool:
        return bool(self.document_file_id)


def parse_update(update: dict[str, Any]) -> IncomingMessage | None:
    """Extract the message from a webhook update. Returns None for updates we
    don't handle (edits, channel posts, etc.)."""
    message = update.get("message")
    if not message or "chat" not in message:
        return None
    voice = message.get("voice") or {}  # only true voice notes; audio files are not speech
    document = message.get("document") or {}
    sender = message.get("from") or {}
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or "unknown"
    return IncomingMessage(
        chat_id=message["chat"]["id"],
        message_id=message.get("message_id", 0),
        from_name=name,
        text=(message.get("text") or message.get("caption") or "").strip(),
        voice_file_id=voice.get("file_id", ""),
        voice_duration=voice.get("duration", 0),
        document_file_id=document.get("file_id", ""),
        document_name=document.get("file_name", "upload"),
        document_mime=document.get("mime_type", ""),
        raw=update,
    )


def chunk_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split long text on paragraph/line/space boundaries under Telegram's limit."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = text.rfind(sep, 0, limit)
            if cut > 0:
                break
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._token = bot_token
        self._api = f"{API_BASE}/bot{bot_token}"
        self._file_api = f"{API_BASE}/file/bot{bot_token}"
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, *, data: dict | None = None, files: dict | None = None) -> Any:
        response = await self._client.post(f"{self._api}/{method}", data=data, files=files)
        payload = response.json()
        if not payload.get("ok"):
            raise TelegramError(f"{method} failed: {payload.get('description', response.text[:200])}")
        return payload["result"]

    # --- Sending ---

    async def send_text(self, chat_id: int, text: str) -> None:
        for chunk in chunk_text(text):
            await self._call("sendMessage", data={"chat_id": chat_id, "text": chunk})

    async def send_voice(self, chat_id: int, audio_mp3: bytes, transcript: str = "") -> None:
        """Send a voice note; the transcript rides underneath (caption if it
        fits, else a follow-up message) so everything Jarvis says is readable."""
        data: dict[str, Any] = {"chat_id": chat_id}
        follow_up = ""
        if transcript:
            if len(transcript) <= MAX_CAPTION_LEN:
                data["caption"] = transcript
            else:
                follow_up = transcript
        await self._call(
            "sendVoice", data=data, files={"voice": ("jarvis.mp3", audio_mp3, "audio/mpeg")}
        )
        if follow_up:
            await self.send_text(chat_id, follow_up)

    async def send_document(
        self, chat_id: int, data: bytes, filename: str, mime: str = "application/octet-stream", caption: str = ""
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption[:MAX_CAPTION_LEN]
        await self._call(
            "sendDocument", data=payload, files={"document": (filename, data, mime)}
        )

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self._call("sendChatAction", data={"chat_id": chat_id, "action": action})
        except (TelegramError, httpx.HTTPError):  # purely cosmetic — never fail on it
            logger.debug("chat action failed", exc_info=True)

    # --- Receiving ---

    async def download_file(self, file_id: str) -> bytes:
        info = await self._call("getFile", data={"file_id": file_id})
        file_path = info["file_path"]
        response = await self._client.get(f"{self._file_api}/{file_path}")
        response.raise_for_status()
        return response.content

    # --- Webhook management ---

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self._call(
            "setWebhook",
            data={
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": '["message"]',
                "drop_pending_updates": "false",
            },
        )

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", data={})

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict[str, Any]]:
        """Long-poll for updates (local dev only; production uses the webhook)."""
        return await self._call(
            "getUpdates", data={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'}
        )
