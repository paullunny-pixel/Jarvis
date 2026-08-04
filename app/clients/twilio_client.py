"""Twilio voice calls (the custom phone channel — NOT ElevenLabs Agents).

Thin httpx client like every other client here: no SDK, MockTransport-testable.
Credentials come from Settings (Render env) and are used two ways:
- REST calls (place a call, point the number's inbound webhook at us);
- request verification (X-Twilio-Signature) so only Twilio can drive the
  TwiML endpoints.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.twilio.com/2010-04-01"


def valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    """Twilio's scheme: HMAC-SHA1(auth token) over the full URL with every
    POST param appended name+value in alphabetical order, base64-encoded."""
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature or "")


class TwilioClient:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.account_sid = account_sid
        self._base = f"{API_BASE}/Accounts/{account_sid}"
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, auth=(account_sid, auth_token)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def place_call(self, to: str, from_: str, twiml_url: str) -> str:
        """Ring `to` and, when answered, fetch call instructions from
        `twiml_url`. Returns the Call SID, or "" on failure."""
        response = await self._client.post(
            f"{self._base}/Calls.json",
            data={"To": to, "From": from_, "Url": twiml_url, "Method": "POST"},
        )
        if response.status_code in (200, 201):
            return response.json().get("sid", "")
        logger.warning("Twilio call failed: %s %s", response.status_code, response.text[:300])
        return ""

    async def configure_inbound(self, phone_number: str, voice_url: str) -> str:
        """Point the number's 'A call comes in' webhook at us (idempotent).
        Returns "" on success, or an honest one-line reason on failure."""
        listing = await self._client.get(
            f"{self._base}/IncomingPhoneNumbers.json", params={"PhoneNumber": phone_number}
        )
        if listing.status_code != 200:
            return f"Twilio wouldn't list numbers ({listing.status_code})"
        numbers = listing.json().get("incoming_phone_numbers", [])
        if not numbers:
            return f"{phone_number} isn't on this Twilio account"
        number_sid = numbers[0]["sid"]
        update = await self._client.post(
            f"{self._base}/IncomingPhoneNumbers/{number_sid}.json",
            data={"VoiceUrl": voice_url, "VoiceMethod": "POST"},
        )
        if update.status_code != 200:
            return f"Twilio refused the webhook update ({update.status_code})"
        return ""

    async def account_summary(self) -> dict | None:
        """{"type": "Trial"|"Full", "status": "active"|...} — None on failure.
        A Trial account can only call verified numbers; the startup check and
        'status' both surface that honestly."""
        response = await self._client.get(f"{self._base}.json")
        if response.status_code != 200:
            return None
        data = response.json()
        return {"type": data.get("type", ""), "status": data.get("status", "")}
