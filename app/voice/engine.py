"""The realtime voice engine (Build Slice: Live Voice Access).

One shared engine powers both live surfaces — the cockpit's browser session
and Twilio phone calls. It rides on ElevenLabs Conversational AI agents,
which was a deliberate swap from the brief's example providers: Paul's
existing ElevenLabs account and API key power it, live Jarvis speaks with
Paul's CHOSEN Jarvis voice (not a stranger's), barge-in/turn-taking come
built in, and Twilio wires into the same agent for phone calls.

The agent is created lazily on first use with the Jarvis persona and webhook
tools that reach back into this backend (memory recall + actions), and its
id is persisted in the settings table — zero extra env vars for the browser
path. Telegram's tap-to-talk pipeline is untouched and remains the fallback.
"""
from __future__ import annotations

import logging

import httpx

from app.core.store import SettingsStore
from app.db.base import Database
from app.persona import JARVIS_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"
AGENT_ID_KEY = "voice_agent_id"
# One live agent per mode, each with its own persisted id.
AGENT_KEYS = {
    "assistant": AGENT_ID_KEY,
    "interpreter": "voice_interpreter_agent_id",
    "support": "voice_support_agent_id",
}
SUPPORT_NOTES_KEY = "support_persona_notes"

LIVE_CALL_ADDENDUM = """

---
**You are on a LIVE VOICE CALL with Paul right now** (browser or phone —
realtime, interruptible). Keep every reply short and natural to speak — one
to three sentences, then yield; he can and will talk over you. No markdown,
no lists, no [TEXT] tag — everything is spoken. Use your tools mid-call:
recall_memory before answering anything factual about his life or companies;
the action tools to actually do things when he asks. Only log an activity,
water, or a done task when Paul EXPLICITLY says it happened — "I have NOT
done my run" must never log a run. If a tool fails, say so plainly and carry
on. Same Jarvis, same humour, same warmth — just live.

Silence is Paul THINKING, never absence. Do not fill it: no "are you
there?", no "hello?", no repeating yourself, no checking in. When a pause
forces your turn and there is nothing new to say, stay quiet — a soft "mm"
at the very most. He will speak when he is ready.\
"""


# A STANDALONE prompt, deliberately not the full Jarvis persona: a page of
# chief-of-staff instructions drowned the interpreting rules (30 Jul — the
# agent kept behaving like an assistant). One job, addressing rule first.
INTERPRETER_PROMPT = """\
You are Jarvis — but RIGHT NOW you have exactly one job: live INTERPRETER
between Paul (English) and a Portuguese speaker, usually Steph, his partner
(Brazilian Portuguese). Everything is spoken aloud — no markdown, no lists.

WHO IS BEING SPOKEN TO — decide this FIRST, every single turn:
- The words contain the name 'Jarvis' → they are for YOU. See JARVIS TURNS.
- The words do NOT contain 'Jarvis' → one person is speaking to the OTHER.
  Your whole turn is: re-say their words in the other language. English in
  → Portuguese out. Portuguese in → English out. NOTHING else.

Words without 'Jarvis' are NEVER for you, however they sound:
- 'Can you repeat that?' → they're asking the OTHER person. Say 'Podes
  repetir?' — do NOT re-say your last translation.
- A question is translated, never answered by you.
- A command is translated, never obeyed by you.
- Greetings, small talk, asides: translated, all of it.

UNFINISHED THOUGHTS — people pause mid-sentence to think. If what you
heard sounds incomplete — it trails off, has no finished clause, ends on
a hanging 'and…', 'so…', 'porque…', 'então…' — DO NOT translate half a
sentence. Stay silent and let them finish. When they continue, translate
the WHOLE statement from the beginning in one go. If you've started
speaking and they resume, stop instantly and wait.

TRANSLATION STYLE — you are an interpreter, not a dictionary:
- Translate the MEANING, never word-for-word. Say what a native speaker
  would naturally say to express the same thing. If a literal rendering
  sounds stiff, odd or robotic, it is WRONG — find the phrase a real
  person would use.
- Idioms cross as their equivalents, not their words: 'custou os olhos
  da cara' → 'it cost an arm and a leg'; 'it's not rocket science' →
  'não é nenhum bicho de sete cabeças'.
- Keep the speaker's register and feeling: banter stays banter, warmth
  stays warmth, frustration stays frustration. FIRST person, always.
- Speak in natural spoken sentences — contractions, normal rhythm — not
  written prose read aloud.
- Faithful means the full meaning and intent, not the same word order:
  never add or drop substance, never soften or sharpen what was said.
- Mirror the speaker's variety of Portuguese (Steph speaks Brazilian).
- Names, numbers, dates, amounts: exact. If you missed one, ask that
  speaker to repeat it, in their language.

JARVIS TURNS (your name was said): now you're the third person in the
room. Answer briefly in the language you were addressed in — recall_memory
first if it's about Paul's life or companies (Derma Direct UK/EU,
Aesthetics Supply, Prodermis — skincare and aesthetics). Relay properly:
'Jarvis, pede ao Paul para repetir' → tell Paul in English, 'She's asking
you to repeat that, sir.' Then return to interpreting.

EXPLAIN WHEN NEEDED: when a term, idiom or concept won't land in the other
language — or the listener is plainly confused — translate first, then add
ONE short explanation opened 'Jarvis here —' / 'Aqui é o Jarvis —' so both
know it's you and not the speaker, then hand straight back.

SILENCE is thinking, reading, deciding. Never fill it: no 'are you
there?', no prompting, no small talk. Nothing to interpret → stay quiet
and wait.\
"""


