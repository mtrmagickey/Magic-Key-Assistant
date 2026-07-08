# RFC: Local-First Operational Continuity for Micro-Teams

Status: Draft
Date: 2026-03-18
Authors: GitHub Copilot audit based on current LeisureCenterAssistant / LeisureLLM codebase

## 1. Executive Summary

Magic Key Assistant already contains many of the right primitives for operational continuity: typed records for actions, decisions, meetings, obligations, gaps, leads, job runs, request traces, and a minimum viable work-packet kernel. The current problem is not a total absence of state. The problem is that continuity, accountability, and review are split across parallel paths with inconsistent contracts.

The main gaps are:

- Web mutations are durable but usually anonymous.
- Discord mutations usually capture actors, but web and background jobs do not do so consistently.
- Work packets exist, but most web CRUD paths bypass them.
- Traceability between message, request, meeting, decision, action, and review state is partial and schema contracts are inconsistent.
- Confidence handling exists for retrieval and extraction, but the human correction loop is not uniformly attributable or reviewable.
- Audit history is fragmented across business tables, packet events, request traces, lead activity, receipts, and job runs.

The recommended direction is not to replace the existing typed tables with one generic artifact table. The recommended direction is to keep typed authority tables and add a continuity envelope around them:

- stable actor identity,
- append-only audit events,
- normalized cross-record provenance links,
- work packets as the cross-surface continuity engine,
- explicit review states for machine-suggested records,
- and an attributable confidence correction loop.

Recommendation: start with actions, decisions, blocked work, and source links before leads and knowledge gaps. That slice is the narrowest path that improves accountability, traceability, and reviewability for daily micro-team operations without reopening the whole product surface at once.

## 2. Scope and Intent

This RFC audits the current codebase and defines a target architecture for:

1. typed operational records,
2. per-user accountability,
3. durable audit trail,
4. cross-surface continuity engine,
5. review workflows,
6. confidence and human correction.

This RFC does not implement the feature set. It identifies the thinnest vertical slice that proves the concept end to end.

## 3. Current-State Audit

### 3.1 Typed Records Already Present

The repo already persists these typed records in SQLite:

- `tasks`: operational action items with creator and assignee fields.
- `decisions`: strategic or operational decisions with rationale and timestamps.
- `meeting_notes`: captured meeting summaries and raw notes.
- `knowledge_gaps`: unresolved questions with curation and resolution metadata.
- `obligations`: recurring operational requirements.
- `leads` and `lead_activity`: pipeline records plus a lead-specific audit trail.
- `work_packets`, `packet_links`, `packet_events`: a continuity kernel already designed as a workflow-state envelope.
- `request_traces` and `request_stage_events`: durable request lineage.
- `job_runs`: scheduled job execution history.
- `learning_loop_events`, `response_feedback`, `chunk_quality_scores`, `improvement_signals`: the confidence and correction loop.
- `inbox_threads`, `inbox_messages`, `interview_sessions`: review and async follow-up surfaces.

Conclusion: the codebase already has typed artifacts. The missing layer is not storage capacity. It is a consistent accountability and continuity model that binds those artifacts together across web, Discord, and background execution.

### 3.2 Actor Identity Model Today

#### Discord

Discord is the strongest actor model in the repo today.

- Slash commands and context actions commonly store `interaction.user.id` and `interaction.user.display_name` or username.
- Action, meeting, gap, and persona creation paths often write explicit creator fields.
- Receipts and command usage tracking also preserve user identity.

#### Web

Web mode uses a single admin token gate.

- `admin/dependencies.py` authenticates requests with a bearer token or cookie.
- The auth model proves access, but it does not identify which human performed the mutation.
- Most web routes operate as an anonymous `web-user`, `0`, or no actor at all.

#### Background Jobs

Background jobs are durable, but identity is coarse.

- `job_runs` persists run metadata.
- `packet_events` can store `actor_kind` and `actor_ref`.
- Most scheduled logic still records generic service or system actors rather than a request-linked causal chain.

Conclusion: the repo has authorization for web, identity for Discord, and execution history for jobs, but no shared actor model across all three.

### 3.3 Mutation Paths

There are three mutation styles in the codebase.

#### A. Service-backed typed mutations

Examples:

- `core/services/action_service.py`
- `core/services/decision_service.py`
- `core/services/obligation_service.py`
- `core/services/work_packet_service.py`

