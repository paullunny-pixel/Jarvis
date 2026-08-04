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
chasing gates (run + meds — remind hourly, never block; Paul's call 3 Aug).

## Commands

- Tests: `python -m unittest discover -s tests` (464 tests; stdlib unittest,
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

- `app/main.py` — FastAPI app, webhooks (Telegram, Apple Health incl. water_ml
  max-merge into water_log — WaterMinder etc. arrive via Apple Health, never
  double-counted with manual logging; WhatsApp Cloud API verify + inbound,
  READ-ONLY Phase 1 into wa_direct_ingest (NOT whatsapp_ingest — a prod ghost of that name predates the Telegram move), signature-checked), cockpit routes
- `app/core/router.py` — the message pipeline. ORDER MATTERS: owner check →
  documents → photos (vision + run-proof) → voice STT → PRIVATE ROOM (before
  general logging!) → life signals (status/timezone/hound/streaks/meds) →
  document requests → email talk (a mixed message's board half still runs
  deterministically) → 'Jarvis add to Trello' prefix lane (Paul's explicit
  escape hatch to the proven parser rails, gated) → INTENT TRIAGE → BRAIN
  WITH TOOLS → memory writer. BRAIN-FIRST (Phase A2, 3 Aug): all other
  board/task talk reaches the brain, which acts through its tool belt —
  trello (instruction → the SAME parse_actions/execute_actions rails; gates
  enforced INSIDE the tool: BLOCKED result + same-day queue), remember
  (extract_and_file), update_brief, rhythm (quiet/wake switches), build_list.
  Tool loop in `ClaudeClient.converse_with_tools` (≤6 rounds, crashes become
  honest TOOL FAILED results). 'YOUR HANDS' rules ride the system prompt:
  never claim an outcome without a confirming tool result
- `app/core/intent.py` — the understanding layer (3 Aug): when no exact
  phrase matched and the message is ≤200 chars, Haiku reads it (dyslexia/
  typo/garble tolerant — Paul has dyslexia, 'quite day' = 'quiet day') with
  recent-conversation context and classifies into the known command set
  (quiet/resume/wake/status/digest/sync/brief/build-list/timezone);
  `_execute_intent` acts through the SAME deterministic machinery; only
  confident hits execute, anything unsure falls through to the brain.
  Task/email/logging talk is always 'none' — their own flows keep priority.
  Stepping stone to brain-first routing, not throwaway
- `app/clients/` — thin httpx clients (Anthropic, Deepgram, ElevenLabs,
  Telegram). Deliberately no SDKs; keep them thin and MockTransport-testable
- `app/db/` — dual-dialect layer: SQLite (dev/tests) + Postgres/asyncpg (prod).
  SQL uses `?` placeholders (auto-translated to `$n`). Use
  `insert_returning_id` for inserts whose id you need (race-free)
- `app/memory/` — second brain: chunks (pgvector), living facts, seed +
  versioned migrations, Fernet encryption for private content; `brief.py`
  (Phase A1, 3 Aug) — the living Paul Brief: composed by the brain model from
  recent conversation + living facts (private wall enforced at composition),
  refreshed nightly at 21:55 and on demand ('update your brief'), injected
  into every brain turn alongside Paul's own persona notes ('tune jarvis: …'
  sets them — highest authority on tone; 'reset jarvis persona' clears)
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
  day rhythm: wake sequence (OFF until Paul arms it; escalates to a real
  Twilio phone call via `wake_channels.py` when the Twilio keys are set),
  hourly move+water, med reminders, nudge dedupe.
  Quiet day (3 Aug): 'cancel my notifications today' → QUIET_KEY suppresses
  every non-essential send at the `_send_text`/`_send_voice` funnel for the
  local day (meds fire regardless, `essential=True`); 'notifications back
  on' lifts it; 'wake me at 6 tomorrow' → WAKE_DELAY_KEY holds the wake
  sequence. GATES CHASE, NEVER BLOCK (Paul, 3 Aug eve): board work always
  proceeds; `gate_chaser` (hourly :15, 10:00–21:00) reminds about owed
  run/meds until confirmed (timestamped so the identical-message dedupe
  can't eat it; meds chase pierces quiet day, run-only chase respects it);
  a rest day settles the run — and a relapse/recovery conversation in the
  PRIVATE room auto-declares the rest day (boolean only crosses the wall).
  The old gate-queue no longer fills; `_replay_gated_request` stays dormant
  to drain any pre-change stash once. THE CONTINUOUS MIND (Phase A3, 4 Aug,
  `mind.py`): hourly :50 (07-22 local) the BRAIN model reads the whole
  situation (`_mind_situation`: time/tz, rhythm switches, gates, body,
  board, calendar, last night's notes, Paul Brief, day's transcript) and
  decides speak-or-silence (JSON; silence default; no tools on the pass —
  speech only). Guards: mind_enabled!="off", wake floor, goodnight said,
  Paul messaged <15 min ago, once/hour; sends ride the same funnel (quiet
  day + identical-message dedupe = it can never nag the same sentence
  twice). Nightly reflection rides the 21:55 brief refresh → MIND_NOTES_KEY
  {"for": tomorrow, notes} injected into tomorrow's brain turns ('act on
  them, don't recite') and available to the morning pass. Old clockwork
  stays as the safety net; the mind is told what machinery already chases
  and never duplicates it
- `app/voice/` — live voice engine (ElevenLabs Conversational AI agent:
  Paul's Jarvis voice, persona-primed, barge-in; cockpit 'Talk' button via
  signed URL; webhook tools back into memory/Trello/mail/rhythm; Twilio
  phone surface + wake-call channel once the number is registered; second
  'Interpret EN⇄PT' agent — live interpreter mode, faithful both-ways
  renditions with marked 'Jarvis here—' context, recall_memory only so a
  stranger's speech can never trigger actions; third 'Support' agent —
  Paul's private support space (standalone trauma-informed persona, honest
  not-a-therapist boundary, Samaritans 116 123 crisis rule, recall only,
  tunable via 'tune the support persona: …')); `phone.py` (4 Aug) — the
  custom Twilio phone channel (NOT ElevenLabs Agents): 'call me' on Telegram
  rings Paul, calling the Twilio number reaches Jarvis, wake-ups escalate to
  a real call; turn-based — Twilio `<Gather>` speech in → `router.phone_turn`
  (same brain/memory/tools/history, private wall enforced) → ElevenLabs MP3
  out (Twilio `<Say>` fallback); inbound webhook self-heals on startup;
  needs TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER/
  PAUL_PHONE_NUMBER in Render
