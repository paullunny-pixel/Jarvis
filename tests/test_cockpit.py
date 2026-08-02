import os
import re
import tempfile
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.cockpit.page import render_page
from app.cockpit.service import CockpitService
from app.config import Settings
from app.core.store import MessageLog, SettingsStore
from app.heartbeat.streaks import Streaks
from app.memory.store import LivingFacts
from app.db.sqlite import SqliteDatabase


class TestCockpitAuth(unittest.IsolatedAsyncioTestCase):
    """The cockpit lock (31 Jul, Paul's ask): the link alone must show
    nothing — password + signed session cookie on top of HTTPS."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.store = SettingsStore(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def test_password_hash_roundtrip(self):
        from app.cockpit import auth

        stored = auth.hash_password("villa-money-2026")
        self.assertTrue(stored.startswith("pbkdf2$"))
        self.assertNotIn("villa-money-2026", stored)      # never stored plain
        self.assertTrue(auth.verify_password("villa-money-2026", stored))
        self.assertFalse(auth.verify_password("wrong", stored))
        self.assertFalse(auth.verify_password("villa-money-2026", "garbage"))
        # Same password, different salt — hashes never repeat.
        self.assertNotEqual(stored, auth.hash_password("villa-money-2026"))

    def test_session_tokens_sign_and_expire(self):
        from app.cockpit import auth

        token = auth.mint_session("KEY1", now=1000.0)
        self.assertTrue(auth.verify_session(token, "KEY1", now=2000.0))
        self.assertFalse(auth.verify_session(token, "OTHERKEY", now=2000.0))   # wrong key
        self.assertFalse(auth.verify_session(token + "x", "KEY1", now=2000.0))  # tampered
        expired = 1000.0 + auth.SESSION_DAYS * 86400 + 1
        self.assertFalse(auth.verify_session(token, "KEY1", now=expired))
        self.assertFalse(auth.verify_session("", "KEY1"))

    async def test_gate_is_secure_by_default(self):
        from app.cockpit import auth

        # No password set → setup, NEVER data — whatever cookie arrives.
        self.assertEqual(await auth.gate(self.store, ""), "setup")
        self.assertEqual(await auth.gate(self.store, "1e99.deadbeef"), "setup")
        await self.store.set(auth.PASSWORD_KEY, auth.hash_password("pw"))
        await self.store.set(auth.SESSION_KEY, "KEY1")
        self.assertEqual(await auth.gate(self.store, ""), "login")
        self.assertEqual(await auth.gate(self.store, auth.mint_session("KEY1")), "ok")
        # Rotating the session key logs every device out.
        await self.store.set(auth.SESSION_KEY, "KEY2")
        self.assertEqual(await auth.gate(self.store, auth.mint_session("KEY1")), "login")

    async def test_set_password_by_telegram_never_logs_the_password(self):
        from tests.test_router import OWNER, RouterHarness
        from tests.test_telegram_client import text_update

        from app.cockpit import auth

        h = RouterHarness(self.db)
        await h.router.handle_update(
            text_update("set cockpit password hurricane-villa-88", OWNER)
        )
        stored = await self.store.get(auth.PASSWORD_KEY, "")
        self.assertTrue(auth.verify_password("hurricane-villa-88", stored))
        self.assertTrue(await self.store.get(auth.SESSION_KEY, ""))
        # The password reached the hash and NOWHERE else.
        rows = await self.db.fetch_all("SELECT transcript FROM messages")
        joined = " ".join(r["transcript"] for r in rows)
        self.assertNotIn("hurricane-villa-88", joined)
        self.assertIn("[cockpit password updated]", joined)

    async def test_short_password_is_refused(self):
        from tests.test_router import OWNER, RouterHarness
        from tests.test_telegram_client import text_update

        from app.cockpit import auth

        h = RouterHarness(self.db)
        await h.router.handle_update(text_update("set cockpit password abc", OWNER))
        self.assertEqual(await self.store.get(auth.PASSWORD_KEY, ""), "")

    async def test_logout_everywhere_rotates_the_session_key(self):
        from tests.test_router import OWNER, RouterHarness
        from tests.test_telegram_client import text_update

        from app.cockpit import auth

        await self.store.set(auth.SESSION_KEY, "OLDKEY")
        h = RouterHarness(self.db)
        await h.router.handle_update(text_update("log out the cockpit everywhere", OWNER))
        self.assertNotEqual(await self.store.get(auth.SESSION_KEY, ""), "OLDKEY")

    async def test_lockout_after_repeated_failures(self):
        from app.cockpit import auth

        t0 = 1000.0
        for _ in range(auth.MAX_ATTEMPTS - 1):
            await auth.note_failed_login(self.store, now=t0)
        self.assertFalse(await auth.login_locked(self.store, now=t0))
        await auth.note_failed_login(self.store, now=t0)      # the tenth
        self.assertTrue(await auth.login_locked(self.store, now=t0))
        # The lock expires with the window...
        after = t0 + auth.LOCKOUT_MINUTES * 60 + 1
        self.assertFalse(await auth.login_locked(self.store, now=after))
        # ...and a correct login clears the slate immediately.
        await auth.note_failed_login(self.store, now=t0)
        await auth.clear_login_failures(self.store)
        self.assertFalse(await auth.login_locked(self.store, now=t0))

    def test_dashboard_escapes_untrusted_text(self):
        # The injection gap: card titles and calendar text render via
        # innerHTML — every interpolated data field must pass through esc().
        from app.cockpit.page import COCKPIT_HTML

        self.assertIn("&#39;", COCKPIT_HTML)                 # esc covers quotes
        self.assertIn("${esc(s.time)}", COCKPIT_HTML)        # ICS-fed timeline
        self.assertIn("${esc(s.title)}", COCKPIT_HTML)
        self.assertIn("${esc(x.title)}", COCKPIT_HTML)       # Trello card titles
        self.assertNotIn("${s.time}", COCKPIT_HTML)

    def test_login_and_setup_pages(self):
        from app.cockpit import auth

        login = auth.login_page("/cockpit/S/login", error="Wrong password.")
        self.assertIn('action="/cockpit/S/login"', login)
        self.assertIn("Wrong password.", login)
        self.assertIn('type="password"', login)
        setup = auth.setup_page()
        self.assertIn("set cockpit password", setup)
        self.assertIn("no data is served", setup.lower())


class TestCockpit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.living = LivingFacts(self.db)
        self.service = CockpitService(self.db, living=self.living)
        self.today = datetime.now(ZoneInfo("Europe/London")).date()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _plant(self):
        await MessageLog(self.db).log("in", "hello jarvis")
        streaks = Streaks(self.db)
        await streaks.record("run", self.today)
        for i in range(3):
            await self.db.execute(
                "INSERT INTO tasks (trello_id, title, company_slug) VALUES (?, ?, 'prodermis')",
                (f"T{i}", f"task {i}"),
            )
            await self.db.execute(
                "INSERT INTO daily_12 (plan_date, position, task_id, company_slug, done)"
                " VALUES (?, ?, ?, 'prodermis', ?)",
                (self.today.isoformat(), i + 1, i + 1, 1 if i == 0 else 0),
            )
        await self.living.set("villa.paid", "~27% paid (~AED 3.10M)", room="finances")
        await self.living.set("villa.price", "AED 11,638,800", room="finances")
        await self.living.set("villa.next_demand", "~AED 1.2M due ~7 Jan 2027", room="finances")
        await self.living.set("sobriety.days", "41", room="private")
        await self.db.execute(
            "INSERT INTO health_stats (stat_date, weight_kg) VALUES ('2026-07-01', 88.7)"
        )
        await self.db.execute(
            "INSERT INTO health_stats (stat_date, weight_kg) VALUES ('2026-07-25', 87.1)"
        )
        await self.db.execute(
            "INSERT INTO runs (run_date, distance_km, duration_min, source)"
            " VALUES ('2026-07-25', 5.0, 27.5, 'apple_health')"
        )

    async def test_gather_full_shape(self):
        await self._plant()
        data = await self.service.gather()
        self.assertEqual(data["streaks"]["run"]["current"], 1)
        self.assertTrue(data["streaks"]["run"]["done_today"])
        self.assertEqual(data["twelve"]["done"], 1)
        self.assertEqual(data["twelve"]["total"], 3)
        prodermis = next(c for c in data["twelve"]["by_company"] if c["slug"] == "prodermis")
        self.assertEqual(len(prodermis["tasks"]), 3)
        self.assertEqual(data["villa"]["pct"], 27)
        self.assertEqual(data["body"]["weight"]["latest"], 87.1)
        self.assertEqual(data["sobriety"]["days"], 41)
        self.assertEqual(data["day_with_jarvis"], 1)
        self.assertTrue(any(s["type"] == "run" and s["done"] for s in data["today"]))

    async def test_kiefer_preview_carries_no_private_content(self):
        await self._plant()
        data = await self.service.gather()
        preview = data["kiefer"]["preview"].lower()
        self.assertNotIn("sober", preview)
        self.assertNotIn("41 days strong", preview)
        self.assertIn("today's focus", preview)

    async def test_empty_day_still_renders(self):
        data = await self.service.gather()
        self.assertEqual(data["twelve"]["done"], 0)
        self.assertEqual(data["twelve"]["total"], 0)
        self.assertIsNone(data["sobriety"]["days"])
        self.assertFalse(data["villa"]["available"])
        self.assertIsNone(data["body"]["weight"])

    async def test_bonus_appears_only_at_12_of_12(self):
        await self._plant()
        await self.db.execute("UPDATE daily_12 SET done = 1")
        await self.db.execute(
            "INSERT INTO tasks (trello_id, title, company_slug) VALUES ('TB', 'bonus task', 'derma_uk')"
        )
        await self.db.execute(
            "INSERT INTO daily_12 (plan_date, position, task_id, company_slug)"
            " VALUES (?, 0, 4, 'derma_uk')",
            (self.today.isoformat(),),
        )
        data = await self.service.gather()
        self.assertIsNotNone(data["twelve"]["bonus"])
        self.assertEqual(data["twelve"]["bonus"]["title"], "bonus task")

    async def test_sobriety_from_start_date(self):
        await self.living.set("sobriety.start_date", "2026-07-01", room="private")
        data = await self.service.gather()
        self.assertEqual(data["sobriety"]["days"], (date.today() - date(2026, 7, 1)).days)


class TestPage(unittest.TestCase):
    def test_render_injects_data_url_and_design(self):
        html = render_page("/cockpit/abc123/data")
        self.assertIn("fetch('/cockpit/abc123/data')", html)
        self.assertNotIn("DATA_URL", html)
        self.assertIn("PROGRESS COCKPIT", html)
        for section in ("Streaks", "Sobriety", "Kiefer", "hour-by-hour", "reactor"):
            self.assertIn(section, html)
        self.assertIn("🔒 private", html)

    def test_cockpit_secret_stable_and_distinct(self):
        s = Settings(telegram_bot_token="ABC", _env_file=None)
        self.assertEqual(len(s.effective_cockpit_secret), 24)
        self.assertNotEqual(s.effective_cockpit_secret, s.effective_webhook_secret[:24])


if __name__ == "__main__":
    unittest.main()
