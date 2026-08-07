"""OpenAI Chat Completions client — Seat B of the War Room (7 Aug brief).
Thin httpx wrapper, deliberately no SDK, same house style as every other
client here (Zoom, Gemini, Twilio, …).

Reasoning effort ('ultra' on the Full Board, unset on the Quick Board) rides
the `reasoning_effort` field — the API surface for steering an OpenAI
reasoning model's depth. A refusal comes back as `message.refusal` (or a
`content_filter` finish_reason on older shapes) rather than an exception —
callers must treat that as data, not a failure.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIError(RuntimeError):
    pass


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 90.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int = 800,
        effort: str = "",
    ) -> dict[str, Any]:
        """Returns {'text': str, 'refused': bool, 'model_missing': bool}. A
        404 on the model id is reported, never raised — the caller (the
        War Room's model-availability guard) decides the fallback."""
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if effort:
            payload["reasoning_effort"] = effort
        response = await self._client.post(API_URL, json=payload)
        if response.status_code == 404:
            return {"text": "", "refused": False, "model_missing": True, "usage": {"input": 0, "output": 0}}
        if response.status_code >= 400:
            raise OpenAIError(f"OpenAI {response.status_code}: {response.text[:300]}")
        data = response.json()
        usage_raw = data.get("usage") or {}
        usage = {
            "input": int(usage_raw.get("prompt_tokens", 0)),
            "output": int(usage_raw.get("completion_tokens", 0)),
        }
        choices = data.get("choices") or []
        if not choices:
            return {"text": "", "refused": False, "model_missing": False, "usage": usage}
        message = choices[0].get("message") or {}
        refused = bool(message.get("refusal")) or choices[0].get("finish_reason") == "content_filter"
        text = str(message.get("refusal") or message.get("content") or "").strip()
        return {"text": text, "refused": refused, "model_missing": False, "usage": usage}
