import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.persona import build_system_prompt


class TestConfig(unittest.TestCase):
    def make(self, **kw) -> Settings:
        return Settings(_env_file=None, **kw)

    def test_webhook_secret_derived_and_stable(self):
        s = self.make(telegram_bot_token="ABC:123")
        self.assertEqual(len(s.effective_webhook_secret), 32)
        self.assertEqual(s.effective_webhook_secret, self.make(telegram_bot_token="ABC:123").effective_webhook_secret)
        self.assertNotEqual(
            s.effective_webhook_secret, self.make(telegram_bot_token="OTHER").effective_webhook_secret
        )

    def test_explicit_webhook_secret_wins(self):
        s = self.make(webhook_secret="mysecret")
        self.assertEqual(s.effective_webhook_secret, "mysecret")

    def test_defaults(self):
        s = self.make()
        self.assertEqual(s.brain_model, "claude-opus-5")
        self.assertEqual(s.fast_model, "claude-haiku-4-5")
        self.assertEqual(s.timezone_default, "Europe/London")


class TestPersona(unittest.TestCase):
    def test_prompt_carries_paul_time(self):
        now = datetime(2026, 7, 25, 14, 30, tzinfo=ZoneInfo("UTC"))
        prompt = build_system_prompt(now=now, timezone="Asia/Dubai")
        self.assertIn("18:30", prompt)  # Dubai is UTC+4
        self.assertIn("Asia/Dubai", prompt)
        self.assertIn("Jarvis", prompt)
        self.assertIn("beat the freeze", prompt)

    def test_memory_context_appended_when_present(self):
        prompt = build_system_prompt(memory_context="Villa balance: 27% paid.")
        self.assertIn("Villa balance", prompt)
        self.assertNotIn("Relevant knowledge", build_system_prompt())

    def test_persona_is_mate_first_not_butler(self):
        # 31 Jul: Paul asked for less robot, more human — familiarity that grows.
        prompt = build_system_prompt()
        self.assertIn("mate FIRST", prompt)
        self.assertIn("occasional affectionate flourish", prompt)   # 'sir' demoted
        self.assertIn("This relationship GROWS", prompt)
        self.assertIn("never guilt-tripping", prompt)               # hard lines held

    def test_persona_notes_and_brief_ride_every_prompt(self):
        prompt = build_system_prompt(
            persona_notes="Talk to me like ChatGPT does: casual, direct, curious.",
            paul_brief="RIGHT NOW: in Dubai until Friday (as of 31 Jul).",
        )
        self.assertIn("PAUL'S OWN WORDS", prompt)
        self.assertIn("casual, direct, curious", prompt)
        self.assertIn("THE PAUL BRIEF", prompt)
        self.assertIn("in Dubai until Friday", prompt)
        # Notes outrank the base persona and sit above the brief.
        self.assertLess(prompt.index("PAUL'S OWN WORDS"), prompt.index("THE PAUL BRIEF"))
        bare = build_system_prompt()
        self.assertNotIn("PAUL'S OWN WORDS", bare)
        self.assertNotIn("THE PAUL BRIEF", bare)

    def test_prompt_anchors_time_and_forbids_phantom_actions(self):
        # The Monday-after bug: 'Sunday midnight' spoken of as upcoming, and a
        # deletion claimed that never happened. Both rules ride every turn.
        prompt = build_system_prompt()
        self.assertIn("deadline before now is PAST", prompt)
        self.assertIn("NEVER claim a card was created", prompt)


if __name__ == "__main__":
    unittest.main()
