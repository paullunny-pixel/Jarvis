# JARVIS — Data Model (Postgres + pgvector)

*The database Fable builds. PostgreSQL for structured/living data; pgvector for the second brain; object storage for files. Names are a starting point.*

---

## Structured tables (Postgres)

**`user_profile`** — the single user (Paul): name, DOB, nationality, home bases, current_timezone (updated per trip), operating-manual notes, the "why". *(mostly STABLE)*

**`companies`** — the 4: name, description, stage, key metrics (turnover, targets). `LIVING` metrics.
**`projects`** — belongs to a company; name; is_live (max 3 live per company); status.
**`tasks`** — cached Trello cards: trello_id, title, company_id, project_id, due_date, value_label, waiting_on flag, last_moved, status/list, score. Synced two-way with Trello.

**`daily_plan`** — per date: ordered time-blocks (run, meals, 12-task blocks, workout, review) in Paul's tz.
**`daily_12`** — per date: the 12 selected task_ids + done flags + bonus.
**`streaks`** — type (run/workout/12/meals), current_count, best_count, last_date.

**`workouts`** — date, split (P/P/L), location, exercises[] (name, weight, reps, sets) — live-logged.
**`runs`** — date, distance, time/pace (from Apple Health).
**`health_stats`** — date, weight, body_fat, sleep, HR/HRV, other scan/blood results. `LIVING`.
**`nutrition`** — meal plan, macro targets, daily logged meals.

**`finances`** — villa (paid, remaining, next demand, flags), regular commitments (Jade/Steph/John), freedom-number progress. `LIVING`.

**`people`** — team, family, suppliers: name, role, location, notes, contact (as gathered).

**`messages`** — full Telegram log: timestamp, direction, voice_url, transcript, channel.
**`schedules`** — the heartbeat jobs (brief, nudge, review, Kiefer email) + times.
**`upgrade_list`** — Paul's "💡 improve this" items, tagged Level 1 / Level 2 (see Plan §14).

**`sobriety`** `PRIVATE` — **separate encrypted store**: day_count, check-ins, mood, triggers, SOS events. Never joined to business queries; excluded from reports.

---

## The second brain (pgvector)

**`memory_chunks`** — id, content, embedding (vector), **room** (you/companies/health/finances/people/private), **type** (STABLE/LIVING/PRIVATE), source (voice/doc/tool), tags (company/project/person), created_at, superseded_by. Retrieval filters by room+type, ranks by semantic similarity + recency. Living facts are updated in place (old row marked superseded, history kept).

## Document library (object storage + index)

**`documents`** — id, filename, storage_url (R2/S3), room, tags, uploaded_at, extracted_text_ref. On upload: extract text → chunk → embed into `memory_chunks` (linked back to the document) so files are recallable by meaning ("show me the BMI contract").

---

## Key rules
- **LIVING facts update in place** — never duplicate; keep history.
- **PRIVATE room is isolated** — separate store/keys, never enters business context or outbound messages.
- **Everything is Paul's** — encryption at rest; he can audit and correct any record.
