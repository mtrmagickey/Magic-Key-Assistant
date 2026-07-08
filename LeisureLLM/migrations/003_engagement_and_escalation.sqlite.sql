-- Migration: Partner Engagement Metrics and Escalation Tracking (SQLite)
-- Date: 2026-01-01
-- Purpose: Track partner responses, escalations, and sprint cycles

-- Add escalation fields to action items (tasks table)
ALTER TABLE tasks ADD COLUMN blocked_since TEXT;
ALTER TABLE tasks ADD COLUMN escalated INTEGER DEFAULT 0;  -- Boolean
ALTER TABLE tasks ADD COLUMN escalation_notes TEXT;

-- Add engagement tracking fields to knowledge gaps
ALTER TABLE knowledge_gaps ADD COLUMN probing_questions_asked INTEGER DEFAULT 0;
ALTER TABLE knowledge_gaps ADD COLUMN last_probing_question_at TEXT;
ALTER TABLE knowledge_gaps ADD COLUMN response_count INTEGER DEFAULT 0;
ALTER TABLE knowledge_gaps ADD COLUMN last_response_at TEXT;
ALTER TABLE knowledge_gaps ADD COLUMN escalated INTEGER DEFAULT 0;  -- Boolean

-- Partner engagement metrics table
CREATE TABLE IF NOT EXISTS partner_engagement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_user_id INTEGER NOT NULL,
    partner_username TEXT,
    
    -- Question tracking
    questions_asked INTEGER DEFAULT 0,
    questions_answered INTEGER DEFAULT 0,
    last_question_at TEXT,
    last_answer_at TEXT,
    
    -- Response metrics
    avg_response_time_hours REAL,
    response_rate REAL,  -- Percentage 0-100
    helpful_answers INTEGER DEFAULT 0,
    unhelpful_answers INTEGER DEFAULT 0,
    
    -- Weekly stats
    week_start_date TEXT NOT NULL,  -- ISO format YYYY-MM-DD
    week_end_date TEXT,
    
    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    
    UNIQUE(partner_user_id, week_start_date)
);

CREATE INDEX IF NOT EXISTS idx_engagement_partner ON partner_engagement(partner_user_id);
CREATE INDEX IF NOT EXISTS idx_engagement_week ON partner_engagement(week_start_date DESC);

-- Sprint/cycle boundaries table
CREATE TABLE IF NOT EXISTS sprint_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_name TEXT NOT NULL,  -- e.g., "Week 1 2026" or "Sprint 12"
    start_date TEXT NOT NULL,  -- ISO format
    end_date TEXT NOT NULL,
    
    -- Goals and themes
    focus_areas TEXT,  -- JSON array as text
    goals TEXT,  -- JSON array as text
    
    -- Metrics
    action_items_planned INTEGER DEFAULT 0,
    action_items_completed INTEGER DEFAULT 0,
    gaps_resolved INTEGER DEFAULT 0,
    partner_engagement_score REAL,
    
    -- Status
    status TEXT DEFAULT 'active' CHECK (status IN ('planning', 'active', 'completed')),
    retrospective TEXT,  -- Generated at cycle end
    
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    
    UNIQUE(cycle_name)
);

CREATE INDEX IF NOT EXISTS idx_sprint_status ON sprint_cycles(status);
CREATE INDEX IF NOT EXISTS idx_sprint_dates ON sprint_cycles(start_date DESC);

-- Link action items to knowledge gaps (many-to-many)
CREATE TABLE IF NOT EXISTS action_gap_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    gap_id INTEGER NOT NULL REFERENCES knowledge_gaps(id) ON DELETE CASCADE,
    link_type TEXT DEFAULT 'resolves' CHECK (link_type IN ('resolves', 'related', 'blocks')),
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    
    UNIQUE(action_id, gap_id)
);

CREATE INDEX IF NOT EXISTS idx_action_gap_action ON action_gap_links(action_id);
CREATE INDEX IF NOT EXISTS idx_action_gap_gap ON action_gap_links(gap_id);

-- Escalation log for audit trail
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('action_item', 'knowledge_gap')),
    entity_id INTEGER NOT NULL,
    
    reason TEXT NOT NULL,  -- e.g., 'blocked_2_weeks', 'no_response_3_questions'
    escalated_to_user_id INTEGER,
    escalated_to_username TEXT,
    escalation_message TEXT,
    
    escalated_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolution_notes TEXT,
    
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed'))
);

CREATE INDEX IF NOT EXISTS idx_escalations_entity ON escalations(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_escalations_status ON escalations(status);
CREATE INDEX IF NOT EXISTS idx_escalations_date ON escalations(escalated_at DESC);
