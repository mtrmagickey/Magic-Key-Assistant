-- Migration 009: Agentic infrastructure
-- Adds tables for tool execution auditing and interaction memory.
-- These support the chatbot→agent transition: tool calling, confirmation
-- gates, query logging, and concern thread detection.

-- ═══════════════════════════════════════════════════════════════════════
-- Tool execution audit log
-- Every tool invocation (chat or autonomous) is recorded here.
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tool_executions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name       TEXT    NOT NULL,
    arguments       TEXT,               -- JSON
    success         BOOLEAN NOT NULL DEFAULT 0,
    message         TEXT,
    artifact_refs   TEXT,               -- JSON array of "[type#id]" strings
    source          TEXT    NOT NULL DEFAULT 'chat',  -- chat | autonomous | workflow
    confirmed_by_user BOOLEAN NOT NULL DEFAULT 0,
    executed_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tool_exec_name     ON tool_executions(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_exec_time     ON tool_executions(executed_at);
CREATE INDEX IF NOT EXISTS idx_tool_exec_source   ON tool_executions(source);


-- ═══════════════════════════════════════════════════════════════════════
-- Chat interaction log (interaction memory)
-- Records every chat query, which tools were invoked, which artifacts
-- were touched, and what concern thread (recurring topic) it belongs to.
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS chat_interactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT    NOT NULL,
    response_summary TEXT,              -- first 500 chars of response
    source          TEXT    NOT NULL DEFAULT 'web',  -- web | discord
    user_id         TEXT,
    -- Tool usage
    tools_invoked   TEXT,               -- JSON array of tool names
    artifact_refs   TEXT,               -- JSON array of "[type#id]" strings
    -- RAG context
    sources_used    TEXT,               -- JSON array of source file paths
    -- Concern threading
    concern_thread  TEXT,               -- auto-detected topic cluster
    -- Timestamps
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_inter_time    ON chat_interactions(created_at);
CREATE INDEX IF NOT EXISTS idx_chat_inter_concern ON chat_interactions(concern_thread);
CREATE INDEX IF NOT EXISTS idx_chat_inter_user    ON chat_interactions(user_id);


-- ═══════════════════════════════════════════════════════════════════════
-- Concern threads — recurring topics the user returns to
-- Populated by the concern detection service; referenced by
-- chat_interactions.concern_thread.
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS concern_threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT    NOT NULL,          -- short label, e.g. "Henderson timeline"
    keywords        TEXT,                      -- JSON array for matching
    first_seen      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT    NOT NULL DEFAULT (datetime('now')),
    query_count     INTEGER NOT NULL DEFAULT 1,
    artifact_refs   TEXT,                      -- JSON array of related artifacts
    status          TEXT    NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'resolved', 'stale'))
);

CREATE INDEX IF NOT EXISTS idx_concern_status ON concern_threads(status);
