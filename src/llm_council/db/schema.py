"""Final v1 schema (P2, Q6 freeze) — shared column definitions, per-backend DDL.

Exactly 8 tables: councils, council_hats, mcp_instances, seat_health,
council_scores, chat_sessions, chat_messages. The 4 legacy tables (sessions,
messages, model_switches, pending_attachments) are DROPPED on init if present —
this is the fresh-install path; PG data is carried over by
scripts/migrate_pg_to_sqlite.py, not by init.

Timestamps: TEXT ISO-8601 UTC on SQLite, TIMESTAMPTZ on Postgres (converted to
ISO strings at the facade boundary). JSON payloads (usage) are TEXT on both.
"""

from __future__ import annotations

FINAL_TABLES = (
    "councils",
    "council_hats",
    "mcp_instances",
    "seat_health",
    "council_scores",
    "chat_sessions",
    "chat_messages",
    "telemetry_queue",  # P6: local retry queue for anonymous score events
)

LEGACY_TABLES = ("sessions", "messages", "model_switches", "pending_attachments")

# --------------------------------------------------------------------------- #
# SQLite DDL (aiosqlite). Placeholders `?`; INTEGER PRIMARY KEY AUTOINCREMENT;
# timestamps TEXT ISO-8601 UTC.
# --------------------------------------------------------------------------- #

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS councils (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    working_dir TEXT NOT NULL,
    brief       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | done | closed
    kind        TEXT NOT NULL DEFAULT 'adhoc',
    owner       TEXT,
    council_uuid TEXT,                             -- P6: random uuid4, public telemetry id
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS council_hats (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    council_id   INTEGER NOT NULL REFERENCES councils(id) ON DELETE CASCADE,
    hat          TEXT NOT NULL,                    -- blind label: 'hat1', ...
    model        TEXT NOT NULL,                    -- hidden mapping (reveal only)
    session_id   TEXT,                             -- seat CLI session id (resume)
    seat_backend TEXT,                             -- runner_kind: claude|pi|codex
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|error
    answer       TEXT,
    error        TEXT,
    usage        TEXT,                             -- JSON
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS council_hats_council_idx ON council_hats (council_id);

CREATE TABLE IF NOT EXISTS mcp_instances (
    instance_id TEXT PRIMARY KEY,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seat_health (
    seat           TEXT NOT NULL,
    model          TEXT NOT NULL,
    status         TEXT NOT NULL,              -- 'ok' | 'cooldown'
    reason         TEXT,                       -- balance | quota | probe_failed
    last_error     TEXT,
    cooldown_until TEXT,
    checked_at     TEXT NOT NULL,
    PRIMARY KEY (seat, model)
);

CREATE TABLE IF NOT EXISTS council_scores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    council_id INTEGER NOT NULL REFERENCES councils(id) ON DELETE CASCADE,
    hat        TEXT NOT NULL,
    model      TEXT NOT NULL,
    score      INTEGER NOT NULL CHECK (score BETWEEN 1 AND 10),
    notes      TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (council_id, hat)
);
CREATE INDEX IF NOT EXISTS council_scores_model_idx ON council_scores (model);

-- Decision #19: direct seat-chat persistence.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    seat           TEXT NOT NULL,              -- seats.yaml key
    model          TEXT NOT NULL,              -- actually used (after health pick)
    seat_backend   TEXT NOT NULL,              -- runner_kind: claude|pi|codex
    working_dir    TEXT NOT NULL,
    system_prompt  TEXT DEFAULT '',
    cli_session_id TEXT,                       -- resume (NULL until first turn)
    status         TEXT DEFAULT 'idle',        -- idle|running|done|error
    created_at     TEXT NOT NULL,
    last_activity  TEXT NOT NULL,
    closed         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,             -- user|assistant
    content         TEXT NOT NULL,             -- plain text only
    usage           TEXT,                      -- JSON {input,output,cache_read,cache_write}
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_session_idx ON chat_messages (chat_session_id, id);

-- P6: telemetry retry queue (survives restarts; exponential backoff).
CREATE TABLE IF NOT EXISTS telemetry_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT NOT NULL,                    -- allowlisted JSON event
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT,
    created_at   TEXT NOT NULL
);
"""

# --------------------------------------------------------------------------- #
# Postgres DDL (asyncpg). Callers still write `?` — postgres.py rewrites to $N.
# TIMESTAMPTZ columns; the backend converts datetime<->ISO-str at the boundary.
# --------------------------------------------------------------------------- #

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS councils (
    id          BIGSERIAL PRIMARY KEY,
    working_dir TEXT NOT NULL,
    brief       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    kind        TEXT NOT NULL DEFAULT 'adhoc',
    owner       TEXT,
    council_uuid TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS council_hats (
    id           BIGSERIAL PRIMARY KEY,
    council_id   BIGINT NOT NULL REFERENCES councils(id) ON DELETE CASCADE,
    hat          TEXT NOT NULL,
    model        TEXT NOT NULL,
    session_id   TEXT,
    seat_backend TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',
    answer       TEXT,
    error        TEXT,
    usage        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS council_hats_council_idx ON council_hats (council_id);

CREATE TABLE IF NOT EXISTS mcp_instances (
    instance_id TEXT PRIMARY KEY,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seat_health (
    seat           TEXT NOT NULL,
    model          TEXT NOT NULL,
    status         TEXT NOT NULL,
    reason         TEXT,
    last_error     TEXT,
    cooldown_until TIMESTAMPTZ,
    checked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (seat, model)
);

CREATE TABLE IF NOT EXISTS council_scores (
    id         BIGSERIAL PRIMARY KEY,
    council_id BIGINT NOT NULL REFERENCES councils(id) ON DELETE CASCADE,
    hat        TEXT NOT NULL,
    model      TEXT NOT NULL,
    score      SMALLINT NOT NULL CHECK (score BETWEEN 1 AND 10),
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (council_id, hat)
);
CREATE INDEX IF NOT EXISTS council_scores_model_idx ON council_scores (model);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id             BIGSERIAL PRIMARY KEY,
    seat           TEXT NOT NULL,
    model          TEXT NOT NULL,
    seat_backend   TEXT NOT NULL,
    working_dir    TEXT NOT NULL,
    system_prompt  TEXT DEFAULT '',
    cli_session_id TEXT,
    status         TEXT DEFAULT 'idle',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed         BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              BIGSERIAL PRIMARY KEY,
    chat_session_id BIGINT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    usage           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session_idx ON chat_messages (chat_session_id, id);

CREATE TABLE IF NOT EXISTS telemetry_queue (
    id           BIGSERIAL PRIMARY KEY,
    payload      TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
