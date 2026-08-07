"""Withings smart-scale auto-logging (7 Aug build slice): weight + body-fat
land in health_stats every morning, no manual entry. Same OAuth-token-in-
settings-store shape as GoogleCalendar (app/heartbeat/gcalendar.py) — dormant
(.client.configured is False) until Paul supplies WITHINGS_CLIENT_ID/SECRET
and clicks the connect link once.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from app.clients.withings_client import ReauthNeeded, WithingsClient
from app.core.store import SettingsStore
from app.db.base import Database

logger = logging.getLogger(__name__)

TOKEN_KEY = "withings_refresh_token"
LAST_SYNC_KEY = "withings_last_sync"


class Withings:
    def __init__(self, client: WithingsClient, db: Database, store: SettingsStore) -> None:
        self.client = client
        self._db = db
        self._store = store
        self._token_loaded = False

    async def _ensure_token(self) -> None:
        if not self._token_loaded:
            self.client.refresh_token = await self._store.get(TOKEN_KEY, "")
            self._token_loaded = True
        if not self.client.refresh_token:
            raise ReauthNeeded("Withings not connected yet")

    async def authorised(self) -> bool:
        return bool(await self._store.get(TOKEN_KEY, ""))

    async def store_token(self, refresh_token: str) -> None:
        await self._store.set(TOKEN_KEY, refresh_token)
        self.client.refresh_token = refresh_token
        self._token_loaded = True

    async def daily_sync(self, now: datetime | None = None) -> dict:
        """Pull the last 24h of measurements and file the latest weight/
        body-fat into health_stats. Returns what landed (for a status line);
        never raises — a failed sync just tries again next tick."""
        if not self.client.configured:
            return {"synced": False, "reason": "not configured"}
        try:
            await self._ensure_token()
        except ReauthNeeded:
            return {"synced": False, "reason": "not connected — say 'connect withings' to Jarvis"}
        now = now or datetime.now(timezone.utc)
        start = int(now.timestamp()) - 86400
        end = int(now.timestamp())
        try:
            groups = await self.client.latest_measurements(start, end)
        except ReauthNeeded:
            return {"synced": False, "reason": "Withings token expired — reconnect needed"}
        except Exception:
            logger.exception("Withings measurement fetch failed")
            return {"synced": False, "reason": "fetch failed"}
        weight_kg = 0.0
        fat_pct = 0.0
        latest_date = 0
        for grp in groups:
            grp_date = int(grp.get("date") or 0)
            for m in grp.get("measures") or []:
                value = float(m.get("value", 0)) * (10 ** float(m.get("unit", 0)))
                if m.get("type") == 1 and grp_date >= latest_date:  # weight (kg)
                    weight_kg = round(value, 2)
                elif m.get("type") == 6 and grp_date >= latest_date:  # fat ratio (%)
                    fat_pct = round(value, 1)
            latest_date = max(latest_date, grp_date)
        if weight_kg <= 0 and fat_pct <= 0:
            return {"synced": False, "reason": "no fresh measurement in the last 24h"}
        stat_date = datetime.fromtimestamp(latest_date or end, tz=timezone.utc).date().isoformat()
        await self._db.execute(
            "INSERT INTO health_stats (stat_date, weight_kg, sleep_hours, steps,"
            " resting_hr, hrv, raw, body_fat_pct) VALUES (?, ?, 0, 0, 0, 0, '{}', ?)",
            (stat_date, weight_kg, fat_pct),
        )
        await self._store.set(LAST_SYNC_KEY, now.isoformat())
        return {"synced": True, "weight_kg": weight_kg, "body_fat_pct": fat_pct, "date": stat_date}
