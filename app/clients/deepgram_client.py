"""Deepgram speech-to-text (Paul's voice notes → text). Nova model, binary upload."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.deepgram.com/v1/listen"


class TranscriptionError(RuntimeError):
    pass


class DeepgramClient:
    def __init__(
        self,
        api_key: str,
        model: str = "nova-3",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"Authorization": f"Token {api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def transcribe(self, audio: bytes, mimetype: str = "audio/ogg") -> str:
        """Transcribe a voice note. Telegram voice notes are OGG/Opus — Deepgram
        decodes them natively, so no audio conversion step is needed."""
        params = {
            "model": self.model,
            "smart_format": "true",
            "language": "en",
        }
        response = await self._client.post(
            API_URL, params=params, content=audio, headers={"Content-Type": mimetype}
        )
        if response.status_code != 200:
            raise TranscriptionError(f"Deepgram {response.status_code}: {response.text[:300]}")
        data = response.json()
        try:
            transcript = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError) as exc:
            raise TranscriptionError(f"Unexpected Deepgram response shape: {data}") from exc
        return transcript.strip()
