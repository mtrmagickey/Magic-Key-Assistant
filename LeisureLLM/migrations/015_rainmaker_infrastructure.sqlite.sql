-- Migration 015: Ensure rainmaker and leads infrastructure tables exist
-- Replaces: fix_rainmaker_schema_v2.py
-- All statements are idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS rainmaker_seen_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    title TEXT,
    source_query TEXT,
    assessment TEXT CHECK (assessment IN ('elevated', 'passed', 'stale')),
    assessment_reason TEXT,
    lead_id INTEGER,
    first_seen_date TEXT NOT NULL,
    last_seen_date TEXT,
    seen_count INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rainmaker_seen_hash ON rainmaker_seen_opportunities(url_hash);
CREATE INDEX IF NOT EXISTS idx_rainmaker_seen_date ON rainmaker_seen_opportunities(first_seen_date DESC);
CREATE INDEX IF NOT EXISTS idx_rainmaker_seen_assessment ON rainmaker_seen_opportunities(assessment);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    source TEXT NOT NULL CHECK (source IN ('scout', 'dreamer', 'manual', 'referral', 'past_client')),
    source_id TEXT,
    status TEXT NOT NULL DEFAULT 'cold' CHECK (status IN ('cold', 'warm', 'hot', 'proposal', 'won', 'lost', 'dormant')),
    priority TEXT DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    owner_user_id INTEGER,
    owner_username TEXT,
    contact_name TEXT,
    contact_info TEXT,
    contact_org TEXT,
    value_estimate TEXT,
    next_action TEXT,
    next_action_date TEXT,
    proposal_due_date TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    last_activity TEXT DEFAULT (datetime('now')),
    closed_at TEXT,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS lead_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    activity_type TEXT NOT NULL CHECK (activity_type IN ('creation', 'status_change', 'note', 'outreach', 'meeting', 'proposal_sent', 'follow_up', 'nudge')),
    summary TEXT,
    old_status TEXT,
    new_status TEXT,
    created_by_user_id INTEGER,
    created_by_username TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Corpus self-interrogation: tracks structural analysis of the knowledge base.
-- The bot examines its own corpus with hierarchical questions and routes
-- findings to web research (public gaps) or human review (institutional gaps).
CREATE TABLE IF NOT EXISTS corpus_interrogations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    interrogation_date TEXT NOT NULL,
    interrogation_type TEXT NOT NULL CHECK (interrogation_type IN ('strategic', 'drill_down', 'verification')),
    domain TEXT,
    question TEXT NOT NULL,
    finding TEXT,
    severity TEXT CHECK (severity IN ('critical', 'significant', 'minor', 'informational')),
    action_type TEXT CHECK (action_type IN ('web_research', 'human_review', 'verification', 'auto_close', 'gap_created')),
    action_result TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'resolved', 'dismissed')),
    parent_id INTEGER REFERENCES corpus_interrogations(id),
    gap_id INTEGER,
    web_research_path TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_interrogation_run ON corpus_interrogations(run_id);
CREATE INDEX IF NOT EXISTS idx_interrogation_status ON corpus_interrogations(status);
CREATE INDEX IF NOT EXISTS idx_interrogation_date ON corpus_interrogations(interrogation_date DESC);
