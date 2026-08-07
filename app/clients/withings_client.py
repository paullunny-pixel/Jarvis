"""Withings (Health Mate) client (7 Aug build slice) — thin httpx, no SDK.

Same OAuth2-with-stored-refresh-token shape as GoogleCalendarClient
(app/clients/gcal_client.py) — Paul consents once, the refresh token lives
in the settings store, access tokens mint on demand. Withings' own token
endpoint is non-standard (a single /v2/oauth2 URL keyed by an `action`
form field rather than separate authorize/refresh paths) but the CALLER
contract mirrors Google's exactly: auth_url() / exchange_code() / a
ReauthNeeded-raising _token() cycle.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
SCOPE = "user.metrics"

# Withings measure types (Measure API): 1 = weight (kg), 6 = fat ratio (%)
WEIGHT_TYPE = 1
FAT_RATIO_TYPE = 6


class WithingsError(RuntimeError):
    pass


class ReauthNeeded(WithingsError):
    """The refresh token is dead/revoked — Paul must re-consent."""


class WithingsClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._client = httpx.AsyncClient(transport=transport, timeout=20.0)
        self._access = ""
        self._access_expiry = 0.0
        self.refresh_token = ""   # injected by the service after load

    @property
    def configured(self) -> bool:
        return bool(self._id and self._secret)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ OAuth

    def auth_url(self, redirect_uri: str, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "response_type": "code",
            "client_id": self._id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
        })

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        response = await self._client.post(TOKEN_URL, data={
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": self._id,
            "client_secret": self._secret,
            "code": code,
            "redirect_uri": redirect_uri,
        })
        data = self._unwrap(response, "code exchange")
        refresh = data.get("refresh_token", "")
        if not refresh:
            raise WithingsError("Withings returned no refresh_token — retry the connect link")
        self.refresh_token = refresh
        self._access = data.get("access_token", "")
        self._access_expiry = time.monotonic() + int(data.get("expires_in", 10800)) - 60
        return refresh

    async def _token(self) -> str:
        if self._access and time.monotonic() < self._access_expiry:
            return self._access
        if not self.refresh_token:
            raise ReauthNeeded("no refresh token stored")
        response = await self._client.post(TOKEN_URL, data={
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": self._id,
            "client_secret": self._secret,
            "refresh_token": self.refresh_token,
        })
        try:
            data = self._unwrap(response, "token refresh")
        except WithingsError as exc:
            if "invalid" in str(exc).lower() or "expired" in str(exc).lower():
                raise ReauthNeeded(str(exc)) from exc
            raise
        self._access = data.get("access_token", "")
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self._access_expiry = time.monotonic() + int(data.get("expires_in", 10800)) - 60
        return self._access

    def _unwrap(self, response: httpx.Response, what: str) -> dict[str, Any]:
        """Withings wraps everything in {"status": 0, "body": {...}} — a 200
        HTTP status does NOT mean success; status must be checked too."""
        if response.status_code != 200:
            raise WithingsError(f"{what} refused ({response.status_code}): {response.text[:200]}")
        payload = response.json()
        if payload.get("status") != 0:
            raise WithingsError(f"{what} failed (Withings status {payload.get('status')}): {payload}")
        return payload.get("body") or {}

    # ------------------------------------------------------------ reads

    async def latest_measurements(self, start_unix: int, end_unix: int) -> list[dict]:
        """Weight + body-fat measures in [start_unix, end_unix]. Returns raw
        measure groups; the service layer converts units and picks the
        latest value per type."""
        token = await self._token()
        response = await self._client.post(
            MEASURE_URL,
            data={
                "action": "getmeas",
                "meastypes": f"{WEIGHT_TYPE},{FAT_RATIO_TYPE}",
                "category": 1,
                "startdate": start_unix,
                "enddate": end_unix,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            raise ReauthNeeded("access rejected — token likely revoked")
        body = self._unwrap(response, "measurements read")
        return body.get("measuregrps", [])
