"""Brazil Portuguese course (7 Aug brief, ⏳ 28 Aug): voice-led daily lessons.

Two tracks, in the brief's priority order:
1. THE SPEECH — five lines to Seu Ednei, drilled line by line, building to
   the whole piece. Muscle memory by 28 Aug.
2. TABLE SURVIVAL — greetings, politeness, the golden "I don't speak
   Portuguese well yet" line. A few new per day.

The coach is deliberately mechanical and testable: it decides WHAT to drill
(spaced repetition, weak-first), scores an attempt against the target
deterministically (STT transcript vs the line), and keeps the mastery
ledger in pt_phrases. The warm human coaching line on top comes from the
brain model in the router lane — never from here.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from app.core.store import SettingsStore
from app.db.base import Database

TRIP_DATE = date(2026, 8, 28)
LESSON_KEY = "pt_lesson"          # active-session state (settings store JSON)
PASS_MARK = 70                    # score ≥ this = a pass for the ledger
MASTERY_PASSES = 3                # passes at which a phrase counts mastered
MAX_TRIES_PER_PHRASE = 3          # then move on — gentle, never a grind
NEW_PER_DAY = 3                   # survival phrases introduced per lesson

# The speech — address the father as "Seu Ednei". Verbatim from the brief.
EDNEI_SPEECH: list[tuple[str, str]] = [
    ("Seu Ednei, eu queria conversar com o senhor a sós, de coração.",
     "Seu Ednei, I'd like to speak with you alone, from the heart."),
    ("Eu amo muito a sua filha.",
     "I love your daughter very much."),
    ("Um dia, eu gostaria de pedir a sua bênção para me casar com ela.",
     "One day, I'd like to ask your blessing to marry her."),
    ("Mas, antes disso, eu queria que o senhor tivesse a chance de me conhecer melhor.",
     "But before that, I'd like you to have the chance to get to know me better."),
    ("E quero que o senhor saiba: aconteça o que acontecer, eu vou sempre cuidar dela e protegê-la.",
     "And I want you to know: whatever happens, I will always look after her and protect her."),
]

SURVIVAL_SET: list[tuple[str, str]] = [
    ("Oi, tudo bem?", "Hi, how's it going?"),
    ("Olá, muito prazer.", "Hello, lovely to meet you."),
    ("Obrigado.", "Thank you (from a man: obrigadO)."),
    ("Com licença.", "Excuse me."),
    ("Desculpa.", "Sorry."),
    ("Tchau, até logo!", "Bye, see you soon!"),
    ("Desculpa, eu ainda não falo bem português.",
     "Sorry, I don't speak Portuguese well yet — the golden line."),
    ("Que delícia!", "How delicious! — table gold."),
    ("Pode repetir, por favor?", "Could you say that again, please?"),
    ("Saúde!", "Cheers!"),
]


def _normalise(text: str) -> str:
    """Compare what was said, not how it was spelt: lowercase, accents
    stripped (STT is inconsistent with them), punctuation dropped."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text).strip()


def score_attempt(target: str, heard: str) -> int:
    """0–100: how close the STT transcript of Paul's attempt is to the
    target line. Deterministic on purpose — the ledger must not wobble
    with model mood. Word-level and character-level similarity blended so
    one fluffed word doesn't sink a long line."""
    t_norm, h_norm = _normalise(target), _normalise(heard)
    if not h_norm:
        return 0
    char_sim = SequenceMatcher(None, t_norm, h_norm).ratio()
    t_words, h_words = t_norm.split(), h_norm.split()
    hits = sum(1 for w in t_words if w in h_words)
    word_sim = hits / max(len(t_words), 1)
    return round(100 * (0.5 * char_sim + 0.5 * word_sim))


