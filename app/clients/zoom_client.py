"""Zoom Meetings client (6 Aug) — thin httpx wrapper, deliberately no SDK.

Server-to-Server OAuth on Paul's own Zoom account: an account_credentials
token (cached until near expiry) and POST /users/me/meetings. That's the
whole surface Layer A needs — instant or scheduled meetings with
join-before-host on, so nobody ever waits on Paul to click Start.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://zoom.us/oauth/token"
API_BASE = "https://api.zoom.us/v2"


class ZoomError(RuntimeError):
    pass


class ZoomClient:
    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = httpx.AsyncClient(transport=transport, timeout=20.0)
        self._token = ""
        self._token_expiry = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self._client_id and self._client_secret)

    async def close(self) -> None:
        await self._client.aclose()

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        response = await self._client.post(
            TOKEN_URL,
            params={"grant_type": "account_credentials", "account_id": self.account_id},
            auth=(self._client_id, self._client_secret),
        )
        if response.status_code != 200:
            raise ZoomError(f"Zoom token refused ({response.status_code}): {response.text[:200]}")
        data = response.json()
        self._token = data.get("access_token", "")
        if not self._token:
            raise ZoomError("Zoom token response carried no access_token")
        # Refresh a minute early — a token dying mid-request is a bad surprise.
        self._token_expiry = time.monotonic() + int(data.get("expires_in", 3600)) - 60
        return self._token

    async def create_meeting(
        self,
        topic: str = "Paul's meeting",
        start_iso: str | None = None,   # 'YYYY-MM-DDTHH:MM:SS' local (with tz below)
        tz_name: str = "Asia/Dubai",
        duration_min: int = 60,
    ) -> dict[str, Any]:
        """Create an instant meeting (no start_iso) or a scheduled one.
        Returns {'join_url', 'start_url', 'id', 'password', 'topic'}."""
        token = await self._access_token()
        payload: dict[str, Any] = {
            "topic": topic[:190],
            "type": 2 if start_iso else 1,
            "settings": {
                "join_before_host": True,   # nobody waits on Paul
                "jbh_time": 0,
                "waiting_room": False,
            },
        }
        if start_iso:
            payload["start_time"] = start_iso
            payload["timezone"] = tz_name
            payload["duration"] = duration_min
        response = await self._client.post(
            f"{API_BASE}/users/me/meetings",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code not in (200, 201):
            raise ZoomError(f"Zoom create refused ({response.status_code}): {response.text[:200]}")
        data = response.json()
        return {
            "id": data.get("id"),
            "topic": data.get("topic", topic),
            "join_url": data.get("join_url", ""),
            "start_url": data.get("start_url", ""),
            "password": data.get("password", ""),
            "start_time": data.get("start_time", ""),
        }
