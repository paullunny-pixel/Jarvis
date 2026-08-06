"""Meetings Layer B (build order §6): Otter's post-meeting emails become
Brain Dump actions + a remembered meeting + a Telegram note. No Otter API
key exists yet, so this rides the mail accounts already wired for Phase 2 —
fake IMAP throughout, Claude mocked at the HTTP layer like every other
client."""
import json
import os
import tempfile
import unittest
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.telegram_client import IncomingMessage
from app.core.store import SettingsStore
from app.db.sqlite import SqliteDatabase
from app.mail.client import MailAccount, MailClient
from app.mail.service import MailService
from app.meetings import MeetingMaker
from app.meetings_notetaker import MeetingNotetaker
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder
from app.memory.store import MemoryStore
from tests.test_mail import FakeIMAP, raw_email

TZ = ZoneInfo("Europe/London")

PARSED = {
    "title": "Derma sync",
    "company": "Derma",
    "attendees": ["Harry"],
    "summary": "Discussed Q3 pipeline and moved to weekly cadence.",
    "decisions": ["Move to weekly cadence"],
    "actions": [{"title": "Send Harry the pipeline doc", "owner": "Harry", "due": "Friday"}],
}

EMPTY_PARSED = {"title": "", "actions": []}


class FakeLayer:
    """Stands in for TrelloLayer — records every create_card_full call."""

    owner_tz = TZ

    def __init__(self):
        self.calls = []

    async def create_card_full(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": f"card{len(self.calls)}"}


def claude_returning(payload: dict, requests: list | None = None) -> ClaudeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(payload)}]})

    return ClaudeClient("K", transport=httpx.MockTransport(handler))


class NotetakerHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.memory = MemoryStore(self.db, HashEmbedder(), PrivateBox("k"))
        self.store = SettingsStore(self.db)
        self.sent = []
        self.layer = FakeLayer()

        async def send_text(text: str) -> None:
            self.sent.append(text)

        self._send_text = send_text

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def _mail(self, messages: list[bytes]) -> MailService:
        account = MailAccount("paul@gmail.com", "app-pass")
        client = MailClient(account, imap_factory=lambda: FakeIMAP(messages))
        return MailService([client], self.db)

    def _notetaker(self, mail: MailService, claude: ClaudeClient) -> MeetingNotetaker:
        async def layer_factory():
            return self.layer

        return MeetingNotetaker(mail, claude, layer_factory, self.memory, self.store, self._send_text)


class TestNotetaker(NotetakerHarness):
    async def test_files_actions_notifies_and_remembers(self):
        mail = self._mail([
            raw_email("notes@otter.ai", "Meeting summary: Derma sync", "Full notes and a link."),
            raw_email("mate@example.com", "Padel Saturday?", "You in?"),
        ])
        requests = []
        notetaker = self._notetaker(mail, claude_returning(PARSED, requests))

        processed = await notetaker.check_new()

        self.assertEqual(processed, 1)
        self.assertEqual(len(requests), 1)   # only the Otter email reached Claude
        # One action card + one summary card
        self.assertEqual(len(self.layer.calls), 2)
        action_card = self.layer.calls[0]
        self.assertEqual(action_card["title"], "Send Harry the pipeline doc")
        self.assertEqual(action_card["owner_name"], "Harry")
        self.assertEqual(action_card["list_name"], "Brain Dump")
        self.assertIsNotNone(action_card["due_local"])
        self.assertEqual(action_card["due_local"].weekday(), 4)   # Friday
        summary_card = self.layer.calls[1]
        self.assertIn("Meeting summary", summary_card["title"])
        self.assertIn("Derma sync", summary_card["title"])

        self.assertEqual(len(self.sent), 1)
        self.assertIn("Derma sync", self.sent[0])
        self.assertIn("Added 1 action", self.sent[0])

        hits = await self.memory.search("Derma sync pipeline")
        self.assertTrue(any("Derma sync" in h["content"] for h in hits))
        self.assertTrue(any("companies" == h["room"] for h in hits))

    async def test_dedupes_on_second_sweep(self):
        mail = self._mail([raw_email("notes@otter.ai", "Meeting summary: Derma sync", "Notes.")])
        requests = []
        notetaker = self._notetaker(mail, claude_returning(PARSED, requests))

        first = await notetaker.check_new()
        second = await notetaker.check_new()

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(requests), 1)   # never re-extracted the same email

    async def test_non_otter_sender_is_ignored(self):
        mail = self._mail([
            raw_email("mate@example.com", "About otter.ai", "Random mention of otter.ai in the body."),
        ])
        requests = []
        notetaker = self._notetaker(mail, claude_returning(PARSED, requests))

        processed = await notetaker.check_new()

        self.assertEqual(processed, 0)
        self.assertEqual(requests, [])
        self.assertEqual(self.sent, [])

    async def test_disabled_skips_the_whole_sweep(self):
        mail = self._mail([raw_email("notes@otter.ai", "Meeting summary: Derma sync", "Notes.")])
        requests = []
        notetaker = self._notetaker(mail, claude_returning(PARSED, requests))
        await notetaker.set_enabled(False)

        processed = await notetaker.check_new()

        self.assertEqual(processed, 0)
        self.assertEqual(requests, [])

    async def test_junk_email_marked_seen_but_nothing_filed(self):
        mail = self._mail([raw_email("notes@otter.ai", "Your Otter plan renews soon", "Billing notice.")])
        requests = []
        notetaker = self._notetaker(mail, claude_returning(EMPTY_PARSED, requests))

        first = await notetaker.check_new()
        second = await notetaker.check_new()

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(self.layer.calls, [])
        self.assertEqual(len(requests), 1)   # judged once, never reprocessed

    async def test_no_actions_still_notifies_without_cards(self):
        parsed = {**PARSED, "actions": []}
        mail = self._mail([raw_email("notes@otter.ai", "Meeting summary: Derma sync", "Notes.")])
        notetaker = self._notetaker(mail, claude_returning(parsed))

        processed = await notetaker.check_new()

        self.assertEqual(processed, 1)
        self.assertEqual(self.layer.calls, [])
        self.assertIn("No clear actions to file", self.sent[0])


