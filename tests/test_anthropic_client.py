import json
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient, ClaudeError


def ok_response(text: str, stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "content": [{"type": "text", "text": text}],
            "model": "claude-opus-5",
            "stop_reason": stop_reason,
        },
    )


class TestClaude(unittest.IsolatedAsyncioTestCase):
    async def test_converse_payload_and_extraction(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["key"] = request.headers.get("x-api-key")
            captured["version"] = request.headers.get("anthropic-version")
            captured["payload"] = json.loads(request.content)
            return ok_response("Right. One step: send the first survey.")

        client = ClaudeClient("SKKEY", transport=httpx.MockTransport(handler))
        reply = await client.converse("SYSTEM PROMPT", [{"role": "user", "content": "help"}])
        self.assertEqual(reply, "Right. One step: send the first survey.")
        self.assertEqual(captured["key"], "SKKEY")
        self.assertEqual(captured["payload"]["model"], "claude-opus-5")
        self.assertEqual(captured["payload"]["system"], "SYSTEM PROMPT")
        self.assertEqual(captured["payload"]["messages"][0]["content"], "help")

    async def test_quick_uses_fast_model(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["payload"] = json.loads(request.content)
            return ok_response("classification: task_update")

        client = ClaudeClient("SKKEY", transport=httpx.MockTransport(handler))
        out = await client.quick("classify this")
        self.assertEqual(out, "classification: task_update")
        self.assertEqual(captured["payload"]["model"], "claude-haiku-4-5")

    async def test_retries_on_overload_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(529, text="overloaded")
            return ok_response("second time lucky")

        client = ClaudeClient("SKKEY", transport=httpx.MockTransport(handler))
        client.RATE_WAIT = 0.01  # keep the suite fast
        reply = await client.converse("s", [{"role": "user", "content": "x"}])
        self.assertEqual(reply, "second time lucky")
        self.assertEqual(calls["n"], 2)

    async def test_rate_limit_gets_the_long_wait_not_the_quick_one(self):
        # The BMI research failure: three attempts inside 5 seconds against a
        # 429 all die. Rate limits must wait on the provider's clock.
        import asyncio as _asyncio
        from unittest.mock import patch

        client = ClaudeClient(
            "SKKEY", transport=httpx.MockTransport(lambda r: httpx.Response(429, text="rate"))
        )
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch.object(_asyncio, "sleep", fake_sleep):
            with self.assertRaises(ClaudeError):
                await client.converse("s", [{"role": "user", "content": "x"}])
        self.assertEqual(sleeps, [20.0, 40.0])

    async def test_per_request_timeout_rides_the_wire(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["timeout"] = request.extensions.get("timeout")
            return ok_response("ok")

        client = ClaudeClient("SKKEY", transport=httpx.MockTransport(handler))
        await client.converse("s", [{"role": "user", "content": "x"}], timeout=300.0)
        self.assertEqual(seen["timeout"]["read"], 300.0)

    async def test_a_cut_reply_finishes_itself(self):
        # 6 Aug: a long health answer hit max_tokens and shipped ending at
        # "Worth noting: 5.5 hours' sleep," — mid-sentence. The client must
        # notice the max_tokens stop and continue the generation.
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            calls.append(payload)
            if len(calls) == 1:
                return ok_response("Worth noting: 5.5 hours' sleep, ", "max_tokens")
            return ok_response("that's thin for a 166 pulse. Bank an early night.")

        client = ClaudeClient("SKKEY", transport=httpx.MockTransport(handler))
        reply = await client.converse("s", [{"role": "user", "content": "my Elvanse"}])
        self.assertEqual(
            reply,
            "Worth noting: 5.5 hours' sleep, that's thin for a 166 pulse. Bank an early night.",
        )
        # The continuation prefilled the partial back as an assistant turn,
        # rstripped (the API refuses trailing whitespace on a prefill).
        prefill = calls[1]["messages"][-1]
        self.assertEqual(prefill["role"], "assistant")
        self.assertEqual(prefill["content"], "Worth noting: 5.5 hours' sleep,")

    async def test_continuation_is_bounded(self):
        # A model that never stops must not loop the client forever.
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return ok_response("more words ", "max_tokens")

        client = ClaudeClient("SKKEY", transport=httpx.MockTransport(handler))
        reply = await client.converse("s", [{"role": "user", "content": "x"}])
        self.assertEqual(calls["n"], 3)   # first call + two continuations, then stop
        self.assertEqual(reply, "more words more words more words")

    async def test_tool_loop_final_text_also_continues(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            calls.append(payload)
            if len(calls) == 1:
                return ok_response("Half an answer that ran out of ro", "max_tokens")
            return ok_response("om — and here is the rest.")

        client = ClaudeClient("SKKEY", transport=httpx.MockTransport(handler))
        reply = await client.converse_with_tools(
            "s", [{"role": "user", "content": "x"}],
            tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
            handler=None,   # never called — no tool_use blocks come back
        )
        self.assertEqual(reply, "Half an answer that ran out of room — and here is the rest.")

    async def test_gives_up_after_retries(self):
        client = ClaudeClient(
            "SKKEY", transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
        )
        with self.assertRaises(ClaudeError):
            await client.converse("s", [{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
