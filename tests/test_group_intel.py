"""Group intelligence Part 2 (7 Aug build slice): gists, @-mention actions,
the missed-summary, dismiss, and the thin desktop endpoints on top."""
import json
import os
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from app import main as app_main
from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.core.store import SettingsStore
from app.db.sqlite import SqliteDatabase
from app.groups_intel import GIST_SYSTEM, EXTRACTION_SYSTEM, MISSED_SUMMARY_SYSTEM, GroupIntel
from tests.test_wakeup import OWNER, FakePhone, Harness

GENERAL_ACTION = [{
    "ask": "Confirm the BMI order quantity",
    "asked_by": "Kiefer",
    "source_message": "Paul, can you confirm we're doing 500 units on the BMI order?",
    "ts": "2026-08-07T08:45:00+00:00",
}]


def scripted_claude(routes: dict) -> ClaudeClient:
    """routes: {marker_substring_in_system_prompt: response_text}."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        system = body.get("system", "")
        for marker, text in routes.items():
            if marker in system:
                return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": ""}]})

    client = ClaudeClient("K", transport=httpx.MockTransport(handler))
    client.calls = calls   # type: ignore[attr-defined]
    return client


class FakeLayer:
    owner_tz = None

    def __init__(self):
        self.calls = []

    async def create_card_full(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": f"card{len(self.calls)}"}


class GroupIntelBase(unittest.IsolatedAsyncioTestCase):
    # Extraction defaults to empty so tests that only care about tagging or
    # gists aren't contaminated by a phantom general-extractor action —
    # tests that specifically exercise extraction supply their own routes.
    claude_routes = {
        GIST_SYSTEM[:30]: "Chasing the BMI order.",
        EXTRACTION_SYSTEM[:30]: "[]",
        MISSED_SUMMARY_SYSTEM[:30]: "Derma UK: Kiefer needs the BMI order quantity confirmed.",
    }

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.store = SettingsStore(self.db)
        self.claude = scripted_claude(self.claude_routes)
        self.layer = FakeLayer()

        async def layer_factory():
            return self.layer

        self.gi = GroupIntel(self.db, self.claude, self.store, layer_factory=layer_factory)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _ingest(self, chat_id, chat_title, sender, message, ts, company="derma_uk"):
        await self.db.execute(
            "INSERT INTO telegram_ingest (ts, chat_id, chat_title, company_tag, sender,"
            " sender_id, kind, message) VALUES (?, ?, ?, ?, ?, 0, 'text', ?)",
            (ts, chat_id, chat_title, company, sender, message),
        )


class TestStatus(GroupIntelBase):
    async def test_not_connected_until_something_has_ever_ingested(self):
        self.assertEqual(await self.gi.status(), "not_connected")
        await self._ingest(-1, "Derma UK", "Kiefer", "hi", "2026-08-07T08:00:00+00:00")
        self.assertEqual(await self.gi.status(), "connected")


class TestRefresh(GroupIntelBase):
    async def test_tagged_mention_auto_promotes_without_judgment(self):
        await self._ingest(
            -1, "Derma UK", "Adriana",
            "@Paul can you sign off the new packaging artwork today?",
            "2026-08-07T08:55:00+00:00",
        )
        n = await self.gi.refresh()
        self.assertEqual(n, 1)
        actions = await self.gi.open_actions()
        tagged = [a for a in actions if a["tagged"]]
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["asked_by"], "Adriana")
        self.assertIn("@Paul", tagged[0]["source_message"])

    async def test_general_extraction_files_a_non_tagged_action(self):
        routes = dict(self.claude_routes)
        routes[EXTRACTION_SYSTEM[:30]] = json.dumps(GENERAL_ACTION)
        self.claude = scripted_claude(routes)

        async def layer_factory():
            return self.layer

        self.gi = GroupIntel(self.db, self.claude, self.store, layer_factory=layer_factory)
        await self._ingest(
            -1, "Derma UK", "Kiefer",
            "Paul, can you confirm we're doing 500 units on the BMI order?",
            "2026-08-07T08:45:00+00:00",
        )
        await self.gi.refresh()
        actions = await self.gi.open_actions()
        self.assertEqual(len(actions), 1)
        self.assertFalse(actions[0]["tagged"])
        self.assertEqual(actions[0]["asked_by"], "Kiefer")

    async def test_general_extractor_deduped_against_a_tagged_copy(self):
        text = "@Paul, can you confirm we're doing 500 units on the BMI order?"
        routes = dict(self.claude_routes)
        routes[EXTRACTION_SYSTEM[:30]] = json.dumps([{
            "ask": "Confirm the BMI order quantity", "asked_by": "Kiefer",
            "source_message": text, "ts": "2026-08-07T08:45:00+00:00",
        }])
        self.claude = scripted_claude(routes)

        async def layer_factory():
            return self.layer

        self.gi = GroupIntel(self.db, self.claude, self.store, layer_factory=layer_factory)
        await self._ingest(-1, "Derma UK", "Kiefer", text, "2026-08-07T08:45:00+00:00")
        await self.gi.refresh()
        actions = await self.gi.open_actions()
        self.assertEqual(len(actions), 1)          # not two — tagged wins, no duplicate
        self.assertTrue(actions[0]["tagged"])

    async def test_gist_and_message_count_land_in_group_summaries(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "morning all", "2026-08-07T08:00:00+00:00")
        await self.gi.refresh()
        groups = await self.gi.group_summaries()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["chat_title"], "Derma UK")
        self.assertIn("BMI order", groups[0]["gist"])
        self.assertEqual(groups[0]["message_count"], 1)

    async def test_watermark_advances_so_a_second_refresh_is_a_no_op(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "hi", "2026-08-07T08:00:00+00:00")
        first = await self.gi.refresh()
        n_calls = len(self.claude.calls)
        second = await self.gi.refresh()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(self.claude.calls), n_calls)   # no new Claude calls


class TestActionActions(GroupIntelBase):
    async def test_file_to_trello_updates_status_and_creates_a_card(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "@Paul sort the invoice please", "2026-08-07T08:00:00+00:00")
        await self.gi.refresh()
        [action] = await self.gi.open_actions()
        msg = await self.gi.action_to_trello(action["id"])
        self.assertIn("Filed to Brain Dump", msg)
        self.assertEqual(len(self.layer.calls), 1)
        self.assertEqual(self.layer.calls[0]["list_name"], "Brain Dump")
        self.assertEqual(await self.gi.open_actions(), [])

        # Second attempt on the same (now non-open) action is a no-op, not a duplicate card.
        again = await self.gi.action_to_trello(action["id"])
        self.assertIn("already", again)
        self.assertEqual(len(self.layer.calls), 1)

    async def test_ignore_closes_without_touching_trello(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "@Paul random FYI", "2026-08-07T08:00:00+00:00")
        await self.gi.refresh()
        [action] = await self.gi.open_actions()
        msg = await self.gi.action_ignore(action["id"])
        self.assertEqual(msg, "Ignored.")
        self.assertEqual(await self.gi.open_actions(), [])
        self.assertEqual(self.layer.calls, [])

    async def test_missing_action_id_is_handled_honestly(self):
        self.assertIn("doesn't exist", await self.gi.action_to_trello(9999))
        self.assertIn("doesn't exist", await self.gi.action_ignore(9999))


class TestMissedSummaryAndDismiss(GroupIntelBase):
    async def test_missed_summary_regenerates_on_refresh(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "hi", "2026-08-07T08:00:00+00:00")
        await self.gi.refresh()
        summary = await self.gi.missed_summary()
        self.assertIn("BMI order", summary["text"])

    async def test_dismiss_clears_summary_and_counts_but_not_actions(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "@Paul please confirm", "2026-08-07T08:00:00+00:00")
        await self.gi.refresh()
        actions_before = await self.gi.open_actions()
        self.assertEqual(len(actions_before), 1)

        await self.gi.dismiss_summary()

        summary = await self.gi.missed_summary()
        self.assertEqual(summary["text"], "")
        groups = await self.gi.group_summaries()
        self.assertEqual(groups[0]["message_count"], 0)   # count resets; the gist itself stays
        # The easy bug to ship: actions must survive a dismiss untouched.
        actions_after = await self.gi.open_actions()
        self.assertEqual(len(actions_after), 1)
        self.assertEqual(actions_after[0]["id"], actions_before[0]["id"])

    async def test_dismissed_summary_stays_retrievable(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "hi", "2026-08-07T08:00:00+00:00")
        await self.gi.refresh()
        await self.gi.dismiss_summary()
        history = await self.gi.dismissed_history()
        self.assertEqual(len(history), 1)
        self.assertIn("BMI order", history[0]["text"])

    async def test_uncleared_count_reflects_open_actions_only(self):
        await self._ingest(-1, "Derma UK", "Kiefer", "@Paul one", "2026-08-07T08:00:00+00:00")
        await self._ingest(-1, "Derma UK", "Adriana", "@Paul two", "2026-08-07T08:05:00+00:00")
        await self.gi.refresh()
        self.assertEqual(await self.gi.uncleared_count(), 2)
        [a, _] = await self.gi.open_actions()
        await self.gi.action_ignore(a["id"])
        self.assertEqual(await self.gi.uncleared_count(), 1)


class TestFixtures(unittest.TestCase):
    def test_fixtures_carry_all_four_sample_shapes(self):
        fx = GroupIntel.fixtures()
        self.assertIn("group_summaries", fx)
        self.assertIn("open_actions", fx)
        self.assertIn("missed_summary", fx)
        self.assertTrue(any(a["tagged"] for a in fx["open_actions"]))
        self.assertTrue(any(not a["tagged"] for a in fx["open_actions"]))


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
        self.layer = FakeLayer()

        async def layer_factory():
            return self.layer

        self.claude = scripted_claude(GroupIntelBase.claude_routes)
        self.router.group_intel = GroupIntel(self.db, self.claude, self.store, layer_factory=layer_factory)
        self.secret = settings.effective_desktop_secret
        app_main.app.state.router = self.router
        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        del app_main.app.state.router
        await self.db.close()
        self._dir.cleanup()

    async def _ingest(self, chat_id, chat_title, sender, message, ts):
        await self.db.execute(
            "INSERT INTO telegram_ingest (ts, chat_id, chat_title, company_tag, sender,"
            " sender_id, kind, message) VALUES (?, ?, ?, '', ?, 0, 'text', ?)",
            (ts, chat_id, chat_title, sender, message),
        )


class TestEndpoints(EndpointsBase):
    def test_wrong_secret_403s(self):
        for url in (
            "/desktop/WRONG/groups/summaries",
            "/desktop/WRONG/groups/actions",
            "/desktop/WRONG/groups/missed-summary",
            "/desktop/WRONG/groups/uncleared-count",
            "/desktop/WRONG/groups/fixtures",
        ):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_not_connected_is_explicit_not_empty(self):
        for path in ("summaries", "actions", "missed-summary"):
            response = self.client.get(f"/desktop/{self.secret}/groups/{path}")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["connected"])
        count = self.client.get(f"/desktop/{self.secret}/groups/uncleared-count")
        self.assertFalse(count.json()["connected"])
        self.assertEqual(count.json()["count"], 0)

    async def test_connected_data_flows_through_after_a_refresh(self):
        await self._ingest(-1, "Derma UK", "Adriana", "@Paul confirm please", "2026-08-07T08:00:00+00:00")
        await self.router.group_intel.refresh()

        summaries = self.client.get(f"/desktop/{self.secret}/groups/summaries").json()
        self.assertTrue(summaries["connected"])
        self.assertEqual(len(summaries["groups"]), 1)

        actions = self.client.get(f"/desktop/{self.secret}/groups/actions").json()
        self.assertTrue(actions["connected"])
        self.assertEqual(len(actions["actions"]), 1)
        action_id = actions["actions"][0]["id"]

        missed = self.client.get(f"/desktop/{self.secret}/groups/missed-summary").json()
        self.assertTrue(missed["connected"])
        self.assertIn("BMI order", missed["text"])

        uncleared = self.client.get(f"/desktop/{self.secret}/groups/uncleared-count").json()
        self.assertEqual(uncleared["count"], 1)

        trello = self.client.post(f"/desktop/{self.secret}/groups/actions/{action_id}/trello")
        self.assertTrue(trello.json()["ok"])
        self.assertEqual(len(self.layer.calls), 1)

        dismiss = self.client.post(f"/desktop/{self.secret}/groups/dismiss-summary")
        self.assertTrue(dismiss.json()["ok"])
        missed_after = self.client.get(f"/desktop/{self.secret}/groups/missed-summary").json()
        self.assertEqual(missed_after["text"], "")

    async def test_ignore_endpoint(self):
        await self._ingest(-1, "Derma UK", "Adriana", "@Paul confirm please", "2026-08-07T08:00:00+00:00")
        await self.router.group_intel.refresh()
        actions = self.client.get(f"/desktop/{self.secret}/groups/actions").json()["actions"]
        action_id = actions[0]["id"]
        ignore = self.client.post(f"/desktop/{self.secret}/groups/actions/{action_id}/ignore")
        self.assertTrue(ignore.json()["ok"])
        left = self.client.get(f"/desktop/{self.secret}/groups/actions").json()["actions"]
        self.assertEqual(left, [])

    def test_fixtures_endpoint(self):
        fx = self.client.get(f"/desktop/{self.secret}/groups/fixtures").json()
        self.assertIn("group_summaries", fx)
        self.assertIn("open_actions", fx)
        self.assertIn("missed_summary", fx)


if __name__ == "__main__":
    unittest.main()
