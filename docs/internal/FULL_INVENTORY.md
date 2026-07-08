# Magic Key Assistant — Exhaustive Feature Inventory

> Generated from source-code audit of every `.py`, `.sql`, `.yaml`, `.json`, `.html`, and `.md` file.
> Covers: Discord commands, scheduled jobs, web GUI pages, API endpoints, services, database tables, prompt templates, known tech debt, and the full configuration surface.

---

## 1. Discord Slash Commands

### LLM Cog (`cogs/LLM.py`)

| Command | Access | Description |
|---------|--------|-------------|
| `/ask` | Everyone | Ask the bot a question; 3-phase pipeline (Initial → Critique → Synthesize) with RAG retrieval, superseded-doc filtering, knowledge-gap detection on sparse context |
| `/deep_consult` | Everyone | Extended multi-phase response with deeper context retrieval |
| `/health` | Owner | System health check (LLM, DB, Chroma, Tavily) |
| `/tour` | Everyone | 7-page interactive walkthrough of bot capabilities |
| `/reloadcogs` | Owner | Hot-reload all cogs without restart |

### DocumentAuthor Cog (`cogs/DocumentAuthor.py`)

| Command | Access | Description |
|---------|--------|-------------|
| `/remember` | Everyone | Save information to knowledge base as a memo with YAML frontmatter |
| `/teach` | Everyone | Teach the bot a specific fact or procedure |
| `/prompt_note` | Owner | Add a note to the operational context prompt file |
| `/reindex_docs` | Owner | Trigger incremental Chroma reindex of all docs |

### ActionItems Cog (`cogs/ActionItems.py`)

| Command | Access | Description |
|---------|--------|-------------|
| `/action add` | Everyone | Create a new action item (title, owner, due, priority, project, tags) |
| `/action list` | Everyone | List actions filtered by status, assignee, project, priority |
| `/action done` | Everyone | Mark an action item as done (by ID) |
| `/action thin` | Everyone | Move old completed/cancelled items to archive (configurable cutoff) |
| `/action cleanup` | Owner | Bulk clean stale action items |

### KnowledgeGapTracker Cog (`cogs/KnowledgeGapTracker.py`)

| Command | Access | Description |
|---------|--------|-------------|
| `/interview` | Partners | Start an interactive Q&A session; optional preemptive topic + seed_count |
| `/gap list` | Owner | View knowledge gaps filtered by status |
| `/gap curate` | Partners | Keep/defer/discard a specific gap with priority adjustment |
| `/gap bulk` | Partners | Bulk curate gaps (filters: topic_contains, question_contains, older_than_days, scope); dry_run supported |
| `/gap cleanup` | Partners | Automated duplicate/low-signal sweep with dry_run preview |
| `/archivist_report` | Admin | Archivist collection health report |
| `/thursday_prompt` | Admin | Test Thursday probing question generation |

### AutonomousOps Cog (`cogs/AutonomousOps.py`)

| Command | Access | Description |
|---------|--------|-------------|
| `/report` | Everyone | Generate a report (type: daily/weekly/sprint) |
| `/admin_run` | Owner | Manually trigger a scheduled job (scout/dreamer/strategic/rainmaker/async_meeting/digest) |
| `/ingest` | Owner | Manual document ingestion from docs folder |
| `/hire` | Everyone | Create a custom persona via modal (name, emoji, personality, concerns, project context) |
| `/fire` | Everyone | Remove a custom persona via dropdown selector |
| `/staff` | Everyone | List all active personas (built-in + custom) |
| `/dev_status` | Owner | Comprehensive system check (DB, Chroma, LLM, Tavily, cogs, scheduler, personas, pipeline) |
| `/set_bots_channel` | Owner | Set the bots-office channel for autonomous posts |
| `/db_status` | Owner | Database statistics (table counts, sizes, recent activity) |
| `/agenda` | Partners | Add a topic to the persona meeting agenda (with priority + optional context + expiry) |
| `/agenda_list` | Partners | View pending agenda items |
| `/agenda_remove` | Partners | Remove an agenda item by ID |
| `/did` | Partners | Log a quick win/update via modal (category + details + optional link) |
| `/did_list` | Owner | Audit partner updates |
| `/leaderboard` | Everyone | Partner points leaderboard (period: week/month/all) |
| `/parse_meeting` | Everyone | Save meeting notes (file upload via attachment OR 3-part modal); creates docs/meetings/ file + reindex |
| `/persona_meeting` | Partners | Force-trigger a persona meeting (type + topic) |

### SetupWizard Cog (`cogs/SetupWizard.py`)

| Command | Access | Description |
|---------|--------|-------------|
| `/setup` | Everyone | Guided onboarding wizard (org info → mode → modules → writes YAML configs) |

### FeedbackView Cog (`cogs/FeedbackView.py`)

| Command | Access | Description |
|---------|--------|-------------|
| *(no slash commands)* | — | Provides persistent feedback buttons (👍/👎) on bot responses + auto-improvement memo after 2+ negatives |

### Commented-Out / Disabled Commands

| Command | Location | Reason |
|---------|----------|--------|
| `/lead add` | AutonomousOps.py | Fully implemented but commented out "to reduce complexity" |
| `/lead list` | AutonomousOps.py | Same |
| `/lead update` | AutonomousOps.py | Same |
| `/lead pipeline` | AutonomousOps.py | Same |
| `/lead touch` | AutonomousOps.py | Same |
| "Ask Bot About This" (context menu) | LLM.py | Commented out — `TODO: Context menus need to be registered differently in Discord.py 2.x` |

---

## 2. Discord Context Menus

