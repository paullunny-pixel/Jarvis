"""Team Radar (Build Slice, 7 Aug): the four card states, the 'taking too
long' signals, coverage honesty, the daily line, the interrupt, and the
hard 'nothing reaches Harry/Adriana/Kiefer' rule. No real network — a
duck-typed fake Trello client that RAISES on any write call, so the
nothing-outbound rule is enforced on the wire, not on intent."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.core.store import SettingsStore
from app.db.sqlite import SqliteDatabase
from app.radar import states
from app.radar.service import DAILY_KEY, TeamRadar
from app.radar.sync import RadarSync


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


class WriteAttempted(AssertionError):
    pass


class FakeTrelloClient:
    """Duck-types just the read methods RadarSync calls; every write-shaped
    method raises so the nothing-outbound / read-only rules fail LOUDLY
    if this module ever tries to touch Trello."""

    def __init__(self, boards, lists, members, custom_fields, cards):
        self._boards = boards
        self._lists = lists
        self._members = members
        self._custom_fields = custom_fields
        self._cards = cards

    async def my_boards(self):
        return self._boards

    async def board_lists(self, board_id):
        return self._lists.get(board_id, [])

    async def board_members(self, board_id):
        return self._members.get(board_id, [])

    async def board_custom_fields(self, board_id):
        return self._custom_fields.get(board_id, [])

    async def board_cards_full(self, board_id):
        return self._cards.get(board_id, [])

    async def comment(self, *a, **kw):
        raise WriteAttempted("comment")

    async def update_card(self, *a, **kw):
        raise WriteAttempted("update_card")

    async def move_card(self, *a, **kw):
        raise WriteAttempted("move_card")

    async def assign_member(self, *a, **kw):
        raise WriteAttempted("assign_member")

    async def archive_card(self, *a, **kw):
        raise WriteAttempted("archive_card")

    async def create_card(self, *a, **kw):
        raise WriteAttempted("create_card")

    async def add_label(self, *a, **kw):
        raise WriteAttempted("add_label")


MASTER = "board1"
DOMAIN_OPTIONS = [
    {"id": "opt-prodermis", "value": {"text": "Prodermis"}},
    {"id": "opt-derma", "value": {"text": "Derma"}},
]

FIXTURE = dict(
    boards=[{"id": MASTER, "name": "Master Board"}],
    lists={MASTER: [
        {"id": "L1", "name": "Paul Today"},
        {"id": "L2", "name": "This Week"},
        {"id": "L3", "name": "Backlog"},
    ]},
    members={MASTER: [
        {"id": "M1", "fullName": "Kiefer Brindle"},
        {"id": "M2", "fullName": "Paul Lunny"},
    ]},
    custom_fields={MASTER: [{"name": "Domain", "options": DOMAIN_OPTIONS}]},
)


def card(id_, name, idList, idMembers=None, due="", dueComplete=False,
         dateLastActivity="", domain_option="", checkItems=0, checkItemsChecked=0, comments=0):
    item = {
        "id": id_, "name": name, "idList": idList, "idBoard": MASTER,
        "idMembers": idMembers or [], "due": due, "dueComplete": dueComplete,
        "dateLastActivity": dateLastActivity, "labels": [],
        "badges": {"checkItems": checkItems, "checkItemsChecked": checkItemsChecked, "comments": comments},
        "shortUrl": f"https://trello.com/c/{id_}",
    }
    if domain_option:
        item["customFieldItems"] = [{"idValue": domain_option}]
    return item


class RadarBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.store = SettingsStore(self.db)
        self.radar = TeamRadar(self.db, self.store)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def sync_for(self, cards_for_master: list[dict]) -> RadarSync:
        fixture = dict(FIXTURE, cards={MASTER: cards_for_master})
        client = FakeTrelloClient(**fixture)
        return RadarSync(client, self.db, self.store)


class TestStatesPure(unittest.TestCase):
    def test_backlog_never_flags_no_update(self):
        c = {"due": "", "due_complete": 0, "list_name": "Backlog",
             "last_activity": iso(NOW - timedelta(days=40))}
        self.assertEqual(states.card_state(c, NOW)["state"], "on_track")

    def test_paul_today_untouched_three_days_is_no_update(self):
        c = {"due": "", "due_complete": 0, "list_name": "Paul Today",
             "last_activity": iso(NOW - timedelta(days=3))}
        result = states.card_state(c, NOW)
        self.assertEqual(result["state"], "no_update")
        self.assertIn((NOW - timedelta(days=3)).date().isoformat(), result["evidence"])

    def test_overdue_beats_no_update(self):
        c = {"due": iso(NOW - timedelta(days=5)), "due_complete": 0, "list_name": "Paul Today",
             "last_activity": iso(NOW - timedelta(days=10))}
        self.assertEqual(states.card_state(c, NOW)["state"], "overdue")

    def test_due_soon_within_48h(self):
        c = {"due": iso(NOW + timedelta(hours=20)), "due_complete": 0, "list_name": "This Week",
             "last_activity": iso(NOW)}
        self.assertEqual(states.card_state(c, NOW)["state"], "due_soon")

    def test_cycle_stats_learning_below_sample_floor(self):
        result = states.cycle_stats([1.0, 2.0, 3.0])
        self.assertTrue(result["learning"])
        self.assertIsNone(result["median"])

    def test_cycle_stats_real_median_at_sample_floor(self):
        result = states.cycle_stats([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertFalse(result["learning"])
        self.assertEqual(result["median"], 3.0)

    def test_no_signal_on_a_card_younger_than_a_week(self):
        c = {"first_seen": iso(NOW - timedelta(days=2)), "due_change_count": 5, "list_name": "Paul Today",
             "list_entered_at": iso(NOW - timedelta(days=2)), "_now": NOW}
        self.assertEqual(states.taking_too_long_signals(c, {}, {"median": None, "learning": True}), [])

    def test_due_churn_signal_fires_at_threshold(self):
        c = {"first_seen": iso(NOW - timedelta(days=20)), "due_change_count": 2, "list_name": "This Week",
             "list_entered_at": iso(NOW - timedelta(days=5)), "_now": NOW}
        signals = states.taking_too_long_signals(c, {}, {"median": None, "learning": True})
        self.assertTrue(any(s["kind"] == "due_churn" for s in signals))
        self.assertIn("moved 2 times", signals[0]["evidence"])

    def test_ping_pong_signal_on_three_revisits(self):
        c = {"first_seen": iso(NOW - timedelta(days=20)), "due_change_count": 0, "list_name": "This Week",
             "list_entered_at": iso(NOW - timedelta(days=5)), "_now": NOW}
        signals = states.taking_too_long_signals(c, {"This Week": 3}, {"median": None, "learning": True})
        self.assertTrue(any(s["kind"] == "ping_pong" for s in signals))

    def test_aged_signal_only_fires_with_real_median(self):
        c = {"first_seen": iso(NOW - timedelta(days=20)), "due_change_count": 0, "list_name": "This Week",
             "list_entered_at": iso(NOW - timedelta(days=10)), "_now": NOW}
        # Learning (no median yet) — no 'aged' signal invented.
        self.assertEqual(states.taking_too_long_signals(c, {}, {"median": None, "learning": True}), [])
        # Real median, way past double it — fires.
        signals = states.taking_too_long_signals(c, {}, {"median": 2.0, "learning": False})
        self.assertTrue(any(s["kind"] == "aged" for s in signals))


class TestSyncAndCoverage(RadarBase):
    async def test_backlog_card_never_flagged(self):
        sync = self.sync_for([
            card("C1", "Old idea", "L3", dateLastActivity=iso(NOW - timedelta(days=30))),
        ])
        await sync.sync()
        derived = await self.radar._derive_all()
        self.assertEqual(derived[0]["state"], "on_track")

    async def test_overdue_and_untouched_shows_overdue_only(self):
        sync = self.sync_for([
            card("C1", "Overdue thing", "L1", idMembers=["M1"],
                 due=iso(NOW - timedelta(days=4)), dueComplete=False,
                 dateLastActivity=iso(NOW - timedelta(days=10))),
        ])
        await sync.sync()
        derived = await self.radar._derive_all()
        self.assertEqual(derived[0]["state"], "overdue")

    async def test_company_resolves_from_custom_field(self):
        sync = self.sync_for([
            card("C1", "Anything", "L1", domain_option="opt-prodermis"),
        ])
        await sync.sync()
        derived = await self.radar._derive_all()
        self.assertEqual(derived[0]["company_tag"], "Prodermis")

    async def test_no_boards_visible_for_a_person_with_zero_cards(self):
        sync = self.sync_for([card("C1", "Kiefer's card", "L1", idMembers=["M1"])])
        await sync.sync()
        cols = await self.radar.columns()
        self.assertTrue(cols["Harry"]["no_boards_visible"])
        self.assertFalse(cols["Kiefer"]["no_boards_visible"])
        spoken = await self.radar.person_cards_spoken("Harry")
        self.assertIn("No boards visible for Harry", spoken)

    async def test_stale_sync_is_flagged(self):
        await self.store.set("radar_last_sync_at", iso(datetime.now(timezone.utc) - timedelta(hours=3)))
        self.assertTrue((await self.radar.coverage())["stale"])
        await self.store.set("radar_last_sync_at", iso(datetime.now(timezone.utc)))
        self.assertFalse((await self.radar.coverage())["stale"])

    async def test_card_removed_from_trello_is_archived_not_deleted(self):
        sync = self.sync_for([card("C1", "Gone soon", "L1")])
        await sync.sync()
        sync2 = self.sync_for([])   # C1 no longer returned by the board poll
        await sync2.sync()
        row = await self.db.fetch_one("SELECT archived FROM radar_cards WHERE card_id = 'C1'")
        self.assertEqual(row["archived"], 1)
        derived = await self.radar._derive_all()
        self.assertEqual(derived, [])   # archived cards never surface


class TestNeedsYou(RadarBase):
    async def test_capped_at_five_with_accurate_overflow(self):
        cards = [
            card(f"C{i}", f"Overdue {i}", "L1", idMembers=["M1"],
                 due=iso(NOW - timedelta(days=1)), dueComplete=False)
            for i in range(8)
        ]
        sync = self.sync_for(cards)
        await sync.sync()
        needs = await self.radar.needs_you()
        self.assertEqual(len(needs["rows"]), 5)
        self.assertEqual(needs["overflow"], 3)

    async def test_unassigned_cards_never_appear_in_needs_you(self):
        sync = self.sync_for([
            card("C1", "Nobody's overdue thing", "L1", idMembers=[],
                 due=iso(NOW - timedelta(days=2)), dueComplete=False),
        ])
        await sync.sync()
        needs = await self.radar.needs_you()
        self.assertEqual(needs["rows"], [])


class TestDailyLineAndInterrupt(RadarBase):
    async def test_silence_when_nothing_overdue(self):
        sync = self.sync_for([card("C1", "Fine", "L1", idMembers=["M1"])])
        await sync.sync()
        self.assertEqual(await self.radar.daily_line("2026-08-07"), "")

    async def test_daily_line_fires_once_per_day(self):
        sync = self.sync_for([
            card("C1", "Late thing", "L1", idMembers=["M1"],
                 due=iso(NOW - timedelta(days=1)), dueComplete=False),
        ])
        await sync.sync()
        first = await self.radar.daily_line("2026-08-07")
        self.assertIn("overdue", first)
        second = await self.radar.daily_line("2026-08-07")
        self.assertEqual(second, "")   # already pushed today

    async def test_interrupt_fires_only_for_warroom_cards_going_overdue(self):
        await self.db.execute(
            "INSERT INTO warroom_watch (session_id, card_id, board_key, created_at) VALUES (42, 'C1', 'master', ?)",
            (iso(NOW),),
        )
        sync = self.sync_for([card("C1", "Approved project card", "L1", idMembers=["M1"])])
        await sync.sync()
        self.assertEqual(await self.radar.check_interrupt("2026-08-07"), "")   # not overdue yet

        sync2 = self.sync_for([
            card("C1", "Approved project card", "L1", idMembers=["M1"],
                 due=iso(NOW - timedelta(days=1)), dueComplete=False),
        ])
        await sync2.sync()
        message = await self.radar.check_interrupt("2026-08-07")
        self.assertIn("War Room session", message)
        # Capped at once a day.
        self.assertEqual(await self.radar.check_interrupt("2026-08-07"), "")


class TestWarRoomIntegration(RadarBase):
    async def test_project_status_reads_from_warroom_watch(self):
        await self.db.execute(
            "INSERT INTO warroom_watch (session_id, card_id, board_key, created_at) VALUES (7, 'C1', 'master', ?)",
            (iso(NOW),),
        )
        sync = self.sync_for([card("C1", "Ad campaign card 1", "L1", idMembers=["M2"])])
        await sync.sync()
        status = await self.radar.project_status(7)
        self.assertEqual(len(status["cards"]), 1)
        self.assertEqual(status["cards"][0]["title"], "Ad campaign card 1")
        spoken = await self.radar.project_status_spoken(7)
        self.assertIn("1 card(s)", spoken)


class TestNothingOutboundAndReadOnly(RadarBase):
    async def test_full_week_of_overdue_fixtures_never_writes_to_trello(self):
        """§7's hard rule, tested on the wire: the fake client raises on
        ANY write call, so a silent bug here fails the test immediately."""
        cards = [
            card("C1", "Harry's overdue thing", "L1", idMembers=["M1"],
                 due=iso(NOW - timedelta(days=4)), dueComplete=False),
            card("C2", "Kiefer's overdue thing", "L1", idMembers=["M1"],
                 due=iso(NOW - timedelta(days=2)), dueComplete=False),
            card("C3", "Paul's overdue thing", "L1", idMembers=["M2"],
                 due=iso(NOW - timedelta(days=1)), dueComplete=False),
        ]
        sync = self.sync_for(cards)
        await sync.sync()   # would raise WriteAttempted if it ever tried to write
        await self.radar.daily_line("2026-08-07")
        await self.radar.check_interrupt("2026-08-07")
        await self.radar.needs_you()
        await self.radar.columns()
        # If we got here, zero writes were attempted — the assertion IS the test.

    def test_no_write_method_names_appear_in_radar_source(self):
        import inspect

        from app.radar import service, sync

        for module in (sync, service):
            source = inspect.getsource(module)
            for forbidden in ("comment(", "assign_member(", ".move_card(", ".archive_card(", "add_label("):
                self.assertNotIn(forbidden, source, f"{forbidden} found in {module.__name__}")


class TestEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi.testclient import TestClient

        from app import main as app_main
        from app.clients.deepgram_client import DeepgramClient
        from app.clients.elevenlabs_client import ElevenLabsClient
        from app.config import Settings
        from app.core.router import JarvisRouter
        from tests.test_wakeup import OWNER, FakePhone, Harness

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings, db=self.db,
            telegram=self.h.jobs.telegram,
            claude=self.h.jobs.claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            heartbeat=self.h.jobs,
        )
        self.store = SettingsStore(self.db)
        self.router.radar = TeamRadar(self.db, self.store)
        self.secret = settings.effective_desktop_secret
        app_main.app.state.router = self.router
        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        from app import main as app_main

        del app_main.app.state.router
        await self.db.close()
        self._dir.cleanup()

    def test_wrong_secret_403s(self):
        for url in (
            "/desktop/WRONG/radar/needs-you", "/desktop/WRONG/radar/columns",
            "/desktop/WRONG/radar/rollup", "/desktop/WRONG/radar/coverage",
        ):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    async def test_endpoints_return_coverage_and_data_shape(self):
        sync = RadarSync(FakeTrelloClient(**dict(FIXTURE, cards={MASTER: [
            card("C1", "Something", "L1", idMembers=["M1"]),
        ]})), self.db, self.store)
        await sync.sync()

        needs = self.client.get(f"/desktop/{self.secret}/radar/needs-you").json()
        self.assertIn("coverage", needs)
        self.assertIn("rows", needs)

        columns = self.client.get(f"/desktop/{self.secret}/radar/columns").json()
        self.assertIn("Kiefer", columns["columns"])
        self.assertIn("Harry", columns["columns"])

        rollup = self.client.get(f"/desktop/{self.secret}/radar/rollup").json()
        self.assertIn("companies", rollup)

        coverage = self.client.get(f"/desktop/{self.secret}/radar/coverage").json()
        self.assertIn("Master Board", coverage["boards"])


if __name__ == "__main__":
    unittest.main()
