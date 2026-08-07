"""Jarvis backend — FastAPI app (runs on Render as an always-on web service).

Endpoints:
- POST /webhook/telegram/{secret} — Telegram pushes updates here (verified twice:
  secret URL path + X-Telegram-Bot-Api-Secret-Token header).
- GET /healthz — Render health check.

On startup: open the database, build the service clients, and (when PUBLIC_URL
is set) register the webhook with Telegram automatically.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.clients.twilio_client import TwilioClient, valid_signature
from app.cockpit.page import render_page
from app.cockpit.service import CockpitService
from app.config import get_settings
from app.core.router import JarvisRouter
from app.core.store import SettingsStore
from app.daily12.service import Daily12Service
from app.daily12.trello import TrelloClient
from app.db import get_database
from app.documents.service import DocumentLibrary
from app.documents.storage import make_object_store
from app.heartbeat.calendar_ics import IcsCalendar
from app.heartbeat.emailer import Emailer
from app.heartbeat.gates import GateKeeper
from app.heartbeat.jobs import HeartbeatJobs
from app.heartbeat.scheduler import Heartbeat
from app.heartbeat.streaks import Streaks
from app.mail.client import MailAccount, MailClient
from app.mail.service import MailService
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder, VoyageEmbedder
from app.memory.seed import load_day_one_brain
from app.memory.store import LivingFacts, MemoryStore
from app.private.service import PrivateTrack
from app.voice.engine import VoiceEngine
from app.voice.phone import SIGNATURE_ERROR_LINE, PhoneChannel, error_twiml
from app.voice.tools import VoiceTools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("jarvis")


def build_components() -> tuple[JarvisRouter, Heartbeat]:
    settings = get_settings()
    db = get_database(settings.database_url, settings.sqlite_path)

    # Second brain (Milestone 2). Voyage embeds in production; the local
    # embedder keeps everything working before that key exists.
    memory = living = library = None
    if settings.memory_enabled:
        if settings.voyage_api_key:
            embedder = VoyageEmbedder(
                settings.voyage_api_key, model=settings.embed_model, dim=settings.embed_dim
            )
        else:
            logger.warning("VOYAGE_API_KEY not set — using local embeddings (dev quality)")
            embedder = HashEmbedder(dim=settings.embed_dim)
        memory = MemoryStore(db, embedder, PrivateBox(settings.private_room_key))
        living = LivingFacts(db)
        library = DocumentLibrary(
            db,
            memory,
            make_object_store(
                settings.r2_access_key, settings.r2_secret_key,
                settings.r2_bucket, settings.r2_endpoint,
            ),
        )

    # Trello + the Daily 12 (Milestone 3) — activates when the keys exist.
    claude = ClaudeClient(
        settings.anthropic_api_key,
        brain_model=settings.brain_model,
        fast_model=settings.fast_model,
    )
    daily12 = None
    if settings.trello_key and settings.trello_token:
        daily12 = Daily12Service(
            db,
            TrelloClient(settings.trello_key, settings.trello_token),
            claude,
            timezone_default=settings.timezone_default,
            board_filter=settings.trello_boards,
            today_list=settings.trello_today_list,
            personal_list=settings.trello_personal_list,
            per_company=settings.focus_per_company,
            personal_max=settings.focus_personal_max,
        )
    else:
        logger.warning("Trello keys not set — the Daily 12 is dormant until they are")

    telegram = TelegramClient(settings.telegram_bot_token)
    elevenlabs = ElevenLabsClient(
        settings.elevenlabs_api_key,
        voice_id=settings.elevenlabs_voice_id,
        model=settings.elevenlabs_model,
    )

    # The private sobriety track (Milestone 6) — walled off, encrypted, warm.
    private_track = PrivateTrack(
        db,
        claude,
        PrivateBox(settings.private_room_key),
        memory=memory,
        living=living,
    )

    # The gates (chase, never block). Run reminders honour Paul's 6 Aug call:
    # off until RUN_REMINDERS_ENABLED=true (blood pressure first).
    gates = GateKeeper(db, Streaks(db), run_reminders=settings.run_reminders_enabled)

    # Email inboxes (Phase 2) — activate per configured account.
    mail = None
    accounts = get_settings().email_accounts()
    if accounts:
        mail = MailService(
            [MailClient(MailAccount(address, password)) for address, password in accounts], db
        )
        logger.info("Email connected: %s", ", ".join(a for a, _ in accounts))
    else:
        logger.warning("No email accounts set — inbox triage dormant until they are")

    # Live voice (Build Slice: Voice Access) — shared realtime engine for the
    # cockpit's browser sessions and Twilio phone calls. Rides the existing
    # ElevenLabs key; dormant until Paul opens a session.
    voice_engine = None
    if settings.elevenlabs_api_key:
        voice_engine = VoiceEngine(
            settings.elevenlabs_api_key,
            settings.elevenlabs_voice_id,
            db,
            public_url=settings.public_url,
            tool_secret=settings.effective_voice_tool_secret,
        )

    # The Twilio phone channel (4 Aug): a custom pipeline, not ElevenLabs
    # Agents — 'call me', inbound calls to the Twilio number, and the wake-up
    # escalation, all in Jarvis's own voice, all through the same brain.
    phone_channel = None
    if settings.twilio_account_sid and settings.twilio_auth_token:
        phone_channel = PhoneChannel(
            TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token),
            elevenlabs,
            from_number=settings.twilio_from_number,
            paul_number=settings.paul_phone_number,
            public_url=settings.public_url,
            secret=settings.effective_phone_secret,
        )
        if not phone_channel.configured:
            logger.warning(
                "Twilio keys present but TWILIO_FROM_NUMBER / PAUL_PHONE_NUMBER / "
                "PUBLIC_URL incomplete — the phone channel stays dormant"
            )

    # The heartbeat (Milestone 4).
    jobs = HeartbeatJobs(
        settings=settings,
        db=db,
        telegram=telegram,
        claude=claude,
        elevenlabs=elevenlabs,
        daily12=daily12,
        calendar=IcsCalendar(settings.calendar_ics_url) if settings.calendar_ics_url else None,
        emailer=Emailer(settings.gmail_address, settings.gmail_app_password),
        kiefer_email=settings.kiefer_email,
        private_track=private_track,
        gates=gates,
        mail=mail,
        voice_engine=voice_engine,
        phone_channel=phone_channel,
    )
    heartbeat = Heartbeat(jobs)

    router_obj = JarvisRouter(
        settings=settings,
        db=db,
        memory=memory,
        living=living,
        library=library,
        daily12=daily12,
        heartbeat=jobs,
        on_timezone_change=heartbeat.reschedule,
        private_track=private_track,
        gates=gates,
        mail=mail,
        telegram=telegram,
        claude=claude,
        deepgram=DeepgramClient(
            settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
        ),
        elevenlabs=elevenlabs,
    )
    router_obj.voice_engine = voice_engine
    router_obj.phone_channel = phone_channel
    if phone_channel is not None:
        phone_channel.brain = router_obj.phone_turn
        # Realtime upgrade (7 Aug): with the live engine up, inbound calls
        # stream to the agent (interruptible); Gather stays the fallback.
        phone_channel.realtime_available = (
            voice_engine is not None and settings.phone_realtime_enabled
        )
    # Google Calendar live read+write (6 Aug slice): needs only the OAuth
    # client keys — Paul authorises via the connect link; ICS stays fallback.
    if settings.google_oauth_client_id and settings.google_oauth_client_secret:
        from app.clients.gcal_client import GoogleCalendarClient
        from app.heartbeat.gcalendar import GoogleCalendar

        router_obj.gcal = GoogleCalendar(
            GoogleCalendarClient(
                settings.google_oauth_client_id, settings.google_oauth_client_secret
            ),
            db, SettingsStore(db),
        )
        jobs.gcal = router_obj.gcal
    # Meetings Layer B (build order §6): Otter's meeting notes → Brain Dump
    # actions + remembered meeting. Rides the mail accounts already wired for
    # Phase 2 — no new keys, no bot for Jarvis to dispatch (Otter auto-joins
    # on its own). Dormant if no email account is configured.
    if mail is not None:
        from app.meetings_notetaker import MeetingNotetaker

        jobs.notetaker = MeetingNotetaker(
            mail, claude, jobs._chase_layer, memory, jobs.store, jobs._send_text
        )
    # Group intelligence Part 2 (7 Aug): channel-agnostic, rides the
    # Telegram org ingest that's already live — no new keys needed. Trello
    # filing rides the same shared layer as everything else (one watcher).
    from app.groups_intel import GroupIntel

    jobs.group_intel = GroupIntel(db, claude, jobs.store, layer_factory=jobs._chase_layer)
    router_obj.group_intel = jobs.group_intel
    # Zoom quick-start (Layer A, 6 Aug): 'start a new Zoom meeting' → both
    # links on Telegram + a Brain Dump backup card.
    if settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret:
        from app.clients.zoom_client import ZoomClient
        from app.meetings import MeetingMaker

        router_obj.meetings = MeetingMaker(
            ZoomClient(
                settings.zoom_account_id, settings.zoom_client_id,
                settings.zoom_client_secret, user_email=settings.zoom_user_email,
            ),
            layer_factory=jobs._chase_layer,
            notetaker=jobs.notetaker,
        )
    # WhatsApp Part 1 (7 Aug): Paul ↔ Jarvis 1:1, same brain as Telegram.
    # Dormant on the send side (WHATSAPP_SENDING_ENABLED) and on routing
    # (WHATSAPP_OWNER_NUMBER) until Paul supplies both — the webhook handler
    # falls back to the old read-only ingest until then.
    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        from app.clients.whatsapp_client import WhatsAppClient

        router_obj.whatsapp = WhatsAppClient(
            settings.whatsapp_access_token,
            settings.whatsapp_phone_number_id,
            sending_enabled=settings.whatsapp_sending_enabled,
        )
    # The War Room (7 Aug brief): three vendors, two tiers. Built now per
    # Paul's ask — dormant on the two seats that need new keys until both
    # OPENAI_API_KEY and GOOGLE_AI_API_KEY exist; .configured gates every
    # real run so a half-configured board never fires with a silent gap.
    # Trello filing rides the SAME shared layer as everything else — no
    # second watcher; card-watching is a registration seam for Team Radar's
    # (not-yet-built) detection engine, per the brief's own instruction.
    from app.clients.gemini_client import GeminiClient
    from app.clients.openai_client import OpenAIClient
    from app.daily12.scoring import COMPANY_NAMES
    from app.warroom.service import WarRoom

    router_obj.warroom = WarRoom(
        db, claude, jobs.store, memory, living, jobs._chase_layer, settings,
        openai_client=OpenAIClient(settings.openai_api_key) if settings.openai_api_key else None,
        gemini_client=GeminiClient(settings.google_ai_api_key) if settings.google_ai_api_key else None,
        company_names=list(COMPANY_NAMES.values()),
        full_budget_usd=settings.warroom_full_budget_usd,
        quick_budget_usd=settings.warroom_quick_budget_usd,
        monthly_ceiling_usd=settings.warroom_monthly_ceiling_usd,
        escalate_value_gbp=settings.warroom_escalate_value_gbp,
    )
    # Team Radar (7 Aug brief): no new key — runs entirely on the Trello
    # connection already live. It IS the War Room's watcher (reads
    # warroom_watch); do not build a second one anywhere else.
    if settings.trello_key and settings.trello_token:
        from app.daily12.trello import TrelloClient as _RadarTrelloClient
        from app.radar.service import TeamRadar
        from app.radar.sync import RadarSync

        radar_client = _RadarTrelloClient(settings.trello_key, settings.trello_token)
        jobs.radar_sync = RadarSync(radar_client, db, jobs.store)
        jobs.radar = TeamRadar(db, jobs.store)
        router_obj.radar = jobs.radar
    # GPS awareness + daily working memory (7 Aug brief): extends the
    # existing timezone-following pipe (app/heartbeat/location.py), never
    # replaces it. No new key needed for §1/§2/§4/§5 — only §3's
    # traffic-aware leave-now alerts need GOOGLE_MAPS_API_KEY, which is
    # flagged as needed and hasn't been supplied; that client stays
    # dormant (.configured gates it) until it is.
    from app.clients.maps_client import MapsClient
    from app.location.service import LocationAwareness

    jobs.maps = MapsClient(settings.google_maps_api_key)
    jobs.location = LocationAwareness(
        db, jobs.store, living, jobs._send_text,
        gcal=router_obj.gcal, daily12=daily12, memory=memory, private_track=private_track,
        timezone_default=settings.timezone_default,
    )
    router_obj.location = jobs.location
    router_obj.voice_tools = VoiceTools(
        db, memory=memory, living=living, daily12=daily12, mail=mail, jobs=jobs,
        timezone_default=settings.timezone_default,
    )
    return router_obj, heartbeat


@asynccontextmanager
async def lifespan(app: FastAPI):
    router, heartbeat = build_components()
    await router.db.init()
    app.state.router = router
    app.state.heartbeat = heartbeat

    # Load the Day-One Brain seed (idempotent, versioned).
    if router.memory is not None and router.living is not None:
        try:
            loaded = await load_day_one_brain(router.memory, router.living, SettingsStore(router.db))
            if loaded:
                logger.info("Day-One Brain seeded: %d chunks", loaded)
        except Exception:
            logger.exception("Day-One Brain seeding failed (service continues)")

    # One-off (guarded, inert after 10 Aug 2026): Paul's standing instruction
    # from 4 Aug 11:16 — 'Run and meds were skipping today and we will for
    # this week while I recover' — predated the skip lever, so the engineer
    # applies it here rather than making Paul repeat himself.
    if router.gates is not None:
        try:
            store = SettingsStore(router.db)
            if not await store.get("oneoff_recovery_week_2026_08"):
                from datetime import date as _date, timedelta as _td

                start = _date(2026, 8, 4)
                for offset in range(7):
                    await router.gates.override(
                        ["run", "meds"],
                        "Recovery week — Paul's instruction, 4 Aug 2026 11:16 (applied by engineer)",
                        start + _td(days=offset),
                    )
                await store.set("oneoff_recovery_week_2026_08", "applied")
                logger.info("Recovery week excusal applied (4-10 Aug 2026)")
        except Exception:
            logger.exception("Recovery-week one-off failed (service continues)")

    settings = router.settings
    if settings.public_url and settings.telegram_bot_token:
        url = f"{settings.public_url.rstrip('/')}/webhook/telegram/{settings.effective_webhook_secret}"
        try:
            await router.telegram.set_webhook(url, settings.effective_webhook_secret)
            logger.info("Telegram webhook registered")
        except Exception:
            logger.exception("Could not register Telegram webhook (will keep serving)")

    # Point the Twilio number's inbound webhook at us (idempotent, self-heals
    # every deploy) and surface the trial-account limitation honestly.
    phone = getattr(router, "phone_channel", None)
    if phone is not None and phone.configured:
        try:
            problem = await phone.twilio.configure_inbound(phone.from_number, phone.answer_url())
            if problem:
                logger.warning("Twilio inbound webhook NOT set: %s", problem)
            else:
                logger.info("Twilio inbound webhook registered — calling the number reaches Jarvis")
            account = await phone.twilio.account_summary()
            if account and account.get("type") == "Trial":
                logger.warning(
                    "Twilio account is on TRIAL — it can only ring verified numbers; "
                    "upgrade it (or verify Paul's number) before relying on 'call me'"
                )
        except Exception:
            logger.exception("Twilio phone setup failed (bot still serves)")

    # Start the heartbeat — the day runs itself from here.
    if settings.heartbeat_enabled and settings.telegram_bot_token:
        try:
            await heartbeat.start()
        except Exception:
            logger.exception("Heartbeat failed to start (bot still serves)")

    yield

    heartbeat.shutdown()
    await router.db.close()
    closables = [router.telegram, router.claude, router.deepgram, router.elevenlabs]
    if getattr(router, "voice_engine", None) is not None:
        closables.append(router.voice_engine)
    if getattr(router, "phone_channel", None) is not None:
        closables.append(router.phone_channel.twilio)
    if getattr(router, "meetings", None) is not None:
        closables.append(router.meetings.zoom)
    for client in closables:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, lifespan=lifespan)

# Strong references to in-flight update tasks — asyncio only weak-refs tasks,
# so without this a message could be garbage-collected mid-handling.
_inflight: set = set()


def _track(task) -> None:
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "jarvis"}


@app.post("/webhook/telegram/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    router: JarvisRouter = request.app.state.router
    expected = router.settings.effective_webhook_secret
    if not (
        hmac.compare_digest(secret, expected)
        and hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected)
    ):
        raise HTTPException(status_code=403, detail="nope")
    update = await request.json()
    # Answer Telegram fast; do the actual work in the background.
    _track(asyncio.create_task(router.handle_update(update)))
    return {"ok": True}


def _check_cockpit_secret(router: "JarvisRouter", secret: str) -> None:
    if not hmac.compare_digest(secret, router.settings.effective_cockpit_secret):
        raise HTTPException(status_code=404)


async def _cockpit_gate(router: "JarvisRouter", request: Request) -> str:
    from app.cockpit import auth as cockpit_auth

    return await cockpit_auth.gate(
        router.store, request.cookies.get(cockpit_auth.COOKIE_NAME, "")
    )


@app.get("/cockpit/{secret}")
async def cockpit_page(secret: str, request: Request):
    """The Progress Cockpit. The link finds the door; the password opens it —
    a leaked/forwarded link alone shows no personal data."""
    from fastapi.responses import HTMLResponse

    from app.cockpit import auth as cockpit_auth

    router: JarvisRouter = request.app.state.router
    _check_cockpit_secret(router, secret)
    state = await _cockpit_gate(router, request)
    if state == "setup":
        return HTMLResponse(cockpit_auth.setup_page())
    if state == "login":
        return HTMLResponse(cockpit_auth.login_page(f"/cockpit/{secret}/login"))
    return HTMLResponse(render_page(f"/cockpit/{secret}/data"))


@app.post("/cockpit/{secret}/login")
async def cockpit_login(secret: str, request: Request):
    from urllib.parse import parse_qs

    from fastapi.responses import HTMLResponse, RedirectResponse

    from app.cockpit import auth as cockpit_auth

    router: JarvisRouter = request.app.state.router
    _check_cockpit_secret(router, secret)
    stored = await router.store.get(cockpit_auth.PASSWORD_KEY, "")
    if not stored:
        return HTMLResponse(cockpit_auth.setup_page())
    if await cockpit_auth.login_locked(router.store):
        return HTMLResponse(
            cockpit_auth.login_page(
                f"/cockpit/{secret}/login",
                error="Too many attempts — locked for a while. Try again later.",
            ),
            status_code=429,
        )
    body = (await request.body()).decode("utf-8", "replace")
    password = parse_qs(body).get("password", [""])[0]
    if not cockpit_auth.verify_password(password, stored):
        await cockpit_auth.note_failed_login(router.store)
        await asyncio.sleep(1.0)  # blunt the brute force
        return HTMLResponse(
            cockpit_auth.login_page(f"/cockpit/{secret}/login", error="Wrong password."),
            status_code=401,
        )
    await cockpit_auth.clear_login_failures(router.store)
    key = await router.store.get(cockpit_auth.SESSION_KEY, "")
    if not key:
        import os as _os

        key = _os.urandom(32).hex()
        await router.store.set(cockpit_auth.SESSION_KEY, key)
    response = RedirectResponse(url=f"/cockpit/{secret}", status_code=303)
    response.set_cookie(
        cockpit_auth.COOKIE_NAME,
        cockpit_auth.mint_session(key),
        max_age=cockpit_auth.SESSION_DAYS * 86400,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/cockpit",
    )
    return response


@app.get("/cockpit/{secret}/data")
async def cockpit_data(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _check_cockpit_secret(router, secret)
    if await _cockpit_gate(router, request) != "ok":
        raise HTTPException(status_code=401, detail="cockpit locked")
    service = CockpitService(
        router.db,
        living=router.living,
        calendar=router.heartbeat.calendar_feed() if router.heartbeat else None,
        timezone_default=router.settings.timezone_default,
    )
    return await service.gather()


@app.post("/cockpit/{secret}/voice-url")
async def cockpit_voice_url(secret: str, request: Request) -> dict:
    """Mint a short-lived live-session URL for the cockpit's Talk button."""
    router: JarvisRouter = request.app.state.router
    _check_cockpit_secret(router, secret)
    if await _cockpit_gate(router, request) != "ok":
        raise HTTPException(status_code=401, detail="cockpit locked")
    engine = getattr(router, "voice_engine", None)
    if engine is None:
        return {"error": "Live voice needs the ElevenLabs key — it's not set."}
    mode = request.query_params.get("mode", "assistant")
    if mode not in ("assistant", "interpreter", "support"):
        mode = "assistant"
    try:
        return {"url": await engine.signed_session_url(mode=mode)}
    except Exception as exc:
        logging.getLogger("jarvis").exception("Live voice session failed")
        detail = str(exc)
        if "convai" in detail and ("401" in detail or "unauthorized" in detail):
            return {
                "error": (
                    "Your ElevenLabs API key can't manage Conversational AI agents yet. "
                    "Fix (2 min): elevenlabs.io → your profile → API Keys → edit the key "
                    "Jarvis uses (or create a new one) and enable the Conversational AI "
                    "permissions (read + write). If you make a new key, update "
                    "ELEVENLABS_API_KEY in Render, then try this button again."
                )
            }
        return {"error": f"Couldn't open a live session: {detail[:200]}"}


