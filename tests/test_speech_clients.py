import unittest

import httpx

from app.clients.deepgram_client import DeepgramClient, TranscriptionError
from app.clients.elevenlabs_client import ElevenLabsClient, SynthesisError


class TestDeepgram(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_success(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["content_type"] = request.headers.get("content-type")
            captured["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "results": {
                        "channels": [
                            {"alternatives": [{"transcript": "Morning Jarvis, plan my day.", "confidence": 0.99}]}
                        ]
                    }
                },
            )

        client = DeepgramClient("DGKEY", transport=httpx.MockTransport(handler))
        text = await client.transcribe(b"OGGBYTES", "audio/ogg")
        self.assertEqual(text, "Morning Jarvis, plan my day.")
        self.assertIn("model=nova-3", captured["url"])
        self.assertIn("smart_format=true", captured["url"])
        self.assertEqual(captured["auth"], "Token DGKEY")
        self.assertEqual(captured["content_type"], "audio/ogg")
        self.assertEqual(captured["body"], b"OGGBYTES")

    async def test_transcribe_http_error(self):
        client = DeepgramClient(
            "DGKEY", transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key"))
        )
        with self.assertRaises(TranscriptionError):
            await client.transcribe(b"x")

    async def test_transcribe_bad_shape(self):
        client = DeepgramClient(
            "DGKEY", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"weird": 1}))
        )
        with self.assertRaises(TranscriptionError):
            await client.transcribe(b"x")


class TestElevenLabs(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize_success(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["key"] = request.headers.get("xi-api-key")
            captured["body"] = request.content.decode()
            return httpx.Response(200, content=b"MP3AUDIO")

        client = ElevenLabsClient("XIKEY", voice_id="VOICE42", transport=httpx.MockTransport(handler))
        audio = await client.synthesize("Do it now, Paul.")
        self.assertEqual(audio, b"MP3AUDIO")
        self.assertIn("/text-to-speech/VOICE42", captured["url"])
        self.assertIn("output_format=mp3_44100_128", captured["url"])
        self.assertEqual(captured["key"], "XIKEY")
        self.assertIn("Do it now, Paul.", captured["body"])
        self.assertIn("eleven_multilingual_v2", captured["body"])

    async def test_synthesize_error(self):
        client = ElevenLabsClient(
            "XIKEY", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(402, text="quota"))
        )
        with self.assertRaises(SynthesisError):
            await client.synthesize("hi")


if __name__ == "__main__":
    unittest.main()
