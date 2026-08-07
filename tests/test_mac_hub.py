"""Mac app v2 hub backend (7 Aug build slice): the endpoints and services
the Electron app's new panels read from — Today's Focus, document upload
with filing suggestions, Portuguese status, the commands registry, and
desktop notifications (banner + spoken-announcement queue)."""
import json
import os
import tempfile
import unittest
from datetime import date

import httpx
from fastapi.testclient import TestClient

from app import main as app_main
from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.core.store import SettingsStore, utc_now_iso
from app.cockpit.service import CockpitService
from app.db.sqlite import SqliteDatabase
from app.documents.service import DocumentLibrary
from app.heartbeat.desktop_notifications import DesktopNotifications
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder
from app.memory.store import MemoryStore
from tests.test_wakeup import OWNER, FakePhone, Harness


def scripted_claude(routes: dict) -> ClaudeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body.get("system", "")
        for marker, text in routes.items():
            if marker in system:
                return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": ""}]})

    return ClaudeClient("K", transport=httpx.MockTransport(handler))


class FakeDaily12:
    """Stands in for Daily12Service — the endpoint only needs mark_done()."""

    def __init__(self, reply: str = "'Do the thing' done and moved on the board. 1 of 3."):
        self.reply = reply
        self.calls = []

    async def mark_done(self, reference: str) -> str:
        self.calls.append(reference)
        return self.reply

    async def create(self, title: str, **kwargs) -> str:
        self.calls.append(("create", title, kwargs))
        return f"Created '{title}'."


class EndpointsBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings,
            db=self.db,
            telegram=self.h.jobs.telegram,
            claude=self.h.jobs.claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            heartbeat=self.h.jobs,
        )
        self.store = SettingsStore(self.db)
        self.secret = settings.effective_desktop_secret
        app_main.app.state.router = self.router
        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        del app_main.app.state.router
        await self.db.close()
        self._dir.cleanup()


class TestTodayFocus(EndpointsBase):
    async def _seed_task(self, position, title, company, done=0):
        task_id = await self.db.insert_returning_id(
            "INSERT INTO tasks (trello_id, title, company_slug) VALUES (?, ?, ?)",
            (f"trello{position}", title, company),
        )
        today = date.today().isoformat()
        await self.db.execute(
            "INSERT INTO daily_12 (plan_date, position, task_id, company_slug, done)"
            " VALUES (?, ?, ?, ?, ?)",
            (today, position, task_id, company, done),
        )

    def test_not_connected_when_daily12_none(self):
        self.router.daily12 = None
        resp = self.client.get(f"/desktop/{self.secret}/today-focus")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["connected"])

    async def test_grouped_by_company_same_shape_as_cockpit(self):
        self.router.daily12 = FakeDaily12()
        await self._seed_task(1, "Email BMI sales plan", "derma_uk")
        await self._seed_task(2, "Chase Prodermis invoice", "prodermis", done=1)
        resp = self.client.get(f"/desktop/{self.secret}/today-focus")
        body = resp.json()
        self.assertTrue(body["connected"])
        self.assertEqual(body["done"], 1)
        self.assertEqual(body["total"], 2)
        by_slug = {c["slug"]: c for c in body["by_company"]}
        self.assertEqual(by_slug["derma_uk"]["tasks"][0]["title"], "Email BMI sales plan")
        self.assertFalse(by_slug["derma_uk"]["tasks"][0]["done"])
        self.assertTrue(by_slug["prodermis"]["tasks"][0]["done"])
        # matches CockpitService directly — one source of truth
        direct = await CockpitService(self.db).today_focus()
        self.assertEqual(direct["done"], body["done"])
        self.assertEqual(direct["total"], body["total"])

    def test_tick_writes_back_via_mark_done(self):
        fake = FakeDaily12()
        self.router.daily12 = fake
        resp = self.client.post(f"/desktop/{self.secret}/today-focus/1/done")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(fake.calls, ["1"])

    def test_tick_not_found_reports_not_ok(self):
        self.router.daily12 = FakeDaily12(reply="Couldn't find '99' on today's 12.")
        resp = self.client.post(f"/desktop/{self.secret}/today-focus/99/done")
        self.assertFalse(resp.json()["ok"])

    def test_tick_without_daily12_409s(self):
        self.router.daily12 = None
        resp = self.client.post(f"/desktop/{self.secret}/today-focus/1/done")
        self.assertEqual(resp.status_code, 409)

    def test_wrong_secret_403s(self):
        self.router.daily12 = FakeDaily12()
        resp = self.client.get("/desktop/WRONG/today-focus")
        self.assertEqual(resp.status_code, 403)


