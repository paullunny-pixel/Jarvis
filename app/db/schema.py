"""Schema DDL, per docs/Jarvis-Data-Model.md. Grows milestone by milestone.

`{pk}` expands to the dialect's auto-increment primary key. Statements are
idempotent (IF NOT EXISTS) — safe to run on every startup.
"""

TABLES: list[str] = [
    # --- Milestone 1: the conversation log (every message, both directions) ---
    """
    CREATE TABLE IF NOT EXISTS messages (
        id {pk},
        ts TEXT NOT NULL,
        direction TEXT NOT NULL,          -- 'in' (Paul) | 'out' (Jarvis)
        channel TEXT NOT NULL DEFAULT 'telegram',
        chat_id BIGINT NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'text',-- 'text' | 'voice'
        transcript TEXT NOT NULL,
        voice_duration INTEGER NOT NULL DEFAULT 0,
        meta TEXT NOT NULL DEFAULT '{{}}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (ts)",
    "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, ts)",
    # --- Settings / living key-values (owner lock, timezone, etc.) ---
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # --- Milestone 2: the second brain ---
    """
    CREATE TABLE IF NOT EXISTS memory_chunks (
        id {pk},
        content TEXT NOT NULL,
        embedding {embedding},
        room TEXT NOT NULL,               -- you|companies|health|finances|people|private
        type TEXT NOT NULL,               -- STABLE|LIVING|PRIVATE
        source TEXT NOT NULL DEFAULT 'conversation',
        tags TEXT NOT NULL DEFAULT '[]',
        document_id BIGINT NOT NULL DEFAULT 0,
        is_private INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        superseded_by BIGINT NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunks_room ON memory_chunks (room, superseded_by)",
    """
    CREATE TABLE IF NOT EXISTS living_facts (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        room TEXT NOT NULL DEFAULT 'you',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS living_facts_history (
        id {pk},
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        replaced_at TEXT NOT NULL
    )
    """,
    # --- Milestone 2: the document library ---
    """
    CREATE TABLE IF NOT EXISTS documents (
        id {pk},
        filename TEXT NOT NULL,
        mime TEXT NOT NULL DEFAULT '',
        storage TEXT NOT NULL DEFAULT 'db',   -- 'r2' | 'db'
        storage_ref TEXT NOT NULL DEFAULT '', -- R2 object key (when storage='r2')
        content BLOB_TYPE,                    -- file bytes (when storage='db')
        room TEXT NOT NULL DEFAULT 'companies',
        tags TEXT NOT NULL DEFAULT '[]',
        uploaded_at TEXT NOT NULL,
        extracted_chars INTEGER NOT NULL DEFAULT 0
    )
    """,
    # --- Milestone 3: Trello + the Daily 12 ---
    """
    CREATE TABLE IF NOT EXISTS companies (
        id {pk},
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id {pk},
        company_slug TEXT NOT NULL,
        name TEXT NOT NULL,
        is_live INTEGER NOT NULL DEFAULT 0,   -- max 3 live per company
        status TEXT NOT NULL DEFAULT 'active',
        last_activity TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_projects_company ON projects (company_slug, is_live)",
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id {pk},
        trello_id TEXT NOT NULL UNIQUE,
        board_id TEXT NOT NULL DEFAULT '',
        board_name TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        company_slug TEXT NOT NULL DEFAULT '',
        project_id BIGINT NOT NULL DEFAULT 0,
        due_date TEXT NOT NULL DEFAULT '',
        money INTEGER NOT NULL DEFAULT 0,       -- 0-3 from £ labels
        waiting_on_paul INTEGER NOT NULL DEFAULT 0,
        last_moved TEXT NOT NULL DEFAULT '',
        list_name TEXT NOT NULL DEFAULT '',
        actionable INTEGER NOT NULL DEFAULT 1,  -- excludes Blocked/Waiting/Done lists
        score REAL NOT NULL DEFAULT 0,
        defer_count INTEGER NOT NULL DEFAULT 0, -- times Paul pushed it (avoidance learning)
        synced_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_12 (
        id {pk},
        plan_date TEXT NOT NULL,               -- YYYY-MM-DD in Paul's timezone
        position INTEGER NOT NULL,             -- 1..12, 0 = bonus
        task_id BIGINT NOT NULL,
        company_slug TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        done_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily12_date ON daily_12 (plan_date, position)",
    # --- Milestone 4: the heartbeat ---
    """
    CREATE TABLE IF NOT EXISTS streaks (
        type TEXT PRIMARY KEY,              -- run | workout | twelve | meals
        current_count INTEGER NOT NULL DEFAULT 0,
        best_count INTEGER NOT NULL DEFAULT 0,
        last_date TEXT NOT NULL DEFAULT ''  -- YYYY-MM-DD in Paul's timezone
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id {pk},
        run_date TEXT NOT NULL,
        distance_km REAL NOT NULL DEFAULT 0,
        duration_min REAL NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'told'   -- told | apple_health
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS health_stats (
        id {pk},
        stat_date TEXT NOT NULL,
        weight_kg REAL NOT NULL DEFAULT 0,
        sleep_hours REAL NOT NULL DEFAULT 0,
        steps INTEGER NOT NULL DEFAULT 0,
        resting_hr INTEGER NOT NULL DEFAULT 0,
        hrv REAL NOT NULL DEFAULT 0,
        raw TEXT NOT NULL DEFAULT '{{}}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_plan (
        plan_date TEXT PRIMARY KEY,
        plan_text TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # --- Master Update: override failsafe + nudge idempotency ---
    """
    CREATE TABLE IF NOT EXISTS override_log (
        id {pk},
        ts TEXT NOT NULL,
        item TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nudge_state (
        nudge_key TEXT PRIMARY KEY,
        last_sent_at TEXT NOT NULL
    )
    """,
    # --- Calendar write trail (6 Aug): every Google Calendar write Jarvis
    # makes, plain-English detail + undo payload ('undo that').
    """
    CREATE TABLE IF NOT EXISTS calendar_log (
        id {pk},
        ts TEXT NOT NULL,
        action TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        calendar_id TEXT NOT NULL DEFAULT '',
        event_id TEXT NOT NULL DEFAULT '',
        undo TEXT NOT NULL DEFAULT '{{}}'
    )
    """,
    # --- Master Update: wake-up, sleep, hourly rhythm, med adherence ---
    """
    CREATE TABLE IF NOT EXISTS wake_log (
        id {pk},
        day TEXT NOT NULL,
        wake_time TEXT NOT NULL,
        photo_ref TEXT NOT NULL DEFAULT '',
        method TEXT NOT NULL DEFAULT 'selfie'   -- selfie | run_proof | told | override
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sleep_log (
        id {pk},
        day TEXT NOT NULL,
        goodnight_time TEXT NOT NULL,
        tz TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS water_log (
        day TEXT PRIMARY KEY,
        ml INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS movement_log (
        day TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS focus_sprints (
        id {pk},
        started_at TEXT NOT NULL,
        length_min INTEGER NOT NULL DEFAULT 25,
        task TEXT NOT NULL DEFAULT '',
        completed INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id {pk},
        day TEXT NOT NULL,
        kind TEXT NOT NULL,                     -- run | workout | recovery
        note TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_day ON activity_log (kind, day)",
    """
    CREATE TABLE IF NOT EXISTS med_adherence (
        id {pk},
        day TEXT NOT NULL,
        item TEXT NOT NULL,                     -- adhd | supplements | trt
        taken_at TEXT NOT NULL
    )
    """,
    # --- Telegram org ingestion (replaces the WhatsApp §13 route) ---
    """
    CREATE TABLE IF NOT EXISTS telegram_ingest (
        id {pk},
        ts TEXT NOT NULL,
        chat_id BIGINT NOT NULL DEFAULT 0,
        chat_title TEXT NOT NULL DEFAULT '',
        company_tag TEXT NOT NULL DEFAULT '',
        sender TEXT NOT NULL DEFAULT '',
        sender_id BIGINT NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'text',    -- text | voice | photo | document
        message TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tg_ingest_company ON telegram_ingest (company_tag, ts)",
    "CREATE INDEX IF NOT EXISTS idx_tg_ingest_chat ON telegram_ingest (chat_id, ts)",
    # --- WhatsApp read-only ingestion (official Cloud API, second number; 4 Aug).
    # NOT named 'whatsapp_ingest': the removed pre-Telegram route left a table
    # of that name (group_id/group_name shape) in prod, and reusing the name
    # crash-looped the 4 Aug deploys (UndefinedColumnError on wa_id). Schema
    # never drops, so the ghost stays; this table sidesteps it. ---
    """
    CREATE TABLE IF NOT EXISTS wa_direct_ingest (
        id {pk},
        ts TEXT NOT NULL,
        wa_id TEXT NOT NULL DEFAULT '',
        sender TEXT NOT NULL DEFAULT '',
        company_tag TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'text',    -- text | voice | image | document
        message TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wa_direct_ingest ON wa_direct_ingest (wa_id, ts)",
    # --- Milestone 6: the private sobriety track (content encrypted at rest) ---
    """
    CREATE TABLE IF NOT EXISTS sobriety (
        id {pk},
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,               -- checkin | sos | milestone | reset | note
        content TEXT NOT NULL DEFAULT '', -- sealed by PrivateBox
        mood INTEGER NOT NULL DEFAULT 0,  -- 1-5, 0 = unknown
        day_count INTEGER NOT NULL DEFAULT 0
    )
    """,
]

PK = {
    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "postgres": "BIGSERIAL PRIMARY KEY",
}

# Additive column migrations for tables that already exist in production.
# Postgres gets ADD COLUMN IF NOT EXISTS; SQLite (no IF NOT EXISTS for
# columns) relies on the adapter swallowing the duplicate-column error.
MIGRATIONS: list[str] = [
    "ALTER TABLE tasks ADD COLUMN board_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN board_name TEXT NOT NULL DEFAULT ''",
]


def migration_statements(dialect: str) -> list[str]:
    if dialect == "postgres":
        return [m.replace("ADD COLUMN", "ADD COLUMN IF NOT EXISTS") for m in MIGRATIONS]
    return list(MIGRATIONS)

def _embed_dim() -> int:
    """The pgvector column dimension follows the configured embedder."""
    try:
        from app.config import get_settings

        return int(get_settings().embed_dim)
    except Exception:
        return 1024


EMBEDDING_COLUMN = {
    "sqlite": "TEXT",              # JSON array; cosine computed in Python
    "postgres": "vector({dim})",   # pgvector, matches EMBED_DIM (Voyage default 1024)
}

BLOB = {"sqlite": "BLOB", "postgres": "BYTEA"}

PRELUDE = {
    "sqlite": [],
    "postgres": ["CREATE EXTENSION IF NOT EXISTS vector"],
}

EPILOGUE = {
    "sqlite": [],
    "postgres": [
        "CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON memory_chunks"
        " USING hnsw (embedding vector_cosine_ops)"
    ],
}


def ddl_statements(dialect: str) -> list[str]:
    pk = PK[dialect]
    embedding = EMBEDDING_COLUMN[dialect].format(dim=_embed_dim())
    statements = [
        stmt.format(pk=pk, embedding=embedding).replace("BLOB_TYPE", BLOB[dialect])
        for stmt in TABLES
    ]
    return PRELUDE[dialect] + statements + EPILOGUE[dialect]
