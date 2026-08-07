"""The War Room orchestrator (7 Aug brief). Ties together seats.py
(who sits where), redaction.py (§9), protocol.py (the phases), report.py
(§5 merge) and trello_plan.py (§3 the Quick Board's real deliverable).

Phase 0 (frame) never spends a token — it's pure context assembly + tier
choice, shown back to Paul before anything runs. confirm_and_run() is the
one call that actually pays for model calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

from app.warroom.protocol import (
    DISSENT_PUSH, call_seat, looks_like_agreement, phase1_system, phase2_system,
    phase3_system, phase4_system,
)
from app.warroom.redaction import redact
from app.warroom.report import merge_report
from app.warroom.seats import build_seats
from app.warroom import trello_plan

logger = logging.getLogger(__name__)

FRAME_KEY = "warroom_pending_frame"
ROTATION_KEY = "warroom_role_rotation"
MONTHLY_KEY_PREFIX = "warroom_monthly_spend:"

FULL_WALLCLOCK_SEC = 900
QUICK_WALLCLOCK_SEC = 180

MONEY = re.compile(r"[£$€]\s?([\d,]+(?:\.\d+)?)\s?(k|m)?", re.IGNORECASE)
CONTRACT_WORDS = re.compile(r"\b(contract|legal|regulat|licen[cs]e|compliance)\b", re.IGNORECASE)
PERSON_JOB_WORDS = re.compile(
    r"\b(hire|hiring|fire|firing|redundan|restructur|sack|dismiss|role change)\b", re.IGNORECASE
)
HARD_TO_REVERSE = re.compile(r"\b(permanent|irreversible|can't undo|cannot undo|burn.?the.?bridge)\b", re.IGNORECASE)
TRIVIAL_HINT = re.compile(
    r"^\s*(what|when|how much|who|where|is|does|did)\b.{0,60}\?\s*$", re.IGNORECASE
)


class WarRoom:
    def __init__(
        self, db, claude, store, memory, living, layer_factory, settings,
        openai_client=None, gemini_client=None, company_names=None,
        full_budget_usd: float = 5.0, quick_budget_usd: float = 0.5,
        monthly_ceiling_usd: float = 100.0, escalate_value_gbp: float = 10000.0,
    ) -> None:
        self.db = db
        self.claude = claude
        self.store = store
        self.memory = memory
        self.living = living
        self._layer_factory = layer_factory
        self._settings = settings
        self._openai = openai_client
        self._gemini = gemini_client
        self._company_names = company_names or []
        self.full_budget = full_budget_usd
        self.quick_budget = quick_budget_usd
        self.monthly_ceiling = monthly_ceiling_usd
        self.escalate_value_gbp = escalate_value_gbp

    @property
    def configured(self) -> bool:
        return self._openai is not None and self._gemini is not None

    # ------------------------------------------------------------ §2 tier choice

    def choose_tier(self, question: str, forced: str = "") -> tuple[str, str]:
        if forced in ("full", "quick"):
            return forced, "you asked for it directly"
        for amount, mult in MONEY.findall(question):
            try:
                value = float(amount.replace(",", "")) * (1000 if mult.lower() == "k" else 1_000_000 if mult.lower() == "m" else 1)
            except ValueError:
                continue
            if value > self.escalate_value_gbp:
                return "full", f"the figure mentioned (~{amount}{mult}) is over the £{self.escalate_value_gbp:,.0f} threshold"
        if CONTRACT_WORDS.search(question):
            return "full", "it touches a contract or a legal/regulatory commitment"
        if PERSON_JOB_WORDS.search(question):
            return "full", "it affects a person's job"
        if HARD_TO_REVERSE.search(question):
            return "full", "it reads as hard or expensive to reverse"
        mentioned = [c for c in self._company_names if c.lower() in question.lower()]
        if len(mentioned) > 1:
            return "full", f"it touches more than one company ({', '.join(mentioned)})"
        return "quick", "a single, reversible piece of work — the Quick Board fits"

    # ------------------------------------------------------------ Phase 0 — frame

    async def frame(self, question: str, forced_tier: str = "", session_type: str = "") -> dict:
        question = question.strip()
        tier, reason = self.choose_tier(question, forced_tier)
        trivial = bool(TRIVIAL_HINT.match(question)) and len(question) < 80
        context = await self._gather_context(question)
        cost_estimate = self.full_budget if tier == "full" else self.quick_budget
        month_spent = await self.monthly_spend()
        framed = {
            "question": question,
            "tier": tier,
            "tier_reason": reason,
            "session_type": session_type or "strategy",
            "context_summary": context,
            "cost_estimate_usd": cost_estimate,
            "trivial_hint": trivial,
            "month_spent_usd": month_spent,
            "over_monthly_ceiling": (month_spent + cost_estimate) > self.monthly_ceiling,
        }
        await self.store.set(FRAME_KEY, json.dumps(framed))
        return framed

    async def _gather_context(self, question: str) -> str:
        """§0's 'the context' — pulled from the second brain, THE WALL held
        by construction: never rooms=['private'], never include_private."""
        parts = []
        if self.memory is not None:
            try:
                chunks = await self.memory.search(
                    question, k=10, rooms=["companies", "finances", "people", "you"],
                    include_private=False,
                )
                parts.extend(f"[{c['room']}] {c['content']}" for c in chunks)
            except Exception:
                logger.exception("War Room context recall failed — continuing without")
        if self.living is not None:
            try:
                facts = await self.living.all_current(exclude_private=True)
                parts.extend(f"{f['key']}: {f['value']}" for f in facts[:30])
            except Exception:
                logger.exception("War Room living-facts recall failed — continuing without")
        prior = await self._prior_decisions(question)
        if prior:
            parts.append("PRIOR WAR ROOM DECISIONS:\n" + prior)
        return "\n".join(parts)[:20000]

    async def _prior_decisions(self, question: str) -> str:
        rows = await self.db.fetch_all(
            "SELECT question, verdict, created_at FROM warroom_sessions"
            " WHERE status = 'done' ORDER BY id DESC LIMIT 20"
        )
        if not rows:
            return ""
        return "\n".join(f"- {r['created_at'][:10]}: {r['question']} → {r['verdict']}" for r in rows)

    # ------------------------------------------------------------ Phase 1-5

    async def confirm_and_run(self, unredacted: bool = False) -> dict:
        raw = await self.store.get(FRAME_KEY, "")
        if not raw:
            return {"error": "Nothing framed yet — ask a question first."}
        framed = json.loads(raw)
        await self.store.set(FRAME_KEY, "")
        return await self._run_session(framed, unredacted=unredacted)

    async def _run_session(self, framed: dict, unredacted: bool = False, carry_forward: dict | None = None) -> dict:
        tier = framed["tier"]
        rotation = int(await self.store.get(ROTATION_KEY, "0") or "0")
        await self.store.set(ROTATION_KEY, str(rotation + 1))
        seats = build_seats(tier, self._settings_ns(), rotation)
        brief_text = await redact(self.claude, framed["context_summary"], skip_llm_pass=unredacted)
        brief_text = f"QUESTION: {framed['question']}\n\nCONTEXT:\n{brief_text}"

        result = await self._run_phases(tier, seats, brief_text, carry_forward)
        owners = await self._owner_candidates()
        report = await merge_report(
            self.claude, tier, result["transcript"], owners, result["consensus_weak"]
        )
        session_id = await self._archive(framed, tier, seats, result, report)
        return {
            "session_id": session_id, "tier": tier, "report": report,
            "cost_usd": result["total_cost"], "consensus_weak": result["consensus_weak"],
            "seats": result["seats"], "wall_clock_hit": result.get("wall_clock_hit", False),
            "budget_hit": result.get("budget_hit", False),
        }

    def _settings_ns(self):
        class _NS:
            pass

        ns = _NS()
        for attr in (
            "warroom_full_model_a", "warroom_full_model_b", "warroom_full_model_c",
            "warroom_quick_model_a", "warroom_quick_model_b", "warroom_quick_model_c",
            "warroom_full_effort_b",
        ):
            setattr(ns, attr, getattr(self._settings, attr))
        return ns

    async def _run_phases(self, tier: str, seats: list, brief_text: str, carry_forward: dict | None) -> dict:
        clients = {"openai": self._openai, "google": self._gemini}
        wl = {"full": {1: 400, 2: 400, 3: 400, 4: 200}, "quick": {1: 200, 2: 200}}[tier]
        budget = self.full_budget if tier == "full" else self.quick_budget
        wall_cap = FULL_WALLCLOCK_SEC if tier == "full" else QUICK_WALLCLOCK_SEC
        start = time.monotonic()
        total_cost = 0.0
        transcript: list[dict] = []
        wall_clock_hit = False
        budget_hit = False

        def _record(seat, res) -> dict:
            return {
                "key": seat.key, "role": seat.role, "vendor": seat.vendor, "model": seat.model,
                "parsed": res["parsed"], "text": res["text"], "refused": res["refused"],
                "error": res["error"], "fallback_from": res["fallback_from"],
            }

        # Phase 1 — blind, parallel. Carried forward from a Quick Board escalation
        # instead of re-run, so nothing is paid for twice (§2 mid-session escalation).
        if carry_forward and carry_forward.get("phase1"):
            phase1_records = carry_forward["phase1"]
            transcript.append({"phase": 1, "seats": phase1_records, "carried_forward": True})
        else:
            async def run1(seat):
                system = phase1_system(seat, brief_text)
                return await call_seat(clients, self.claude, seat, system, "Give your Phase 1 assessment now, JSON only.", wl[1])

            results1 = await asyncio.gather(*(run1(s) for s in seats))
            phase1_records = []
            for seat, res in zip(seats, results1):
                total_cost += res["cost_usd"]
                phase1_records.append(_record(seat, res))
            transcript.append({"phase": 1, "seats": phase1_records})

        if time.monotonic() - start > wall_cap or total_cost > budget:
            wall_clock_hit = time.monotonic() - start > wall_cap
            budget_hit = total_cost > budget
            return {"transcript": transcript, "consensus_weak": False, "total_cost": total_cost,
                    "seats": [_seat_summary(s) for s in seats],
                    "wall_clock_hit": wall_clock_hit, "budget_hit": budget_hit}

        # Phase 2 — cross-exam, mandatory dissent re-prompt
        async def run2(seat, own_key):
            others = [r for r in phase1_records if r["key"] != own_key]
            system = phase2_system(seat, others, quick=(tier == "quick"))
            prompt = "Give your Phase 2 cross-examination now, JSON only."
            res = await call_seat(clients, self.claude, seat, system, prompt, wl[2])
            if looks_like_agreement(res["parsed"]):
                res2 = await call_seat(
                    clients, self.claude, seat, system + DISSENT_PUSH,
                    "Try again — find real disagreement. JSON only.", wl[2],
                )
                res2["cost_usd"] = res2["cost_usd"] + res["cost_usd"]
                return res2
            return res

        results2 = await asyncio.gather(*(run2(s, s.key) for s in seats))
        phase2_records = []
        for seat, res in zip(seats, results2):
            total_cost += res["cost_usd"]
            phase2_records.append(_record(seat, res))
        transcript.append({"phase": 2, "seats": phase2_records})
        consensus_weak = all(looks_like_agreement(r["parsed"]) for r in phase2_records)

        if tier == "quick":
            return {"transcript": transcript, "consensus_weak": consensus_weak, "total_cost": total_cost,
                    "seats": [_seat_summary(s) for s in seats],
                    "wall_clock_hit": wall_clock_hit, "budget_hit": budget_hit}

        if time.monotonic() - start > wall_cap or total_cost > budget:
            return {"transcript": transcript, "consensus_weak": consensus_weak, "total_cost": total_cost,
                    "seats": [_seat_summary(s) for s in seats],
                    "wall_clock_hit": time.monotonic() - start > wall_cap, "budget_hit": total_cost > budget}

        # Phase 3 — rebuttal, full only, conditional on real disagreement (the
        # convergence cut-off: no new argument survived Phase 2 → skip straight to 4)
        if not consensus_weak:
            async def run3(seat):
                system = phase3_system(seat, phase2_records)
                return await call_seat(clients, self.claude, seat, system, "Give your rebuttal now, JSON only.", wl[3])

            results3 = await asyncio.gather(*(run3(s) for s in seats))
            phase3_records = []
            for seat, res in zip(seats, results3):
                total_cost += res["cost_usd"]
                phase3_records.append(_record(seat, res))
            transcript.append({"phase": 3, "seats": phase3_records, "skipped": False})
        else:
            transcript.append({"phase": 3, "seats": [], "skipped": True,
                                "reason": "positions converged after cross-examination"})

        if time.monotonic() - start > wall_cap or total_cost > budget:
            return {"transcript": transcript, "consensus_weak": consensus_weak, "total_cost": total_cost,
                    "seats": [_seat_summary(s) for s in seats],
                    "wall_clock_hit": time.monotonic() - start > wall_cap, "budget_hit": total_cost > budget}

        # Phase 4 — Jarvis calls time, full only
        async def run4(seat):
            system = phase4_system(seat)
            return await call_seat(clients, self.claude, seat, system, "Give your final conclusions now, JSON only.", wl[4])

        results4 = await asyncio.gather(*(run4(s) for s in seats))
        phase4_records = []
        for seat, res in zip(seats, results4):
            total_cost += res["cost_usd"]
            phase4_records.append(_record(seat, res))
        transcript.append({"phase": 4, "seats": phase4_records})

        return {"transcript": transcript, "consensus_weak": consensus_weak, "total_cost": total_cost,
                "seats": [_seat_summary(s) for s in seats],
                "wall_clock_hit": wall_clock_hit, "budget_hit": budget_hit}

    async def _owner_candidates(self) -> list[str]:
        names = ["Paul"]
        if self._layer_factory is None:
            return names
        try:
            layer = await self._layer_factory()
            bmap = layer.board("master") if layer is not None else None
            if bmap is not None:
                names = sorted({m.split()[0] for m in bmap.members} | {"Paul"})
        except Exception:
            logger.exception("War Room owner-candidate lookup failed — Paul only")
        return names

    # ------------------------------------------------------------ archive + memory (§8)

    async def _archive(self, framed: dict, tier: str, seats: list, result: dict, report: dict) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        session_id = await self.db.insert_returning_id(
            "INSERT INTO warroom_sessions (created_at, tier, session_type, company, question,"
            " status, report, transcript, seats, cost_usd, consensus_weak, verdict)"
            " VALUES (?, ?, ?, ?, ?, 'done', ?, ?, ?, ?, ?, ?)",
            (
                now, tier, framed.get("session_type", ""), framed.get("company", ""),
                framed["question"], json.dumps(report), json.dumps(result["transcript"]),
                json.dumps(result["seats"]), result["total_cost"],
                1 if result["consensus_weak"] else 0, str(report.get("verdict", ""))[:500],
            ),
        )
        for i, ap in enumerate(report.get("action_points", [])):
            await self.db.execute(
                "INSERT INTO warroom_action_points (session_id, idx, title, why,"
                " owner_suggestion, due) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, i, str(ap.get("title", ""))[:200], str(ap.get("why", "")),
                 str(ap.get("owner_suggestion", "")), str(ap.get("due", ""))),
            )
        await self._add_month_spend(result["total_cost"])
        if self.memory is not None and report.get("verdict"):
            try:
                await self.memory.add_chunk(
                    f"War Room ({tier}) on '{framed['question']}': {report['verdict']} — "
                    f"{report.get('recommendation', '')}",
                    room="companies", type_="STABLE", source="warroom",
                    tags=["warroom", tier, framed.get("company", "")],
                )
            except Exception:
                logger.exception("War Room memory writeback failed")
        return session_id

    async def _add_month_spend(self, cost: float) -> None:
        key = MONTHLY_KEY_PREFIX + datetime.now(timezone.utc).strftime("%Y-%m")
        current = float(await self.store.get(key, "0") or "0")
        await self.store.set(key, str(current + cost))

    async def monthly_spend(self) -> float:
        key = MONTHLY_KEY_PREFIX + datetime.now(timezone.utc).strftime("%Y-%m")
        return float(await self.store.get(key, "0") or "0")

    # ------------------------------------------------------------ §2 mid-session escalation

    async def escalate(self, quick_session_id: int) -> dict:
        row = await self.db.fetch_one(
            "SELECT * FROM warroom_sessions WHERE id = ?", (quick_session_id,)
        )
        if row is None:
            return {"error": "That session doesn't exist any more."}
        transcript = json.loads(row["transcript"])
        phase1 = next((p["seats"] for p in transcript if p["phase"] == 1), [])
        framed = {
            "question": row["question"], "tier": "full", "session_type": row["session_type"],
            "company": row["company"],
            "context_summary": "",   # already baked into the carried-forward Phase 1 answers
        }
        return await self._run_session(framed, carry_forward={"phase1": phase1})

    # ------------------------------------------------------------ §7 approve/reject/discuss

    async def approve_action(self, session_id: int, action_id: int, owner_override: str = "") -> str:
        row = await self.db.fetch_one(
            "SELECT * FROM warroom_action_points WHERE id = ? AND session_id = ?",
            (action_id, session_id),
        )
        if row is None:
            return "That action point doesn't exist."
        if row["status"] != "pending":
            return f"Already {row['status']}."
        if self._layer_factory is None:
            return "Trello isn't wired up — can't file this one."
        layer = await self._layer_factory()
        owner = owner_override or row["owner_suggestion"] or "Paul"
        session = await self.db.fetch_one("SELECT id FROM warroom_sessions WHERE id = ?", (session_id,))
        try:
            cards = await trello_plan.create_project(
                layer, session_id,
                [{"title": row["title"], "why": row["why"], "owner": owner, "due": row["due"],
                  "list_name": "Brain Dump"}],
            )
        except Exception:
            logger.exception("War Room approve_action failed")
            return "Couldn't file that just now — try again shortly."
        if not cards["created"]:
            return "; ".join(cards["warnings"]) or "Filing failed."
        card = cards["created"][0]
        await self.db.execute(
            "UPDATE warroom_action_points SET status = 'approved', card_id = ? WHERE id = ?",
            (card["card_id"], action_id),
        )
        await self.db.execute(
            "INSERT INTO warroom_watch (session_id, card_id, board_key, created_at) VALUES (?, ?, 'master', ?)",
            (session_id, card["card_id"], datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        warn = f" ({cards['warnings'][0]})" if cards["warnings"] else ""
        return f"Filed: {row['title']}{warn}"

    async def reject_action(self, session_id: int, action_id: int, reason: str = "") -> str:
        row = await self.db.fetch_one(
            "SELECT id, status FROM warroom_action_points WHERE id = ? AND session_id = ?",
            (action_id, session_id),
        )
        if row is None:
            return "That action point doesn't exist."
        if row["status"] != "pending":
            return f"Already {row['status']}."
        await self.db.execute(
            "UPDATE warroom_action_points SET status = 'rejected', reject_reason = ? WHERE id = ?",
            (reason, action_id),
        )
        return "Rejected — noted for next time."

    # ------------------------------------------------------------ §3 Quick Board project

    async def preview_project(self, session_id: int) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT * FROM warroom_action_points WHERE session_id = ? AND status = 'pending' ORDER BY idx",
            (session_id,),
        )
        if self._layer_factory is None:
            return []
        layer = await self._layer_factory()
        action_points = [
            {"title": r["title"], "why": r["why"], "owner_suggestion": r["owner_suggestion"], "due": r["due"]}
            for r in rows
        ]
        return await trello_plan.preview_cards(layer, action_points)

    async def create_project(self, session_id: int, edited_cards: list[dict] | None = None) -> dict:
        cards = edited_cards if edited_cards is not None else await self.preview_project(session_id)
        if not cards:
            return {"created": [], "warnings": ["Nothing to create."]}
        layer = await self._layer_factory()
        result = await trello_plan.create_project(layer, session_id, cards)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for card in result["created"]:
            await self.db.execute(
                "INSERT INTO warroom_watch (session_id, card_id, board_key, created_at) VALUES (?, ?, 'master', ?)",
                (session_id, card["card_id"], now),
            )
        await self.db.execute(
            "UPDATE warroom_action_points SET status = 'approved' WHERE session_id = ? AND status = 'pending'",
            (session_id,),
        )
        await trello_plan.register_undo(self.store, session_id, "master", result["created"])
        return result

    async def undo(self) -> str:
        if self._layer_factory is None:
            return "Nothing to undo."
        layer = await self._layer_factory()
        return await trello_plan.undo_last_project(layer, self.store)

    # ------------------------------------------------------------ §8 archive search

    async def search_archive(self, query: str) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT id, created_at, tier, company, question, verdict FROM warroom_sessions"
            " WHERE status = 'done' AND (question LIKE ? OR company LIKE ? OR verdict LIKE ?)"
            " ORDER BY id DESC LIMIT 10",
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        )
        return rows

    async def get_session(self, session_id: int) -> dict | None:
        row = await self.db.fetch_one("SELECT * FROM warroom_sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        row["report"] = json.loads(row["report"])
        row["transcript"] = json.loads(row["transcript"])
        row["seats"] = json.loads(row["seats"])
        return row


def _seat_summary(seat) -> dict:
    return {"key": seat.key, "vendor": seat.vendor, "model": seat.model, "role": seat.role}
