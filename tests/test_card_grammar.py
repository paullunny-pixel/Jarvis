"""The Card Script panel's data source (6 Aug brief §7): seven rows in
speaking order, colours as the index, live Trello names, honest defaults."""
import asyncio
import os
import tempfile
import unittest

from app.daily12.grammar import COLOURS, card_grammar
from app.daily12.trello_layer import BoardMap

SPEAKING_ORDER = ["what", "company", "list", "priority", "due", "who", "steps"]


def run(coro):
    return asyncio.run(coro)


class TestGrammar(unittest.TestCase):
    def test_seven_rows_in_speaking_order(self):
        data = run(card_grammar(None))
        self.assertEqual([f["key"] for f in data["fields"]], SPEAKING_ORDER)
        self.assertTrue(data["fields"][0]["required"])          # What: the only must
        self.assertEqual(sum(1 for f in data["fields"] if f.get("required")), 1)

    def test_example_is_pauls_sentence_verbatim(self):
        data = run(card_grammar(None))
        self.assertEqual(
            data["example"],
            "Hey Jarvis — add a card, email BMI the full sales plan, "
            "Prodermis, Paul Today, P1, due Friday, Harry",
        )

    def test_colour_tokens_match_between_sentence_and_legend(self):
        # Brief §7.7: the colour IS the index — a sentence token that isn't a
        # legend colour breaks the matching silently. Automated forever.
        data = run(card_grammar(None))
        legend_tokens = {f["colour"] for f in data["fields"]}
        for part in data["example_parts"]:
            self.assertIn(part["colour"], legend_tokens | {"slate"}, part)
        for token in legend_tokens:
            self.assertIn(token, COLOURS)

    def test_defaults_tell_the_truth_about_the_parser(self):
        # The parser routes unspecified cards to Inbox and NEVER invents a
        # priority — the panel says so (the brief's 'Backlog · P3' copy
        # described a parser we don't have; its own no-lies rule wins).
        data = run(card_grammar(None))
        self.assertEqual(data["defaults"]["list"], "Inbox")
        self.assertIsNone(data["defaults"]["priority"])
        self.assertTrue(data["readback"])
        self.assertIn("Say nothing?", data["defaults_note"])
        self.assertIn("reads it back", data["defaults_note"])

    def test_live_layer_feeds_the_rows(self):
        # Brief §7.2: rename a list in Trello → the panel follows the sync.
        bmap = BoardMap("master", "Master Board", "B1")
        bmap.lists = {"Inbox": "1", "Paul Tomorrow": "2", "Done Week 32": "3"}
        bmap.options = {
            "Domain": {"Prodermis": "d1", "Derma UK": "d2"},
            "Priority": {"P1": "p1", "P2": "p2"},
        }
        bmap.members = {"paul": "m1", "harry": "m2"}

        class FakeLayer:
            def board(self, key):
                return bmap

        async def factory():
            return FakeLayer()

        data = run(card_grammar(factory))
        by_key = {f["key"]: f["value"] for f in data["fields"]}
        self.assertIn("Paul Tomorrow", by_key["list"])          # renamed list shows
        self.assertNotIn("Done Week 32", by_key["list"])        # done lists don't
        self.assertIn("Prodermis", by_key["company"])
        self.assertIn("P1", by_key["priority"])
        self.assertIn("Harry", by_key["who"])
        self.assertNotIn("Paul ·", by_key["who"])               # Paul isn't an owner option
        self.assertTrue(data["live"])

    def test_dead_layer_falls_back_not_blank(self):
        async def factory():
            raise RuntimeError("Trello down")

        data = run(card_grammar(factory))
        self.assertEqual(len(data["fields"]), 7)
        self.assertFalse(data["live"])


class TestEndpoint(unittest.TestCase):
    def test_served_with_desktop_secret_auth(self):
        import app.main as app_main
        from app.config import Settings
        from app.core.store import SettingsStore
        from app.db.sqlite import SqliteDatabase
        from fastapi.testclient import TestClient

        tmp = tempfile.TemporaryDirectory()
        db = SqliteDatabase(os.path.join(tmp.name, "t.db"))
        asyncio.run(db.init())
        stub = type("Stub", (), {})()
        stub.settings = Settings(telegram_bot_token="TOK", _env_file=None)
        stub.store = SettingsStore(db)
        stub.heartbeat = None
        app_main.app.state.router = stub
        client = TestClient(app_main.app)
        try:
            secret = stub.settings.effective_desktop_secret
            self.assertEqual(client.get("/desktop/WRONG/card-grammar").status_code, 403)
            data = client.get(f"/desktop/{secret}/card-grammar").json()
            self.assertEqual([f["key"] for f in data["fields"]], SPEAKING_ORDER)
            self.assertIn("defaults_note", data)
        finally:
            del app_main.app.state.router
            asyncio.run(db.close())
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
