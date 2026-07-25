"""Plan §16 exact-logic verification."""
import unittest
from datetime import date

from app.daily12.scoring import (
    COMPANIES,
    WEIGHTS,
    Card,
    avoidance_score,
    deadline_score,
    is_actionable_list,
    money_score,
    parse_iso_date,
    score_card,
    select_daily_12,
    unblocks_score,
)

TODAY = date(2026, 7, 25)


def card(i, company="derma_uk", project=1, **kw) -> Card:
    return Card(id=i, title=f"card {i}", company=company, project_id=project, **kw)


class TestComponentScores(unittest.TestCase):
    def test_deadline_overdue_is_1(self):
        self.assertEqual(deadline_score(date(2026, 7, 20), TODAY), 1.0)
        self.assertEqual(deadline_score(TODAY, TODAY), 1.0)

    def test_deadline_decays_with_days_out(self):
        week_out = deadline_score(date(2026, 8, 1), TODAY)
        fortnight_out = deadline_score(date(2026, 8, 8), TODAY)
        self.assertGreater(week_out, fortnight_out)
        self.assertEqual(fortnight_out, 0.0)
        self.assertEqual(deadline_score(None, TODAY), 0.0)

    def test_money_from_label_grade(self):
        self.assertEqual(money_score(0), 0.0)
        self.assertEqual(money_score(3), 1.0)
        self.assertAlmostEqual(money_score(2), 2 / 3)

    def test_unblocks(self):
        self.assertEqual(unblocks_score(True), 1.0)
        self.assertEqual(unblocks_score(False), 0.0)

    def test_avoidance_grows_with_stagnation(self):
        fresh = avoidance_score(TODAY, TODAY)
        week = avoidance_score(date(2026, 7, 18), TODAY)
        month = avoidance_score(date(2026, 6, 20), TODAY)
        self.assertEqual(fresh, 0.0)
        self.assertLess(week, month)
        self.assertEqual(month, 1.0)  # capped

    def test_avoidance_defer_boost_and_cap(self):
        base = avoidance_score(TODAY, TODAY, defer_count=0)
        deferred = avoidance_score(TODAY, TODAY, defer_count=2)
        self.assertAlmostEqual(deferred - base, 0.2)
        self.assertAlmostEqual(avoidance_score(TODAY, TODAY, defer_count=10), 0.3)

    def test_weights_match_spec(self):
        self.assertEqual(WEIGHTS, {"deadline": 0.35, "money": 0.25, "unblocks": 0.25, "avoidance": 0.15})
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0)

    def test_score_formula_exact(self):
        c = card(1, due_date=TODAY, money=3, waiting_on_paul=True, last_moved=date(2026, 6, 1))
        # 0.35·1 + 0.25·1 + 0.25·1 + 0.15·1 = 1.0
        self.assertEqual(score_card(c, TODAY), 1.0)
        c2 = card(2, due_date=None, money=0, waiting_on_paul=False, last_moved=TODAY)
        self.assertEqual(score_card(c2, TODAY), 0.0)

    def test_excluded_lists(self):
        self.assertFalse(is_actionable_list("Done"))
        self.assertFalse(is_actionable_list("Blocked"))
        self.assertFalse(is_actionable_list(" blocked/waiting "))
        self.assertTrue(is_actionable_list("In Progress"))

    def test_parse_iso_date(self):
        self.assertEqual(parse_iso_date("2026-07-25T10:00:00.000Z"), date(2026, 7, 25))
        self.assertIsNone(parse_iso_date(""))
        self.assertIsNone(parse_iso_date("garbage"))


class TestSelection(unittest.TestCase):
    def make_board(self):
        cards = []
        i = 0
        for company_index, company in enumerate(COMPANIES):
            for project in (1, 2, 3):
                project_id = company_index * 10 + project
                for n in range(4):  # 4 cards per project → 48 cards
                    i += 1
                    cards.append(
                        card(
                            i, company=company, project=project_id,
                            due_date=date(2026, 7, 25 + n),  # first card most urgent
                            last_moved=date(2026, 7, 20),
                        )
                    )
        live = {c: [ci * 10 + 1, ci * 10 + 2, ci * 10 + 3] for ci, c in enumerate(COMPANIES)}
        return cards, live

    def test_selects_exactly_12_one_per_live_project(self):
        cards, live = self.make_board()
        sel = select_daily_12(cards, live, TODAY)
        self.assertEqual(len(sel.picks), 12)
        for company in COMPANIES:
            self.assertEqual(len(sel.by_company[company]), 3)
            projects = {c.project_id for c in sel.by_company[company]}
            self.assertEqual(len(projects), 3)  # one card per project

    def test_top_scored_card_wins_within_project(self):
        cards, live = self.make_board()
        sel = select_daily_12(cards, live, TODAY)
        for pick in sel.picks:
            rivals = [
                c for c in cards
                if c.project_id == pick.project_id and c.actionable and c.id != pick.id
            ]
            self.assertTrue(all(pick.score >= r.score for r in rivals))

    def test_empty_project_pulls_next_best_from_company(self):
        cards, live = self.make_board()
        # project 3 of derma_uk (id 3) has no cards left
        cards = [c for c in cards if c.project_id != 3]
        sel = select_daily_12(cards, live, TODAY)
        self.assertEqual(len(sel.by_company["derma_uk"]), 3)  # still 3 — backfilled
        self.assertEqual(len(sel.picks), 12)

    def test_non_actionable_cards_excluded(self):
        cards, live = self.make_board()
        for c in cards:
            if c.company == "prodermis":
                c.actionable = False
        sel = select_daily_12(cards, live, TODAY)
        self.assertEqual(sel.by_company["prodermis"], [])
        self.assertEqual(len(sel.picks), 9)

    def test_bonus_is_next_highest_unpicked(self):
        cards, live = self.make_board()
        sel = select_daily_12(cards, live, TODAY)
        self.assertIsNotNone(sel.bonus)
        picked = {c.id for c in sel.picks}
        self.assertNotIn(sel.bonus.id, picked)
        for c in cards:
            if c.id not in picked and c.id != sel.bonus.id:
                self.assertGreaterEqual(sel.bonus.score, c.score)

    def test_no_duplicate_picks(self):
        cards, live = self.make_board()
        sel = select_daily_12(cards, live, TODAY)
        ids = [c.id for c in sel.picks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_urgent_waiting_money_card_beats_quiet_card(self):
        urgent = card(1, due_date=TODAY, money=3, waiting_on_paul=True, last_moved=date(2026, 6, 1))
        quiet = card(2, due_date=None, money=0, last_moved=TODAY)
        sel = select_daily_12([urgent, quiet], {"derma_uk": [1]}, TODAY)
        self.assertEqual(sel.picks[0].id, 1)


if __name__ == "__main__":
    unittest.main()
