# JARVIS — Fable Kickoff Brief (Phase 1 MVP)

*Hand this to Fable to start the build. Source of truth = two companion docs: **Jarvis — Day-One Brain** (the knowledge) and **Jarvis — Build & Launch Plan** (the full spec, incl. locked stack §15 and Daily 12 logic §16). This brief tells Fable exactly what to build first and in what order.*

---

## Role & goal

Build **Jarvis**, a voice-first personal AI chief-of-staff for Paul, operating through **Telegram** with a **web cockpit**. It knows Paul (a private RAG "second brain"), serves a focused **Daily 12** from his Trello, chases him when he stalls, and coaches his training, nutrition and day. Design principle: ADHD-first — shrink work to one next step, create urgency, protect the daily win. Firm-coach personality (comes in hard when Paul avoids), warm on the private sobriety track.

## Locked stack (see Plan §15 — use latest versions at build time)

Claude Opus (brain) + Claude Haiku (cheap tasks) · Deepgram (STT) · ElevenLabs (TTS) · Telegram Bot API (python-telegram-bot) · Python/FastAPI backend · PostgreSQL + pgvector (structured data + second brain) · Cloudflare R2/S3 (document library) · APScheduler (heartbeat) · Render (hosting). Gmail / Google Calendar / Trello APIs. Apple Health via iOS Shortcut webhook.

---

## Phase 1 scope — build these, in this order

**Milestone 1 — Telegram voice loop.**
Bot receives a voice note → Deepgram transcribes → Claude responds in Jarvis's persona → ElevenLabs replies by voice → transcript stored. Text input also works. Smart mix (voice for chat, text for lists). *Done = Paul can hold a natural voice conversation with Jarvis and every message is logged.*

**Milestone 2 — The second brain.**
Postgres + pgvector. Ingestion pipeline: text/voice/uploaded files → chunk → embed → store, tagged by room (You/Companies/Health/Finances/People/Private) and type (STABLE/LIVING/PRIVATE). Load the **Day-One Brain** as seed data. Document library: upload → extract text → embed → retrievable ("show me the BMI contract"). Living records update in place; Private room isolated. *Done = Jarvis answers from Paul's real knowledge and recalls uploaded docs.*

**Milestone 3 — Trello + the Daily 12.**
Connect Trello; tag cards by company + project; implement the exact scoring/selection in Plan §16. Each morning produce 12 (one per live project × 4 companies), hide the rest, unlock a bonus at 12/12. Voice feedback writes back to the board (move/create/assign/date/comment). *Done = Paul gets his 12 each day and updates the board by talking.*

**Milestone 4 — The heartbeat.**
APScheduler jobs in Paul's current timezone: 07:00 brief (hour-by-hour day merging the 12 + calendar + run/workout/meals), ~13:30 nudge, 21:00 review + co-plan tomorrow + private sobriety check-in, 21:00 friendly Kiefer daily-summary email. Hound-mode auto-trigger (missed run or behind by midday) + manual "hound me". Streaks: run, workout, the 12, meals. *Done = the day runs itself and Kiefer gets the nightly note.*

**Milestone 5 — The web cockpit (read-first).**
Render the Progress Cockpit (use the delivered prototype as the design): streaks, the 12, body progress, villa, hour-by-hour, Kiefer note, private sobriety panel. Live data from the backend. *Done = Paul can see everything in one glance.*

**Milestone 6 — Private sobriety track.**
Walled-off encrypted room. Daily check-in, SOS command (immediate supportive flow), trigger-aware nudges around flights/trade shows (from calendar) and lonely stretches. Never enters business context or the Kiefer note. *Done = supportive, private, always-available.*

*(Trainer/chef live coaching, full two-way Gmail, MyFitnessPal, finance/villa tracker = Phase 2 per Plan §9. Apple Health daily Shortcut can land in Phase 1 if quick.)*

---

## What to ask Paul for, as you go (he has the accounts — just needs to authorise)

Telegram bot token (BotFather) · Trello key+token · Google OAuth (Calendar + Gmail) · Claude / Deepgram / ElevenLabs API keys · Render account. Request each at the milestone that needs it, with copy-paste steps.

## Guardrails

Firm-coach persona but never cruel; sobriety always supportive, never shamed, never reported. Everything is Paul's private data — encryption at rest, Private room isolated, auditable/correctable. Build modular and additive (Plan §14) so new capabilities bolt on later.

---

## Copy-paste starter prompt for Fable

> Build Phase 1 of "Jarvis," a voice-first personal AI chief-of-staff for me (Paul), per the attached Build & Launch Plan and Day-One Brain. Use the locked stack in §15 and the Daily 12 logic in §16. Start with Milestone 1 (Telegram voice loop: Deepgram transcription, Claude Opus brain with the Jarvis firm-coach persona, ElevenLabs voice replies, full transcript logging), then proceed through Milestones 2–6 in order. Seed the second brain with my Day-One Brain. Ask me for each API key/authorisation at the moment you need it, with step-by-step instructions. Keep the sobriety track private and supportive, isolated from all business context. Build it modular so I can add features later.
