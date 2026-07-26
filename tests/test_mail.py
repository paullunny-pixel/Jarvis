"""Phase 2 email tests: multi-inbox triage over fake IMAP, the draft →
confirm → send safety flow, voice-command parsing, and the router wiring.
No real network anywhere — IMAP/SMTP are injected fakes, Claude is mocked
at the HTTP layer like every other client."""
import json
import os
import tempfile
import unittest
from email.mime.text import MIMEText

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.db.sqlite import SqliteDatabase
from app.mail import commands
from app.mail.client import MailAccount, MailClient, label_for
from app.mail.service import DRAFT_KEY, MailService
from tests.test_router import OWNER, RouterHarness
from tests.test_telegram_client import text_update


def raw_email(from_addr: str, subject: str, body: str, from_name: str = "") -> bytes:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["Subject"] = subject
    msg["Date"] = "Fri, 24 Jul 2026 10:00:00 +0000"
    return msg.as_bytes()


class FakeIMAP:
    """Just enough of imaplib for MailClient: login/select/search/fetch."""

    def __init__(self, messages: list[bytes], fail_login: bool = False):
        self._messages = messages
        self._fail = fail_login

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        if self._fail:
            raise RuntimeError("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", []

    def select(self, mailbox, readonly=False):
        return "OK", []

    def search(self, charset, criterion):
        ids = b" ".join(str(i + 1).encode() for i in range(len(self._messages)))
        return "OK", [ids]

    def fetch(self, msg_id, spec):
        raw = self._messages[int(msg_id) - 1]
        return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]


class FakeSMTP:
    """Records sends into a shared list instead of talking to Google."""

    def __init__(self, sent: list, fail: bool = False):
        self._sent = sent
        self._fail = fail
        self._user = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        self._user = user

    def sendmail(self, from_addr, to_addrs, msg):
        if self._fail:
            raise RuntimeError("smtp down")
        self._sent.append({"from": from_addr, "to": to_addrs, "raw": msg})


INBOXES = {
    "paullunny@gmail.com": [
        raw_email("mate@example.com", "Padel Saturday?", "You in for 10am?", "Kenny"),
        raw_email("noreply@bank.com", "Statement ready", "Your statement is ready."),
    ],
    "info@dermadirect.co.uk": [
        raw_email("harry@dermadirect.co.uk", "Website QA list", "Found three bugs on checkout.", "Harry"),
    ],
    "info@prodermis.com": [],
}


def build_service(db, sent: list, fail_login: set | None = None) -> MailService:
    clients = []
    for address, messages in INBOXES.items():
        account = MailAccount(address, "app-pass")
        fail = fail_login is not None and address in fail_login
        clients.append(
            MailClient(
                account,
                imap_factory=lambda m=messages, f=fail: FakeIMAP(m, fail_login=f),
                smtp_factory=lambda: FakeSMTP(sent),
            )
        )
    return MailService(clients, db)


class TestLabels(unittest.TestCase):
    def test_known_domains(self):
        self.assertEqual(label_for("paullunny@gmail.com"), "Personal")
        self.assertEqual(label_for("info@dermadirect.co.uk"), "Derma Direct")
        self.assertEqual(label_for("info@prodermis.com"), "Prodermis")
        self.assertEqual(label_for("x@aestheticssupply.com"), "Aestheticssupply")