@app.api_route("/voice/tools/{secret}/{tool_name}", methods=["GET", "POST"])
async def voice_tool(secret: str, tool_name: str, request: Request) -> dict:
    """Webhook the live agent calls mid-conversation (memory + actions)."""
    router: JarvisRouter = request.app.state.router
    if not hmac.compare_digest(secret, router.settings.effective_voice_tool_secret):
        raise HTTPException(status_code=403, detail="nope")
    tools: VoiceTools | None = getattr(router, "voice_tools", None)
    if tools is None:
        return {"result": "Tools aren't wired on this deployment."}
    try:
        args = await request.json()
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    return {"result": await tools.dispatch(tool_name, args)}


def _phone_gate(router: "JarvisRouter", secret: str):
    """The phone endpoints exist only when the channel does, behind an
    unguessable path segment — everything else 404s."""
    phone = getattr(router, "phone_channel", None)
    if phone is None or not hmac.compare_digest(secret, router.settings.effective_phone_secret):
        raise HTTPException(status_code=404)
    return phone


async def _twilio_form(request: Request) -> dict[str, str]:
    """Twilio posts application/x-www-form-urlencoded. Parsed by hand (same
    pattern as the cockpit login) — request.form() needs the python-multipart
    package this project deliberately doesn't ship, and the AssertionError it
    raises took down the first live call (4 Aug: 'application error').
    keep_blank_values matters: Twilio sends blank fields (CallerName=,
    FromCity= — usually empty for UK mobiles) and includes them in the
    signature; dropping them failed every real signature check (third call)."""
    from urllib.parse import parse_qs

    body = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _twilio_signed(router: "JarvisRouter", request: Request, params: dict) -> bool:
    """Verify X-Twilio-Signature against the public URL Twilio actually hit.
    Without PUBLIC_URL (local dev) there's nothing to sign against — the
    secret path still guards the door."""
    settings = router.settings
    if not settings.public_url:
        return True
    url = settings.public_url.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return valid_signature(
        settings.twilio_auth_token, url, params, request.headers.get("X-Twilio-Signature", "")
    )


