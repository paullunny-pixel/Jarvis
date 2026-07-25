# Jarvis — Paul's voice-first AI chief-of-staff

Phase 1 build, per `docs/Jarvis-Build-and-Launch-Plan.md` (locked stack §15, Daily 12 logic §16),
`docs/Jarvis-System-Prompt.md` (persona) and `docs/Jarvis-Data-Model.md` (schema).

## Milestone status

| # | Milestone | Status |
|---|---|---|
| 1 | Telegram voice loop (Deepgram → Claude Opus → ElevenLabs, full transcript log) | ✅ Built & tested |
| 2 | Second brain (pgvector, rooms, Day-One Brain seed, document library) | ✅ Built & tested |
| 3 | Trello + the Daily 12 (§16 scoring) | ✅ Built & tested |
| 4 | Heartbeat (07:00 brief · 13:30 nudge · 21:00 review + Kiefer note · hound mode · streaks) | ✅ Built & tested |
| 5 | Web cockpit (Progress Cockpit design) | ✅ Built & tested |
| 6 | Private sobriety track (walled-off, encrypted) | ✅ Built & tested |

## How it works (Milestone 1)

```
Telegram voice note ──▶ Deepgram (nova-3) ──▶ transcript
                                                 │
              message log (Postgres/SQLite) ◀────┤
                                                 ▼
                              Claude Opus (Jarvis persona + conversation history)
                                                 │
                     smart mix policy: voice for chat, text for lists/links
                        │                                        │
                        ▼                                        ▼
       ElevenLabs TTS ──▶ Telegram voice note          Telegram text message
                          (transcript underneath)
```

- **Only Paul can talk to it** — the bot locks onto the first chat that messages it
  (or set `TELEGRAM_OWNER_CHAT_ID` explicitly). Strangers get one polite line.
- **Every message both directions is logged** to the `messages` table — searchable, replayable.
- **Voice replies carry their transcript** (caption or follow-up message) per the spec.
- If TTS ever fails, Jarvis falls back to text — he always answers.

## Deploy (Render)

1. Push this repo to GitHub (private).
2. Render → **New + → Blueprint** → select the repo. `render.yaml` provisions the
   web service + Postgres together.
3. Set the environment variables it asks for (see `.env.example`).
4. After first deploy, set `PUBLIC_URL` to the service URL (e.g. `https://jarvis-xxxx.onrender.com`).
   On restart Jarvis registers its own Telegram webhook.
5. Message the bot — you're talking to Jarvis.

## Run locally (developers)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python -m scripts.run_polling   # long-polls Telegram; SQLite; no public URL needed
```

## Tests

```bash
python -m unittest discover -s tests
```

162 tests cover the reply policy, all service clients (mocked at the HTTP
layer), the second brain (rooms, privacy wall, encryption, seeding), the exact
§16 Daily 12 scoring and selection, Trello write-back, streaks, heartbeat jobs
(incl. the private-content guard on the Kiefer note), the cockpit data layer,
the private sobriety track, and the full end-to-end router flows. An
independent adversarial review pass ran over the whole codebase before v1;
all findings are fixed.

## Engineering notes

- **Locked stack honoured:** Python/FastAPI · Postgres (SQLite fallback for dev/tests) ·
  Claude Opus (`claude-opus-5`) + Haiku (`claude-haiku-4-5`) · Deepgram `nova-3` ·
  ElevenLabs · Telegram Bot API · Render. Models are env-overridable for future releases.
- The Telegram layer speaks the official Bot API directly via `httpx` (webhook mode in
  production, polling for local dev). It's a deliberate thin client: single-user bot,
  fully unit-testable, no framework magic. Swappable without touching `app/core`.
- No audio transcoding is needed anywhere: Telegram voice notes are OGG/Opus which
  Deepgram ingests natively; ElevenLabs returns MP3 which Telegram accepts for voice.
- **Modular & additive (Plan §14):** milestones bolt on as new packages
  (`app/memory`, `app/daily12`, `app/heartbeat`, `app/cockpit`, `app/private`)
  behind the same router — no rebuilds.