These are the best foundation for continuity because they can be wrapped with attribution and audit behavior.

#### B. Router-level raw SQL mutations

Examples:

- `admin/routers/artifacts.py`
- `admin/routers/knowledge.py`
- parts of `admin/routers/inbox.py`
- parts of `admin/routers/chat.py`

These routes often bypass the domain services and therefore also bypass any future shared mutation contract. This is the biggest architectural reason accountability is inconsistent.

#### C. Discord cog mutations

Examples:

- `cogs/ActionItems.py`
- `cogs/DocumentAuthor.py`
- `cogs/KnowledgeGapTracker.py`
- `cogs/AutonomousOps.py`

These paths often capture actor data correctly, but they do not uniformly emit normalized provenance links, request links, or audit events that web can also consume.

### 3.4 Scheduled Jobs and Continuity Logic

Current scheduled and continuity-oriented logic exists in multiple places:

- `core/sweep_jobs.py`: obligation sweep, SOP drift, rail escalation, forward planner, invariant checks.
- `cogs/AutonomousOps.py`: recurring operational jobs, persona jobs, partner updates, async meeting support.
- persona mixins in `cogs/personas/`: Scout, Dreamer, Rainmaker, Steward, Curator.
- `job_runs` and `autonomous_posts`: job-level persistence and post audit.

The durable job substrate exists. The missing piece is consistent linkage from:

- triggering request or human review,
- to job run,
- to records created or mutated,
- to continuity state transition.

### 3.5 Review Surfaces Already Present

Existing review surfaces are stronger than the underlying continuity model.

- `admin/routers/activity.py`: unified feed for jobs, learning events, tools, and packet events.
- `admin/routers/knowledge.py`: gap browsing, curation, answering, bulk review.
- `admin/routers/inbox.py`: async inbox, interview flows, human follow-up.
- `admin/routers/continuity.py`: obligations and feedback.
- `admin/routers/artifacts.py`: actions, leads, meetings.
- `admin/routers/chat.py`: request traces, feedback, chat generation.

Conclusion: the UI and API already expose multiple review surfaces, but the underlying state model is not yet strict enough for them to function as a reliable operational review system.

### 3.6 Traceability Between Messages, Meetings, Decisions, Actions, and Sources

The repo has traceability pieces, but they are incomplete and inconsistent.

What already exists:

- meeting notes can link to actions and decisions,
- tasks and decisions can carry source meeting references,
- `source_links` exists in both migration history and fallback schema,
- request traces can store produced artifact references,
- packet links can connect work packets to typed records.

Observed problems:

- the `source_links` contract drifted between migration 004 and the fallback schema in `database.py`,
- `MeetingService` still reflects older column and link assumptions,
- work packets are not the default wrapper for actions and decisions created through the web API,
- request traces live in a durable sidecar database, but business mutations do not always carry back a request identifier.

Conclusion: provenance exists, but it is not yet trustworthy enough to reconstruct why a record exists, who created it, what source justified it, and what review state it is in.

### 3.7 Extraction Pipeline and Confidence Handling

The codebase already has a serious local-first extraction and correction pipeline.

- `services/chunk_enrichment.py` extracts structured chunk metadata and confidence.
- `services/answer_self_assessment.py` scores answer grounding and gap likelihood.
- `services/feedback_learning_loop.py` records chunk quality, prompt variants, and improvement signals.
- chat and inbox routes can write response feedback.

Observed problems:

- web chat feedback is usually anonymous,
- inbox and chat feedback paths are not fully normalized around one correction contract,
- confidence can trigger gaps, but the resulting human correction path is not always attributable,
- confidence and review state are not yet promoted to first-class concepts for operational records such as proposed action or proposed decision.

Conclusion: the confidence loop is relatively advanced for retrieval quality, but not yet integrated into operational continuity.

### 3.8 Persistence Guarantees for Audit History

Strong points:

- SQLite in WAL mode,
- numbered migrations,
- sidecar request-trace persistence across restart,
- durable job and packet event tables,
- append-only style in several history tables.

Weak points:

- no generic append-only audit log for all record mutations,
- some packet snapshots are optional rather than enforced by contract,
- web-mode actor identity is usually lost at write time,
- some review flows update authority tables directly without a corresponding shared mutation event.

## 4. Architectural Gaps That Matter Most

The most important gaps to close are:

1. Missing cross-surface actor identity.
2. Missing append-only mutation audit trail.
3. Web CRUD paths bypassing the continuity kernel.
4. Provenance schema drift around sources and meetings.
5. Review surfaces operating on partially attributable state.
6. Confidence-driven artifacts lacking explicit human correction and approval states.

## 5. Target Architecture

### 5.1 Principle: Keep Typed Authority Tables

Do not replace `tasks`, `decisions`, `meeting_notes`, `knowledge_gaps`, `obligations`, or `leads` with one generic artifacts table.

Instead:

- keep each typed table authoritative for its own business facts,
- standardize mutation entry through services,
- and wrap those records in a continuity layer for attribution, provenance, review, and cross-surface follow-through.

### 5.2 Target Component 1: Typed Operational Records

The operational core for micro-teams should initially be:

- action,
- decision,
- meeting note,
- source link,
- blocked work state.

For the first slice, a standalone `blockers` table is not required. Use blocked status plus explicit blocked reason in authority and continuity state first. A dedicated blocker record can be added later if blocked work becomes a materially different object than blocked action.

Target pattern:

- meeting note records the source discussion,
- decision captures commitment or conclusion,
- action captures assigned operational follow-through,
- source/provenance links connect those records to meeting, message, request, document, or URL,
- work packet represents the cross-surface lifecycle of execution and review.

### 5.3 Target Component 2: Per-User Accountability

Add a shared actor model.

#### New model: `actors`

Suggested fields:

- `actor_key` primary key,
- `actor_kind` enum: `discord_user`, `web_user`, `system_job`, `service`,
- `surface` enum: `discord`, `web`, `system`,
- `external_id`,
- `display_name`,
- `auth_source`,
- `created_at`,
- `last_seen_at`,
- `is_active`.

Rules:

- every mutation resolves to an `actor_key`,
- Discord users map from Discord IDs,
- web mode resolves a local human identity rather than anonymous admin token access,
- system jobs still get actor rows, but with stable job-specific identity rather than generic `system`.

This is the minimum step required to satisfy the product direction that all state mutations must be attributable to an actor and persist across restart.

### 5.4 Target Component 3: Durable Audit Trail

Add an append-only audit model rather than trying to infer history from current rows.

#### New model: `audit_events`

Suggested fields:

- `id`,
- `entity_type`,
- `entity_id`,
- `operation`,
- `actor_key`,
- `request_id`,
- `job_run_id`,
- `packet_id`,
- `before_json`,
- `after_json`,
- `change_summary`,
- `created_at`.

Contract:

- every service-backed mutation appends one audit event,
- audit events are immutable,
- audit events reference the actor, request, and continuity packet when available.

This should complement, not replace, specialized histories like `lead_activity` or `packet_events`.

### 5.5 Target Component 4: Cross-Surface Continuity Engine

Use `work_packets` as the continuity layer across web, Discord, and jobs.

Required upgrades:

- actions and decisions created in web mode create or attach to work packets,
- request traces link to packets and packets link back to request traces,
- packet events always include actor identity and snapshot data,
- packet links become the standard graph edge between continuity state and business records,
- system jobs transition packets instead of only mutating business rows silently.

Continuity rule:

- authority tables hold facts,
- work packets hold workflow state.

### 5.6 Target Component 5: Review Workflows

Introduce explicit review semantics for proposed machine-generated or machine-extracted records.

#### New model: `artifact_reviews`

Suggested fields:

- `id`,
- `entity_type`,
- `entity_id`,
- `review_kind`,
- `status` enum: `pending`, `approved`, `rejected`, `corrected`,
- `assigned_actor_key`,
- `reviewed_by_actor_key`,
- `review_notes`,
- `created_at`,
- `reviewed_at`.

Usage:

- any machine-suggested action or decision can enter review,
- blocked or ambiguous packets can surface in one queue,
- confidence below threshold can require review before activation.

Review should work in web mode first. Discord can remain a producer, but not the only review surface.

### 5.7 Target Component 6: Confidence and Human Correction Loop

Promote confidence from retrieval metadata into operational review logic.

Rules:

- extracted artifacts carry confidence and extraction source,
- low-confidence artifacts create `pending` reviews rather than immediately becoming authoritative records,
- human corrections append audit events and review events,
- response feedback is attributable to the acting user,
- request traces record whether confidence caused human escalation.

This connects local-first model uncertainty to accountable team operations.

## 6. Thin Vertical Slice Recommendation

### 6.1 Start Here

