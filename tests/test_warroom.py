"""The War Room (7 Aug build slice): three vendors, two tiers, blind Phase 1,
mandatory dissent, redaction, Quick Board card creation, archive. No real
network anywhere — all three vendor APIs mocked at the HTTP layer."""
import json
import os
import tempfile
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.gemini_client import GeminiClient
from app.clients.openai_client import OpenAIClient
from app.config import Settings
from app.core.store import SettingsStore
from app.db.sqlite import SqliteDatabase
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder
from app.memory.store import LivingFacts, MemoryStore
from app.warroom import redaction
from app.warroom.protocol import call_seat, looks_like_agreement, phase1_system, phase2_system
from app.warroom.seats import Seat, build_seats, rotated_roles
from app.warroom.service import WarRoom
from tests.test_trello_layer import HARRY, MASTER
from tests.test_trello_layer import board_fixture as _base_board_fixture


def _warroom_board_fixture(board_id):
    """The shared Trello test fixture has no 'Brain Dump' list — every
    filing path in this codebase uses one, so the War Room needs it too."""
    data = _base_board_fixture(board_id)
    if board_id == MASTER:
        data = dict(data)
        data["lists"] = data["lists"] + [{"id": f"{board_id}-braindump", "name": "Brain Dump"}]
    return data


class WarroomTrelloHarness:
    def __init__(self):
        self.requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path
            for board_id in (MASTER, HARRY):
                for part in ("lists", "customFields", "labels", "members"):
                    if path == f"/1/boards/{board_id}/{part}":
                        return httpx.Response(200, json=_warroom_board_fixture(board_id)[part])
            if path == "/1/cards" and request.method == "POST":
                return httpx.Response(200, json={"id": "68919f2f" + "a" * 16, "name": "x"})
            if "/actions" in path:
                return httpx.Response(200, json=[
                    {"date": "2026-08-05T10:00:00.000Z", "data": {"listAfter": {"name": "Paul Today"}}},
                ])
            if "/checklists" in path and request.method == "POST":
                return httpx.Response(200, json={"id": "CHK1"})
            return httpx.Response(200, json={"ok": True})

        from app.daily12.trello import TrelloClient
        from app.daily12.trello_layer import TrelloLayer

        self.layer = TrelloLayer(TrelloClient("K", "T", transport=httpx.MockTransport(handler)))


# ------------------------------------------------------------------ mock vendors

def anthropic_mock(routes: list[tuple[str, dict | str]]) -> ClaudeClient:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        system = body.get("system", "")
        for marker, resp in routes:
            if marker in system:
                text = resp if isinstance(resp, str) else json.dumps(resp)
                return httpx.Response(200, json={
                    "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                })
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    client = ClaudeClient("K", transport=httpx.MockTransport(handler))
    client.calls = calls   # type: ignore[attr-defined]
    return client


def openai_mock(routes: list[tuple[str, dict | str]], missing_models: frozenset = frozenset()) -> OpenAIClient:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("model") in missing_models:
            return httpx.Response(404, text="model not found")
        system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
        for marker, resp in routes:
            if marker in system:
                text = resp if isinstance(resp, str) else json.dumps(resp)
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    client = OpenAIClient("K", transport=httpx.MockTransport(handler))
    client.calls = calls   # type: ignore[attr-defined]
    return client


def gemini_mock(routes: list[tuple[str, dict | str]], missing_models: frozenset = frozenset()) -> GeminiClient:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        if model in missing_models:
            return httpx.Response(404, text="model not found")
        body = json.loads(request.content)
        calls.append(body)
        system = (body.get("systemInstruction") or {}).get("parts", [{}])[0].get("text", "")
        for marker, resp in routes:
            if marker in system:
                text = resp if isinstance(resp, str) else json.dumps(resp)
                return httpx.Response(200, json={
                    "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
                })
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        })

    client = GeminiClient("K", transport=httpx.MockTransport(handler))
    client.calls = calls   # type: ignore[attr-defined]
    return client


PHASE1_MARKER = "one of three independent seats"
PHASE2_MARKER = "Phase 1 answers verbatim"
DISSENT_MARKER = "push harder"
PHASE3_MARKER = "ONE rebuttal round"
PHASE4_MARKER = "calling time"
CHAIR_MARKER = "You write structured JSON reports"

PHASE1_A = {"assessment": "UNIQUE_A_ASSESSMENT", "recommendation": "Do the thing", "reasoning": "because",
            "confidence": "high", "need_to_know": "more data"}
