# Unified Review Queue Architecture

## Purpose

The unified review queue provides one canonical query path for mixed reviewable work across:

- extraction proposals awaiting human judgment
- operational continuity states that indicate overdue, unowned, unresolved, or escalated work

The queue is intentionally derived. It does not replace the source-of-truth records in:

- `operational_extraction_proposals`
- `operational_records`
- `operational_continuity_states`

It adds a thin overlay for review-specific controls that do not belong in those source tables, such as deferrals, escalation routing metadata, recurring review snapshots, and completion markers.

## Canonical Queue Model

`ReviewQueueService` normalizes all supported reviewable work into one service-level DTO: `ReviewQueueItem`.

Each queue item exposes:

- stable queue item id
- `item_type`
- `severity`
- `created_at`, `detected_at`, `last_seen_at`
- owner, project, and workspace scope where available
- linked `operational_record_id` or `proposal_id`
- linked provenance or source references
- human-readable reason
- recommended next actions
- deferral and escalation overlay fields

Current item types:

- `extraction_proposal_low_confidence`
- `extraction_proposal_pending_human_review`
- `action_overdue`
- `action_unowned`
- `decision_unresolved`
- `blocker_escalated_or_stale`

## Source Adapters

### Extraction proposals

Pending extraction proposals are read from `operational_extraction_proposals`.

- Low-confidence proposals become `extraction_proposal_low_confidence`.
- Other pending proposals become `extraction_proposal_pending_human_review`.
- Proposal source details and source entity references are carried into queue items directly.

### Continuity states

Active continuity states are read from `operational_continuity_states` joined to `operational_records`.

- `overdue` on action records becomes `action_overdue`
- `unowned` on action records becomes `action_unowned`
- `unresolved` on decision records becomes `decision_unresolved`
- `stale` and `escalated` on blocker records are deduplicated into one `blocker_escalated_or_stale` item

Record-backed queue items enrich themselves with provenance-derived source references through `ProvenanceService`.

## Persisted Overlay State

Migration `024_operational_review_queue.sqlite.sql` adds the following overlay tables:

### `operational_review_queue_state`

Stores durable review-control metadata for each queue item:

- deferral timestamp and rationale
- deferral count
- severity override
- escalation destination metadata
- resolved marker and rationale
- last queue action metadata

This table is not a second truth for the item itself. If the underlying proposal or continuity condition still exists, the queue item is still derived from that canonical source.

### `operational_review_queue_actions`

Append-only history of queue actions such as:

- defer
- escalate
- proposal accepted from queue
- proposal rejected from queue
- owner assigned from queue
- review resolved

### `operational_review_sessions`

Represents generated daily or weekly review sessions.

Each session stores:

- cadence
- scope filters
- snapshot summary
- created and completed markers

### `operational_review_session_items`

Stores the item snapshot captured in a generated review session.

This is intentionally snapshot data so a daily or weekly review can be reconstructed later even if the live queue changes.

## Policy Engine

`ReviewQueuePolicy` governs review cadence rules.

Current policy behavior:

- deferrals are capped by item type and severity
- overdue actions cannot be deferred indefinitely
- repeated deferrals raise severity
- repeated deferrals can auto-flag escalation routing metadata
- unresolved decisions above the configured threshold must still surface in weekly review snapshots even when deferred

Current default examples:

- overdue actions: short defer window
- critical items: very short defer window
- repeated deferral threshold: severity bump after the second defer, escalation flag after the third defer

## Queue Actions

Queue actions always mutate the canonical underlying object when one exists.

### Proposal-backed actions

- accept
- accept with edits
- merge
- reject

These call `ExtractionProposalService`, which writes the canonical record and provenance edge where appropriate.

### Record-backed actions

- assign owner
- resolve by state transition
- defer
- escalate

Assignment and resolution use `OperationalRecordService`, then run a continuity sweep so the live queue reflects the new underlying state.

## Auditability

Every queue action writes two durable records:

1. A row in `operational_review_queue_actions`
2. An append-only audit event in `operational_audit_events`

This preserves actor attribution for:

- deferrals
- escalations
- queue-driven proposal review
- owner assignment
- queue-driven resolution
- review session generation and completion

## API Surface

The unified API is exposed through `admin/routers/review_queue.py`.

Current endpoints:

- `GET /api/v1/review-queue`
- `GET /api/v1/review-queue/{item_id}`
- `POST /api/v1/review-queue/{item_id}/actions`
- `POST /api/v1/review-queue/bulk-actions`
- `POST /api/v1/review-queue/sessions`
- `GET /api/v1/review-queue/sessions/{session_id}`
- `POST /api/v1/review-queue/sessions/{session_id}/complete`

## Web-Only Mode

The queue logic lives entirely in core services and admin routes.

- web-only mode fully supports aggregation, actioning, deferral, escalation, and review sessions
- Discord can consume the same service later
- Discord does not own queue logic or queue state

## Testing Strategy

The queue is validated at two layers:

- service tests for aggregation, deduplication, policy, and recurrence
- web-only API tests for mixed queue listing, canonical mutation paths, audit writes, bulk actions, and session completion