class PortugueseCoach:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.store = SettingsStore(db)

    # ------------------------------------------------------------- seeding
    async def ensure_seeded(self) -> None:
        """Idempotent: load the speech + survival set once; never duplicate,
        never overwrite drill history."""
        for position, (text, gloss) in enumerate(EDNEI_SPEECH, start=1):
            await self._seed_row(text, gloss, "speech", position)
        for position, (text, gloss) in enumerate(SURVIVAL_SET, start=1):
            await self._seed_row(text, gloss, "survival", position)

    async def _seed_row(self, text: str, gloss: str, kind: str, position: int) -> None:
        row = await self.db.fetch_one("SELECT id FROM pt_phrases WHERE text = ?", (text,))
        if row is None:
            await self.db.execute(
                "INSERT INTO pt_phrases (text, gloss, kind, position) VALUES (?, ?, ?, ?)",
                (text, gloss, kind, position),
            )

    # ------------------------------------------------------- lesson session
    async def active(self) -> dict[str, Any] | None:
        raw = await self.store.get(LESSON_KEY)
        if not raw:
            return None
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return state if state.get("queue") is not None else None

    async def start_lesson(self, today: date) -> dict[str, Any]:
        """Build today's drill queue: the speech first — every line up to and
        including the frontier (the first line never yet passed), so one
        clean pass unlocks the next line while daily rehearsal of the
        earlier ones builds mastery — then weak survival phrases, then up
        to NEW_PER_DAY fresh ones. Returns the opening turn."""
        await self.ensure_seeded()
        speech = await self.db.fetch_all(
            "SELECT * FROM pt_phrases WHERE kind = 'speech' ORDER BY position"
        )
        queue: list[int] = []
        frontier_hit = False
        for row in speech:
            if frontier_hit:
                break
            queue.append(row["id"])
            if row["passes"] < 1:
                frontier_hit = True   # include the frontier line, stop there
        # Weak survival phrases first (lowest last score, oldest seen)…
        weak = await self.db.fetch_all(
            "SELECT id FROM pt_phrases WHERE kind = 'survival' AND introduced = 1"
            " AND passes < ? ORDER BY COALESCE(last_score, 0), COALESCE(last_seen, ''), position"
            " LIMIT 3",
            (MASTERY_PASSES,),
        )
        queue.extend(r["id"] for r in weak)
        # …then a few brand-new ones.
        fresh = await self.db.fetch_all(
            "SELECT id FROM pt_phrases WHERE kind = 'survival' AND introduced = 0"
            " ORDER BY position LIMIT ?",
            (NEW_PER_DAY,),
        )
        queue.extend(r["id"] for r in fresh)
        state = {
            "queue": queue, "index": 0, "tries": 0, "attempts": 0,
            "day": today.isoformat(), "scores": [],
        }
        await self.store.set(LESSON_KEY, json.dumps(state))
        return await self._turn_for(state)

    async def _turn_for(self, state: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT * FROM pt_phrases WHERE id = ?", (state["queue"][state["index"]],)
        )
        total = len(state["queue"])
        return {
            "done": False,
            "line": row["text"],
            "gloss": row["gloss"],
            "kind": row["kind"],
            "number": state["index"] + 1,
            "total": total,
            "is_new": not row["introduced"],
        }

    async def attempt(self, today: date, heard: str) -> dict[str, Any]:
        """Paul spoke: score it, update the ledger, decide what's next.
        Returns {score, target, gloss, heard, next: turn|None, done, passed}."""
        state = await self.active()
        if state is None:
            return {"error": "no active lesson"}
        phrase_id = state["queue"][state["index"]]
        row = await self.db.fetch_one("SELECT * FROM pt_phrases WHERE id = ?", (phrase_id,))
        score = score_attempt(row["text"], heard)
        passed = score >= PASS_MARK
        await self.db.execute(
            "UPDATE pt_phrases SET introduced = 1, attempts = attempts + 1,"
            " passes = passes + ?, last_score = ?, last_seen = ? WHERE id = ?",
            (1 if passed else 0, score, today.isoformat(), phrase_id),
        )
        state["attempts"] += 1
        state["tries"] += 1
        state["scores"].append(score)
        move_on = passed or state["tries"] >= MAX_TRIES_PER_PHRASE
        result: dict[str, Any] = {
            "score": score, "passed": passed, "target": row["text"],
            "gloss": row["gloss"], "heard": heard, "done": False, "next": None,
            "retry": not move_on,
        }
        if move_on:
            state["index"] += 1
            state["tries"] = 0
        if state["index"] >= len(state["queue"]):
            result["done"] = True
            result["summary"] = await self._finish(state, today)
        else:
            await self.store.set(LESSON_KEY, json.dumps(state))
            if move_on:
                result["next"] = await self._turn_for(state)
        return result

    async def end_lesson(self, today: date) -> dict[str, Any] | None:
        """'That's enough' — end early with grace. Enough work done still
        counts the day (the streak is about showing up, not perfection)."""
        state = await self.active()
        if state is None:
            return None
        return await self._finish(state, today)

    async def _finish(self, state: dict[str, Any], today: date) -> dict[str, Any]:
        await self.store.set(LESSON_KEY, "")   # store has no delete; empty = over
        scores = state.get("scores") or []
        counts = bool(state.get("attempts", 0) >= 1)
        return {
            "attempts": state.get("attempts", 0),
            "avg_score": round(sum(scores) / len(scores)) if scores else 0,
            "counts_for_streak": counts,
            "readiness": await self.readiness(today),
            "steph_phrase": await self.steph_phrase(today),
        }

    # --------------------------------------------------- readiness + bridge
    async def readiness(self, today: date) -> dict[str, Any]:
        """The gauge: speech mastery is the headline (the brief's priority),
        survival the second hand. Mastery per phrase = min(passes,3)/3."""
        await self.ensure_seeded()
        rows = await self.db.fetch_all("SELECT kind, passes FROM pt_phrases")
        speech = [min(r["passes"], MASTERY_PASSES) / MASTERY_PASSES
                  for r in rows if r["kind"] == "speech"]
        survival = [min(r["passes"], MASTERY_PASSES) / MASTERY_PASSES
                    for r in rows if r["kind"] == "survival"]
        speech_pct = round(100 * sum(speech) / len(speech)) if speech else 0
        survival_pct = round(100 * sum(survival) / len(survival)) if survival else 0
        return {
            "days_left": max((TRIP_DATE - today).days, 0),
            "speech_pct": speech_pct,
            "survival_pct": survival_pct,
            "overall_pct": round(0.7 * speech_pct + 0.3 * survival_pct),
        }

    async def steph_phrase(self, today: date) -> dict[str, str]:
        """The Steph bridge: one phrase to try on her today. Rotates through
        what Paul has actually been taught; before anything is introduced it
        offers the simplest opener."""
        rows = await self.db.fetch_all(
            "SELECT text, gloss FROM pt_phrases WHERE kind = 'survival' AND introduced = 1"
            " ORDER BY position"
        )
        if not rows:
            text, gloss = SURVIVAL_SET[0]
            return {"text": text, "gloss": gloss}
        row = rows[today.toordinal() % len(rows)]
        return {"text": row["text"], "gloss": row["gloss"]}
