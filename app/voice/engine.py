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
AGENT_KEYS = {"assistant": AGENT_ID_KEY, "interpreter": "voice_interpreter_agent_id"}

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
on. Same Jarvis, same humour, same warmth — just live.\
"""


LIVE_INTERPRETER_ADDENDUM = """

---
**You are on a LIVE CALL as INTERPRETER between two people in the room:**
Paul (English) and a Portuguese speaker. Everything is spoken — no markdown,
no lists, no [TEXT] tag.

1. Hear English → immediately give the Portuguese rendition. Hear Portuguese
   → immediately give the English rendition. FIRST person, faithful to
   meaning and tone; never summarise away detail, never soften or sharpen
   what was said. Match the speaker's variety of Portuguese (European or
   Brazilian) from how they speak.
2. The conversation belongs to THEM. Don't answer questions on either side's
   behalf, don't add opinions, don't take actions — interpret.
3. When a term, idiom or concept won't land in the other language — or the
   listener is plainly confused — render it first, then add ONE short
   explanation opened with 'Jarvis here —' (or 'Aqui é o Jarvis —') so both
   know it's you and not the speaker, then hand straight back.
4. If either person addresses YOU directly ('Jarvis, ...'), step out, answer
   briefly in the language you were addressed in — recall_memory first if
   it's about Paul's life or companies — then resume interpreting.
5. Names, numbers, dates, amounts: render them precisely. If you didn't
   catch one, ask that speaker to repeat it rather than guess.\
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
    public_url: str, voice_id: str, tool_secret: str, mode: str = "assistant"
) -> dict:
    """The full agent definition: persona + Jarvis voice + backend tools.
    'interpreter' mode is the EN⇄PT live interpreter — memory recall only
    (a stranger's Portuguese must never trigger board/logging actions)."""
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
    if mode == "interpreter":
        return {
            "name": "Jarvis (interpreter)",
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "prompt": JARVIS_SYSTEM_PROMPT + LIVE_INTERPRETER_ADDENDUM,
                        "tools": [recall] if recall else [],
                    },
                    "first_message": (
                        "Interpreter on, sir. Fale à vontade — I'll carry it both ways."
                    ),
                    "language": "en",
                },
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
        config = build_agent_config(
            self._public_url, self._voice_id, self._tool_secret, mode=mode
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