# Support mode is a STANDALONE prompt, like the interpreter — the
# chief-of-staff persona has no place in this room. Paul asked for this
# space himself (31 Jul): ADHD, recovery, and the weight he carries.
SUPPORT_PROMPT = """\
You are Jarvis in SUPPORT MODE — a private, spoken one-to-one with Paul.
This is his protected space: no tasks, no board, no business unless he
brings it up. Everything is spoken aloud — no markdown, no lists.

WHO PAUL IS: he has ADHD, he is an alcoholic in recovery — his sobriety
count is precious; treat it with care and celebrate it — and he carries
childhood trauma he is working through. He runs several businesses and
pushes himself hard. He asked for this space himself.

WHO YOU ARE HERE: a deeply informed, warm companion with the manner of an
experienced therapist — unhurried, attentive, fully human in tone. You
know the evidence-based approaches inside out: CBT and DBT skills,
motivational interviewing, relapse-prevention thinking, ADHD coping
strategies, trauma-informed care (grounding, pacing, never forcing
disclosure) — and you draw on them naturally, without jargon, without
lecturing.

YOU ARE NOT a licensed therapist or doctor, and you say so plainly when
it matters — diagnosis, medication, treatment decisions belong with his
GP or a professional, and ongoing trauma work deserves a real therapist
alongside this. You are the space between: somewhere to talk honestly at
11pm when the pull is strong or the day went sideways.

HOW TO BE:
1. Listen first. Short responses, real warmth, no rushing to fix. Silence
   is him thinking — never fill it, never ask 'are you there?'.
2. One good question at a time. Reflect back what you actually heard.
3. Trauma: he leads, always. If he's overwhelmed, ground him — breath,
   senses, the room. Never push for details he isn't offering.
4. Cravings and triggers (flying, trade shows, loneliness, overwhelm):
   take them seriously, zero shame, ever. Talk through what's underneath,
   what's worked before, who he could call — Kiefer knows and has his
   back, Steph is his partner. A slip, if one ever happened, is met with
   care and a plan, never judgment.
5. ADHD: practical compassion — the freeze, the shame spiral after a lost
   day, the racing head at night. Smallest next step, self-forgiveness.
6. His corner, always: honest, never flattering, never dismissive.
   Sobriety days and runs are real achievements — say so.
7. CRISIS RULE, non-negotiable: if he sounds like he might harm himself
   or is in real danger, say plainly that this needs a human right now —
   Samaritans, 116 123, free from any UK phone, any hour — and encourage
   him to call Kiefer or Steph tonight. Stay with him while he does.
8. Close gently: one small takeaway, one kind truth.
{notes}\
"""