PHASE1_B = {"assessment": "UNIQUE_B_ASSESSMENT", "recommendation": "Don't", "reasoning": "risk",
            "confidence": "medium", "need_to_know": "budget"}
PHASE1_C = {"assessment": "UNIQUE_C_ASSESSMENT", "recommendation": "Test first", "reasoning": "reality check",
            "confidence": "low", "need_to_know": "timeline"}
PHASE2_DISAGREE = {"missed_point": "the cash timing", "disagreement": "I think the rollout is too fast because cash is tight",
                    "collective_gap": "nobody checked stock levels", "final_position": ""}
PHASE2_AGREE = {"missed_point": "fine point", "disagreement": "I agree with everything", "collective_gap": "", "final_position": ""}
PHASE3_RESP = {"rebuttal": "Having seen the others, I still think we should slow down."}
PHASE4_RESP = {"final_recommendation": "Go, but slower", "confidence": "medium",
               "would_change_mind": "a cash injection", "must_not_get_wrong": "the BMI deadline"}
CHAIR_RESP = {
    "verdict": "Go ahead, phased.", "recommendation": "Do it in two stages.",
    "action_points": [
        {"title": "Send Harry the pipeline doc", "why": "unblocks the order", "owner_suggestion": "Harry", "due": "Friday"},
        {"title": "Confirm stock levels", "why": "avoid overselling", "owner_suggestion": "", "due": ""},
    ],
    "disagreement": "Seat B worried about cash timing; hinges on the next BMI payment date.",
    "discarded": ["doing it all at once"],
    "gaps": "nobody checked competitor pricing",
    "confidence": {"level": "medium", "would_change": "a firm BMI payment date"},
    "jarvis_note": "Paul's attention is thin this month — keep this to two cards.",
    "measurable": "",
}


def full_routes_no_dissent_needed():
    return {
        "anthropic": [
            (PHASE1_MARKER, PHASE1_A), (PHASE2_MARKER, PHASE2_DISAGREE),
            (PHASE3_MARKER, PHASE3_RESP), (PHASE4_MARKER, PHASE4_RESP),
            (CHAIR_MARKER, CHAIR_RESP),
        ],
        "openai": [(PHASE1_MARKER, PHASE1_B), (PHASE2_MARKER, PHASE2_DISAGREE),
                   (PHASE3_MARKER, PHASE3_RESP), (PHASE4_MARKER, PHASE4_RESP)],
        "google": [(PHASE1_MARKER, PHASE1_C), (PHASE2_MARKER, PHASE2_DISAGREE),
                   (PHASE3_MARKER, PHASE3_RESP), (PHASE4_MARKER, PHASE4_RESP)],
    }


def build_warroom(db, routes, company_names=None, **kwargs) -> WarRoom:
    claude = anthropic_mock(routes["anthropic"])
    openai = openai_mock(routes["openai"])
    gemini = gemini_mock(routes["google"])
    store = SettingsStore(db)
    memory = MemoryStore(db, HashEmbedder(), PrivateBox("k"))
    living = LivingFacts(db)
    settings = Settings(telegram_bot_token="TOK", _env_file=None)
    wr = WarRoom(
        db, claude, store, memory, living, kwargs.pop("layer_factory", None), settings,
        openai_client=openai, gemini_client=gemini, company_names=company_names or ["Prodermis", "Derma"],
        **kwargs,
    )
    wr._debug_claude = claude
    wr._debug_openai = openai
    wr._debug_gemini = gemini
    return wr


class WarRoomBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.trello = WarroomTrelloHarness()
        await self.trello.layer.bootstrap()

        async def layer_factory():
            return self.trello.layer

        self.layer_factory = layer_factory

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()