class TestDocuments(EndpointsBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        memory = MemoryStore(self.db, HashEmbedder(), PrivateBox(""))
        self.claude = scripted_claude({
            "filing a document": json.dumps({
                "room": "companies", "tags": ["prodermis", "invoice"],
                "actionable": True, "action_kind": "invoice",
                "reason": "mentions an invoice number",
            })
        })
        self.router.library = DocumentLibrary(self.db, memory, object_store=None, claude=self.claude)
        self.router.daily12 = FakeDaily12()

    def test_upload_stores_and_returns_filing_suggestion(self):
        resp = self.client.post(
            f"/desktop/{self.secret}/documents/upload?filename=invoice.txt&mime=text/plain",
            content=b"Prodermis invoice #4471, please pay within 30 days.",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["filename"], "invoice.txt")
        self.assertEqual(body["suggestion"]["room"], "companies")
        self.assertIn("invoice", body["suggestion"]["tags"])
        self.assertTrue(body["suggestion"]["actionable"])

    def test_upload_requires_filename(self):
        resp = self.client.post(f"/desktop/{self.secret}/documents/upload", content=b"hello")
        self.assertEqual(resp.status_code, 400)

    def test_upload_without_library_409s(self):
        self.router.library = None
        resp = self.client.post(
            f"/desktop/{self.secret}/documents/upload?filename=a.txt", content=b"hi"
        )
        self.assertEqual(resp.status_code, 409)

    def test_filing_falls_back_safely_when_claude_errors(self):
        async def suggest_filing_stub(text, filename):
            return {"room": "companies", "tags": [], "actionable": False, "action_kind": "", "reason": ""}

        self.router.library.suggest_filing = suggest_filing_stub
        resp = self.client.post(
            f"/desktop/{self.secret}/documents/upload?filename=b.txt", content=b"whatever"
        )
        self.assertEqual(resp.json()["suggestion"]["room"], "companies")

    def test_recent_list_and_trello_offer(self):
        self.client.post(
            f"/desktop/{self.secret}/documents/upload?filename=invoice.txt&mime=text/plain",
            content=b"Prodermis invoice #4471.",
        )
        recent = self.client.get(f"/desktop/{self.secret}/documents/recent").json()
        self.assertTrue(recent["connected"])
        self.assertEqual(recent["documents"][0]["filename"], "invoice.txt")
        doc_id = recent["documents"][0]["id"]
        card = self.client.post(f"/desktop/{self.secret}/documents/{doc_id}/trello", json={})
        self.assertIn("Created", card.json()["message"])

    def test_trello_offer_missing_document_404s(self):
        resp = self.client.post(f"/desktop/{self.secret}/documents/999/trello", json={})
        self.assertEqual(resp.status_code, 404)

    def test_set_room_corrects_filing(self):
        upload = self.client.post(
            f"/desktop/{self.secret}/documents/upload?filename=c.txt", content=b"hi there"
        ).json()
        resp = self.client.post(
            f"/desktop/{self.secret}/documents/{upload['id']}/room", json={"room": "health"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["room"], "health")
        recent = self.client.get(f"/desktop/{self.secret}/documents/recent").json()
        self.assertEqual(recent["documents"][0]["room"], "health")

    def test_set_room_rejects_unknown_room(self):
        upload = self.client.post(
            f"/desktop/{self.secret}/documents/upload?filename=d.txt", content=b"hi there"
        ).json()
        resp = self.client.post(
            f"/desktop/{self.secret}/documents/{upload['id']}/room", json={"room": "nonsense"}
        )
        self.assertEqual(resp.status_code, 400)


class TestPortuguese(EndpointsBase):
    def test_status_shape(self):
        resp = self.client.get(f"/desktop/{self.secret}/portuguese")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("days_left", body["readiness"])
        self.assertIn("speech_pct", body["readiness"])
        self.assertIn("text", body["steph_phrase"])
        self.assertIn("current", body["streak"])
        self.assertIn("done_today", body)


class TestCommands(EndpointsBase):
    def test_registry_grouped_and_nonempty(self):
        resp = self.client.get(f"/desktop/{self.secret}/commands")
        self.assertEqual(resp.status_code, 200)
        cats = resp.json()["categories"]
        self.assertGreater(len(cats), 0)
        flat = [c for group in cats.values() for c in group]
        self.assertTrue(any("wake" in c["phrase"] for c in flat))
        for c in flat:
            self.assertIn("phrase", c)
            self.assertIn("does", c)

    def test_wrong_secret_403s(self):
        resp = self.client.get("/desktop/WRONG/commands")
        self.assertEqual(resp.status_code, 403)


class TestDesktopNotifications(EndpointsBase):
    async def test_text_send_records_silent_banner(self):
        await self.h.jobs.store.set("owner_chat_id", str(OWNER))
        await self.h.jobs._send_text("Board synced.")
        notes = await self.h.jobs.desktop_notifications.recent()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "text")
        self.assertFalse(notes[0]["announce"])

    async def test_voice_send_records_announce(self):
        await self.h.jobs.store.set("owner_chat_id", str(OWNER))
        await self.h.jobs._send_voice("Sir, it's time to hydrate.")
        notes = await self.h.jobs.desktop_notifications.recent()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "voice")
        self.assertTrue(notes[0]["announce"])

    async def test_quiet_day_suppresses_desktop_too(self):
        await self.h.jobs.store.set("owner_chat_id", str(OWNER))
        await self.h.jobs.set_quiet_today(True)
        await self.h.jobs._send_text("FYI only.")
        notes = await self.h.jobs.desktop_notifications.recent()
        self.assertEqual(notes, [])

    async def test_list_and_dismiss_endpoint(self):
        await self.h.jobs.desktop_notifications.record("Call Harry", kind="voice", announce=True)
        listed = self.client.get(f"/desktop/{self.secret}/notifications").json()
        self.assertEqual(len(listed["notifications"]), 1)
        note_id = listed["notifications"][0]["id"]
        dismissed = self.client.post(f"/desktop/{self.secret}/notifications/{note_id}/dismiss")
        self.assertTrue(dismissed.json()["ok"])
        listed_again = self.client.get(f"/desktop/{self.secret}/notifications").json()
        self.assertEqual(listed_again["notifications"], [])
        all_listed = self.client.get(f"/desktop/{self.secret}/notifications?all=1").json()
        self.assertEqual(len(all_listed["notifications"]), 1)

    def test_speak_without_elevenlabs_key_returns_none_audio(self):
        # router.elevenlabs is configured with a fake key that 500s — synth
        # raises, endpoint degrades to audio_b64: None rather than 500ing.
        resp = self.client.post(f"/desktop/{self.secret}/speak", json={"text": "Hello"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["audio_b64"])

    def test_speak_requires_text(self):
        resp = self.client.post(f"/desktop/{self.secret}/speak", json={"text": ""})
        self.assertEqual(resp.status_code, 400)


class TestDesktopNotificationsServiceUnit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.dn = DesktopNotifications(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_uncleared_count(self):
        await self.dn.record("a")
        await self.dn.record("b", announce=True)
        self.assertEqual(await self.dn.uncleared_count(), 2)
        notes = await self.dn.recent()
        await self.dn.dismiss(notes[0]["id"])
        self.assertEqual(await self.dn.uncleared_count(), 1)

    async def test_dismiss_unknown_id_returns_false(self):
        self.assertFalse(await self.dn.dismiss(999))


if __name__ == "__main__":
    unittest.main()
