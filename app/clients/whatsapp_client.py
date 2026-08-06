"""WhatsApp Business Cloud API — the OFFICIAL route, on Jarvis's own dedicated
number (never Paul's personal account; his call, 4 Aug).

Part 1 (7 Aug brief): Paul talks to Jarvis on WhatsApp same as Telegram —
send_text/send_voice work once WHATSAPP_SENDING_ENABLED is flipped on
(still Paul's deliberate call, not a config accident: both REFUSE while
sending is disabled) and download_media pulls his voice notes down for
Deepgram. Everyone except Paul's own number stays read-only at the router
level (app/core/router.py), not here — this client just carries messages.

House style: thin httpx, no SDK, MockTransport-testable.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v20.0"


class WhatsAppError(RuntimeError):
    pass


@dataclass
class IncomingWa:
    wa_id: str      # sender's WhatsApp id (their phone number, digits only)
    name: str       # profile name, when Meta includes it
    kind: str       # text | voice | image | document | …
    text: str       # body text for text messages; empty for voice (see media_id)
    media_id: str   # set for voice/audio messages — download_media() fetches it
    ts: str         # Meta's unix-seconds timestamp, as given


def parse_webhook(payload: dict) -> list[IncomingWa]:
    """Cloud API webhook payload → the messages inside it. Status updates
    (delivered/read receipts) carry no 'messages' and yield nothing."""
    out: list[IncomingWa] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                str(c.get("wa_id") or ""): str((c.get("profile") or {}).get("name") or "")
                for c in value.get("contacts") or []
            }
            for msg in value.get("messages") or []:
                wa_id = str(msg.get("from") or "")
                if not wa_id:
                    continue
                mtype = str(msg.get("type") or "text")
                media_id = ""
                if mtype == "text":
                    kind, text = "text", str((msg.get("text") or {}).get("body") or "")
                elif mtype in ("audio", "voice"):
                    kind, text = "voice", ""
                    media_id = str((msg.get("audio") or msg.get("voice") or {}).get("id") or "")
                else:
                    kind, text = mtype, f"[{mtype}]"
                if not text and not media_id:
                    continue
                out.append(IncomingWa(
                    wa_id=wa_id,
                    name=names.get(wa_id, ""),
                    kind=kind,
                    text=text[:2000],
                    media_id=media_id,
                    ts=str(msg.get("timestamp") or ""),
                ))
    return out


def valid_signature(app_secret: str, body: bytes, header: str) -> bool:
    """Meta signs webhook POSTs with X-Hub-Signature-256. No secret configured
    → accept (the endpoint is still obscure + verify-token gated on GET);
    secret configured → the signature must check out."""
    if not app_secret:
        return True
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


class WhatsAppClient:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        sending_enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.phone_number_id = phone_number_id
        self.sending_enabled = sending_enabled
        self._client = httpx.AsyncClient(
            base_url=GRAPH_URL,
            transport=transport,
            timeout=15.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send_text(self, to_wa_id: str, text: str) -> bool:
        """Phase-2 seam. While sending is disabled this REFUSES — read-only is
        a promise to Paul, not a suggestion."""
        if not self.sending_enabled:
            logger.warning("WhatsApp send refused — Phase 1 is read-only (Paul's call, 4 Aug)")
            return False
        response = await self._client.post(
            f"/{self.phone_number_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_wa_id,
                "type": "text",
                "text": {"body": text[:4000]},
            },
        )
        if response.status_code >= 400:
            logger.error("WhatsApp send failed %s: %s", response.status_code, response.text[:200])
            return False
        return True

    async def send_voice(self, to_wa_id: str, audio_mp3: bytes) -> bool:
        """Voice reply: upload the audio (a two-step Graph dance — media
        endpoint first, then reference the resulting id in the message)."""
        if not self.sending_enabled:
            logger.warning("WhatsApp send refused — Phase 1 is read-only (Paul's call, 4 Aug)")
            return False
        upload = await self._client.post(
            f"/{self.phone_number_id}/media",
            data={"messaging_product": "whatsapp"},
            files={"file": ("reply.mp3", audio_mp3, "audio/mpeg")},
        )
        if upload.status_code >= 400:
            logger.error("WhatsApp media upload failed %s: %s", upload.status_code, upload.text[:200])
            return False
        media_id = str(upload.json().get("id") or "")
        if not media_id:
            logger.error("WhatsApp media upload returned no id")
            return False
        response = await self._client.post(
            f"/{self.phone_number_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_wa_id,
                "type": "audio",
                "audio": {"id": media_id},
            },
        )
        if response.status_code >= 400:
            logger.error("WhatsApp voice send failed %s: %s", response.status_code, response.text[:200])
            return False
        return True

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Two-step Graph fetch for an inbound voice note: id → {url,
        mime_type}, then the url itself (same Bearer header covers both —
        Meta's attachment host requires it, unlike a public CDN link)."""
        meta = await self._client.get(f"/{media_id}")
        if meta.status_code >= 400:
            raise WhatsAppError(f"media lookup failed ({meta.status_code}): {meta.text[:200]}")
        info = meta.json()
        url = str(info.get("url") or "")
        mime = str(info.get("mime_type") or "audio/ogg")
        if not url:
            raise WhatsAppError("media lookup returned no url")
        audio = await self._client.get(url)
        if audio.status_code >= 400:
            raise WhatsAppError(f"media download failed ({audio.status_code})")
        return audio.content, mime
