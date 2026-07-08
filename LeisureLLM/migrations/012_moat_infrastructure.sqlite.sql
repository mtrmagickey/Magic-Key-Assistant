-- Migration 012: Moat Infrastructure Tables
-- Adds tables for: user preferences, folder watching, inference cost tracking,
-- feedback learning loop (chunk quality, prompt variants, improvement signals).
-- These collectively form the switching-cost, workflow-integration, cost-visibility,
-- and network-effect moats.

-- ═══════════════════════════════════════════════════════════════
-- 1. USER PREFERENCES (Personalisation / Switching-Cost Moat)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT PRIMARY KEY,
    preferences TEXT NOT NULL DEFAULT '{}',
    interaction_stats TEXT NOT NULL DEFAULT '{}',
    learned_topics TEXT NOT NULL DEFAULT '[]',
    style_signals TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS preference_learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    signal_value TEXT NOT NULL,
    old_preference TEXT,
    new_preference TEXT,
    confidence REAL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pref_learning_user
    ON preference_learning_log(user_id, created_at DESC);


-- ═══════════════════════════════════════════════════════════════
-- 2. FOLDER WATCHER (Workflow Integration Moat)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS watched_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    extensions TEXT NOT NULL DEFAULT '[]',
    recursive INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS auto_ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER REFERENCES watched_folders(id),
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER,
    file_modified_at TEXT,
    action TEXT NOT NULL CHECK (action IN ('ingested', 'updated', 'skipped', 'error')),
    detail TEXT,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ingest_log_file
    ON auto_ingest_log(file_path, processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_log_folder
    ON auto_ingest_log(folder_id, processed_at DESC);


-- ═══════════════════════════════════════════════════════════════
-- 3. INFERENCE COST TRACKING (Cost Counter-Position Moat)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS inference_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    backend_type TEXT NOT NULL,
    backend_name TEXT NOT NULL,
    model TEXT NOT NULL,
    pipeline_role TEXT NOT NULL DEFAULT 'single',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0.0,
    cloud_equiv_cost_usd REAL NOT NULL DEFAULT 0.0,
    savings_usd REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    query_hash TEXT,
    session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_inference_timestamp
    ON inference_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_inference_backend
    ON inference_log(backend_type, model);
CREATE INDEX IF NOT EXISTS idx_inference_role
    ON inference_log(pipeline_role);

CREATE TABLE IF NOT EXISTS cost_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend_name TEXT NOT NULL,
    period TEXT NOT NULL CHECK (period IN ('daily', 'weekly', 'monthly')),
    budget_usd REAL NOT NULL,
    current_spend_usd REAL NOT NULL DEFAULT 0.0,
    period_start TEXT NOT NULL,
    alert_threshold REAL NOT NULL DEFAULT 0.8,
    alert_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(backend_name, period)
);


-- ═══════════════════════════════════════════════════════════════
-- 4. FEEDBACK LEARNING LOOP (Network Effect Moat)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS prompt_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'system_prompt',
    prompt_text TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    total_uses INTEGER NOT NULL DEFAULT 0,
    helpful_count INTEGER NOT NULL DEFAULT 0,
    unhelpful_count INTEGER NOT NULL DEFAULT 0,
    helpfulness_rate REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    retired_at TEXT,
    UNIQUE(variant_name, category)
);

CREATE TABLE IF NOT EXISTS chunk_quality_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL,
    source_path TEXT,
    times_retrieved INTEGER NOT NULL DEFAULT 0,
    helpful_retrievals INTEGER NOT NULL DEFAULT 0,
    unhelpful_retrievals INTEGER NOT NULL DEFAULT 0,
    quality_score REAL NOT NULL DEFAULT 0.5,
    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_chunk_quality_score
    ON chunk_quality_scores(quality_score);
CREATE INDEX IF NOT EXISTS idx_chunk_quality_id
    ON chunk_quality_scores(chunk_id);

CREATE TABLE IF NOT EXISTS improvement_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_type TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    signal_data TEXT NOT NULL DEFAULT '{}',
    anonymised INTEGER NOT NULL DEFAULT 1,
    opt_in_shared INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_improvement_signals_type
    ON improvement_signals(signal_type, created_at DESC);
