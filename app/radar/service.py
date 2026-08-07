"""§6/§7/§8 — the view, the daily push, and voice answers. Pure derivation
over what sync.py already wrote; this module makes zero Trello calls, so
it can never accidentally become the second watcher the brief forbids.

§1's honesty rule again: coverage (§2) rides through every read so a
surface can always tell 'nothing to worry about' from 'I can't see this'.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.radar.states import card_state, cycle_stats, taking_too_long_signals
from app.radar.sync import BOARDS_SEEN_KEY, LAST_SYNC_KEY

ROSTER = ["Harry", "Adriana", "Kiefer", "Paul"]
STALE_AFTER_HOURS = 2
NEEDS_YOU_CAP = 5

DAILY_KEY = "radar_daily_pushed_date"
INTERRUPT_KEY = "radar_interrupt_pushed_date"
KNOWN_OVERDUE_KEY = "radar_known_warroom_overdue"


def _parse(ts: str):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _person_hits(owners: list[str]) -> list[str]:
    return [name for name in ROSTER if any(name.lower() in (o or "").lower() for o in owners)]


class TeamRadar:
    def __init__(self, db, store) -> None:
        self.db = db
        self.store = store

    # ------------------------------------------------------------ §2 coverage

    async def coverage(self) -> dict:
        try:
            seen = json.loads(await self.store.get(BOARDS_SEEN_KEY, "{}"))
        except Exception:
            seen = {}
        last_sync = await self.store.get(LAST_SYNC_KEY, "")
        dt = _parse(last_sync)
        stale = dt is None or (datetime.now(timezone.utc) - dt) > timedelta(hours=STALE_AFTER_HOURS)
        boards_by_person = await self._boards_by_person()
        return {
            "boards": seen.get("ok", []),
            "sync_errors": seen.get("errors", []),
            "last_sync_at": last_sync or None,
            "stale": stale,
            "boards_by_person": boards_by_person,
            "coverage_note": (
                "This may not be everything — confirm Harry, Adriana and Kiefer don't "
                "work on a board Jarvis can't currently reach."
            ),
        }

    async def _boards_by_person(self) -> dict:
        rows = await self.db.fetch_all(
            "SELECT DISTINCT board_name, owners FROM radar_cards WHERE archived = 0"
        )
        out: dict[str, set] = {name: set() for name in ROSTER}
        for row in rows:
            try:
                owners = json.loads(row["owners"])
            except Exception:
                owners = []
            for person in _person_hits(owners):
                out[person].add(row["board_name"])
        return {name: sorted(boards) for name, boards in out.items()}

    # ------------------------------------------------------------ derivation

    async def _cycle_medians(self) -> dict:
        rows = await self.db.fetch_all(
            "SELECT list_name, entered_at, left_at FROM radar_list_history WHERE left_at != ''"
        )
        by_list: dict[str, list[float]] = {}
        for row in rows:
            entered, left = _parse(row["entered_at"]), _parse(row["left_at"])
            if entered and left:
                by_list.setdefault(row["list_name"], []).append((left - entered).total_seconds() / 86400)
        return {name: cycle_stats(days) for name, days in by_list.items()}

    async def _list_visit_counts(self) -> dict[str, dict[str, int]]:
        rows = await self.db.fetch_all("SELECT card_id, list_name FROM radar_list_history")
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            per_card = out.setdefault(row["card_id"], {})
            per_card[row["list_name"]] = per_card.get(row["list_name"], 0) + 1
        return out

    async def _derive_all(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        cards = await self.db.fetch_all("SELECT * FROM radar_cards WHERE archived = 0")
        medians = await self._cycle_medians()
        visit_counts = await self._list_visit_counts()
        out = []
        for card in cards:
            try:
                owners_list = json.loads(card["owners"])
            except Exception:
                owners_list = []
            state = card_state(card, now)
            signals = taking_too_long_signals(
                {**card, "_now": now},
                visit_counts.get(card["card_id"], {}),
                medians.get(card["list_name"], {"median": None, "count": 0, "learning": True}),
            )
            out.append({
                **card, "owners_list": owners_list, "state": state["state"],
                "state_evidence": state["evidence"], "signals": signals,
            })
        return out

    # ------------------------------------------------------------ §6a needs you

    async def needs_you(self) -> dict:
        derived = await self._derive_all()
        ranked = []
        for card in derived:
            if not card["owners_list"]:
                continue
            if card["state"] == "overdue":
                rank = 0
            elif any(s["kind"] == "due_churn" for s in card["signals"]):
                rank = 1
            elif card["state"] == "no_update":
                rank = 2
            elif card["state"] == "due_soon":
                rank = 3
            else:
                continue
            ranked.append((rank, card))
        ranked.sort(key=lambda t: (t[0], t[1].get("due") or "9999"))
        top = ranked[:NEEDS_YOU_CAP]
        rows = [
            {
                "card_id": c["card_id"], "title": c["title"],
                "who": ", ".join(c["owners_list"]) or "unassigned",
                "company": c["company_tag"],
                "evidence": c["state_evidence"] or next((s["evidence"] for s in c["signals"]), ""),
                "url": c.get("short_url") or f"https://trello.com/c/{c['card_id']}",
            }
            for _, c in top
        ]
        return {"rows": rows, "overflow": max(0, len(ranked) - NEEDS_YOU_CAP)}

    # ------------------------------------------------------------ §6b columns

    async def columns(self) -> dict:
        derived = await self._derive_all()
        boards_by_person = await self._boards_by_person()
        out = {}
        for person in ROSTER:
            person_cards = [c for c in derived if person in c["owners_list"]]
            counts = {"on_track": 0, "due_soon": 0, "overdue": 0, "no_update": 0}
            by_company: dict[str, list] = {}
            for c in person_cards:
                counts[c["state"]] = counts.get(c["state"], 0) + 1
                by_company.setdefault(c["company_tag"] or "Unclassified", []).append({
                    "card_id": c["card_id"], "title": c["title"], "list_name": c["list_name"],
                    "due": c["due"], "state": c["state"], "evidence": c["state_evidence"],
                    "url": c.get("short_url", ""),
                })
            visible = boards_by_person.get(person, [])
            out[person] = {
                "boards_visible": visible,
                "no_boards_visible": not visible,
                "counts": counts,
                "by_company": by_company,
            }
        return out

    # ------------------------------------------------------------ §6c company rollup

    async def company_rollup(self) -> list[dict]:
        derived = await self._derive_all()
        by_company: dict[str, list] = {}
        for c in derived:
            by_company.setdefault(c["company_tag"] or "Unclassified", []).append(c)
        out = []
        for company, cards in by_company.items():
            moving = sum(1 for c in cards if c["state"] in ("on_track", "due_soon"))
            stuck = sum(1 for c in cards if c["state"] in ("overdue", "no_update"))
            oldest = min(cards, key=lambda c: c.get("first_seen") or "9999", default=None)
            out.append({
                "company": company, "moving": moving, "stuck": stuck,
                "oldest": oldest["title"] if oldest else None,
            })
        return out

    # ------------------------------------------------------------ §7 daily push

    async def daily_line(self, today_iso: str) -> str:
        """One line, once a day, alongside Today's Focus. Silence when
        nothing's slipped — a daily 'all clear' trains Paul to stop reading."""
        if await self.store.get(DAILY_KEY, "") == today_iso:
            return ""
        await self.store.set(DAILY_KEY, today_iso)
        needs = await self.needs_you()
        overdue = [r for r in needs["rows"] if "overdue" in r["evidence"]]
        if not overdue:
            return ""
        if len(overdue) == 1:
            r = overdue[0]
            return f"Team: {r['who']}'s {r['title']} is overdue — {r['evidence']}."
        parts = "; ".join(f"{r['who']}'s {r['title']} ({r['evidence']})" for r in overdue[:3])
        more = f" +{len(overdue) - 3} more" if len(overdue) > 3 else ""
        return f"Team: {len(overdue)} overdue — {parts}.{more}"

    async def check_interrupt(self, today_iso: str) -> str:
        """§7's interrupt, capped at once a day: a War Room-approved card
        crossing into overdue. Everything else waits for the daily line."""
        if await self.store.get(INTERRUPT_KEY, "") == today_iso:
            return ""
        derived = await self._derive_all()
        try:
            known = set(json.loads(await self.store.get(KNOWN_OVERDUE_KEY, "[]")))
        except Exception:
            known = set()
        now_overdue = {c["card_id"] for c in derived if c["state"] == "overdue" and c["warroom_session_id"]}
        await self.store.set(KNOWN_OVERDUE_KEY, json.dumps(sorted(now_overdue)))
        newly = now_overdue - known
        if not newly:
            return ""
        card = next(c for c in derived if c["card_id"] in newly)
        await self.store.set(INTERRUPT_KEY, today_iso)
        return f"Heads up — \"{card['title']}\" from your War Room session just went overdue: {card['state_evidence']}."

    # ------------------------------------------------------------ §8 voice

    async def rollup_spoken(self) -> str:
        rollup = await self.company_rollup()
        coverage = await self.coverage()
        if not rollup:
            return "Nothing on the radar yet."
        moving_total = sum(r["moving"] for r in rollup)
        stuck_total = sum(r["stuck"] for r in rollup)
        lines = [f"{moving_total} cards moving, {stuck_total} stuck across {len(rollup)} companies."]
        stuck_companies = [r["company"] for r in rollup if r["stuck"] > 0]
        if stuck_companies:
            lines.append(f"Stuck in: {', '.join(stuck_companies)}.")
        if coverage["stale"]:
            lines.append("Heads up — this data's more than 2 hours old.")
        return " ".join(lines)

    async def person_cards_spoken(self, name: str) -> str:
        match = next((p for p in ROSTER if p.lower() == name.strip().lower()), None)
        if match is None:
            return f"I don't track {name} on the radar — only Harry, Adriana, Kiefer and you."
        cols = await self.columns()
        data = cols[match]
        if data["no_boards_visible"]:
            return f"No boards visible for {match} — I might not be able to see their work."
        total = sum(data["counts"].values())
        if total == 0:
            return f"Nothing assigned to {match} on the boards I can see."
        bits = ", ".join(f"{company}: {len(cards)}" for company, cards in data["by_company"].items())
        return f"{match} has {total} card(s) — {bits}."

    async def slipping_spoken(self) -> str:
        needs = await self.needs_you()
        if not needs["rows"]:
            return "Nothing slipping right now."
        lines = [f"{len(needs['rows'])} thing(s)."]
        lines.extend(f"{r['who']}'s {r['title']} — {r['evidence']}." for r in needs["rows"])
        if needs["overflow"]:
            lines.append(f"Plus {needs['overflow']} more.")
        return " ".join(lines)

    async def taking_too_long_spoken(self) -> str:
        derived = await self._derive_all()
        flagged = [c for c in derived if c["signals"]]
        if not flagged:
            return "Nothing's flagged as taking too long right now."
        lines = [f"{c['title']} — {c['signals'][0]['evidence']}." for c in flagged[:5]]
        return " ".join(lines)

    async def project_status(self, session_id: int) -> dict:
        rows = await self.db.fetch_all(
            "SELECT * FROM radar_cards WHERE warroom_session_id = ? AND archived = 0", (session_id,)
        )
        now = datetime.now(timezone.utc)
        cards = []
        for row in rows:
            state = card_state(row, now)
            cards.append({"title": row["title"], "state": state["state"], "evidence": state["evidence"]})
        return {"cards": cards}

    async def project_status_spoken(self, session_id: int) -> str:
        status = await self.project_status(session_id)
        if not status["cards"]:
            return "No cards on the radar from that session yet."
        moving = sum(1 for c in status["cards"] if c["state"] in ("on_track", "due_soon"))
        stuck = sum(1 for c in status["cards"] if c["state"] in ("overdue", "no_update"))
        return f"{len(status['cards'])} card(s): {moving} moving, {stuck} stuck."