class TestMailService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.sent: list = []
        self.service = build_service(self.db, self.sent)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_overview_covers_every_inbox(self):
        text = await self.service.overview()
        self.assertIn("Personal: 2 unread", text)
        self.assertIn("Derma Direct: 1 unread", text)
        self.assertIn("Prodermis: 0 unread", text)
        self.assertIn("Padel Saturday?", text)

    async def test_match_by_hint(self):
        self.assertEqual(self.service.match("prodermis")[0].account.address, "info@prodermis.com")
        self.assertEqual(self.service.match("the derma inbox")[0].account.label, "Derma Direct")
        self.assertEqual(self.service.match("personal")[0].account.address, "paullunny@gmail.com")
        self.assertEqual(len(self.service.match("all")), 3)
        self.assertEqual(len(self.service.match("")), 3)

    async def test_read_single_account(self):
        text = await self.service.read("derma", limit=5)
        self.assertIn("Website QA list", text)
        self.assertNotIn("Padel", text)

    async def test_one_broken_inbox_degrades_gracefully(self):
        service = build_service(self.db, self.sent, fail_login={"info@prodermis.com"})
        text = await service.overview()
        self.assertIn("Personal: 2 unread", text)
        self.assertIn("Prodermis: couldn't reach it just now", text)

    async def test_reply_draft_then_confirmed_send(self):
        await self.service.read("derma")  # Paul was shown Harry's email (index 1)
        readback = await self.service.draft("On it — fixes by Friday.\n\nPaul", reply_index=1)
        self.assertIn("harry@dermadirect.co.uk", readback)
        self.assertIn("Re: Website QA list", readback)
        self.assertIn("send it", readback.lower())
        self.assertEqual(self.sent, [])  # NOTHING sent yet — the invariant

        result = await self.service.send_pending()
        self.assertIn("Sent", result)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["from"], "info@dermadirect.co.uk")  # right account
        self.assertEqual(self.sent[0]["to"], ["harry@dermadirect.co.uk"])
        # Draft is consumed — a second "send it" has nothing to fire.
        self.assertIn("no draft", (await self.service.send_pending()).lower())

    async def test_draft_needs_a_recipient(self):
        result = await self.service.draft("Hello there")
        self.assertIn("Who's it going to", result)
        self.assertIsNone(await self.service.pending_draft())

    async def test_cancel_scraps_without_sending(self):
        await self.service.draft("Test", to="x@example.com", subject="T")
        self.assertIn("Scrapped", await self.service.cancel_draft())
        self.assertIsNone(await self.service.pending_draft())
        self.assertEqual(self.sent, [])

    async def test_stale_draft_never_sends(self):
        await self.service.draft("Old words", to="x@example.com", subject="Old")
        # Age the draft past the TTL by rewriting its timestamp.
        raw = json.loads(await self.service._settings.get(DRAFT_KEY))
        raw["created"] = "2026-07-20T09:00:00+00:00"
        await self.service._settings.set(DRAFT_KEY, json.dumps(raw))
        self.assertIsNone(await self.service.pending_draft())
        self.assertIn("no draft", (await self.service.send_pending()).lower())
        self.assertEqual(self.sent, [])

    async def test_smtp_failure_keeps_the_draft(self):
        service = build_service(self.db, self.sent)
        for client in service._clients:
            client._smtp = lambda: FakeSMTP(self.sent, fail=True)
        await service.draft("Hi", to="x@example.com", subject="T")
        result = await service.send_pending()
        self.assertIn("still here", result)
        self.assertIsNotNone(await service.pending_draft())  # retryable

    async def test_health_reports_per_account_truth(self):
        service = build_service(self.db, self.sent, fail_login={"info@prodermis.com"})
        checks = await service.health()
        by_label = {c["label"]: c for c in checks}
        self.assertTrue(by_label["Personal"]["ok"])
        self.assertFalse(by_label["Prodermis"]["ok"])
        self.assertIn("AUTHENTICATIONFAILED", by_label["Prodermis"]["error"])

    async def test_brief_line(self):
        line = await self.service.brief_line()
        self.assertIn("INBOXES", line)
        self.assertIn("Personal 2", line)
        self.assertIn("Prodermis 0", line)


class TestCommandGates(unittest.TestCase):
    def test_email_hint(self):
        for phrase in (
            "check my email",
            "anything in the inbox?",
            "how are the inboxes looking",
            "reply to Alicia and tell her it's shipped",
            "any unread from Harry?",
            "draft an email to the notary",
        ):
            self.assertTrue(commands.mentions_email(phrase), phrase)
        for phrase in (
            "morning, how are we",
            "log my run",
            "put a card on Kiefer for the VAT return",
            "what should I eat before the gym",
        ):
            self.assertFalse(commands.mentions_email(phrase), phrase)

    def test_confirm_send_is_strict(self):
        for phrase in ("send it", "Send it.", "yes, send it", "fire it off", "send the email"):
            self.assertTrue(commands.confirms_send(phrase), phrase)
        for phrase in (
            "don't send it",
            "send it to Kiefer on Trello",   # task talk, not the email confirm
            "should I send it?",
            "scrap it",
        ):
            self.assertFalse(commands.confirms_send(phrase), phrase)
        self.assertTrue(commands.cancels_send("scrap it"))
        self.assertTrue(commands.cancels_send("don't send that"))


class TestCommandParsing(unittest.IsolatedAsyncioTestCase):
    async def test_parse_actions_via_haiku(self):
        def handler(request: httpx.Request) -> httpx.Response:
            actions = [{"action": "check", "account": "all"}]
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": json.dumps(actions)}]}
            )

        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        actions = await commands.parse_actions(
            claude, "how's my email looking", ["Personal"], []
        )
        self.assertEqual(actions, [{"action": "check", "account": "all"}])


class TestRouterEmailFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.sent: list = []

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_check_email_is_answered_from_the_inboxes(self):
        h = RouterHarness(
            self.db,
            claude_json={
                "content": [{"type": "text", "text": '[{"action":"check","account":"all"}]'}]
            },
        )
        h.router.mail = build_service(self.db, self.sent)
        await h.router.handle_update(text_update("check my email", OWNER))
        self.assertIn("sendMessage", h.methods())
        from urllib.parse import unquote_plus

        payload = unquote_plus(dict(h.telegram_calls)["sendMessage"].decode())
        self.assertIn("Personal: 2 unread", payload)

    async def test_send_it_without_a_draft_never_reaches_email(self):
        h = RouterHarness(self.db)  # ordinary conversation mock
        h.router.mail = build_service(self.db, self.sent)
        await h.router.handle_update(text_update("send it", OWNER))
        self.assertEqual(self.sent, [])  # nothing pending → nothing sent


if __name__ == "__main__":
    unittest.main()
