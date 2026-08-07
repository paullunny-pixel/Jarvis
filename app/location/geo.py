"""§1/§2 — distance + place classification. Pure functions: given a fix and
Paul's own taught places (never assumed), say what's nearby. Airports are
the one category worth a curated seed (§2's 'confirm, don't assume' rule
means a false positive just costs one Telegram question, but a miss means
the sobriety travel-support trigger never fires)."""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000
DEFAULT_RADIUS_M = 200

# Major airports near Paul's regions (app/heartbeat/location.py's REGIONS) —
# a starting seed, not exhaustive. A miss here just means one fewer
# confirmed 'flying today?' prompt, never a false claim.
AIRPORTS = [
    ("Heathrow", 51.4700, -0.4543, 3000),
    ("Gatwick", 51.1537, -0.1821, 2500),
    ("Dubai International", 25.2532, 55.3657, 4000),
    ("Al Maktoum International", 24.8994, 55.1610, 4000),
    ("Lisbon Airport", 38.7813, -9.1359, 2500),
    ("Madrid Barajas", 40.4936, -3.5668, 3500),
    ("Rome Fiumicino", 41.8003, 12.2389, 3500),
    ("Incheon", 37.4602, 126.4407, 4500),
    ("Sao Paulo Guarulhos", -23.4356, -46.4731, 3500),
]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def classify(lat: float, lon: float, named_places: list[dict]) -> dict:
    """{'name': str, 'kind': str}. Named places (taught by Paul) win over
    the curated airport seed, which wins over 'travelling'/'unknown'.
    Never invents a place name he hasn't taught or a curated list hasn't
    named — 'unknown place' is an honest answer."""
    for place in named_places:
        distance = haversine_m(lat, lon, place["lat"], place["lon"])
        if distance <= place.get("radius_m", DEFAULT_RADIUS_M):
            return {"name": place["name"], "kind": place.get("kind") or "other", "distance_m": distance}
    for name, alat, alon, radius in AIRPORTS:
        distance = haversine_m(lat, lon, alat, alon)
        if distance <= radius:
            return {"name": name, "kind": "airport", "distance_m": distance}
    return {"name": "", "kind": "unknown", "distance_m": None}