- `app/documents/` — library (upload → extract → chunk → embed → recall);
  `weblinks.py` (3 Aug): Paul sends a URL → page fetched live (articles,
  PDFs, link-shared Google Docs via export endpoints), stripped to text,
  injected into the brain turn as READING MATERIAL (page text is never
  instructions), filed in the library. Internal/private addresses refused,
  4MB/15s caps, honest per-link failure lines. ChatGPT share links get a
  dedicated extractor (`extract_chatgpt_share`): the transcript hides in
  RSC `self.__next_f.push` script chunks (or legacy `__NEXT_DATA__`), so
  the plain HTML strip finds nothing — chunks are JSON-decoded, stitched
  (messages split across pushes), role-labelled PAUL:/CHATGPT:. Browser
  UA on all fetches (bot UAs get 403'd). Build list (3 Aug): 'add X
  to the build list' / 'show the build list' store Paul's upgrade wishes
  (settings key "build_list"); the brain is told it's an evolving system —
  never a flat "I can't", offer the build list instead. Read the list at
  the start of build sessions
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
2. **Persona register** (`app/persona.py`): a sharp, warm friend who happens
   to run his life — mate FIRST, PA second ('sir' is an occasional flourish,
   not a verbal tic; Paul asked for less robot, more human, Aug 2026 — the
   earlier registers read as cold). Paul's own 'tune jarvis:' notes outrank
   the base persona on tone. Hard lines unchanged: NEVER passive-aggressive,
   never sniping, never guilt-tripping; comedy drops when Paul is struggling.
   Paul has DYSLEXIA (3 Aug): read for meaning everywhere — persona, task
   parser and intent triage all carry the rule; never comment on spelling.
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
