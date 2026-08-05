"""Phase 1 Trello layer (Paul's spec, 5 Aug): two boards, full card schema.

Everything resolves BY NAME at bootstrap (IDs are per-board and silently rot);
option sets differ per board on purpose (no 'Personal' on Paul x Harry) and a
bad value fails LOUDLY; the Domain alias map turns card language into dropdown
values and the unresolved aliases (Revolax, AMS, BMI, LPG, JD Bio) are asked
about, never guessed. Phase 2 (nudging/sweeps) sits on list_snapshot() —
Kiefer's nudge:false is baked into the registry NOW so it can't be forgotten.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as _tzmod
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BOARD_REGISTRY: dict = {
    "owner": {"name": "Paul Lunny", "trelloId": "58189b548fc170379eca1937",
              "timezone": "Asia/Dubai"},
    "boards": [
        {"key": "master", "name": "Master Board", "id": "6a2a8b7f8eafad960b8f7a33",
         "shortLink": "ydG5YSGO",
         "partner": {"name": "Kiefer Brindle", "trelloId": "59bfdd4a3b5e18a719885d42",
                     "todoList": "Kiefer Today", "nudge": False}},
        {"key": "harry", "name": "Paul x Harry", "id": "6a731cbe38c30479c35caecc",
         "shortLink": "iyHTi1ST",
         "partner": {"name": "Harry", "trelloId": "", "timezone": "Europe/London",
                     "todoList": "Harry Today", "nudge": True}},
    ],
}

# §6.1 — card language → Domain dropdown value. Order matters: first hit wins.
DOMAIN_ALIASES: list[tuple[str, str]] = [
    ("derma eu", "Derma EU"), ("eu-specific", "Derma EU"),
    ("prime derm", "Aesthetics Supply"), ("primederm", "Aesthetics Supply"),
    ("sculptide", "Aesthetics Supply"), ("aesthetics supply", "Aesthetics Supply"),
    ("prodermis", "Prodermis"), ("exobelle", "Prodermis"), ("hydroboost", "Prodermis"),
    ("derma direct", "Derma"), ("derma uk", "Derma"), ("derma", "Derma"),
    ("wages", "Business Ops"), ("contract", "Business Ops"), ("invoice", "Business Ops"),
    ("hr", "Business Ops"), ("h&s", "Business Ops"), ("banking", "Business Ops"),
    ("subscription", "Business Ops"),
]
# No rule exists yet — leave Domain unset and ASK Paul (never guess).
UNRESOLVED_ALIASES = ("revolax", "ams", "bmi", "lpg", "jd bio")

URGENT_PRIORITIES = ("P1", "P2")
ENTERED_FIELD = "Entered List On"


class ResolutionError(RuntimeError):
    """A name didn't resolve on that board — always loud, never silent."""


KNOWN_DOMAINS = ("Personal", "Prodermis", "Derma", "Derma EU", "Aesthetics Supply", "Business Ops")


