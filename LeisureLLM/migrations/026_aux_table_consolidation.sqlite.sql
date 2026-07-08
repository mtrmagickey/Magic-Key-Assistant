-- Migration 026: Capture orphaned auxiliary tables
--
-- These tables were previously created only by database._ensure_aux_tables()
-- (the runtime fallback path). This migration makes them part of the
-- sequential migration chain so that _ensure_aux_tables() can be slimmed to
-- column-fix-up logic only.
--
-- All statements are IF NOT EXISTS so this is safe to run against databases
-- that already have these tables from the runtime path.

-- ===== Partner engagement =====

CREATE TABLE IF NOT EXISTS partner_point_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_user_id INTEGER NOT NULL,
    partner_username TEXT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    points INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(partner_user_id, entity_type, entity_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_partner_points_user ON partner_point_events(partner_user_id);
CREATE INDEX IF NOT EXISTS idx_partner_points_created ON partner_point_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_partner_points_entity ON partner_point_events(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS partner_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_user_id INTEGER NOT NULL,
    partner_username TEXT,
    category TEXT,
    details TEXT NOT NULL,
    link TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    used_at TEXT,
    used_in_meeting_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_partner_updates_user ON partner_updates(partner_user_id);
CREATE INDEX IF NOT EXISTS idx_partner_updates_created ON partner_updates(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_partner_updates_used ON partner_updates(used_at);

-- ===== PM automation =====

CREATE TABLE IF NOT EXISTS open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL UNIQUE,
    author_user_id INTEGER NOT NULL,
    author_username TEXT,
    question_text TEXT,
    asked_at TEXT DEFAULT (datetime('now')),
    last_pinged_at TEXT,
    ping_count INTEGER DEFAULT 0,
    resolved_at TEXT,
    assigned_to_user_id INTEGER,
    assigned_to_username TEXT
);
CREATE INDEX IF NOT EXISTS idx_open_questions_channel ON open_questions(channel_id);
CREATE INDEX IF NOT EXISTS idx_open_questions_asked ON open_questions(asked_at DESC);
CREATE INDEX IF NOT EXISTS idx_open_questions_resolved ON open_questions(resolved_at);

CREATE TABLE IF NOT EXISTS pm_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL UNIQUE,
    author_user_id INTEGER,
    author_username TEXT,
    proposal_type TEXT NOT NULL CHECK (proposal_type IN ('action_item','decision')),
    proposed_title TEXT,
    proposed_body TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pm_proposals_created ON pm_proposals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pm_proposals_type ON pm_proposals(proposal_type);

CREATE TABLE IF NOT EXISTS pm_threads (
    thread_id INTEGER PRIMARY KEY,
    guild_id INTEGER,
    channel_id INTEGER,
    purpose TEXT NOT NULL,
    run_date TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pm_threads_purpose ON pm_threads(purpose);
CREATE INDEX IF NOT EXISTS idx_pm_threads_date ON pm_threads(run_date DESC);

CREATE TABLE IF NOT EXISTS pm_dashboard_state (
    week_start_date TEXT PRIMARY KEY,
    guild_id INTEGER,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pm_dashboard_updated ON pm_dashboard_state(updated_at DESC);

-- ===== Steward: bot observability =====

CREATE TABLE IF NOT EXISTS bot_command_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_name TEXT NOT NULL,
    cog_name TEXT,
    user_id INTEGER,
    username TEXT,
    channel_id INTEGER,
    guild_id INTEGER,
    success INTEGER DEFAULT 1,
    error_message TEXT,
    execution_time_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cmd_usage_command ON bot_command_usage(command_name);
CREATE INDEX IF NOT EXISTS idx_cmd_usage_date ON bot_command_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cmd_usage_user ON bot_command_usage(user_id);

CREATE TABLE IF NOT EXISTS bot_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text TEXT NOT NULL,
    question_hash TEXT,
    user_id INTEGER,
    username TEXT,
    channel_id INTEGER,
    response_quality TEXT CHECK (response_quality IN ('helpful', 'unhelpful', 'unknown')),
    had_sources INTEGER DEFAULT 0,
    source_count INTEGER DEFAULT 0,
    feedback_received INTEGER DEFAULT 0,
    related_gap_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bot_questions_hash ON bot_questions(question_hash);
CREATE INDEX IF NOT EXISTS idx_bot_questions_date ON bot_questions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bot_questions_quality ON bot_questions(response_quality);

CREATE TABLE IF NOT EXISTS learning_loop_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id INTEGER,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'gap_opened', 'gap_investigated', 'memo_drafted', 'memo_ingested',
        'improvement_verified', 'gap_closed', 'gap_stale'
    )),
    description TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_learning_loop_gap ON learning_loop_events(gap_id);
CREATE INDEX IF NOT EXISTS idx_learning_loop_type ON learning_loop_events(event_type);
CREATE INDEX IF NOT EXISTS idx_learning_loop_date ON learning_loop_events(created_at DESC);

CREATE TABLE IF NOT EXISTS bot_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL UNIQUE,
    questions_asked INTEGER DEFAULT 0,
    questions_helpful INTEGER DEFAULT 0,
    questions_unhelpful INTEGER DEFAULT 0,
    commands_used INTEGER DEFAULT 0,
    unique_users INTEGER DEFAULT 0,
    docs_ingested INTEGER DEFAULT 0,
    gaps_opened INTEGER DEFAULT 0,
    gaps_closed INTEGER DEFAULT 0,
    memos_written INTEGER DEFAULT 0,
    leads_created INTEGER DEFAULT 0,
    leads_won INTEGER DEFAULT 0,
    feedback_count INTEGER DEFAULT 0,
    recurring_questions_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_health_date ON bot_health_snapshots(snapshot_date DESC);

CREATE TABLE IF NOT EXISTS recurring_blind_spots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_pattern TEXT NOT NULL,
    occurrence_count INTEGER DEFAULT 1,
    last_asked_at TEXT,
    example_questions TEXT,
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'addressed', 'dismissed')),
    resolution_notes TEXT,
    related_gap_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_blind_spots_status ON recurring_blind_spots(status);
