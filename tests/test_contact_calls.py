"""Whitelisted-contact calls (7 Aug): 'call John' rings John Debono for a
friendly catch-up. The whitelist is code, not chat; the guest conversation
runs a scoped persona behind a hard privacy wall — never Paul's brain, tools
or memory; outcomes are reported honestly (answered / no answer / failed)."""
import os
import tempfile
import unittest

from app.voice.contacts import (
    CONTACTS, find_contact, guest_system_prompt, normalise_number, number_matches,
    pick_greeting,
)
from app.voice.phone import PhoneChannel
from app.db.sqlite import SqliteDatabase
from tests.test_phone import OWNER, FakeTTS, PhoneHarness
from tests.test_telegram_client import text_update

JOHN = CONTACTS[0]


class FakeTwilio:
    """kwargs-tolerant fake — guest calls pass status_callback."""

    def __init__(self, sid="CA1"):
        self.sid = sid
        self.calls = []

    async def place_call(self, to, from_, url, status_callback=""):
        self.calls.append({"to": to, "from": from_, "url": url, "status": status_callback})
        return self.sid


def make_channel(twilio=None, tts=None) -> PhoneChannel:
    return PhoneChannel(
        twilio if twilio is not None else FakeTwilio(),
        tts if tts is not None else FakeTTS(),
        from_number="+18574206042",
        paul_number="+447498847149",
        public_url="https://j.example",
        secret="SECRET",
    )


# ------------------------------------------------------------ the whitelist


class TestWhitelist(unittest.TestCase):
    def test_john_is_seeded_with_his_number(self):
        self.assertEqual(JOHN["name"], "John Debono")
        self.assertEqual(normalise_number(JOHN["number"]), "447881901402")

    def test_find_contact_matches_aliases_word_bounded(self):
        self.assertIsNotNone(find_contact("john debono"))
        self.assertIsNotNone(find_contact("give john a bell"))
        self.assertIsNone(find_contact("johnson from accounts"))
        self.assertIsNone(find_contact("dave"))

    def test_number_matching_is_format_tolerant(self):
        for raw in ("+44 7881 901402", "07881901402", "447881901402", "0044 7881 901402"):
            self.assertTrue(number_matches(JOHN, raw), raw)
        self.assertFalse(number_matches(JOHN, "+44 1111 111111"))

    def test_greeting_identifies_jarvis_and_paul(self):
        for _ in range(10):
            greeting = pick_greeting(JOHN)
            self.assertIn("Jarvis", greeting)
            self.assertIn("Paul", greeting)

    def test_guest_prompt_carries_the_wall_and_the_colour(self):
        prompt = guest_system_prompt(JOHN)
        self.assertIn("NEVER share", prompt)
        self.assertIn("no tools", prompt.lower())
        self.assertIn("Sasha", prompt)       # the personal colour rides along
        self.assertIn("Northampton", prompt)
        self.assertIn("[[bye]]", prompt)


# ------------------------------------------------------------ the channel


