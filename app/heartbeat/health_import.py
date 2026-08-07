"""Health Auto Export → Jarvis's flat health format (5 Aug).

Two clients feed /webhook/apple-health: the hand-built iOS Shortcut posts our
flat JSON with the secret in the body; the Health Auto Export app posts its
nested {"data": {"metrics": [...], "workouts": [...]}} and can only carry the
secret as a header — this module translates the nested shape.

Only TODAY's data points count (the app should be set to export range
'Today'), so water arrives as the day's running total and the existing
max-merge stays correct against manual '300ml' logging.
"""
from __future__ import annotations

from datetime import datetime


def _today(points: list, today_iso: str) -> list:
    """Today's points — but the export range is already 'Today', so if the
    app stamped its aggregate in another timezone and nothing matches, take
    what was sent rather than silently dropping it (the 750ml bug, 5 Aug)."""
    points = points or []
    matched = [p for p in points if str(p.get("date", ""))[:10] == today_iso]
    return matched or points


def flatten_export(payload: dict, now_local: datetime) -> dict:
    """Nested Health Auto Export payload → the flat fields the endpoint reads.
    Defensive throughout: unknown metrics are ignored, units converted."""
    data = payload.get("data") or {}
    today_iso = now_local.date().isoformat()
    flat: dict = {"date": today_iso}
    for metric in data.get("metrics") or []:
        # Display names ('Dietary Water') and snake_case ('dietary_water')
        # both normalise to the same key — HAE varies by version.
        name = str(metric.get("name", "")).lower().strip().replace(" ", "_")
        units = str(metric.get("units", "")).lower()
        points = _today(metric.get("data"), today_iso)
        if not points:
            continue
        total = sum(float(p.get("qty") or 0) for p in points)
        if name == "step_count":
            flat["steps"] = int(total)
        elif name in ("dietary_water", "water", "water_intake"):
            if units in ("l", "liter", "litre", "liters", "litres"):
                flat["water_ml"] = round(total * 1000)
            elif units in ("fl_oz", "floz", "fl oz", "oz"):
                flat["water_ml"] = round(total * 29.5735)
            elif units in ("cup", "cups"):
                flat["water_ml"] = round(total * 236.588)
            else:
                flat["water_ml"] = round(total)
        elif name in ("weight_body_mass", "body_mass", "weight"):
            latest = float(points[-1].get("qty") or 0)
            flat["weight_kg"] = round(latest * 0.45359237, 2) if units in ("lb", "lbs") else latest
        elif name == "resting_heart_rate":
            flat["resting_hr"] = int(round(total / len(points)))
        elif name == "heart_rate":
            # Continuous samples (distinct from the daily resting_heart_rate
            # average) — the Watch-wearing chaser needs only the freshest
            # timestamp, to know how long it's been since a sample arrived.
            latest_at = max((str(p.get("date", "")) for p in points), default="")
            if latest_at:
                flat["last_hr_at"] = latest_at
        elif name in ("active_energy", "active_energy_burned"):
            # HAE reports the day's running total for a 'Today' export range —
            # same max-merge shape as water_ml (see merge_active_kcal_total).
            flat["active_kcal"] = round(total, 1)
        elif name == "heart_rate_variability":
            flat["hrv"] = round(total / len(points), 1)
        elif name == "sleep_analysis":
            hours = 0.0
            for p in points:
                hours = max(hours, float(p.get("totalSleep") or p.get("asleep") or p.get("qty") or 0))
            flat["sleep_hours"] = hours
    for workout in data.get("workouts") or []:
        if "run" not in str(workout.get("name", "")).lower():
            continue
        start = str(workout.get("start", ""))
        if start and start[:10] != today_iso:
            continue
        distance = workout.get("distance") or {}
        km = float(distance.get("qty") or 0)
        if str(distance.get("units", "")).lower() in ("mi", "mile", "miles"):
            km = km * 1.60934
        if km <= float(flat.get("run_km") or 0):
            continue
        flat["run_km"] = round(km, 2)
        duration = workout.get("duration")
        try:
            minutes = float(duration)
            # HAE reports seconds; anything over 3 hours as 'minutes' was seconds
            flat["run_min"] = round(minutes / 60, 1) if minutes > 180 else minutes
        except (TypeError, ValueError):
            pass
    return flat
