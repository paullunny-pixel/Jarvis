"""The Card Script panel's single source of truth (6 Aug brief §4).

The Mac app renders /desktop/{secret}/card-grammar verbatim — nothing about
the dictation grammar is hard-coded in the app. Values come from the SAME
live Trello sync the card parser resolves against, so a renamed list shows
up on the next sync, not the next release. When the layer isn't available
(keys missing, Trello down) a static snapshot keeps the panel honest-enough
rather than blank.

One deliberate divergence from the brief's copy: the DEFAULTS row shows what
the parser ACTUALLY does — unspecified cards land on Master Board / Inbox
with NO invented priority (the never-invent rule) and read back before any
write. The brief's 'Backlog · P3' text described a parser we don't have, and
the brief's own rule ('a cheat-sheet that lies is worse than no cheat-sheet')
outranks its copy.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The colour IS the index (brief §3a) — tokens shared by sentence + legend.
COLOURS = {
    "ink": "#ffffff", "gold": "#f0b429", "green": "#3fd08a",
    "purple": "#a78bfa", "accent": "#5ec8f2", "pink": "#ff9ec4",
    "slate": "#4a5a70",
}

# Paul's own sentence, verbatim (brief §3a) — segmented for colouring.
EXAMPLE_PARTS = [
    {"text": "Hey Jarvis — add a card, ", "colour": "slate"},
    {"text": "email BMI the full sales plan", "colour": "ink"},
    {"text": ", ", "colour": "slate"},
    {"text": "Prodermis", "colour": "gold"},
    {"text": ", ", "colour": "slate"},
    {"text": "Paul Today", "colour": "green"},
    {"text": ", ", "colour": "slate"},
    {"text": "P1", "colour": "purple"},
    {"text": ", ", "colour": "slate"},
    {"text": "due Friday", "colour": "accent"},
    {"text": ", ", "colour": "slate"},
    {"text": "Harry", "colour": "pink"},
]

FALLBACK = {
    "company": "Derma UK/EU · Prodermis · Aesthetics · Personal",
    "list": "Inbox · Paul Today · This Week",
    "priority": "P1–P5 · P1/P2 auto-tag Urgent",
    "who": "Harry · Kiefer — or nobody = you",
}

PREFERRED_LISTS = ["Inbox", "Paul Today", "This Week", "Backlog", "Brain Dump"]


def _fields(company: str, lists: str, priority: str, who: str) -> list[dict]:
    """Seven rows, in SPEAKING order (brief §3b) — never re-sorted."""
    return [
        {"key": "what", "label": "What", "colour": "ink",
         "value": "Plain English. The only must.", "required": True},
        {"key": "company", "label": "Company", "colour": "gold", "value": company},
        {"key": "list", "label": "List", "colour": "green", "value": lists},
        {"key": "priority", "label": "Priority", "colour": "purple", "value": priority},
        {"key": "due", "label": "Due", "colour": "accent",
         "value": '"Friday" · "28 Aug" · "end of month"'},
        {"key": "who", "label": "Who", "colour": "pink", "value": who},
        {"key": "steps", "label": "Steps", "colour": "slate",
         "value": '"with a checklist: quote, compare, order"'},
    ]


async def card_grammar(layer_factory=None) -> dict:
    company, lists, priority, who = (
        FALLBACK["company"], FALLBACK["list"], FALLBACK["priority"], FALLBACK["who"]
    )
    live = False
    if layer_factory is not None:
        try:
            layer = await layer_factory()
            bmap = layer.board("master") if layer is not None else None
            if bmap is not None:
                domains = sorted(bmap.options.get("Domain", {}))
                if domains:
                    company = " · ".join(domains)
                names = [n for n in PREFERRED_LISTS if n in bmap.lists]
                names += [
                    n for n in bmap.lists
                    if n not in names and not n.lower().startswith(("done", "(archive"))
                ][: max(0, 4 - len(names))]
                if names:
                    lists = " · ".join(names[:4])
                prios = sorted(bmap.options.get("Priority", {}))
                if prios:
                    priority = " · ".join(prios) + " · P1/P2 auto-tag Urgent"
                members = sorted(m.title() for m in bmap.members if m.lower() != "paul")
                if members:
                    who = " · ".join(members) + " — or nobody = you"
                live = True
        except Exception:
            logger.exception("Card grammar: live Trello read failed — static fallback")
    return {
        "example": "".join(p["text"] for p in EXAMPLE_PARTS),
        "example_parts": EXAMPLE_PARTS,
        "colours": COLOURS,
        "fields": _fields(company, lists, priority, who),
        # What the parser truly does with a bare card (never-invent rule).
        "defaults": {"board": "Master Board", "list": "Inbox", "priority": None,
                     "owner": "Paul", "due": None},
        "defaults_note": (
            "Say nothing? Master Board · Inbox · no priority · yours. "
            "He reads it back first — \"no, This Week\" fixes it."
        ),
        "readback": True,
        "live": live,
    }
