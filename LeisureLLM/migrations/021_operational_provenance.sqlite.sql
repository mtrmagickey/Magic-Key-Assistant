-- Migration 021: Bidirectional operational provenance edges
-- Adds a generic edge model so canonical operational records can answer
-- why they exist and what evidence or blockers they connect to.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operational_provenance_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id TEXT NOT NULL UNIQUE,
    source_entity_type TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    target_entity_type TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    explanation TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_by_actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    source_context_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_entity_type, source_entity_id, target_entity_type, target_entity_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_operational_provenance_source
    ON operational_provenance_edges(source_entity_type, source_entity_id, relationship, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_provenance_target
    ON operational_provenance_edges(target_entity_type, target_entity_id, relationship, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_provenance_actor
    ON operational_provenance_edges(created_by_actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_provenance_created
    ON operational_provenance_edges(created_at DESC);