@app.post("/twilio/voice/{secret}/answer")
async def twilio_answer(secret: str, request: Request):
    """First TwiML of every call — outbound greetings and inbound answers."""
    from fastapi.responses import Response

    router: JarvisRouter = request.app.state.router
    phone = _phone_gate(router, secret)
    params = await _twilio_form(request)
    logger.info(
        "Twilio answer webhook: CallSid=%s status=%s direction=%s",
        params.get("CallSid", "?"), params.get("CallStatus", "?"), params.get("Direction", "?"),
    )
    if not _twilio_signed(router, request, params):
        # A signature mismatch is config trouble, not an attack to die on —
        # the unguessable path already gates the door (the Telegram-webhook
        # trust model). Say so out loud, take no instructions, log the why.
        logger.warning(
            "Twilio signature check FAILED on %s (header %s) — refusing politely",
            request.url.path, "present" if request.headers.get("X-Twilio-Signature") else "MISSING",
        )
        return Response(content=error_twiml(SIGNATURE_ERROR_LINE), media_type="text/xml")
    params["g"] = request.query_params.get("g", "")
    params["fallback"] = request.query_params.get("fallback", "")
    try:
        twiml = await phone.handle_answer(params)
    except Exception:
        logger.exception("Twilio answer handler failed — speaking the error instead")
        twiml = error_twiml()
    return Response(content=twiml, media_type="text/xml")


