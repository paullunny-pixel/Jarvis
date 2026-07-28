"""Voice feedback → Trello actions (§16.8).

A cheap Haiku call turns Paul's natural speech into structured actions:
"number three's done, push the BMI one to Friday, and stick a card on Kiefer
for the VAT return" → [done 3] [defer bmi→friday] [create "VAT return"→Kiefer].
A keyword gate keeps the parser off ordinary conversation.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.clients.anthropic_client import ClaudeClient
from app.daily12.service import Daily12Service, relative_due

logger = logging.getLogger(__name__)

TASK_HINT = re.compile(
    r"\b(daily )?(12|twelve)\b|\b(today'?s )?focus\b|\btask|\btrello|\bcard\b|\bdone\b"
    r"|\bfinished\b|\btick\b|\bqueue\b|\bpromote\b|\bthis week\b|\bbrain dump\b"
    r"|\b(delete|archive|remove|bin|scrap)\b.{0,24}\b(card|task|order|test|one)\b"
    r"|\bpush (it|that|the|number)|\bput .{0,30}on (kiefer|harry|adriana|adrianna|alicia|kenny|ella)"
    r"|\bassign\b|\bmy (list|plan|tasks)\b"
    r"|\b(add|put|stick|pop|book|schedule)\b.{0,40}\b(calendar|diary)\b",
    re.IGNORECASE,
)

SHOW_PLAN = re.compile(
    r"\b(what('| i)?s|show|give|read)\b.{0,24}\b(my )?(daily |today'?s )?(12|twelve|focus|plan|tasks|list)\b"
    r"|\bplan my (12|twelve|day|focus)\b",
    re.IGNORECASE,
)

PARSER_SYSTEM = """\
You convert Paul's message into Trello task actions. Today's Focus list is:
{plan}

Reply ONLY a JSON array of actions (empty if the message contains none):
- {{"action":"done","target":"<position number or title words>"}}
- {{"action":"defer","target":"...","when":"friday|tomorrow|next week|YYYY-MM-DD"}}
- {{"action":"create","title":"...","assignee":"<name or empty>","when":"<optional>","list":"<the exact column/list name he said (e.g. 'Paul Today', 'This Week', 'Brain Dump'), or empty if he didn't name one>"}}
- {{"action":"comment","target":"...","text":"..."}}
- {{"action":"queue","target":"<number from the This Week listing, or title words>"}}   (move a card into Paul Today for tomorrow)
- {{"action":"promote","target":"..."}}   (Sunday grooming: Brain Dump → This Week)
- {{"action":"archive","target":"..."}}   (delete/remove/bin a card — archives it on Trello)
- {{"action":"show"}}   (he wants to see/hear the list)
- {{"action":"calendar_event","title":"...","when":"<the date/time words he said, or empty>"}}   (he asked to put something in his CALENDAR or diary — never turn this into a plain create)
Only include actions he clearly asked for — and cover EVERY instruction in the
message: two asks means two actions, never just the first. "Put X on <person>"
= create + assignee.\
"""


def mentions_tasks(text: str) -> bool:
    return bool(TASK_HINT.search(text) or SHOW_PLAN.search(text))


def wants_plan(text: str) -> bool:
    return bool(SHOW_PLAN.search(text))


async def parse_actions(claude: ClaudeClient, text: str, plan_text: str) -> list[dict[str, Any]]:
    try:
        raw = await claude.quick(
            text, system=PARSER_SYSTEM.format(plan=plan_text or "(no plan yet)"), max_tokens=600
        )
        start, end = raw.find("["), raw.rfind("]")
        if start < 0:
            return []
        actions = json.loads(raw[start : end + 1])
        return [a for a in actions if isinstance(a, dict) and a.get("action")]
    except Exception:
        logger.exception("Action parsing failed")
        return []


async def execute_actions(
    service: Daily12Service, actions: list[dict[str, Any]]
) -> tuple[list[str], bool]:
    """Run the actions; returns (result lines, whether to show the plan)."""
    results: list[str] = []
    show = False
    today = await service.paul_today()
    for action in actions[:8]:
        kind = action.get("action")
        try:
            if kind == "done":
                results.append(await service.mark_done(str(action.get("target", ""))))
            elif kind == "defer":
                due_iso, human = relative_due(today, str(action.get("when", "tomorrow")))
                results.append(await service.defer(str(action.get("target", "")), due_iso, human))
            elif kind == "create":
                due_iso = ""
                if action.get("when"):
                    due_iso, _ = relative_due(today, str(action["when"]))
                results.append(
                    await service.create(
                        str(action.get("title", "New task")),
                        assignee=str(action.get("assignee", "") or ""),
                        due_iso=due_iso,
                        list_name=str(action.get("list", "") or ""),
                    )
                )
            elif kind == "comment":
                results.append(
                    await service.comment(str(action.get("target", "")), str(action.get("text", "")))
                )
            elif kind == "queue":
                results.append(await service.queue_for_today(str(action.get("target", ""))))
            elif kind == "promote":
                results.append(await service.promote_to_week(str(action.get("target", ""))))
            elif kind == "archive":
                results.append(await service.archive(str(action.get("target", ""))))
            elif kind == "calendar_event":
                # Honesty over silence: calendar write-back isn't wired yet
                # (needs the Google OAuth upgrade), so the event is parked as a
                # card rather than quietly dropped. Never claim it's booked.
                title = str(action.get("title", "") or "Untitled event")
                due_iso = ""
                if action.get("when"):
                    due_iso, _ = relative_due(today, str(action["when"]))
                parked = await service.create(f"CALENDAR: {title}", due_iso=due_iso)
                results.append(
                    "On the calendar I have to be straight with you, sir — I can "
                    "read it but can't add events yet (that needs the Google "
                    f"OAuth upgrade). So it isn't lost: {parked}"
                )
            elif kind == "show":
                show = True
        except Exception:
            logger.exception("Action failed: %s", action)
            results.append(f"Something went wrong with '{kind}' — try that one again.")
    return results, show
