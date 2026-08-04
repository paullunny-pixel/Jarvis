"""GPS → Jarvis (4 Aug): an iPhone Shortcuts automation posts Paul's
coordinates a few times a day; Jarvis resolves where he is, switches ALL the
clocks to follow him (the 01:30-lights-out-in-Dubai bug class dies here), and
keeps a living 'where Paul is' fact for the brain and the mind.

No heavy geo dependency: Paul's world is a short list of regions (home bases,
Steph's Brazil, supplier countries). A curated bounding-box table covers them;
anywhere else falls back to the longitude's UTC offset — clocks still right,
place name honest ('somewhere around UTC+3')."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAST_LOCATION_KEY = "last_location"

# (place label, tz name, lat_min, lat_max, lon_min, lon_max) — order matters
# where boxes touch (Portugal before Spain).
REGIONS = [
    ("the UK", "Europe/London", 49.5, 61.0, -11.0, 2.0),
    ("Dubai", "Asia/Dubai", 22.0, 26.5, 51.0, 56.5),
    ("Portugal", "Europe/Lisbon", 36.8, 42.2, -9.6, -6.0),
    ("Spain", "Europe/Madrid", 36.0, 43.8, -9.5, 3.5),
    ("Italy", "Europe/Rome", 36.0, 47.2, 6.6, 18.6),
    ("South Korea", "Asia/Seoul", 33.0, 38.7, 124.5, 130.0),
    ("Brazil", "America/Sao_Paulo", -34.0, -1.0, -74.0, -34.0),
]


def resolve(lat: float, lon: float) -> tuple[str, str]:
    """(place, timezone) for coordinates. Curated regions first, honest
    longitude-offset fallback everywhere else."""
    for place, tz, lat_min, lat_max, lon_min, lon_max in REGIONS:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return place, tz
    offset = round(lon / 15)
    offset = max(-12, min(14, offset))
    if offset == 0:
        return "somewhere around UTC", "Etc/GMT"
    # Etc/GMT signs are inverted by convention: Etc/GMT-4 means UTC+4.
    tz = f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
    return f"somewhere around UTC{offset:+d}", tz


async def apply_location(store, living, lat: float, lon: float) -> dict:
    """Record the fix; switch the clocks if Paul has moved timezone.
    Returns {'place', 'tz', 'changed'} — caller reschedules + notifies on
    changed."""
    place, tz = resolve(lat, lon)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await store.set(
        LAST_LOCATION_KEY,
        json.dumps({"lat": lat, "lon": lon, "place": place, "tz": tz, "ts": now_iso}),
    )
    if living is not None:
        try:
            await living.set(
                "location.current", f"{place} (phone GPS, {now_iso[11:16]} UTC)", room="you"
            )
        except Exception:
            logger.exception("Living location fact failed — clocks still handled")
    current = await store.get("current_timezone", "")
    changed = tz != current
    if changed:
        await store.set("current_timezone", tz)
        logger.info("Clocks follow Paul: %s → %s (%s)", current or "default", tz, place)
    return {"place": place, "tz": tz, "changed": changed}