@app.post("/twilio/voice/{secret}/turn")
async def twilio_turn(secret: str, request: Request):
    """One conversational turn: Paul's speech in, Jarvis's reply TwiML out."""
    from fastapi.responses import Response

    router: JarvisRouter = request.app.state.router
    phone = _phone_gate(router, secret)
    params = await _twilio_form(request)
    logger.info(
        "Twilio turn webhook: CallSid=%s heard %d chars (confidence %s)",
        params.get("CallSid", "?"), len(params.get("SpeechResult", "")),
        params.get("Confidence", "?"),
    )
    if not _twilio_signed(router, request, params):
        logger.warning(
            "Twilio signature check FAILED on %s (header %s) — refusing politely",
            request.url.path, "present" if request.headers.get("X-Twilio-Signature") else "MISSING",
        )
        return Response(content=error_twiml(SIGNATURE_ERROR_LINE), media_type="text/xml")
    try:
        twiml = await phone.handle_turn(params)
    except Exception:
        logger.exception("Twilio turn handler failed — speaking the error instead")
        twiml = error_twiml()
    return Response(content=twiml, media_type="text/xml")


@app.get("/twilio/audio/{secret}/{audio_id}.mp3")
async def twilio_audio(secret: str, audio_id: str, request: Request):
    """Short-lived reply audio (Jarvis's ElevenLabs voice) for <Play>."""
    from fastapi.responses import Response

    router: JarvisRouter = request.app.state.router
    phone = _phone_gate(router, secret)
    data = phone.get_audio(audio_id)
    if data is None:
        raise HTTPException(status_code=404)
    return Response(content=data, media_type="audio/mpeg")


