# JARVIS — Keys & Access Checklist

*The credentials Jarvis needs. Paul already has the underlying accounts — this is about creating keys / clicking "authorise". Fable will ask for each at the milestone that needs it. Keep these secret; they go in the host's secret manager, never in code.*

---

## New sign-ups (exist only because of Jarvis)
- [ ] **Anthropic (Claude)** — API key for the brain (Opus + Haiku).
- [ ] **Deepgram** — API key for speech-to-text.
- [ ] **ElevenLabs** — API key + chosen "Jarvis" voice ID for text-to-speech.
- [ ] **Render** — account to host the backend + Postgres (pgvector).
- [ ] **Cloudflare R2** (or AWS S3) — bucket + keys for the document library.

## Authorise your existing accounts
- [ ] **Telegram** — create the bot via **BotFather**, get the bot token (2-minute chat).
- [ ] **Trello** — API key + token (authorise your Master Board).
- [ ] **Google** — OAuth for **Calendar** and **Gmail** (personal + each work inbox you want connected).
- [ ] **Apple Health** — set up the daily **iOS Shortcut** that pushes your stats to Jarvis's webhook (Fable gives you the shortcut).

## Environment variables (Fable fills these in)
```
ANTHROPIC_API_KEY=
DEEPGRAM_API_KEY=
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
TELEGRAM_BOT_TOKEN=
TRELLO_KEY=
TRELLO_TOKEN=
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
DATABASE_URL=            # Postgres (Render)
R2_ACCESS_KEY=
R2_SECRET_KEY=
R2_BUCKET=
APPLE_HEALTH_WEBHOOK_SECRET=
```

*You don't need to gather these up front — Fable prompts you for each one, with steps, when it reaches the milestone that uses it.*
