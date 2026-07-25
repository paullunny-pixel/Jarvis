"""Claude API client (the brain). Thin httpx wrapper around /v1/messages.

Two tiers per the locked stack: the brain model (Opus) for conversation and
coaching, the fast model (Haiku) for routing/parsing/classification.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class ClaudeError(RuntimeError):
    pass


class ClaudeClient:
    def __init__(
        self,
        api_key: str,
        brain_model: str = "claude-opus-5",
        fast_model: str = "claude-haiku-4-5",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.brain_model = brain_model
        self.fast_model = fast_model
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, payload: dict[str, Any], retries: int = 2) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await self._client.post(API_URL, json=payload)
                if response.status_code in (429, 500, 502, 503, 529):
                    raise ClaudeError(f"retryable status {response.status_code}: {response.text[:200]}")
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ClaudeError) as exc:  # noqa: PERF203
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise ClaudeError(f"Claude API failed after {retries + 1} attempts: {last_error}")

    @staticmethod
    def _text_of(data: dict[str, Any]) -> str:
        return "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()

    async def converse(
        self,
        system: str,
        messages: list[dict[str, Any]],  # content may be text or vision blocks
        max_tokens: int = 1024,
    ) -> str:
        """Full-quality Jarvis reply (brain model)."""
        data = await self._call(
            {
                "model": self.brain_model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages,
            }
        )
        return self._text_of(data)

    async def quick_vision(
        self,
        prompt: str,
        image_b64: str,
        media_type: str = "image/jpeg",
        system: str = "",
        max_tokens: int = 300,
    ) -> str:
        """Cheap/fast task over an image (fast model has vision)."""
        payload: dict[str, Any] = {
            "model": self.fast_model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        if system:
            payload["system"] = system
        return self._text_of(await self._call(payload))

    async def quick(self, prompt: str, system: str = "", max_tokens: int = 300) -> str:
        """Cheap/fast structured task (fast model): routing, parsing, classification."""
        payload: dict[str, Any] = {
            "model": self.fast_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        return self._text_of(await self._call(payload))