class TestGuestCalls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.twilio = FakeTwilio()
        self.tts = FakeTTS()
        self.channel = make_channel(twilio=self.twilio, tts=self.tts)
        self.events = []

        async def on_event(event, contact, detail=""):
            self.events.append((event, contact["key"], detail))

        self.channel.on_guest_event = on_event

    async def _place(self) -> str:
        sid = await self.channel.call_contact(JOHN, "John! Jarvis here, me old chum.")
        self.assertEqual(sid, "CA1")
        return sid

    async def test_call_contact_dials_john_with_status_callback(self):
        await self._place()
        [call] = self.twilio.calls
        self.assertEqual(call["to"], "+447881901402")
        self.assertIn("/twilio/voice/SECRET/status", call["status"])

    async def test_answer_greets_and_reports(self):
        sid = await self._place()
        twiml = await self.channel.handle_answer({"CallSid": sid, "Direction": "outbound-api"})
        self.assertIn("<Gather", twiml)
        self.assertEqual(self.tts.texts[0], "John! Jarvis here, me old chum.")
        self.assertEqual(self.events[0][0], "answered")
        # Silence fallback signs off warmly to JOHN, never 'Speak soon, sir'.
        self.assertIn("Cheers John", twiml)
        self.assertNotIn("Speak soon, sir", twiml)

    async def test_guest_turn_never_touches_pauls_brain(self):
        sid = await self._place()
        await self.channel.handle_answer({"CallSid": sid})

        async def pauls_brain(speech):
            raise AssertionError("a third party reached Paul's brain — the wall is broken")

        async def guest_brain(contact_key, speech, call_sid):
            self.assertEqual(contact_key, "john")
            self.assertEqual(call_sid, sid)
            return "Pip's doing well? Glad to hear it."

        self.channel.brain = pauls_brain
        self.channel.guest_brain = guest_brain
        twiml = await self.channel.handle_turn(
            {"CallSid": sid, "SpeechResult": "Pip's doing great thanks"}
        )
        self.assertIn("<Gather", twiml)
        self.assertIn("Pip's doing well? Glad to hear it.", self.tts.texts)

    async def test_goodbye_ends_with_guest_farewell(self):
        sid = await self._place()
        await self.channel.handle_answer({"CallSid": sid})
        twiml = await self.channel.handle_turn(
            {"CallSid": sid, "SpeechResult": "lovely to chat, goodbye"}
        )
        self.assertIn("<Hangup/>", twiml)
        self.assertIn("Cheers John", self.tts.texts[-1])
        self.assertIn(("ended", "john", "they said goodbye"), self.events)

    async def test_guest_brain_bye_prefix_hangs_up(self):
        sid = await self._place()
        await self.channel.handle_answer({"CallSid": sid})

        async def guest_brain(contact_key, speech, call_sid):
            return "[[bye]]Take care John — I'll pass that along to Paul."

        self.channel.guest_brain = guest_brain
        twiml = await self.channel.handle_turn({"CallSid": sid, "SpeechResult": "right, better go"})
        self.assertIn("<Hangup/>", twiml)
        self.assertIn(("ended", "john", "wound up naturally"), self.events)

    async def test_no_guest_brain_winds_up_honestly(self):
        sid = await self._place()
        await self.channel.handle_answer({"CallSid": sid})
        twiml = await self.channel.handle_turn({"CallSid": sid, "SpeechResult": "so how's Paul?"})
        self.assertIn("<Hangup/>", twiml)   # never wings it with a third party
        self.assertTrue(any(e[0] == "ended" for e in self.events))

    async def test_status_no_answer_is_reported(self):
        sid = await self._place()
        await self.channel.handle_status({"CallSid": sid, "CallStatus": "no-answer"})
        self.assertEqual(self.events, [("no-answer", "john", "")])
        # Record is gone — a late duplicate status stays silent.
        await self.channel.handle_status({"CallSid": sid, "CallStatus": "no-answer"})
        self.assertEqual(len(self.events), 1)

    async def test_completed_without_a_single_turn_counts_as_no_answer(self):
        sid = await self._place()
        await self.channel.handle_status({"CallSid": sid, "CallStatus": "completed"})
        self.assertEqual(self.events[0][0], "no-answer")

    async def test_completed_after_answer_stays_quiet(self):
        sid = await self._place()
        await self.channel.handle_answer({"CallSid": sid})
        self.events.clear()
        await self.channel.handle_status({"CallSid": sid, "CallStatus": "completed"})
        self.assertEqual(self.events, [])   # the goodbye turn already told Paul


# ------------------------------------------------------------ the router lane