| Menu Item | Status | Location |
|-----------|--------|----------|
| "Ask Bot About This" | **Disabled** (commented out) | `cogs/LLM.py` |
| "Remember This" | **Active** | `cogs/DocumentAuthor.py` |

---

## 3. Discord Listeners

| Event | Cog | Behaviour |
|-------|-----|-----------|
| `on_message` (mentions) | LLM | Responds to @bot mentions in allowed channels with RAG pipeline |
| `on_message` (PM automation) | AutonomousOps | Extracts "I will…" → action items, "Decision:" → decisions; tracks open questions; handles thread replies for weekly_planning/midweek_risk/friday_closeout |
| `on_message` (Dreamer) | AutonomousOps | #schemes-n-dreams channel: 25% chance response, hourly rate limit, temp=0.9 |
| `on_connect` | LLM | Connection logging |
| `on_disconnect` | LLM | Disconnect logging |
| `on_resumed` | LLM | Resume logging |
| `on_ready` | LLM | Ready logging + status set |
| `on_raw_reaction_add` | ActionItems | Emoji claim on action items |
| `on_raw_reaction_remove` | ActionItems | Emoji unclaim on action items |

---

## 4. Scheduled Jobs (Background Tasks)

All jobs use `discord.ext.tasks` loops with UTC/Eastern timezone awareness and job-run idempotency via `job_runs` table.

### Work Module

| Job | Schedule | Description |
|-----|----------|-------------|
| `daily_digest` | 8:00 AM ET daily | Morning digest of yesterday's key events |
| `thursday_async_meeting` | 10:00 AM ET Thursdays | Async standup: 4 threads + action agenda + partner updates |
| `monthly_partners_meeting` | 10:00 AM ET, 1st Tuesday | Monthly partners meeting agenda and minutes |
| `end_of_week_reflection` | 6:00 PM ET Thursdays | Individual reflection on the week |
| `weekly_strategic_review` | 8:00 PM ET Sundays | Chief strategic analysis |
| `monday_planning_kickoff` | 10:00 AM ET Mondays | Start-of-week planning |
| `friday_closeout` | 4:00 PM ET Fridays | End-of-week wrap-up |
| `dreamer_ideation_cycle` | 2:30 PM ET Tuesdays | Blue-sky ideation → Scout+Archivist investigation → viability scoring → Manager escalation |

### Memory Module

| Job | Schedule | Description |
|-----|----------|-------------|
| `daily_knowledge_refresh` | 6:00 AM ET daily | Incremental Chroma reindex of changed docs |
| `question_watchdog` | 11:00 AM ET daily | Scan for unanswered questions → knowledge gaps |
| `weekly_dashboard_update` | 9:00 AM ET daily | Refresh cached analytics |
| `daily_scout_search` | 7:00 AM ET daily | Tavily web search with LLM-planned research paths + novelty loop |
| `scout_background_crawl` | Every 45 min, weekdays 9-5 | Background crawl of seeded URLs |

### Pipeline / Rainmaker Module

| Job | Schedule | Description |
|-----|----------|-------------|
| `rainmaker_morning_pipeline` | 8:30 AM ET weekdays | Pipeline status report: leads by stage, overdue actions, today's follow-ups |
| `rainmaker_opportunity_hunt` | 10:00 AM ET weekdays | NC eVP scraping + Tavily search + LLM assessment + lead creation. Daily theme rotation: Museums Mon, Nature Tue, Government Wed, Corporate Thu, Construction Fri |
| `rainmaker_follow_up_nudges` | 2:00 PM ET weekdays | Follow-up reminders for stale leads |
| `rainmaker_weekly_cold_review` | 10:30 AM ET Mondays | Review cold leads for re-engagement or archival |
| `rainmaker_past_client_checkin` | 11:00 AM ET Wednesdays | Check-in prompts for past clients due for re-engagement |

### Health / Steward Module

| Job | Schedule | Description |
|-----|----------|-------------|
| `steward_daily_health_check` | 6:00 PM ET daily | System health: questions asked, unhelpful rate, days since ingest, open gaps without progress, blind spots |
| `steward_weekly_self_assessment` | 5:00 PM ET Sundays | Engagement trends, learning loop, feature usage, recommendations |
| `steward_learning_loop_audit` | 9:30 AM ET Wednesdays | Gaps opened/closed, memos written, docs ingested, closure rate |

### Knowledge Gap Module

| Job | Schedule | Description |
|-----|----------|-------------|
| `gap_reminder` | 9:00 AM ET daily | Reminder for stale gaps (7+ days without progress) |
| `gap_hygiene_sweep` | 11:10 AM ET daily | Auto-defer duplicate/low-signal gaps |
| `bounty_board_post` | 11:20 AM ET daily | Post top repeated gaps to bounty-board channel |
| `archivist_shelf_check` | 11:40 AM ET Mondays | Collection health audit with 3 probing questions per gap, 21-day cooldown |
| `thursday_partner_prompt` | 10:30 AM ET Thursdays | Probing question to random partner; style rotation (operator/socratic/red_team/checklist/numbers) |
| `weekly_gap_escalation_check` | 11:00 AM ET Mondays | Escalate gaps with 3+ unanswered probing questions |

### Action Items Module

| Job | Schedule | Description |
|-----|----------|-------------|
| `daily_stale_sweep` | 9:05 AM ET daily | Flag stale action items |
| `daily_top3` | 8:45 AM ET weekdays | Post top 3 priorities for the day |
| `weekly_escalation_check` | 10:30 AM ET Mondays | Escalation ladder check |
| `weekly_action_checkin` | 9:15 AM ET Mondays | Weekly action health check-in |

### Persona Meetings Module

