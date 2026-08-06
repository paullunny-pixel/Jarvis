"""Health Auto Export adapter (5 Aug): nested format → flat fields, header
auth, and hourly-safe run dedupe."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.heartbeat.health_import import flatten_export

# Endpoint tests run against the real clock — dates must roll with it
# (hardcoded 2026-08-05 broke the suite at midnight, 6 Aug). And they must
# roll in the ENDPOINT'S timezone: between 21:00 London and midnight, Dubai
# is already tomorrow, and a Dubai-dated payload missed the server's 'today'
# (20:49 UTC, 6 Aug — the second midnight bug of the day).
NOW = datetime.now(ZoneInfo("Europe/London")).replace(hour=11, minute=0)
TODAY = NOW.date().isoformat()
YESTERDAY = (NOW - timedelta(days=1)).date().isoformat()


def hae_payload():
    return {"data": {
        "metrics": [
            {"name": "step_count", "units": "count", "data": [
                {"date": f"{TODAY} 09:00:00 +0400", "qty": 1200},
                {"date": f"{TODAY} 10:00:00 +0400", "qty": 800},
                {"date": f"{YESTERDAY} 22:00:00 +0400", "qty": 5000},   # not today
            ]},
            {"name": "dietary_water", "units": "mL", "data": [
                {"date": f"{TODAY} 09:30:00 +0400", "qty": 500},
                {"date": f"{TODAY} 10:30:00 +0400", "qty": 300},
            ]},
            {"name": "weight_body_mass", "units": "kg", "data": [
                {"date": f"{TODAY} 08:35:00 +0400", "qty": 92.4},
            ]},
            {"name": "resting_heart_rate", "units": "count/min", "data": [
                {"date": f"{TODAY} 08:00:00 +0400", "qty": 57},
                {"date": f"{TODAY} 10:00:00 +0400", "qty": 59},
            ]},
            {"name": "sleep_analysis", "units": "hr", "data": [
                {"date": f"{TODAY} 08:00:00 +0400", "totalSleep": 7.2},
            ]},
        ],
        "workouts": [
            {"name": "Outdoor Run", "start": f"{TODAY} 08:40:00 +0400",
             "distance": {"qty": 5.2, "units": "km"}, "duration": 1860},
        ],
    }}


class TestFlatten(unittest.TestCase):
    def test_metrics_flatten_today_only(self):
        flat = flatten_export(hae_payload(), NOW)
        self.assertEqual(flat["steps"], 2000)          # yesterday's 5000 excluded
        self.assertEqual(flat["water_ml"], 800)        # today's running total
        self.assertEqual(flat["weight_kg"], 92.4)
        self.assertEqual(flat["resting_hr"], 58)       # averaged
        self.assertEqual(flat["sleep_hours"], 7.2)
        self.assertEqual(flat["date"], TODAY)

    def test_run_workout_with_seconds_duration(self):
        flat = flatten_export(hae_payload(), NOW)
        self.assertEqual(flat["run_km"], 5.2)
        self.assertEqual(flat["run_min"], 31.0)        # 1860s → minutes

    def test_litres_and_pounds_convert(self):
        payload = {"data": {"metrics": [
            {"name": "dietary_water", "units": "L", "data": [
                {"date": f"{TODAY} 09:00:00 +0400", "qty": 1.5}]},
            {"name": "weight_body_mass", "units": "lb", "data": [
                {"date": f"{TODAY} 09:00:00 +0400", "qty": 200}]},
        ]}}
        flat = flatten_export(payload, NOW)
        self.assertEqual(flat["water_ml"], 1500)
        self.assertAlmostEqual(flat["weight_kg"], 90.72, places=2)

    def test_non_run_and_stale_workouts_ignored(self):
        payload = {"data": {"workouts": [
            {"name": "Yoga", "start": f"{TODAY} 09:00:00 +0400",
             "distance": {"qty": 9, "units": "km"}},
            {"name": "Outdoor Run", "start": f"{YESTERDAY} 08:00:00 +0400",
             "distance": {"qty": 5.0, "units": "km"}},
        ]}}
        self.assertNotIn("run_km", flatten_export(payload, NOW))

    def test_empty_payload_is_harmless(self):
        self.assertEqual(flatten_export({"data": {}}, NOW), {"date": TODAY})

    def test_water_name_and_unit_variants_all_land(self):
        # Display-name metric, fl_oz units — WaterMinder/HAE version drift.
        payload = {"data": {"metrics": [
            {"name": "Dietary Water", "units": "fl_oz", "data": [
                {"date": f"{TODAY} 09:00:00 +0400", "qty": 25.36}]},
        ]}}
        self.assertEqual(flatten_export(payload, NOW)["water_ml"], 750)

    def test_foreign_timezone_stamp_still_counts(self):
        # A 'Today' export stamped in UTC can look like yesterday — the range
        # is already Today, so what was sent is what counts (the 750ml bug).
        payload = {"data": {"metrics": [
            {"name": "dietary_water", "units": "mL", "data": [
                {"date": f"{YESTERDAY} 20:30:00 +0000", "qty": 750}]},
        ]}}
        self.assertEqual(flatten_export(payload, NOW)["water_ml"], 750)

    def test_parsed_summary_rides_the_response(self):
        pass  # covered in TestEndpointAuth below


class TestEndpointAuth(unittest.TestCase):
    def setUp(self):
        import asyncio
        import os
        import tempfile

        from fastapi.testclient import TestClient

        from app import main as app_main
        from app.config import Settings
        from app.core.store import SettingsStore
        from app.db.sqlite import SqliteDatabase
        from app.heartbeat.streaks import Streaks

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        asyncio.run(self.db.init())
        stub = type("StubRouter", (), {})()
        stub.settings = Settings(
            telegram_bot_token="TOK", apple_health_webhook_secret="SHHH", _env_file=None
        )
        stub.db = self.db
        stub.store = SettingsStore(self.db)
        stub.streaks = Streaks(self.db)
        self._app = app_main.app
        self._app.state.router = stub
        self.client = TestClient(self._app)

    def tearDown(self):
        import asyncio

        del self._app.state.router
        asyncio.run(self.db.close())
        self._dir.cleanup()

    def test_header_auth_accepts_nested_format(self):
        response = self.client.post(
            "/webhook/apple-health", json=hae_payload(),
            headers={"X-Health-Secret": "SHHH"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["water_recorded"])
        self.assertEqual(body["parsed"]["water_ml"], 800)   # visible in the app's log
        self.assertEqual(body["parsed"]["steps"], 2000)

        async def _crumb():
            import json as _json

            from app.core.store import SettingsStore

            return _json.loads(await SettingsStore(self.db).get("health_last_import"))

        import asyncio

        crumb = asyncio.run(_crumb())
        self.assertEqual(crumb["parsed"]["water_ml"], 800)   # 'status' can see it

    def test_query_auth_works_too(self):
        response = self.client.post(
            "/webhook/apple-health?secret=SHHH", json=hae_payload()
        )
        self.assertEqual(response.status_code, 200)

    def test_wrong_or_missing_secret_403s(self):
        self.assertEqual(
            self.client.post("/webhook/apple-health", json=hae_payload()).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                "/webhook/apple-health", json=hae_payload(),
                headers={"X-Health-Secret": "WRONG"},
            ).status_code, 403,
        )

    def test_hourly_reposts_log_the_run_once(self):
        for _ in range(3):
            response = self.client.post(
                "/webhook/apple-health", json=hae_payload(),
                headers={"X-Health-Secret": "SHHH"},
            )
            self.assertEqual(response.status_code, 200)

        async def _count():
            rows = await self.db.fetch_all("SELECT id FROM runs")
            return len(rows)

        import asyncio

        self.assertEqual(asyncio.run(_count()), 1)

    def test_flat_shortcut_format_still_works(self):
        response = self.client.post(
            "/webhook/apple-health",
            json={"secret": "SHHH", "steps": 900, "water_ml": 400},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["water_recorded"])
