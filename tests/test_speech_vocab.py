"""Deepgram tuning (5 Aug): en-GB, nova-3 keyterm boosting, and the live
vocabulary built from the second brain."""
import os
import tempfile
import unittest
from urllib.parse import parse_qsl

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from app.memory.store import LivingFacts

DG_RESPONSE = {"results": {"channels": [{"alternatives": [{"transcript": "hello"}]}]}}


class TestDeepgramParams(unittest.IsolatedAsyncioTestCase):
    async def test_language_and_keyterms_ride_the_query(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = parse_qsl(request.url.query.decode())
            return httpx.Response(200, json=DG_RESPONSE)

        client = DeepgramClient("K", transport=httpx.MockTransport(handler))
        await client.transcribe(b"AUDIO", keyterms=["Prodermis", "Kiefer", "Nexfill"])
        params = seen["params"]
        self.assertIn(("language", "en-GB"), params)
        keyterms = [v for k, v in params if k == "keyterm"]
        self.assertEqual(keyterms, ["Prodermis", "Kiefer", "Nexfill"])
        await client.close()

    async def test_no_keyterms_is_clean(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = parse_qsl(request.url.query.decode())
            return httpx.Response(200, json=DG_RESPONSE)

        client = DeepgramClient("K", transport=httpx.MockTransport(handler))
        await client.transcribe(b"AUDIO")
        self.assertNotIn("keyterm", [k for k, _ in seen["params"]])
        await client.close()


class TestLiveVocabulary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.living = LivingFacts(self.db)
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=1, _env_file=None)
        mock = httpx.MockTransport(lambda r: httpx.Response(500))
        self.router = JarvisRouter(
            settings=settings, db=self.db, living=self.living,
            telegram=TelegramClient("TOK", transport=mock),
            claude=ClaudeClient("K", transport=mock),
            deepgram=DeepgramClient("K", transport=mock),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=mock),
        )

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_seeds_plus_brain_names_no_duplicates(self):
        await self.living.set("people.marijana.role", "Runs the Warsaw clinic with Dr Nowak", room="people")
        await self.living.set("companies.glowlab.status", "New supplier — GlowLab, Riga", room="companies")
        await self.living.set("health.weight_kg", "92", room="health")   # wrong room — excluded
        vocab = await self.router._speech_vocabulary()
        self.assertIn("Prodermis", vocab)      # seed
        self.assertIn("Marijana", vocab)       # key segment, titled
        self.assertIn("Nowak", vocab)          # capitalised word from a value
        self.assertIn("GlowLab", vocab)
        self.assertLessEqual(len(vocab), 100)
        self.assertEqual(len(vocab), len(set(vocab)))

    async def test_vocabulary_is_cached_for_the_hour(self):
        first = await self.router._speech_vocabulary()
        await self.living.set("people.newperson.role", "Just met", room="people")
        self.assertEqual(await self.router._speech_vocabulary(), first)   # cache holds
        self.router._speech_vocab_ts -= 3601                              # hour passes
        self.assertIn("Newperson", await self.router._speech_vocabulary())
