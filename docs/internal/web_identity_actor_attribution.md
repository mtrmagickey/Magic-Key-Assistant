# Web Identity And Actor Attribution

Status: Draft
Date: 2026-03-18

## Purpose

This document describes the durable web identity layer added so the admin console behaves like a local-first operational continuity system for micro-teams instead of a shared single-user workspace.

The implementation keeps existing behavior additive:

- the legacy local bootstrap token still exists,
- web sessions now resolve to durable user accounts,
- web mutations can now be attributed to a distinct actor,
- legacy unattributed rows can be backfilled with explicit provenance.

## Models Added

### `web_accounts`

Durable web-console accounts.

Fields:

- `id`
- `stable_id`
- `username`
- `username_normalized`
- `display_name`
- `role`
- `password_hash`
- `actor_id`
- `is_active`
- `bootstrap_source`
- `created_by_account_id`
- `last_login_at`
- `created_at`
- `updated_at`

Current roles:

- `admin`
- `manager`
- `member`

### `web_sessions`

Persistent authenticated session records for the web console.

Fields:

- `id`
- `stable_id`
- `account_id`
- `session_token_hash`
- `ip_address`
- `user_agent`
- `created_at`
- `last_seen_at`
- `expires_at`
- `revoked_at`

### `operational_record_legacy_links`

Bridges legacy authority rows to canonical `operational_records` rows.

Fields:

- `id`
- `record_id`
- `legacy_table`
- `legacy_id`
- `linked_at`

This makes additive adoption practical without rewriting every authority table first.

## Services Added

### `LeisureLLM/core/actors.py`

Shared actor abstraction used by the web path and designed to be usable by Discord and system-job paths.

Key pieces:

- `ActorContext`
- role normalization helpers
- role ordering helpers

### `LeisureLLM/core/services/web_identity_service.py`

Web identity and session service.

Current responsibilities:

- create runtime-safe auth tables if missing,
- bootstrap the first admin from the local bootstrap token,
- create admin-managed web users,
- hash and verify passwords,
- issue, resolve, and revoke durable sessions,
- return request-safe actor context for authenticated web users.

### `LeisureLLM/core/services/operational_record_service.py`

Extended in this task with:

- legacy-link lookup,
- legacy-link creation,
- metadata updates for canonical records.

## Routes Added Or Changed

### Auth routes in `LeisureLLM/admin/server.py`

Changed:

- `GET /login`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/status`
- `GET /api/v1/auth/reveal-token`

Added:

- `POST /api/v1/auth/bootstrap`

Current behavior:

- no existing web accounts: bootstrap admin is required,
- bootstrap uses the existing local token file,
- subsequent sign-in uses username and password,
- authenticated sessions use the `mka_session` cookie,
- auth status includes current user identity and bootstrap state.

### Account management routes in `LeisureLLM/admin/routers/accounts.py`

Added:

- `GET /api/v1/admin/users`
- `POST /api/v1/admin/users`

Current behavior:

- admin only,
- minimal admin-created-user flow,
- no invitation UX,
- no SSO.

### Dependency and guard changes in `LeisureLLM/admin/dependencies.py`

Added:

- request-scoped current actor resolution,
- member, manager, and admin role guards,
- auth-disabled synthetic actor context for test and bypass mode.

Current guard shape:

- `require_member`: authenticated session required,
- `require_manager`: manager or admin required,
- `require_admin`: admin required.

### Artifact mutation routes in `LeisureLLM/admin/routers/artifacts.py`

Changed mutation paths:

- `POST /api/v1/actions`
- `PATCH /api/v1/actions/{action_id}`
- `POST /api/v1/actions/{action_id}/done`
- `POST /api/v1/actions/{action_id}/cancel`
- `POST /api/v1/decisions`

Current behavior:

- web requests now resolve to a current actor,
- action ownership attempts to resolve assigned web usernames into canonical owner actors,
- canonical operational records and events now persist attributable create and update history,
- legacy `tasks` and `decisions` rows remain in place.

## Migration Strategy

Added migration:

- `019_web_identity_and_actor_attribution.sqlite.sql`

Current migration responsibilities:

- create `web_accounts`, `web_sessions`, and `operational_record_legacy_links`,
- seed a durable `legacy-backfill` system actor,
- backfill legacy `tasks`, `decisions`, and `source_links` into canonical `operational_records`,
- write explicit `backfilled_from_legacy` event provenance for those records.

Backfill policy:

- unattributed legacy rows map to the `legacy-backfill` system actor,
- new bootstrap-admin systems attribute forward mutations to real web actors,
- provenance remains explicit in canonical event payloads.

## Background Jobs

This task did not add background jobs.

## Tests Added Or Updated

Updated:

- `tests/test_admin_gui.py`

Added:

- `tests/test_web_identity_actor_attribution.py`

Coverage in this task includes:

- bootstrap admin creation,
- username/password login,
- durable session behavior,
- admin-only user creation,
- role enforcement,
- proof that two web users are distinguishable in stored action mutations.