CREATE INDEX IF NOT EXISTS idx_blind_spots_count ON recurring_blind_spots(occurrence_count DESC);

-- ===== Persona meetings =====

CREATE TABLE IF NOT EXISTS persona_meeting_takeaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date TEXT NOT NULL,
    meeting_topic TEXT,
    attendees TEXT,
    insight TEXT NOT NULL,
    owner TEXT,
    urgency TEXT CHECK (urgency IN ('low', 'medium', 'high')),
    why_now TEXT,
    opening_provocation TEXT,
    tangent_explored TEXT,
    unresolved_tension TEXT,
    actioned INTEGER DEFAULT 0,
    actioned_as TEXT,
    actioned_entity_id INTEGER,
    included_in_digest_date TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_takeaways_date ON persona_meeting_takeaways(meeting_date DESC);
CREATE INDEX IF NOT EXISTS idx_takeaways_urgency ON persona_meeting_takeaways(urgency);
CREATE INDEX IF NOT EXISTS idx_takeaways_actioned ON persona_meeting_takeaways(actioned);
CREATE INDEX IF NOT EXISTS idx_takeaways_digest ON persona_meeting_takeaways(included_in_digest_date);

-- ===== Custom personas =====

CREATE TABLE IF NOT EXISTS custom_personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '👤',
    personality TEXT NOT NULL,
    concerns TEXT NOT NULL,
    project_context TEXT,
    created_by_user_id INTEGER NOT NULL,
    created_by_username TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_custom_personas_active ON custom_personas(active);
CREATE INDEX IF NOT EXISTS idx_custom_personas_key ON custom_personas(key);

-- ===== Past clients =====

CREATE TABLE IF NOT EXISTS past_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_name TEXT NOT NULL,
    contact_name TEXT,
    contact_email TEXT,
    last_project_date TEXT,
    project_summary TEXT,
    relationship_notes TEXT,
    reengagement_date TEXT,
    last_contacted_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_past_clients_reengage ON past_clients(reengagement_date);
CREATE INDEX IF NOT EXISTS idx_past_clients_org ON past_clients(org_name);
