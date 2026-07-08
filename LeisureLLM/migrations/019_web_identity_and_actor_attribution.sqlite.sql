-- Migration 019: durable web identities, sessions, and actor attribution backfill

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO operational_actors (
    stable_id,
    actor_kind,
    external_ref,
    display_name,
    created_at,
    updated_at
)
VALUES (
    'actor_system_legacy_backfill',
    'system',
    'legacy-backfill',
    'Legacy Backfill System',
    datetime('now'),
    datetime('now')
);

CREATE TABLE IF NOT EXISTS web_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    username_normalized TEXT NOT NULL UNIQUE,
    display_name TEXT,
    role TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'member')),
    password_hash TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
    is_active INTEGER NOT NULL DEFAULT 1,
    bootstrap_source TEXT,
    created_by_account_id INTEGER REFERENCES web_accounts(id) ON DELETE SET NULL,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_web_accounts_role
    ON web_accounts(role, is_active);

CREATE TABLE IF NOT EXISTS web_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_id TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
    session_token_hash TEXT NOT NULL UNIQUE,
    ip_address TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_account
    ON web_sessions(account_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry
    ON web_sessions(expires_at, revoked_at);

CREATE TABLE IF NOT EXISTS operational_record_legacy_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES operational_records(id) ON DELETE CASCADE,
    legacy_table TEXT NOT NULL,
    legacy_id INTEGER NOT NULL,
    linked_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(legacy_table, legacy_id),
    UNIQUE(record_id, legacy_table, legacy_id)
);

CREATE INDEX IF NOT EXISTS idx_operational_record_legacy_links_record
    ON operational_record_legacy_links(record_id, linked_at DESC);

INSERT INTO operational_records (
    stable_id,
    record_type,
    title,
    summary,
    state,
    owner_id,
    created_by_actor_id,
    updated_by_actor_id,
    source_context_id,
    workspace_scope,
    project_scope,
    due_at,
    stale_after_at,
    review_at,
    rationale,
    notes,
    canonical_payload_json,
    created_at,
    updated_at,
    resolved_at,
    archived_at
)
SELECT
    'oprec_legacy_task_' || t.id,
    'action',
    COALESCE(NULLIF(TRIM(t.title), ''), 'Legacy action #' || t.id),
    NULLIF(TRIM(t.description), ''),
    CASE
        WHEN LOWER(COALESCE(t.status, 'todo')) = 'done' THEN 'done'
        WHEN LOWER(COALESCE(t.status, 'todo')) = 'cancelled' THEN 'canceled'
        WHEN LOWER(COALESCE(t.status, 'todo')) = 'in_progress' THEN 'in_progress'
        WHEN LOWER(COALESCE(t.status, 'todo')) = 'blocked' THEN 'blocked'
        WHEN COALESCE(NULLIF(TRIM(t.assigned_to_username), ''), '') = '' THEN 'unowned'
        ELSE 'open'
    END,
    NULL,
    sys.id,
    sys.id,
    'legacy:tasks:' || t.id,
    'default',
    CASE WHEN t.project_id IS NOT NULL THEN CAST(t.project_id AS TEXT) END,
    t.due_date,
    CASE
        WHEN LOWER(COALESCE(t.status, 'todo')) IN ('done', 'cancelled') THEN NULL
        ELSE datetime(COALESCE(t.updated_at, t.created_at, datetime('now')), '+14 days')
    END,
    NULL,
    NULL,
    t.notes,
    json_object(
        'legacy_table', 'tasks',
        'legacy_id', t.id,
        'legacy_status', t.status,
        'assigned_to_user_id', t.assigned_to_user_id,
        'assigned_to_username', t.assigned_to_username,
        'created_by_user_id', t.created_by_user_id,
        'created_by_username', t.created_by_username,
        'priority', t.priority,
        'migration_version', 19
    ),
    COALESCE(t.created_at, datetime('now')),
    COALESCE(t.updated_at, t.created_at, datetime('now')),
    CASE
        WHEN LOWER(COALESCE(t.status, 'todo')) = 'done' THEN COALESCE(t.completed_at, t.updated_at, t.created_at, datetime('now'))
    END,
    CASE
        WHEN LOWER(COALESCE(t.status, 'todo')) = 'cancelled' THEN COALESCE(t.updated_at, t.created_at, datetime('now'))
    END
