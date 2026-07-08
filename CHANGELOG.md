# Changelog

All notable changes to Magic Key Assistant are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.8.0] — 2026-04-20

### Added

- **Feedback learning scheduler** — nightly background cycle that retires
  under-performing prompt variants, refines prompts from negative signals,
  surfaces low-quality chunks, scans for knowledge gaps, aggregates
  improvement signals, and applies decay
  (`LeisureLLM/core/feedback_learning_runner.py`).
- **Prompt refinement from negative feedback** — when any failure mode
  (factual error, missing info, clarity, too verbose, too brief) accumulates
  enough signals, a behavioural directive variant is auto-created and injected
  into the system prompt at generation time.
- **Batch gap auto-creation** — feedback signals above a configurable
  threshold automatically create knowledge-gap records with failure-mode
  context; existing gaps get `times_asked` incremented instead of duplicated.
- **Enriched signal aggregation** — `_aggregate_signals()` now returns
  `by_failure_mode`, `top_topics`, `chunk_correlation`, and actionable
  `recommendations`.
- **Two new API endpoints** on the moat router:
  `GET /api/v1/feedback/prompt-suffix` (view active suffix) and
  `POST /api/v1/feedback/refine-prompts` (manual trigger).
- **VSLM-assisted chat policy** — `async_enhance_policy()` escalates
  assistive → deep lane when the local model classifies a query as complex
  with sufficient confidence.
- **Persona meetings system** — hourly persona-meeting mixin with
  role-based exercises, knowledge-tension detection, R&D topic generation,
  weekly digest, anti-repetition, and web-research integration
  (`LeisureLLM/cogs/mixins/persona_meetings.py`).
- **SqliteService base class** — eliminates repetitive connection / row
  plumbing across services (`LeisureLLM/core/services/_sqlite_service.py`).
- **Migration 026** — consolidates runtime auxiliary tables (partner events,
  PM proposals, bot health snapshots, persona meeting takeaways, custom
  personas, past clients) into the formal migration chain.
- **18 new tests** for the feedback learning loop covering prompt refinement,
  suffix generation, gap auto-creation, signal aggregation, learning cycle
  orchestration, and anonymised export
  (`tests/test_feedback_learning_loop.py`).
- **Architecture docs** — `docs/architecture/local-llm.md` (local LLM
  runtime) and `docs/architecture/system.md` (system overview).
- **Internal planning docs** — product roadmap, release readiness checklist,
  repository annotations.
- **Product one-pager** — `docs/product/one_pager.md`.
- **Release engineering docs** — development-build guide and Windows
  installer guide under `docs/release/`.
- **Dev tooling** — bulk failsoft rewriter, test runner, setup diagnostic,
  phase-1 template checker, knowledge-capital dashboard check.

### Changed

- **README.md completely rewritten** — new product positioning, capability
  matrix, architecture diagram, accelerator descriptions (Scout, Dreamer,
  Rainmaker, Steward, Curator), trust-controls section, setup flow.
- Feedback learning loop `run_learning_cycle()` now orchestrates six steps:
  retire variants → refine prompts → surface low-quality chunks → scan for
  gaps → aggregate signals → apply decay.
- `export_anonymised_signals()` updated for backward compatibility with the
  enriched signal structure.

### Fixed

- **Starlette 1.0 `TemplateResponse` compatibility** — updated 24 calls
  across 8 router files to use the new keyword-argument signature
  (`request=`, `name=`, `context=`), fixing HTTP 500 on all page routes.
- **Gap auto-creation INSERT** used non-existent columns (`priority`,
  `source`); corrected to `priority_score` and `question` matching the
  `knowledge_gaps` schema.
- **Gap duplicate detection** upgraded from first-word LIKE match to exact
  topic-string match.
- **Missing `_GAP_INDICATORS`** re-exported from `admin.routers.chat` so the
  web-chat gap-detection test suite can import the canonical indicator list.
- **Migration 027** adds `curation_status`, `curation_reason`, `curated_at`,
  and `curated_by_username` columns to `knowledge_gaps` (also patched in
  `_ensure_aux_tables` for backward compatibility).
- **FakeDB test mocks** across 8 test files updated with `execute`,
  `fetchone`, `fetchall`, `fetch_dicts`, and `fetch_one_dict` methods to
  match the `Database` convenience API, fixing 147 pre-existing test
  failures.
- **`_Timer.elapsed_ms`** initialised in `__enter__` so it's safe to access
  inside an exception handler before `__exit__` runs.
- **Review-queue defer test** switched from hard-coded past date to
  `datetime.now(tz=utc)` so the 30-day-ahead defer target stays in the
  future.
- **Self-assessment timeout mock** — set `mock_router.timeouts.self_assessment`
  so `asyncio.wait_for` receives a real number instead of a `MagicMock`.
- Dead `assert False` in `tests/test_system_health.py` replaced with
  `pytest.fail()`.

## [0.7.5] — 2025

_Initial tracked release._
