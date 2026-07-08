-- Migration 022: Extraction proposals for operational record review
-- Stores uncertain extractions as auditable proposals that humans can
-- accept, edit, reject, or merge into canonical operational records.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operational_extraction_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL UNIQUE,
    record_type TEXT NOT NULL CHECK (record_type IN ('action', 'decision', 'blocker', 'source_link')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected', 'merged')),
    title TEXT NOT NULL,
    summary TEXT,
    extracted_fields_json TEXT NOT NULL DEFAULT '{}',
    final_fields_json TEXT,
    field_confidence_json TEXT NOT NULL DEFAULT '{}',
    record_confidence REAL NOT NULL DEFAULT 0.0,
    effective_confidence REAL NOT NULL DEFAULT 0.0,
    rationale TEXT,
    supporting_snippet TEXT,
    source_entity_type TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    source_context_id TEXT,
    source_details_json TEXT NOT NULL DEFAULT '{}',
    extraction_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_by_actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    reviewed_by_actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    canonical_record_id INTEGER REFERENCES operational_records(id) ON DELETE SET NULL,
    merged_into_record_id INTEGER REFERENCES operational_records(id) ON DELETE SET NULL,
    review_notes TEXT,
    review_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_operational_extraction_proposals_status_confidence
    ON operational_extraction_proposals(status, effective_confidence ASC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_operational_extraction_proposals_type_status
    ON operational_extraction_proposals(record_type, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_extraction_proposals_source
    ON operational_extraction_proposals(source_entity_type, source_entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_extraction_proposals_context
    ON operational_extraction_proposals(source_context_id, created_at DESC);

CREATE TABLE IF NOT EXISTS operational_extraction_proposal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_row_id INTEGER NOT NULL REFERENCES operational_extraction_proposals(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    previous_status TEXT,
    new_status TEXT,
    summary TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_operational_extraction_proposal_events_proposal
    ON operational_extraction_proposal_events(proposal_row_id, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_operational_extraction_proposal_events_actor
    ON operational_extraction_proposal_events(actor_id, created_at DESC);