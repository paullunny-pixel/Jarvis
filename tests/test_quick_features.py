"""Quick ADHD wins (7 Aug build slice): deadline radar, Withings smart-scale
auto-logging, meds refill tracking, follow-up chaser."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.telegram_client import TelegramClient
from app.clients.withings_client import ReauthNeeded, WithingsClient
from app.config import Settings
from app.db.sqlite import SqliteDatabase
from app.deadlines import DeadlineRadar
from app.followup import OutboundWatch
from app.heartbeat.jobs import HeartbeatJobs
from app.mail.client import MailAccount, MailClient
from app.mail.service import MailService
from app.meds_supply import MedSupply
from app.withings_service import Withings

OWNER = 111
TZ = ZoneInfo("Europe/London")


class Harness:
    def __init__(self, db):
        self.telegram_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.jobs = HeartbeatJobs(
            settings=settings, db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db)
        self.today = date.today()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()


# ------------------------------------------------------------- Deadline radar
class TestDeadlineRadar(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.radar = DeadlineRadar(self.db)
        self.h.jobs.deadlines = self.radar

    async def test_add_and_list(self):
        await self.radar.add_date("Eva's birthday", "2026-09-28", recurring_yearly=True)
        items = await self.radar.list_dates(self.today)
        self.assertEqual(items[0]["label"], "Eva's birthday")

    async def test_duplicate_is_not_added_twice(self):
        await self.radar.add_date("Villa demand", "2027-01-07")
        reply = await self.radar.add_date("Villa demand", "2027-01-07")
        self.assertIn("Already tracking", reply)

    async def test_bad_date_is_refused(self):
        reply = await self.radar.add_date("Nonsense", "not-a-date")
        self.assertIn("isn't a date", reply)

    async def test_remove_unknown_says_so(self):
        reply = await self.radar.remove_date("Nothing here")
        self.assertIn("Couldn't find", reply)

    async def test_recurring_date_rolls_to_next_year_once_past(self):
        # A date from last year, recurring yearly, must project to a future occurrence.
        stale = date(self.today.year - 1, 6, 15)
        await self.radar.add_date("Anniversary", stale.isoformat(), recurring_yearly=True)
        items = await self.radar.upcoming(self.today)
        [item] = [i for i in items if i["label"] == "Anniversary"]
        self.assertGreaterEqual(item["date"], self.today)

    async def test_trello_due_dates_are_included(self):
        due = (self.today + timedelta(days=3)).isoformat() + "T00:00:00.000Z"
        await self.db.execute(
            "INSERT INTO tasks (trello_id, title, actionable, due_date) VALUES ('T1', 'Renew passport', 1, ?)",
            (due,),
        )
        items = await self.radar.upcoming(self.today)
        self.assertTrue(any(i["label"] == "Renew passport" for i in items))

    async def test_due_countdowns_matches_lead_days(self):
        await self.radar.add_date("Day-of thing", self.today.isoformat())
        await self.radar.add_date("Three days out", (self.today + timedelta(days=3)).isoformat())
        await self.radar.add_date("Two days out (not a lead day)", (self.today + timedelta(days=2)).isoformat())
        due = await self.radar.due_countdowns(self.today)
        labels = {i["label"] for i in due}
        self.assertIn("Day-of thing", labels)
        self.assertIn("Three days out", labels)
        self.assertNotIn("Two days out (not a lead day)", labels)

    async def test_tick_sends_a_line_per_due_item_once(self):
        await self.radar.add_date("Passport renewal", self.today.isoformat())
        await self.h.jobs.deadline_radar_tick(datetime.now(TZ))
        self.assertEqual(len(self.h.texts()), 1)
        self.assertIn("Passport renewal", self.h.texts()[0])
        await self.h.jobs.deadline_radar_tick(datetime.now(TZ))
        self.assertEqual(len(self.h.texts()), 1)  # same day, same threshold — not repeated

    async def test_tick_is_a_noop_when_nothing_wired(self):
        self.h.jobs.deadlines = None
        await self.h.jobs.deadline_radar_tick(datetime.now(TZ))
        self.assertEqual(self.h.texts(), [])


# ------------------------------------------------------------- Meds refill
class TestMedSupply(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.supply = MedSupply(self.db)
        self.h.jobs.med_supply = self.supply

    async def test_unknown_item_is_refused(self):
        reply = await self.supply.set_by_date("vitamins", "2026-09-01")
        self.assertIn("Don't recognise", reply)

    async def test_set_by_date_and_status(self):
        await self.supply.set_by_date("adhd", "2026-09-01", warn_days_before=5)
        [row] = await self.supply.status()
        self.assertEqual(row["run_out_date"], "2026-09-01")
        self.assertEqual(row["warn_days_before"], 5)

    async def test_set_by_quantity_computes_run_out(self):
        await self.supply.set_by_quantity(
            "adhd", quantity=30, doses_per_day=1, start_date_iso=self.today.isoformat()
        )
        [row] = await self.supply.status()
        expected = self.today + timedelta(days=30)
        self.assertEqual(row["run_out_date"], expected.isoformat())

    async def test_resetting_an_item_replaces_not_duplicates(self):
        await self.supply.set_by_date("trt", "2026-09-01")
        await self.supply.set_by_date("trt", "2026-10-01")
        rows = await self.supply.status()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_out_date"], "2026-10-01")

    async def test_due_warnings_within_window(self):
        await self.supply.set_by_date("adhd", (self.today + timedelta(days=3)).isoformat(), warn_days_before=5)
        await self.supply.set_by_date("trt", (self.today + timedelta(days=20)).isoformat(), warn_days_before=5)
        due = await self.supply.due_warnings(self.today)
        items = {d["item"] for d in due}
        self.assertIn("adhd", items)
        self.assertNotIn("trt", items)

    async def test_past_run_out_stops_nagging(self):
        await self.supply.set_by_date("adhd", (self.today - timedelta(days=1)).isoformat(), warn_days_before=5)
        due = await self.supply.due_warnings(self.today)
        self.assertEqual(due, [])

    async def test_tick_sends_essential_reorder_line(self):
        await self.supply.set_by_date("adhd", (self.today + timedelta(days=5)).isoformat())
        await self.h.jobs.meds_refill_tick(datetime.now(TZ))
        [msg] = self.h.texts()
        self.assertIn("ADHD", msg)
        self.assertIn("reorder", msg)

    async def test_tick_essential_pierces_quiet_day(self):
        await self.h.jobs.set_quiet_today(True)
        await self.supply.set_by_date("adhd", self.today.isoformat())
        await self.h.jobs.meds_refill_tick(datetime.now(TZ))
        self.assertEqual(len(self.h.texts()), 1)


# ------------------------------------------------------------- Follow-up chaser
def raw_email(from_addr, subject, body, to="", days_ago=0, from_name=""):
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["Subject"] = subject
    if to:
        msg["To"] = to
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    msg["Date"] = when.strftime("%a, %d %b %Y %H:%M:%S +0000")
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self, inbox=None, sent=None):
        self._inbox = inbox or []
        self._sent = sent or []
        self._messages = self._inbox

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        return "OK", []

    def select(self, mailbox, readonly=False):
        low = str(mailbox).lower()
        if "sent" in low:
            self._messages = self._sent
            return ("OK", []) if self._sent is not None else ("NO", [])
        self._messages = self._inbox
        return "OK", []

    def search(self, charset, *criteria):
        import re as _re

        crit = " ".join(
            c.decode() if isinstance(c, (bytes, bytearray)) else str(c) for c in criteria
        )
        matches = list(range(1, len(self._messages) + 1))
        from_filter = _re.search(r'FROM "([^"]+)"', crit)
        if from_filter:
            addr = from_filter.group(1).lower().encode()
            matches = [
                i + 1 for i, raw in enumerate(self._messages)
                if addr in raw.split(b"\n\n", 1)[0].lower()
            ]
        ids = b" ".join(str(i).encode() for i in matches)
        return "OK", [ids]

    def fetch(self, msg_id, spec):
        raw = self._messages[int(msg_id) - 1]
        header = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"1 (BODY[HEADER] {%d}" % len(header), header), b")"]


class TestOutboundWatch(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.watch = OutboundWatch(self.db, mail=None)
        self.h.jobs.outbound_watch = self.watch

    async def test_flag_and_list(self):
        await self.watch.flag("harry@dermadirect.co.uk", "BMI sales plan")
        items = await self.watch.list_open()
        self.assertEqual(items[0]["subject"], "BMI sales plan")

    async def test_flagging_the_same_thing_twice_does_not_duplicate(self):
        await self.watch.flag("harry@dermadirect.co.uk", "BMI sales plan")
        reply = await self.watch.flag("harry@dermadirect.co.uk", "BMI sales plan")
        self.assertIn("Already chasing", reply)
        self.assertEqual(len(await self.watch.list_open()), 1)

    async def test_resolve_by_subject(self):
        await self.watch.flag("harry@dermadirect.co.uk", "BMI sales plan")
        reply = await self.watch.resolve("BMI")
        self.assertIn("off the chase list", reply)
        self.assertEqual(await self.watch.list_open(), [])

    async def test_resolve_nothing_open_says_so(self):
        reply = await self.watch.resolve("nothing like this exists")
        self.assertIn("Nothing open", reply)

    async def test_due_chases_respects_threshold(self):
        await self.db.execute(
            "INSERT INTO outbound_watch (subject, recipient, sent_at, source, status)"
            " VALUES ('Fresh ask', 'a@x.com', ?, 'paul_flagged', 'open')",
            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),),
        )
        await self.db.execute(
            "INSERT INTO outbound_watch (subject, recipient, sent_at, source, status)"
            " VALUES ('Old ask', 'b@x.com', ?, 'paul_flagged', 'open')",
            ((datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),),
        )
        due = await self.watch.due_chases(self.today)
        subjects = {d["subject"] for d in due}
        self.assertNotIn("Fresh ask", subjects)
        self.assertIn("Old ask", subjects)

    async def test_recently_chased_is_not_rechased_immediately(self):
        await self.db.execute(
            "INSERT INTO outbound_watch (subject, recipient, sent_at, source, status, last_chased_at)"
            " VALUES ('Old ask', 'b@x.com', ?, 'paul_flagged', 'open', ?)",
            (
                (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
                (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            ),
        )
        due = await self.watch.due_chases(self.today)
        self.assertEqual(due, [])

    async def test_tick_sends_and_marks_chased(self):
        await self.watch.flag("harry@dermadirect.co.uk", "BMI sales plan")
        await self.db.execute(
            "UPDATE outbound_watch SET sent_at = ?",
            ((datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),),
        )
        await self.h.jobs.followup_chaser_tick(datetime.now(TZ))
        [msg] = self.h.texts()
        self.assertIn("harry@dermadirect.co.uk", msg)
        self.assertIn("BMI sales plan", msg)
        [row] = await self.watch.list_open()
        self.assertTrue(row["last_chased_at"])

    async def test_auto_detect_flags_unanswered_sent_item(self):
        account = MailAccount("paul@dermadirect.co.uk", "app-pw")
        sent = [raw_email("paul@dermadirect.co.uk", "Quote request", "Can you send pricing?",
                           to="supplier@x.com", days_ago=5)]
        client = MailClient(account, imap_factory=lambda: FakeIMAP(inbox=[], sent=sent))
        mail = MailService([client], self.db)
        watch = OutboundWatch(self.db, mail)
        flagged = await watch.auto_detect_sweep()
        self.assertEqual(flagged, 1)
        [item] = await watch.list_open()
        self.assertEqual(item["source"], "auto_detected")
        self.assertEqual(item["recipient"], "supplier@x.com")

    async def test_auto_detect_skips_items_with_a_reply(self):
        account = MailAccount("paul@dermadirect.co.uk", "app-pw")
        sent = [raw_email("paul@dermadirect.co.uk", "Quote request", "Can you send pricing?",
                           to="supplier@x.com", days_ago=5)]
        inbox = [raw_email("supplier@x.com", "Re: Quote request", "Here's the pricing.", days_ago=3)]
        client = MailClient(account, imap_factory=lambda: FakeIMAP(inbox=inbox, sent=sent))
        mail = MailService([client], self.db)
        watch = OutboundWatch(self.db, mail)
        flagged = await watch.auto_detect_sweep()
        self.assertEqual(flagged, 0)
        self.assertEqual(await watch.list_open(), [])

    async def test_auto_detect_never_touches_a_paul_flagged_twin(self):
        await self.watch.flag("supplier@x.com", "Quote request")
        account = MailAccount("paul@dermadirect.co.uk", "app-pw")
        sent = [raw_email("paul@dermadirect.co.uk", "Quote request", "Can you send pricing?",
                           to="supplier@x.com", days_ago=5)]
        client = MailClient(account, imap_factory=lambda: FakeIMAP(inbox=[], sent=sent))
        mail = MailService([client], self.db)
        watch2 = OutboundWatch(self.db, mail)
        await watch2.auto_detect_sweep()
        self.assertEqual(len(await self.watch.list_open()), 1)  # still just the one, no duplicate

    async def test_tick_without_mail_wired_is_a_noop(self):
        self.h.jobs.outbound_watch = None
        await self.h.jobs.followup_chaser_tick(datetime.now(TZ))
        self.assertEqual(self.h.texts(), [])


# ------------------------------------------------------------- Withings
class TestWithingsClient(unittest.IsolatedAsyncioTestCase):
    def _handler(self, token_status=0, refresh_status=0, measure_status=0, groups=None):
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in str(request.url):
                body = {
                    "status": token_status,
                    "body": {"access_token": "A1", "refresh_token": "R1", "expires_in": 10800},
                }
                return httpx.Response(200, json=body)
            if "measure" in str(request.url):
                return httpx.Response(200, json={
                    "status": measure_status,
                    "body": {"measuregrps": groups or []},
                })
            return httpx.Response(404)
        return handler

    async def test_auth_url_carries_client_id_and_state(self):
        client = WithingsClient("CID", "SECRET")
        url = client.auth_url("https://x/callback", "SECRETSTATE")
        self.assertIn("client_id=CID", url)
        self.assertIn("state=SECRETSTATE", url)

    async def test_exchange_code_stores_refresh_token(self):
        client = WithingsClient("CID", "SECRET", transport=httpx.MockTransport(self._handler()))
        refresh = await client.exchange_code("CODE", "https://x/callback")
        self.assertEqual(refresh, "R1")
        self.assertEqual(client.refresh_token, "R1")

    async def test_bad_status_raises_withings_error(self):
        from app.clients.withings_client import WithingsError

        client = WithingsClient("CID", "SECRET", transport=httpx.MockTransport(self._handler(token_status=503)))
        with self.assertRaises(WithingsError):
            await client.exchange_code("CODE", "https://x/callback")

    async def test_no_refresh_token_raises_reauth(self):
        client = WithingsClient("CID", "SECRET", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        with self.assertRaises(ReauthNeeded):
            await client._token()

    async def test_measurements_parse_weight_and_fat(self):
        groups = [{
            "date": int(datetime.now(timezone.utc).timestamp()),
            "measures": [
                {"type": 1, "value": 8234, "unit": -2},   # 82.34 kg
                {"type": 6, "value": 184, "unit": -1},    # 18.4 %
            ],
        }]
        client = WithingsClient("CID", "SECRET", transport=httpx.MockTransport(self._handler(groups=groups)))
        client.refresh_token = "R1"
        result = await client.latest_measurements(0, 9999999999)
        self.assertEqual(len(result), 1)


class TestWithingsService(DbCase):
    async def test_not_configured_reports_so(self):
        client = WithingsClient("", "")
        service = Withings(client, self.db, self.h.jobs.store)
        result = await service.daily_sync(datetime.now(timezone.utc))
        self.assertFalse(result["synced"])
        self.assertIn("not configured", result["reason"])

    async def test_not_connected_reports_so(self):
        client = WithingsClient("CID", "SECRET")
        service = Withings(client, self.db, self.h.jobs.store)
        result = await service.daily_sync(datetime.now(timezone.utc))
        self.assertFalse(result["synced"])
        self.assertIn("not connected", result["reason"])

    async def test_successful_sync_writes_health_stats(self):
        now_ts = int(datetime.now(timezone.utc).timestamp())
        groups = [{
            "date": now_ts,
            "measures": [{"type": 1, "value": 8200, "unit": -2}],
        }]

        def handler(request: httpx.Request) -> httpx.Response:
            if "measure" in str(request.url):
                return httpx.Response(200, json={"status": 0, "body": {"measuregrps": groups}})
            return httpx.Response(200, json={"status": 0, "body": {"access_token": "A1"}})

        client = WithingsClient("CID", "SECRET", transport=httpx.MockTransport(handler))
        service = Withings(client, self.db, self.h.jobs.store)
        await service.store_token("R1")
        result = await service.daily_sync(datetime.now(timezone.utc))
        self.assertTrue(result["synced"])
        self.assertEqual(result["weight_kg"], 82.0)
        row = await self.db.fetch_one("SELECT weight_kg FROM health_stats WHERE weight_kg > 0")
        self.assertEqual(row["weight_kg"], 82.0)

    async def test_tick_delegates_when_wired(self):
        client = WithingsClient("", "")
        self.h.jobs.withings = Withings(client, self.db, self.h.jobs.store)
        await self.h.jobs.withings_tick(datetime.now(timezone.utc))  # not configured — no crash

    async def test_tick_is_a_noop_when_not_wired(self):
        self.h.jobs.withings = None
        await self.h.jobs.withings_tick(datetime.now(timezone.utc))  # just shouldn't raise


if __name__ == "__main__":
    unittest.main()
