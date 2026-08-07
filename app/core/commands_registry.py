"""The Commands tab's data (Mac app v2, §3): a maintained, categorised list
of the phrases/keywords Jarvis understands. Not auto-extracted from
router.py — that file mixes hundreds of regex/intent branches and reliable
extraction isn't practical; this list is the authoritative source instead,
kept current by hand as commands are added. Every thin client (Mac app,
cockpit) renders the SAME list via GET /desktop/{secret}/commands."""
from __future__ import annotations

COMMANDS: list[dict[str, str]] = [
    # --- Rhythm / wake ---
    {"category": "Rhythm", "phrase": "set wake 05:00", "does": "Lock tomorrow's gospel wake time — Jarvis calls until proof."},
    {"category": "Rhythm", "phrase": "wake me at 6 tomorrow", "does": "One-off delay to the wake sequence."},
    {"category": "Rhythm", "phrase": "test alarm", "does": "Drills the wake sequence with short intervals — no real data written."},
    {"category": "Rhythm", "phrase": "override", "does": "Stands the wake call down instantly, no penalty."},
    {"category": "Rhythm", "phrase": "goodnight", "does": "Ends the day — nightly brief refresh, reflection notes for tomorrow."},
    {"category": "Rhythm", "phrase": "quiet day / cancel my notifications today", "does": "Suppresses every non-essential send for the local day (meds still fire)."},
    {"category": "Rhythm", "phrase": "notifications back on", "does": "Lifts a quiet day early."},
    {"category": "Rhythm", "phrase": "I'm in [city] / what's the time there", "does": "Sets/checks the working timezone."},

    # --- Body / gates ---
    {"category": "Body", "phrase": "water 500ml / water Xml", "does": "Logs water manually (Apple Health imports merge, never double-count)."},
    {"category": "Body", "phrase": "moved", "does": "Logs an hourly movement break."},
    {"category": "Body", "phrase": "I've done my run / I have NOT done my run", "does": "Logs (or explicitly skips) the run gate — negation-aware."},
    {"category": "Body", "phrase": "meds done", "does": "Logs ADHD/supplements/TRT adherence."},
    {"category": "Body", "phrase": "rest day", "does": "Settles the run gate without a run — a valid entry, not a broken streak."},

    # --- Board / Today's Focus ---
    {"category": "Board", "phrase": "Jarvis add to Trello: ...", "does": "Explicit escape hatch to the proven card parser (title, domain, list, priority, due, owner, checklist, board)."},
    {"category": "Board", "phrase": "park: [task]", "does": "Files a task straight into Brain Dump."},
    {"category": "Board", "phrase": "focus sprint", "does": "Starts a focused work sprint on today's next task."},
    {"category": "Board", "phrase": "sync the board", "does": "Forces a fresh Trello pull for Today's Focus."},
    {"category": "Board", "phrase": "hound me", "does": "Turns on hourly reminders for an owed task."},
    {"category": "Board", "phrase": "I've done [task]", "does": "Ticks a Today's Focus task and moves the Trello card."},

    # --- Meetings ---
    {"category": "Meetings", "phrase": "start a new zoom meeting", "does": "Creates the meeting, sends join links, arms Otter to record and file notes."},
    {"category": "Meetings", "phrase": "catch me up on [group]", "does": "Summarises a work Telegram group since you last checked."},

    # --- Mail ---
    {"category": "Mail", "phrase": "draft an email to ...", "does": "Writes a draft in your own style — sends only after a read-back confirm."},
    {"category": "Mail", "phrase": "read all my emails from ... and summarise", "does": "Whole-mailbox research across every connected inbox."},
    {"category": "Mail", "phrase": "learn my style", "does": "Distils your writing style from voice notes and Telegram messages."},

    # --- Second brain / documents ---
    {"category": "Brain", "phrase": "remember that ...", "does": "Files a living fact into the second brain."},
    {"category": "Brain", "phrase": "show me the [X] contract/document", "does": "Recalls a filed document by meaning."},
    {"category": "Brain", "phrase": "add X to the build list", "does": "Notes an upgrade idea for a future build session."},
    {"category": "Brain", "phrase": "show the build list", "does": "Reads back the pending upgrade wishlist."},

    # --- Persona / cockpit ---
    {"category": "Settings", "phrase": "tune jarvis: ...", "does": "Adjusts tone/persona notes — outranks the base persona."},
    {"category": "Settings", "phrase": "reset jarvis persona", "does": "Clears your tuning notes back to the base persona."},
    {"category": "Settings", "phrase": "set cockpit password ...", "does": "Locks the web cockpit behind a password."},
    {"category": "Settings", "phrase": "log out the cockpit everywhere", "does": "Rotates the cockpit session key, ending every logged-in session."},
    {"category": "Settings", "phrase": "update your brief", "does": "Refreshes the living Paul Brief on demand."},

    # --- War Room / Team Radar ---
    {"category": "War Room", "phrase": "war room: [topic]", "does": "Frames a multi-advisor session — confirm before it spends anything."},
    {"category": "War Room", "phrase": "what needs me", "does": "Team Radar's top-5 stalled/blocked items across every board."},

    # --- Location / GPS ---
    {"category": "Location", "phrase": "this is [place name]", "does": "Teaches Jarvis a named place for future recognition."},
    {"category": "Location", "phrase": "remind me at [place] to ...", "does": "Adds a recurring geofence task."},
    {"category": "Location", "phrase": "flying today? yes/no", "does": "Confirms an airport-detected travel day."},
]


def commands_by_category() -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for cmd in COMMANDS:
        grouped.setdefault(cmd["category"], []).append(
            {"phrase": cmd["phrase"], "does": cmd["does"]}
        )
    return grouped