Yes: start with actions, decisions, blocked work, and source links before leads and knowledge gaps.

Reason:

- this is the daily operational core for a micro-team,
- the tables and services already exist,
- the review loop is smaller and easier to validate,
- the data model is less open-ended than leads or knowledge gaps,
- and this slice directly proves accountability, traceability, and continuity.

### 6.2 End-to-End Slice

The thinnest proving slice should be:

1. A human creates a meeting note or action/decision from web or Discord.
2. The mutation resolves to a durable actor.
3. The system writes a provenance edge to the source message, meeting, request, or URL.
4. The system appends an audit event.
5. The system creates or updates a work packet.
6. A reviewer can inspect the packet timeline and mark it active, blocked, completed, or corrected.
7. The activity feed and request trace can explain who changed what and why.

Proof criteria:

- works in web mode,
- works in Discord mode,
- survives restart,
- every mutation is attributable,
- every record can be traced back to a source and a request,
- blocked state is visible in one review queue.

### 6.3 What Not to Include in the First Slice

Do not include these in the proof slice:

- lead lifecycle redesign,
- full knowledge-gap workflow redesign,
- standalone blocker table,
- persona workflow expansion,
- cosmetic UI polish,
- cross-machine sync,
- role-based access redesign beyond basic local actor attribution.

## 7. Phased Implementation Plan

### Phase 0: Normalize Contracts and Actor Resolution

Goal: stop adding continuity features on top of mismatched mutation paths.

Deliver:

- shared actor resolution helper for web, Discord, and jobs,
- service-first mutation contract for actions and decisions,
- normalized provenance edge model,
- request trace actor fields,
- packet event snapshot enforcement in service code.

Dependencies:

- none.

### Phase 1: Vertical Slice for Actions and Decisions

Goal: make action and decision creation fully attributable and traceable.

Deliver:

- web action and decision creation routed through services,
- actor resolution on web requests,
- provenance links from action and decision to source request and optional meeting/message,
- append-only audit events,
- work packet creation for action and decision mutations,
- activity feed entries and packet review visibility.

Dependencies:

- Phase 0 actor resolution,
- Phase 0 provenance normalization.

### Phase 2: Blocked Work and Review Queue

Goal: make blocked operational state explicit and reviewable.

Deliver:

- blocked reason contract for actions and packets,
- review queue page or API for `awaiting_human` and `blocked` packets,
- approval and correction workflow for low-confidence or ambiguous operational records,
- audit linkage for all review actions.

Dependencies:

- Phase 1 packet emission and audit events.

### Phase 3: Obligations and Scheduled Continuity

Goal: bring recurring operations under the same continuity contract.

Deliver:

- obligation mutations emit audit events,
- sweep jobs write requestless but attributable system actors,
- job runs link to packet transitions and affected authority rows,
- review surface can explain why a job changed a record.

Dependencies:

- Phase 1 actor and packet linkage,
- Phase 2 blocked/review handling.

### Phase 4: Confidence-Gated Suggestions and Human Correction

Goal: make machine-generated operational suggestions reviewable rather than silently authoritative.

Deliver:

- attributable web and inbox feedback,
- explicit review objects for machine-suggested actions and decisions,
- low-confidence extraction routed into review instead of direct activation,
- request trace fields showing when confidence triggered human escalation.

Dependencies:

- Phase 1 audit trail,
- Phase 2 review queue.

### Phase 5: Extend to Leads and Knowledge Gaps

Goal: apply the proven continuity contract to broader product surfaces.

Deliver:

- lead continuity packet emission,
- lead activity normalization into shared audit events,
- attributable knowledge-gap curation and resolution in web mode,
- continuity links between chat request, gap, memo, and improvement verification.

Dependencies:

- completed vertical slice in actions and decisions,
- completed review and audit substrate.

## 8. Exact Files and Modules to Change First

Start with these modules in this order:

1. `LeisureLLM/admin/dependencies.py`
   - add shared web actor resolution instead of token-only authorization.

2. `LeisureLLM/admin/routers/artifacts.py`
   - stop raw SQL writes for action and decision creation and updates;
   - route through services;
   - emit provenance, audit, and work packet state.

3. `LeisureLLM/core/services/action_service.py`
   - make service the canonical mutation path for actions;
   - accept actor and provenance metadata;
   - emit audit and packet updates.

4. `LeisureLLM/core/services/decision_service.py`
   - same normalization as action service.