FROM tasks t
CROSS JOIN (
    SELECT id FROM operational_actors WHERE actor_kind = 'system' AND external_ref = 'legacy-backfill'
) AS sys
LEFT JOIN operational_record_legacy_links l
    ON l.legacy_table = 'tasks' AND l.legacy_id = t.id
WHERE l.id IS NULL;

INSERT OR IGNORE INTO operational_record_legacy_links (record_id, legacy_table, legacy_id, linked_at)
SELECT r.id, 'tasks', t.id, datetime('now')
FROM tasks t
JOIN operational_records r ON r.stable_id = 'oprec_legacy_task_' || t.id;

INSERT INTO operational_record_events (
    record_id,
    stable_id,
    event_type,
    previous_state,
    new_state,
    actor_id,
    source_context_id,
    summary,
    payload_json,
    created_at
)
SELECT
    r.id,
    r.stable_id,
    'backfilled_from_legacy',
    NULL,
    r.state,
    sys.id,
    'migration:019',
    'Backfilled legacy task without durable actor attribution.',
    json_object('legacy_table', 'tasks', 'legacy_id', t.id, 'provenance', 'system-backfill', 'migration_version', 19),
    COALESCE(r.created_at, datetime('now'))
FROM tasks t
JOIN operational_records r ON r.stable_id = 'oprec_legacy_task_' || t.id
CROSS JOIN (
    SELECT id FROM operational_actors WHERE actor_kind = 'system' AND external_ref = 'legacy-backfill'
) AS sys
LEFT JOIN operational_record_events e
    ON e.record_id = r.id AND e.event_type = 'backfilled_from_legacy'
WHERE e.id IS NULL;

INSERT INTO operational_records (
    stable_id,
    record_type,
    title,
    summary,
    state,
    owner_id,
    created_by_actor_id,
    updated_by_actor_id,
    source_context_id,
    workspace_scope,
    project_scope,
    due_at,
    stale_after_at,
    review_at,
    rationale,
    notes,
    canonical_payload_json,
    created_at,
    updated_at,
    resolved_at,
    archived_at
)
SELECT
    'oprec_legacy_decision_' || d.id,
    'decision',
    COALESCE(NULLIF(TRIM(d.title), ''), 'Legacy decision #' || d.id),
    COALESCE(NULLIF(TRIM(d.description), ''), NULLIF(TRIM(d.context), '')),
    CASE
        WHEN d.superseded_by_decision_id IS NOT NULL THEN 'superseded'
        ELSE 'accepted'
    END,
    NULL,
    sys.id,
    sys.id,
    'legacy:decisions:' || d.id,
    'default',
    CASE WHEN d.related_project_id IS NOT NULL THEN CAST(d.related_project_id AS TEXT) END,
    NULL,
    NULL,
    d.reviewed_at,
    d.rationale,
    NULL,
    json_object(
        'legacy_table', 'decisions',
        'legacy_id', d.id,
        'decision', d.decision,
        'decided_by', d.decided_by,
        'impact', d.impact,
        'category', d.category,
        'superseded_by_decision_id', d.superseded_by_decision_id,
        'migration_version', 19
    ),
    COALESCE(d.decided_at, datetime('now')),
    COALESCE(d.reviewed_at, d.decided_at, datetime('now')),
    COALESCE(d.decided_at, datetime('now')),
    NULL
FROM decisions d
CROSS JOIN (
    SELECT id FROM operational_actors WHERE actor_kind = 'system' AND external_ref = 'legacy-backfill'
) AS sys
LEFT JOIN operational_record_legacy_links l
    ON l.legacy_table = 'decisions' AND l.legacy_id = d.id
WHERE l.id IS NULL;

INSERT OR IGNORE INTO operational_record_legacy_links (record_id, legacy_table, legacy_id, linked_at)
SELECT r.id, 'decisions', d.id, datetime('now')
FROM decisions d
JOIN operational_records r ON r.stable_id = 'oprec_legacy_decision_' || d.id;

