"""Trello sync, company/project tagging, Daily 12 generation and board write-back."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.clients.anthropic_client import ClaudeClient
from app.core.store import SettingsStore, utc_now_iso
from app.daily12.scoring import (
    COMPANIES,
    COMPANY_NAMES,
    Card,
    is_actionable_list,
    parse_iso_date,
    select_daily_12,
)
from app.daily12.trello import TrelloClient
from app.db.base import Database

logger = logging.getLogger(__name__)

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
    ) -> None:
        self._db = db
        self._trello = trello
        self._claude = claude
        self._settings = SettingsStore(db)
        self._tz_default = timezone_default
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
        tasks cache; retire cached cards that fall outside the scope; tag any
        untagged cards."""
        boards = await self._scoped_boards()

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

        await self._tag_untagged()
        await self._refresh_project_activity()
        await self._settings.set("trello_last_sync", utc_now_iso())
        return total

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

    async def generate(self, plan_date: date | None = None, resync: bool = True) -> list[dict]:
        """Build (or rebuild) the Daily 12 for the date. Idempotent per day —
        an existing plan is returned untouched unless resync forces scoring
        anew before the day starts."""
        plan_date = plan_date or await self.paul_today()
        existing = await self.plan(plan_date)
        if existing:
            return existing
        if resync:
            try:
                await self.sync()
            except Exception:
                logger.exception("Trello sync failed — selecting from cache")

        rows = await self._db.fetch_all(
            "SELECT * FROM tasks WHERE actionable = 1 AND company_slug != ''"
        )
        cards = [
            Card(
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
            for r in rows
        ]
        selection = select_daily_12(cards, await self.live_projects(), plan_date)

        # A UNIQUE index on (plan_date, position) + conflict-ignoring inserts
        # make generation race-safe: if the 07:00 job and a "plan my 12" land
        # together, exactly one plan wins.
        ignore = (
            "INSERT INTO daily_12 (plan_date, position, task_id, company_slug) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (plan_date, position) DO NOTHING"
            if self._db.dialect == "postgres"
            else "INSERT OR IGNORE INTO daily_12 (plan_date, position, task_id, company_slug)"
            " VALUES (?, ?, ?, ?)"
        )
        position = 0
        for card in selection.picks[:12]:
            position += 1
            await self._db.execute(ignore, (plan_date.isoformat(), position, card.id, card.company))
            await self._db.execute("UPDATE tasks SET score = ? WHERE id = ?", (card.score, card.id))
        if selection.bonus is not None:
            await self._db.execute(
                ignore, (plan_date.isoformat(), 0, selection.bonus.id, selection.bonus.company)
            )
        return await self.plan(plan_date)

    async def plan(self, plan_date: date) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT d.position, d.done, d.company_slug, t.id AS task_id, t.title, t.trello_id,"
            " t.board_id, t.due_date, t.url, t.score"
            " FROM daily_12 d JOIN tasks t ON t.id = d.task_id"
            " WHERE d.plan_date = ? ORDER BY CASE d.position WHEN 0 THEN 99 ELSE d.position END",
            (plan_date.isoformat(),),
        )

    async def format_plan(self, plan_date: date | None = None) -> str:
        """The 12, grouped by company, bonus hidden until 12/12 (§16.6-7)."""
        plan_date = plan_date or await self.paul_today()
        rows = await self.plan(plan_date)
        if not rows:
            return "No Daily 12 yet for today — say 'plan my 12' and I'll build it."
        main = [r for r in rows if r["position"] != 0]
        bonus = next((r for r in rows if r["position"] == 0), None)
        done_count = sum(1 for r in main if r["done"])
        lines = [f"THE DAILY 12 — {plan_date.strftime('%A %d %B')} · {done_count}/12 done"]
        for company in COMPANIES:
            company_rows = [r for r in main if r["company_slug"] == company]
            if not company_rows:
                continue
            lines.append("")
            lines.append(COMPANY_NAMES.get(company, company).upper())
            for r in company_rows:
                mark = "✅" if r["done"] else "▫️"
                due = f" (due {r['due_date'][:10]})" if r["due_date"] else ""
                lines.append(f"{mark} {r['position']}. {r['title']}{due}")
        if done_count >= 12 and bonus is not None:
            mark = "✅" if bonus["done"] else "🎁"
            lines.append("")
            lines.append(f"{mark} BONUS UNLOCKED: {bonus['title']}")
        return "\n".join(lines)

    # ------------------------------------------------- feedback → board (§16.8)

    async def find_plan_task(self, reference: str, plan_date: date | None = None) -> dict | None:
        """Resolve 'number 4' / a title fragment to a task on today's plan."""
        plan_date = plan_date or await self.paul_today()
        rows = await self.plan(plan_date)
        reference = reference.strip().lower()
        if reference.isdigit():
            n = int(reference)
            return next((r for r in rows if r["position"] == n), None)
        scored = [
            (sum(1 for w in reference.split() if w in r["title"].lower()), r) for r in rows
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored and scored[0][0] > 0 else None

    async def _done_list_id(self, board_id: str) -> str:
        """The Done list on the given board — cards move within their own board."""
        for l in await self._trello.board_lists(board_id):
            if l["name"].strip().lower() in ("done", "complete", "completed"):
                return l["id"]
        return ""

    async def _inbox_list_id(self) -> str:
        board_id = await self.resolve_board()
        lists = await self._trello.board_lists(board_id)
        for l in lists:
            if l["name"].strip().lower() in ("inbox", "to do", "todo", "backlog"):
                return l["id"]
        return lists[0]["id"] if lists else ""

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
        rows = await self.plan(await self.paul_today())
        done = sum(1 for r in rows if r["position"] != 0 and r["done"])
        if done >= 12:
            bonus = next((r for r in rows if r["position"] == 0), None)
            extra = f" That's all 12 — bonus unlocked: {bonus['title']}." if bonus else " That's all 12. Outstanding."
            return f"'{task['title']}' done — 12 of 12.{extra}"
        return f"'{task['title']}' done and moved on the board. {done} of 12."

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

    async def create(self, title: str, assignee: str = "", due_iso: str = "") -> str:
        try:
            list_id = await self._inbox_list_id()
            card = await self._trello.create_card(list_id, title, due_iso=due_iso)
            if assignee:
                member_id = await self._member_id(assignee)
                if member_id:
                    await self._trello.assign_member(card["id"], member_id)
                else:
                    await self._trello.comment(card["id"], f"For {assignee} (from Paul, via Jarvis)")
            who = f" on {assignee.title()}" if assignee else ""
            return f"Created '{title}'{who}."
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
