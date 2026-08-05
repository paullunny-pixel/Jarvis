"""Phase 3 — Voice Intake (Paul's spec, 5 Aug): three minutes of talking
becomes correctly-populated cards on the right boards.

The priority is ACCURACY over speed and VISIBLE UNCERTAINTY over confident
guessing — a card silently filed wrong is worse than a blank field, because
Paul has no signal to check it. So: one extraction call with full context
(aliases + rulings, per-board options, people, the open-card index for
dedup, today's date in Dubai), a numbered confirm-before-write summary,
corrections in natural language until Paul says OK, all-or-nothing writes
with exact partial-failure reporting, and a per-field accuracy log that
earns confirmation-relaxation field by field (§9).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PENDING_KEY = "intake_pending"       # the batch awaiting Paul's OK
ACCURACY_KEY = "intake_accuracy"     # rolling per-field correction log (last 50)

APPROVE = re.compile(r"^\s*(ok(ay)?|yes|yep|write( them)?|go( ahead)?|approved?|do it)\s*[.!]?\s*$", re.I)
DISCARD = re.compile(r"^\s*(cancel|drop (it|them|the lot|all)|scrap (it|them)|forget it)\s*[.!]?\s*$", re.I)

EXTRACTION_SYSTEM = """You extract Trello card intents from Paul's spoken ramble.
Reply ONLY with JSON matching:
{"cards":[{"action":"create|update|archive","matchedCardId":null,"matchConfidence":0.0,
"title":"","description":null,"board":"master|harry","list":"","domain":null,
"priority":null,"owner":null,"due":null,
"checklist":[{"text":"","owner":null,"due":null}],
"confidence":0.0,"uncertainties":[]}],"unassignedRemarks":[]}

RULES (each matters):
- SEGMENT on topic change, not pauses. One ramble is usually several cards.
- LAST STATEMENT WINS: 'P3 — no actually P2' means P2. 'cancel that' drops
  the preceding intent entirely.
- SEQUENCE LANGUAGE IS A CHECKLIST: 'first X, then Y, then Z' is ONE card
  with three checklist items, never three cards.
- NEVER INVENT: no stated deadline means due=null. Do not infer from tone.
- FLAG, DON'T GUESS: an unconfident field goes null with a specific question
  in uncertainties.