INSERT INTO operational_record_events (
    record_id,
    stable_id,
    event_type,
    previous_state,
    new_state,
    actor_id,
    source_context_id,
    summary,
    payload_json,
    created_at
)
SELECT
    r.id,
    r.stable_id,
    'backfilled_from_legacy',
    NULL,
    r.state,
    sys.id,
    'migration:019',
    'Backfilled legacy decision without durable actor attribution.',
    json_object('legacy_table', 'decisions', 'legacy_id', d.id, 'provenance', 'system-backfill', 'migration_version', 19),
    COALESCE(r.created_at, datetime('now'))
FROM decisions d
JOIN operational_records r ON r.stable_id = 'oprec_legacy_decision_' || d.id
CROSS JOIN (
    SELECT id FROM operational_actors WHERE actor_kind = 'system' AND external_ref = 'legacy-backfill'
) AS sys
LEFT JOIN operational_record_events e
    ON e.record_id = r.id AND e.event_type = 'backfilled_from_legacy'
WHERE e.id IS NULL;

INSERT INTO operational_records (
    stable_id,
    record_type,
    title,
    summary,
    state,
    owner_id,
    created_by_actor_id,
    updated_by_actor_id,
    source_context_id,
    workspace_scope,
    project_scope,
    due_at,
    stale_after_at,
    review_at,
    rationale,
    notes,
    canonical_payload_json,
    created_at,
    updated_at,
    resolved_at,
    archived_at
)
SELECT
    'oprec_legacy_source_link_' || sl.id,
    'source_link',
    'Legacy source link #' || sl.id,
    COALESCE(NULLIF(TRIM(sl.source_type), ''), 'related'),
    'active',
    NULL,
    sys.id,
    sys.id,
    'legacy:source_links:' || sl.id,
    'default',
    NULL,
    NULL,
    datetime(COALESCE(sl.created_at, datetime('now')), '+30 days'),
    NULL,
    COALESCE(NULLIF(TRIM(sl.source_type), ''), 'related'),
    NULL,
    json_object(
        'legacy_table', 'source_links',
        'legacy_id', sl.id,
        'record_type', sl.record_type,
        'record_id', sl.record_id,
        'source_type', sl.source_type,
        'source_id', sl.source_id,
        'metadata', sl.metadata,
        'migration_version', 19
    ),
    COALESCE(sl.created_at, datetime('now')),
    COALESCE(sl.created_at, datetime('now')),
    NULL,
    NULL
FROM source_links sl
CROSS JOIN (
    SELECT id FROM operational_actors WHERE actor_kind = 'system' AND external_ref = 'legacy-backfill'
) AS sys
LEFT JOIN operational_record_legacy_links l
    ON l.legacy_table = 'source_links' AND l.legacy_id = sl.id
WHERE l.id IS NULL;

INSERT OR IGNORE INTO operational_record_legacy_links (record_id, legacy_table, legacy_id, linked_at)
SELECT r.id, 'source_links', sl.id, datetime('now')
FROM source_links sl
JOIN operational_records r ON r.stable_id = 'oprec_legacy_source_link_' || sl.id;

INSERT INTO operational_record_events (
    record_id,
    stable_id,
    event_type,
    previous_state,
    new_state,
    actor_id,
    source_context_id,
    summary,
    payload_json,
    created_at
)
SELECT
    r.id,
    r.stable_id,
    'backfilled_from_legacy',
    NULL,
    r.state,
    sys.id,
    'migration:019',
    'Backfilled legacy source link without durable actor attribution.',
    json_object('legacy_table', 'source_links', 'legacy_id', sl.id, 'provenance', 'system-backfill', 'migration_version', 19),
    COALESCE(r.created_at, datetime('now'))
FROM source_links sl
JOIN operational_records r ON r.stable_id = 'oprec_legacy_source_link_' || sl.id
CROSS JOIN (
    SELECT id FROM operational_actors WHERE actor_kind = 'system' AND external_ref = 'legacy-backfill'
) AS sys
LEFT JOIN operational_record_events e
    ON e.record_id = r.id AND e.event_type = 'backfilled_from_legacy'
WHERE e.id IS NULL;