| Job | Schedule | Description |
|-----|----------|-------------|
| `hourly_persona_meeting` | Every 2h, 8 AM–8 PM | Event-triggered persona meetings. Trigger hierarchy: partner agenda → knowledge tension → DB triggers → fallback proof point |
| `weekly_persona_meeting_digest` | 4:30 PM ET Fridays | Weekly digest compiling all takeaways by urgency |

### Sprint Cycles (separate cog)

| Job | Schedule | Description |
|-----|----------|-------------|
| `auto_start_weekly_cycle` | 12:01 AM ET Mondays | Auto-create new weekly sprint cycle |
| `auto_close_weekly_cycle` | 11:50 PM ET Sundays | Auto-close current sprint cycle |

---

## 5. Web Admin GUI — Pages

FastAPI app served at `http://127.0.0.1:8000`. Templates in `admin/templates/`.

| Route | Template | Description |
|-------|----------|-------------|
| `GET /` | `dashboard.html` | Main dashboard: bot status, Ollama status, quick actions. Redirects to `/setup` on first run |
| `GET /setup` | `setup.html` | First-run setup wizard (keys, org profile, rail maps) |
| `GET /settings` | `settings.html` | Bot configuration editor (all 12 config sections) |
| `GET /org` | `org.html` | Organization profile and workflows editor |
| `GET /router` | `model_router.html` | Model router configuration (backends, pipeline roles, test prompt) |
| `GET /knowledge` | `knowledge_base.html` | Knowledge base browser (document counts, sizes, folder-open) |
| `GET /gaps` | `knowledge_gaps.html` | Knowledge gaps manager (list, filter, curate, bulk actions) |
| `GET /actions` | `actions.html` | Action items CRUD with status filters and stats |
| `GET /leads` | `leads.html` | Leads pipeline with stage advancement |
| `GET /meetings` | `meetings.html` | Meeting notes browser |
| `GET /analytics` | `analytics.html` | Analytics overview (charts, trends, engagement) |
| `GET /obligations` | `obligations.html` | Recurring obligations tracker |
| `GET /feedback` | `feedback.html` | Product feedback collection and resolution |
| `GET /inbox` | `inbox.html` | Persistent async Q&A threads and knowledge gap interviews |
| `GET /jobs` | `jobs.html` | Scheduled job registry with manual triggers |
| `GET /teach` | `teach.html` | Teach the system new facts via web form |
| `GET /explorer` | `explorer.html` | Read-only browser for all DB tables (pagination, filters, CSV export) |
| `GET /activity` | `activity.html` | Unified timeline of system events |
| `GET /guide` | `guide.html` | Quick-start guide and documentation links |
| `GET /login` | `login.html` | Admin token login page |

---

## 6. REST API Endpoints

All endpoints prefixed with `/api/v1/`. Auth via Bearer token, cookie, or query param.

### System (`admin/routers/system.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/ollama/status` | Ollama installation + running status |
| `POST` | `/api/v1/ollama/install` | Auto-download and install Ollama (Windows) |
| `POST` | `/api/v1/bot/restart` | Restart the Discord bot |
| `GET` | `/api/v1/bot/status` | Bot connection status and metadata |
| `GET` | `/api/v1/backup/list` | List available backup files |
| `POST` | `/api/v1/backup/create` | Create a new database backup |
| `POST` | `/api/v1/backup/restore/{filename}` | Restore from a backup (with safety backup first) |
| `POST` | `/api/v1/support-bundle` | Generate a support bundle ZIP (redacted config + schema + stats + env) |
| `POST` | `/api/v1/sweeps/run` | Run sweep jobs (obligation, SOP drift, rail escalation) |
| `POST` | `/api/v1/seed` | Populate database with sample data |
| `POST` | `/api/v1/retention/prune` | Prune old autonomous_posts + job_runs |

### llama.cpp (`admin/routers/system.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/llamacpp/status` | llama.cpp server status (installed, running, port, models) |
| `POST` | `/api/v1/llamacpp/install` | Download and install llama.cpp binary (SSE streaming progress) |
| `POST` | `/api/v1/llamacpp/start` | Start llama.cpp server with hardware-optimal flags |
| `POST` | `/api/v1/llamacpp/stop` | Stop the running llama.cpp server |
| `GET` | `/api/v1/llamacpp/models` | List downloaded GGUF models |
| `POST` | `/api/v1/llamacpp/models/download` | Download a GGUF model from HuggingFace (SSE streaming) |

### Settings (`admin/routers/settings.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/secrets/list` | List configured secrets (masked) |
| `POST` | `/api/v1/secrets/set` | Set a secret (OS keyring) |
| `POST` | `/api/v1/secrets/delete/{key}` | Delete a secret |
| `GET` | `/api/v1/secrets/test/{key}` | Test a secret (connectivity check) |
| `GET` | `/api/v1/config/all` | Get all bot configuration |
| `GET` | `/api/v1/config/sections` | List configuration sections with metadata |
| `GET` | `/api/v1/config/{section}` | Get a specific config section |
| `POST` | `/api/v1/config/{section}` | Update a config section |
| `POST` | `/api/v1/config/{section}/reset` | Reset a config section to defaults |
| `POST` | `/api/v1/config/reset-all` | Reset all config to defaults |
| `GET` | `/api/v1/config/export` | Export full config as JSON |
| `POST` | `/api/v1/config/import` | Import config from JSON |
| `GET` | `/api/v1/prompts` | List all prompt files |
| `GET` | `/api/v1/prompts/{prompt_key}` | Get a prompt file's content |
| `POST` | `/api/v1/prompts/{prompt_key}` | Update a prompt file |
| `POST` | `/api/v1/prompts/{prompt_key}/backup` | Backup a prompt file before editing |
| `GET` | `/api/v1/org/profile` | Get org profile (YAML) |
| `POST` | `/api/v1/org/profile` | Update org profile |
| `GET` | `/api/v1/org/workflows` | Get workflow config (YAML) |
| `POST` | `/api/v1/org/workflows` | Update workflow config |
| `GET` | `/api/v1/setup/status` | Check first-run setup status |
| `POST` | `/api/v1/setup/keys` | Set API keys during setup |
| `POST` | `/api/v1/setup/complete` | Mark setup as complete |
| `GET` | `/api/v1/rail-maps` | Get rail map definitions (YAML) |
| `GET` | `/api/v1/rail-maps/sidebar` | Get rail maps for sidebar navigation |
| `POST` | `/api/v1/setup/rail-map` | Create rails from a rail map template |