- Board routing: Harry's task -> harry. Kiefer's -> master. Personal or
  Aesthetics Supply domain -> ALWAYS master (not options on harry's board).
  Otherwise Paul's tasks -> master. Genuinely ambiguous -> uncertainty.
- Dedup: if the speech matches a card in the OPEN CARD INDEX, action=update
  with matchedCardId and matchConfidence; describe only the CHANGES in the
  fields. When unsure whether it's that card, keep action=create with the
  matchedCardId and a LOW matchConfidence so the system asks.
- Cards on '(Archive)' lists are FAIR GAME to update, move or archive — the
  cleanup operation is exactly about pulling them back onto live lists.
  Route their moves to a LIVE list (Inbox if unspecified); never INTO an
  archive list.
- DELETE LANGUAGE: 'delete/archive/get rid of/bin/don't need X any more'
  -> action=archive with matchedCardId from the index (title in "title").
  No confident match -> keep action=archive, matchedCardId=null, and put
  'which card?' in uncertainties. Archiving is safe — never refuse it.
- People not on the boards (Sarah, Steph, Marko...) go in the description,
  never in owner.
- Relative dates resolve against Paul's timezone shown below, output UTC ISO.
- Speech that maps to no card goes in unassignedRemarks, never dropped.
- CONVERSATION IS NOT CAPTURE: if the message reads as an answer to a
  question, a mood/state report, reflection, or general chat — even when it
  mentions work words — return ZERO cards. Only concrete work instructions
  become cards.
- NEVER invent: no card may contain a person, task or detail that is not in
  Paul's actual words. When in doubt, unassignedRemarks.
- Nothing actionable at all -> {"cards":[],"unassignedRemarks":[...]}."""


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def resolve_due(raw: str, now: datetime) -> datetime | None:
    """ISO first; then the relative words humans (and models) actually use —
    'tomorrow' in a due field must become a date, never a crash (00:16,
    6 Aug: a card failed partway on Invalid isoformat 'tomorrow')."""
    raw = str(raw or "").strip().lower()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("z", "+00:00"))
    except ValueError:
        pass
    from datetime import timedelta

    if raw in ("today", "tonight", "end of day", "eod"):
        return now.replace(hour=18, minute=0, second=0, microsecond=0)
    if raw in ("tomorrow", "tomorow", "tommorow", "tommorrow"):
        return (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    for i, day in enumerate(WEEKDAYS):
        if day in raw:
            ahead = (i - now.weekday()) % 7 or 7
            return (now + timedelta(days=ahead)).replace(hour=18, minute=0, second=0, microsecond=0)
    if raw in ("next week",):
        return (now + timedelta(days=7)).replace(hour=18, minute=0, second=0, microsecond=0)
    return None


def title_similarity(a: str, b: str) -> float:
    """Token-set overlap — the programmatic half of two-stage dedup (§6)."""
    ta = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 2}
    tb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


class VoiceIntake:
    def __init__(self, claude, store, settings, layer_factory) -> None:
        self.claude = claude
        self.store = store
        self.settings = settings
        self._layer_factory = layer_factory   # async () -> TrelloLayer | None

    async def _layer(self):
        try:
            return await self._layer_factory()
        except Exception:
            logger.exception("Intake: Trello layer unavailable — degrading honestly")
            return None

    # ------------------------------------------------------------ context (§4)

    async def _card_index(self, layer) -> list[dict]:
        if layer is None:
            return []
        index = []
        for key in ("master", "harry"):
            try:
                bmap = layer.board(key)
                all_lists = {**bmap.lists, **getattr(bmap, "archive_lists", {})}
                for card in await layer.client.board_cards(bmap.id):
                    list_name = next(
                        (n for n, i in all_lists.items() if i == card.get("idList")), "?"
                    )
                    index.append({"id": card["id"], "title": card["name"],
                                  "list": list_name, "board": key})
            except Exception:
                logger.exception("Intake index failed for %s", key)
        return index

    async def extract(self, transcript: str) -> dict:
        """One LLM call, full context in, structured intents out."""
        from app.daily12.trello_layer import DOMAIN_ALIASES, KNOWN_DOMAINS, UNRESOLVED_ALIASES

        try:
            rulings = json.loads(await self.store.get("trello_domain_rulings", "{}"))
        except Exception:
            rulings = {}
        layer = await self._layer()
        index = await self._card_index(layer)
        now = datetime.now(ZoneInfo("Asia/Dubai"))
        context = (
            f"TODAY: {now.strftime('%A %Y-%m-%d %H:%M')} Asia/Dubai.\n"
            f"DOMAINS: {', '.join(KNOWN_DOMAINS)} (Personal and Aesthetics Supply "
            "exist ONLY on master).\n"
            f"ALIASES: {json.dumps(dict(DOMAIN_ALIASES))}\n"
            f"PAUL'S RULINGS (these win): {json.dumps(rulings)}\n"
            f"UNRULED (never guess, ask): {', '.join(a for a in UNRESOLVED_ALIASES if a not in rulings)}\n"
            "PRIORITY ANCHORS: P1 drop-everything/blocking; P2 must happen today; "
            "P3 this week; P4 this month; P5 backlog.\n"
            "PEOPLE ON BOARDS: Paul (both), Harry (harry board), Kiefer (master).\n"
            f"OPEN CARD INDEX ({len(index)} cards): {json.dumps(index)[:8000]}"
        )
        raw = await self.claude.converse(
            EXTRACTION_SYSTEM + "\n\n" + context,
            [{"role": "user", "content": transcript}],
            max_tokens=2000, timeout=120.0,
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        batch = json.loads(match.group(0)) if match else {"cards": [], "unassignedRemarks": []}
        # §6 stage two: programmatic verification of every proposed match.
        titles = {c["id"]: c["title"] for c in index}
        for card in batch.get("cards", []):
            matched = card.get("matchedCardId")
            if matched and matched in titles:
                ratio = title_similarity(card.get("title", ""), titles[matched])
                agree = ratio >= 0.6
                confident = float(card.get("matchConfidence") or 0) >= 0.75
                if card.get("action") == "update" and not (agree and confident):
                    card["action"] = "create"
                    card.setdefault("uncertainties", []).append(
                        f"Same as existing '{titles[matched]}', or new?"
                    )
        return batch

    # ------------------------------------------------------------ summary (§7)

    def compose_summary(self, batch: dict) -> str:
        cards = batch.get("cards", [])
        if not cards:
            return ""
        lines = [f"Got {len(cards)} thing{'s' if len(cards) != 1 else ''} from that:", ""]
        for n, card in enumerate(cards, 1):
            kind = {"update": "UPDATE", "archive": "ARCHIVE"}.get(
                card.get("action") or "", "NEW"
            )
            lines.append(f"{n}. {kind} · {card.get('title', '(untitled)')}")
            if kind == "ARCHIVE":
                for question in card.get("uncertainties", []) or []:
                    lines.append(f"   ⚠ {question}")
                continue
            bits = [b for b in (
                card.get("domain"), card.get("priority"), card.get("owner"),
                f"due {card['due'][:10]}" if card.get("due") else "no deadline",
            ) if b]
            lines.append("   " + " · ".join(bits))
            if card.get("checklist"):
                lines.append("   " + "  ".join(f"☐ {i.get('text', '')}" for i in card["checklist"][:6]))
            board = "Paul x Harry" if card.get("board") == "harry" else "Master Board"
            lines.append(f"   → {board} / {card.get('list') or 'Inbox'}")
            for question in card.get("uncertainties", []) or []:
                lines.append(f"   ⚠ {question}")
        for remark in batch.get("unassignedRemarks", []) or []:
            lines.append(f"   (couldn't place: “{remark}”)")
        lines += ["", "Reply OK to write these, or tell me what to change."]
        return "\n".join(lines)

    # ------------------------------------------------------------ the loop

    async def pending(self) -> dict | None:
        try:
            data = json.loads(await self.store.get(PENDING_KEY, "") or "null")
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def start_batch(self, transcript: str) -> str | None:
        """Extract + stash + return the summary; None means nothing actionable
        (the caller lets the conversation flow on)."""
        batch = await self.extract(transcript)
        if not batch.get("cards"):
            return None
        now = datetime.now(ZoneInfo("Asia/Dubai"))
        await self.store.set(PENDING_KEY, json.dumps({
            "batch": batch, "transcript": transcript[:4000],
            "created": now.isoformat(timespec="seconds"), "reminded": False,
            "original": batch,   # what Jarvis proposed — the §9 baseline
        }))
        return self.compose_summary(batch)

    async def handle_reply(self, text: str) -> str | None:
        """Paul's next message while a batch is pending: OK writes, cancel
        drops, anything else is a correction — re-render, ask again. Returns
        None if no batch is pending (caller continues as normal)."""
        pend = await self.pending()
        if pend is None:
            return None
        if DISCARD.match(text):
            await self.store.set(PENDING_KEY, "")
            return "Scrapped — nothing written."
        if APPROVE.match(text):
            result = await self.write_batch(pend)
            await self.store.set(PENDING_KEY, "")
            await self._log_accuracy(pend)
            return result
        # A long new work-ramble means new content, not a correction (§2).
        if len(text) > 400:
            return None
        raw = await self.claude.converse(
            "You are correcting a pending Trello card batch. Apply Paul's "
            "correction to the JSON and reply ONLY with the corrected JSON, "
            "same schema, all other fields unchanged. 'drop N' removes card N.",
            [{"role": "user", "content": json.dumps(pend["batch"]) + "\n\nCORRECTION: " + text}],
            max_tokens=2000, timeout=120.0,
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return "Didn't catch that as a correction — say it against the numbers, like '2 should be P1'."
        try:
            pend["batch"] = json.loads(match.group(0))
        except Exception:
            return "That correction didn't parse cleanly — go again?"
        pend["corrected"] = True
        await self.store.set(PENDING_KEY, json.dumps(pend))
        return self.compose_summary(pend["batch"])

    async def remind_or_park(self, now: datetime | None = None) -> None:
        """§7 timeout: unanswered a few hours → remind once; another few →
        park it (kept, announced, retrievable) — never silently dropped."""
        pend = await self.pending()
        if pend is None:
            return
        now = now or datetime.now(ZoneInfo("Asia/Dubai"))
        age_h = (now - datetime.fromisoformat(pend["created"])).total_seconds() / 3600
        if age_h >= 6:
            await self.store.set(PENDING_KEY, "")
            await self.store.set("intake_parked", json.dumps(pend))
            raise IntakeParked(
                f"Parked the {len(pend['batch'].get('cards', []))} unconfirmed cards from "
                "earlier — say 'bring back the parked cards' when you want them."
            )
        if age_h >= 3 and not pend.get("reminded"):
            pend["reminded"] = True
            await self.store.set(PENDING_KEY, json.dumps(pend))
            raise IntakeReminder(
                "Still holding those cards from earlier, sir — OK writes them, "
                "'cancel' scraps them."
            )

    async def unpark(self) -> str | None:
        try:
            parked = json.loads(await self.store.get("intake_parked", "") or "null")
        except Exception:
            parked = None
        if not isinstance(parked, dict):
            return None
        parked["created"] = datetime.now(ZoneInfo("Asia/Dubai")).isoformat(timespec="seconds")
        parked["reminded"] = False
        await self.store.set(PENDING_KEY, json.dumps(parked))
        await self.store.set("intake_parked", "")
        return self.compose_summary(parked["batch"])

    # ------------------------------------------------------------ write (§8)

    async def write_batch(self, pend: dict) -> str:
        layer = await self._layer()
        if layer is None:
            return (
                "WRITE FAILED — Trello isn't reachable (keys or network). The batch "
                "is still held; say OK again once Trello's back."
            )
        results = []
        now_dubai = datetime.now(ZoneInfo("Asia/Dubai"))
        for n, card in enumerate(pend["batch"].get("cards", []), 1):
            title = card.get("title", "(untitled)")
            try:
                due = None
                due_note = ""
                if card.get("due"):
                    due = resolve_due(card["due"], now_dubai)
                    if due is None:
                        # A bad date never kills the card — write without it, say so.
                        due_note = (
                            f" (couldn't make a date of '{card['due']}' — created "
                            "WITHOUT a deadline; tell me the date and I'll set it)"
                        )
                if card.get("action") == "archive":
                    if not card.get("matchedCardId"):
                        results.append(
                            f"{n}. ⚠️ couldn't archive '{title}' — no matching card found; "
                            "tell me its exact title."
                        )
                        continue
                    await layer.client.archive_card(card["matchedCardId"])
                    results.append(f"{n}. ✅ archived '{title}'")
                elif card.get("action") == "update" and card.get("matchedCardId"):
                    steps = []
                    if card.get("domain"):
                        await layer.set_domain(card["board"], card["matchedCardId"], card["domain"])
                        steps.append("domain")
                    if card.get("priority"):
                        await layer.set_priority(card["board"], card["matchedCardId"], card["priority"])
                        steps.append("priority")
                    if due:
                        await layer.client.update_card(
                            card["matchedCardId"], due=layer._due_utc(due)
                        )
                        steps.append("due")
                    if card.get("list"):
                        await layer.move_card(card["board"], card["matchedCardId"], card["list"])
                        steps.append("list")
                    results.append(f"{n}. ✅ updated '{title}' ({', '.join(steps) or 'no changes'})")
                else:
                    checklist = [
                        {"name": i.get("text", ""),
                         "member": i.get("owner") or "",
                         "due_local": resolve_due(i.get("due"), now_dubai)}
                        for i in (card.get("checklist") or []) if i.get("text")
                    ]
                    owner = card.get("owner") or ""
                    owner_full = {"Kiefer": "Kiefer Brindle", "Paul": "Paul Lunny"}.get(owner, owner)
                    await layer.create_card_full(
                        card.get("board") or "master",
                        card.get("list") or "Inbox",
                        title,
                        desc=card.get("description") or "",
                        due_local=due,
                        owner_name=owner_full,
                        domain=card.get("domain") or "",
                        priority=card.get("priority") or "",
                        checklist=checklist or None,
                    )
                    results.append(f"{n}. ✅ created '{title}'{due_note}")
            except Exception as exc:
                logger.exception("Intake write failed for '%s'", title)
                results.append(f"{n}. ⚠️ '{title}' FAILED PARTWAY: {str(exc)[:150]}")
        return "Written:\n" + "\n".join(results)

    # ------------------------------------------------------------ §9 accuracy

    async def _log_accuracy(self, pend: dict) -> None:
        try:
            log = json.loads(await self.store.get(ACCURACY_KEY, "[]"))
        except Exception:
            log = []
        corrected = bool(pend.get("corrected"))
        original = {c.get("title"): c for c in (pend.get("original") or {}).get("cards", [])}
        for card in pend["batch"].get("cards", []):
            base = original.get(card.get("title")) or {}
            entry = {"corrected_fields": [
                f for f in ("domain", "priority", "owner", "due", "list", "title", "board")
                if corrected and base and base.get(f) != card.get(f)
            ]}
            log.append(entry)
        await self.store.set(ACCURACY_KEY, json.dumps(log[-50:]))

    async def accuracy_report(self) -> str:
        try:
            log = json.loads(await self.store.get(ACCURACY_KEY, "[]"))
        except Exception:
            log = []
        if not log:
            return "No intake history yet — accuracy tracking starts with the first written batch."
        total = len(log)
        lines = [f"Intake accuracy (last {total} cards):"]
        for field in ("domain", "priority", "owner", "due", "list", "title"):
            wrong = sum(1 for e in log if field in e.get("corrected_fields", []))
            pct = round(100 * (total - wrong) / total)
            grad = "  ← ready to drop confirmation" if pct >= 95 and total >= 50 else ""
            lines.append(f"  {field:<9}{pct}%  ({wrong} corrections){grad}")
        return "\n".join(lines)


class IntakeReminder(RuntimeError):
    """Raised by remind_or_park with the reminder text (caller sends it)."""


class IntakeParked(RuntimeError):
    """Raised by remind_or_park with the parked notice (caller sends it)."""
