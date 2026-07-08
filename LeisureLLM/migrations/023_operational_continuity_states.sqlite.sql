-- Migration 023: Operational continuity states
-- Persists computed continuity conditions separately from the canonical
-- lifecycle state so multiple active risks can be surfaced at once.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operational_continuity_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES operational_records(id) ON DELETE CASCADE,
    record_stable_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    continuity_state TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cleared')),
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    source_context_id TEXT,
    created_by_actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    updated_by_actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    first_observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    cleared_at TEXT,
    UNIQUE(record_id, continuity_state)
);

CREATE INDEX IF NOT EXISTS idx_operational_continuity_states_status
    ON operational_continuity_states(status, continuity_state, last_observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_operational_continuity_states_record
    ON operational_continuity_states(record_id, status, continuity_state);

CREATE INDEX IF NOT EXISTS idx_operational_continuity_states_type
    ON operational_continuity_states(record_type, continuity_state, status);

CREATE TABLE IF NOT EXISTS operational_continuity_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    continuity_state_id INTEGER NOT NULL REFERENCES operational_continuity_states(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL REFERENCES operational_records(id) ON DELETE CASCADE,
    record_type TEXT NOT NULL,
    continuity_state TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    source_context_id TEXT,
    summary TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_operational_continuity_state_events_state
    ON operational_continuity_state_events(continuity_state_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_operational_continuity_state_events_record
    ON operational_continuity_state_events(record_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_operational_continuity_state_events_type
    ON operational_continuity_state_events(continuity_state, event_type, created_at DESC);