5. `LeisureLLM/core/services/work_packet_service.py`
   - make snapshot emission mandatory in service behavior;
   - support request and actor linkage consistently.

6. `LeisureLLM/services/request_tracing.py`
   - add actor fields and explicit packet or audit linkage.

7. `LeisureLLM/admin/routers/activity.py`
   - expose new audit and review events in the unified feed.

8. `LeisureLLM/database.py`
   - keep fallback schema aligned with real migrations;
   - stop schema drift from reappearing in runtime backfills.

9. `LeisureLLM/migrations/`
   - add new additive migrations for actor, audit, and provenance tables.

10. `LeisureLLM/admin/routers/chat.py`
    - link request traces to downstream operational artifacts and review state.

Next group after the first slice:

- `LeisureLLM/admin/routers/continuity.py`
- `LeisureLLM/core/services/obligation_service.py`
- `LeisureLLM/core/sweep_jobs.py`
- `LeisureLLM/admin/routers/knowledge.py`
- `LeisureLLM/admin/routers/inbox.py`

## 9. Data Migrations Required

Use additive migrations only.

### Required for the first vertical slice

1. `018_actor_identity.sqlite.sql`
   - create `actors` table,
   - optional `actor_aliases` table if needed for Discord and web identity mapping,
   - add indexes for `actor_kind`, `surface`, and `external_id`.

2. `019_audit_events.sqlite.sql`
   - create append-only `audit_events` table,
   - index by entity, actor, request, packet, and time.

3. `020_record_edges.sqlite.sql`
   - create a new normalized provenance graph table instead of mutating `source_links` again,
   - support links such as request -> decision, meeting -> action, message -> meeting, URL -> decision.

4. `021_request_trace_actor_fields.sqlite.sql`
   - add `actor_key`, `actor_kind`, `actor_display_name`, and optional `parent_request_id` to `request_traces`,
   - add request reference fields where packet and audit linkage needs them.

### Required soon after the first slice

5. `022_review_queue.sqlite.sql`
   - create `artifact_reviews` table,
   - add review indexes and assignment metadata.

6. `023_feedback_attribution.sqlite.sql`
   - add attributable actor fields to `response_feedback`,
   - add actor fields to `learning_loop_events` if that table remains active.

### Explicitly not required in the first slice

- no destructive rebuild of `tasks`, `decisions`, or `meeting_notes`,
- no standalone `blockers` table,
- no lead schema redesign,
- no knowledge-gap table rewrite.

## 10. Features to Defer

Defer these until the core continuity slice is proven:

- lead and pipeline continuity redesign,
- full knowledge-gap continuity rewrite,
- standalone blocker records,
- cross-instance or network sync,
- expanded RBAC beyond local actor attribution,
- new persona orchestration features,
- speculative dashboards or visual polish,
- migration of all historical tables to a single audit abstraction.

## 11. Risks and Unresolved Questions

### 11.1 Schema Drift Risk

Some services and routers still reflect older schemas or earlier naming conventions. The known risk is not only missing features. The risk is writing new continuity logic against mismatched contracts.

Mitigation:

- normalize services first,
- align `database.py` fallback schema with migration reality,
- avoid extending `source_links` again; introduce a clean normalized edge table.

### 11.2 Web Identity Product Risk

Web mode currently authenticates an operator, not a person.

Mitigation:

- introduce lightweight local actor selection or login identity before expanding review-heavy workflows,
- do not block initial implementation on full RBAC.

### 11.3 Split Audit Storage Risk

`request_traces` are stored durably but in a sidecar database. That is acceptable for local-first operation, but it complicates joins and export.

Mitigation:

- keep sidecar traces in the first slice,
- use shared IDs rather than forcing a storage merge immediately.

### 11.4 Historical Backfill Risk

Old records will not have actor attribution or normalized provenance.

Mitigation:

- backfill system actors where safe,
- do not attempt perfect historical reconstruction,
- clearly distinguish inferred attribution from authoritative attribution.

## 12. Decision

Adopt a continuity architecture that:

- preserves existing typed authority tables,
- adds shared actor identity,
- adds append-only audit events,
- uses work packets as the cross-surface workflow kernel,
- introduces normalized provenance edges,
- and starts with actions, decisions, blocked work, and source links before leads and knowledge gaps.

That is the smallest path that makes MKA behave less like a general AI workspace and more like a local-first operational continuity system for micro-teams.
