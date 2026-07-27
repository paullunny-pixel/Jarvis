"""Regression: Jarvis must know his own wiring (no confabulated 'not connected'),
'status' runs live checks, and a show request builds the plan instead of the
'no plan yet' brush-off."""
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.daily12.service import Daily12Service
from app.daily12.trello import TrelloClient
from app.db.sqlite import SqliteDatabase
from tests.test_daily12_service import BOARD, CARDS, LISTS, TAGS
from tests.test_telegram_client import text_update

OWNER = 111


class StatusHarness:
    def __init__(self, db, with_trello=True, trello_fails=False, voyage=True):
        self.telegram_calls = []
        self.claude_requests = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.claude_requests.append(payload)
            if "haiku" in payload["model"]:
                return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(TAGS)}]})
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Quite so, sir."}]})

        def trello_handler(request: httpx.Request) -> httpx.Response:
            if trello_fails:
                return httpx.Response(401, text="invalid token")
            path = urlparse(str(request.url)).path
            if path.endswith("/members/me/boards"):
                return httpx.Response(200, json=[BOARD])
            if path.endswith("/lists"):
                return httpx.Response(200, json=LISTS)
            if path.endswith("/cards"):
                return httpx.Response(200, json=CARDS)
            return httpx.Response(200, json={})

        settings = Settings(
            telegram_bot_token="TOK",
            telegram_owner_chat_id=OWNER,
            voyage_api_key="pa-x" if voyage else "",
            _env_file=None,
        )
        claude = ClaudeClient("K", transport=httpx.MockTransport(claude_handler))
        daily12 = None
        if with_trello:
            daily12 = Daily12Service(
                db, TrelloClient("K", "T", transport=httpx.MockTransport(trello_handler)), claude
            )
        self.router = JarvisRouter(
            settings=settings,
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            daily12=daily12,
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class TestStatusAndShow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_confirm_trello_access_runs_live_check(self):
        h = StatusHarness(self.db)
        await h.router.handle_update(text_update("Can you confirm access to my Trello board", OWNER))
        [report] = h.texts()
        self.assertIn("SYSTEM CHECK", report)
        self.assertIn("Trello: connected", report)
        self.assertIn("Master Board", report)
        self.assertEqual(h.claude_requests, [])  # no confabulation possible — code answered

    async def test_status_reports_broken_trello_truthfully(self):
        h = StatusHarness(self.db, trello_fails=True)
        await h.router.handle_update(text_update("status", OWNER))
        [report] = h.texts()
        self.assertIn("live check failed", report)

    async def test_status_without_trello_keys(self):
        h = StatusHarness(self.db, with_trello=False, voyage=False)
        await h.router.handle_update(text_update("system check please", OWNER))
        [report] = h.texts()
        self.assertIn("Trello: not connected", report)
        self.assertIn("local fallback", report)

    async def test_brain_receives_ground_truth_about_integrations(self):
        h = StatusHarness(self.db)
        await h.router.handle_update(text_update("morning, how are we looking?", OWNER))
        system = h.claude_requests[-1]["system"]
        self.assertIn("ground truth about your integrations", system)
        self.assertIn("Trello / Today's Focus: CONNECTED", system)

    async def test_show_request_builds_the_plan_instead_of_brush_off(self):
        h = StatusHarness(self.db)
        await h.router.handle_update(text_update("what's my 12?", OWNER))
        combined = " ".join(h.texts())
        self.assertIn("TODAY'S FOCUS", combined)
        self.assertNotIn("is clear", combined)
        rows = await self.db.fetch_all("SELECT * FROM daily_12 WHERE position != 0")
        self.assertGreater(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
