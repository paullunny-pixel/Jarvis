"""Twilio Media Streams ↔ ElevenLabs Conversational AI bridge (7 Aug).

The realtime phone upgrade: an inbound call opens a Twilio <Connect><Stream>
WebSocket into us, and we pump audio both ways between the caller and the
SAME live ElevenLabs agent the cockpit's Talk button uses — persona-primed,
second-brain recall, action tools. Barge-in comes for free: the agent sends
an 'interruption' event and we tell Twilio to clear its playback buffer.

Formats: Twilio speaks 8kHz mu-law, the agent speaks whatever its metadata
declares (usually 16kHz PCM) — audioop transcodes both directions, keeping
resample filter state per direction so chunk edges don't crackle. (audioop
is deprecated for 3.13; Render runs 3.11 — revisit if the runtime moves.)

Everything here is deliberately dumb plumbing with injected sockets, so the
whole conversation loop tests with fakes — no network, no Twilio, no
ElevenLabs.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging

logger = logging.getLogger(__name__)

MAX_CALL_SECONDS = 30 * 60   # nobody needs a longer call with their PA


def parse_format(name: str) -> tuple[str, int]:
    """'pcm_16000' → ('pcm', 16000); 'ulaw_8000' → ('ulaw', 8000)."""
    try:
        kind, rate = name.rsplit("_", 1)
        return kind, int(rate)
    except (ValueError, AttributeError):
        return "pcm", 16000


class _Resampler:
    """audioop.ratecv with carried state — one per direction."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src, self.dst = src_rate, dst_rate
        self._state = None

    def __call__(self, pcm: bytes) -> bytes:
        if self.src == self.dst:
            return pcm
        out, self._state = audioop.ratecv(pcm, 2, 1, self.src, self.dst, self._state)
        return out


class MediaBridge:
    """Pumps one call. `twilio_ws` is the accepted server socket (needs
    receive_text/send_text); `eleven` is the client socket to the agent
    (needs send/recv/close with str payloads)."""

    def __init__(self, twilio_ws, eleven) -> None:
        self.twilio_ws = twilio_ws
        self.eleven = eleven
        self.stream_sid = ""
        # Until the agent's metadata arrives, assume its defaults. Caller
        # audio HOLDS for the metadata (Twilio streams instantly, the agent's
        # first message races it) — transcoding the opening words to the
        # wrong format would garble the very start of every call.
        self.in_fmt = ("pcm", 16000)    # what the agent wants to hear
        self.out_fmt = ("pcm", 16000)   # what the agent sends back
        self._meta = asyncio.Event()
        self._up = _Resampler(8000, 16000)
        self._down = _Resampler(16000, 8000)

    # ---------------------------------------------------------- transcode

    def _to_eleven(self, mulaw: bytes) -> bytes:
        kind, rate = self.in_fmt
        if kind == "ulaw" and rate == 8000:
            return mulaw
        pcm = audioop.ulaw2lin(mulaw, 2)
        if self._up.dst != rate:
            self._up = _Resampler(8000, rate)
        return self._up(pcm)

    def _to_twilio(self, agent_audio: bytes) -> bytes:
        kind, rate = self.out_fmt
        if kind == "ulaw" and rate == 8000:
            return agent_audio
        if self._down.src != rate:
            self._down = _Resampler(rate, 8000)
        return audioop.lin2ulaw(self._down(agent_audio), 2)

    # -------------------------------------------------------------- pumps

    async def _pump_twilio(self) -> None:
        """Caller → agent. Ends on the stream's 'stop' or a socket close."""
        while True:
            data = json.loads(await self.twilio_ws.receive_text())
            event = data.get("event")
            if event == "start":
                self.stream_sid = (data.get("start") or {}).get("streamSid", "")
            elif event == "media":
                if not self._meta.is_set():
                    try:
                        await asyncio.wait_for(self._meta.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        self._meta.set()   # carry on with the defaults, honestly
                chunk = self._to_eleven(base64.b64decode(data["media"]["payload"]))
                await self.eleven.send(json.dumps(
                    {"user_audio_chunk": base64.b64encode(chunk).decode()}
                ))
            elif event == "stop":
                return

    async def _pump_eleven(self) -> None:
        """Agent → caller, plus the protocol chores (metadata, ping,
        interruption→clear). Ends when the agent closes the session."""
        while True:
            data = json.loads(await self.eleven.recv())
            kind = data.get("type")
            if kind == "conversation_initiation_metadata":
                meta = data.get("conversation_initiation_metadata_event") or {}
                self.out_fmt = parse_format(meta.get("agent_output_audio_format", "pcm_16000"))
                self.in_fmt = parse_format(meta.get("user_input_audio_format", "pcm_16000"))
                self._meta.set()
            elif kind == "audio":
                payload = self._to_twilio(
                    base64.b64decode(data["audio_event"]["audio_base_64"])
                )
                await self.twilio_ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(payload).decode()},
                }))
            elif kind == "interruption":
                # Paul talked over Jarvis — bin whatever Twilio hasn't played.
                await self.twilio_ws.send_text(json.dumps(
                    {"event": "clear", "streamSid": self.stream_sid}
                ))
            elif kind == "ping":
                event_id = (data.get("ping_event") or {}).get("event_id")
                await self.eleven.send(json.dumps({"type": "pong", "event_id": event_id}))

    async def run(self) -> None:
        """Run both pumps until either side hangs up (or the hard cap)."""
        try:
            await self.eleven.send(json.dumps({"type": "conversation_initiation_client_data"}))
        except Exception:
            logger.exception("Agent session opener failed — bridge won't start")
            return
        pumps = [
            asyncio.create_task(self._pump_twilio()),
            asyncio.create_task(self._pump_eleven()),
        ]
        try:
            done, pending = await asyncio.wait(
                pumps, return_when=asyncio.FIRST_COMPLETED, timeout=MAX_CALL_SECONDS
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    # Socket closes surface as exceptions — that's just how
                    # calls end; log real faults, not hangups, at error level.
                    logger.info("Bridge pump ended: %s", exc)
        finally:
            for task in pumps:
                task.cancel()
            try:
                await self.eleven.close()
            except Exception:
                pass