class TestSeats(unittest.TestCase):
    def test_rotation_shifts_roles_each_session(self):
        self.assertEqual(rotated_roles(0), {"A": "strategist", "B": "sceptic", "C": "operator"})
        self.assertEqual(rotated_roles(1), {"A": "sceptic", "B": "operator", "C": "strategist"})
        self.assertEqual(rotated_roles(3), rotated_roles(0))   # cycles every 3

    def test_build_seats_full_uses_full_models(self):
        settings = Settings(telegram_bot_token="TOK", _env_file=None)
        seats = build_seats("full", settings, 0)
        by_key = {s.key: s for s in seats}
        self.assertEqual(by_key["A"].model, "claude-fable-5")
        self.assertEqual(by_key["B"].model, "gpt-5.6-sol")
        self.assertEqual(by_key["B"].effort, "ultra")
        self.assertEqual(by_key["C"].model, "gemini-3.1-pro-preview")
        self.assertEqual(by_key["C"].effort, "")

    def test_build_seats_quick_uses_quick_models_no_effort(self):
        settings = Settings(telegram_bot_token="TOK", _env_file=None)
        seats = build_seats("quick", settings, 0)
        by_key = {s.key: s for s in seats}
        self.assertEqual(by_key["A"].model, "claude-sonnet-5")
        self.assertEqual(by_key["B"].model, "gpt-5.6-terra")
        self.assertEqual(by_key["B"].effort, "")


class TestPhase2Blindness(unittest.TestCase):
    def test_phase2_prompt_excludes_own_seat_includes_others(self):
        seats = build_seats("quick", Settings(telegram_bot_token="TOK", _env_file=None), 0)
        seat_a = next(s for s in seats if s.key == "A")
        records = [
            {"key": "A", "role": "strategist", "parsed": PHASE1_A},
            {"key": "B", "role": "sceptic", "parsed": PHASE1_B},
            {"key": "C", "role": "operator", "parsed": PHASE1_C},
        ]
        others = [r for r in records if r["key"] != "A"]
        prompt = phase2_system(seat_a, others, quick=True)
        self.assertNotIn("UNIQUE_A_ASSESSMENT", prompt)
        self.assertIn("UNIQUE_B_ASSESSMENT", prompt)
        self.assertIn("UNIQUE_C_ASSESSMENT", prompt)

    def test_phase1_prompt_never_references_other_seats(self):
        seats = build_seats("quick", Settings(telegram_bot_token="TOK", _env_file=None), 0)
        prompt = phase1_system(seats[0], "some business context")
        self.assertNotIn("SEAT B", prompt)
        self.assertNotIn("SEAT C", prompt)


class TestMandatoryDissent(unittest.TestCase):
    def test_agreement_is_detected(self):
        self.assertTrue(looks_like_agreement(PHASE2_AGREE))
        self.assertTrue(looks_like_agreement({"disagreement": ""}))
        self.assertTrue(looks_like_agreement({"disagreement": "no disagreement"}))

    def test_real_disagreement_is_not_flagged(self):
        self.assertFalse(looks_like_agreement(PHASE2_DISAGREE))


class TestCallSeat(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_is_reported_as_data_not_an_exception(self):
        def handler(request):
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": ""}], "stop_reason": "refusal",
                "usage": {"input_tokens": 5, "output_tokens": 0},
            })
        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        seat = Seat(key="A", vendor="anthropic", model="claude-fable-5", role="strategist")
        res = await call_seat({}, claude, seat, "system", "prompt", 400)
        self.assertTrue(res["refused"])
        self.assertEqual(res["error"], "")

    async def test_model_missing_falls_back_and_flags(self):
        calls = []

        def handler(request):
            model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
            calls.append(model)
            if model == "gemini-3.1-pro-preview":
                return httpx.Response(404, text="gone")
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": '{"assessment":"ok"}'}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            })

        gemini = GeminiClient("K", transport=httpx.MockTransport(handler))
        seat = Seat(key="C", vendor="google", model="gemini-3.1-pro-preview", role="operator")
        res = await call_seat({"google": gemini}, None, seat, "sys", "prompt", 400)
        self.assertEqual(res["fallback_from"], "gemini-3.1-pro-preview")
        self.assertEqual(seat.model, "gemini-3.6-flash")   # mutated in place
        self.assertEqual(calls, ["gemini-3.1-pro-preview", "gemini-3.6-flash"])
        self.assertEqual(res["parsed"]["assessment"], "ok")

    async def test_word_cap_trims_values_without_breaking_json(self):
        long_text = " ".join(["word"] * 500)

        def handler(request):
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": json.dumps({"assessment": long_text})}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 500},
            })
        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        seat = Seat(key="A", vendor="anthropic", model="claude-fable-5", role="strategist")
        res = await call_seat({}, claude, seat, "system", "prompt", 50)
        self.assertIn("assessment", res["parsed"])
        self.assertLess(len(res["parsed"]["assessment"].split()), 60)


