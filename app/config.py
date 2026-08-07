"""Jarvis configuration — all secrets and tunables come from environment variables.

On Render these are set in the service's Environment tab (or via render.yaml
`envVars`). Locally they load from a `.env` file. Nothing secret lives in code.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core services (Milestone 1) ---
    anthropic_api_key: str = ""
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "ocxtQ2JlkrCrim3Wvtji"  # Paul's chosen Jarvis voice; swap any time
    telegram_bot_token: str = ""

    # Only Paul may talk to Jarvis. Set after first contact (Jarvis tells you the id).
    # 0 = accept the first human who messages and lock onto them.
    telegram_owner_chat_id: int = 0

    # --- Models (latest as of July 2026; override via env when new ones ship) ---
    brain_model: str = "claude-opus-5"          # judgement, coaching, conversation
    fast_model: str = "claude-haiku-4-5"        # routing, parsing, classification
    # Live phone calls trade Opus's depth for Sonnet's speed — same persona,
    # memory and tools; seconds shorter per spoken turn (Paul, 5 Aug: the lag
    # was the complaint, not the answers). Override via PHONE_MODEL.
    phone_model: str = "claude-sonnet-5"
    deepgram_model: str = "nova-3"
    deepgram_language: str = "en-GB"    # Paul's accent — tuned 5 Aug
    elevenlabs_model: str = "eleven_multilingual_v2"

    # --- Second brain (Milestone 2) ---
    voyage_api_key: str = ""                    # embeddings (Anthropic's partner); free tier is plenty
    embed_model: str = "voyage-3.5-lite"
    embed_dim: int = 1024
    memory_enabled: bool = True

    # --- Storage ---
    database_url: str = ""       # Render Postgres URL; empty => local SQLite file
    sqlite_path: str = "jarvis.db"

    # --- Web / webhook ---
    public_url: str = ""         # e.g. https://jarvis-xyz.onrender.com — set on Render
    webhook_secret: str = ""     # random string; auto-derived from bot token if empty

    # --- Behaviour ---
    timezone_default: str = "Europe/London"
    history_messages: int = 40   # how much recent conversation the brain sees
    max_reply_voice_seconds: int = 90

    # --- Heartbeat (Milestone 4) ---
    heartbeat_enabled: bool = True
    gmail_address: str = ""            # sends the nightly Kiefer note (app password, no OAuth)
    gmail_app_password: str = ""
    kiefer_email: str = ""
    calendar_ics_url: str = ""         # Google Calendar 'secret address in iCal format';
                                       # several calendars = several URLs, comma-separated

    # --- Email inboxes (Phase 2) — IMAP/SMTP via Google app passwords ---
    email1_address: str = ""
    email1_app_password: str = ""
    email2_address: str = ""
    email2_app_password: str = ""
    email3_address: str = ""
    email3_app_password: str = ""
    email4_address: str = ""
    email4_app_password: str = ""

    # --- Phase 1 later milestones (requested when needed) ---
    trello_key: str = ""
    trello_token: str = ""
    # Which boards Jarvis works from: comma-separated names, or "all" for
    # every open board. Paul's call (July 2026): Master Board only while the
    # board gets knocked into shape.
    trello_boards: str = "Master Board"
    # Today's Focus sourcing (Master Update §2): the two source lists.
    trello_today_list: str = "Paul Today"
    trello_personal_list: str = "Paul Personal"
    focus_per_company: int = 3
    focus_personal_max: int = 3
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Withings smart scale (7 Aug build slice) — same OAuth2 shape as Google
    # Calendar above; dormant (.client.configured False) until both are set.
    withings_client_id: str = ""
    withings_client_secret: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""
    r2_bucket: str = ""
    r2_endpoint: str = ""
    apple_health_webhook_secret: str = ""
    desktop_app_secret: str = ""   # Mac app auth (falls back to a derived token)
    private_room_key: str = ""   # encryption key for the sobriety room (Milestone 6)

    # --- WhatsApp Business Cloud API (official; Jarvis's own second number).
    # PHASE 1 IS READ-ONLY (Paul, 4 Aug): sending stays off until he flips it.
    whatsapp_verify_token: str = ""      # webhook verification handshake (Meta dashboard)
    whatsapp_app_secret: str = ""        # X-Hub-Signature-256 validation
    whatsapp_access_token: str = ""      # Graph API token (send seam, unused in Phase 1)
    whatsapp_phone_number_id: str = ""   # the number's id on Meta (send seam)
    whatsapp_sending_enabled: bool = False
    # Part 1 (7 Aug): Paul's OWN WhatsApp number (digits only, e.g. 447700900123).
    # Jarvis's number receives messages from anyone; only this one routes to
    # the brain and gets a reply — everyone else is silently ignored. Unset =
    # the old Phase 1 read-only ingest behaviour holds (nothing routes, nothing sends).
    whatsapp_owner_number: str = ""

    # --- Live voice (Build Slice: Voice Access) — rides on the ElevenLabs key ---
    # Phone-call surface: the Twilio number registered in ElevenLabs (its
    # phone_number_id) + Paul's own number for outbound wake calls.
    elevenlabs_phone_number_id: str = ""
    paul_phone_number: str = ""

    # --- Day rhythm (Master Update §§4-6) ---
    water_target_ml: int = 2500
    # Intelligent water pacing (5 Aug): ahead of the hourly curve = SILENCE;
    # only falling a full hour behind earns a line. 200ml/hr over a ~15h
    # waking day ≈ 3L — right for indoor Dubai; run/heat days drink harder.
    water_pace_ml_per_hour: int = 200
    water_heat_pace_ml: int = 275         # run days + declared heat days
    # Smart move reminder (7 Aug build slice): pace-vs-goal exactly like
    # water — the Move ring's kcal goal spread across a waking day, silent
    # when on track. Apple doesn't export the GOAL itself (only what he's
    # burned), so it's a tunable here, not read from Health.
    move_goal_kcal: int = 500
    move_active_hours: float = 14.0

    # Watch-wearing chaser (7 Aug build slice): no heart-rate sample for
    # this many minutes during the waking day = watch off wrist. Standing
    # down (Paul's word, e.g. "I'm at dinner") pauses calls for this long.
    watch_gap_minutes: int = 60
    watch_standdown_minutes: int = 60

    # The custom Twilio voice channel (built 4 Aug): 'call me', inbound calls
    # and the wake-up escalation all ride these + paul_phone_number.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    # Realtime upgrade (7 Aug): inbound calls stream to the live ElevenLabs
    # agent (interruptible). Off switches every call back to turn-based.
    phone_realtime_enabled: bool = True

    # --- Zoom meetings, Layer A (6 Aug): Server-to-Server OAuth app on
    # Paul's Zoom account (scopes meeting:write, user:read). All three set →
    # 'start a new Zoom meeting' works.
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    zoom_user_email: str = ""   # optional: pin the meeting host (else account owner)

    # Bedtime protection OFF (Paul, 5 Aug 23:07: 'based on my workload it's
    # not working — kill that for now'). The machinery stays built; flip
    # BEDTIME_ENABLED=true when he wants the 22:30 rule back.
    bedtime_enabled: bool = False

    # Run reminders OFF (Paul, 6 Aug: 'cancel the reminder for the run — I
    # have high blood pressure and need to work on that first'). Runs still
    # LOG if he does one; nothing chases, calls or guilts. Machinery stays
    # built — flip RUN_REMINDERS_ENABLED=true when he's ready.
    run_reminders_enabled: bool = False

    # --- Chase engine (Phase 2, 5 Aug) ---
    # DORMANT until Paul switches it on (CHASE_ENABLED=true): built and
    # tested, but the tick never fires — his call, 5 Aug eve.
    chase_enabled: bool = False
    # Dry-run ON by default (spec §13): every would-be nudge is shown to Paul
    # instead of sent. Flip CHASE_DRY_RUN=false after the test sequence.
    chase_dry_run: bool = True

    # --- Voice intake (Phase 3, 5 Aug) ---
    intake_enabled: bool = True
    intake_confirm: bool = True   # confirm-before-write until accuracy earns trust (§9)

    # --- Wake & Hydrate v2 (Paul's brief, 5 Aug) — all tunable via env ---
    wake_response_wait: int = 5           # listen window before Jarvis leads (he's asleep)
    max_call_seconds: int = 75            # one call's motivation run, then re-call
    wake_callback_interval_min: int = 2   # minutes between callbacks until proof
    test_callback_interval_sec: int = 30  # 'test alarm' iterates fast
    test_max_min: int = 10                # a drill self-expires — never chases all night
    hydration_ml: int = 500
    electrolytes: bool = True
    hydrate_grace_min: int = 5            # minutes before the water re-nudge
    max_escalation_min: int = 90          # welfare backstop: total silence limit
    welfare_contact: str = ""             # optional email to notify on backstop
    wake_code_refresh_min: int = 5        # cockpit anti-cheat code rotation

    # --- The War Room (7 Aug brief): three seats, three vendors, two tiers.
    # Dormant until BOTH new keys exist — Anthropic's is already in place
    # for Seat A and the chair. Model ids are config, never hard-coded, so
    # the lineup can move without a release; the model-availability guard
    # in app/warroom/ falls back and flags rather than failing silently.
    openai_api_key: str = ""
    google_ai_api_key: str = ""
    warroom_full_model_a: str = "claude-fable-5"
    warroom_full_model_b: str = "gpt-5.6-sol"
    warroom_full_model_c: str = "gemini-3.1-pro-preview"
    warroom_quick_model_a: str = "claude-sonnet-5"
    warroom_quick_model_b: str = "gpt-5.6-terra"
    warroom_quick_model_c: str = "gemini-3.6-flash"
    warroom_full_effort_b: str = "ultra"   # Seat B (OpenAI) is the only seat with a effort knob
    warroom_full_budget_usd: float = 5.0
    warroom_quick_budget_usd: float = 0.50
    warroom_monthly_ceiling_usd: float = 100.0
    warroom_escalate_value_gbp: float = 10000.0

    # --- GPS awareness + daily working memory (7 Aug brief) §3's leave-now
    # alerts need a maps/traffic key — flagged as needed, not yet supplied.
    # Everything else in the brief (location history, named places,
    # situational confirm, geofenced tasks, daily working memory) needs no
    # new key and runs on the existing GPS pipe.
    google_maps_api_key: str = ""

    def email_accounts(self) -> list[tuple[str, str]]:
        """(address, app_password) for every configured inbox slot."""
        slots = [
            (self.email1_address, self.email1_app_password),
            (self.email2_address, self.email2_app_password),
            (self.email3_address, self.email3_app_password),
            (self.email4_address, self.email4_app_password),
        ]
        return [(a.strip(), p.strip()) for a, p in slots if a.strip() and p.strip()]

    @property
    def effective_cockpit_secret(self) -> str:
        import hashlib

        return hashlib.sha256(f"jarvis-cockpit:{self.telegram_bot_token}".encode()).hexdigest()[:24]

    @property
    def effective_phone_secret(self) -> str:
        """Unguessable path segment for the Twilio voice + audio endpoints."""
        import hashlib

        return hashlib.sha256(f"jarvis-phone:{self.telegram_bot_token}".encode()).hexdigest()[:32]

    @property
    def effective_desktop_secret(self) -> str:
        """Auth token for the Mac desktop app's endpoints. DESKTOP_APP_SECRET
        env wins; otherwise derived from the bot token so it works with zero
        config — Paul asks 'desktop setup' on Telegram to get it."""
        if self.desktop_app_secret:
            return self.desktop_app_secret
        import hashlib

        return hashlib.sha256(f"jarvis-desktop:{self.telegram_bot_token}".encode()).hexdigest()[:32]

    @property
    def effective_voice_tool_secret(self) -> str:
        """Auth token in the live agent's tool webhook URLs."""
        import hashlib

        return hashlib.sha256(f"jarvis-voice:{self.telegram_bot_token}".encode()).hexdigest()[:32]

    @property
    def effective_webhook_secret(self) -> str:
        if self.webhook_secret:
            return self.webhook_secret
        # Derive a stable, unguessable path segment from the bot token.
        import hashlib

        return hashlib.sha256(f"jarvis:{self.telegram_bot_token}".encode()).hexdigest()[:32]


@lru_cache
def get_settings() -> Settings:
    return Settings()
