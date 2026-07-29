"""Trello sync, company/project tagging, Daily 12 generation and board write-back."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.clients.anthropic_client import ClaudeClient
from app.core.store import SettingsStore, utc_now_iso
from app.daily12.scoring import (
    COMPANIES,
    COMPANY_NAMES,
    Card,
    avoidance_score,
    is_actionable_list,
    parse_iso_date,
    select_focus,
)
from app.daily12.trello import TrelloClient
from app.db.base import Database

logger = logging.getLogger(__name__)

# Trello System Brief §3 — the Master Board label system, two axes.
# Domain (exactly one per card) + Flags (stack as many as apply).
DOMAIN_LABELS = {
    "personal": ("Personal", "green"),
    "prodermis": ("Prodermis", "purple"),
    "derma": ("Derma", "sky"),
    "business_ops": ("Business Ops", "blue"),
}
FLAG_LABELS = {
    "urgent": ("Urgent", "red"),
    "money": ("£ Money", "orange"),
    "waiting": ("Waiting On", "yellow"),
    "delegate": ("Delegate", "pink"),
}
LABEL_IDS_KEY = "trello_label_ids"

TAGGING_SYSTEM = """\
You classify Trello cards for Paul's four companies. Companies (use these slugs):
- derma_uk: Derma Direct UK — UK online wholesaler (fillers, boosters, gloves), Harry ops, website relaunch, retention marketing.
- derma_eu: Derma Direct EU — EU/US copy of the UK business, not yet trading, Dutch company formation, NL bonded warehouse.
- aesthetics_supply: Aesthetics Supply UK — grey-market products, ~40 accounts, needs marketing.
- prodermis: Prodermis — own manufacturing brand (BMI fillers, Pyway boosters/mesotherapy, Barcelona skincare), distributors (Alicia overseas, Jane & Karen UK), Ministry of Health registrations, £5k/month UK target.
Personal/other cards (house sale, body scan, family admin) → company "".

