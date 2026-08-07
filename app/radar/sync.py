"""§2/§3 — the sync worker. Poll boards, not cards (one call per board);
enumerate EVERY board the token can reach, not just the two registered
in app/daily12/trello_layer.py's BOARD_REGISTRY, because §2's whole point
is that coverage is unknown and must be said out loud, not assumed from a
hard-coded list. Keeps its own history (radar_snapshots, radar_list_history)
since Trello's action history can't be relied on for cycle times (§3).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.daily12.trello_layer import classify_domain

logger = logging.getLogger(__name__)

BOARDS_SEEN_KEY = "radar_boards_seen"
LAST_SYNC_KEY = "radar_last_sync_at"


class RadarSync:
    def __init__(self, client, db, store) -> None:
        self.client = client
        self.db = db
        self.store = store

    async def sync(self) -> dict:
        """Returns {'boards': [names], 'synced_at': iso, 'errors': [names]}.
        A board that fails to sync is reported, never silently dropped from
        the coverage note."""
        boards = await self.client.my_boards()
        ok, errors = [], []
        seen_card_ids: set[str] = set()
        for board in boards:
            board_id, board_name = board["id"], board.get("name", "")
            try:
                await self._sync_board(board_id, board_name, seen_card_ids)
                ok.append(board_name)
            except Exception:
                logger.exception("Radar sync failed for board %s", board_name)
                errors.append(board_name)
        await self._close_missing_cards(seen_card_ids)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await self.store.set(LAST_SYNC_KEY, now)
        await self.store.set(BOARDS_SEEN_KEY, json.dumps({"ok": ok, "errors": errors}))
        return {"boards": ok, "errors": errors, "synced_at": now}

    async def _sync_board(self, board_id: str, board_name: str, seen_card_ids: set) -> None:
        lists = {row["id"]: row["name"] for row in await self.client.board_lists(board_id)}
        members = {
            row["id"]: row.get("fullName") or row.get("username", "")
            for row in await self.client.board_members(board_id)
        }
        domain_map = await self._domain_field_map(board_id)
        cards = await self.client.board_cards_full(board_id)
        for card in cards:
            await self._upsert_card(card, board_id, board_name, lists, members, domain_map)
            seen_card_ids.add(card["id"])

    async def _domain_field_map(self, board_id: str) -> dict:
        """{customFieldItem option id -> resolved Domain text}, best-effort
        — a board with no Domain field (most of what the token can newly
        reach won't have one) just falls back to the text classifier."""
        try:
            fields = await self.client.board_custom_fields(board_id)
        except Exception:
            return {}
        out = {}
        for field in fields:
            if field.get("name") != "Domain":
                continue
            for opt in field.get("options") or []:
                out[opt["id"]] = opt["value"]["text"]
        return out

    @staticmethod
    def _resolve_company(card: dict, domain_map: dict, title: str) -> str:
        for item in card.get("customFieldItems") or []:
            option_id = item.get("idValue")
            if option_id and option_id in domain_map:
                return domain_map[option_id]
        domain, _unresolved = classify_domain(title)
        return domain or ""

    async def _upsert_card(
        self, card: dict, board_id: str, board_name: str, lists: dict, members: dict, domain_map: dict,
    ) -> None:
        card_id = card["id"]
        list_name = lists.get(card.get("idList", ""), "")
        owners = [members.get(m, m) for m in card.get("idMembers") or []]
        due = card.get("due") or ""
        due_complete = bool(card.get("dueComplete"))
        last_activity = card.get("dateLastActivity") or ""
        badges = card.get("badges") or {}
        checklist_total = int(badges.get("checkItems", 0) or 0)
        checklist_done = int(badges.get("checkItemsChecked", 0) or 0)
        comment_count = int(badges.get("comments", 0) or 0)
        company = self._resolve_company(card, domain_map, card.get("name", ""))
        warroom_session_id = await self._warroom_session_for(card_id)
        short_url = card.get("shortUrl") or f"https://trello.com/c/{card_id}"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        existing = await self.db.fetch_one("SELECT * FROM radar_cards WHERE card_id = ?", (card_id,))
        if existing is None:
            await self.db.execute(
                "INSERT INTO radar_cards (card_id, board_id, board_name, list_id, list_name,"
                " list_entered_at, title, company_tag, owners, due, due_complete, due_change_count,"
                " last_activity, checklist_total, checklist_done, comment_count, warroom_session_id,"
                " short_url, first_seen, updated_at, archived)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (card_id, board_id, board_name, card.get("idList", ""), list_name, now,
                 card.get("name", "")[:300], company, json.dumps(owners), due,
                 1 if due_complete else 0, last_activity, checklist_total, checklist_done,
                 comment_count, warroom_session_id, short_url, now, now),
            )
            await self.db.execute(
                "INSERT INTO radar_list_history (card_id, list_name, entered_at, left_at)"
                " VALUES (?, ?, ?, '')",
                (card_id, list_name, now),
            )
        else:
            list_entered_at = existing["list_entered_at"]
            due_change_count = existing["due_change_count"]
            if list_name != existing["list_name"]:
                await self.db.execute(
                    "UPDATE radar_list_history SET left_at = ?"
                    " WHERE card_id = ? AND list_name = ? AND left_at = ''",
                    (now, card_id, existing["list_name"]),
                )
                await self.db.execute(
                    "INSERT INTO radar_list_history (card_id, list_name, entered_at, left_at)"
                    " VALUES (?, ?, ?, '')",
                    (card_id, list_name, now),
                )
                list_entered_at = now
            if due != existing["due"] and existing["due"]:
                due_change_count += 1
            await self.db.execute(
                "UPDATE radar_cards SET board_id=?, board_name=?, list_id=?, list_name=?,"
                " list_entered_at=?, title=?, company_tag=?, owners=?, due=?, due_complete=?,"
                " due_change_count=?, last_activity=?, checklist_total=?, checklist_done=?,"
                " comment_count=?, warroom_session_id=?, short_url=?, updated_at=?, archived=0 WHERE card_id=?",
                (board_id, board_name, card.get("idList", ""), list_name, list_entered_at,
                 card.get("name", "")[:300], company, json.dumps(owners), due,
                 1 if due_complete else 0, due_change_count, last_activity, checklist_total,
                 checklist_done, comment_count, warroom_session_id, short_url, now, card_id),
            )
        await self.db.execute(
            "INSERT INTO radar_snapshots (card_id, ts, list_name, due, owners, last_activity)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (card_id, now, list_name, due, json.dumps(owners), last_activity),
        )

    async def _warroom_session_for(self, card_id: str) -> int:
        row = await self.db.fetch_one(
            "SELECT session_id FROM warroom_watch WHERE card_id = ? ORDER BY id DESC LIMIT 1", (card_id,)
        )
        return row["session_id"] if row else 0

    async def _close_missing_cards(self, seen_card_ids: set) -> None:
        """A card that vanished from every board's poll was archived or
        deleted on Trello's side — mark it here too so it drops out of
        active views instead of lying about still being open."""
        rows = await self.db.fetch_all("SELECT card_id, list_name FROM radar_cards WHERE archived = 0")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in rows:
            if row["card_id"] in seen_card_ids:
                continue
            await self.db.execute(
                "UPDATE radar_cards SET archived = 1, updated_at = ? WHERE card_id = ?",
                (now, row["card_id"]),
            )
            await self.db.execute(
                "UPDATE radar_list_history SET left_at = ? WHERE card_id = ? AND left_at = ''",
                (now, row["card_id"]),
            )