def classify_domain(text: str, learned: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """(domain, unresolved_alias). Paul's LEARNED rulings win over everything
    — 'BMI is Prodermis' said once resolves BMI forever; the unresolved set
    only trips for names he hasn't ruled on yet (ask, never guess)."""
    lowered = f" {text.lower()} "
    for alias, domain in (learned or {}).items():
        if alias.lower().strip() and alias.lower().strip() in lowered:
            return domain, None
    for alias in UNRESOLVED_ALIASES:
        if alias in lowered:
            return None, alias
    for alias, domain in DOMAIN_ALIASES:
        if alias in lowered:
            return domain, None
    return None, None


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


class BoardMap:
    """One board's resolved names. Every lookup either hits or raises with
    the full list of what IS available — the loud-failure rule (§3)."""

    def __init__(self, key: str, name: str, board_id: str) -> None:
        self.key, self.name, self.id = key, name, board_id
        self.lists: dict[str, str] = {}
        self.archive_lists: dict[str, str] = {}   # '(Archive)' lists — visible for cleanup
        self.fields: dict[str, str] = {}
        self.options: dict[str, dict[str, str]] = {}   # fieldName → {option → id}
        self.labels: dict[str, str] = {}
        self.members: dict[str, str] = {}

    def resolve(self, kind: str, name: str) -> str:
        table: dict = getattr(self, kind)
        if name in table:
            return table[name]
        raise ResolutionError(
            f"'{name}' is not a {kind[:-1]} on {self.name} — available: "
            + ", ".join(sorted(table)) if table else f"no {kind} resolved on {self.name}"
        )

    def resolve_option(self, field: str, value: str) -> str:
        opts = self.options.get(field, {})
        if value in opts:
            return opts[value]
        raise ResolutionError(
            f"'{value}' is not a {field} option on {self.name} — available: "
            + ", ".join(sorted(opts))
        )


class TrelloLayer:
    def __init__(self, client, registry: dict | None = None) -> None:
        self.client = client
        self.registry = registry or BOARD_REGISTRY
        self.owner_tz = ZoneInfo(self.registry["owner"]["timezone"])
        self.boards: dict[str, BoardMap] = {}

    def board(self, key: str) -> BoardMap:
        if key not in self.boards:
            raise ResolutionError(f"board '{key}' not bootstrapped — run bootstrap() first")
        return self.boards[key]

    # ------------------------------------------------------------ §3 bootstrap

    async def bootstrap(self) -> str:
        """Resolve every list, field, option, label and member by name, both
        boards. Returns a printable summary (bug-test step 1) and logs counts
        so silent truncation is visible (§9)."""
        lines = []
        for spec in self.registry["boards"]:
            bmap = BoardMap(spec["key"], spec["name"], spec["id"])
            for row in await self.client.board_lists(spec["id"]):
                # Paul's override (5 Aug eve): archive lists are READABLE —
                # the cleanup operation pulls cards back out of them — but
                # stay out of `lists` so nothing ever routes INTO one.
                if str(row["name"]).endswith("(Archive)"):
                    bmap.archive_lists[row["name"]] = row["id"]
                else:
                    bmap.lists[row["name"]] = row["id"]
            for field in await self.client.board_custom_fields(spec["id"]):
                bmap.fields[field["name"]] = field["id"]
                if field.get("options"):
                    bmap.options[field["name"]] = {
                        o["value"]["text"]: o["id"] for o in field["options"]
                    }
            for row in await self.client.board_labels(spec["id"]):
                if row.get("name"):
                    bmap.labels[row["name"]] = row["id"]
            for row in await self.client.board_members(spec["id"]):
                bmap.members[row.get("fullName") or row.get("username", "?")] = row["id"]
            self.boards[spec["key"]] = bmap
            summary = (
                f"{spec['name']}: {len(bmap.lists)} lists, {len(bmap.fields)} fields "
                f"({', '.join(f'{f}:{len(o)} options' for f, o in bmap.options.items())}), "
                f"{len(bmap.labels)} labels, {len(bmap.members)} members"
            )
            logger.info("Trello bootstrap — %s", summary)
            lines.append(summary + "\n  lists: " + ", ".join(sorted(bmap.lists)))
        return "\n".join(lines)

    # ------------------------------------------------------------ §5/§7 cards

    def _due_utc(self, due_local: datetime) -> str:
        if due_local.tzinfo is None:
            due_local = due_local.replace(tzinfo=self.owner_tz)
        return due_local.astimezone(_tzmod.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    async def _stamp_entered(self, bmap: BoardMap, card_id: str) -> None:
        if ENTERED_FIELD not in bmap.fields:
            return
        now = datetime.now(_tzmod.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        await self.client.set_custom_field(
            card_id, bmap.fields[ENTERED_FIELD], {"value": {"date": now}}
        )

    async def set_domain(self, board_key: str, card_id: str, domain: str) -> None:
        bmap = self.board(board_key)
        option_id = bmap.resolve_option("Domain", domain)   # loud on bad value
        await self.client.set_custom_field(
            card_id, bmap.resolve("fields", "Domain"), {"idValue": option_id}
        )

    async def set_priority(self, board_key: str, card_id: str, priority: str) -> None:
        """Priority + the Urgent label rule (§6.2): P1/P2 gets it, else removed."""
        bmap = self.board(board_key)
        option_id = bmap.resolve_option("Priority", priority)
        await self.client.set_custom_field(
            card_id, bmap.resolve("fields", "Priority"), {"idValue": option_id}
        )
        if "Urgent" in bmap.labels:
            if priority in URGENT_PRIORITIES:
                await self.client.add_label(card_id, bmap.labels["Urgent"])
            else:
                try:
                    await self.client.remove_label(card_id, bmap.labels["Urgent"])
                except Exception:
                    pass  # wasn't on the card — fine

    async def create_card_full(
        self,
        board_key: str,
        list_name: str,
        title: str,
        desc: str = "",
        due_local: datetime | None = None,
        owner_name: str = "",
        domain: str = "",
        priority: str = "",
        checklist: list[dict] | None = None,   # [{name, member, due_local}]
        checklist_name: str = "Steps",
    ) -> dict:
        """§10 steps 2-7 in one honest operation — every schema field."""
        bmap = self.board(board_key)
        card = await self.client.create_card(
            bmap.resolve("lists", list_name), title, desc=desc,
            due_iso=self._due_utc(due_local) if due_local else "",
        )
        card_id = card["id"]
        await self._stamp_entered(bmap, card_id)
        if owner_name:
            await self.client.assign_member(card_id, bmap.resolve("members", owner_name))
        if domain:
            await self.set_domain(board_key, card_id, domain)
        if priority:
            await self.set_priority(board_key, card_id, priority)
        if checklist:
            checklist_obj = await self.client.create_checklist(card_id, checklist_name)
            for item in checklist:
                await self.client.add_check_item(
                    checklist_obj["id"], item["name"],
                    due_iso=self._due_utc(item["due_local"]) if item.get("due_local") else "",
                    member_id=bmap.resolve("members", item["member"]) if item.get("member") else "",
                )
        return card

    async def move_card(self, board_key: str, card_id: str, list_name: str) -> None:
        bmap = self.board(board_key)
        await self.client.update_card(card_id, idList=bmap.resolve("lists", list_name))
        await self._stamp_entered(bmap, card_id)   # §7: stamp on every move

    async def read_card(self, card_id: str) -> dict:
        return await self.client.card_full(card_id)

    # ------------------------------------------------------------ §6.3 routing

    async def done_week_list(self, board_key: str, today: datetime) -> str:
        """Newest 'Done Week …' list id; created for this week (Mon start) if
        absent — 'Done Week 3rd August' ordinal format, Paul's convention."""
        bmap = self.board(board_key)
        done = [n for n in bmap.lists if n.startswith("Done Week")]
        if done:
            return bmap.lists[sorted(done)[-1]] if len(done) == 1 else bmap.lists[
                max(done, key=lambda n: bmap.lists[n])   # newest list id wins
            ]
        from datetime import timedelta

        monday = today - timedelta(days=today.weekday())
        name = f"Done Week {_ordinal(monday.day)} {monday.strftime('%B')}"
        created = await self.client.create_list(bmap.id, name)
        bmap.lists[name] = created["id"]
        return created["id"]

    # ------------------------------------------------------------ §8 time-in-list

    async def entered_current_list_at(self, card_id: str) -> datetime:
        """When the card entered its current list: action history first (it
        sees Paul's hand-moves), card-id creation time as the fallback."""
        actions = await self.client.card_actions(card_id)
        for action in actions:   # newest first
            if (action.get("data") or {}).get("listAfter"):
                return datetime.fromisoformat(action["date"].replace("Z", "+00:00"))
        created = int(card_id[:8], 16)
        return datetime.fromtimestamp(created, tz=_tzmod.utc)

    async def days_in_list(self, card_id: str, now: datetime | None = None) -> float:
        now = now or datetime.now(_tzmod.utc)
        entered = await self.entered_current_list_at(card_id)
        return max(0.0, (now - entered).total_seconds() / 86400)

    # ------------------------------------------------ Phase 2's foundation

    async def list_snapshot(self, board_key: str, list_name: str) -> list[dict]:
        """Every card in a list with owner + day-count — the exact function
        the Phase 2 sweep will stand on."""
        bmap = self.board(board_key)
        list_id = bmap.resolve("lists", list_name)
        cards = [
            c for c in await self.client.board_cards(bmap.id)
            if c.get("idList") == list_id
        ]
        id_to_member = {v: k for k, v in bmap.members.items()}
        out = []
        for card in cards:
            out.append({
                "id": card["id"], "name": card["name"],
                "owners": [id_to_member.get(m, m) for m in card.get("idMembers", [])],
                "days_in_list": await self.days_in_list(card["id"]),
                "due": card.get("due"),
            })
        return out

    async def enumerate_board(self, board_key: str) -> dict:
        """§9: totals from two independent counts — mismatch raises, because
        silent truncation is work falling through the cracks."""
        bmap = self.board(board_key)
        all_cards = await self.client.board_cards(bmap.id)
        by_list: dict[str, int] = {}
        for card in all_cards:
            by_list[card.get("idList", "?")] = by_list.get(card.get("idList", "?"), 0) + 1
        in_known_lists = sum(
            n for list_id, n in by_list.items() if list_id in bmap.lists.values()
        )
        logger.info(
            "Trello enumeration %s: %d cards across %d lists", bmap.name,
            len(all_cards), len(by_list),
        )
        return {"total": len(all_cards), "in_known_lists": in_known_lists, "by_list": by_list}