### Model Router (`admin/routers/model_router_api.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/router/backends` | List registered backends and their models |
| `GET` | `/api/v1/router/pipeline` | Get current pipeline configuration |
| `POST` | `/api/v1/router/pipeline` | Update full pipeline configuration |
| `POST` | `/api/v1/router/pipeline/role/{role_name}` | Update a single pipeline role |
| `POST` | `/api/v1/router/test` | Test the pipeline with a prompt |
| `POST` | `/api/v1/router/backends/refresh` | Re-register backends from secrets |
| `GET` | `/api/v1/router/presets` | List pipeline presets with suitability info |
| `POST` | `/api/v1/router/presets/{preset_name}/apply` | Apply a pipeline preset |
| `GET` | `/api/v1/router/token-monitor` | Recent token truncation events and stats |
| `GET` | `/api/v1/router/vslm/status` | VSLM router status and statistics |
| `POST` | `/api/v1/router/vslm/configure` | Configure VSLM router model and settings |
| `POST` | `/api/v1/router/vslm/test` | Test VSLM classification on a query |

### Knowledge (`admin/routers/knowledge.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/knowledge/stats` | Knowledge base statistics |
| `POST` | `/api/v1/knowledge/open-folder/{folder_key}` | Open a docs folder in OS file explorer |
| `GET` | `/api/v1/gaps` | List gaps with filtering (status, topic, sort) |
| `GET` | `/api/v1/gaps/stats` | Gap statistics by status/topic |
| `GET` | `/api/v1/gaps/{gap_id}` | Get a specific gap |
| `PATCH` | `/api/v1/gaps/{gap_id}` | Update a gap |
| `POST` | `/api/v1/gaps/bulk` | Bulk update gaps |
| `DELETE` | `/api/v1/gaps/{gap_id}` | Delete a gap |

### Artifacts (`admin/routers/artifacts.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/actions` | List actions with filters (status, owner, priority) |
| `POST` | `/api/v1/actions` | Create an action item |
| `GET` | `/api/v1/actions/{action_id}` | Get a specific action |
| `PATCH` | `/api/v1/actions/{action_id}` | Update an action |
| `POST` | `/api/v1/actions/{action_id}/done` | Mark an action as done |
| `POST` | `/api/v1/actions/{action_id}/cancel` | Cancel an action |
| `GET` | `/api/v1/actions/stats` | Action statistics by status |
| `GET` | `/api/v1/leads` | List leads with filters |
| `GET` | `/api/v1/leads/pipeline` | Pipeline summary (counts by stage) |
| `POST` | `/api/v1/leads` | Create a lead |
| `GET` | `/api/v1/leads/{lead_id}` | Get a specific lead |
| `POST` | `/api/v1/leads/{lead_id}/advance` | Advance a lead to the next stage |

### Continuity (`admin/routers/continuity.py`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/obligations` | List obligations |
| `POST` | `/api/v1/obligations` | Create an obligation |
| `GET` | `/api/v1/obligations/{obl_id}` | Get a specific obligation |
| `PATCH` | `/api/v1/obligations/{obl_id}` | Update an obligation |
| `POST` | `/api/v1/obligations/{obl_id}/complete` | Mark an obligation as completed |
| `GET` | `/api/v1/obligations/stats` | Obligation statistics |
| `GET` | `/api/v1/sops` | List SOPs |
| `POST` | `/api/v1/sops` | Create a SOP |
| `GET` | `/api/v1/sops/{sop_id}` | Get a specific SOP |
| `PATCH` | `/api/v1/sops/{sop_id}` | Update a SOP |
| `POST` | `/api/v1/sops/{sop_id}/exercise` | Record a SOP exercise |
| `GET` | `/api/v1/rails` | List rails |
| `POST` | `/api/v1/rails` | Create a rail |
| `POST` | `/api/v1/rails/from-map` | Create rails from a YAML rail map template |
| `GET` | `/api/v1/rails/{rail_id}` | Get a rail with stages |
| `POST` | `/api/v1/rails/{rail_id}/advance` | Advance a rail to the next stage |
| `PATCH` | `/api/v1/rails/stages/{stage_id}` | Update a stage |
| `POST` | `/api/v1/rails/stages/{stage_id}/complete` | Complete a stage |
| `GET` | `/api/v1/rails/escalations` | Get overdue rail stage escalations |
| `GET` | `/api/v1/feedback` | List feedback items |
| `POST` | `/api/v1/feedback` | Submit feedback |
| `POST` | `/api/v1/feedback/{fb_id}/resolve` | Resolve a feedback item |
| `POST` | `/api/v1/feedback/report-problem` | Report a problem with auto support bundle |

---

## 7. Services

### External Services (`services/`)

