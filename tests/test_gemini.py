"""Ask Gemini (8 Aug): the cockpit's second, separate AI chat box. Jarvis's
own brain stays Claude — this client is dashboard furniture, walled off from
memory and private data by construction (it only ever sees the box's own
messages)."""
import json
import unittest

import httpx

from app.clients.gemini_client import CHAT_SYSTEM, GeminiClient, GeminiError


class TestGeminiClient(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler, model="gemini-flash-latest"):
        return GeminiClient("KEY", model=model, transport=httpx.MockTransport(handler))

    async def test_chat_sends_history_and_returns_reply(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["key"] = request.url.params.get("key")   # key rides the URL, Google's convention
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"candidates": [{"content": {
                "parts": [{"text": "The capital of Brazil is Brasília."}]
            }}]})

        client = self._client(handler)
        reply = await client.chat([
            {"role": "user", "text": "hello"},
            {"role": "model", "text": "hi!"},
            {"role": "user", "text": "capital of Brazil?"},
        ])
        self.assertEqual(reply, "The capital of Brazil is Brasília.")
        self.assertIn("/models/gemini-flash-latest:generateContent", seen["path"])
        self.assertEqual(seen["key"], "KEY")
        self.assertEqual(len(seen["body"]["contents"]), 3)
        self.assertEqual(seen["body"]["contents"][2]["parts"][0]["text"], "capital of Brazil?")
        # The system instruction rides along and names the arrangement.
        system_text = seen["body"]["systemInstruction"]["parts"][0]["text"]
        self.assertEqual(system_text, CHAT_SYSTEM)
        self.assertIn("not replacing", system_text)
        await client.close()

    async def test_model_is_swappable_without_code(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"candidates": [{"content": {
                "parts": [{"text": "ok"}]
            }}]})

        client = self._client(handler, model="gemini-3.1-flash")
        await client.chat([{"role": "user", "text": "hi"}])
        self.assertIn("/models/gemini-3.1-flash:generateContent", seen["path"])
        await client.close()

    async def test_junk_roles_and_blanks_are_dropped(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"candidates": [{"content": {
                "parts": [{"text": "ok"}]
            }}]})

        client = self._client(handler)
        await client.chat([
            {"role": "system", "text": "ignore me"},   # not a Gemini role
            {"role": "user", "text": "   "},           # blank
            {"role": "user", "text": "real question"},
        ])
        self.assertEqual(len(seen["body"]["contents"]), 1)
        await client.close()

    async def test_refusal_raises_for_an_honest_endpoint_line(self):
        client = self._client(lambda r: httpx.Response(429, json={"error": "quota"}))
        with self.assertRaises(GeminiError):
            await client.chat([{"role": "user", "text": "hi"}])
        await client.close()

    async def test_empty_reply_raises_too(self):
        client = self._client(lambda r: httpx.Response(200, json={"candidates": []}))
        with self.assertRaises(GeminiError):
            await client.chat([{"role": "user", "text": "hi"}])
        await client.close()

    async def test_the_war_room_seat_contract_still_stands(self):
        # 8 Aug regression guard: adding chat() must never disturb Seat C —
        # generate() keeps its own model arg and refusal-as-data contract.
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/models/gemini-3.1-pro-preview:generateContent", request.url.path)
            return httpx.Response(200, json={"candidates": [{"content": {
                "parts": [{"text": "seat c speaks"}]}, "finishReason": "STOP"}]})

        client = self._client(handler)
        result = await client.generate("gemini-3.1-pro-preview", "sys", "prompt")
        self.assertEqual(result["text"], "seat c speaks")
        self.assertFalse(result["refused"])
        self.assertFalse(result["model_missing"])
        await client.close()

    async def test_configured_flag(self):
        client = GeminiClient("", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        self.assertFalse(client.configured)
        keyed = GeminiClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        self.assertTrue(keyed.configured)
        await client.close()
        await keyed.close()


class TestCockpitPanel(unittest.TestCase):
    def test_the_page_carries_the_gemini_box(self):
        from app.cockpit.page import render_page

        page = render_page("/cockpit/S/data")
        self.assertIn("Ask Gemini", page)
        self.assertIn("/gemini", page)   # the endpoint the box posts to
        # Honest labelling: it's a separate AI with no access to Paul's data.
        self.assertIn("knows nothing about your data", page)


if __name__ == "__main__":
    unittest.main()
