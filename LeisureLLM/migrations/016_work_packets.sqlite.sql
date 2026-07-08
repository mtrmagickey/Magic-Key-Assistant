-- Migration 016: Minimum viable work packet kernel
-- Adds durable workflow-state tables without changing business-object authority.

CREATE TABLE IF NOT EXISTS work_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_key TEXT NOT NULL UNIQUE,
    packet_type TEXT NOT NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'active', 'blocked', 'awaiting_human', 'completed', 'cancelled', 'failed')),
    lane TEXT NOT NULL DEFAULT 'assistive'
        CHECK (lane IN ('deterministic', 'assistive', 'reasoning', 'maintenance')),
    owner_kind TEXT NOT NULL DEFAULT 'system',
    owner_ref TEXT,
    next_step TEXT,
    blocked_reason TEXT,
    approval_required INTEGER NOT NULL DEFAULT 0,
    approval_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK (approval_status IN ('not_required', 'pending', 'approved', 'rejected')),
    current_summary TEXT,
    completion_summary TEXT,
    created_from_type TEXT NOT NULL,
    created_from_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    terminal_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_packets_status_updated
    ON work_packets(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_packets_lane_status
    ON work_packets(lane, status);
CREATE INDEX IF NOT EXISTS idx_work_packets_created_from
    ON work_packets(created_from_type, created_from_id);

CREATE TABLE IF NOT EXISTS packet_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id INTEGER NOT NULL REFERENCES work_packets(id) ON DELETE CASCADE,
    link_role TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    target_key TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_packet_links_packet
    ON packet_links(packet_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_packet_links_target
    ON packet_links(target_type, target_id, target_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_packet_links_unique
    ON packet_links(packet_id, link_role, target_type, COALESCE(target_id, ''), COALESCE(target_key, ''));

CREATE TABLE IF NOT EXISTS packet_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id INTEGER NOT NULL REFERENCES work_packets(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    lane TEXT,
    actor_kind TEXT NOT NULL DEFAULT 'system',
    actor_ref TEXT,
    summary TEXT,
    snapshot_json TEXT,
    related_job_run_id INTEGER,
    related_tool_execution_id INTEGER,
    related_chat_interaction_id INTEGER,
    related_inbox_thread_id INTEGER,
    requires_confirmation INTEGER NOT NULL DEFAULT 0,
    confirmation_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK (confirmation_status IN ('not_required', 'pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_packet_events_packet_time
    ON packet_events(packet_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_packet_events_type_time
    ON packet_events(event_type, created_at DESC);
