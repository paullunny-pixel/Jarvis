"""Talk to Gemini — live native-speech bridge (8 Aug).

The cockpit's 'Ask Gemini' box grows a voice: the browser opens a WebSocket
to us, we open one to Google's Gemini Live API, and this bridge pumps audio
both ways — Paul's mic in (16kHz PCM), Gemini's own native voice out (24kHz
PCM), barge-in passed through as an 'interrupted' signal so the browser can
bin its playback queue. Same arrangement as the text box: a SEPARATE AI,
walled off from Jarvis's memory and private data — the conversation on this
socket is all Gemini ever hears. The API key stays server-side; the browser
never sees it.

Same philosophy as media_bridge.py: deliberately dumb plumbing with injected
sockets so the whole loop tests with fakes — no network, no Google.
"""
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

MAX_SESSION_SECONDS = 30 * 60

LIVE_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

VOICE_SYSTEM = (
    "You are Gemini, a general-purpose AI assistant having a spoken "
    "conversation from Paul's dashboard. You sit alongside (not replacing) "
    "his main assistant Jarvis. Keep replies natural and conversational — "
    "short spoken turns, no lists, no markdown. Paul has dyslexia and may "
    "misspeak words: read for meaning, never comment on it."
)


def setup_message(model: str) -> str:
    """The first frame Google expects on a Live session."""
    return json.dumps({
        "setup": {
            "model": f"models/{model}",
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "systemInstruction": {"parts": [{"text": VOICE_SYSTEM}]},
        }
    })


class GeminiLiveBridge:
    """Pumps one voice session. `browser_ws` is the accepted server socket
    (needs receive_text/send_text); `google` is the client socket to the
    Live API (needs send/recv/close with str payloads)."""

    def __init__(self, browser_ws, google) -> None:
        self.browser_ws = browser_ws
        self.google = google
        self._closed = asyncio.Event()

    async def run(self, model: str) -> None:
        try:
            await self.google.send(setup_message(model))
        except Exception:
            logger.exception("Gemini Live setup send failed")
            await self._tell_browser({"error": "Couldn't reach Gemini Live — try again in a moment."})
            return
        up = asyncio.create_task(self._pump_up())
        down = asyncio.create_task(self._pump_down())
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=MAX_SESSION_SECONDS)
        except asyncio.TimeoutError:
            logger.info("Gemini Live session hit the %ss cap", MAX_SESSION_SECONDS)
        finally:
            for task in (up, down):
                task.cancel()
            try:
                await self.google.close()
            except Exception:  # noqa: BLE001
                pass

    async def _tell_browser(self, payload: dict) -> None:
        try:
            await self.browser_ws.send_text(json.dumps(payload))
        except Exception:  # noqa: BLE001
            self._closed.set()

    # ------------------------------------------------ browser → Google

    async def _pump_up(self) -> None:
        try:
            while True:
                raw = await self.browser_ws.receive_text()
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                audio = frame.get("audio")
                if audio:
                    await self.google.send(json.dumps({
                        "realtimeInput": {
                            "audio": {"data": audio, "mimeType": "audio/pcm;rate=16000"}
                        }
                    }))
        except Exception:   # disconnect (WebSocketDisconnect or socket death)
            self._closed.set()

    # ------------------------------------------------ Google → browser

    async def _pump_down(self) -> None:
        try:
            while True:
                raw = await self.google.recv()
                if raw is None:
                    break
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if "setupComplete" in frame:
                    await self._tell_browser({"ready": True})
                    continue
                content = frame.get("serverContent") or {}
                if content.get("interrupted"):
                    # Paul talked over Gemini — the browser bins its queue.
                    await self._tell_browser({"interrupted": True})
                for part in (content.get("modelTurn") or {}).get("parts") or []:
                    data = (part.get("inlineData") or {}).get("data")
                    if data:
                        await self._tell_browser({"audio": data})
                if content.get("turnComplete"):
                    await self._tell_browser({"turnComplete": True})
        except Exception:
            logger.info("Gemini Live downstream ended")
        self._closed.set()