| Service | File | Description |
|---------|------|-------------|
| `LLMService` | `llm_service.py` | OpenAI `ChatOpenAI` wrapper with tenacity retry (3 attempts, exponential backoff 2–30s). Methods: `generate()`, `complete()`, `health_check()` |
| `ModelRouter` | `model_router.py` | Multi-backend LLM router. Backends: OpenAI, Anthropic, Ollama, OpenRouter, llama.cpp, Custom. 3-phase pipeline: Initial → Critique → Synthesize. Auto-model-pulling for Ollama. VSLM-based auto-routing. Token estimation and truncation detection |
| `TavilyService` | `tavily_service.py` | Async Tavily web search wrapper. Methods: `search()`, `health_check()` |
| `ServiceContainer` | `container.py` | Bundles LLMService + TavilyService. `build()` classmethod, `health_report()` |
| `BotConfigManager` | `bot_config.py` | 12 config sections as dataclasses; load/save JSON; get/update/reset per section |
| `SystemTools` | `system_tools.py` | `get_ollama_status()`, `download_file()`, `install_ollama_windows()` |
| `SecretsManager` | `secrets.py` | OS keyring (Windows Credential Manager / macOS Keychain) + env var fallback + memory cache |
| `AgenticChat` | `agentic_chat.py` | Tool registry, tool-calling loop, confirmation gates for mutations |
| `InteractionMemory` | `interaction_memory.py` | Query logging and concern thread detection across sessions |
| `CorpusHealth` | `corpus_health.py` | Knowledge base health metrics and outlier detection |
| `WebResearch` | `web_research.py` | Extended web research orchestration for Scout persona |
| `DeviceCapability` | `device_capability.py` | Hardware detection and model recommendation |
| `HyDERetrieval` | `hyde_retrieval.py` | Hypothetical Document Embeddings retrieval pipeline |
| `ChunkEnrichment` | `chunk_enrichment.py` | LLM-at-ingestion enrichment with anti-hallucination validation |
| `LlamaCppManager` | `llamacpp_manager.py` | llama.cpp server management: download, configure, launch, model management, auto-register as backend |
| `PipelinePresets` | `pipeline_presets.py` | Speed / Balanced / Quality pipeline presets resolved against installed models |
| `TokenEstimator` | `token_estimator.py` | Token estimation and silent context truncation detection with risk levels and event logging |
| `VSLMRouter` | `vslm_router.py` | Query complexity classification (SIMPLE/MODERATE/COMPLEX) via small model, auto-selects pipeline preset |

### Core Services (`core/services/`)

| Service | File | Methods |
|---------|------|---------|
| `ActionService` | `action_service.py` | `create`, `get`, `list_by_status`, `mark_done`, `mark_cancelled`, `get_overdue`, `get_stale`, `stats` |
| `DecisionService` | `decision_service.py` | `create`, `get`, `search`, `list_recent`, `for_meeting`, `for_project` |
| `LeadService` | `lead_service.py` | `create`, `get`, `advance_stage`, `log_touchpoint`, `get_stale`, `list_by_stage`, `get_activities`, `pipeline_summary` |
| `MeetingService` | `meeting_service.py` | `create`, `get`, `list_recent`, `search`, `link_action`, `link_decision`, `get_linked_actions`, `get_linked_decisions`, `add_source_link`, `get_source_links` |
| `ObligationService` | `obligation_service.py` | `create`, `get`, `list_all`, `get_upcoming`, `get_overdue`, `update`, `mark_completed`, `mark_overdue`, `stats` |
| `SOPService` | `sop_service.py` | `create`, `get`, `list_all`, `get_stale`, `update`, `mark_exercised`, `mark_reviewed`, `stats` |
| `RailsService` | `rails_service.py` | `create_from_map`, `create_rail`, `get_rail`, `list_rails`, `update_rail`, `add_stage`, `list_stages`, `update_stage`, `advance_stage`, `complete_stage`, `get_escalation_candidates`, `stats` |
| `FeedbackService` | `feedback_service.py` | `create`, `get`, `list_all`, `update`, `resolve`, `stats` |

### Core Infrastructure (`core/`)

| Module | File | Description |
|--------|------|-------------|
| `OrgProfile` / `WorkflowConfig` | `config_loader.py` | Loads `org_profile.yaml` + `workflows.yaml`; typed dataclass access; `org_context_for_prompts()` provides prompt-ready strings |
| Entities | `entities.py` | 11 dataclass entities: `ActionItem`, `Decision`, `Lead`, `MeetingNote`, `KnowledgeGap`, `SourceLink`, `Obligation`, `SOP`, `Feedback`, `Rail`, `RailStage`. Enums: `ActionStatus`, `Priority`, `LeadStage`, `GapStatus` |
| `JobMeta` / `JOB_REGISTRY` | `job_registry.py` | Declarative registry of all 29 scheduled tasks with schedule, module, gate, cog, manual_trigger flag, description. `is_gate_open()` evaluates workflow flags. Wired into AutonomousOps `cog_load`/`cog_unload` |
| `TrustGate` | `trust_controls.py` | Noise-reduction guardrails: quiet hours (22–7), change-required gate, noise budget (3 posts/job/day), audit logging to `autonomous_posts` table |
| `ArtifactContract` | `artifact_contract.py` | Enforces `[type#ID]` artifact references in autonomous posts. `validate_post()`, `extract_refs()`, `format_refs()` |
| Sweep Jobs | `sweep_jobs.py` | `obligation_sweep()`, `sop_drift_check()`, `rail_escalation_check()`, `run_all_sweeps()` |
| Backup/Restore | `backup_restore.py` | `backup_database()` (VACUUM INTO), `list_backups()`, `restore_database()` (with safety backup), `snapshot_config()`, `create_support_bundle()` |
| Chroma Factory | `chroma_factory.py` | `get_vectorstore()` — returns LangChain Chroma in embedded or HTTP mode based on `CHROMA_HOST` env var |
| Seed Workspace | `seed_workspace.py` | `seed_workspace()` — populates fresh DB with sample data for all entity types (idempotent via `.seed_complete` flag) |