class TestQuickStartConsentLine(unittest.IsolatedAsyncioTestCase):
    async def test_line_appears_when_notetaker_enabled(self):
        class AlwaysOn:
            async def enabled(self):
                return True

        from app.clients.zoom_client import ZoomClient

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "oauth/token" in url:
                return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
            if "/users?" in url or url.endswith("/users"):
                return httpx.Response(200, json={"users": [{"id": "u1", "email": "p@x.com", "role_name": "Owner"}]})
            captured["create"] = json.loads(request.content)
            return httpx.Response(201, json={
                "id": 1, "topic": "t", "join_url": "https://zoom.us/j/1",
                "start_url": "https://zoom.us/s/1", "password": "",
            })

        zoom = ZoomClient("A", "I", "S", transport=httpx.MockTransport(handler))
        maker = MeetingMaker(zoom, notetaker=AlwaysOn())
        from datetime import datetime

        reply = await maker.quick_start("start a new zoom meeting", datetime(2026, 8, 6, 10, 0), "Europe/London")
        self.assertIn("Otter's watching this one too", reply)

    async def test_line_absent_when_no_notetaker(self):
        from app.clients.zoom_client import ZoomClient
        from datetime import datetime

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "oauth/token" in url:
                return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
            if "/users?" in url or url.endswith("/users"):
                return httpx.Response(200, json={"users": [{"id": "u1", "email": "p@x.com", "role_name": "Owner"}]})
            return httpx.Response(201, json={
                "id": 1, "topic": "t", "join_url": "https://zoom.us/j/1",
                "start_url": "https://zoom.us/s/1", "password": "",
            })

        zoom = ZoomClient("A", "I", "S", transport=httpx.MockTransport(handler))
        maker = MeetingMaker(zoom)
        reply = await maker.quick_start("start a new zoom meeting", datetime(2026, 8, 6, 10, 0), "Europe/London")
        self.assertNotIn("Otter", reply)


class TestRhythmToggle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.clients.deepgram_client import DeepgramClient
        from app.clients.elevenlabs_client import ElevenLabsClient
        from app.config import Settings
        from app.core.router import JarvisRouter
        from tests.test_wakeup import OWNER, FakePhone, Harness

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        mock = httpx.MockTransport(lambda r: httpx.Response(500))
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings, db=self.db,
            telegram=self.h.jobs.telegram,
            claude=ClaudeClient("K", transport=mock),
            deepgram=DeepgramClient("K", transport=mock),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=mock),
            heartbeat=self.h.jobs,
        )
        self.msg = IncomingMessage(chat_id=OWNER, message_id=1, from_name="Paul")

        store = SettingsStore(self.db)

        async def send_text(text: str) -> None:
            pass

        self.h.jobs.notetaker = MeetingNotetaker(None, None, None, None, store, send_text)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_toggle_off_and_back_on(self):
        self.assertTrue(await self.h.jobs.notetaker.enabled())

        off = await self.router._dispatch_tool("rhythm", {"meeting_notetaker": False}, self.msg)
        self.assertIn("OFF", off)
        self.assertFalse(await self.h.jobs.notetaker.enabled())

        on = await self.router._dispatch_tool("rhythm", {"meeting_notetaker": True}, self.msg)
        self.assertIn("ON", on)
        self.assertTrue(await self.h.jobs.notetaker.enabled())


if __name__ == "__main__":
    unittest.main()
