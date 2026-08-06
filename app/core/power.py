"""The master switch (6 Aug): Paul can deactivate Jarvis COMPLETELY and bring
him back with a word.

'Jarvis off' (the whole message — so 'turn off the water reminders' can never
trip it) powers everything down: no replies, no calls, no reminders, no jobs,
not even meds. Work-group and health-data ingestion keep filing silently so
nothing is lost, but Jarvis never speaks or acts on any of it. Only Paul
saying 'Jarvis on' (matched generously — he is dyslexic and the stakes of a
missed wake word are a dead assistant) restores him. The state persists in
the settings store, so a redeploy changes nothing.
"""
from __future__ import annotations

import re

POWER_KEY = "jarvis_power"   # "off" = deactivated; anything else = live

# OFF is deliberately strict: the ENTIRE message must be the command.
POWER_OFF_RE = re.compile(
    r"^\W*(hey\s+)?(jarvis|javis|jarvs)?\W*(please\s+)?"
    r"(deactivate|de\s?activ\w+|shut\s?down|power\s?(off|down)|"
    r"switch\s?(yourself\s+)?off|turn\s?(yourself\s+)?off|go\s+offline|off)"
    r"\W*(jarvis|javis)?\W*((completely|fully|totally|now|please|mate|for\s+now)\W*)*$",
    re.IGNORECASE,
)

# ON is deliberately generous: it is only ever consulted while powered off,
# owner-only, where a false positive just wakes Jarvis up.
POWER_ON_RE = re.compile(
    r"(jarvis|javis|jarvs)\W*(is\s+)?(back\s+)?on\b|power\s*(up|on)|"
    r"switch\s*(back\s*)?on|turn\s*(back\s*)?on|\b(re)?activate\b|\bwake\s+up\b|"
    r"\bcome\s+back\b|\bback\s+on(line)?\b|\bonline\b|^\W*on\W*$",
    re.IGNORECASE,
)

OFF_ACK = (
    "Powering down, boss. No messages, no calls, no reminders — not even meds — "
    "until you say the word. I'll keep quietly filing what lands (work groups, "
    "health data) but I won't speak or touch anything. "
    "Say 'Jarvis on' whenever you want me back."
)

ON_ACK = "Back online. Everything's live again — reminders, calls, the lot. What did I miss?"

PHONE_OFF_LINE = (
    "I'm switched off at the moment, sir. Text me 'Jarvis on' and I'm all yours."
)
