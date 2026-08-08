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


def setup_message(model: str, system: str = VOICE_SYSTEM, voice: str = "") -> str:
    """The first frame Google expects on a Live session. `voice` picks one of
    the prebuilt Live voices (Bia speaks as Aoede); empty keeps the default."""
    config: dict = {"responseModalities": ["AUDIO"]}
    if voice:
        config["speechConfig"] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
        }
    return json.dumps({
        "setup": {
            "model": f"models/{model}",
            "generationConfig": config,
            "systemInstruction": {"parts": [{"text": system}]},
        }
    })


# --- Bia, the live EN⇄PT interpreter on Paul's desktop (8 Aug brief) ---

BIA_INTRO_FULL = (
    "Oi Stef! Eu sou a Bia, e vou ser a tradutora de vocês. Vou repetir essa "
    "introdução algumas vezes até ela virar costume — depois disso as "
    "traduções vão começar direto, tipo 'oi gente, vamos conversar'. Lembra: "
    "por favor, não me interrompe, porque posso perder o que a outra pessoa "
    "disse. Você fala, eu escuto; depois eu falo com o Paul em inglês; ele "
    "fala, eu escuto; e aí eu falo com você. Vamos lá!"
)

BIA_INTRO_LIGHT = (
    "Oi Stef, Bia aqui de novo — só lembrando as regras: uma pessoa fala de "
    "cada vez e ninguém me interrompe. Você fala, eu traduzo pro Paul; ele "
    "fala, eu traduzo pra você. Vamos lá!"
)

BIA_KICKOFF = "[A sessão começou — faça sua abertura agora.]"


def bia_system(first_time: bool) -> str:
    """Bia's whole world: a live interpreter between Paul (English) and Stef
    (Brazilian Portuguese). Scripted opening (full the first session, lighter
    after), then pure faithful interpretation both ways."""
    intro = BIA_INTRO_FULL if first_time else BIA_INTRO_LIGHT
    return (
        "You are Bia, a warm Brazilian interpreter running live between two "
        "people: Paul, who speaks English, and Stef, who speaks Brazilian "
        "Portuguese.\n\n"
        "OPENING: the moment the session starts, say EXACTLY this, in "
        f"Brazilian Portuguese, before anything else: \"{intro}\"\n\n"
        "AFTER THE OPENING, you are a pure interpreter:\n"
        "- Hear Portuguese → render it for Paul in natural English.\n"
        "- Hear English → render it for Stef in natural Brazilian Portuguese.\n"
        "- Faithful both ways: meaning, tone and warmth — first person, "
        "never 'she said'. Complete but tight; no summarising away detail.\n"
        "- You NEVER add your own opinions, answers or side conversation. "
        "If someone asks you something directly, one short friendly line, "
        "then back to interpreting.\n"
        "- People misspeak and mix words — read for meaning, never comment "
        "on it.\n"
        "- Short spoken turns, no lists, no markdown — this is a live "
        "conversation."
    )


class GeminiLiveBridge:
    """Pumps one voice session. `browser_ws` is the accepted server socket
    (needs receive_text/send_text); `google` is the client socket to the
    Live API (needs send/recv/close with str payloads)."""

    def __init__(self, browser_ws, google) -> None:
        self.browser_ws = browser_ws
        self.google = google
        self._closed = asyncio.Event()
        self._kickoff = ""

    async def run(
        self, model: str, system: str = VOICE_SYSTEM, voice: str = "",
        kickoff: str = "",
    ) -> None:
        self._kickoff = kickoff
        try:
            await self.google.send(setup_message(model, system=system, voice=voice))
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
                    if self._kickoff:
                        # Bia opens the session herself — the scripted intro
                        # plays before anyone has said a word.
                        await self.google.send(json.dumps({
                            "clientContent": {
                                "turns": [{"role": "user",
                                           "parts": [{"text": self._kickoff}]}],
                                "turnComplete": True,
                            }
                        }))
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
