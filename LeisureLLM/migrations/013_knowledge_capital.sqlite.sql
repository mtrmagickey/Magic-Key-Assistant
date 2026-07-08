-- Migration 013: Knowledge Capital Tracking
-- Records flywheel events: web cache saves, cache reuse hits,
-- confidence improvements, and corpus growth milestones.
-- Powers the Knowledge Capital dashboard.

CREATE TABLE IF NOT EXISTS knowledge_capital_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'web_cache_saved',       -- web search result cached to corpus
        'web_cache_reused',      -- cached web result served instead of new search
        'web_search_performed',  -- live web search was needed (no cache hit)
        'confidence_improved',   -- topic confidence rose above threshold
        'gap_auto_closed',       -- gap automatically closed by high-confidence answer
        'gap_auto_researched',   -- gap immediately researched via web
        'corpus_milestone'       -- corpus hit a size milestone (100, 500, 1000 docs)
    )),
    topic TEXT,                   -- question or topic slug
    detail TEXT,                  -- human-readable description
    meta_json TEXT,               -- optional JSON payload (savings_ms, old/new confidence, etc.)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_kc_events_type
    ON knowledge_capital_events(event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_kc_events_date
    ON knowledge_capital_events(created_at DESC);
