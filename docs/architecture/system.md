# System Architecture

> Stable map of the product. This document explains responsibilities and boundaries rather than volatile file counts.

## Runtime Shape

Magic Key Assistant is a local-first operations system built around one shared application core.

At runtime, the product combines:

- a FastAPI web console for setup, chat, review, and administration
- an optional Discord bot surface for team workflows
- scheduled continuity and accelerator jobs
- a shared service layer for records, routing, retrieval, and auditing
- a local data layer built on SQLite and ChromaDB

For most users the web console is the primary control plane. Discord is optional, not foundational.

## Core Boundaries

### Presentation Layer

The presentation layer owns user interaction and HTTP or Discord-specific concerns.

It includes:

- web routes, templates, and page shells
- streaming chat endpoints
- setup and configuration flows
- Discord commands, listeners, and threaded team interactions

This layer should not own business rules or database shape. It should delegate to services.

### Service Layer

The service layer owns operational logic.

This is where the system decides:

- how records are created and updated
- how review queues and continuity states are computed
- how model routing works
- how tool calls are validated and audited
- how onboarding, retrieval, and follow-through behave across surfaces

The architectural rule is simple: if a behavior must work from both web and Discord, it belongs here.

### Data Layer

SQLite is the system of record for durable operational state. ChromaDB is the retrieval index for document and chunk embeddings.

SQLite holds:

- operational records such as actions, decisions, leads, meetings, obligations, and review state
- audit trails and provenance edges
- job runs, request traces, and feedback loops
- identity and control-plane data

ChromaDB holds:

- indexed document chunks
- retrieval metadata
- enrichment outputs used to improve recall and ranking

## Primary Flows

### Capture

Natural-language input from chat, meetings, or web forms is normalized into durable records instead of remaining trapped in transient conversation.

### Retrieval

Documents and structured records are retrieved together so answers can combine narrative context with operational state.

### Continuity

Scheduled sweeps compute what is overdue, stale, unowned, unresolved, or otherwise at risk, then surface it through the review queue and admin UI.

### Control

Configuration, guide content, model routing, and operator tools all live in the web console so the product can be run locally without external SaaS dependencies.

## Operating Modes

### Solo Mode

Solo mode uses the web console without requiring Discord. It is the default when no Discord token is configured.

### Team Mode

Team mode adds Discord as a collaboration surface while keeping the same shared services and underlying records.

## Architectural Principles

- Local-first by default. Cloud providers are optional accelerators, not prerequisites.
- Durable records over ambient chatter. Background behavior should create, update, or explain state.
- Shared services over duplicated surface logic. Web and Discord should not fork business rules.
- Forward-only migrations over runtime schema improvisation.
- Confirmation and auditability for mutations.
- Capability-oriented documentation over brittle counts of files, routes, or templates.

## Related Docs

- [Getting Started](../../GET_STARTED.md)
- [Local LLM Architecture](local-llm.md)
- [Full Inventory](../internal/FULL_INVENTORY.md)
- [Local Inference Fallback](../internal/local_inference_fallback.md)
