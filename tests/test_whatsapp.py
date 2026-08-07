"""WhatsApp read-only Phase 1 (official Cloud API, second number) and the
WaterMinder → Apple Health → water_log pipe."""
import json
import os
import tempfile
import unittest

import httpx

from app.clients.whatsapp_client import (
    WhatsAppClient,
    parse_webhook,
    valid_signature,
)
from app.db.sqlite import SqliteDatabase

# A Cloud API webhook payload as Meta sends it: one text, one voice, one image,
# plus a status-update change that must yield nothing.
CLOUD_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WABA1",
        "changes": [
            {"value": {
                "messaging_product": "whatsapp",
                "contacts": [{"profile": {"name": "Kiefer Brindle"}, "wa_id": "447700900001"}],
                "messages": [
                    {"from": "447700900001", "id": "wamid.1", "timestamp": "1754300000",
                     "type": "text", "text": {"body": "Paul — invoices signed, sending the pack over"}},
                    {"from": "447700900001", "id": "wamid.2", "timestamp": "1754300060",
                     "type": "audio", "audio": {"id": "MEDIA1", "voice": True}},
                    {"from": "447700900002", "id": "wamid.3", "timestamp": "1754300120",
                     "type": "image", "image": {"id": "MEDIA2"}},
                ],
            }, "field": "messages"},
            {"value": {"messaging_product": "whatsapp",
                       "statuses": [{"id": "wamid.9", "status": "delivered"}]},
             "field": "messages"},
        ],
    }],
}


class TestParseWebhook(unittest.TestCase):
    def test_messages_parse_with_names_and_media_markers(self):
        messages = parse_webhook(CLOUD_PAYLOAD)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0].wa_id, "447700900001")
        self.assertEqual(messages[0].name, "Kiefer Brindle")
        self.assertIn("invoices signed", messages[0].text)
        self.assertEqual(messages[1].kind, "voice")
        self.assertEqual(messages[1].text, "")
        self.assertEqual(messages[1].media_id, "MEDIA1")
        self.assertEqual(messages[2].kind, "image")
        self.assertEqual(messages[2].name, "")     # unknown sender, no profile
        self.assertEqual(messages[2].text, "[image]")

    def test_status_updates_and_garbage_yield_nothing(self):
        self.assertEqual(parse_webhook({"entry": [{"changes": [{"value": {
            "statuses": [{"status": "read"}]}}]}]}), [])
        self.assertEqual(parse_webhook({}), [])
        self.assertEqual(parse_webhook({"entry": None}), [])


class TestLegacyGhostTable(unittest.TestCase):
    def test_schema_never_reuses_the_old_whatsapp_table_name(self):
        # The removed pre-Telegram route left 'whatsapp_ingest' (group_id
        # shape) in prod; reusing the name crash-looped the 4 Aug deploys.
        from app.db import schema

        joined = " ".join(schema.TABLES)
        self.assertNotIn(" whatsapp_ingest", joined)
        self.assertIn("wa_direct_ingest", joined)


class TestSignature(unittest.TestCase):
    def test_signature_checks_out_and_rejects_wrong(self):
        import hashlib
        import hmac as _hmac

        body = b'{"entry": []}'
        good = "sha256=" + _hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
        self.assertTrue(valid_signature("appsecret", body, good))
        self.assertFalse(valid_signature("appsecret", body, "sha256=deadbeef"))
        self.assertFalse(valid_signature("appsecret", body, ""))

    def test_no_secret_configured_accepts(self):
        self.assertTrue(valid_signature("", b"x", ""))


class TestReadOnlyPromise(unittest.IsolatedAsyncioTestCase):
    async def test_send_refuses_while_sending_disabled(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})

        client = WhatsAppClient("TOKEN", "PN1", sending_enabled=False,
                                transport=httpx.MockTransport(handler))
        self.assertFalse(await client.send_text("447700900001", "hello"))
        self.assertEqual(calls, [])                    # not even an HTTP attempt
        await client.close()

    async def test_send_works_only_when_deliberately_enabled(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})

        client = WhatsAppClient("TOKEN", "PN1", sending_enabled=True,
                                transport=httpx.MockTransport(handler))
        self.assertTrue(await client.send_text("447700900001", "Phase 2 someday"))
        self.assertIn("/PN1/messages", captured["path"])
        self.assertEqual(captured["json"]["to"], "447700900001")
        await client.close()


class TestIngestAndDigest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_digest_carries_whatsapp_lines_and_watermarks(self):
        from tests.test_heartbeat import JobsHarness

        h = JobsHarness(self.db, flourish="DIGEST TEXT")
        await self.db.execute(
            "INSERT INTO wa_direct_ingest (ts, wa_id, sender, company_tag, kind, message)"
            " VALUES ('2026-08-04T10:00:00+00:00', '447700900001', 'Kiefer Brindle', '',"
            " 'text', 'invoices signed, pack incoming')"
        )
        sent = await h.jobs.group_digest(force=True)
        self.assertTrue(sent)
        body = " ".join(b for m, b in h.telegram_calls if m == "sendMessage")
        self.assertIn("1+message", body.replace("%20", "+"))   # counted in the header
        self.assertEqual(await h.jobs.store.get("whatsapp_digest_last_id"), "1")
        # Nothing new → no second digest.
        self.assertFalse(await h.jobs.group_digest(force=True))


class TestWaterFromAppleHealth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_health_total_lands_and_merges_by_max(self):
        from app.main import merge_water_total

        # WaterMinder's cumulative total lands…
        self.assertTrue(await merge_water_total(self.db, "2026-08-04", 900))
        row = await self.db.fetch_one("SELECT ml FROM water_log WHERE day = '2026-08-04'")
        self.assertEqual(row["ml"], 900)
        # …a later, larger export raises it…
        await merge_water_total(self.db, "2026-08-04", 1500)
        row = await self.db.fetch_one("SELECT ml FROM water_log WHERE day = '2026-08-04'")
        self.assertEqual(row["ml"], 1500)
        # …but a manual total already ahead is never dragged down.
        await self.db.execute("UPDATE water_log SET ml = 2000 WHERE day = '2026-08-04'")
        await merge_water_total(self.db, "2026-08-04", 1600)
        row = await self.db.fetch_one("SELECT ml FROM water_log WHERE day = '2026-08-04'")
        self.assertEqual(row["ml"], 2000)

    async def test_zero_or_missing_water_records_nothing(self):
        from app.main import merge_water_total

        self.assertFalse(await merge_water_total(self.db, "2026-08-04", 0))
        self.assertIsNone(await self.db.fetch_one("SELECT ml FROM water_log"))


if __name__ == "__main__":
    unittest.main()