---

## 8. Persona System

### Built-in Personas (8)

| Persona | Emoji | Role | Mixin |
|---------|-------|------|-------|
| **Librarian** (Archivist) | 📚 | Knowledge management, gap tracking, document curation | KnowledgeGapTracker cog |
| **Coordinator** (Manager) | 📋 | Meeting orchestration, weekly planning, escalation routing | AutonomousOps core |
| **Scout** | 🔍 | Web research, opportunity discovery, RFP hunting, novel finding detection | `ScoutMixin` |
| **Dreamer** | 💡 | Blue-sky ideation, creative idea generation, viability scoring | `DreamerMixin` |
| **Rainmaker** | 💰 | Lead management, pipeline review, opportunity assessment | `RainmakerMixin` |
| **Steward** | 🛡️ | Self-monitoring, health checks, blind spot detection, learning loop audit | `StewardMixin` |
| **Curator** | 🎨 | Knowledge curation, quality gating, enrichment oversight | `CuratorMixin` |
| **Shepherd** | 🐑 | Partner engagement, onboarding guidance | Via meetings |
| **Accountant** | 📊 | Financial tracking, budget analysis | Via meetings |

### Custom Personas

Created via `/hire`, stored in `custom_personas` table. Active in persona meetings. Removable via `/fire`.

### Meeting Types (9)

`general`, `risk_review`, `pipeline_review`, `gaps_review`, `standup`, `technical_deep_dive`, `prototyping`, `research`, `strategic`

### Persona Exercise Types (7)

| Exercise | Assigned Roles |
|----------|---------------|
| Devil's Advocate | Dreamer + Scout |
| Pre-Mortem | Steward + Coordinator |
| Case Study | Librarian + Scout |
| Technical Spike | Scout + Dreamer |
| Client Roleplay | Rainmaker + Shepherd |
| Prioritization Poker | Coordinator + Accountant |
| Proof Point Sprint | All personas (fallback exercise) |

### Meeting Trigger Hierarchy

1. **Partner agenda** — items submitted via `/agenda`
2. **Knowledge tension** — LLM-detected tensions in diverse ChromaDB docs
3. **DB triggers** — completed project, overdue task, stale gap, stale lead, too many open tasks, user agenda
4. **Fallback** — proof point sprint exercise

---

## 9. Database Tables

SQLite with WAL mode, foreign keys enabled, migration-based schema.

### Core Tables (Migration 001)

| Table | Purpose |
|-------|---------|
| `projects` | Project tracking (name, client, status, budget, dates) |
| `tasks` | Action items (status: todo/in_progress/blocked/done/cancelled, priority, owner, due date, tags, hours) |
| `clients` | Client CRM (industry, website, contacts, relationship_status, lifetime_value) |
| `contacts` | Contact details linked to clients |
| `opportunities` | Sales pipeline (value, probability, status, source, close dates) |
| `touchpoints` | Client interaction log (type, summary, outcome, next action) |
| `decisions` | Decision capture with rationale, category, impact, supersession chain |
| `job_runs` | Scheduled task execution tracking (idempotency) |
| `receipts` | Command execution audit trail |
| `runbooks` | Operational procedure storage |

### Feedback & Gaps (Migration 002)

| Table | Purpose |
|-------|---------|
| `response_feedback` | Bot response feedback (helpful/not_helpful, improvement memo tracking) |
| `knowledge_gaps` | Knowledge gap tracking (topic, question, times_asked, priority_score, status, curation, probing questions) |
| `interview_sessions` | Q&A interview session tracking |
| `interview_questions` | Individual interview questions linked to sessions and gaps |

### Engagement & Escalation (Migration 003)

| Table | Purpose |
|-------|---------|
| `partner_engagement` | Weekly partner metrics (questions asked/answered, response rate, helpful/unhelpful) |
| `sprint_cycles` | Sprint/cycle boundaries with goals and metrics |
| `action_gap_links` | Many-to-many linking of actions to gaps (resolves/related/blocks) |
| `escalations` | Escalation audit trail for action items and knowledge gaps |

### Meeting Notes & Source Links (Migration 004)

| Table | Purpose |
|-------|---------|
| `meeting_notes` | Structured meeting output (summary, attendees, raw text) |
| `source_links` | Provenance chain linking artifacts to origins |

### Continuity & Rails (Migration 005)

| Table | Purpose |
|-------|---------|
| `obligations` | Recurring requirements (frequency, owner, next_due, status, checklist, evidence) |
| `sops` | Standard Operating Procedures (versioned, body, checklist, exercise/review tracking) |
| `feedback` | Structured product feedback (category, severity, environment snapshot) |
| `rails` | Venture lifecycle tracks (validate/launch/operate) |
| `rail_stages` | Ordered stages within rails (required/actual outputs, escalation_days) |
| `autonomous_posts` | Trust controls audit log (job_name, suppressed, suppression_reason) |

### Auxiliary Tables (created in `database.py._ensure_aux_tables()`)

