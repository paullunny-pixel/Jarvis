"""The mixed email+board message (13:56, 6 Aug): 'search my emails for g
prime' grew an Urgent Trello card titled 'Search for information regarding
G-Prime in emails' — the legacy direct-write parser ate the instruction.
The board half of a mixed message now rides intake's confirm-first flow."""
import os
import tempfile
import unittest

import httpx

from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from tests.test_telegram_client import text_update
from tests.test_wakeup import OWNER, FakePhone, Harness


class FakeIntake:
    def __init__(self, summary):
        self.summary = summary
        self.batches = []

    async def start_batch(self, transcript):
        self.batches.append(transcript)
        return self.summary, []

    async def handle_reply(self, transcript):
        return None   # no pending confirmation

    async def pending(self):
        return False


class TestMixedMessageBoardHalf(unittest.IsolatedAsyncioTestCase):
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
        # The email half is 'handled' — we're testing what happens NEXT.
        self.router.mail = object()

        async def email_talk(message, transcript):
            return True

        self.router._handle_email_talk = email_talk

        async def task_talk_must_not_run(message, transcript):
            raise AssertionError(
                "the direct-write task parser ran on a mixed message — "
                "that's the G-Prime card bug back from the dead"
            )

        self.router._handle_task_talk = task_talk_must_not_run

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_board_half_goes_through_intake_confirm(self):
        fake = FakeIntake("1. Chase the BMI dossier — reply OK to write.")
        self.router._intake = lambda: fake
        await self.router.handle_update(
            text_update("read my emails from BMI and put a card on the board to chase them", OWNER)
        )
        self.assertEqual(len(fake.batches), 1)          # extraction ran…
        self.assertIn(fake.summary, self.h.texts())     # …and asked first

    async def test_no_board_content_means_no_card_and_no_noise(self):
        fake = FakeIntake(None)   # extraction found nothing card-worthy
        self.router._intake = lambda: fake
        await self.router.handle_update(
            text_update("search my emails for g prime and tell me the task history", OWNER)
        )
        self.assertEqual(len(fake.batches), 1)
        self.assertEqual(self.h.texts(), [])            # silence, not a card


class TestTaskDictationStaysOutOfMail(unittest.TestCase):
    def test_the_rule_rides_the_email_parser_prompt(self):
        # 13:55, 6 Aug: Paul read out a Trello card about searching emails and
        # the mail lane ran a full mailbox research he never asked for. The
        # parser prompt carries the rule; this pins it there.
        from app.mail.commands import PARSER_SYSTEM

        self.assertIn("TASK DICTATION IS NOT AN EMAIL COMMAND", PARSER_SYSTEM)
        self.assertIn("The task pipeline captures it", PARSER_SYSTEM)


if __name__ == "__main__":
    unittest.main()