@app.websocket("/twilio/media/{secret}")
async def twilio_media(websocket: WebSocket, secret: str):
    """The realtime call (7 Aug): Twilio's Media Stream lands here and gets
    bridged to the live ElevenLabs agent. Closing early (bad secret, engine
    down, agent unreachable) makes Twilio run the <Redirect> fallback in the
    answer TwiML — the caller drops to the turn-based flow, never dead air."""
    from app.voice.media_bridge import MediaBridge

    router: JarvisRouter = websocket.app.state.router
    phone = getattr(router, "phone_channel", None)
    engine = getattr(router, "voice_engine", None)
    if phone is None or not hmac.compare_digest(
        secret, router.settings.effective_phone_secret
    ):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    try:
        if phone.eleven_connect is not None:
            eleven = await phone.eleven_connect()
        else:
            if engine is None:
                await websocket.close()
                return
            import websockets as _ws

            url = await engine.signed_session_url("assistant")
            eleven = await _ws.connect(url, max_size=2 ** 22)
    except Exception:
        logger.exception("Agent socket failed — closing so the Gather fallback runs")
        await websocket.close()
        return
    try:
        await MediaBridge(websocket, eleven).run()
    except Exception:
        logger.exception("Media bridge ended with an error")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# --- The Mac desktop app (6 Aug): 'Hey Jarvis' on Paul's desk. The app
# records locally (wake-word gated) and posts here; STT, brain and TTS all
# run server-side so no API key ever leaves Render.

def _desktop_gate(router: "JarvisRouter", secret: str) -> None:
    import hmac as _hmac

    if not _hmac.compare_digest(secret, router.settings.effective_desktop_secret):
        raise HTTPException(status_code=403)


async def _desktop_reply_payload(router: "JarvisRouter", transcript: str, spoken: bool) -> dict:
    from app.core.reply_policy import decide_reply, strip_for_speech

    raw = await router.desktop_turn(transcript, spoken=spoken)
    channel, reply_text = decide_reply(raw, incoming_was_voice=spoken)
    audio_b64 = None
    if channel == "voice" and router.elevenlabs is not None:
        import base64 as _base64

        try:
            audio = await router.elevenlabs.synthesize(strip_for_speech(reply_text))
            audio_b64 = _base64.b64encode(audio).decode()
        except Exception:
            logger.exception("Desktop TTS failed — text-only reply")
    return {"transcript": transcript, "reply": reply_text, "audio_b64": audio_b64}


@app.get("/desktop/{secret}/ping")
async def desktop_ping(secret: str, request: Request) -> dict:
    """The app's 'Connected ✓' check."""
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    return {"ok": True, "service": "jarvis-desktop"}


@app.get("/desktop/{secret}/dashboard")
async def desktop_dashboard(secret: str, request: Request):
    """The Mac app's Dashboard button (6 Aug: Paul shouldn't have to
    remember the cockpit URL). Desktop secret in → redirect to the real
    cockpit address; the cockpit's own password/session lock still applies."""
    from fastapi.responses import RedirectResponse

    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    return RedirectResponse(url=f"/cockpit/{router.settings.effective_cockpit_secret}")


@app.post("/desktop/{secret}/message")
async def desktop_message(secret: str, request: Request) -> dict:
    """A typed turn from the desktop app: JSON {"text": ...}."""
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    try:
        body = await request.json()
        text = str(body.get("text", "")).strip()
    except Exception:
        text = ""
    if not text:
        return {"transcript": "", "reply": "Say again?", "audio_b64": None}
    try:
        return await _desktop_reply_payload(router, text, spoken=False)
    except Exception:
        logger.exception("Desktop message turn failed")
        return {
            "transcript": text, "audio_b64": None,
            "reply": "Hit a snag processing that one — say it again and I'm on it.",
        }


@app.post("/desktop/{secret}/voice")
async def desktop_voice(secret: str, request: Request) -> dict:
    """A spoken turn: raw audio in the body (Content-Type tells Deepgram the
    format — the app sends audio/webm). Transcript, reply and Jarvis's voice
    (base64 MP3) come back in one JSON response."""
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    audio = await request.body()
    if not audio:
        return {"transcript": "", "reply": "Say again?", "audio_b64": None}
    mimetype = request.headers.get("content-type") or "audio/webm"
    try:
        transcript = await router.deepgram.transcribe(
            audio, mimetype.split(";")[0], keyterms=await router._speech_vocabulary()
        )
    except Exception:
        logger.exception("Desktop STT failed")
        transcript = ""
    if not transcript:
        return {
            "transcript": "", "audio_b64": None,
            "reply": "Couldn't make that out — give me it again?",
        }
    try:
        return await _desktop_reply_payload(router, transcript, spoken=True)
    except Exception:
        logger.exception("Desktop voice turn failed")
        return {
            "transcript": transcript, "audio_b64": None,
            "reply": "Hit a snag processing that one — say it again and I'm on it.",
        }


