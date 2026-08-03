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
    RETRY_WAIT = 1.5   # transient 5xx — quick retry
    RATE_WAIT = 20.0   # 429/529 clear on the provider's clock, not ours

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

    async def _call(
        self, payload: dict[str, Any], retries: int = 2, timeout: float | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": timeout} if timeout else {}
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await self._client.post(API_URL, json=payload, **kwargs)
                if response.status_code in (429, 500, 502, 503, 529):
                    raise ClaudeError(f"retryable status {response.status_code}: {response.text[:200]}")
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ClaudeError) as exc:  # noqa: PERF203
                last_error = exc
                if attempt < retries:
                    # A rate limit retried after 1.5s just burns the attempt —
                    # it needs the provider's window to pass.
                    wait = self.RETRY_WAIT * (attempt + 1)
                    if "429" in str(exc) or "529" in str(exc):
                        wait = self.RATE_WAIT * (attempt + 1)
                    await asyncio.sleep(wait)
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
        timeout: float | None = None,  # long-corpus jobs (email research) need more than chat
    ) -> str:
        """Full-quality Jarvis reply (brain model)."""
        data = await self._call(
            {
                "model": self.brain_model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages,
            },
            timeout=timeout,
        )
        return self._text_of(data)

    async def converse_with_tools(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        handler,  # async (name: str, input: dict) -> str — executes one tool call
        max_rounds: int = 6,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> str:
        """Brain-first (Phase A2): the tool-use loop. The brain model answers
        with text and/or tool calls; each call runs through `handler` (the
        deterministic machinery) and its result goes back until the model
        speaks plain text. The final text is the reply."""
        convo = list(messages)
        text = ""
        for _ in range(max_rounds):
            data = await self._call(
                {
                    "model": self.brain_model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": convo,
                    "tools": tools,
                },
                timeout=timeout,
            )
            text = self._text_of(data)
            tool_uses = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
            if not tool_uses:
                return text
            convo.append({"role": "assistant", "content": data["content"]})
            results = []
            for use in tool_uses:
                try:
                    out = await handler(use.get("name", ""), use.get("input") or {})
                except Exception as exc:  # a tool crash must never kill the reply
                    logger.exception("Tool '%s' failed", use.get("name"))
                    out = f"TOOL FAILED: {type(exc).__name__} — tell Paul honestly; do not pretend it worked."
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.get("id", ""),
                        "content": str(out)[:4000],
                    }
                )
            convo.append({"role": "user", "content": results})
        return text  # rounds exhausted — whatever text we have is the reply

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
