# Jarvis — Paul's voice-first AI chief-of-staff

Read `docs/` before making design decisions — especially
`Jarvis-Build-and-Launch-Plan.md` (§15 locked stack, §16 Daily 12 scoring),
`Jarvis-System-Prompt.md` (persona) and `Jarvis-Data-Model.md`. Phase 1 is
complete and LIVE in production; Paul (non-technical) talks to this bot daily.

## What this is

A Telegram bot (webhook mode) + FastAPI backend on Render with managed
Postgres (pgvector). Voice notes → Deepgram STT → Claude Opus (Jarvis persona)
→ ElevenLabs TTS replies. Second brain (rooms/types + living facts + document
library), Trello-driven "Today's Focus" task engine (né Daily 12), APScheduler heartbeat
(briefs/nudges/reviews/Kiefer email), web cockpit, private sobriety track,
non-skippable gates (run + meds).

## Commands

- Tests: `python -m unittest discover -s tests` (365 tests; stdlib unittest,
  NOT pytest — keep it that way. Only `requirements.txt` is needed; two PDF
  tests also use `reportlab` and skip automatically when it isn't installed)
- No local run needed for most work; local dev uses SQLite automatically
  (`DATABASE_URL` empty) and `python -m scripts.run_polling` for a live bot.

## Deploy flow

Push to `main` on GitHub → Render auto-deploys (`render.yaml` blueprint:
web service + Postgres). Secrets live ONLY in Render's Environment tab —
never commit keys, tokens, or `.env`. Schema changes are additive and
idempotent (`app/db/schema.py` runs on every startup; `IF NOT EXISTS` only).

## Architecture map

- `app/main.py` — FastAPI app, webhooks (Telegram, Apple Health), cockpit routes
- `app/core/router.py` — the message pipeline. ORDER MATTERS: owner check →
  documents → photos (vision + run-proof) → voice STT → PRIVATE ROOM (before
  general logging!) → life signals (status/timezone/hound/streaks/meds) →
  document requests → email talk (never swallows a mixed message's board
  half) → gated task-talk → brain conversation → memory writer
- `app/clients/` — thin httpx clients (Anthropic, Deepgram, ElevenLabs,
  Telegram). Deliberately no SDKs; keep them thin and MockTransport-testable
- `app/db/` — dual-dialect layer: SQLite (dev/tests) + Postgres/asyncpg (prod).
  SQL uses `?` placeholders (auto-translated to `$n`). Use
  `insert_returning_id` for inserts whose id you need (race-free)
- `app/memory/` — second brain: chunks (pgvector), living facts, seed +
  versioned migrations, Fernet encryption for private content
- `app/daily12/` — Trello sync, §16 scoring (pure functions in `scoring.py`),
  voice-command parsing, board write-back. Master Board system (Trello brief,
  30 Jul): two-axis labels (Domain: Personal/Prodermis/Derma/Business Ops ·
  Flags: Urgent/£ Money/Waiting On/Delegate) self-healed daily by
  `ensure_labels`; new cards get domain+flags+member+description; exact-title
  duplicates refused; done cards route to the newest 'Done Week …' list;
  done/blocked/waiting lists matched by shape, not exact name
- `app/mail/` — Paul's inboxes (IMAP/SMTP, Google app passwords, no OAuth):
  triage/read across all accounts, whole-mailbox research ('read all my
  emails from BMI and summarise...' — Gmail X-GM-RAW search + brain-model
  write-up; drafts must be finished words, placeholder bodies refused),
  drafts by voice IN PAUL'S OWN STYLE
  ('learn my style' distills it from his voice-note transcripts, Telegram
  messages and his own work-group lines — NOT his emails, many AI-written;
  private-exchange markers are excluded); a send fires
  ONLY after Paul confirms a read-back draft; recipient-less drafts are
  held, and a stale draft (45 min) needs one fresh read-back confirm
- `app/heartbeat/` — scheduler jobs, streaks, gates, ICS calendar, SMTP email,
  day rhythm: wake sequence (OFF until Paul arms it; channel seam for Twilio
  in `wake_channels.py`), hourly move+water, med reminders, nudge dedupe
- `app/voice/` — live voice engine (ElevenLabs Conversational AI agent:
  Paul's Jarvis voice, persona-primed, barge-in; cockpit 'Talk' button via
  signed URL; webhook tools back into memory/Trello/mail/rhythm; Twilio
  phone surface + wake-call channel once the number is registered; second
  'Interpret EN⇄PT' agent — live interpreter mode, faithful both-ways
  renditions with marked 'Jarvis here—' context, recall_memory only so a
  stranger's speech can never trigger actions; third 'Support' agent —
  Paul's private support space (standalone trauma-informed persona, honest
  not-a-therapist boundary, Samaritans 116 123 crisis rule, recall only,
  tunable via 'tune the support persona: …'))
- `app/private/` — sobriety track (walled off)
- `app/cockpit/` — dashboard (design source: `docs/prototype-progress-cockpit.html`).
  Locked (31 Jul): the link alone serves NO data — password set via Telegram
  ('set cockpit password …', PBKDF2 hash, redacted from the message log),
  signed 30-day session cookie, 'log out the cockpit everywhere' rotates the
  session key; page/data/voice-url all gate through `app/cockpit/auth.py`

## Hard invariants — never break these

1. **The private wall.** Private-room/PRIVATE-type content never enters
   business context, the general message log (redacted `[private exchange]`
   markers only), or any outbound report. The Kiefer note composes ONLY from
   task/streak data and passes `assert_no_private_content` before sending.
   Sobriety rows are encrypted with `PrivateBox` (PRIVATE_ROOM_KEY env).
2. **Persona register** (`app/persona.py`): composed, courteous British AI
   aide who is genuinely good company — warm, playful, a real sense of humour
   (Paul asked for this, July 2026; the earlier formal register read as cold).
   Hard lines unchanged: NEVER passive-aggressive, never sniping, never
   guilt-tripping; comedy drops when Paul is struggling.
3. **§16 scoring weights** (0.35/0.25/0.25/0.15) are spec-locked; tunable via
   arguments, not by editing constants casually.
4. **Negation-aware logging:** phrase matchers only nominate; Haiku confirms
   done/not_done before any streak/gate/board mutation ("I have NOT done my
   run" must never log a run — there's a regression suite, keep it green).
5. **Every reply degrades gracefully** — TTS failure → text; Claude failure →
   honest fallback line; Trello failure → local state + apology. The bot must
   always answer.
6. **Self-knowledge:** integration ground truth is injected into every brain
   turn (`_integration_status`) and "status" runs live checks — the model must
   never guess about its own wiring.

## Working with Paul

He speaks in feature ideas and screenshots, not specs. Small tweaks (tone,
times, thresholds) are "Level 1" — often just prompt/config edits. New
capabilities are "Level 2" — keep them modular bolt-ons per Plan §14. Pending
upgrade list: calendar event alerts (deferred by Paul); optional Telegram
userbot for DM coverage (MTProto, only if Paul asks). Org ingestion runs on
TELEGRAM now (the org moved there; WhatsApp route removed): the bot sits in
work groups (privacy mode OFF in BotFather), ingests every message —
voice notes transcribed — tagged group→company, read-only, summarised via
'catch me up on X'; 'map the <name> group to <company>' remaps on the fly. Phase 2 (per Plan §9): email is live (read/draft/confirmed
send); calendar reads multiple ICS feeds — write-back (event creation via
OAuth) still pending; then Apple Health + MyFitnessPal, finance/villa
tracker, live workout coaching.
