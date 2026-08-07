"""§3 — the Quick Board's real deliverable. Uses the SAME card rules as
everywhere else (the Mac app cheat-sheet, no new logic): create_card_full,
match_members, the natural-language due parser. A coherent sequence, not a
dump — only the genuine first action lands in Paul Today, the rest sit in
Brain Dump. Preview never writes anything; create_project is the one
mutating call, and it's undoable for 60 seconds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

UNDO_WINDOW_SEC = 60
UNDO_KEY = "warroom_last_created_cards"


def _resolve_due(text: str, now: datetime):
    if not text:
        return None
    from app.meetings import parse_when

    return parse_when(text, now)


async def preview_cards(layer, action_points: list[dict], board_key: str = "master") -> list[dict]:
    """Non-mutating. Flags any owner suggestion that isn't a real Trello
    member on this board — never silently creates an ownerless card later."""
    from app.daily12.trello_layer import match_members

    bmap = layer.board(board_key)
    cards = []
    for i, ap in enumerate(action_points):
        owner = str(ap.get("owner_suggestion") or "Paul").strip() or "Paul"
        warning = ""
        if owner.lower() not in ("paul", "me", "myself"):
            ids, unknown = match_members(bmap, owner)
            if not ids:
                warning = f"{owner} isn't on this board — the card will be created unassigned"
        cards.append({
            "title": str(ap.get("title", ""))[:120],
            "why": str(ap.get("why", "")),
            "owner": owner,
            "due": str(ap.get("due", "")),
            "list_name": "Paul Today" if i == 0 else "Brain Dump",
            "sequence": i,
            "warning": warning,
        })
    return cards


async def create_project(
    layer, session_id: int, cards: list[dict], board_key: str = "master",
    session_link: str = "",
) -> dict:
    """The one mutating call — 'Create the project' for the whole set at
    once. Returns {'created': [{'card_id','title','list_name'}], 'warnings': []}."""
    now = datetime.now(layer.owner_tz)
    created, warnings = [], []
    for card in cards:
        desc = card.get("why", "").strip()
        if session_link:
            desc += f"\n\nFrom the War Room session: {session_link}"
        else:
            desc += f"\n\nFrom War Room session #{session_id}."
        try:
            result = await layer.create_card_full(
                board_key=board_key, list_name=card["list_name"], title=card["title"],
                desc=desc, owner_name=card["owner"],
                due_local=_resolve_due(card.get("due", ""), now),
            )
        except Exception:
            logger.exception("War Room card creation failed: %s", card["title"])
            warnings.append(f"Failed to create: {card['title']}")
            continue
        if result.get("_unassigned_owners"):
            warnings.append(f"{card['owner']} isn't on this board — created unassigned")
        created.append({"card_id": result["id"], "title": card["title"], "list_name": card["list_name"]})
    return {"created": created, "warnings": warnings}


async def register_undo(store, session_id: int, board_key: str, created: list[dict]) -> None:
    import json as _json

    await store.set(
        UNDO_KEY,
        _json.dumps({
            "session_id": session_id, "board_key": board_key,
            "card_ids": [c["card_id"] for c in created],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }),
    )


async def undo_last_project(layer, store) -> str:
    import json as _json

    raw = await store.get(UNDO_KEY, "")
    if not raw:
        return "Nothing to undo."
    try:
        record = _json.loads(raw)
        created_at = datetime.fromisoformat(record["created_at"])
    except Exception:
        return "Nothing to undo."
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created_at).total_seconds()
    if age > UNDO_WINDOW_SEC:
        return "That was more than a minute ago — too late to undo."
    for card_id in record["card_ids"]:
        try:
            await layer.client.archive_card(card_id)
        except Exception:
            logger.exception("Undo: failed to archive card %s", card_id)
    await store.set(UNDO_KEY, "")
    return f"Undone — {len(record['card_ids'])} card(s) archived."
