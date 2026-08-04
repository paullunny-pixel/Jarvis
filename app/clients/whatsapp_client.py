"""WhatsApp Business Cloud API — the OFFICIAL route, on Jarvis's own dedicated
number (never Paul's personal account; his call, 4 Aug).

PHASE 1 IS READ-ONLY: Jarvis ingests what arrives on the number and sends
nothing — Paul's explicit choice ('not to kill the number'). send_text exists
as the Phase-2 seam and REFUSES while sending is disabled; enabling it is a
deliberate future decision, not a config accident.

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


@dataclass
class IncomingWa:
    wa_id: str      # sender's WhatsApp id (their phone number, digits only)
    name: str       # profile name, when Meta includes it
    kind: str       # text | voice | image | document | …
    text: str       # body text, or a [marker] for media (download = Phase 2)
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
                if mtype == "text":
                    kind, text = "text", str((msg.get("text") or {}).get("body") or "")
                elif mtype in ("audio", "voice"):
                    kind, text = "voice", "[voice note — transcription comes with Phase 2]"
                else:
                    kind, text = mtype, f"[{mtype}]"
                if not text:
                    continue
                out.append(IncomingWa(
                    wa_id=wa_id,
                    name=names.get(wa_id, ""),
                    kind=kind,
                    text=text[:2000],
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
