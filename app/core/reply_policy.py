"""The smart voice/text mix (locked in Plan §15):
voice for conversation and coaching, text for lists, links and reference detail.

Two signals decide the channel:
1. The model can force text by starting its reply with a [TEXT] tag
   (the persona prompt tells it when to).
2. A structural check catches list-like/link-heavy replies regardless.

Everything is logged as text either way.
"""
from __future__ import annotations

import re

TEXT_TAG = re.compile(r"^\s*\[TEXT\]\s*", re.IGNORECASE)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
URL = re.compile(r"https?://\S+")
MARKDOWNISH = re.compile(r"(^#{1,6}\s)|(\*\*)|(^\s*\|.*\|\s*$)", re.MULTILINE)

# Roughly how much text fits in a comfortable voice note.
MAX_VOICE_CHARS = 900


def decide_reply(raw_reply: str, incoming_was_voice: bool) -> tuple[str, str]:
    """Return (channel, cleaned_text) where channel is 'voice' or 'text'."""
    tagged = bool(TEXT_TAG.match(raw_reply))
    text = TEXT_TAG.sub("", raw_reply, count=1).strip()

    if tagged:
        return "text", text
    if URL.search(text) or MARKDOWNISH.search(text):
        return "text", text
    if len(BULLET.findall(text)) >= 2:
        return "text", text
    if len(text) > MAX_VOICE_CHARS:
        return "text", text
    if not incoming_was_voice and len(text) > 300:
        # Paul typed (may be somewhere he can't play audio) and the reply is
        # substantial — text is the considerate default; short quips still speak.
        return "text", text
    return "voice", text


def strip_for_speech(text: str) -> str:
    """Light cleanup so the TTS never reads formatting characters aloud."""
    cleaned = re.sub(r"[*_#`]+", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()
