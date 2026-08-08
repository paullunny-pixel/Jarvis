"""Talk to Gemini (8 Aug): the live native-speech bridge. Fake sockets both
sides — no network, no Google — same harness philosophy as the Twilio media
bridge tests."""
import asyncio
import json
import unittest

from app.voice.gemini_live import VOICE_SYSTEM, GeminiLiveBridge, setup_message


class FakeBrowser:
    """The accepted server socket: scripted incoming frames, captured sends."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def receive_text(self):
        if not self._frames:
            await asyncio.sleep(3600)   # browser goes quiet, not away
        return self._frames.pop(0)

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class FakeGoogle:
    """The client socket to the Live API: scripted replies, captured sends."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.sent = []
        self.closed = False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if not self._replies:
            await asyncio.sleep(3600)
        return self._replies.pop(0)

    async def close(self):
        self.closed = True


async def run_bridge(browser, google, seconds=0.2):
    bridge = GeminiLiveBridge(browser, google)
    task = asyncio.create_task(bridge.run("test-model"))
    await asyncio.sleep(seconds)
    bridge._closed.set()
    await task


class TestSetup(unittest.TestCase):
    def test_setup_frame_names_the_model_and_audio(self):
        frame = json.loads(setup_message("gemini-3.1-flash-live"))
        self.assertEqual(frame["setup"]["model"], "models/gemini-3.1-flash-live")
        self.assertEqual(frame["setup"]["generationConfig"]["responseModalities"], ["AUDIO"])
        system = frame["setup"]["systemInstruction"]["parts"][0]["text"]
        self.assertEqual(system, VOICE_SYSTEM)
        self.assertIn("not replacing", system)   # honest arrangement, spoken too


class TestBridge(unittest.IsolatedAsyncioTestCase):
    async def test_setup_goes_first_and_ready_reaches_browser(self):
        browser = FakeBrowser([])
        google = FakeGoogle([json.dumps({"setupComplete": {}})])
        await run_bridge(browser, google)
        self.assertIn("setup", google.sent[0])
        self.assertIn({"ready": True}, browser.sent)
        self.assertTrue(google.closed)

    async def test_mic_audio_is_forwarded_as_realtime_input(self):
        browser = FakeBrowser([json.dumps({"audio": "UEND"})])
        google = FakeGoogle([])
        await run_bridge(browser, google)
        audio_frames = [f for f in google.sent if "realtimeInput" in f]
        self.assertEqual(len(audio_frames), 1)
        audio = audio_frames[0]["realtimeInput"]["audio"]
        self.assertEqual(audio["data"], "UEND")
        self.assertEqual(audio["mimeType"], "audio/pcm;rate=16000")

    async def test_gemini_voice_and_turn_complete_reach_browser(self):
        google = FakeGoogle([
            json.dumps({"setupComplete": {}}),
            json.dumps({"serverContent": {
                "modelTurn": {"parts": [
                    {"inlineData": {"data": "QUJD", "mimeType": "audio/pcm;rate=24000"}},
                    {"inlineData": {"data": "REVG", "mimeType": "audio/pcm;rate=24000"}},
                ]},
            }}),
            json.dumps({"serverContent": {"turnComplete": True}}),
        ])
        browser = FakeBrowser([])
        await run_bridge(browser, google)
        audio = [f["audio"] for f in browser.sent if "audio" in f]
        self.assertEqual(audio, ["QUJD", "REVG"])
        self.assertIn({"turnComplete": True}, browser.sent)

    async def test_barge_in_signal_passes_through(self):
        google = FakeGoogle([
            json.dumps({"serverContent": {"interrupted": True}}),
        ])
        browser = FakeBrowser([])
        await run_bridge(browser, google)
        self.assertIn({"interrupted": True}, browser.sent)

    async def test_dead_google_socket_gets_an_honest_error(self):
        class DeadGoogle:
            async def send(self, text):
                raise RuntimeError("no route to Google")

            async def close(self):
                pass

        browser = FakeBrowser([])
        bridge = GeminiLiveBridge(browser, DeadGoogle())
        await bridge.run("test-model")
        self.assertTrue(any("error" in f for f in browser.sent))

    async def test_browser_disconnect_ends_the_session(self):
        class GoneBrowser(FakeBrowser):
            async def receive_text(self):
                raise RuntimeError("WebSocketDisconnect")

        browser = GoneBrowser([])
        google = FakeGoogle([])
        bridge = GeminiLiveBridge(browser, google)
        await asyncio.wait_for(bridge.run("test-model"), timeout=2)
        self.assertTrue(google.closed)

    async def test_junk_frames_are_ignored_not_fatal(self):
        browser = FakeBrowser(["not json", json.dumps({"audio": "UEND"})])
        google = FakeGoogle(["also not json"])
        await run_bridge(browser, google)
        self.assertTrue(any("realtimeInput" in f for f in google.sent))


class TestCockpitButton(unittest.TestCase):
    def test_the_page_carries_the_talk_button_and_ws_path(self):
        from app.cockpit.page import render_page

        page = render_page("/cockpit/S/data")
        self.assertIn("Talk to Gemini", page)
        self.assertIn("/gemini-live", page)


if __name__ == "__main__":
    unittest.main()