| Table | Purpose |
|-------|---------|
| `meeting_agenda_items` | Partner-submitted discussion topics with priority and expiry |
| `partner_point_events` | Gamification point awards (entity_type, reason, points) |
| `partner_updates` | Partner quick wins logged via `/did` |
| `open_questions` | Tracked unanswered questions from channels |
| `pm_proposals` | PM automation: proposed action items / decisions from chat |
| `pm_threads` | PM automation: thread tracking |
| `pm_dashboard_state` | PM automation: dashboard message state |
| `leads` | Sales leads with full pipeline tracking (source, status, contact, value, next action) |
| `lead_activity` | Lead activity log (type: created/status_change/note/outreach/meeting/proposal_sent/follow_up/nudge) |
| `past_clients` | Past client re-engagement tracking |
| `bot_command_usage` | Command execution metrics for engagement analytics |
| `bot_questions` | Questions asked to bot with quality tracking |
| `learning_loop_events` | Gap → investigation → memo → ingest → verification lifecycle |
| `bot_health_snapshots` | Periodic health metric snapshots |
| `recurring_blind_spots` | Recurring unanswered question patterns |
| `persona_meeting_takeaways` | Meeting insights/takeaways for weekly digest |
| `custom_personas` | User-created personas (key, name, emoji, personality, concerns) |
| `rainmaker_seen_opportunities` | Dedup tracker for Rainmaker-scanned URLs |
| `schema_versions` | Migration tracking (created by `migrations/runner.py`) |
| `task_owners` | Multi-owner support for tasks (from `refactor_actions_owner.py`) |

---

## 10. Prompt Templates

### System Prompts (`prompts/`)

| File | Purpose |
|------|---------|
| `system_prompt.txt` | Main system prompt for the bot's LLM persona |
| `system_prompt_backup.txt` | Backup of system prompt |
| `operational_context.txt` | Operational facts injected into many prompts (capabilities, constraints) |
| `operational_context.example.txt` | Example operational context for new installs |
| `exercise_types.md` | Definitions of the 7 persona exercise types |

### Persona Prompts (`prompts/personas/`)

| File | Persona |
|------|---------|
| `librarian.txt` | Archivist/Librarian persona prompt |
| `coordinator.txt` | Coordinator/Manager persona prompt |
| `scout.txt` | Scout persona prompt |
| `dreamer.txt` | Dreamer persona prompt |
| `rainmaker.txt` | Rainmaker persona prompt |
| `steward.txt` | Steward persona prompt |
| `shepherd.txt` | Shepherd persona prompt |
| `accountant.txt` | Accountant persona prompt |

### Inline Prompt Templates (in code)

Numerous `ChatPromptTemplate.from_template(...)` prompts embedded throughout:
- Scout plan generation, summary generation, follow-up query generation
- Dreamer idea generation, refinement, scheme response
- Rainmaker opportunity assessment, hunt query generation
- Knowledge gap classification, probing question generation, anti-tautology rewriting
- Meeting takeaway extraction, persona conversation generation (with anti-slop filtering)
- Improvement memo generation (from negative feedback)
- Interview memo generation, sufficiency check, open questions extraction
- Temporal context scoring

---

## 11. Configuration Surface

### YAML Configuration Files (`config/`)

| File | Sections | Key Settings |
|------|----------|--------------|
| `org_profile.yaml` | org (name, tagline, industry, location, region, capabilities), members (discord_user_id, name, roles), channels, branding, mode, timezone | Org identity, team roster, channel mapping |
| `workflows.yaml` | memory, work, pipeline, health, persona_meetings, automation (artifact_contract), trust_controls, sweeps, noise_budget | Feature toggles, trust gates, sweep intervals |
| `rail_maps.yaml` | Predefined rail stage templates for validate/launch/operate tracks | Template-based rail creation |

### JSON Configuration Files (`config/`)

| File | Purpose |
|------|---------|
| `bot_settings.json` | All 12 runtime config sections (persisted by BotConfigManager) |
| `model_router.json` | Pipeline role configuration (backend, model, temperature per role) |
| `model_router_schema.json` | JSON schema for model_router.json |
| `model_router.example.json` | Example model router configuration |

### Config Dataclass Sections (`services/bot_config.py`)

| Section | Key Fields |
|---------|------------|
| `DiscordSettings` | guild_id, bots_channel_id, partners_channel_id, allowed_channel_ids |
| `PartnerSettings` | partner_user_ids, partner_role_name |
| `PMAutomationSettings` | enabled, extract_actions, extract_decisions, track_open_questions |
| `ActionItemSettings` | wip_limit, stale_days, auto_assign, escalation_enabled |
| `GamificationSettings` | enabled, action_complete_points, gap_resolve_points, interview_points, bounty_claim_bonus |
| `ScoutSettings` | enabled, daily_search_enabled, background_crawl_enabled, max_results_per_search |
| `StewardSettings` | enabled, daily_health_check, weekly_self_assessment, learning_loop_audit |
| `DreamerSettings` | enabled, temperature, min_viability_score, min_grounding_score |
| `RainmakerSettings` | enabled, morning_pipeline, opportunity_hunt, follow_up_nudges |
| `KnowledgeGapSettings` | enabled, gap_reminder, Thursday_prompt, escalation_check, hygiene_sweep |
| `LLMSettings` | model_name, temperature, max_tokens, embedding_model |
| `ScheduleSettings` | timezone, quiet_hours_start, quiet_hours_end |

### Environment Variables / Secrets

| Key | Source | Purpose |
|-----|--------|---------|
| `discord_token` | Keyring/env | Discord bot token |
| `openai` | Keyring/env | OpenAI API key |
| `anthropic` | Keyring/env | Anthropic API key |
| `openrouter` | Keyring/env | OpenRouter API key |
| `tavily` | Keyring/env | Tavily search API key |
| `database_url` | Keyring/env | Database path override |
| `CHROMA_HOST` | Env | Chroma HTTP host (for Docker mode) |
| `ADMIN_AUTH_DISABLED` | Env | Disable admin GUI auth for local dev |

---

## 12. Known Tech Debt & TODOs

### Explicit TODOs in Code

| Location | Item |
|----------|------|
| `cogs/LLM.py:861` | `TODO: Context menus need to be registered differently in Discord.py 2.x` — "Ask Bot About This" context menu is commented out |