# --- Google Calendar OAuth (6 Aug): Paul clicks ONE link, Google asks for
# consent, the refresh token lands in the settings store. State is the
# desktop secret — unguessable, already shared with his own surfaces.

@app.get("/google/connect/{secret}")
async def google_connect(secret: str, request: Request):
    from fastapi.responses import RedirectResponse

    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gcal = getattr(router, "gcal", None)
    if gcal is None or not gcal.client.configured:
        raise HTTPException(status_code=409, detail="Google OAuth keys not configured")
    redirect_uri = f"{router.settings.public_url.rstrip('/')}/google/callback"
    return RedirectResponse(url=gcal.client.auth_url(redirect_uri, state=secret))


@app.get("/google/callback")
async def google_callback(request: Request):
    from fastapi.responses import HTMLResponse

    router: JarvisRouter = request.app.state.router
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    _desktop_gate(router, state)
    gcal = getattr(router, "gcal", None)
    if gcal is None or not code:
        return HTMLResponse("<h3>Something's missing — ask Jarvis to connect again.</h3>", status_code=400)
    try:
        redirect_uri = f"{router.settings.public_url.rstrip('/')}/google/callback"
        refresh = await gcal.client.exchange_code(code, redirect_uri)
        await gcal.store_token(refresh)
    except Exception as exc:
        logger.exception("Google Calendar code exchange failed")
        return HTMLResponse(
            f"<h3>Google said no: {str(exc)[:200]}</h3><p>Tell Jarvis and he'll sort it.</p>",
            status_code=502,
        )
    return HTMLResponse(
        "<h3>✅ Google Calendar connected.</h3>"
        "<p>You can close this tab — Jarvis reads and writes your calendar live now.</p>"
    )


@app.get("/desktop/{secret}/card-grammar")
async def desktop_card_grammar(secret: str, request: Request) -> dict:
    """The Card Script panel's data (6 Aug brief): the dictation grammar,
    rendered from the SAME live Trello config the parser resolves against."""
    from app.daily12.grammar import card_grammar

    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    factory = getattr(router.heartbeat, "_chase_layer", None) if router.heartbeat else None
    return await card_grammar(factory)


@app.get("/desktop/{secret}/calendar")
async def desktop_calendar(secret: str, request: Request) -> dict:
    """next_up / today / week for every thin surface (Mac app, cockpit)."""
    from zoneinfo import ZoneInfo as _Z

    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gcal = getattr(router, "gcal", None)
    if gcal is None or not await gcal.authorised():
        return {"connected": False, "reason": "Google Calendar not connected — say 'connect google calendar' to Jarvis"}
    from app.core.router import TIMEZONE_KEY

    tz = _Z(await router.store.get(TIMEZONE_KEY, router.settings.timezone_default))
    which = request.query_params.get("range", "next_up")
    from app.clients.gcal_client import ReauthNeeded

    try:
        if which == "today":
            return {"connected": True, "today": await gcal.today(tz)}
        if which == "week":
            return {"connected": True, "week": await gcal.week(tz)}
        return {"connected": True, "next_up": await gcal.next_up(tz)}
    except ReauthNeeded:
        return {"connected": False, "reason": "Google token expired — say 'connect google calendar' to Jarvis"}


# --- Group intelligence, Part 2 (7 Aug) — thin endpoints, GroupIntel does the work ---