For each card, also name its project — a short recurring workstream name (e.g. "Website relaunch", "Dutch company formation", "BMI relationship", "Distributor registrations"). Reply ONLY a JSON array, one item per card, same order:
[{"company": "<slug or empty>", "project": "<name>"}]\
"""


class Daily12Service:
    def __init__(
        self,
        db: Database,
        trello: TrelloClient,
        claude: ClaudeClient,
        timezone_default: str = "Europe/London",
        board_filter: str = "",
        today_list: str = "Paul Today",
        personal_list: str = "Paul Personal",
        per_company: int = 3,
        personal_max: int = 3,
    ) -> None:
        self._db = db
        self._trello = trello
        self._claude = claude
        self._settings = SettingsStore(db)
        self._tz_default = timezone_default
        self._today_list = today_list.strip().lower()
        self._personal_list = personal_list.strip().lower()
        self._per_company = per_company
        self._personal_max = personal_max
        # Board scope: names to work from; empty / "all" = every open board.
        self._board_filter = [
            name.strip().lower()
            for name in (board_filter or "").split(",")
            if name.strip() and name.strip().lower() != "all"
        ]

    async def health(self) -> dict:
        """Live connection check — actually calls Trello and reports the truth."""
        try:
            boards = await self._trello.my_boards()
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        scoped = self._apply_filter(boards)
        cached = await self._db.fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE actionable = 1")
        return {
            "ok": True,
            "boards": len(boards),
            "board_name": ", ".join(b.get("name", "") for b in scoped),
            "scoped": len(scoped) != len(boards),
            "cached_cards": int(cached["n"]) if cached else 0,
            "last_sync": await self._settings.get("trello_last_sync", "never"),
        }

    # ------------------------------------------------------------------ sync

    def _apply_filter(self, boards: list[dict]) -> list[dict]:
        if not self._board_filter:
            return boards
        scoped = [
            b for b in boards if b.get("name", "").strip().lower() in self._board_filter
        ]
        if not scoped:
            logger.warning(
                "Board scope %s matched none of the visible boards — reading every board "
                "rather than going dark", self._board_filter,
            )
            return boards
        return scoped

    async def _scoped_boards(self) -> list[dict]:
        boards = await self._trello.my_boards()
        if not boards:
            raise RuntimeError("No Trello boards visible to this token")
        return self._apply_filter(boards)

    async def resolve_board(self) -> str:
        """Default board for writes (new cards, member lookup): the busiest
        in-scope board — with a single-board scope, simply that board."""
        boards = await self._scoped_boards()
        ids = [b["id"] for b in boards]
        placeholders = ", ".join("?" for _ in ids)
        row = await self._db.fetch_one(
            f"SELECT board_id, COUNT(*) AS n FROM tasks WHERE board_id IN ({placeholders})"
            " GROUP BY board_id ORDER BY n DESC LIMIT 1",
            tuple(ids),
        )
        if row:
            return row["board_id"]
        return boards[0]["id"]

    async def sync(self) -> int:
        """Pull every card from every list on every in-scope board into the
        tasks cache; retire cached cards that fall outside the scope OR that
        vanished from the board (deleted/archived/moved away); tag untagged."""
        boards = await self._scoped_boards()
        run_started = utc_now_iso()

        total = 0
        for board in boards:
            lists = {l["id"]: l["name"] for l in await self._trello.board_lists(board["id"])}
            for card in await self._trello.board_cards(board["id"]):
                list_name = lists.get(card.get("idList", ""), "")
                labels = [l.get("name", "").lower() for l in card.get("labels", [])]
                money = _money_from_labels(labels)
                waiting = any("waiting" in l and "paul" in l for l in labels) or any(
                    l in ("waiting-on-paul", "team waiting") for l in labels
                )
                await self._upsert_task(card, board, list_name, money, waiting)
                total += 1

        # Cards cached from boards now outside the scope leave the working
        # pool (history stays; they come straight back if the scope widens).
        ids = [b["id"] for b in boards]
        placeholders = ", ".join("?" for _ in ids)
        await self._db.execute(
            f"UPDATE tasks SET actionable = 0 WHERE board_id NOT IN ({placeholders})",
            tuple(ids),
        )
        # And cards this sync did NOT see on their own board are gone from
        # Trello (deleted/archived) — retire them, or ghosts haunt the plan.
        await self._db.execute(
            f"UPDATE tasks SET actionable = 0"
            f" WHERE board_id IN ({placeholders}) AND synced_at < ?",
            (*ids, run_started),
        )

        # Cards Paul ticked off IN TRELLO (moved to any done-ish list — 'Done',
        # 'Done!', 'Done Week 27th July') count on today's plan too.
        await self._db.execute(
            "UPDATE daily_12 SET done = 1, done_at = ? WHERE plan_date = ? AND done = 0"
            " AND task_id IN (SELECT id FROM tasks WHERE LOWER(list_name) LIKE 'done%'"
            " OR LOWER(list_name) IN ('complete', 'completed'))",
            (utc_now_iso(), (await self.paul_today()).isoformat()),
        )

        # Label system (Trello System Brief §3): self-heal once a day so the
        # 8 Domain/Flag labels always exist and their ids stay cached.
        today_key = (await self.paul_today()).isoformat()
        if await self._settings.get("labels_ensured_day") != today_key:
            try:
                await self.ensure_labels()
                await self._settings.set("labels_ensured_day", today_key)
            except Exception:
                logger.exception("Label setup failed — will retry next sync")

        await self._tag_untagged()
        await self._refresh_project_activity()
        await self._settings.set("trello_last_sync", utc_now_iso())
        return total

    async def ensure_labels(self, board_id: str = "") -> dict[str, str]:
        """Make the Master Board label system real (Brief §3/§5): keep named
        labels as-is, rename blank slots of the right colour, create what's
        missing. Idempotent; returns and caches {key: label_id}."""
        board_id = board_id or await self.resolve_board()
        existing = await self._trello.board_labels(board_id)
        by_name = {
            (l.get("name") or "").strip().lower(): l for l in existing if (l.get("name") or "").strip()
        }
        blanks: dict[str, list] = {}
        for label in existing:
            if not (label.get("name") or "").strip():
                blanks.setdefault(label.get("color") or "", []).append(label)
        ids: dict[str, str] = {}
        for key, (name, color) in {**DOMAIN_LABELS, **FLAG_LABELS}.items():
            hit = by_name.get(name.lower())
            if hit:
                ids[key] = hit["id"]
                continue
            try:
                if blanks.get(color):
                    blank = blanks[color].pop(0)
                    await self._trello.rename_label(blank["id"], name)
                    ids[key] = blank["id"]
                else:
                    created = await self._trello.create_label(board_id, name, color)
                    if created.get("id"):
                        ids[key] = created["id"]
            except Exception:
                logger.exception("Label setup failed for '%s'", name)
        await self._settings.set(LABEL_IDS_KEY, json.dumps({"board": board_id, "ids": ids}))
        return ids

    async def _label_ids(self) -> dict[str, str]:
        try:
            cached = json.loads(await self._settings.get(LABEL_IDS_KEY, "{}"))
            if cached.get("ids"):
                return cached["ids"]
        except Exception:
            pass
        try:
            return await self.ensure_labels()
        except Exception:
            logger.exception("Couldn't resolve board labels")
            return {}

    async def _upsert_task(
        self, card: dict, board: dict, list_name: str, money: int, waiting: bool
    ) -> None:
        existing = await self._db.fetch_one(
            "SELECT id FROM tasks WHERE trello_id = ?", (card["id"],)
        )
        common = (
            board["id"],
            board.get("name", ""),
            card.get("name", ""),
            card.get("desc", "")[:2000],
            card.get("shortUrl", ""),
            card.get("due") or "",
            money,
            1 if waiting else 0,
            (card.get("dateLastActivity") or "")[:10],
            list_name,
            1 if is_actionable_list(list_name) else 0,
            utc_now_iso(),
        )
        if existing:
            await self._db.execute(
                "UPDATE tasks SET board_id=?, board_name=?, title=?, description=?, url=?,"
                " due_date=?, money=?, waiting_on_paul=?, last_moved=?, list_name=?,"
                " actionable=?, synced_at=?"
                " WHERE trello_id=?",
                (*common, card["id"]),
            )
        else:
            await self._db.execute(
                "INSERT INTO tasks (board_id, board_name, title, description, url, due_date,"
                " money, waiting_on_paul, last_moved, list_name, actionable, synced_at, trello_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*common, card["id"]),
            )

    async def _tag_untagged(self) -> None:
        rows = await self._db.fetch_all(
            "SELECT id, title, description, list_name FROM tasks"
            " WHERE company_slug = '' AND actionable = 1 LIMIT 40"
        )
        if not rows:
            return
        listing = "\n".join(
            f"{i+1}. [{r['list_name']}] {r['title']} — {r['description'][:120]}"
            for i, r in enumerate(rows)
        )
        try:
            raw = await self._claude.quick(listing, system=TAGGING_SYSTEM, max_tokens=1500)
            start, end = raw.find("["), raw.rfind("]")
            items = json.loads(raw[start : end + 1]) if start >= 0 else []
        except Exception:
            logger.exception("Card tagging failed — will retry next sync")
            return
        for row, item in zip(rows, items):
            company = item.get("company", "")
            if company not in COMPANIES:
                company = ""
            project_id = 0
            project = (item.get("project") or "").strip()
            if company and project:
                project_id = await self._upsert_project(company, project)
            await self._db.execute(
                "UPDATE tasks SET company_slug = ?, project_id = ? WHERE id = ?",
                (company, project_id, row["id"]),
            )

    async def _upsert_project(self, company: str, name: str) -> int:
        row = await self._db.fetch_one(
            "SELECT id FROM projects WHERE company_slug = ? AND LOWER(name) = ?",
            (company, name.lower()),
        )
        if row:
            return int(row["id"])
        return await self._db.insert_returning_id(
            "INSERT INTO projects (company_slug, name, is_live, last_activity) VALUES (?, ?, 0, ?)",
            (company, name, utc_now_iso()),
        )

    async def _refresh_project_activity(self) -> None:
        rows = await self._db.fetch_all(
            "SELECT project_id, MAX(last_moved) AS latest FROM tasks"
            " WHERE project_id != 0 GROUP BY project_id"
        )
        for row in rows:
            await self._db.execute(
                "UPDATE projects SET last_activity = ? WHERE id = ?",
                (row["latest"] or "", row["project_id"]),
            )

    # -------------------------------------------------- live projects (§16.2)

    async def live_projects(self) -> dict[str, list[int]]:
        """Paul-confirmed live projects; Jarvis proposes the 3 most recently
        active per company where none are confirmed yet."""
        result: dict[str, list[int]] = {}
        for company in COMPANIES:
            rows = await self._db.fetch_all(
                "SELECT id FROM projects WHERE company_slug = ? AND is_live = 1 LIMIT 3",
                (company,),
            )
            if not rows:
                rows = await self._db.fetch_all(
                    "SELECT id FROM projects WHERE company_slug = ?"
                    " ORDER BY last_activity DESC LIMIT 3",
                    (company,),
                )
                for row in rows:
                    await self._db.execute(
                        "UPDATE projects SET is_live = 1 WHERE id = ?", (row["id"],)
                    )
            result[company] = [int(r["id"]) for r in rows]
        return result

    async def set_live_projects(self, company: str, project_names: list[str]) -> None:
        await self._db.execute(
            "UPDATE projects SET is_live = 0 WHERE company_slug = ?", (company,)
        )
        for name in project_names[:3]:
            project_id = await self._upsert_project(company, name)
            await self._db.execute("UPDATE projects SET is_live = 1 WHERE id = ?", (project_id,))

    # ------------------------------------------------------------- the twelve

    async def paul_today(self, timezone: str | None = None) -> date:
        tz = ZoneInfo(timezone or await self._settings.get("current_timezone", self._tz_default))
        return datetime.now(tz).date()

    def _card_from_row(self, r: dict) -> Card:
        return Card(
            id=int(r["id"]),
            title=r["title"],
            company=r["company_slug"],
            project_id=int(r["project_id"]),
            due_date=parse_iso_date(r["due_date"]),
            money=int(r["money"]),
            waiting_on_paul=bool(r["waiting_on_paul"]),
            last_moved=parse_iso_date(r["last_moved"]),
            defer_count=int(r["defer_count"]),
        )

    async def generate(self, plan_date: date | None = None, resync: bool = True) -> list[dict]:
        """Build (or rebuild) Today's Focus for the date. Idempotent per day.
        Pool = the 'Paul Today' list (≤3 per company) + 'Paul Personal' (≤3);
        variable length, zero is a valid day. §16 scoring orders the pool."""
        plan_date = plan_date or await self.paul_today()
        existing = await self.plan(plan_date)
        if existing:
            return existing
        if resync:
            try:
                await self.sync()
            except Exception:
                logger.exception("Trello sync failed — selecting from cache")

        business = [
            self._card_from_row(r)
            for r in await self._db.fetch_all(
                "SELECT * FROM tasks WHERE actionable = 1 AND LOWER(list_name) = ?",
                (self._today_list,),
            )
        ]
        personal = [
            self._card_from_row(r)
            for r in await self._db.fetch_all(
                "SELECT * FROM tasks WHERE actionable = 1 AND LOWER(list_name) = ?",
                (self._personal_list,),
            )
        ]
        selection = select_focus(
            business, personal, plan_date,
            per_company=self._per_company, personal_max=self._personal_max,
        )

        # A UNIQUE index on (plan_date, position) + conflict-ignoring inserts
        # make generation race-safe: if the 07:00 job and a "plan my day" land
        # together, exactly one plan wins.
        ignore = (
            "INSERT INTO daily_12 (plan_date, position, task_id, company_slug) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (plan_date, position) DO NOTHING"
            if self._db.dialect == "postgres"
            else "INSERT OR IGNORE INTO daily_12 (plan_date, position, task_id, company_slug)"
            " VALUES (?, ?, ?, ?)"
        )
        position = 0
        personal_ids = {c.id for c in selection.by_group.get("personal", [])}
        for card in selection.picks:
            position += 1
            group = "personal" if card.id in personal_ids else card.company
            await self._db.execute(ignore, (plan_date.isoformat(), position, card.id, group))
            await self._db.execute("UPDATE tasks SET score = ? WHERE id = ?", (card.score, card.id))
        return await self.plan(plan_date)

    async def plan(self, plan_date: date) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT d.position, d.done, d.company_slug, t.id AS task_id, t.title, t.trello_id,"
            " t.board_id, t.due_date, t.url, t.score"
            " FROM daily_12 d JOIN tasks t ON t.id = d.task_id"
            " WHERE d.plan_date = ? ORDER BY CASE d.position WHEN 0 THEN 99 ELSE d.position END",
            (plan_date.isoformat(),),
        )

    GROUP_LABELS = {**COMPANY_NAMES, "": "Other", "personal": "Personal"}

    async def format_plan(self, plan_date: date | None = None) -> str:
        """Today's Focus, grouped by company then Personal. Variable length —
        an empty day is a clear day, not a failure."""
        plan_date = plan_date or await self.paul_today()
        rows = await self.plan(plan_date)
        if not rows:
            return (
                "Today's Focus is clear — nothing queued in 'Paul Today' or 'Paul Personal'. "
                "Move cards in (or tell me what matters) and I'll line the day up."
            )
        main = [r for r in rows if r["position"] != 0]
        done_count = sum(1 for r in main if r["done"])
        lines = [
            f"TODAY'S FOCUS — {plan_date.strftime('%A %d %B')} · {done_count}/{len(main)} done"
        ]
        for group in COMPANIES + ["", "personal"]:
            group_rows = [r for r in main if r["company_slug"] == group]
            if not group_rows:
                continue
            lines.append("")
            lines.append(self.GROUP_LABELS.get(group, group).upper())
            for r in group_rows:
                mark = "✅" if r["done"] else "▫️"
                due = ""
                if r["due_date"]:
                    due_day = parse_iso_date(r["due_date"])
                    overdue = " — OVERDUE" if due_day and due_day < plan_date else ""
                    due = f" (due {r['due_date'][:10]}{overdue})"
                lines.append(f"{mark} {r['position']}. {r['title']}{due}")
        return "\n".join(lines)

    DONE_LISTS = {"done", "complete", "completed"}
    PARKED_LISTS = {"blocked", "waiting", "blocked/waiting"}

    async def recently_cleared(self, days: int = 7) -> str:
        """Cards that recently LEFT play — ticked to Done, archived, or parked.
        Injected beside the live plan: absence from the plan alone can't prove
        a remembered obligation is finished, so the brain gets told outright
        (the BMI-surveys bug: memory kept chasing cards the board had closed)."""
        cutoff = (
            datetime.now(ZoneInfo("UTC")) - timedelta(days=days)
        ).isoformat(timespec="seconds")
        rows = await self._db.fetch_all(
            "SELECT title, list_name FROM tasks WHERE actionable = 0"
            " AND synced_at >= ? ORDER BY synced_at DESC LIMIT 12",
            (cutoff,),
        )
        if not rows:
            return ""
        lines = [
            "RECENTLY CLEARED OR PARKED — these are NOT open tasks. Never say "
            "one is still due or chase it, whatever memory or old chat says:"
        ]
        for r in rows:
            list_name = (r["list_name"] or "").strip().lower()
            if list_name.startswith("done") or list_name in self.DONE_LISTS:
                state = "DONE — completed on the board"
            elif (
                "blocked" in list_name or list_name.startswith("waiting")
                or list_name in self.PARKED_LISTS
            ):
                state = f"parked in {r['list_name']} (deliberately not active)"
            else:
                state = "gone from the board (archived or removed)"
            lines.append(f"- {r['title']} — {state}")
        return "\n".join(lines)

    async def frog(self, plan_date: date | None = None) -> str:
        """Eat-the-frog (§8): the most-avoided undone item on today's list."""
        plan_date = plan_date or await self.paul_today()
        rows = [r for r in await self.plan(plan_date) if not r["done"]]
        if not rows:
            return ""
        best_title, best_score = "", -1.0
        for r in rows:
            task = await self._db.fetch_one(
                "SELECT last_moved, defer_count FROM tasks WHERE id = ?", (r["task_id"],)
            )
            if task is None:
                continue
            score = avoidance_score(
                parse_iso_date(task["last_moved"]), plan_date, int(task["defer_count"])
            )
            if score > best_score:
                best_title, best_score = r["title"], score
        return best_title if best_score > 0 else rows[0]["title"]

    # ------------------------------------------------- feedback → board (§16.8)

    async def find_plan_task(self, reference: str, plan_date: date | None = None) -> dict | None:
        """Resolve 'number 4' / a title fragment to a task on today's plan."""
        plan_date = plan_date or await self.paul_today()
        rows = await self.plan(plan_date)
        reference = reference.strip().lower()
        if reference.isdigit():
            n = int(reference)
            return next((r for r in rows if r["position"] == n), None)
        match, ties = best_title_match(reference, rows)
        return match or (ties[0] if ties else None)

    async def _done_list_id(self, board_id: str) -> str:
        """The Done list on the given board — cards move within their own
        board. The Master Board keeps a rolling weekly archive ('Done Week
        27th July', newest first in board order) — completions go to the
        CURRENT week's list, never the legacy 'Done!' pile (Brief §3.3)."""
        lists = await self._trello.board_lists(board_id)
        for l in lists:
            if l["name"].strip().lower().startswith("done week"):
                return l["id"]
        for l in lists:
            if l["name"].strip().lower().rstrip("!") in ("done", "complete", "completed"):
                return l["id"]
        return ""

    # -------------------------------- §3 planning rituals + §7 park-it

    WEEK_LIST = "this week"
    DUMP_LIST = "brain dump"
    WEEK_LISTING_KEY = "week_listing"

    async def cards_in_list(self, list_name: str, limit: int = 12) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT id, title, due_date FROM tasks WHERE actionable = 1"
            " AND LOWER(list_name) = ? ORDER BY score DESC, id LIMIT ?",
            (list_name.strip().lower(), limit),
        )

    async def week_preview(self, sunday: bool = False) -> str:
        """Numbered 'This Week' (and, on Sundays, 'Brain Dump') listing for the
        evening ritual; the numbering is remembered so 'queue 2 and 5' works."""
        week = await self.cards_in_list(self.WEEK_LIST)
        listing = [int(r["id"]) for r in week]
        lines = []
        if week:
            lines.append("THIS WEEK — pick tomorrow's cards ('queue 2 and 5', or by name):")
            for i, r in enumerate(week, 1):
                due = f" (due {r['due_date'][:10]})" if r["due_date"] else ""
                lines.append(f"{i}. {r['title']}{due}")
        if sunday:
            dump = await self.cards_in_list(self.DUMP_LIST)
            if dump:
                lines.append("")
                lines.append("BRAIN DUMP — Sunday grooming ('promote <name>' moves it to This Week):")
                offset = len(listing)
                for i, r in enumerate(dump, offset + 1):
                    lines.append(f"{i}. {r['title']}")
                listing += [int(r["id"]) for r in dump]
        await self._settings.set(self.WEEK_LISTING_KEY, json.dumps(listing))
        return "\n".join(lines)

    async def _find_card(self, reference: str, list_name: str) -> dict | None:
        reference = reference.strip().lower()
        if reference.isdigit():
            try:
                listing = json.loads(await self._settings.get(self.WEEK_LISTING_KEY, "[]"))
                task_id = listing[int(reference) - 1]
            except (IndexError, ValueError):
                return None
            return await self._db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        rows = await self._db.fetch_all(
            "SELECT * FROM tasks WHERE actionable = 1 AND LOWER(list_name) = ?",
            (list_name,),
        )
        match, ties = best_title_match(reference, rows)
        return match or (ties[0] if ties else None)

    async def _target_list_id(self, board_id: str, list_name: str) -> str:
        for l in await self._trello.board_lists(board_id):
            if l["name"].strip().lower() == list_name.strip().lower():
                return l["id"]
        return ""

    async def _move_to_list(self, task: dict, target_list: str) -> bool:
        board_id = task["board_id"] or await self.resolve_board()
        list_id = await self._target_list_id(board_id, target_list)
        if not list_id:
            return False
        await self._trello.move_card(task["trello_id"], list_id)
        await self._db.execute(
            "UPDATE tasks SET list_name = ? WHERE id = ?", (target_list, task["id"])
        )
        return True

    async def queue_for_today(self, reference: str) -> str:
        """Evening ritual: move a chosen card into 'Paul Today' for tomorrow."""
        task = await self._find_card(reference, self.WEEK_LIST)
        if task is None:
            task = await self._find_card(reference, self.DUMP_LIST)
        if task is None:
            return f"Couldn't place '{reference}' in This Week or Brain Dump."
        try:
            if await self._move_to_list(task, self._today_list.title()):
                return f"'{task['title']}' queued for tomorrow."
            return f"No '{self._today_list.title()}' list on that board yet, sir."
        except Exception:
            logger.exception("Queue move failed")
            return f"Trello wouldn't take the move for '{task['title']}' — noted locally; try again shortly."

    async def promote_to_week(self, reference: str) -> str:
        """Sunday grooming: Brain Dump → This Week."""
        task = await self._find_card(reference, self.DUMP_LIST)
        if task is None:
            return f"Couldn't find '{reference}' in Brain Dump."
        try:
            if await self._move_to_list(task, "This Week"):
                return f"'{task['title']}' promoted to This Week."
            return "No 'This Week' list on that board yet, sir."
        except Exception:
            logger.exception("Promote move failed")
            return f"Trello wouldn't take the move for '{task['title']}' — try again shortly."

    async def park(self, title: str) -> str:
        """§7 park-it: a distracting thought goes straight to Brain Dump."""
        title = title.strip().rstrip(".")
        if not title:
            return "Park what, sir? Give me the thought."
        try:
            board_id = await self.resolve_board()
            list_id = await self._target_list_id(board_id, self.DUMP_LIST)
            if not list_id:
                return "There's no 'Brain Dump' list on the board yet — create it and I'll park things there."
            await self._trello.create_card(list_id, title)
            return f"Parked: '{title}'. Back to what you were doing."
        except Exception:
            logger.exception("Park failed")
            return f"Couldn't reach Trello to park '{title}' — jot it back to me shortly."

    async def _fuzzy_list_id(self, board_id: str, name: str) -> tuple[str, str]:
        """Match a spoken column name to a real board list — exact first,
        then contains, then word overlap. Returns (id, actual name)."""
        name_l = name.strip().lower()
        lists = await self._trello.board_lists(board_id)
        for l in lists:
            if l["name"].strip().lower() == name_l:
                return l["id"], l["name"]
        for l in lists:
            ln = l["name"].strip().lower()
            if name_l and (name_l in ln or ln in name_l):
                return l["id"], l["name"]
        words = set(name_l.split())
        best, overlap = None, 0
        for l in lists:
            hit = len(words & set(l["name"].lower().split()))
            if hit > overlap:
                best, overlap = l, hit
        if best is not None:
            return best["id"], best["name"]
        return "", ""

    async def _inbox_list_id(self) -> tuple[str, str]:
        """Default landing list for a new card when Paul names no column:
        classic inboxes first, then Brain Dump (the conveyor's intake)."""
        board_id = await self.resolve_board()
        lists = await self._trello.board_lists(board_id)
        for l in lists:
            if l["name"].strip().lower() in ("inbox", "to do", "todo", "backlog", "brain dump"):
                return l["id"], l["name"]
        return (lists[0]["id"], lists[0]["name"]) if lists else ("", "")

    async def mark_done(self, reference: str) -> str:
        task = await self.find_plan_task(reference)
        if task is None:
            return f"Couldn't find '{reference}' on today's 12."
        plan_date = (await self.paul_today()).isoformat()
        await self._db.execute(
            "UPDATE daily_12 SET done = 1, done_at = ? WHERE plan_date = ? AND task_id = ?",
            (utc_now_iso(), plan_date, task["task_id"]),
        )
        try:
            board_id = task["board_id"] or await self.resolve_board()
            done_list = await self._done_list_id(board_id)
            if done_list:
                await self._trello.move_card(task["trello_id"], done_list)
        except Exception:
            logger.exception("Trello move failed (local state updated)")
        rows = [r for r in await self.plan(await self.paul_today()) if r["position"] != 0]
        done = sum(1 for r in rows if r["done"])
        total = len(rows)
        if total and done >= total:
            return (
                f"'{task['title']}' done — {done} of {total}. That's the lot: "
                "Today's Focus cleared. Outstanding."
            )
        return f"'{task['title']}' done and moved on the board. {done} of {total}."

    async def defer(self, reference: str, due_iso: str, human_when: str) -> str:
        task = await self.find_plan_task(reference)
        if task is None:
            return f"Couldn't find '{reference}' on today's 12."
        await self._db.execute(
            "UPDATE tasks SET due_date = ?, defer_count = defer_count + 1 WHERE id = ?",
            (due_iso, task["task_id"]),
        )
        try:
            await self._trello.set_due(task["trello_id"], due_iso)
        except Exception:
            logger.exception("Trello due-date update failed")
        row = await self._db.fetch_one(
            "SELECT defer_count FROM tasks WHERE id = ?", (task["task_id"],)
        )
        nudge = ""
        if row and int(row["defer_count"]) >= 3:
            nudge = (
                " I'll note that's this one's third deferral, sir — it will keep rising up the "
                "list until we deal with it. Shall we give it two minutes tomorrow, first thing?"
            )
        return f"'{task['title']}' pushed to {human_when}.{nudge}"

    async def create(
        self, title: str, assignee: str = "", due_iso: str = "", list_name: str = "",
        domain: str = "", flags: list[str] | None = None, description: str = "",
    ) -> str:
        """New card — in the column Paul NAMED when he named one, carrying the
        Brief §3 label system (one Domain + stacked Flags) and the member.
        An exact-title twin already in play is never duplicated (Brief §4.1)."""
        try:
            def _norm(text: str) -> str:
                return " ".join(re.findall(r"[\w£']+", text.lower()))

            twin = next(
                (
                    r for r in await self._db.fetch_all(
                        "SELECT title, list_name FROM tasks WHERE actionable = 1"
                    )
                    if _norm(r["title"]) == _norm(title)
                ),
                None,
            )
            if twin is not None:
                return (
                    f"'{twin['title']}' is already on the board (in {twin['list_name']}) "
                    "— not duplicating it. Say 'update' or 'archive' it if you want changes."
                )
            list_label = ""
            list_id = ""
            if list_name:
                board_id = await self.resolve_board()
                list_id, list_label = await self._fuzzy_list_id(board_id, list_name)
            if not list_id:
                list_id, list_label = await self._inbox_list_id()
            card = await self._trello.create_card(
                list_id, title, desc=description, due_iso=due_iso
            )
            applied: list[str] = []
            wanted = ([domain] if domain else []) + [f for f in (flags or []) if f]
            if wanted:
                label_ids = await self._label_ids()
                spec = {**DOMAIN_LABELS, **FLAG_LABELS}
                for key in wanted:
                    label_id = label_ids.get(key)
                    if not label_id or key not in spec:
                        continue
                    try:
                        await self._trello.add_label(card["id"], label_id)
                        applied.append(spec[key][0])
                    except Exception:
                        logger.exception("Label '%s' failed on new card", key)
            if assignee:
                member_id = await self._member_id(assignee)
                if member_id:
                    await self._trello.assign_member(card["id"], member_id)
                else:
                    await self._trello.comment(card["id"], f"For {assignee} (from Paul, via Jarvis)")
            # Paul as member is set silently — 'on Paul' is noise to Paul.
            who = f" on {assignee.title()}" if assignee and assignee.lower() != "paul" else ""
            where = f" in {list_label}" if list_label else ""
            labelled = f" · {', '.join(applied)}" if applied else ""
            return f"Created '{title}'{who}{where}{labelled}."
        except Exception:
            logger.exception("Trello create failed")
            return f"Couldn't reach Trello to create '{title}' — I'll keep it noted; try again shortly."

    async def _member_id(self, name: str) -> str:
        board_id = await self.resolve_board()
        name_lower = name.strip().lower()
        for member in await self._trello.board_members(board_id):
            if name_lower in member.get("fullName", "").lower() or name_lower in member.get("username", "").lower():
                return member["id"]
        return ""

    async def archive(self, reference: str) -> str:
        """Really remove a card (archive on Trello) — the capability behind
        'delete the test order', so it can never again be claimed unperformed."""
        rows = await self._db.fetch_all("SELECT * FROM tasks WHERE actionable = 1")
        task, ties = best_title_match(reference, rows)
        if task is None and ties:
            # Archiving is permanent — a dead heat never gets guessed at
            # (the Eva-lunch bug: 'the lunch card' must not bin a bystander).
            names = "' and '".join(t["title"] for t in ties[:3])
            return (
                f"Hold on — more than one card matches '{reference}': '{names}'. "
                "Archiving is permanent, so give me the exact title and I'll do it."
            )
        if task is None:
            return f"Couldn't find '{reference}' on the board to archive."
        try:
            await self._trello.archive_card(task["trello_id"])
        except Exception:
            logger.exception("Trello archive failed")
            return f"Trello wouldn't archive '{task['title']}' just now — try again shortly."
        await self._db.execute("UPDATE tasks SET actionable = 0 WHERE id = ?", (task["id"],))
        await self._db.execute(
            "DELETE FROM daily_12 WHERE task_id = ? AND plan_date = ?",
            (task["id"], (await self.paul_today()).isoformat()),
        )
        return f"Archived '{task['title']}' — actually gone from the board this time."

    async def comment(self, reference: str, text: str) -> str:
        task = await self.find_plan_task(reference)
        if task is None:
            return f"Couldn't find '{reference}' on today's 12."
        try:
            await self._trello.comment(task["trello_id"], f"{text} — Paul, via Jarvis")
        except Exception:
            logger.exception("Trello comment failed")
            return "Couldn't reach Trello for the comment — try again shortly."
        return f"Noted on '{task['title']}'."


