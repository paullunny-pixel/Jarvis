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
            "INSERT INTO whatsapp_ingest (ts, wa_id, sender, company_tag, kind, message)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso, m.wa_id, m.name[:120], "", m.kind, m.text),
        )
    return {"ok": True, "ingested": len(messages)}


@app.post("/webhook/apple-health")
async def apple_health(request: Request) -> dict:
    """Daily push from the iOS Shortcut: run, sleep, weight, steps, HR/HRV."""
    router: JarvisRouter = request.app.state.router
    payload = await request.json()
    secret = router.settings.apple_health_webhook_secret
    if not secret or not hmac.compare_digest(str(payload.get("secret", "")), secret):
        raise HTTPException(status_code=403, detail="nope")

    from datetime import datetime
    from zoneinfo import ZoneInfo

    import json as _json

    tz = ZoneInfo(await router.store.get("current_timezone", router.settings.timezone_default))
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
            _json.dumps(payload)[:4000],
        ),
    )
    run_km = float(payload.get("run_km") or 0)
    recorded_run = False
    if run_km >= 4.5:  # the daily 5k, with GPS wobble tolerance
        day = datetime.fromisoformat(stat_date).date()
        await router.streaks.record("run", day)
        await router.db.execute(
            "INSERT INTO runs (run_date, distance_km, duration_min, source) VALUES (?, ?, ?, 'apple_health')",
            (stat_date, run_km, float(payload.get("run_min") or 0)),
        )
        recorded_run = True
    water_recorded = await merge_water_total(
        router.db, stat_date, int(payload.get("water_ml") or 0)
    )
    return {"ok": True, "run_recorded": recorded_run, "water_recorded": water_recorded}
