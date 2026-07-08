-- Migration 008: Inbox threads — persistent async question & interview threads
-- Date: 2026-02-16

-- Replaces the real-time chat with an email-like inbox where questions are
-- processed asynchronously and interview sessions walk through knowledge gaps.

CREATE TABLE IF NOT EXISTS inbox_threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT    NOT NULL,
    thread_type     TEXT    NOT NULL DEFAULT 'question',   -- 'question' | 'interview'
    status          TEXT    NOT NULL DEFAULT 'processing', -- 'processing' | 'ready' | 'read' | 'archived'
    processing_status TEXT,                                 -- human-readable progress text
    gap_id          INTEGER,                                -- current gap (interview threads)
    interview_session_id INTEGER,                           -- FK to interview_sessions
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    is_starred      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inbox_messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id            INTEGER NOT NULL REFERENCES inbox_threads(id) ON DELETE CASCADE,
    role                 TEXT    NOT NULL,             -- 'user' | 'assistant' | 'system'
    content              TEXT    NOT NULL DEFAULT '',
    sources_json         TEXT,                         -- JSON array of source citations
    chunk_sources_json   TEXT,                         -- JSON array of chunk source paths
    pipeline_stages_json TEXT,                         -- JSON dict of stage outputs
    models_used_json     TEXT,                         -- JSON dict of models per stage
    processing_time_ms   INTEGER,                      -- wall-clock time for this response
    is_ingested          INTEGER NOT NULL DEFAULT 0,
    ingested_at          TEXT,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_inbox_threads_status  ON inbox_threads(status);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_updated ON inbox_threads(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_thread ON inbox_messages(thread_id);