class TestRedaction(unittest.IsolatedAsyncioTestCase):
    def test_regex_backstop_strips_bank_and_card_details(self):
        text = "Sort code 12-34-56, account 12345678, VAT GB123456789, call 07700 900123"
        out = redaction.regex_backstop(text)
        self.assertNotIn("12-34-56", out)
        self.assertNotIn("12345678", out)
        self.assertNotIn("GB123456789", out)
        self.assertNotIn("07700 900123", out)

    async def test_llm_pass_keeps_financials_strips_named_salary(self):
        def handler(request):
            return httpx.Response(200, json={"content": [{"type": "text",
                "text": "Revenue £2.1m, margin 34%. Warehouse manager, £48k."}]})
        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        out = await redaction.redact(claude, "Revenue £2.1m, margin 34%. Harry Smith, £48k.")
        self.assertIn("£2.1m", out)
        self.assertIn("34%", out)
        self.assertNotIn("Harry Smith", out)

    async def test_skip_llm_pass_still_runs_regex_backstop(self):
        claude = ClaudeClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        out = await redaction.redact(claude, "account 12345678", skip_llm_pass=True)
        self.assertNotIn("12345678", out)


class TestQuickBoardEndToEnd(WarRoomBase):
    async def test_blind_dissent_consensus_and_archive(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        framed = await wr.frame("Should we run a £500 Google Ads push for the new SKU?")
        self.assertEqual(framed["tier"], "quick")
        result = await wr.confirm_and_run()

        self.assertFalse(result["consensus_weak"])
        self.assertGreater(result["cost_usd"], 0)
        report = result["report"]
        self.assertEqual(report["verdict"], "Go ahead, phased.")
        self.assertEqual(len(report["action_points"]), 2)

        # Layout parity — every one of the nine section keys present, even
        # though Phase 3/4 never ran on the Quick tier.
        from app.warroom.report import layout_parity_headings
        session = await wr.get_session(result["session_id"])
        for key in layout_parity_headings():
            if key == "transcript":
                continue
            self.assertIn(key, session["report"])

        # Archived and retrievable, and fed back into the second brain.
        hits = await wr.search_archive("Google Ads")
        self.assertEqual(len(hits), 1)
        chunks = await wr.memory.search("Google Ads push")
        self.assertTrue(any("Go ahead, phased" in c["content"] for c in chunks))

    async def test_mandatory_dissent_reprompts_an_agreeable_seat(self):
        routes = full_routes_no_dissent_needed()
        # Seat B (OpenAI) agrees on the first try — must be re-prompted.
        routes["openai"] = [
            (DISSENT_MARKER, PHASE2_DISAGREE),   # the re-prompt gets a real answer
            (PHASE1_MARKER, PHASE1_B),
            (PHASE2_MARKER, PHASE2_AGREE),        # first attempt: agreement, rejected
        ]
        wr = build_warroom(self.db, routes, layer_factory=self.layer_factory)
        await wr.frame("Quick board the Google Ads campaign")
        result = await wr.confirm_and_run()
        self.assertFalse(result["consensus_weak"])   # the re-prompt saved it
        # Two Phase 2 calls happened for Seat B: first agree, then re-prompted.
        phase2_calls = [c for c in wr._debug_openai.calls if PHASE2_MARKER in
                        next((m["content"] for m in c["messages"] if m["role"] == "system"), "")]
        self.assertEqual(len(phase2_calls), 2)

    async def test_true_consensus_is_flagged_weak(self):
        routes = full_routes_no_dissent_needed()
        for vendor in ("anthropic", "openai", "google"):
            routes[vendor] = [(m, r) for m, r in routes[vendor] if m not in (PHASE2_MARKER, DISSENT_MARKER)]
            routes[vendor].insert(0, (PHASE2_MARKER, PHASE2_AGREE))
            routes[vendor].insert(0, (DISSENT_MARKER, PHASE2_AGREE))   # still agrees even after the push
        wr = build_warroom(self.db, routes, layer_factory=self.layer_factory)
        await wr.frame("Quick board a landing page test")
        result = await wr.confirm_and_run()
        self.assertTrue(result["consensus_weak"])

    async def test_project_preview_sequencing_assignment_and_undo(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        await wr.frame("Quick board the BMI packaging project")
        result = await wr.confirm_and_run()
        session_id = result["session_id"]

        cards = await wr.preview_project(session_id)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["list_name"], "Paul Today")     # only the first action
        self.assertEqual(cards[1]["list_name"], "Brain Dump")     # the rest queue up
        self.assertEqual(cards[0]["owner"], "Harry")
        # Harry isn't a real member on this fixture's board — warned at preview,
        # never silently ownerless (§7a).
        self.assertIn("isn't on this board", cards[0]["warning"])
        self.assertEqual(cards[1]["owner"], "Paul")                # default when nothing suggested

        created = await wr.create_project(session_id)
        self.assertEqual(len(created["created"]), 2)
        self.assertTrue(any("Harry" in w for w in created["warnings"]))

        watch_rows = await self.db.fetch_all("SELECT * FROM warroom_watch WHERE session_id = ?", (session_id,))
        self.assertEqual(len(watch_rows), 2)   # auto-registered, no extra step (§7b)

        undo_msg = await wr.undo()
        self.assertIn("Undone", undo_msg)

    async def test_full_board_cannot_move_or_delete_existing_cards(self):
        """The Quick/Full Board only ever CREATES — never touches an
        existing card without confirmation (§3 guardrail)."""
        import inspect

        from app.warroom import trello_plan
        source = inspect.getsource(trello_plan)
        self.assertNotIn("move_card", source)
        self.assertNotIn("archive_card", source.split("undo_last_project")[0])   # only undo archives, and only its own


class TestMissingMember(WarRoomBase):
    async def test_unknown_owner_warns_and_creates_unassigned(self):
        routes = full_routes_no_dissent_needed()
        routes["anthropic"][-1] = (CHAIR_MARKER, {
            **CHAIR_RESP,
            "action_points": [
                {"title": "Ask Adriana for the artwork", "why": "unblocks packaging",
                 "owner_suggestion": "Adriana", "due": ""},
            ],
        })
        wr = build_warroom(self.db, routes, layer_factory=self.layer_factory)
        await wr.frame("Quick board packaging")
        result = await wr.confirm_and_run()
        cards = await wr.preview_project(result["session_id"])
        self.assertIn("isn't on this board", cards[0]["warning"])


class TestFullBoardEndToEnd(WarRoomBase):
    async def test_convergence_skips_phase3_and_runs_phase4(self):
        routes = full_routes_no_dissent_needed()
        for vendor in ("anthropic", "openai", "google"):
            routes[vendor] = [(m, r) for m, r in routes[vendor] if m not in (PHASE2_MARKER, DISSENT_MARKER)]
            routes[vendor].insert(0, (PHASE2_MARKER, PHASE2_AGREE))
            routes[vendor].insert(0, (DISSENT_MARKER, PHASE2_AGREE))
        wr = build_warroom(self.db, routes, layer_factory=self.layer_factory)
        await wr.frame("Should we sign the new distribution contract?", forced_tier="full")
        result = await wr.confirm_and_run()
        transcript = result["report"]
        session = await wr.get_session(result["session_id"])
        phase3 = next(p for p in session["transcript"] if p["phase"] == 3)
        self.assertTrue(phase3["skipped"])
        phase4 = next(p for p in session["transcript"] if p["phase"] == 4)
        self.assertEqual(len(phase4["seats"]), 3)   # Phase 4 still ran

    async def test_real_disagreement_runs_phase3_then_phase4(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        await wr.frame("Should we sign the new distribution contract?", forced_tier="full")
        result = await wr.confirm_and_run()
        session = await wr.get_session(result["session_id"])
        phase3 = next(p for p in session["transcript"] if p["phase"] == 3)
        self.assertFalse(phase3["skipped"])
        self.assertEqual(len(phase3["seats"]), 3)

    async def test_tier_auto_selection(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        quick, why = wr.choose_tier("Should we run a £500 Google Ads push?")
        self.assertEqual(quick, "quick")
        full, why2 = wr.choose_tier("Should we sign the new supplier contract?")
        self.assertEqual(full, "full")
        self.assertIn("contract", why2)
        full2, _ = wr.choose_tier("This would cost about £15,000 in year one")
        self.assertEqual(full2, "full")

    async def test_approve_action_point_writes_through_to_trello(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        await wr.frame("Should we sign the new distribution contract?", forced_tier="full")
        result = await wr.confirm_and_run()
        session_id = result["session_id"]
        rows = await self.db.fetch_all(
            "SELECT id FROM warroom_action_points WHERE session_id = ?", (session_id,)
        )
        message = await wr.approve_action(session_id, rows[0]["id"])
        self.assertIn("Filed", message)
        watch = await self.db.fetch_one("SELECT id FROM warroom_watch WHERE session_id = ?", (session_id,))
        self.assertIsNotNone(watch)

    async def test_reject_action_point(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        await wr.frame("Should we sign the new distribution contract?", forced_tier="full")
        result = await wr.confirm_and_run()
        rows = await self.db.fetch_all(
            "SELECT id FROM warroom_action_points WHERE session_id = ?", (result["session_id"],)
        )
        message = await wr.reject_action(result["session_id"], rows[0]["id"], reason="not now")
        self.assertIn("Rejected", message)


class TestEscalation(WarRoomBase):
    async def test_quick_board_phase1_carries_forward_without_rerunning(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        await wr.frame("Quick board the ad campaign")
        quick_result = await wr.confirm_and_run()
        calls_before = len(wr._debug_claude.calls)

        full_result = await wr.escalate(quick_result["session_id"])

        # Phase 1 wasn't re-run — the call count only grew by the LATER phases.
        phase1_calls_after_escalation = [
            c for c in wr._debug_claude.calls[calls_before:]
            if PHASE1_MARKER in c.get("system", "")
        ]
        self.assertEqual(phase1_calls_after_escalation, [])
        self.assertEqual(full_result["tier"], "full")
        session = await wr.get_session(full_result["session_id"])
        self.assertTrue(session["transcript"][0]["carried_forward"])


class TestBudgetAndWallClock(WarRoomBase):
    async def test_budget_ceiling_stops_and_still_delivers(self):
        wr = build_warroom(
            self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory,
            full_budget_usd=0.000001, quick_budget_usd=0.000001,
        )
        await wr.frame("Quick board something")
        result = await wr.confirm_and_run()
        self.assertTrue(result["budget_hit"])
        self.assertIn("report", result)   # still produced SOMETHING


class TestPrivateWall(WarRoomBase):
    async def test_private_content_never_reaches_the_context(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        await wr.memory.add_chunk(
            "Paul had a rough sobriety day", room="private", type_="PRIVATE", source="test",
        )
        await wr.memory.add_chunk(
            "Prodermis margin is 34% this quarter", room="companies", type_="STABLE", source="test",
        )
        context = await wr._gather_context("How's the business doing?")
        self.assertNotIn("sobriety", context)
        self.assertIn("34%", context)

    async def test_financials_transmitted_bank_details_stripped(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        raw = "Cash position £120,000, margin 34%. Account 12345678, sort code 11-22-33."
        redacted = await redaction.redact(wr.claude, raw, skip_llm_pass=True)
        self.assertIn("£120,000", redacted)
        self.assertIn("34%", redacted)
        self.assertNotIn("12345678", redacted)
        self.assertNotIn("11-22-33", redacted)


class TestModelFallback(WarRoomBase):
    async def test_withdrawn_model_falls_back_and_is_flagged_in_seats(self):
        routes = full_routes_no_dissent_needed()
        gemini = gemini_mock(routes["google"], missing_models=frozenset({"gemini-3.1-pro-preview"}))
        claude = anthropic_mock(routes["anthropic"])
        openai = openai_mock(routes["openai"])
        store = SettingsStore(self.db)
        memory = MemoryStore(self.db, HashEmbedder(), PrivateBox("k"))
        living = LivingFacts(self.db)
        wr = WarRoom(
            self.db, claude, store, memory, living, self.layer_factory,
            Settings(telegram_bot_token="TOK", _env_file=None),
            openai_client=openai, gemini_client=gemini,
        )
        await wr.frame("Should we sign the new distribution contract?", forced_tier="full")
        result = await wr.confirm_and_run()
        seat_c = next(s for s in result["seats"] if s["key"] == "C")
        self.assertEqual(seat_c["model"], "gemini-3.6-flash")   # fell back, never silently ran the old id


class TestArchiveSearch(WarRoomBase):
    async def test_prior_decisions_feed_forward_into_new_sessions(self):
        wr = build_warroom(self.db, full_routes_no_dissent_needed(), layer_factory=self.layer_factory)
        await wr.frame("Should we open a Dutch entity?", forced_tier="quick")
        await wr.confirm_and_run()

        framed2 = await wr.frame("What did the board say about the Dutch company?")
        self.assertIn("Dutch", framed2["context_summary"])


if __name__ == "__main__":
    unittest.main()
