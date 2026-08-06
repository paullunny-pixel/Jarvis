"""Google Calendar client (6 Aug) — thin httpx, deliberately no SDK.

OAuth2 with a stored refresh token: Paul consents ONCE via the connect URL,
the refresh token lives in the settings store, and access tokens are minted
on demand. A dead refresh token raises ReauthNeeded — callers turn that into
a clear 're-authorise Google' message, never a silent blank.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar"


class GcalError(RuntimeError):
    pass


class ReauthNeeded(GcalError):
    """The refresh token is dead/revoked — Paul must re-consent."""


class GoogleCalendarClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._client = httpx.AsyncClient(transport=transport, timeout=25.0)
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
            "client_id": self._id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",     # forces a fresh refresh token every time
            "state": state,
        })

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Authorization code → refresh token (caller persists it)."""
        response = await self._client.post(TOKEN_URL, data={
            "client_id": self._id,
            "client_secret": self._secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        if response.status_code != 200:
            raise GcalError(f"code exchange refused ({response.status_code}): {response.text[:200]}")
        data = response.json()
        refresh = data.get("refresh_token", "")
        if not refresh:
            raise GcalError("Google returned no refresh_token — retry the connect link")
        self.refresh_token = refresh
        self._access = data.get("access_token", "")
        self._access_expiry = time.monotonic() + int(data.get("expires_in", 3600)) - 60
        return refresh

    async def _token(self) -> str:
        if self._access and time.monotonic() < self._access_expiry:
            return self._access
        if not self.refresh_token:
            raise ReauthNeeded("no refresh token stored")
        response = await self._client.post(TOKEN_URL, data={
            "client_id": self._id,
            "client_secret": self._secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        })
        if response.status_code != 200:
            if "invalid_grant" in response.text:
                raise ReauthNeeded("refresh token revoked/expired")
            raise GcalError(f"token refresh failed ({response.status_code}): {response.text[:200]}")
        data = response.json()
        self._access = data.get("access_token", "")
        self._access_expiry = time.monotonic() + int(data.get("expires_in", 3600)) - 60
        return self._access

    async def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        token = await self._token()
        response = await self._client.request(
            method, f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"}, **kwargs,
        )
        if response.status_code == 401:
            raise ReauthNeeded("access rejected — token likely revoked")
        if response.status_code not in (200, 201, 204):
            raise GcalError(f"{method} {path} refused ({response.status_code}): {response.text[:200]}")
        return response.json() if response.status_code != 204 and response.text else {}

    # ------------------------------------------------------------ reads

    async def calendars(self) -> list[dict]:
        data = await self._call("GET", "/users/me/calendarList")
        return [
            {"id": c.get("id", ""), "name": c.get("summary", ""), "primary": bool(c.get("primary"))}
            for c in data.get("items", [])
        ]

    async def events(self, calendar_id: str, time_min: str, time_max: str) -> list[dict]:
        """Expanded instances (recurring handled by Google: singleEvents=True
        includes moved instances and drops cancelled ones), ordered by start."""
        data = await self._call(
            "GET", f"/calendars/{httpx.QueryParams({'q': calendar_id})['q']}/events",
            params={
                "timeMin": time_min, "timeMax": time_max,
                "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
            },
        )
        return data.get("items", [])

    # ------------------------------------------------------------ writes

    async def create_event(self, calendar_id: str, body: dict) -> dict:
        return await self._call("POST", f"/calendars/{calendar_id}/events", json=body)

    async def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return await self._call("PATCH", f"/calendars/{calendar_id}/events/{event_id}", json=body)

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await self._call("DELETE", f"/calendars/{calendar_id}/events/{event_id}")

    async def get_event(self, calendar_id: str, event_id: str) -> dict:
        return await self._call("GET", f"/calendars/{calendar_id}/events/{event_id}")
