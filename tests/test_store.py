import os
import tempfile
import unittest

from app.core.store import MessageLog, SettingsStore
from app.db.postgres import translate_placeholders
from app.db.sqlite import SqliteDatabase


class TestStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.log = MessageLog(self.db)
        self.store = SettingsStore(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_log_and_recent_order(self):
        await self.log.log("in", "first", chat_id=1, kind="voice", voice_duration=4)
        await self.log.log("out", "second", chat_id=1)
        rows = await self.log.recent()
        self.assertEqual([r["transcript"] for r in rows], ["first", "second"])

    async def test_claude_messages_alternate_and_start_with_user(self):
        await self.log.log("out", "unprompted nudge")  # would start with assistant
        await self.log.log("in", "hi")
        await self.log.log("in", "you there?")  # consecutive user turns merge
        await self.log.log("out", "here")
        msgs = await self.log.as_claude_messages()
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "hi\nyou there?")
        self.assertEqual(msgs[1], {"role": "assistant", "content": "here"})

    async def test_empty_transcripts_excluded_from_history(self):
        await self.log.log("in", "", kind="voice", meta={"empty_transcript": True})
        await self.log.log("in", "real message")
        msgs = await self.log.as_claude_messages()
        self.assertEqual(len(msgs), 1)

    async def test_settings_roundtrip_and_overwrite(self):
        self.assertEqual(await self.store.get("owner_chat_id", "none"), "none")
        await self.store.set("owner_chat_id", "123")
        await self.store.set("owner_chat_id", "456")
        self.assertEqual(await self.store.get("owner_chat_id"), "456")


class TestPlaceholderTranslation(unittest.TestCase):
    def test_translation(self):
        self.assertEqual(
            translate_placeholders("INSERT INTO t (a, b) VALUES (?, ?)"),
            "INSERT INTO t (a, b) VALUES ($1, $2)",
        )
        self.assertEqual(translate_placeholders("SELECT 1"), "SELECT 1")


if __name__ == "__main__":
    unittest.main()
