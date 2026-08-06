"""The realtime phone upgrade (7 Aug): Twilio Media Streams bridged to the
live ElevenLabs agent — pumps, transcoding, TwiML branching, WS endpoint."""
import asyncio
import audioop
import base64
import json
import os
import tempfile
import unittest

from app.voice.media_bridge import MediaBridge, parse_format
from app.voice.phone import PhoneChannel


class FakeTwilioWS:
    """Server side: scripted inbound events, records everything sent."""

    def __init__(self, events):
        self._events = list(events)
        self.sent = []
        self.closed = False

    async def receive_text(self):
        if not self._events:
            # Twilio hung up — starlette raises on a closed socket.
            raise RuntimeError("twilio socket closed")
        return json.dumps(self._events.pop(0))

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        self.closed = True


class FakeElevenWS:
    """Client side: scripted agent messages, records what the bridge sends."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if not self._messages:
            await asyncio.sleep(0.05)   # keep the session open until Twilio ends
            raise RuntimeError("eleven socket closed")
        return json.dumps(self._messages.pop(0))

    async def close(self):
        self.closed = True


def pcm_tone(rate=16000, ms=20):
    import math

    n = rate * ms // 1000
    return b"".join(
        int(3000 * math.sin(2 * math.pi * 440 * i / rate)).to_bytes(2, "little", signed=True)
        for i in range(n)
    )


METADATA = {
    "type": "conversation_initiation_metadata",
    "conversation_initiation_metadata_event": {
        "agent_output_audio_format": "pcm_16000",
        "user_input_audio_format": "pcm_16000",
    },
}


class TestBridge(unittest.IsolatedAsyncioTestCase):
    async def test_full_call_pumps_both_ways(self):
        mulaw = audioop.lin2ulaw(pcm_tone(8000), 2)
        twilio = FakeTwilioWS([
            {"event": "connected"},
            {"event": "start", "start": {"streamSid": "MZ123"}},
            {"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}},
            {"event": "stop"},
        ])
        eleven = FakeElevenWS([
            METADATA,
            {"type": "audio",
             "audio_event": {"audio_base_64": base64.b64encode(pcm_tone()).decode()}},
            {"type": "ping", "ping_event": {"event_id": 7}},
        ])
        await MediaBridge(twilio, eleven).run()
        # Bridge opened the agent session, forwarded Paul's audio, ponged.
        kinds = [m.get("type") for m in eleven.sent]
        self.assertIn("conversation_initiation_client_data", kinds)
        self.assertIn("pong", kinds)
        chunks = [m for m in eleven.sent if "user_audio_chunk" in m]
        self.assertEqual(len(chunks), 1)
        # Upsampled 8k→16k: roughly twice the bytes of the mulaw-decoded PCM.
        sent_pcm = base64.b64decode(chunks[0]["user_audio_chunk"])
        self.assertAlmostEqual(len(sent_pcm), len(mulaw) * 2 * 2, delta=64)
        # Agent audio came back as mulaw media on the right stream.
        media = [m for m in twilio.sent if m["event"] == "media"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["streamSid"], "MZ123")
        self.assertTrue(eleven.closed)

    async def test_interruption_clears_twilio_buffer(self):
        twilio = FakeTwilioWS([
            {"event": "start", "start": {"streamSid": "MZ9"}},
            {"event": "stop"},
        ])
        eleven = FakeElevenWS([METADATA, {"type": "interruption"}])
        await MediaBridge(twilio, eleven).run()
        self.assertIn({"event": "clear", "streamSid": "MZ9"}, twilio.sent)

    async def test_ulaw_agent_passes_straight_through(self):
        mulaw = audioop.lin2ulaw(pcm_tone(8000), 2)
        twilio = FakeTwilioWS([
            {"event": "start", "start": {"streamSid": "MZ1"}},
            {"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}},
            {"event": "stop"},
        ])
        ulaw_meta = {
            "type": "conversation_initiation_metadata",
            "conversation_initiation_metadata_event": {
                "agent_output_audio_format": "ulaw_8000",
                "user_input_audio_format": "ulaw_8000",
            },
        }
        eleven = FakeElevenWS([
            ulaw_meta,
            {"type": "audio", "audio_event": {"audio_base_64": base64.b64encode(mulaw).decode()}},
        ])
        await MediaBridge(twilio, eleven).run()
        chunk = next(m for m in eleven.sent if "user_audio_chunk" in m)
        self.assertEqual(base64.b64decode(chunk["user_audio_chunk"]), mulaw)
        media = next(m for m in twilio.sent if m["event"] == "media")
        self.assertEqual(base64.b64decode(media["media"]["payload"]), mulaw)

    def test_format_parser_shrugs_at_nonsense(self):
        self.assertEqual(parse_format("pcm_16000"), ("pcm", 16000))
        self.assertEqual(parse_format("ulaw_8000"), ("ulaw", 8000))
        self.assertEqual(parse_format(""), ("pcm", 16000))
        self.assertEqual(parse_format(None), ("pcm", 16000))


class TestAnswerBranch(unittest.IsolatedAsyncioTestCase):
    def _phone(self, realtime=True):
        phone = PhoneChannel(
            twilio=None, elevenlabs=None,
            from_number="+1", paul_number="+2",
            public_url="https://jarvis.example", secret="S3CRET",
        )
        phone.realtime_available = realtime
        return phone

    async def test_inbound_call_streams_when_realtime_up(self):
        twiml = await self._phone().handle_answer({"Direction": "inbound"})
        self.assertIn('<Connect><Stream url="wss://jarvis.example/twilio/media/S3CRET"/></Connect>', twiml)
        self.assertIn("fallback=1", twiml)   # the no-dead-air escape hatch

    async def test_fallback_flag_forces_turn_based(self):
        phone = self._phone()

        async def no_tts(text):
            return f"<Say>{text}</Say>"

        phone._voiced = no_tts
        twiml = await phone.handle_answer({"Direction": "inbound", "fallback": "1"})
        self.assertNotIn("<Connect>", twiml)
        self.assertIn("<Gather", twiml)

    async def test_outbound_greeting_and_wake_script_stay_turn_based(self):
        phone = self._phone()

        async def no_tts(text):
            return f"<Say>{text}</Say>"

        phone._voiced = no_tts
        out = await phone.handle_answer({"Direction": "outbound-api"})
        self.assertNotIn("<Connect>", out)

        async def script(speech):
            return None

        phone.script_handler = script
        inbound_during_wake = await phone.handle_answer({"Direction": "inbound"})
        self.assertNotIn("<Connect>", inbound_during_wake)

    async def test_realtime_off_keeps_everything_as_before(self):
        phone = self._phone(realtime=False)

        async def no_tts(text):
            return f"<Say>{text}</Say>"

        phone._voiced = no_tts
        twiml = await phone.handle_answer({"Direction": "inbound"})
        self.assertNotIn("<Connect>", twiml)
        self.assertIn("<Gather", twiml)


class TestEndpoint(unittest.TestCase):
    def test_ws_auth_and_bridge_roundtrip(self):
        import app.main as app_main
        from app.config import Settings
        from app.db.sqlite import SqliteDatabase
        from fastapi.testclient import TestClient

        tmp = tempfile.TemporaryDirectory()
        db = SqliteDatabase(os.path.join(tmp.name, "t.db"))
        asyncio.run(db.init())
        stub = type("Stub", (), {})()
        stub.settings = Settings(telegram_bot_token="TOK", _env_file=None)
        phone = PhoneChannel(
            twilio=None, elevenlabs=None, from_number="+1", paul_number="+2",
            public_url="https://jarvis.example",
            secret=stub.settings.effective_phone_secret,
        )
        eleven = FakeElevenWS([METADATA])

        async def connect():
            return eleven

        phone.eleven_connect = connect
        stub.phone_channel = phone
        stub.voice_engine = None
        app_main.app.state.router = stub
        client = TestClient(app_main.app)
        try:
            secret = stub.settings.effective_phone_secret
            # Wrong secret: closed before accept does any work.
            with self.assertRaises(Exception):
                with client.websocket_connect("/twilio/media/WRONG") as ws:
                    ws.receive_text()
            with client.websocket_connect(f"/twilio/media/{secret}") as ws:
                ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ5"}}))
                ws.send_text(json.dumps({"event": "stop"}))
            self.assertTrue(
                any("conversation_initiation_client_data" == m.get("type") for m in eleven.sent)
            )
            self.assertTrue(eleven.closed)
        finally:
            del app_main.app.state.router
            asyncio.run(db.close())
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
