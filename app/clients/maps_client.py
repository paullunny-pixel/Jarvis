"""§3 — traffic-aware leave-now alerts. Thin httpx wrapper over Google's
Directions API (traffic-aware ETA between two points). Dormant until
GOOGLE_MAPS_API_KEY exists — this brief flagged the key as needed and it
hasn't been supplied yet, so this client is built and testable but nothing
calls it live until the key lands, same pattern as every other dormant
integration in this codebase.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://maps.googleapis.com/maps/api/directions/json"


class MapsError(RuntimeError):
    pass


class MapsClient:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def eta_with_traffic(
        self, origin_lat: float, origin_lon: float, dest: str,
    ) -> dict[str, Any]:
        """{'duration_min': int, 'duration_in_traffic_min': int, 'summary': str}
        — dest can be an address or 'lat,lon'. Uses departure_time=now so
        Google returns live-traffic duration, not the free-flow estimate."""
        response = await self._client.get(API_URL, params={
            "origin": f"{origin_lat},{origin_lon}",
            "destination": dest,
            "departure_time": "now",
            "key": self._api_key,
        })
        if response.status_code != 200:
            raise MapsError(f"Directions {response.status_code}: {response.text[:200]}")
        data = response.json()
        if data.get("status") != "OK":
            raise MapsError(f"Directions API status {data.get('status')}: {data.get('error_message', '')[:200]}")
        routes = data.get("routes") or []
        if not routes:
            raise MapsError("no route returned")
        leg = routes[0]["legs"][0]
        duration_s = leg["duration"]["value"]
        traffic_s = leg.get("duration_in_traffic", leg["duration"])["value"]
        return {
            "duration_min": round(duration_s / 60),
            "duration_in_traffic_min": round(traffic_s / 60),
            "summary": routes[0].get("summary", ""),
        }


def leave_now_message(event_title: str, eta: dict, event_start_iso: str, buffer_min: int = 10) -> str:
    """§3's 'leave now / leave in X' line, from a real ETA + buffer."""
    from datetime import datetime

    start = datetime.fromisoformat(event_start_iso)
    now = datetime.now(start.tzinfo)
    minutes_until_start = max(0, int((start - now).total_seconds() // 60))
    travel = eta["duration_in_traffic_min"]
    leave_in = minutes_until_start - travel - buffer_min
    if leave_in <= 0:
        return f"Leave now for {event_title} — {travel} min in traffic, you're already tight."
    return f"Leave in {leave_in} min for {event_title} — {travel} min in current traffic."
