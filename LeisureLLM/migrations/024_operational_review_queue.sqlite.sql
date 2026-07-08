-- Migration 024: Unified operational review queue overlays and sessions
-- Keeps reviewable work derived from canonical proposals and continuity
-- state while persisting review-specific controls such as deferrals,
-- escalations, snapshots, and completion markers.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operational_review_queue_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_item_id TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL,
    underlying_entity_type TEXT NOT NULL,
    underlying_entity_id TEXT NOT NULL,
    proposal_id INTEGER REFERENCES operational_extraction_proposals(id) ON DELETE SET NULL,
    operational_record_id INTEGER REFERENCES operational_records(id) ON DELETE SET NULL,
    deferred_until TEXT,
    deferral_rationale TEXT,
    defer_count INTEGER NOT NULL DEFAULT 0,
    severity_override TEXT,
    escalation_destination_json TEXT NOT NULL DEFAULT '{}',
    escalated_at TEXT,
    escalated_by_actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    last_action_at TEXT,
    last_action_type TEXT,
    last_action_by_actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    resolved_at TEXT,
    resolved_by_actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    resolution_rationale TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_operational_review_queue_state_entity
    ON operational_review_queue_state(underlying_entity_type, underlying_entity_id);

CREATE INDEX IF NOT EXISTS idx_operational_review_queue_state_record
    ON operational_review_queue_state(operational_record_id, resolved_at, deferred_until);

CREATE INDEX IF NOT EXISTS idx_operational_review_queue_state_proposal
    ON operational_review_queue_state(proposal_id, resolved_at, deferred_until);

CREATE TABLE IF NOT EXISTS operational_review_queue_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL UNIQUE,
    review_item_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    action_type TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    rationale TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    review_session_id INTEGER REFERENCES operational_review_sessions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_operational_review_queue_actions_item
    ON operational_review_queue_actions(review_item_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_operational_review_queue_actions_actor
    ON operational_review_queue_actions(actor_id, created_at DESC);

CREATE TABLE IF NOT EXISTS operational_review_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    cadence TEXT NOT NULL CHECK (cadence IN ('daily', 'weekly')),
    scope TEXT NOT NULL CHECK (scope IN ('mine', 'team', 'all')),
    owner_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    workspace_scope TEXT,
    project_scope TEXT,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_by_actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    completed_by_actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    completion_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_operational_review_sessions_cadence
    ON operational_review_sessions(cadence, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_operational_review_sessions_completion
    ON operational_review_sessions(completed_at, cadence, created_at DESC);

CREATE TABLE IF NOT EXISTS operational_review_session_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_session_id INTEGER NOT NULL REFERENCES operational_review_sessions(id) ON DELETE CASCADE,
    review_item_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(review_session_id, review_item_id)
);

CREATE INDEX IF NOT EXISTS idx_operational_review_session_items_session
    ON operational_review_session_items(review_session_id, severity, created_at DESC);