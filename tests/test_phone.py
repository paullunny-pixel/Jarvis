"""The Twilio phone channel (4 Aug): client, TwiML conversation loop, the
'call me' trigger and the wake-up escalation preference."""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.clients.twilio_client import TwilioClient, valid_signature
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from app.heartbeat.jobs import HeartbeatJobs
from app.heartbeat.wake_channels import TwilioCallWakeChannel
from app.voice.phone import DEFAULT_GREETING, PhoneChannel
from tests.test_telegram_client import text_update

OWNER = 111


# ------------------------------------------------------------ the thin client


class TestTwilioClient(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler):
        return TwilioClient("AC1", "tok", transport=httpx.MockTransport(handler))

    async def test_place_call_posts_the_essentials(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["form"] = parse_qs(request.content.decode())
            return httpx.Response(201, json={"sid": "CA99"})

        client = self._client(handler)
        sid = await client.place_call("+447", "+1857", "https://j.example/answer?g=t1")
        self.assertEqual(sid, "CA99")
        self.assertEqual(seen["path"], "/2010-04-01/Accounts/AC1/Calls.json")
        self.assertEqual(seen["form"]["To"], ["+447"])
        self.assertEqual(seen["form"]["From"], ["+1857"])
        self.assertEqual(seen["form"]["Url"], ["https://j.example/answer?g=t1"])
        await client.close()

    async def test_place_call_failure_returns_empty(self):
        client = self._client(lambda r: httpx.Response(400, json={"message": "no"}))
        self.assertEqual(await client.place_call("+447", "+1857", "https://x"), "")
        await client.close()

    async def test_configure_inbound_updates_the_number(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={
                    "incoming_phone_numbers": [{"sid": "PN7", "phone_number": "+1857"}]
                })
            posts.append((request.url.path, parse_qs(request.content.decode())))
            return httpx.Response(200, json={"sid": "PN7"})

        client = self._client(handler)
        problem = await client.configure_inbound("+1857", "https://j.example/answer")
        self.assertEqual(problem, "")
        path, form = posts[0]
        self.assertIn("/IncomingPhoneNumbers/PN7.json", path)
        self.assertEqual(form["VoiceUrl"], ["https://j.example/answer"])
        self.assertEqual(form["VoiceMethod"], ["POST"])
        await client.close()

    async def test_configure_inbound_missing_number_is_honest(self):
        client = self._client(
            lambda r: httpx.Response(200, json={"incoming_phone_numbers": []})
        )
        self.assertIn("isn't on this Twilio account", await client.configure_inbound("+1", "u"))
        await client.close()

    async def test_account_summary_reads_trial(self):
        client = self._client(
            lambda r: httpx.Response(200, json={"type": "Trial", "status": "active"})
        )
        self.assertEqual(await client.account_summary(), {"type": "Trial", "status": "active"})
        await client.close()

    def test_signature_scheme_round_trip(self):
        url = "https://j.example/twilio/voice/S/turn"
        params = {"CallSid": "CA1", "SpeechResult": "hello"}
        payload = url + "".join(k + v for k, v in sorted(params.items()))
        good = base64.b64encode(
            hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertTrue(valid_signature("tok", url, params, good))
        self.assertFalse(valid_signature("tok", url, params, "forged"))
        self.assertFalse(valid_signature("tok", url, params, ""))


# ------------------------------------------------------------ the channel


class FakeTTS:
    def __init__(self, fail=False):
        self.fail = fail
        self.texts = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("tts down")
        return b"MP3BYTES"


class FakeTwilio:
    def __init__(self, sid="CA1"):
        self.sid = sid
        self.calls = []

    async def place_call(self, to, from_, url):
        self.calls.append((to, from_, url))
        return self.sid


def make_channel(tts=None, twilio=None) -> PhoneChannel:
    return PhoneChannel(
        twilio if twilio is not None else FakeTwilio(),
        tts if tts is not None else FakeTTS(),
        from_number="+18574206042",
        paul_number="+447498847149",
        public_url="https://j.example",
        secret="SECRET",
    )


class TestPhoneChannel(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_channel_refuses_to_call(self):
        channel = PhoneChannel(
            FakeTwilio(), FakeTTS(), from_number="", paul_number="+44",
            public_url="https://j.example", secret="S",
        )
        self.assertFalse(channel.configured)
        self.assertFalse(await channel.call_paul())

    async def test_call_paul_places_call_with_greeting_token(self):
        twilio = FakeTwilio()
        channel = make_channel(twilio=twilio)
        self.assertTrue(await channel.call_paul("Morning, sir. Up."))
        to, from_, url = twilio.calls[0]
        self.assertEqual(to, "+447498847149")
        self.assertEqual(from_, "+18574206042")
        self.assertIn("https://j.example/twilio/voice/SECRET/answer?g=", url)
        token = url.split("g=")[1]
        twiml = await channel.handle_answer({"g": token})
        self.assertIn("<Gather", twiml)
        self.assertIn("/twilio/voice/SECRET/turn", twiml)
        self.assertIn("<Play>", twiml)   # greeting in Jarvis's own voice

    async def test_failed_placement_returns_false(self):
        channel = make_channel(twilio=FakeTwilio(sid=""))
        self.assertFalse(await channel.call_paul())

    async def test_inbound_answer_uses_default_greeting(self):
        tts = FakeTTS()
        channel = make_channel(tts=tts)
        twiml = await channel.handle_answer({})
        self.assertIn("<Gather", twiml)
        self.assertEqual(tts.texts[0], DEFAULT_GREETING)

    async def test_turn_runs_the_brain_and_replies(self):
        tts = FakeTTS()
        channel = make_channel(tts=tts)
        heard = []

        async def brain(text):
            heard.append(text)
            return "Board's clear, sir."

        channel.brain = brain
        twiml = await channel.handle_turn({"SpeechResult": "what's on the board"})
        self.assertEqual(heard, ["what's on the board"])
        self.assertIn("Board's clear, sir.", tts.texts)
        self.assertIn("<Gather", twiml)  # keeps listening

    async def test_goodbye_hangs_up_without_the_brain(self):
        channel = make_channel()
        called = []

        async def brain(text):
            called.append(text)
            return "?"

        channel.brain = brain
        twiml = await channel.handle_turn({"SpeechResult": "goodbye Jarvis"})
        self.assertEqual(called, [])
        self.assertIn("<Hangup/>", twiml)
        self.assertNotIn("<Gather", twiml)

    async def test_brain_failure_gets_an_honest_line_not_a_dead_call(self):
        channel = make_channel()

        async def brain(text):
            raise RuntimeError("boom")

        channel.brain = brain
        twiml = await channel.handle_turn({"SpeechResult": "hello"})
        self.assertIn("<Gather", twiml)   # the call survives

    async def test_tts_failure_falls_back_to_say(self):
        channel = make_channel(tts=FakeTTS(fail=True))

        async def brain(text):
            return "Reply text"

        channel.brain = brain
        twiml = await channel.handle_turn({"SpeechResult": "hello"})
        self.assertIn("<Say", twiml)
        self.assertIn("Reply text", twiml)

    async def test_empty_speech_reprompts(self):
        channel = make_channel()
        twiml = await channel.handle_turn({"SpeechResult": ""})
        self.assertIn("<Gather", twiml)

    async def test_audio_cache_serves_then_expires(self):
        channel = make_channel()
        audio_id = channel.stash_audio(b"BYTES")
        self.assertEqual(channel.get_audio(audio_id), b"BYTES")
        born, blob = channel._audio[audio_id]
        channel._audio[audio_id] = (born - 16 * 60, blob)   # age it past the TTL
        self.assertIsNone(channel.get_audio(audio_id))

    async def test_reply_xml_is_escaped(self):
        channel = make_channel(tts=FakeTTS(fail=True))

        async def brain(text):
            return "Profit <up> & rising"

        channel.brain = brain
        twiml = await channel.handle_turn({"SpeechResult": "numbers"})
        self.assertIn("Profit &lt;up&gt; &amp; rising", twiml)


# ------------------------------------------------------------ the HTTP layer
# (the first live call died in request parsing, not the channel — so the
# endpoints get exercised end-to-end, signature and all)


class TestTwilioEndpoints(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from app import main as app_main

        self.settings = Settings(
            telegram_bot_token="TOK", twilio_account_sid="AC1", twilio_auth_token="tok",
            twilio_from_number="+18574206042", paul_phone_number="+447498847149",
            public_url="https://j.example", _env_file=None,
        )
        self.secret = self.settings.effective_phone_secret
        self.channel = PhoneChannel(
            FakeTwilio(), FakeTTS(), from_number="+18574206042",
            paul_number="+447498847149", public_url="https://j.example", secret=self.secret,
        )

        async def brain(text):
            return f"Heard: {text}"

        self.channel.brain = brain
        stub = type("StubRouter", (), {})()
        stub.settings = self.settings
        stub.phone_channel = self.channel
        self._app = app_main.app
        self._app.state.router = stub
        self.client = TestClient(self._app)

    def tearDown(self):
        del self._app.state.router

    def _signed(self, path, params):
        payload = f"https://j.example{path}" + "".join(k + v for k, v in sorted(params.items()))
        return base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()

    def test_answer_parses_twilio_form_and_speaks(self):
        path = f"/twilio/voice/{self.secret}/answer?g=missing"
        params = {"CallSid": "CA1", "CallStatus": "in-progress", "From": "+18574206042"}
        response = self.client.post(
            path, data=params, headers={"X-Twilio-Signature": self._signed(path, params)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("<Gather", response.text)
        self.assertIn("text/xml", response.headers["content-type"])

    def test_turn_round_trips_speech_to_the_brain(self):
        path = f"/twilio/voice/{self.secret}/turn"
        params = {"CallSid": "CA1", "SpeechResult": "what's on the board", "Confidence": "0.92"}
        response = self.client.post(
            path, data=params, headers={"X-Twilio-Signature": self._signed(path, params)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Heard: what's on the board", "".join(self.channel._elevenlabs.texts))

    def test_forged_signature_is_refused(self):
        path = f"/twilio/voice/{self.secret}/turn"
        response = self.client.post(
            path, data={"SpeechResult": "hi"}, headers={"X-Twilio-Signature": "forged"}
        )
        self.assertEqual(response.status_code, 403)

    def test_wrong_secret_404s(self):
        response = self.client.post("/twilio/voice/WRONG/turn", data={"SpeechResult": "hi"})
        self.assertEqual(response.status_code, 404)

    def test_handler_crash_speaks_instead_of_500ing(self):
        async def broken(params):
            raise RuntimeError("boom")

        self.channel.handle_turn = broken
        path = f"/twilio/voice/{self.secret}/turn"
        params = {"SpeechResult": "hello"}
        response = self.client.post(
            path, data=params, headers={"X-Twilio-Signature": self._signed(path, params)}
        )
        self.assertEqual(response.status_code, 200)   # Twilio must get TwiML, never a 500
        self.assertIn("<Say", response.text)
        self.assertIn("<Hangup/>", response.text)

    def test_audio_endpoint_serves_and_misses(self):
        audio_id = self.channel.stash_audio(b"MP3BYTES")
        hit = self.client.get(f"/twilio/audio/{self.secret}/{audio_id}.mp3")
        self.assertEqual(hit.status_code, 200)
        self.assertEqual(hit.content, b"MP3BYTES")
        self.assertEqual(hit.headers["content-type"], "audio/mpeg")
        miss = self.client.get(f"/twilio/audio/{self.secret}/deadbeef.mp3")
        self.assertEqual(miss.status_code, 404)


# ------------------------------------------------------------ wake escalation


class TestWakeChannelPreference(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def _jobs(self, phone_channel=None):
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        telegram = TelegramClient(
            "TOK", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": {}}))
        )
        claude = ClaudeClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        return HeartbeatJobs(
            settings=settings, db=self.db, telegram=telegram, claude=claude,
            phone_channel=phone_channel,
        )

    async def test_configured_twilio_channel_becomes_the_phone_wake(self):
        channel = make_channel()
        jobs = self._jobs(phone_channel=channel)
        self.assertIsInstance(jobs.phone_wake, TwilioCallWakeChannel)
        await jobs.phone_wake.escalate(5, "Up, sir.")
        to, _, url = channel.twilio.calls[0]
        self.assertEqual(to, "+447498847149")
        self.assertIn("answer?g=", url)

    async def test_no_channel_means_no_phone_wake(self):
        self.assertIsNone(self._jobs().phone_wake)

    async def test_unconfigured_channel_is_ignored(self):
        channel = PhoneChannel(
            FakeTwilio(), FakeTTS(), from_number="", paul_number="",
            public_url="", secret="S",
        )
        self.assertIsNone(self._jobs(phone_channel=channel).phone_wake)


# ------------------------------------------------------------ 'call me'


class PhoneHarness:
    def __init__(self, db):
        self.telegram_calls = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Noted."}]})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings,
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class TestCallMeTrigger(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = PhoneHarness(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_call_me_places_the_call(self):
        channel = make_channel()
        self.h.router.phone_channel = channel
        await self.h.router.handle_update(text_update("call me", OWNER))
        self.assertEqual(len(channel.twilio.calls), 1)
        [ack] = self.h.texts()
        self.assertIn("Ringing you now", ack)

    async def test_ring_me_variant_works(self):
        channel = make_channel()
        self.h.router.phone_channel = channel
        await self.h.router.handle_update(text_update("Jarvis, ring me", OWNER))
        self.assertEqual(len(channel.twilio.calls), 1)

    async def test_unconfigured_gets_an_honest_no(self):
        await self.h.router.handle_update(text_update("call me", OWNER))
        [ack] = self.h.texts()
        self.assertIn("No phone line wired up yet", ack)

    async def test_twilio_refusal_is_reported_honestly(self):
        channel = make_channel(twilio=FakeTwilio(sid=""))
        self.h.router.phone_channel = channel
        await self.h.router.handle_update(text_update("call me", OWNER))
        [ack] = self.h.texts()
        self.assertIn("didn't go through", ack)

    async def test_deferred_call_requests_stay_conversation(self):
        channel = make_channel()
        self.h.router.phone_channel = channel
        await self.h.router.handle_update(
            text_update("call me when the invoices land", OWNER)
        )
        self.assertEqual(channel.twilio.calls, [])   # no call placed

    async def test_status_mentions_the_phone_line(self):
        channel = make_channel()

        async def account_summary():
            return {"type": "Trial", "status": "active"}

        channel.twilio.account_summary = account_summary
        self.h.router.phone_channel = channel
        await self.h.router.handle_update(text_update("status", OWNER))
        [report] = self.h.texts()
        self.assertIn("Phone line (Twilio): +18574206042", report)
        self.assertIn("TRIAL", report)


# ------------------------------------------------------------ phone_turn


class TestPhoneTurn(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = PhoneHarness(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_phone_turn_answers_and_logs_on_the_phone_channel(self):
        reply = await self.h.router.phone_turn("how are we looking today")
        self.assertEqual(reply, "Noted.")
        rows = await self.db.fetch_all(
            "SELECT direction, transcript, channel FROM messages ORDER BY id"
        )
        self.assertEqual(
            [(r["direction"], r["channel"]) for r in rows],
            [("in", "phone"), ("out", "phone")],
        )

    async def test_empty_turn_asks_again_without_logging(self):
        self.assertEqual(await self.h.router.phone_turn("  "), "Say again?")
        rows = await self.db.fetch_all("SELECT id FROM messages")
        self.assertEqual(rows, [])

    async def test_private_topics_never_reach_the_general_log(self):
        from app.memory.crypto import PrivateBox
        from app.private.service import PrivateTrack

        self.h.router.private_track = PrivateTrack(self.db, self.h.router.claude, PrivateBox("k"))
        reply = await self.h.router.phone_turn("sos I'm really struggling tonight")
        self.assertIn("private room", reply)
        rows = await self.db.fetch_all("SELECT transcript FROM messages")
        self.assertEqual([r["transcript"] for r in rows], ["[private exchange]"])