class TestCallJohnLane(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = PhoneHarness(self.db)
        self.twilio = FakeTwilio()
        self.channel = make_channel(twilio=self.twilio)
        self.h.router.phone_channel = self.channel

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_call_john_debono_dials(self):
        await self.h.router.handle_update(text_update("call John Debono", OWNER))
        [call] = self.twilio.calls
        self.assertEqual(call["to"], "+447881901402")
        [ack] = self.h.texts()
        self.assertIn("Ringing John Debono", ack)
        self.assertIn("+447881901402", ack)

    async def test_the_spec_test_case_with_number_dials(self):
        await self.h.router.handle_update(text_update(
            "Jarvis call John on this number using twillio +44 7881 901402", OWNER
        ))
        [call] = self.twilio.calls
        self.assertEqual(call["to"], "+447881901402")

    async def test_wrong_number_refuses_to_dial(self):
        await self.h.router.handle_update(
            text_update("call John on +44 1111 111111", OWNER)
        )
        self.assertEqual(self.twilio.calls, [])
        [ack] = self.h.texts()
        self.assertIn("doesn't match", ack)

    async def test_unlisted_name_gets_an_honest_refusal(self):
        await self.h.router.handle_update(text_update("call Dave", OWNER))
        self.assertEqual(self.twilio.calls, [])
        [ack] = self.h.texts()
        self.assertIn("registered contacts", ack)
        self.assertIn("John Debono", ack)

    async def test_conversational_call_phrases_stay_conversation(self):
        for phrase in ("call the dentist about my teeth", "call it a day I think"):
            await self.h.router.handle_update(text_update(phrase, OWNER))
        self.assertEqual(self.twilio.calls, [])
        for ack in self.h.texts():
            self.assertNotIn("registered contacts", ack)

    async def test_call_me_still_rings_paul_not_a_contact(self):
        await self.h.router.handle_update(text_update("call me", OWNER))
        [call] = self.twilio.calls
        self.assertEqual(call["to"], "+447498847149")   # Paul, whitelist untouched

    async def test_tell_him_message_rides_the_greeting(self):
        await self.h.router.handle_update(text_update(
            "call John and tell him the villa keys are with reception", OWNER
        ))
        [call] = self.twilio.calls
        record = self.channel._guest_calls[self.twilio.sid]
        self.assertIn("villa keys are with reception", record["greeting"])

    async def test_unconfigured_line_is_honest(self):
        self.h.router.phone_channel = None
        await self.h.router.handle_update(text_update("call John Debono", OWNER))
        [ack] = self.h.texts()
        self.assertIn("No phone line wired up yet", ack)


# ------------------------------------------------------------ the guest brain


class TestContactTurn(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = PhoneHarness(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_scoped_prompt_no_tools_and_history_grows(self):
        reply = await self.h.router.contact_turn("john", "How's Paul keeping?", "CA9")
        self.assertEqual(reply, "Noted.")   # harness's canned Claude line
        [request] = self.h.claude_requests
        self.assertIn("NEVER share", request["system"])
        self.assertIn("Sasha", request["system"])
        self.assertNotIn("tools", request)   # conversation only, by construction
        second = await self.h.router.contact_turn("john", "And the family?", "CA9")
        self.assertEqual(second, "Noted.")
        # Second turn carries the first exchange as history.
        self.assertEqual(len(self.h.claude_requests[1]["messages"]), 3)

    async def test_unknown_contact_key_returns_empty(self):
        self.assertEqual(await self.h.router.contact_turn("stranger", "hi", "CA9"), "")
        self.assertEqual(self.h.claude_requests, [])

    async def test_guest_turns_are_logged_for_pauls_review(self):
        await self.h.router.contact_turn("john", "How's Paul keeping?", "CA9")
        rows = await self.db.fetch_all(
            "SELECT direction, transcript, channel FROM messages ORDER BY id"
        )
        channels = {row["channel"] for row in rows}
        self.assertIn("phone_guest", channels)

    async def test_outcome_events_reach_pauls_telegram(self):
        await self.h.router.guest_call_event("no-answer", JOHN)
        await self.h.router.guest_call_event("answered", JOHN)
        await self.h.router.guest_call_event("ended", JOHN, "they said goodbye")
        texts = self.h.texts()
        self.assertIn("John didn't pick up — no answer. I'll leave it with you.", texts)
        self.assertTrue(any("picked up" in t for t in texts))
        self.assertTrue(any("Wrapped up" in t and "goodbye" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