### Commented-Out Features

| Feature | Location | Notes |
|---------|----------|-------|
| Lead management slash commands (`/lead add/list/update/pipeline/touch`) | `AutonomousOps.py` | Fully implemented (~200 lines) but commented out "to reduce complexity" |

### Structural Debt

| Item | Description |
|------|-------------|
| **Duplicate model definitions** | `models.py` (top-level) and `core/entities.py` define overlapping `TaskStatus`, `Priority`, `KnowledgeGap`, etc. — should be consolidated |
| **AutonomousOps size** | 8,140 lines in a single file; partially mitigated by persona mixins but still very large |
| **KnowledgeGapTracker size** | 3,868 lines — could benefit from service extraction |
| **`database.py._ensure_aux_tables()`** | 500+ lines of CREATE TABLE statements that duplicate migration SQL — fragile if schemas drift |
| **Legacy PostgreSQL remnants** | `001_initial_schema.sql` and `002_feedback_and_gaps.sql` (non-SQLite) still in migrations folder |
| **Job registry wired** | `JOB_REGISTRY` gate-driven iteration replaced ~90 lines of ad-hoc code in AutonomousOps |
| **Trust gate inconsistent usage** | `TrustGate` class exists but not all autonomous posts route through it |
| **NC eVP scraping is fragile** | HTML scraping of government portal — will break on layout changes |
| **`config.py`** exists at both top level and inside `LeisureLLM/` — potential import confusion |

---

## 13. Test Suite

| File | Tests | Coverage Area |
|------|-------|---------------|
| `tests/test_smoke.py` | 12 | Infrastructure: imports, DB connect, migration runner, service instantiation |
| `tests/test_workflow_acceptance.py` | 14 | Workflow: action creation, meeting parsing, gap lifecycle, lead pipeline, obligations, SOPs, rails |
| `tests/test_continuity.py` | 31 | Service layer: ObligationService, SOPService, RailsService, FeedbackService, sweeps, seed_workspace, backup_restore |
| `tests/test_scout_mixin.py` | 15 | Scout persona: state management, novelty filtering, query planning, URL utilities |
| `tests/test_admin_gui.py` | 94 | Admin GUI: page routes return 200, CRUD API smoke, auth flow |
| `tests/test_chat_pipeline.py` | 15 | Chat pipeline: tool calling, confirmation gates, streaming |
| `tests/test_model_router.py` | 36 | Model router: backend registration, pipeline config, fallbacks, presets, VSLM routing |
| `tests/test_chunk_enrichment.py` | 12 | Chunk enrichment: extraction, anti-hallucination, confidence scoring |
| `tests/test_autonomous_ops.py` | 10 | AutonomousOps: digest, meeting creation, trust gate |
| `tests/test_knowledge_gap_tracker.py` | 14 | Knowledge gaps: detection, curation, interview lifecycle |
| `tests/test_inbox.py` | 11 | Inbox: thread creation, message posting, gap interview flow |
| `tests/test_hyde_retrieval.py` | 8 | HyDE retrieval: hypothesis generation, result merging |
| `tests/test_web_chat_gap_detection.py` | 11 | Web chat: automatic knowledge gap detection from sparse context |
| `tests/test_fill_gaps_fallback.py` | 9 | Fill Gaps: defer fallback, stale recovery, priority ordering |
| `tests/test_agentic_features.py` | 33 | Agentic chat: tool calling, planning loop, confirmation gates |
| `tests/test_answer_self_assessment.py` | 19 | Answer quality self-assessment scoring |
| `tests/test_autonomous_ops.py` | 24 | AutonomousOps: digest, meeting creation, trust gate, persona meeting |
| `tests/test_job_registry.py` | 17 | Job registry: gate evaluation, module filtering |
| `tests/test_knowledge_health.py` | 7 | Knowledge health: confidence decay, staleness |
| `tests/test_planning_loop.py` | 8 | Multi-step planning loop execution |
| `tests/test_proactive_suggestions.py` | 16 | Proactive suggestion triggers and deduplication |
| `tests/test_response_cache.py` | 10 | Response cache: hit/miss, TTL, invalidation |
| `tests/conftest.py` | — | Fixtures: in-memory SQLite DB, migration runner |

**Total: 483 passing tests; ~60s runtime**

---

## 14. Document Ingestion Pipeline

| Component | Details |
|-----------|---------|
| **Supported formats** | PDF, DOCX, JSON, JSONL, TXT, MD |
| **Hash change detection** | SHA3-512 hashes tracked in `hashes_v3.csv` |
| **Chunking** | `RecursiveCharacterTextSplitter` (chunk_size=2000, overlap=400) |
| **Embeddings** | Ollama `nomic-embed-text` (local, no cloud API required) |
| **Vector store** | ChromaDB (`chroma_v3/`) |
| **Superseded-doc filtering** | RAG retriever filters out docs with `superseded: true` in YAML frontmatter |
| **Incremental reindex** | `daily_knowledge_refresh` job + manual `/reindex_docs` command |

---

## 15. Deployment

| Method | Details |
|--------|---------|
| **Local** | `python launcher.py` → creates `.venv`, installs deps, runs migrations, opens admin console |
| **Lightweight** | `python start.py` (or `pythonw start.py` on Windows for headless) |
| **Docker** | `Dockerfile` + `docker-compose.yml` |
| **Admin GUI** | FastAPI + Uvicorn on `127.0.0.1:8000` (started alongside bot) |
| **Database setup** | Automatic via `database.py.connect()` migration runner (12 migrations) |
| **Dependencies** | `requirements.txt` + `pyproject.toml` (ruff linting) |