@app.get("/desktop/{secret}/groups/summaries")
async def desktop_groups_summaries(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gi = router.group_intel
    if gi is None or await gi.status() == "not_connected":
        return {"connected": False, "reason": "No group ingest connected yet"}
    return {"connected": True, "groups": await gi.group_summaries()}


@app.get("/desktop/{secret}/groups/actions")
async def desktop_groups_actions(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gi = router.group_intel
    if gi is None or await gi.status() == "not_connected":
        return {"connected": False, "reason": "No group ingest connected yet"}
    return {"connected": True, "actions": await gi.open_actions()}


@app.get("/desktop/{secret}/groups/missed-summary")
async def desktop_groups_missed_summary(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gi = router.group_intel
    if gi is None or await gi.status() == "not_connected":
        return {"connected": False, "reason": "No group ingest connected yet"}
    return {"connected": True, **await gi.missed_summary()}


@app.get("/desktop/{secret}/groups/uncleared-count")
async def desktop_groups_uncleared_count(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gi = router.group_intel
    if gi is None or await gi.status() == "not_connected":
        return {"connected": False, "count": 0}
    return {"connected": True, "count": await gi.uncleared_count()}


@app.post("/desktop/{secret}/groups/dismiss-summary")
async def desktop_groups_dismiss_summary(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gi = router.group_intel
    if gi is None:
        raise HTTPException(status_code=409, detail="group intelligence not wired up")
    await gi.dismiss_summary()
    return {"ok": True}


@app.post("/desktop/{secret}/groups/actions/{action_id}/trello")
async def desktop_groups_action_to_trello(secret: str, action_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gi = router.group_intel
    if gi is None:
        raise HTTPException(status_code=409, detail="group intelligence not wired up")
    return {"ok": True, "message": await gi.action_to_trello(action_id)}


@app.post("/desktop/{secret}/groups/actions/{action_id}/ignore")
async def desktop_groups_action_ignore(secret: str, action_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    gi = router.group_intel
    if gi is None:
        raise HTTPException(status_code=409, detail="group intelligence not wired up")
    return {"ok": True, "message": await gi.action_ignore(action_id)}


@app.get("/desktop/{secret}/groups/fixtures")
async def desktop_groups_fixtures(secret: str, request: Request) -> dict:
    """Sample payloads (§7) so every surface can be built and tested before
    real group traffic exists — never mistaken for live data by a caller
    that checks the endpoint path, not a flag inside the body."""
    router: JarvisRouter = request.app.state.router
    _desktop_gate(router, secret)
    from app.groups_intel import GroupIntel

    return GroupIntel.fixtures()


# --- The War Room (7 Aug) — thin endpoints, WarRoom does the work ---

def _warroom_gate(router: "JarvisRouter", secret: str):
    _desktop_gate(router, secret)
    if router.warroom is None:
        raise HTTPException(status_code=409, detail="War Room not wired up")
    return router.warroom


@app.post("/desktop/{secret}/warroom/frame")
async def warroom_frame(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    if not wr.configured:
        return {"configured": False, "reason": "OPENAI_API_KEY / GOOGLE_AI_API_KEY not set"}
    body = await request.json()
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    framed = await wr.frame(question, forced_tier=str(body.get("tier", "")))
    return {"configured": True, **framed}


@app.post("/desktop/{secret}/warroom/confirm")
async def warroom_confirm(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    body = await request.json() if await request.body() else {}
    return await wr.confirm_and_run(unredacted=bool(body.get("unredacted")))


@app.post("/desktop/{secret}/warroom/escalate/{session_id}")
async def warroom_escalate(secret: str, session_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    return await wr.escalate(session_id)


@app.get("/desktop/{secret}/warroom/session/{session_id}")
async def warroom_session(secret: str, session_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    session = await wr.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.get("/desktop/{secret}/warroom/archive")
async def warroom_archive(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    query = request.query_params.get("q", "")
    return {"sessions": await wr.search_archive(query)}


@app.post("/desktop/{secret}/warroom/actions/{session_id}/{action_id}/approve")
async def warroom_approve(secret: str, session_id: int, action_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    body = await request.json() if await request.body() else {}
    return {"message": await wr.approve_action(session_id, action_id, owner_override=str(body.get("owner", "")))}


@app.post("/desktop/{secret}/warroom/actions/{session_id}/{action_id}/reject")
async def warroom_reject(secret: str, session_id: int, action_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    body = await request.json() if await request.body() else {}
    return {"message": await wr.reject_action(session_id, action_id, reason=str(body.get("reason", "")))}


@app.get("/desktop/{secret}/warroom/preview/{session_id}")
async def warroom_preview(secret: str, session_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    return {"cards": await wr.preview_project(session_id)}


@app.post("/desktop/{secret}/warroom/create/{session_id}")
async def warroom_create(secret: str, session_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    body = await request.json() if await request.body() else {}
    return await wr.create_project(session_id, edited_cards=body.get("cards"))


@app.post("/desktop/{secret}/warroom/undo")
async def warroom_undo(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    wr = _warroom_gate(router, secret)
    return {"message": await wr.undo()}


# --- Team Radar (7 Aug) — same endpoint serves the Mac app AND the cockpit ---

def _radar_gate(router: "JarvisRouter", secret: str):
    _desktop_gate(router, secret)
    if router.radar is None:
        raise HTTPException(status_code=409, detail="Team Radar not wired up")
    return router.radar


@app.get("/desktop/{secret}/radar/needs-you")
async def radar_needs_you(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    radar = _radar_gate(router, secret)
    return {"coverage": await radar.coverage(), **await radar.needs_you()}


@app.get("/desktop/{secret}/radar/columns")
async def radar_columns(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    radar = _radar_gate(router, secret)
    return {"coverage": await radar.coverage(), "columns": await radar.columns()}


@app.get("/desktop/{secret}/radar/rollup")
async def radar_rollup(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    radar = _radar_gate(router, secret)
    return {"coverage": await radar.coverage(), "companies": await radar.company_rollup()}


@app.get("/desktop/{secret}/radar/coverage")
async def radar_coverage(secret: str, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    radar = _radar_gate(router, secret)
    return await radar.coverage()


@app.get("/desktop/{secret}/radar/project/{session_id}")
async def radar_project(secret: str, session_id: int, request: Request) -> dict:
    router: JarvisRouter = request.app.state.router
    radar = _radar_gate(router, secret)
    return await radar.project_status(session_id)


async def merge_water_total(db, day_iso: str, water_ml: int) -> bool:
    """Apple Health (WaterMinder, the Watch, any water app) sends the day's
    CUMULATIVE total — merge by MAX so manual '300ml' logging and the export
    never double-count."""
    if water_ml <= 0:
        return False
    row = await db.fetch_one("SELECT ml FROM water_log WHERE day = ?", (day_iso,))
    total = max(int(row["ml"]) if row else 0, water_ml)
    if db.dialect == "postgres":
        await db.execute(
            "INSERT INTO water_log (day, ml) VALUES (?, ?)"
            " ON CONFLICT (day) DO UPDATE SET ml = EXCLUDED.ml",
            (day_iso, total),
        )
    else:
        await db.execute(
            "INSERT OR REPLACE INTO water_log (day, ml) VALUES (?, ?)", (day_iso, total)
        )
    return True


@app.post("/webhook/location")
async def phone_location(request: Request) -> dict:
    """Paul's iPhone Shortcut posts {secret, lat, lon} a few times a day.
    Shares the Apple Health webhook secret (same phone, same PDF step).
    Moving timezone switches every clock and tells him once."""
    router: JarvisRouter = request.app.state.router
    payload = await request.json()
    secret = router.settings.apple_health_webhook_secret
    if not secret or not hmac.compare_digest(str(payload.get("secret", "")), secret):
        raise HTTPException(status_code=403, detail="nope")
    try:
        lat = float(payload.get("lat") if payload.get("lat") is not None else payload.get("latitude"))
        lon = float(payload.get("lon") if payload.get("lon") is not None else payload.get("longitude"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="lat/lon required")

    from app.heartbeat.location import apply_location

    result = await apply_location(router.store, router.living, lat, lon)
    if result["changed"]:
        try:
            await request.app.state.heartbeat.reschedule()
        except Exception:
            logger.exception("Reschedule after GPS timezone change failed")
        if router.heartbeat is not None:
            try:
                await router.heartbeat._send_text(
                    f"Clocks followed you to {result['place']} — briefs, nudges, "
                    "bedtime and wake-ups all moved with you."
                )
            except Exception:
                logger.exception("Timezone-change note failed — clocks moved anyway")
    # GPS awareness (7 Aug): history + place classification + arrival
    # consumers, layered on TOP of the timezone-following above, never
    # replacing it.
    if router.location is not None:
        try:
            await router.location.record_fix(lat, lon)
        except Exception:
            logger.exception("Location awareness record_fix failed — timezone handling still succeeded")
    return {"ok": True, **result}


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    """Meta's webhook verification handshake — echo the challenge when the
    verify token matches. No token configured = endpoint shut."""
    from fastapi.responses import PlainTextResponse

    settings = request.app.state.router.settings
    params = request.query_params
    if (
        settings.whatsapp_verify_token
        and params.get("hub.mode") == "subscribe"
        and hmac.compare_digest(
            params.get("hub.verify_token", ""), settings.whatsapp_verify_token
        )
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="nope")


@app.post("/webhook/whatsapp")
async def whatsapp_inbound(request: Request) -> dict:
    """Part 1 (7 Aug): Paul's own WhatsApp number reaches the same brain as
    Telegram — text and voice notes in, text or voice replies out, once
    WHATSAPP_OWNER_NUMBER + WHATSAPP_SENDING_ENABLED are set. Any other
    sender is silently ignored (Jarvis's number is a business number
    anyone can message). Until the owner number is set, the original
    Phase 1 read-only ingest holds unchanged — nothing routes, nothing sends."""
    import json as _json
    from datetime import datetime, timezone as _tz

    from app.clients.whatsapp_client import parse_webhook, valid_signature
    from app.core.reply_policy import decide_reply, strip_for_speech

    router: JarvisRouter = request.app.state.router
    settings = router.settings
    if not settings.whatsapp_verify_token:
        raise HTTPException(status_code=403, detail="nope")
    body = await request.body()
    if not valid_signature(
        settings.whatsapp_app_secret, body,
        request.headers.get("X-Hub-Signature-256", ""),
    ):
        raise HTTPException(status_code=403, detail="nope")
    try:
        payload = _json.loads(body or b"{}")
    except Exception:
        return {"ok": True, "ingested": 0}
    messages = parse_webhook(payload)
    now_iso = datetime.now(_tz.utc).isoformat(timespec="seconds")
    owner_digits = "".join(c for c in settings.whatsapp_owner_number if c.isdigit())
    handled = 0
    for m in messages:
        if not owner_digits:
            # Owner not set yet — the original read-only ingest, unchanged.
            await router.db.execute(
                "INSERT INTO wa_direct_ingest (ts, wa_id, sender, company_tag, kind, message)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso, m.wa_id, m.name[:120], "", m.kind, m.text or f"[{m.kind}]"),
            )
            continue
        if m.wa_id != owner_digits:
            continue   # not Paul — silently ignored (Part 1's hard rule)
        transcript, is_voice = m.text, False
        if m.kind == "voice":
            is_voice = True
            transcript = ""
            if m.media_id and router.whatsapp is not None:
                try:
                    audio, mime = await router.whatsapp.download_media(m.media_id)
                    transcript = await router.deepgram.transcribe(audio, mime)
                except Exception:
                    logger.exception("WhatsApp voice transcription failed")
        if not transcript:
            continue
        handled += 1
        reply = await router.whatsapp_turn(transcript, is_voice=is_voice)
        if not reply or router.whatsapp is None:
            continue
        channel, reply_text = decide_reply(reply, incoming_was_voice=is_voice)
        sent = False
        if channel == "voice" and router.elevenlabs is not None:
            try:
                audio_mp3 = await router.elevenlabs.synthesize(strip_for_speech(reply_text))
                sent = await router.whatsapp.send_voice(m.wa_id, audio_mp3)
            except Exception:
                logger.exception("WhatsApp TTS failed — falling back to text")
        if not sent:
            await router.whatsapp.send_text(m.wa_id, reply_text)
    return {"ok": True, "ingested": handled}


@app.post("/webhook/apple-health")
async def apple_health(request: Request) -> dict:
    """Health data in: the iOS Shortcut's flat JSON (secret in the body) or
    the Health Auto Export app's nested format (secret as the X-Health-Secret
    header or ?secret= — the app can't put fields in its body, 5 Aug).
    Hourly posts are safe: water max-merges, the run logs once."""
    router: JarvisRouter = request.app.state.router
    payload = await request.json()
    secret = router.settings.apple_health_webhook_secret
    provided = (
        str(payload.get("secret") or "")
        or request.headers.get("X-Health-Secret", "")
        or request.query_params.get("secret", "")
    )
    if not secret or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=403, detail="nope")

    from datetime import datetime
    from zoneinfo import ZoneInfo

    import json as _json

    tz = ZoneInfo(await router.store.get("current_timezone", router.settings.timezone_default))
    raw_payload = payload
    if isinstance(payload.get("data"), dict):
        from app.heartbeat.health_import import flatten_export

        metric_names = sorted(
            str(m.get("name", "?")) for m in (payload["data"].get("metrics") or [])
        )
        payload = flatten_export(payload, datetime.now(tz))
        logger.info(
            "Health import parsed %s from metrics %s",
            {k: v for k, v in payload.items()}, metric_names[:40],
        )
    stat_date = payload.get("date") or datetime.now(tz).date().isoformat()
    await router.db.execute(
        "INSERT INTO health_stats (stat_date, weight_kg, sleep_hours, steps, resting_hr, hrv, raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            stat_date,
            float(payload.get("weight_kg") or 0),
            float(payload.get("sleep_hours") or 0),
            int(payload.get("steps") or 0),
            int(payload.get("resting_hr") or 0),
            float(payload.get("hrv") or 0),
            _json.dumps(raw_payload)[:4000],
        ),
    )
    run_km = float(payload.get("run_km") or 0)
    recorded_run = False
    if run_km >= 4.5:  # the daily 5k, with GPS wobble tolerance
        day = datetime.fromisoformat(stat_date).date()
        already = await router.db.fetch_one(
            "SELECT id FROM runs WHERE run_date = ?", (stat_date,)
        )
        if already is None:  # hourly re-posts must not duplicate the run
            await router.streaks.record("run", day)
            await router.db.execute(
                "INSERT INTO runs (run_date, distance_km, duration_min, source) VALUES (?, ?, ?, 'apple_health')",
                (stat_date, run_km, float(payload.get("run_min") or 0)),
            )
            recorded_run = True
    water_recorded = await merge_water_total(
        router.db, stat_date, int(payload.get("water_ml") or 0)
    )
    # The parsed picture rides the response so the export app's log shows
    # exactly what landed — no more blind debugging (the 750ml bug, 5 Aug).
    parsed = {
        k: payload.get(k)
        for k in ("date", "steps", "water_ml", "weight_kg", "sleep_hours",
                  "resting_hr", "hrv", "run_km")
        if payload.get(k) is not None
    }
    # ...and 'status' can answer 'did the last import carry water?' directly.
    try:
        await router.store.set(
            "health_last_import",
            _json.dumps({"at": datetime.now(tz).strftime("%H:%M"), "parsed": parsed}),
        )
    except Exception:
        logger.exception("Health import breadcrumb failed (import itself succeeded)")
    return {"ok": True, "run_recorded": recorded_run, "water_recorded": water_recorded,
            "parsed": parsed}
