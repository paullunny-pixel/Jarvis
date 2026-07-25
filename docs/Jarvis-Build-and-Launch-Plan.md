# JARVIS — Build & Launch Plan

*The master spec Fable builds from. Prepared with Paul. Companion to: the Day-One Brain, and the three prototypes (Command Deck, Daily 12, Progress Cockpit).*

---

## 1. What we're building

A voice-first AI chief-of-staff that runs Paul's life from inside **Telegram**, backed by a **web cockpit**. It knows everything about him (a private "second brain"), serves him a focused **Daily 12** from his real Trello, chases him when he freezes, and plays three more roles — **personal trainer, chef/nutritionist and daily planner**. It runs a light accountability game (streaks + a friendly nightly note to Kiefer) and holds a separate, private, supportive **sobriety** track. Built for an ADHD brain: shrink the mountain to one step, create urgency, protect the daily win.

---

## 2. Architecture at a glance

Five parts working together:

1. **The brain** — a frontier reasoning model with a fixed "Jarvis" personality prompt (firm coach; comes in hard when Paul is avoiding).
2. **Ears & mouth** — speech-to-text (voice notes in) and premium text-to-speech (voice replies out), every message transcribed for a searchable log.
3. **The second brain** — a private knowledge base (vector database + retrieval) holding the Day-One Brain and everything Paul adds. Unlimited storage; only the relevant slice loads per task.
4. **The connectors** — secure links to Trello, Google Calendar, Gmail, Apple Health, MyFitnessPal and finance data.
5. **The heartbeat** — an always-on scheduler that runs the day even when Paul is silent (briefs, nudges, reviews, the Kiefer email, sobriety check-ins), timezone-aware.

---

## 3. The second brain — storage, access & update design

**Rooms (namespaces):** You · Companies (×4) · Health · Finances · People · Private. Retrieval can be scoped to a room ("only Derma EU regulatory").

**Three storage types, handled differently:**
- **`STABLE`** (names, roles, brand rules, SOPs) → stored as documents, chunked and embedded for semantic recall.
- **`LIVING`** (weight, villa balance, deadlines, targets, who-owes-what) → stored as **structured records updated in place**, so Jarvis never quotes stale or contradictory facts.
- **`PRIVATE`** (sobriety, health meds, family) → **separate encrypted store**, excluded from business context and all outbound reports.

**How it updates:** anything Paul says or drops in is auto-filed to the right room and type; changed facts overwrite the record (with history kept) rather than piling up duplicates. Paul can audit ("what do you know about X?") and correct at any time.

**Retrieval flow:** on each task/message, Jarvis pulls the most relevant chunks + current living records for the active scope, then acts.

---

## 4. Recommended tech stack (the "excellent" tier)

- **Reasoning model (brain):** a frontier model (e.g. Claude-class) for judgement, coaching and voice.
- **Speech-to-text:** best-in-class STT for voice notes (Whisper-class or better).
- **Text-to-speech:** a premium natural voice (e.g. ElevenLabs-class) for Jarvis's replies.
- **Messaging:** **Telegram Bot API** (free, voice in/out, rich messages).
- **Backend:** a small always-on service (Node/Python) orchestrating the brain, tools and schedule.
- **Database:** Postgres for structured/living data; **pgvector** (or a managed vector DB) for the knowledge base.
- **Scheduler:** a reliable job runner for the proactive heartbeat.
- **Hosting:** managed cloud (e.g. Fly.io / Render / a small cloud VM) — always-on, no server for Paul to babysit.

---

## 5. Surfaces

- **Telegram** — the everyday interface. Voice-first both ways, transcripts underneath, push notifications. Zero technical skill to use.
- **Web cockpit** — the visual home base (prototyped): streaks, the Daily 12, body progress, the villa, the hour-by-hour day, the Kiefer note, the private sobriety panel. Read-first, with light controls.

---

## 6. Integrations & connections checklist

| Connect | Purpose | Notes |
|---|---|---|
| **Trello** | Read all cards; tag each by **company + project**; serve the Daily 12; write Paul's voice feedback back to cards | Team keeps Trello unchanged; Jarvis sits on top |
| **Google Calendar** | Read/write events; build the hour-by-hour day | |
| **Gmail** | Triage, flag, draft replies by voice | |
| **Apple Health** | Run pace, workouts, HR/HRV, sleep, steps | Via an **Apple Shortcut** daily push (no app needed for MVP) |
| **MyFitnessPal** | Macro/meal logging | Or Jarvis logs meals directly |
| **Finance** | Villa tracker, regular commitments, freedom-number progress | Manual entry first; open-banking later if wanted |
| **Telegram** | The chat channel | BotFather setup |

---

## 7. The Daily 12 engine (logic)

