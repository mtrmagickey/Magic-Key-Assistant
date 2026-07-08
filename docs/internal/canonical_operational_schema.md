# Canonical Operational Schema

Status: Draft
Date: 2026-03-18

## Purpose

This document describes the additive canonical operational record schema introduced for MKA so the system can behave more like a local-first operational continuity system for micro-teams.

This schema does not replace the current authority tables such as `tasks`, `decisions`, `knowledge_gaps`, or `leads`. It adds a shared continuity-oriented record model that can be adopted incrementally.

## Models Added

### `operational_actors`

Stores durable actor identities for any mutation source.

Fields:

- `id`
- `stable_id`
- `actor_kind`
- `external_ref`
- `display_name`
- `created_at`
- `updated_at`

Current intent:

- support web actors,
- support Discord actors,
- support system-job actors,
- keep all operational mutations attributable to a durable actor row.

### `operational_record_types`

Metadata table describing capabilities per canonical record type.

Current seeded types:

- `action`
- `decision`
- `blocker`
- `source_link`

Design note:

Future types such as `lead` and `knowledge_gap` can be introduced with insert-only metadata and state rows rather than a destructive table redesign.

### `operational_record_states`

Canonical state catalog keyed by `record_type` and `state`.

This decouples the schema from hardcoded table-specific `CHECK` clauses and makes new record types additive.

### `operational_record_transitions`

Canonical state transition table keyed by `record_type`, `from_state`, and `to_state`.

This table documents the state machine used by the shared validation layer and makes future record types insert-only.

### `operational_records`

Shared record table containing the fields required across the minimum operational record set.

Fields:

- `id`
- `stable_id`
- `record_type`
- `title`
- `summary`
- `state`
- `owner_id`
- `created_by_actor_id`
- `updated_by_actor_id`
- `source_context_id`
- `workspace_scope`
- `project_scope`
- `due_at`
- `stale_after_at`
- `review_at`
- `rationale`
- `notes`
- `canonical_payload_json`
- `created_at`
- `updated_at`
- `resolved_at`
- `archived_at`

Design note:

`canonical_payload_json` is reserved for type-specific structured fields that do not belong in the shared minimum set. That makes future `lead` and `knowledge_gap` adoption straightforward without forcing an early schema expansion.

### `operational_record_events`

Append-only mutation history for canonical operational records.

Fields:

- `id`
- `record_id`
- `stable_id`
- `event_type`
- `previous_state`
- `new_state`
- `actor_id`
- `source_context_id`
- `summary`
- `payload_json`
- `created_at`

This table provides restart-safe mutation attribution and a durable event stream for review surfaces.

## Shared Validation Layer

Python validation lives in `LeisureLLM/core/operational_records.py`.

It defines:

- canonical record types,
- canonical states,
- per-type state machines,
- shared field validation,
- cross-field constraints such as:
  - action records without an owner must be in `unowned`,
  - source-link records must carry `source_context_id`,
  - decisions cannot carry `due_at`,
  - timestamps must be ISO-8601 compatible.

The implementation uses the repo's existing symbolic state-machine helper rather than adding a second transition framework.

## Service Layer

The shared service lives in `LeisureLLM/core/services/operational_record_service.py`.

Current responsibilities:

- create or update durable actors,
- create canonical operational records,
- enforce per-type transition rules,
- append immutable record events,
- expose record and event retrieval for tests and future review surfaces.

## Canonical State Machines

### Actions

States:

- `open`
- `in_progress`
- `blocked`
- `done`
- `canceled`
- `overdue`
- `stale`
- `unowned`
- `escalated`

### Decisions

States:

- `proposed`
- `accepted`
- `rejected`
- `superseded`
- `unresolved`

### Blockers

States:

- `open`
- `mitigated`
- `resolved`
- `escalated`

### Source Links

States:

- `active`
- `stale`
- `broken`
- `archived`

## Routes and Background Jobs

This schema now has initial web adoption.

Current route adoption:

- action creation and mutation routes in the web console now write attributable canonical records and events,
- decision creation in the web console now writes attributable canonical records and events,
- web identity now resolves to durable `operational_actors` rows before those writes occur.

This task did not add new background jobs.

The schema and service were intentionally added first so existing web and Discord surfaces can migrate onto the canonical model without a speculative UI pass.

## Migration Plan

Implemented migration:

- `018_canonical_operational_records.sqlite.sql`
- `019_web_identity_and_actor_attribution.sqlite.sql`

Recommended next migration steps after adoption begins:

1. Add bridging migrations that link legacy `tasks` and `decisions` rows to canonical `operational_records` rows.
2. Add metadata rows and states for `lead` and `knowledge_gap` without changing the shared table shape.
3. Add audit joins or review surfaces once mutation paths begin writing canonical records in web mode.
