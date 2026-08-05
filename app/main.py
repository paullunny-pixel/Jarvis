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

from fastapi import FastAPI, Header, HTTPException, Request

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

    # The non-skippable gates (run + meds block the day until confirmed).
    gates = GateKeeper(db, Streaks(db))

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
        deepgram=DeepgramClient(settings.deepgram_api_key, model=settings.deepgram_model),
        elevenlabs=elevenlabs,
    )
    router_obj.voice_engine = voice_engine
    router_obj.phone_channel = phone_channel
    if phone_channel is not None:
        phone_channel.brain = router_obj.phone_turn
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
        calendar=router.heartbeat.calendar if router.heartbeat else None,
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
    """READ-ONLY Phase 1 (Paul, 4 Aug): messages arriving on Jarvis's second
    number are ingested for digests and recall. Nothing is ever sent back."""
    import json as _json
    from datetime import datetime, timezone as _tz

    from app.clients.whatsapp_client import parse_webhook, valid_signature

    router: JarvisRouter = request.app.state.router
    if not router.settings.whatsapp_verify_token:
        raise HTTPException(status_code=403, detail="nope")
    body = await request.body()
    if not valid_signature(
        router.settings.whatsapp_app_secret, body,
        request.headers.get("X-Hub-Signature-256", ""),
    ):
        raise HTTPException(status_code=403, detail="nope")
    try:
        payload = _json.loads(body or b"{}")
    except Exception:
        return {"ok": True, "ingested": 0}
    messages = parse_webhook(payload)
    now_iso = datetime.now(_tz.utc).isoformat(timespec="seconds")
    for m in messages:
        await router.db.execute(
            "INSERT INTO wa_direct_ingest (ts, wa_id, sender, company_tag, kind, message)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso, m.wa_id, m.name[:120], "", m.kind, m.text),
        )
    return {"ok": True, "ingested": len(messages)}


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
    return {
        "ok": True,
        "run_recorded": recorded_run,
        "water_recorded": water_recorded,
        "parsed": {
            k: payload.get(k)
            for k in ("date", "steps", "water_ml", "weight_kg", "sleep_hours",
                      "resting_hr", "hrv", "run_km")
            if payload.get(k) is not None
        },
    }
