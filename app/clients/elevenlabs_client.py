"""ElevenLabs text-to-speech (Jarvis's voice). Returns MP3 bytes, which Telegram
accepts directly for voice messages — no conversion step needed."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"


class SynthesisError(RuntimeError):
    pass


class ElevenLabsClient:
    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_multilingual_v2",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.voice_id = voice_id
        self.model = model
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"xi-api-key": api_key},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def synthesize(
        self,
        text: str,
        *,
        model: str | None = None,
        output_format: str = "mp3_44100_128",
    ) -> bytes:
        """Speak `text` in Jarvis's voice; returns MP3 audio bytes. The phone
        channel overrides `model`/`output_format` for latency — a phone line
        is 8kHz anyway, the full-fat model buys nothing there."""
        url = f"{API_BASE}/text-to-speech/{self.voice_id}"
        response = await self._client.post(
            url,
            params={"output_format": output_format},
            json={
                "text": text,
                "model_id": model or self.model,
                "voice_settings": {
                    # Tuned for composed, even delivery (the unflappable-aide register)
                    "stability": 0.62,
                    "similarity_boost": 0.75,
                    "style": 0.15,
                    "use_speaker_boost": True,
                },
            },
        )
        if response.status_code != 200:
            raise SynthesisError(f"ElevenLabs {response.status_code}: {response.text[:300]}")
        return response.content