4 companies × 3 live projects × 1 task each = **12/day**. Selection each morning weights: **deadlines**, **money-movers**, **who's-waiting-on-Paul**, and **what he's dodged longest** — while hiding the other ~50 cards to kill overwhelm. Clear all 12 → **unlock a bonus task**. Voice feedback updates Trello (tick, move, create, reassign, set dates). Streak counts: run, workout, the 12, meals.

---

## 8. Proactive heartbeat (schedules)

- **06:30** — protect the daily 5km run (keystone).
- **07:00** — morning brief (hour-by-hour day, the 12, calendar, money-critical, one push), gentle in the first hour.
- **Midday** — check-in / nudge; escalates to **hound mode** on a bad day (auto-detected via missed run + low completion, or on command "hound me").
- **During workouts** — live coaching (Paul reports lifts, Jarvis logs, progresses, calls the next set).
- **21:00** — evening review + **co-plan tomorrow** hour-by-hour + roll-over + private sobriety check-in; then the **friendly daily note to Kiefer**.
- **Always** — timezone auto-switch (UK ↔ Dubai); trigger-aware sobriety support around flights, trade shows, lonely stretches.

---

## 9. Phased build roadmap

- **Phase 1 — Core loop (MVP, still excellent):** Telegram voice Jarvis + second brain + Daily 12 on Trello + morning/evening rhythm + streaks + Kiefer note + private sobriety check-in/SOS + the read-only cockpit.
- **Phase 2 — Deep integrations:** two-way Calendar & Gmail, Apple Health + MyFitnessPal, finance/villa tracker, live workout coaching.
- **Phase 3 — Intelligence & polish:** pattern-aware proactivity, full interactive cockpit, richer trigger-spotting.
- **Phase 4 — Native app:** wrap as iOS/Android.

---

## 10. Hosting & running costs

Always-on managed hosting keeps Jarvis awake. At the "excellent" tier (frontier model, premium voice both ways, live integrations), realistic running cost is **~£120–250/month**, voice being the biggest variable. Building is via Fable; hosting/keys are the ongoing cost.

---

## 11. What Paul provides — accounts & keys checklist *(no coding — clicking & pasting)*

- Create the **Telegram bot** (2-min chat with BotFather).
- Sign up for the **AI model**, **voice**, and **hosting** services (get API keys — Fable pastes them in).
- **Authorise** Trello, Google (Calendar + Gmail), and set up the **Apple Health Shortcut**.
- Provide bank/finance access preference (manual vs open-banking).
- *(Optional)* a freelance developer for a clean one-off deployment if Paul would rather not touch hosting at all.

---

## 12. Day-One memory load checklist *(to give the brain a head start)*

Gather and drop in (by voice or upload) when ready:
- Company docs: brand guidelines, SOPs, supplier contracts/terms, product lists & registrations.
- Financials: villa SOA (done), payment schedule, bank list, regular commitments.
- Health: body-scan + blood-test results once done, training history, supplement/med schedule.
- People: contact details as they come up.
- Anything in Google Drive / Notion / folders worth bulk-importing.

---

## 13. Action items Jarvis already surfaced *(live, ready to chase on day one)*

- ⚠️ **This weekend:** 3 doctor surveys for **BMI**.
- 🏠 **By 20 Oct:** move out of the **Nottingham** rental — clear/dispose of all belongings first (countdown).
- 🏡 **Project:** sell the house co-owned with **Frankie**.
- 🇦🇪 **Villa:** get Sobha to log the **170,000 AED**; **dispute** the AED 7,601 late fee; start the **monthly pre-fund** for the ~AED 1.2M Jan demand.
- 🧾 **Derma EU / Prodermis:** push the **Dutch company formation** (unblocks NL bonded stock).
- 💉 **Prodermis:** UK sales → **£5,000/month**; chase **registration documents** per distributor country (with BMI).
- 🩺 **Health baselines:** book **body scan** + **blood test**; set **5km benchmark**; set up **macro tracking**.
- 💰 **Personal finances:** kick off the full-reorganisation project with Jarvis.

---

## 14. Evolving Jarvis — it changes as Paul uses it

Jarvis is built to be reshaped continuously, never frozen. Changes fall into two levels:

**Level 1 — say it, done (no build):** preferences and settings Jarvis adjusts on the spot from a Telegram message — nudge intensity & timing, brief/review times, what counts toward a streak, the Kiefer note's contents, macros & training split, which projects are "live," tone, reminders. These live in the brain and dashboard, so they change instantly. *This is most day-to-day tweaking.*

**Level 2 — a small build pass:** new *capabilities* — a new integration (e.g. accounting software), a new dashboard screen, a new company, a new automation. The architecture is **modular and additive**, so "can it also do X?" is a small bolt-on, not a rebuild. These batch up and go to Fable as incremental updates.

**The upgrade loop:** anytime Paul hits friction or has an idea, he flags it to Jarvis with **"💡 improve this"** and a quick note. Jarvis logs it to a **running upgrade list** (tagged Level 1 vs Level 2). Level 1 items it applies immediately; Level 2 items collect for the next build pass. Real usage is the best spec — the system is designed to grow with Paul across the 12-month mission.

