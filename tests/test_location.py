"""GPS → clocks: region resolution, the honest offset fallback, and the
apply path that switches every clock when Paul moves."""
import json
import os
import tempfile
import unittest

from app.heartbeat.location import LAST_LOCATION_KEY, apply_location, resolve
from app.db.sqlite import SqliteDatabase


class TestResolve(unittest.TestCase):
    def test_pauls_world_resolves_by_name(self):
        self.assertEqual(resolve(25.2048, 55.2708), ("Dubai", "Asia/Dubai"))
        self.assertEqual(resolve(52.9548, -1.1581), ("the UK", "Europe/London"))   # Nottingham
        self.assertEqual(resolve(40.4168, -3.7038), ("Spain", "Europe/Madrid"))    # Kenny's Málaga side
        self.assertEqual(resolve(38.7223, -9.1393), ("Portugal", "Europe/Lisbon"))
        self.assertEqual(resolve(41.9028, 12.4964), ("Italy", "Europe/Rome"))
        self.assertEqual(resolve(37.5665, 126.9780), ("South Korea", "Asia/Seoul"))
        self.assertEqual(resolve(-23.5505, -46.6333), ("Brazil", "America/Sao_Paulo"))

    def test_elsewhere_falls_back_to_honest_utc_offset(self):
        place, tz = resolve(40.7128, -74.0060)          # New York
        self.assertEqual(tz, "Etc/GMT+5")               # Etc signs invert: UTC-5
        self.assertIn("UTC-5", place)
        place, tz = resolve(35.6762, 139.6503)          # Tokyo
        self.assertEqual(tz, "Etc/GMT-9")
        self.assertEqual(resolve(51.0, 0.5)[1], "Europe/London")  # UK box wins over offset


class TestApplyLocation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        from app.core.store import SettingsStore
        from app.memory.crypto import PrivateBox
        from app.memory.embedder import HashEmbedder
        from app.memory.store import LivingFacts

        self.store = SettingsStore(self.db)
        self.living = LivingFacts(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_landing_in_dubai_switches_the_clocks_once(self):
        await self.store.set("current_timezone", "Europe/London")
        result = await apply_location(self.store, self.living, 25.2048, 55.2708)
        self.assertEqual(result, {"place": "Dubai", "tz": "Asia/Dubai", "changed": True})
        self.assertEqual(await self.store.get("current_timezone"), "Asia/Dubai")
        # The brain's living facts know where he is.
        self.assertIn("Dubai", await self.living.get("location.current"))
        # A second fix in the same zone changes nothing (no repeat notify).
        again = await apply_location(self.store, self.living, 25.1, 55.3)
        self.assertFalse(again["changed"])
        fix = json.loads(await self.store.get(LAST_LOCATION_KEY))
        self.assertEqual(fix["place"], "Dubai")

    async def test_no_living_facts_still_moves_the_clocks(self):
        await self.store.set("current_timezone", "Asia/Dubai")
        result = await apply_location(self.store, None, 52.95, -1.16)
        self.assertTrue(result["changed"])
        self.assertEqual(await self.store.get("current_timezone"), "Europe/London")


if __name__ == "__main__":
    unittest.main()
