-- Migration 005: Continuity rails — obligations, SOPs, feedback, venture rails
-- Date: 2026-02-07
-- Milestone: M2.5 — Continuity & Trust Controls

-- ── Obligations ──────────────────────────────────────────────
-- Recurring requirements: renewals, filings, inspections, payroll, maintenance
CREATE TABLE IF NOT EXISTS obligations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    frequency TEXT NOT NULL DEFAULT 'monthly',   -- daily|weekly|biweekly|monthly|quarterly|annually|custom
    owner_username TEXT,
    next_due TEXT,                                -- ISO 8601
    last_completed TEXT,
    status TEXT NOT NULL DEFAULT 'active',        -- active|upcoming|overdue|completed|suspended
    checklist TEXT,                               -- JSON array of checklist items
    evidence_links TEXT,                          -- JSON array of evidence refs
    category TEXT,                                -- compliance|financial|operational|legal
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_obligations_next_due ON obligations(next_due);
CREATE INDEX IF NOT EXISTS idx_obligations_status ON obligations(status);

-- ── SOPs (Standard Operating Procedures) ─────────────────────
-- Versioned runbooks with exercise tracking
CREATE TABLE IF NOT EXISTS sops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    owner_username TEXT,
    body TEXT,                                    -- Markdown runbook content
    checklist TEXT,                               -- JSON array of checklist steps
    last_exercised TEXT,                          -- ISO 8601
    last_reviewed TEXT,
    linked_decisions TEXT,                        -- JSON array of decision IDs
    linked_incidents TEXT,                        -- JSON array of incident descriptions
    category TEXT,                                -- onboarding|operations|compliance|emergency
    status TEXT NOT NULL DEFAULT 'active',        -- active|draft|deprecated
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sops_category ON sops(category);
CREATE INDEX IF NOT EXISTS idx_sops_status ON sops(status);

-- ── Feedback ─────────────────────────────────────────────────
-- Structured product feedback with environment snapshots
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    category TEXT,                                -- bug|feature|ux|performance|other
    severity TEXT,                                -- low|medium|high|critical
    context TEXT,                                 -- what was the user doing
    environment_snapshot TEXT,                    -- JSON: OS, python, config hash, etc.
    submitted_by TEXT,
    status TEXT NOT NULL DEFAULT 'new',           -- new|triaged|resolved|wont_fix
    resolution TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status);
CREATE INDEX IF NOT EXISTS idx_feedback_category ON feedback(category);

-- ── Rails (Venture Lifecycle Tracks) ─────────────────────────
-- State machines: Validate, Launch, Operate
CREATE TABLE IF NOT EXISTS rails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rail_type TEXT NOT NULL,                      -- validate|launch|operate
    description TEXT,
    current_stage_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',        -- active|paused|completed|archived
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rails_type ON rails(rail_type);
CREATE INDEX IF NOT EXISTS idx_rails_status ON rails(status);

-- ── Rail Stages ──────────────────────────────────────────────
-- Ordered stages within a rail, each with required outputs
CREATE TABLE IF NOT EXISTS rail_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rail_id INTEGER NOT NULL REFERENCES rails(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,                   -- order within the rail
    description TEXT,
    required_outputs TEXT,                        -- JSON array of required artifact types/descriptions
    actual_outputs TEXT,                          -- JSON array of artifact refs produced
    status TEXT NOT NULL DEFAULT 'not_started',   -- not_started|in_progress|blocked|complete|skipped
    entered_at TEXT,
    completed_at TEXT,
    escalation_days INTEGER NOT NULL DEFAULT 7,  -- days before escalation if incomplete
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_rail_stages_rail ON rail_stages(rail_id);
CREATE INDEX IF NOT EXISTS idx_rail_stages_status ON rail_stages(status);

-- ── Trust Controls ───────────────────────────────────────────
-- Autonomous post audit log — every post the system makes
CREATE TABLE IF NOT EXISTS autonomous_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,                       -- which job produced this
    channel_id INTEGER,
    trigger_condition TEXT,                       -- why this fired
    record_ids_touched TEXT,                      -- JSON array of [type#id] refs
    changes_summary TEXT,                         -- what changed
    suppressed INTEGER NOT NULL DEFAULT 0,        -- 1 if post was suppressed (no-change / quiet hours)
    suppression_reason TEXT,                      -- 'quiet_hours' | 'no_change' | 'noise_budget'
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_autonomous_posts_job ON autonomous_posts(job_name);
CREATE INDEX IF NOT EXISTS idx_autonomous_posts_created ON autonomous_posts(created_at);
