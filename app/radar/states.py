"""§4 — the four card states, and only these four. §5's three 'taking too
long' signals live here too — both are pure functions over a card (+ its
own history), no I/O, so every edge case in the brief's Test list can be
hit directly without a fake Trello connection.

§1's honesty rule shapes this file more than anything else: a Backlog card
untouched for a month must NEVER surface, and 'taking too long' must NEVER
invent a threshold before there's real history to compare against.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Which lists count as 'active' (work in progress) vs noise (§4), matched
# by substring so 'Paul Today' / 'Harry Today' / 'Kiefer Today' / 'Doing'
# all resolve without a per-board config file. Per-list no-update
# threshold in days — one global number would be wrong at both ends.
ACTIVE_LIST_THRESHOLDS = {
    "today": 2,
    "this week": 5,
    "doing": 3,
}
INACTIVE_LIST_MARKERS = ("backlog", "brain dump", "archive")

MIN_CYCLE_SAMPLES = 5     # §5.2 — below this, 'learning what normal looks like'
MIN_CARD_AGE_DAYS = 7     # §5 — no 'taking too long' signal on a card younger than a week
PING_PONG_REVISITS = 3    # entering a list 3+ times = 'moved backwards more than twice'
DUE_CHURN_MIN = 2         # due date pushed twice or more


def is_active_list(list_name: str) -> bool:
    lowered = (list_name or "").lower()
    if any(m in lowered for m in INACTIVE_LIST_MARKERS):
        return False
    return any(k in lowered for k in ACTIVE_LIST_THRESHOLDS)


def no_update_threshold(list_name: str) -> int | None:
    lowered = (list_name or "").lower()
    if any(m in lowered for m in INACTIVE_LIST_MARKERS):
        return None
    for key, days in ACTIVE_LIST_THRESHOLDS.items():
        if key in lowered:
            return days
    return None


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def card_state(card: dict, now: datetime) -> dict:
    """{'state': 'on_track'|'due_soon'|'overdue'|'no_update', 'evidence': str}.
    Overdue beats no-update when both are true (§4's own rule)."""
    due = _parse(card.get("due", ""))
    due_complete = bool(card.get("due_complete"))
    if due and not due_complete and due < now:
        days = (now - due).days
        return {"state": "overdue", "evidence": f"due {due.date().isoformat()}, {days}d overdue"}
    threshold = no_update_threshold(card.get("list_name", ""))
    if threshold is not None:
        last_activity = _parse(card.get("last_activity", ""))
        if last_activity:
            idle_days = (now - last_activity).days
            if idle_days >= threshold:
                return {
                    "state": "no_update",
                    "evidence": f"no update since {last_activity.date().isoformat()} "
                                f"({idle_days}d, this list's threshold is {threshold}d)",
                }
    if due and not due_complete and timedelta(0) <= due - now <= timedelta(hours=48):
        return {"state": "due_soon", "evidence": f"due {due.date().isoformat()}"}
    return {"state": "on_track", "evidence": ""}


def cycle_stats(dwell_days: list[float]) -> dict:
    """Median days-in-list for COMPLETED dwells, from OUR OWN snapshots
    (§5.2). Below MIN_CYCLE_SAMPLES, honestly says so rather than guessing."""
    import statistics

    if len(dwell_days) < MIN_CYCLE_SAMPLES:
        return {"median": None, "count": len(dwell_days), "learning": True}
    return {"median": statistics.median(dwell_days), "count": len(dwell_days), "learning": False}


def taking_too_long_signals(
    card: dict, list_visit_counts: dict[str, int], list_median: dict,
) -> list[dict]:
    """§5's three signals, in order of trustworthiness. Never fires on a
    card younger than a week; never invents a threshold when there's not
    yet enough history for that list."""
    now = card.get("_now") or datetime.now(timezone.utc)
    first_seen = _parse(card.get("first_seen", ""))
    if first_seen and (now - first_seen).days < MIN_CARD_AGE_DAYS:
        return []
    signals = []
    due_churn = card.get("due_change_count", 0)
    if due_churn >= DUE_CHURN_MIN:
        signals.append({
            "kind": "due_churn",
            "evidence": f"due date moved {due_churn} times",
        })
    revisited = {name: n for name, n in list_visit_counts.items() if n >= PING_PONG_REVISITS}
    if revisited:
        worst = max(revisited, key=revisited.get)
        signals.append({
            "kind": "ping_pong",
            "evidence": f"moved in and out of {worst} {revisited[worst]} times",
        })
    if not list_median.get("learning") and list_median.get("median") is not None:
        entered_at = _parse(card.get("list_entered_at", ""))
        if entered_at:
            days_in_list = (now - entered_at).days
            if days_in_list > list_median["median"] * 2:
                signals.append({
                    "kind": "aged",
                    "evidence": f"{days_in_list}d in {card.get('list_name', '')}, "
                                f"median for this list is {list_median['median']:.1f}d",
                })
    return signals
