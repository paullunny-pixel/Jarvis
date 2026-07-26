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
    deepgram_model: str = "nova-3"
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
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""
    r2_bucket: str = ""
    r2_endpoint: str = ""
    apple_health_webhook_secret: str = ""
    private_room_key: str = ""   # encryption key for the sobriety room (Milestone 6)

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
    def effective_webhook_secret(self) -> str:
        if self.webhook_secret:
            return self.webhook_secret
        # Derive a stable, unguessable path segment from the bot token.
        import hashlib

        return hashlib.sha256(f"jarvis:{self.telegram_bot_token}".encode()).hexdigest()[:32]


@lru_cache
def get_settings() -> Settings:
    return Settings()
