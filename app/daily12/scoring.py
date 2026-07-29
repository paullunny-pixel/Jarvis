"""The Daily 12 — exact selection logic from Plan §16, as pure functions.

Score = 0.35·Deadline + 0.25·Money + 0.25·UnblocksOthers + 0.15·AvoidanceAge

- Deadline:     overdue = 1, decays with days-out (linear to 0 at 14 days).
- Money:        from the revenue/payment label (£ = .34, ££ = .67, £££ = 1).
- UnblocksOthers: card where Kiefer/team are waiting on Paul (flag).
- AvoidanceAge: days sitting without progress (1 at 21+ days), boosted by
                repeat deferrals (§16.9 learning: +0.1 each, capped).

Selection: for each company, from each of its 3 live projects take the
top-scored actionable card → 3 × 4 = 12. If a project has none, pull the
next-best card from that company. Bonus = next-highest overall once all 12
are done. The other ~50 cards stay hidden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

WEIGHTS = {"deadline": 0.35, "money": 0.25, "unblocks": 0.25, "avoidance": 0.15}

DEADLINE_HORIZON_DAYS = 14
AVOIDANCE_CEILING_DAYS = 21
DEFER_BOOST = 0.10
DEFER_BOOST_CAP = 0.30

COMPANIES = ["derma_uk", "derma_eu", "aesthetics_supply", "prodermis"]
COMPANY_NAMES = {
    "derma_uk": "Derma Direct UK",
    "derma_eu": "Derma Direct EU",
    "aesthetics_supply": "Aesthetics Supply UK",
    "prodermis": "Prodermis",
}
PROJECTS_PER_COMPANY = 3

EXCLUDED_LISTS = {"done", "blocked", "waiting", "blocked/waiting", "complete", "completed"}


@dataclass
class Card:
    id: int
    title: str
    company: str
    project_id: int = 0
    due_date: date | None = None
    money: int = 0                 # 0..3
    waiting_on_paul: bool = False
    last_moved: date | None = None
    defer_count: int = 0
    actionable: bool = True
    score: float = field(default=0.0, compare=False)


def is_actionable_list(list_name: str) -> bool:
    """Out of play: any Done pile ('Done', 'Done!', 'Done Week 27th July'),
    and anything blocked/waiting ('Blocked / Waiting on') — the real board's
    names vary, so match by shape, not exact string."""
    name = list_name.strip().lower()
    if name in EXCLUDED_LISTS:
        return False
    return not (name.startswith("done") or "blocked" in name or name.startswith("waiting"))


def deadline_score(due: date | None, today: date) -> float:
    if due is None:
        return 0.0
    days_out = (due - today).days
    if days_out <= 0:
        return 1.0
    return max(0.0, 1.0 - days_out / DEADLINE_HORIZON_DAYS)


def money_score(money_label: int) -> float:
    return min(max(money_label, 0), 3) / 3.0


def unblocks_score(waiting_on_paul: bool) -> float:
    return 1.0 if waiting_on_paul else 0.0


def avoidance_score(last_moved: date | None, today: date, defer_count: int = 0) -> float:
    if last_moved is None:
        age = 0.0
    else:
        age = min(1.0, max(0, (today - last_moved).days) / AVOIDANCE_CEILING_DAYS)
    return min(1.0, age + min(defer_count * DEFER_BOOST, DEFER_BOOST_CAP))


def score_card(card: Card, today: date, weights: dict[str, float] | None = None) -> float:
    w = weights or WEIGHTS
    return round(
        w["deadline"] * deadline_score(card.due_date, today)
        + w["money"] * money_score(card.money)
        + w["unblocks"] * unblocks_score(card.waiting_on_paul)
        + w["avoidance"] * avoidance_score(card.last_moved, today, card.defer_count),
        6,
    )


@dataclass
class Selection:
    picks: list[Card]                       # up to 12, grouped by company order
    bonus: Card | None                      # next-highest once the 12 are done
    by_company: dict[str, list[Card]]


def select_daily_12(
    cards: list[Card],
    live_projects: dict[str, list[int]],    # company -> up to 3 live project ids
    today: date,
    weights: dict[str, float] | None = None,
) -> Selection:
    pool = [c for c in cards if c.actionable]
    for card in pool:
        card.score = score_card(card, today, weights)

    picked_ids: set[int] = set()
    by_company: dict[str, list[Card]] = {}

    for company in COMPANIES:
        company_cards = sorted(
            (c for c in pool if c.company == company),
            key=lambda c: c.score,
            reverse=True,
        )
        picks: list[Card] = []
        # 1) top card from each live project
        for project_id in (live_projects.get(company) or [])[:PROJECTS_PER_COMPANY]:
            for card in company_cards:
                if card.project_id == project_id and card.id not in picked_ids:
                    picks.append(card)
                    picked_ids.add(card.id)
                    break
        # 2) empty projects → next-best card from the company (§16.5)
        for card in company_cards:
            if len(picks) >= PROJECTS_PER_COMPANY:
                break
            if card.id not in picked_ids:
                picks.append(card)
                picked_ids.add(card.id)
        by_company[company] = picks

    ordered = [c for company in COMPANIES for c in by_company[company]]
    remaining = sorted(
        (c for c in pool if c.id not in picked_ids), key=lambda c: c.score, reverse=True
    )
    return Selection(picks=ordered, bonus=remaining[0] if remaining else None, by_company=by_company)


@dataclass
class FocusSelection:
    """Today's Focus (Master Update §2): a VARIABLE-length list sourced only
    from the 'Paul Today' (business, ≤3 per company) and 'Paul Personal'
    (≤3) Trello lists. §16 scoring only ORDERS within the pool."""

    picks: list[Card]
    by_group: dict[str, list[Card]]   # company slug, "" (untagged) or "personal"


def select_focus(
    business: list[Card],
    personal: list[Card],
    today: date,
    per_company: int = 3,
    personal_max: int = 3,
    weights: dict[str, float] | None = None,
) -> FocusSelection:
    for card in business + personal:
        card.score = score_card(card, today, weights)
    by_group: dict[str, list[Card]] = {}
    picks: list[Card] = []
    for company in COMPANIES + [""]:   # "" = business cards not yet tagged
        group = sorted(
            (c for c in business if c.actionable and c.company == company),
            key=lambda c: c.score,
            reverse=True,
        )[:per_company]
        if group:
            by_group[company] = group
            picks.extend(group)
    personal_picks = sorted(
        (c for c in personal if c.actionable), key=lambda c: c.score, reverse=True
    )[:personal_max]
    if personal_picks:
        by_group["personal"] = personal_picks
        picks.extend(personal_picks)
    return FocusSelection(picks=picks, by_group=by_group)


def parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None
