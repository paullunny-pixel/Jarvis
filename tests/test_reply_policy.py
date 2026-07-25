import unittest

from app.core.reply_policy import decide_reply, strip_for_speech


class TestReplyPolicy(unittest.TestCase):
    def test_short_coaching_reply_to_voice_is_voice(self):
        channel, text = decide_reply("Paul, stop. Do the survey now. Two minutes.", True)
        self.assertEqual(channel, "voice")
        self.assertEqual(text, "Paul, stop. Do the survey now. Two minutes.")

    def test_text_tag_forces_text(self):
        channel, text = decide_reply("[TEXT] Here's the list:\n1. one\n2. two", True)
        self.assertEqual(channel, "text")
        self.assertNotIn("[TEXT]", text)

    def test_bulleted_reply_is_text(self):
        reply = "Today:\n- run 5km\n- BMI surveys\n- call Kiefer"
        self.assertEqual(decide_reply(reply, True)[0], "text")

    def test_numbered_list_is_text(self):
        reply = "Plan:\n1. Warm up\n2. Push day\n3. Cooldown"
        self.assertEqual(decide_reply(reply, True)[0], "text")

    def test_url_is_text(self):
        self.assertEqual(decide_reply("Board's here https://trello.com/b/x", True)[0], "text")

    def test_very_long_reply_is_text(self):
        self.assertEqual(decide_reply("word " * 400, True)[0], "text")

    def test_short_reply_to_typed_message_is_voice(self):
        self.assertEqual(decide_reply("On it. Surveys first.", False)[0], "voice")

    def test_medium_reply_to_typed_message_is_text(self):
        self.assertEqual(decide_reply("x" * 400, False)[0], "text")

    def test_medium_reply_to_voice_is_voice(self):
        self.assertEqual(decide_reply("x" * 400, True)[0], "voice")

    def test_strip_for_speech(self):
        self.assertEqual(strip_for_speech("**Run** _now_,\n`5km`."), "Run now, 5km.")


if __name__ == "__main__":
    unittest.main()
