import json
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient, ClaudeError


def ok_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "content": [{"type": "text", "text": text}],
            "model": "claude-opus-5",
            "stop_reason": "end_turn",
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
        reply = await client.converse("s", [{"role": "user", "content": "x"}])
        self.assertEqual(reply, "second time lucky")
        self.assertEqual(calls["n"], 2)

    async def test_gives_up_after_retries(self):
        client = ClaudeClient(
            "SKKEY", transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
        )
        with self.assertRaises(ClaudeError):
            await client.converse("s", [{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
