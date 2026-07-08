CREATE TABLE IF NOT EXISTS operational_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    changed_fields_json TEXT,
    actor_id INTEGER,
    surface TEXT NOT NULL DEFAULT 'system',
    correlation_id TEXT,
    source_context_id TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_operational_audit_entity
    ON operational_audit_events(entity_type, entity_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_operational_audit_correlation
    ON operational_audit_events(correlation_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_operational_audit_actor
    ON operational_audit_events(actor_id, created_at DESC, id DESC);