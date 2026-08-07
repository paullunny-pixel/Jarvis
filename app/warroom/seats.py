"""The board — three seats, three vendors, three standing roles (§1).

Selection rule is the logic, not the list: one seat per vendor, config
decides which model each vendor fields per tier. Roles rotate between
sessions so 'risk' never becomes permanently baked into one vendor's
disposition — the rotation index lives in settings (SettingsStore),
advanced once per session by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass

ROLES = ["strategist", "sceptic", "operator"]

ROLE_BRIEFS = {
    "strategist": (
        "You are THE STRATEGIST on this board. Focus on the opportunity: growth, "
        "upside, second- and third-order moves, what this unlocks, what a bolder "
        "version looks like. Don't ignore risk, but your job is to find the upside "
        "case a cautious reading would miss."
    ),
    "sceptic": (
        "You are THE SCEPTIC on this board. Focus on what kills this: downside, "
        "risk, legal and contract exposure, hidden costs, the assumptions being "
        "smuggled in, how it fails. Don't ignore opportunity, but your job is to "
        "find the failure mode an optimistic reading would miss."
    ),
    "operator": (
        "You are THE OPERATOR on this board. Focus on whether this can ACTUALLY be "
        "done — with the people, cash, time and attention Paul actually has, not a "
        "theoretical company. You are the reality check on execution."
    ),
}

# Vendor → fallback model when the configured id 404s (preview withdrawn,
# renamed at GA). '(none)' means there's nowhere sensible left to fall to.
FALLBACK_CHAIN = {
    "anthropic": {"claude-fable-5": "claude-opus-5"},
    "openai": {"gpt-5.6-sol": "gpt-5.6-terra"},
    "google": {"gemini-3.1-pro-preview": "gemini-3.6-flash"},
}


@dataclass
class Seat:
    key: str        # 'A' | 'B' | 'C'
    vendor: str      # 'anthropic' | 'openai' | 'google'
    model: str
    role: str        # 'strategist' | 'sceptic' | 'operator'
    effort: str = ""


def rotated_roles(rotation_index: int) -> dict[str, str]:
    """{'A': role, 'B': role, 'C': role} — shifts by one each session."""
    shift = rotation_index % len(ROLES)
    order = ROLES[shift:] + ROLES[:shift]
    return dict(zip(("A", "B", "C"), order))


def build_seats(tier: str, settings, rotation_index: int) -> list[Seat]:
    roles = rotated_roles(rotation_index)
    if tier == "full":
        models = {
            "A": settings.warroom_full_model_a,
            "B": settings.warroom_full_model_b,
            "C": settings.warroom_full_model_c,
        }
        effort_b = settings.warroom_full_effort_b
    else:
        models = {
            "A": settings.warroom_quick_model_a,
            "B": settings.warroom_quick_model_b,
            "C": settings.warroom_quick_model_c,
        }
        effort_b = ""
    vendors = {"A": "anthropic", "B": "openai", "C": "google"}
    return [
        Seat(
            key=k, vendor=vendors[k], model=models[k], role=roles[k],
            effort=effort_b if k == "B" else "",
        )
        for k in ("A", "B", "C")
    ]