_FILLER_WORDS = {
    "the", "a", "an", "that", "this", "it", "my", "our", "card", "task", "one", "please",
}


def best_title_match(reference: str, rows: list[dict]) -> tuple[dict | None, list[dict]]:
    """Resolve a spoken reference to ONE card. An exact title wins outright —
    'the Lunch card' must hit 'Lunch', never a longer title that merely
    contains the word. Otherwise: most reference words hit, then how much of
    the title they cover. A dead heat returns (None, candidates) so the
    caller can ask instead of guessing."""
    words = re.findall(r"[\w£']+", reference.lower())
    ref_words = [w for w in words if w not in _FILLER_WORDS] or words
    if not ref_words:
        return None, []
    ref_key = " ".join(ref_words)
    scored: list[tuple[tuple[int, float], dict]] = []
    for row in rows:
        title_words = re.findall(r"[\w£']+", row["title"].lower())
        meaningful = [w for w in title_words if w not in _FILLER_WORDS] or title_words
        if " ".join(meaningful) == ref_key:
            return row, []
        hits = sum(
            1
            for w in ref_words
            if any(
                t == w
                or (len(w) >= 4 and t.startswith(w))
                or (len(t) >= 4 and w.startswith(t))
                for t in title_words
            )
        )
        if hits:
            scored.append(((hits, hits / max(len(title_words), 1)), row))
    if not scored:
        return None, []
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, [row for score, row in scored if score == scored[0][0]]
    return scored[0][1], []


def _money_from_labels(labels: list[str]) -> int:
    joined = " ".join(labels)
    if "£££" in joined:
        return 3
    if "££" in joined:
        return 2
    if "£" in joined or "money" in joined or "revenue" in joined or "payment" in joined:
        return 1
    return 0


def relative_due(base: date, when: str) -> tuple[str, str]:
    """'friday' / 'tomorrow' / 'next week' → (ISO date, human label)."""
    when_lower = when.strip().lower()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if when_lower == "tomorrow":
        target = base + timedelta(days=1)
    elif when_lower in ("next week", "monday next week"):
        target = base + timedelta(days=(7 - base.weekday()) or 7)
    elif when_lower in weekdays:
        delta = (weekdays.index(when_lower) - base.weekday()) % 7 or 7
        target = base + timedelta(days=delta)
    else:
        parsed = parse_iso_date(when)
        target = parsed or base + timedelta(days=1)
    return target.isoformat(), target.strftime("%A %d %B")
