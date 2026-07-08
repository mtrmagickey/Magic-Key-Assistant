-- Migration 017: Persistent request tracing for user-visible flow analysis

CREATE TABLE IF NOT EXISTS request_traces (
    request_id TEXT PRIMARY KEY,
    trace_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    entrypoint TEXT NOT NULL,
    route_name TEXT NOT NULL,
    user_visible_flow TEXT NOT NULL,
    conversation_id TEXT,
    thread_id INTEGER,
    packet_id INTEGER,
    lane TEXT NOT NULL,
    query_text_hash TEXT,
    query_word_count INTEGER NOT NULL DEFAULT 0,
    used_cache INTEGER NOT NULL DEFAULT 0,
    cache_key_hash TEXT,
    retrieval_used INTEGER NOT NULL DEFAULT 0,
    retrieval_doc_count INTEGER NOT NULL DEFAULT 0,
    context_word_count INTEGER NOT NULL DEFAULT 0,
    web_augmented INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    retrieval_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    models_used_json TEXT,
    pipeline_stages_json TEXT,
    first_token_ms INTEGER,
    retrieval_ms INTEGER,
    generation_ms INTEGER,
    total_ms INTEGER,
    input_tokens_est INTEGER NOT NULL DEFAULT 0,
    output_tokens_est INTEGER NOT NULL DEFAULT 0,
    total_tokens_est INTEGER NOT NULL DEFAULT 0,
    actual_cost_usd REAL,
    cloud_equiv_cost_usd REAL,
    policy_reason TEXT,
    routing_flags_json TEXT,
    failure_mode TEXT,
    completed_successfully INTEGER NOT NULL DEFAULT 1,
    produced_artifact_type TEXT,
    produced_artifact_id TEXT,
    had_sources INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    background_jobs_spawned_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_request_traces_flow_time
    ON request_traces(user_visible_flow, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_traces_lane_time
    ON request_traces(lane, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_traces_total_ms
    ON request_traces(total_ms DESC);
CREATE INDEX IF NOT EXISTS idx_request_traces_failure
    ON request_traces(failure_mode, created_at DESC);

CREATE TABLE IF NOT EXISTS request_stage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES request_traces(request_id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_request_stage_events_request
    ON request_stage_events(request_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_request_stage_events_name_time
    ON request_stage_events(stage_name, created_at DESC);