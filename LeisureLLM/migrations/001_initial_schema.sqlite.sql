-- Migration: Initial Schema (SQLite)
-- Converted from PostgreSQL for easier deployment
-- Date: 2025-12-20

-- Enable foreign keys
PRAGMA foreign_keys = ON;

-- Projects table
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    client_name TEXT,
    description TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'on_hold', 'completed', 'cancelled')),
    start_date TEXT,  -- ISO 8601 format
    end_date TEXT,
    budget_usd INTEGER,
    actual_cost_usd INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    created_by_user_id INTEGER,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_client ON projects(client_name);

-- Tasks table
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'blocked', 'done', 'cancelled')),
    priority TEXT DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    assigned_to_user_id INTEGER,
    assigned_to_username TEXT,
    created_by_user_id INTEGER,
    created_by_username TEXT,
    due_date TEXT,
    completed_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    tags TEXT,  -- JSON array as text
    estimated_hours REAL,
    actual_hours REAL,
    discord_thread_id INTEGER,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to_user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);

-- Clients table
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    industry TEXT,
    website TEXT,
    primary_contact_name TEXT,
    primary_contact_email TEXT,
    primary_contact_phone TEXT,
    address TEXT,
    relationship_status TEXT DEFAULT 'prospect' CHECK (relationship_status IN ('prospect', 'active', 'past', 'inactive')),
    lifetime_value_usd INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_clients_status ON clients(relationship_status);

-- Contacts table
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    title TEXT,
    email TEXT,
    phone TEXT,
    linkedin_url TEXT,
    role TEXT,
    is_primary INTEGER DEFAULT 0,  -- Boolean: 0=false, 1=true
    last_contact_date TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_contacts_client ON contacts(client_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_last_contact ON contacts(last_contact_date);

-- Opportunities table
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    description TEXT,
    value_usd INTEGER,
    probability INTEGER CHECK (probability >= 0 AND probability <= 100),
    status TEXT DEFAULT 'identified' CHECK (status IN ('identified', 'qualified', 'proposal', 'negotiation', 'won', 'lost', 'abandoned')),
    source TEXT,  -- e.g., 'scout', 'referral', 'inbound'
    scout_search_id INTEGER,
    expected_close_date TEXT,
    actual_close_date TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    created_by_user_id INTEGER,
    assigned_to_user_id INTEGER,
    win_reason TEXT,
    loss_reason TEXT,
    competitor TEXT,
    tags TEXT,  -- JSON array as text
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_opportunities_client ON opportunities(client_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_opportunities_close_date ON opportunities(expected_close_date);
CREATE INDEX IF NOT EXISTS idx_opportunities_source ON opportunities(source);

-- Touchpoints table (interactions with clients/contacts)
CREATE TABLE IF NOT EXISTS touchpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    type TEXT NOT NULL CHECK (type IN ('meeting', 'call', 'email', 'demo', 'proposal', 'follow_up', 'social', 'other')),
    subject TEXT,
    summary TEXT,
    outcome TEXT,
    occurred_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    created_by_user_id INTEGER,
    created_by_username TEXT,
    discord_message_id INTEGER,
    next_action TEXT,
    next_action_date TEXT,
    tags TEXT  -- JSON array as text
);

CREATE INDEX IF NOT EXISTS idx_touchpoints_client ON touchpoints(client_id);
CREATE INDEX IF NOT EXISTS idx_touchpoints_contact ON touchpoints(contact_id);
CREATE INDEX IF NOT EXISTS idx_touchpoints_opportunity ON touchpoints(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_touchpoints_occurred ON touchpoints(occurred_at);

-- Decisions table (company decisions/learnings)
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    context TEXT,
    decision TEXT NOT NULL,
    rationale TEXT,
    decided_by TEXT,  -- JSON array of user IDs as text
    decided_at TEXT DEFAULT (datetime('now')),
    category TEXT,  -- e.g., 'technical', 'business', 'process'
    impact TEXT CHECK (impact IN ('low', 'medium', 'high')),
    related_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    reviewed_at TEXT,
    superseded_by_decision_id INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
    tags TEXT  -- JSON array as text
);

CREATE INDEX IF NOT EXISTS idx_decisions_category ON decisions(category);
CREATE INDEX IF NOT EXISTS idx_decisions_date ON decisions(decided_at);

-- Job Runs table (autonomous operations tracking)
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_seconds REAL,
    triggered_by TEXT,  -- 'schedule', 'manual', 'event'
    output_summary TEXT,
    error_message TEXT,
    metadata TEXT  -- JSON as text
);

CREATE INDEX IF NOT EXISTS idx_job_runs_name ON job_runs(job_name);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at DESC);

-- Receipts table (command execution audit trail)
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    channel_id INTEGER,
    channel_name TEXT,
    guild_id INTEGER,
    executed_at TEXT DEFAULT (datetime('now')),
    parameters TEXT,  -- JSON as text
    result_status TEXT CHECK (result_status IN ('success', 'failure', 'partial')),
    result_summary TEXT,
    execution_time_ms INTEGER,
    related_record_id INTEGER,
    related_record_type TEXT
);

CREATE INDEX IF NOT EXISTS idx_receipts_command ON receipts(command_name);
CREATE INDEX IF NOT EXISTS idx_receipts_user ON receipts(user_id);
CREATE INDEX IF NOT EXISTS idx_receipts_executed ON receipts(executed_at DESC);

-- Runbooks table (operational procedures)
CREATE TABLE IF NOT EXISTS runbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    category TEXT,
    description TEXT,
    steps TEXT NOT NULL,  -- JSON array as text
    when_to_use TEXT,
    related_commands TEXT,  -- JSON array as text
    last_used_at TEXT,
    use_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    created_by_user_id INTEGER,
    tags TEXT  -- JSON array as text
);

CREATE INDEX IF NOT EXISTS idx_runbooks_category ON runbooks(category);
CREATE INDEX IF NOT EXISTS idx_runbooks_last_used ON runbooks(last_used_at);