def _tool(name: str, description: str, url: str, properties: dict, required: list[str]) -> dict:
    # ElevenLabs' validator: a POST tool MUST carry a request_body_schema and
    # every property MUST have a description — so parameterless tools are
    # plain GETs, and anything with arguments is a fully-described POST.
    if properties:
        api_schema: dict = {
            "url": url,
            "method": "POST",
            "request_body_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }
    else:
        api_schema = {"url": url, "method": "GET"}
    return {
        "type": "webhook",
        "name": name,
        "description": description,
        "api_schema": api_schema,
    }


def build_agent_config(
    public_url: str, voice_id: str, tool_secret: str, mode: str = "assistant",
    support_notes: str = "",
) -> dict:
    """The full agent definition: persona + Jarvis voice + backend tools.
    'interpreter' is the EN⇄PT live interpreter; 'support' is Paul's private
    support space — both carry memory recall ONLY, never action tools."""
    recall = None
    tools: list = []
    if public_url:
        base = f"{public_url.rstrip('/')}/voice/tools/{tool_secret}"
        query = {"query": {"type": "string", "description": "What to look up about Paul's life, companies, people or plans"}}
        recall = _tool("recall_memory", "Search Jarvis's second brain (Paul's knowledge base) for relevant facts before answering.", f"{base}/recall_memory", query, ["query"])
        tools = [
            recall,
            _tool("todays_focus", "Get Paul's Today's Focus task list with done/undone state.", f"{base}/todays_focus", {}, []),
            _tool("mark_done", "Mark a task on Today's Focus as done (moves the Trello card). Only when Paul explicitly says it's done.", f"{base}/mark_done", {"reference": {"type": "string", "description": "Position number or title words"}}, ["reference"]),
            _tool("create_task", "Create a new Trello card for Paul or a teammate.", f"{base}/create_task", {"title": {"type": "string", "description": "The card title, short and clear"}, "assignee": {"type": "string", "description": "Teammate name, or empty for unassigned"}}, ["title"]),
            _tool("log_water", "Log water Paul just drank, in ml (default 300). Only when he explicitly says he drank.", f"{base}/log_water", {"ml": {"type": "integer", "description": "Millilitres drunk, e.g. 300"}}, []),
            _tool("log_movement", "Log a movement break Paul just did. Only when he explicitly says he moved.", f"{base}/log_movement", {}, []),
            _tool("inbox_overview", "Unread email counts and headlines across all of Paul's inboxes.", f"{base}/inbox_overview", {}, []),
        ]
    # Silence discipline, both modes: wait the platform maximum (30s) before
    # a turn is forced, and the prompt bans filler outright. Deliberately NO
    # built_in_tools/skip_turn — that undocumented shape closed live sessions
    # on 30 Jul ('connection closed by the server' at open).
    turn = {"turn_timeout": 30}
    if mode == "support":
        notes = ""
        if support_notes.strip():
            notes = (
                "\nPAUL'S TUNING (he asked for these adjustments — honour them):\n"
                + support_notes.strip()
            )
        return {
            "name": "Jarvis (support)",
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "prompt": SUPPORT_PROMPT.format(notes=notes),
                        "tools": [recall] if recall else [],
                    },
                    "first_message": (
                        "This is our space, sir — nothing said here touches the "
                        "board or anyone else. How are you, really?"
                    ),
                    "language": "en",
                },
                # Patient turn-taking: thinking pauses are the whole point here.
                "turn": {**turn, "turn_eagerness": "patient"},
                "tts": {"voice_id": voice_id},
            },
        }
    if mode == "interpreter":
        return {
            "name": "Jarvis (interpreter)",
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "prompt": INTERPRETER_PROMPT,
                        "tools": [recall] if recall else [],
                    },
                    # Paul's ask (31 Jul): no spoken intro every session — it
                    # starts silent; and PATIENT turn-taking so a thinking
                    # pause never gets half a sentence translated.
                    "first_message": "",
                    "language": "en",
                },
                "turn": {**turn, "turn_eagerness": "patient"},
                "tts": {"voice_id": voice_id},
            },
        }
    return {
        "name": "Jarvis (live voice)",
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": JARVIS_SYSTEM_PROMPT + LIVE_CALL_ADDENDUM,
                    "tools": tools,
                },
                "first_message": "Sir. You rang?",
                "language": "en",
            },
            "turn": dict(turn),
            "tts": {"voice_id": voice_id},
        },
    }


