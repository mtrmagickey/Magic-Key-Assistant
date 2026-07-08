-- Migration 018: Canonical operational records
-- Adds a shared, metadata-driven operational record layer without
-- replacing existing authority tables.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operational_actors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_id TEXT NOT NULL UNIQUE,
    actor_kind TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(actor_kind, external_ref)
);

CREATE INDEX IF NOT EXISTS idx_operational_actors_kind_ref
    ON operational_actors(actor_kind, external_ref);

CREATE TABLE IF NOT EXISTS operational_record_types (
    record_type TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT,
    supports_owner INTEGER NOT NULL DEFAULT 1,
    supports_due_at INTEGER NOT NULL DEFAULT 0,
    supports_review_at INTEGER NOT NULL DEFAULT 0,
    supports_stale_after_at INTEGER NOT NULL DEFAULT 0,
    supports_rationale INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS operational_record_states (
    record_type TEXT NOT NULL,
    state TEXT NOT NULL,
    is_terminal INTEGER NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (record_type, state),
    FOREIGN KEY (record_type) REFERENCES operational_record_types(record_type) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS operational_record_transitions (
    record_type TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (record_type, from_state, to_state),
    FOREIGN KEY (record_type, from_state) REFERENCES operational_record_states(record_type, state) ON DELETE CASCADE,
    FOREIGN KEY (record_type, to_state) REFERENCES operational_record_states(record_type, state) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS operational_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_id TEXT NOT NULL UNIQUE,
    record_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL,
    owner_id INTEGER REFERENCES operational_actors(id) ON DELETE SET NULL,
    created_by_actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    updated_by_actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    source_context_id TEXT,
    workspace_scope TEXT,
    project_scope TEXT,
    due_at TEXT,
    stale_after_at TEXT,
    review_at TEXT,
    rationale TEXT,
    notes TEXT,
    canonical_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    archived_at TEXT,
    FOREIGN KEY (record_type) REFERENCES operational_record_types(record_type) ON DELETE RESTRICT,
    FOREIGN KEY (record_type, state) REFERENCES operational_record_states(record_type, state) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_operational_records_type_state
    ON operational_records(record_type, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_records_owner
    ON operational_records(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_records_source_context
    ON operational_records(source_context_id);
CREATE INDEX IF NOT EXISTS idx_operational_records_workspace
    ON operational_records(workspace_scope, project_scope);
CREATE INDEX IF NOT EXISTS idx_operational_records_due_at
    ON operational_records(due_at);
CREATE INDEX IF NOT EXISTS idx_operational_records_review_at
    ON operational_records(review_at);
CREATE INDEX IF NOT EXISTS idx_operational_records_stale_after_at
    ON operational_records(stale_after_at);

CREATE TABLE IF NOT EXISTS operational_record_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES operational_records(id) ON DELETE CASCADE,
    stable_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_state TEXT,
    new_state TEXT,
    actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    source_context_id TEXT,
    summary TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_operational_record_events_record
    ON operational_record_events(record_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_record_events_actor
    ON operational_record_events(actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_record_events_type
    ON operational_record_events(event_type, created_at DESC);

INSERT OR IGNORE INTO operational_record_types
    (record_type, display_name, description, supports_owner, supports_due_at, supports_review_at, supports_stale_after_at, supports_rationale)
VALUES
    ('action', 'Action', 'Trackable operational work item.', 1, 1, 1, 1, 1),
    ('decision', 'Decision', 'Recorded operational or strategic decision.', 1, 0, 1, 0, 1),
    ('blocker', 'Blocker', 'Active impediment to delivery or review.', 1, 0, 1, 1, 1),
    ('source_link', 'Source Link', 'Traceability link to an originating source or evidence item.', 0, 0, 1, 1, 1);

INSERT OR IGNORE INTO operational_record_states
    (record_type, state, is_terminal, is_default, description, sort_order)
VALUES
    ('action', 'open', 0, 1, 'Ready for ownership or execution.', 10),
    ('action', 'in_progress', 0, 0, 'Actively being worked.', 20),
    ('action', 'blocked', 0, 0, 'Cannot proceed until blocker is addressed.', 30),
    ('action', 'done', 1, 0, 'Completed successfully.', 40),
    ('action', 'canceled', 1, 0, 'Canceled intentionally.', 50),
    ('action', 'overdue', 0, 0, 'Past due and unresolved.', 60),
    ('action', 'stale', 0, 0, 'No recent movement and requires review.', 70),
    ('action', 'unowned', 0, 0, 'No owner is currently assigned.', 80),
    ('action', 'escalated', 0, 0, 'Escalated for attention or intervention.', 90),
    ('decision', 'proposed', 0, 1, 'Awaiting acceptance or rejection.', 10),
    ('decision', 'accepted', 1, 0, 'Accepted as the current decision.', 20),
    ('decision', 'rejected', 1, 0, 'Rejected after review.', 30),
    ('decision', 'superseded', 1, 0, 'Replaced by a newer accepted decision.', 40),
    ('decision', 'unresolved', 0, 0, 'Deferred or lacking agreement.', 50),
    ('blocker', 'open', 0, 1, 'Actively blocking execution or review.', 10),
    ('blocker', 'mitigated', 0, 0, 'Impact reduced but not fully resolved.', 20),
    ('blocker', 'resolved', 1, 0, 'No longer blocking.', 30),
    ('blocker', 'escalated', 0, 0, 'Raised for intervention.', 40),
    ('source_link', 'active', 0, 1, 'Source is reachable and current.', 10),
    ('source_link', 'stale', 0, 0, 'Source may still exist but needs review.', 20),
    ('source_link', 'broken', 0, 0, 'Source reference is not currently usable.', 30),
    ('source_link', 'archived', 1, 0, 'Source link retained only for history.', 40);

INSERT OR IGNORE INTO operational_record_transitions
    (record_type, from_state, to_state, description)
VALUES
    ('action', 'open', 'in_progress', 'Start execution.'),
    ('action', 'open', 'blocked', 'Blocked before progress could begin.'),
    ('action', 'open', 'done', 'Completed directly.'),
    ('action', 'open', 'canceled', 'Canceled before work began.'),
    ('action', 'open', 'overdue', 'Past due while still open.'),
    ('action', 'open', 'stale', 'Needs review after inactivity.'),
    ('action', 'open', 'unowned', 'No owner currently assigned.'),
    ('action', 'open', 'escalated', 'Requires intervention.'),
    ('action', 'in_progress', 'open', 'Returned to open work.'),
    ('action', 'in_progress', 'blocked', 'Blocked during execution.'),
    ('action', 'in_progress', 'done', 'Completed after execution.'),
    ('action', 'in_progress', 'canceled', 'Canceled during execution.'),
    ('action', 'in_progress', 'overdue', 'In progress but due date has passed.'),
    ('action', 'in_progress', 'stale', 'Execution stalled and needs review.'),
    ('action', 'in_progress', 'escalated', 'Escalated while in progress.'),
    ('action', 'blocked', 'open', 'Reopened after blocker changed.'),
    ('action', 'blocked', 'in_progress', 'Blocker cleared and work resumed.'),
    ('action', 'blocked', 'canceled', 'Canceled because blocker persisted.'),
    ('action', 'blocked', 'escalated', 'Blocked issue escalated.'),
    ('action', 'overdue', 'in_progress', 'Resumed after overdue state.'),
    ('action', 'overdue', 'blocked', 'Overdue and blocked.'),
    ('action', 'overdue', 'done', 'Completed after being overdue.'),
    ('action', 'overdue', 'canceled', 'Canceled after being overdue.'),
    ('action', 'overdue', 'stale', 'Still inactive after becoming overdue.'),
    ('action', 'overdue', 'escalated', 'Escalated due to overdue state.'),
    ('action', 'stale', 'open', 'Returned to active review backlog.'),
    ('action', 'stale', 'in_progress', 'Work resumed after stale state.'),
    ('action', 'stale', 'blocked', 'Review found an active blocker.'),
    ('action', 'stale', 'done', 'Completed after stale review.'),
    ('action', 'stale', 'canceled', 'Canceled after stale review.'),
    ('action', 'stale', 'overdue', 'Reviewed as overdue.'),
    ('action', 'stale', 'escalated', 'Escalated after stale review.'),
    ('action', 'unowned', 'open', 'Ownership gap acknowledged but not assigned.'),
    ('action', 'unowned', 'in_progress', 'Ownership accepted and execution started.'),
    ('action', 'unowned', 'blocked', 'Cannot proceed without owner or dependency.'),
    ('action', 'unowned', 'canceled', 'Canceled while unowned.'),
    ('action', 'unowned', 'escalated', 'Escalated because ownership is missing.'),
    ('action', 'escalated', 'open', 'Returned from escalation to managed state.'),
    ('action', 'escalated', 'in_progress', 'Execution resumed after escalation.'),
    ('action', 'escalated', 'blocked', 'Escalation confirmed blocker persists.'),
    ('action', 'escalated', 'done', 'Resolved through escalation.'),
    ('action', 'escalated', 'canceled', 'Closed via escalation.'),
    ('decision', 'proposed', 'accepted', 'Approved as the current decision.'),
    ('decision', 'proposed', 'rejected', 'Rejected after review.'),
    ('decision', 'proposed', 'unresolved', 'Deferred pending more context.'),
    ('decision', 'accepted', 'superseded', 'Replaced by a newer decision.'),
    ('decision', 'unresolved', 'accepted', 'Resolved in favor of acceptance.'),
    ('decision', 'unresolved', 'rejected', 'Resolved in favor of rejection.'),
    ('blocker', 'open', 'mitigated', 'Impact reduced but not removed.'),
    ('blocker', 'open', 'resolved', 'Resolved fully.'),
    ('blocker', 'open', 'escalated', 'Requires escalation.'),
    ('blocker', 'mitigated', 'open', 'Mitigation no longer sufficient.'),
    ('blocker', 'mitigated', 'resolved', 'Resolved after mitigation.'),
    ('blocker', 'mitigated', 'escalated', 'Mitigation insufficient; escalate.'),
    ('blocker', 'escalated', 'open', 'Escalation reviewed but blocker remains open.'),
    ('blocker', 'escalated', 'mitigated', 'Escalation produced mitigation.'),
    ('blocker', 'escalated', 'resolved', 'Escalation resolved the blocker.'),
    ('source_link', 'active', 'stale', 'Needs review or refresh.'),
    ('source_link', 'active', 'broken', 'Reachability or integrity failed.'),
    ('source_link', 'active', 'archived', 'Archived intentionally.'),
    ('source_link', 'stale', 'active', 'Reviewed and reactivated.'),
    ('source_link', 'stale', 'broken', 'Confirmed broken.'),
    ('source_link', 'stale', 'archived', 'Archived after review.'),
    ('source_link', 'broken', 'active', 'Repaired or replaced.'),
    ('source_link', 'broken', 'archived', 'Archived after failure.');