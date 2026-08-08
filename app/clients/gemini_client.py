"""Google Generative Language API client — Seat C of the War Room (7 Aug
brief) AND the cockpit's 'Ask Gemini' box (8 Aug). Thin httpx wrapper,
deliberately no SDK. The API key rides the URL (Google's own convention for
this API), not a header.

Two callers, one client:
- `generate()` — the War Room seat contract: refusal/safety blocks come back
  as an empty `candidates` list with `promptFeedback.blockReason` set, or a
  candidate with `finishReason` of SAFETY/RECITATION/OTHER — reported as
  data (never raised), same contract as the OpenAI client so the War Room's
  three seats handle refusal identically regardless of vendor.
- `chat()` — the cockpit box: a stateless multi-turn exchange on the
  instance's own `model`. Jarvis's brain stays Claude (§15 locked stack);
  the box is dashboard furniture, walled off from memory and private data —
  whatever Paul types in it is all Gemini ever sees.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
REFUSAL_REASONS = {"SAFETY", "RECITATION", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT"}

CHAT_SYSTEM = (
    "You are Gemini, a general-purpose AI assistant on Paul's dashboard, "
    "sitting alongside (not replacing) his main assistant Jarvis. Be helpful, "
    "direct and concise. Paul has dyslexia: read for meaning, never comment "
    "on spelling."
)


class GeminiError(RuntimeError):
    pass


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-flash-latest",   # the chat box's model; env-tunable, generate() takes its own
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 90.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def generate(
        self, model: str, system: str, prompt: str, max_tokens: int = 800,
    ) -> dict[str, Any]:
        """Returns {'text': str, 'refused': bool, 'model_missing': bool} —
        same contract as OpenAIClient.chat, so the War Room treats every
        seat's failure modes identically regardless of vendor."""
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        response = await self._client.post(
            f"{API_BASE}/models/{model}:generateContent",
            params={"key": self._api_key},
            json=payload,
        )
        if response.status_code == 404:
            return {"text": "", "refused": False, "model_missing": True, "usage": {"input": 0, "output": 0}}
        if response.status_code >= 400:
            raise GeminiError(f"Gemini {response.status_code}: {response.text[:300]}")
        data = response.json()
        usage_raw = data.get("usageMetadata") or {}
        usage = {
            "input": int(usage_raw.get("promptTokenCount", 0)),
            "output": int(usage_raw.get("candidatesTokenCount", 0)),
        }
        block_reason = (data.get("promptFeedback") or {}).get("blockReason", "")
        candidates = data.get("candidates") or []
        if not candidates:
            return {"text": "", "refused": bool(block_reason), "model_missing": False, "usage": usage}
        candidate = candidates[0]
        finish = str(candidate.get("finishReason", ""))
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        refused = finish in REFUSAL_REASONS or (not text and bool(block_reason))
        return {"text": text, "refused": refused, "model_missing": False, "usage": usage}

    async def chat(self, messages: list[dict]) -> str:
        """One stateless cockpit-box exchange. `messages` is the whole
        conversation so far: [{"role": "user"|"model", "text": ...}, ...].
        Returns Gemini's reply text; raises so the endpoint can be honest."""
        contents = [
            {"role": m["role"], "parts": [{"text": str(m.get("text", ""))[:8000]}]}
            for m in messages[-20:]   # bound the payload; the box is a chat, not an archive
            if m.get("role") in ("user", "model") and str(m.get("text", "")).strip()
        ]
        response = await self._client.post(
            f"{API_BASE}/models/{self.model}:generateContent",
            params={"key": self._api_key},
            json={
                "contents": contents,
                "systemInstruction": {"parts": [{"text": CHAT_SYSTEM}]},
                "generationConfig": {"maxOutputTokens": 2048},
            },
        )
        if response.status_code != 200:
            logger.warning(
                "Gemini chat refused: %s %s", response.status_code, response.text[:300]
            )
            raise GeminiError(f"Gemini returned {response.status_code}")
        data = response.json()
        parts = (
            (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
        )
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise GeminiError("Gemini sent an empty reply")
        return text