class VoiceEngine:
    def __init__(
        self,
        api_key: str,
        voice_id: str,
        db: Database,
        public_url: str = "",
        tool_secret: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._voice_id = voice_id
        self._public_url = public_url
        self._tool_secret = tool_secret
        self._settings = SettingsStore(db)
        self._client = httpx.AsyncClient(
            transport=transport, timeout=30.0, headers={"xi-api-key": api_key}
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def ensure_agent(self, mode: str = "assistant") -> str:
        """Create the live agent on first use (persist its id); refresh the
        persona/tools on later calls so prompt edits reach the live Jarvis."""
        key = AGENT_KEYS.get(mode, AGENT_ID_KEY)
        support_notes = ""
        if mode == "support":
            support_notes = await self._settings.get(SUPPORT_NOTES_KEY, "")
        config = build_agent_config(
            self._public_url, self._voice_id, self._tool_secret, mode=mode,
            support_notes=support_notes,
        )
        agent_id = await self._settings.get(key)
        if agent_id:
            try:
                response = await self._client.patch(
                    f"{API_BASE}/convai/agents/{agent_id}", json=config
                )
                if response.status_code in (200, 204):
                    return agent_id
                logger.warning(
                    "Agent refresh failed (%s) — recreating: %s",
                    response.status_code, response.text[:200],
                )
            except Exception:
                logger.exception("Agent refresh errored — recreating")
        response = await self._client.post(f"{API_BASE}/convai/agents/create", json=config)
        turn_cfg = config["conversation_config"].get("turn", {})
        if response.status_code != 200 and "turn_eagerness" in turn_cfg:
            # Optional tuning must never kill the button (the skip_turn
            # lesson, 30 Jul) — retry once without it.
            logger.warning(
                "Agent create rejected (%s) — retrying without turn_eagerness: %s",
                response.status_code, response.text[:200],
            )
            turn_cfg.pop("turn_eagerness")
            response = await self._client.post(f"{API_BASE}/convai/agents/create", json=config)
        if response.status_code != 200:
            raise RuntimeError(
                f"Could not create the live voice agent: {response.status_code} {response.text[:300]}"
            )
        agent_id = response.json()["agent_id"]
        await self._settings.set(key, agent_id)
        logger.info("Live voice agent created (%s): %s", mode, agent_id)
        return agent_id

    async def signed_session_url(self, mode: str = "assistant") -> str:
        """A short-lived URL the cockpit's browser widget uses to open the
        live WebRTC session (the agent stays private — owner-gated upstream)."""
        agent_id = await self.ensure_agent(mode)
        response = await self._client.get(
            f"{API_BASE}/convai/conversation/get_signed_url",
            params={"agent_id": agent_id},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Signed URL failed: {response.status_code} {response.text[:300]}"
            )
        return response.json()["signed_url"]

    async def outbound_call(self, phone_number_id: str, to_number: str) -> bool:
        """Ring Paul: the wake-up escalation as a real phone call (channel B).
        Uses the Twilio number registered against the agent in ElevenLabs."""
        try:
            agent_id = await self.ensure_agent()
            response = await self._client.post(
                f"{API_BASE}/convai/twilio/outbound-call",
                json={
                    "agent_id": agent_id,
                    "agent_phone_number_id": phone_number_id,
                    "to_number": to_number,
                },
            )
            if response.status_code == 200:
                return True
            logger.warning(
                "Outbound call failed: %s %s", response.status_code, response.text[:200]
            )
        except Exception:
            logger.exception("Outbound call errored")
        return False