---

## 15. Locked technical decisions *(recommended stack, current as of July 2026 — Fable should use the latest version of each at build time)*

| Layer | Locked choice | Why | Fallback |
|---|---|---|---|
| **Brain** (reasoning, coaching, personality) | **Claude Opus** (latest 4.x) | Best nuanced coaching tone, long context, strong agentic tool-use | GPT-5.x |
| **Cheap/fast tasks** (routing, parsing feedback, classification) | **Claude Haiku** (or GPT-mini) | Cuts cost on the ~80% of calls that don't need the big brain | — |
| **Speech-to-text** (your voice notes) | **Deepgram (Nova, latest)** | Fast, accurate, handles accents, cheap per minute | Whisper / AssemblyAI |
| **Text-to-speech** (Jarvis's voice) | **ElevenLabs** | Most natural voice; pick one consistent "Jarvis" voice | OpenAI TTS |
| **Messaging** | **Telegram Bot API** (`python-telegram-bot`) | Native voice in/out, free, rich messages | — |
| **Backend** | **Python + FastAPI**, always-on service | Strongest AI/tooling ecosystem | Node/TS |
| **Structured data** (living records, streaks, schedules, task cache) | **PostgreSQL** | One reliable relational store | — |
| **Knowledge base** (the second brain) | **pgvector on the same Postgres** | One DB to run for MVP; embeddings + semantic search built in | Managed vector DB (Pinecone/Turbopuffer) at scale |
| **Document library** (your uploads) | **Object storage (Cloudflare R2 / S3)** + text extracted, embedded to pgvector, metadata in Postgres | Files kept safely, content made searchable | — |
| **Heartbeat** (briefs, nudges, reviews, emails) | **Job scheduler in-backend** (APScheduler/Celery) | Runs the day even when you're silent | Host cron |
| **Email / Calendar / Trello** | **Gmail API · Google Calendar API · Trello REST API** (OAuth / key+token) | Direct, official, two-way | — |
| **Apple Health** | **iOS Shortcut → webhook** (daily push) | No app needed for v1 | Companion app later |
| **Hosting** | **Render** (always-on web service + managed Postgres w/ pgvector) | Managed, nothing to babysit, cheap | Fly.io / Railway |
| **Secrets** | Host secret manager; encryption at rest; private room isolated | Security by default | — |

**Proactive logic locked:** morning brief **07:00** (gentle in the first hour), midday nudge **~13:30**, evening review + co-plan **21:00**, Kiefer note **21:00**, all in Paul's *current* timezone (stored per-trip; auto-detects or set by "I'm in Dubai"). **Hound mode** auto-triggers when the run is missed **or** completion is below target by midday — raising nudge frequency and firmness — and can be switched on manually with "hound me."

**Voice reply policy:** smart mix — voice for conversation/coaching, text for lists/links — everything transcribed to a searchable log.

## 16. The Daily 12 — exact selection logic

1. **Card metadata:** every Trello card is tagged with **company** (1 of 4), **project**, **due date**, **value/priority**, **waiting-on** flag, **last-moved date**, and list/status.
2. **Live projects:** Paul marks up to **3 active projects per company** (Jarvis proposes from card activity; Paul confirms).
3. **Candidate pool:** actionable cards only — exclude the **Blocked/Waiting** and **Done** lists.
4. **Score each card (0–1 each, weighted):**
   `Score = 0.35·Deadline + 0.25·Money + 0.25·UnblocksOthers + 0.15·AvoidanceAge`
   - *Deadline:* overdue = 1, decays with days-out.
   - *Money:* from a revenue/payment label.
   - *UnblocksOthers:* card where Kiefer/team are waiting on Paul.
   - *AvoidanceAge:* days sitting without progress.
   *(Weights map to Paul's stated priorities and are tunable.)*
5. **Select:** for each company, from each of its 3 live projects take the **top-scored actionable card** → 3 × 4 = **12**. If a project is empty, pull the next-best card from that company.
6. **Present:** grouped by company (as prototyped); the morning brief lists the 12; the other ~50 cards stay hidden.
7. **Bonus unlock:** when all 12 are done, surface the single next-highest-scored card.
8. **Feedback → board:** voice maps to Trello actions — "done" → move to Done · "put on Kiefer" → create + assign on Kiefer Today · "push to Friday" → set due date · freeform → card comment.
9. **Learning:** cards Paul repeatedly defers gain AvoidanceAge weight and a gentle "you keep dodging this" flag; completion patterns tune the weights to him over time.
10. **Streaks:** "Daily 12 cleared" increments only when all 12 are done; partials are logged.

---

*Ready for Fable. The Day-One Brain is the knowledge; this is the build. Every layer, schedule and rule is now specified — no guesswork left. And it's built to keep evolving.*
