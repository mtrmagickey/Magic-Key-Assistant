# Magic Key Assistant

> Local-first AI operations harness for solo operators and small teams — structured memory, durable records, and autonomous routines that keep work visible and moving.

<p align="center">
  <a href="https://github.com/mtrmagickey/Magic-Key-Assistant/releases/latest">
    <img src="https://img.shields.io/badge/⬇%20Download%20for%20Windows-MagicKey%20Beta%20Release-2BA285?style=for-the-badge&logoColor=white" alt="Download Magic Key Assistant for Windows" height="48">
  </a>
</p>

<p align="center">
  <b>No Python. No setup. Just download and double-click.</b><br>
  <sub><a href="https://github.com/mtrmagickey/Magic-Key-Assistant/releases/latest">Get the latest <code>MagicKey-Beta-Release-1.0.exe</code></a> → it installs everything on first run.</sub>
</p>

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![SQLite WAL](https://img.shields.io/badge/sqlite-WAL_mode-blue.svg)](https://www.sqlite.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-admin_console-green.svg)](https://fastapi.tiangolo.com/)
[![Docker Ready](https://img.shields.io/badge/docker-compose-blue.svg)](https://docs.docker.com/compose/)

---

## What is Magic Key Assistant?

Magic Key Assistant is a self-hosted **operations harness** — a domain-specific agent that captures commitments, context, and knowledge from natural conversation, structures them into durable records, and keeps those records actionable over time — without turning your workflow into a bureaucracy.

It wraps any LLM (local or cloud) with the context, tools, memory, and control rails that make it genuinely useful for operations. You run a single process on your own machine. It becomes persistent memory that survives context switches, interruptions, and busy weeks.

**Who is it for?** Casual local-hosted AI users, solo operators, and very small teams (0–6) who want durable memory and follow-through without giving up control of their data.

**What makes it different?**

- **Self-hosted** — runs on your hardware, your rules
- **Web-first** — a browser-based admin console is the primary interface; no terminal needed after first launch
- **Solo or team** — works standalone via the web console, or adds Discord as a team collaboration layer
- **Capture-first** — input is natural language, not forms
- **Forward-only** — scheduled routines surface what's overdue, stale, or missing before it slips
- **Model-flexible** — run local Ollama/llama.cpp by default, then optionally route harder tasks to OpenAI, Anthropic, or OpenRouter when performance matters
- **Agentic** — chat is a command surface: ask questions or give instructions, with confirmation gates for mutations
- **Resilient** — circuit breakers, fallback chains, and self-correcting retrieval keep the system responsive even when backends degrade
- **Self-improving** — feedback loops, confidence decay, and learning signals silently improve retrieval quality the more you use it

**What do you need?** On Windows, just the prebuilt release exe and an LLM backend (Ollama is free and local) — no Python or Git required. Building from source needs Python 3.12+.

---

## How it works

```
                          ┌──────────────────────────────────────────┐
                          │         Magic Key Assistant              │
                          │         (single process)                 │
                          │                                          │
 ┌──────────────┐         │   ┌──────────────┐  ┌───────────────┐   │
 │  Web Console │◄────────┼──►│  FastAPI      │  │  Scheduled    │   │
 │  localhost:8000        │   │  Admin + API  │  │  Routines     │   │
 └──────────────┘         │   └──────┬───────┘  └───────┬───────┘   │
                          │          │                  │           │
 ┌──────────────┐         │   ┌──────▼──────────────────▼───────┐   │
 │   Discord    │◄────────┼──►│       Shared Service Layer       │   │
 │  (optional)  │         │   │  LLM · RAG · Artifacts · Jobs   │   │
 └──────────────┘         │   └──────┬──────────────────┬───────┘   │
                          │          │                  │           │
                          │   ┌──────▼─────┐     ┌──────▼─────┐     │
                          │   │  SQLite    │     │  ChromaDB  │     │
                          │   │  (WAL)     │     │  (vectors) │     │
                          │   └────────────┘     └────────────┘     │
                          └──────────────────────────────────────────┘
```

The web console is the primary interface. It provides conversations, data management, configuration, and system monitoring. Discord is an optional collaboration layer — valuable for teams, unnecessary for solo users. The system auto-detects which mode to run based on whether a Discord token is configured.

---

## Key capabilities

### Structured capture

When you use `/remember`, the Remember form, or `/parse_meeting`, the system classifies your input and writes it to the appropriate artifact table:

| Artifact | What it captures | How it gets created |
|----------|-----------------|---------------------|
| **Action** | Owner, due date, status, dependencies, estimated effort | `/remember`, Remember form, meeting parsing, agentic tool calls |
| **Decision** | What was decided, why, who, rationale, state (proposed → accepted / rejected / superseded) | `/remember` (decision language detected), meeting parsing |
| **Lead** | Stage, next action, value estimate, last touch, activity log | `/remember`, Leads page form, Rainmaker persona |
| **Meeting note** | Summary, extracted actions and decisions, risks, agenda items | `/parse_meeting`, Remember form |
| **Knowledge gap** | Unanswered questions, times asked, linked actions | Auto-detected when chat queries lack context |
| **Obligation** | Recurring requirements (compliance / financial / operational / legal), frequency, next due | Remember form, manual creation |
| **SOP** | Versioned procedures with checklists, linked decisions, last-exercised date | Remember form, manual creation |
| **Rail** | Multi-stage ventures (validate → launch → operate) with required/actual outputs per stage | Rails page, `/remember` |
| **Work packet** | Grouped tasks with cross-links for related work | Agentic tool calls, manual grouping |

The `/ask` command also detects intent — if you type "we decided to go with vendor X" instead of a question, it offers to route the input to `/remember` for structured capture.

All paths write to the same durable store. The web admin console provides full CRUD pages for each artifact type, plus provenance tracking and an audit trail for every mutation.

### Intelligent knowledge base

Documents in `LeisureLLM/docs/` are chunked, embedded into ChromaDB, and available for retrieval-augmented generation. But the system goes beyond simple vector search:

- **LLM enrichment at ingestion** — each chunk is analysed by a local model to extract a summary, topics, content type (17 categories), participants, date ranges, actionability score, key questions, and named entities
- **Anti-hallucination validation** — extracted entities and participants are verified against the source text; confidence is reduced if over half can't be found
- **Org context injection** — your organization profile is used to help the enrichment model recognize domain-specific names and terminology, without leaking that context into the output
- **Temporal awareness** — queries like "what happened last week" resolve against today's date and chunk date ranges
- **Confidence-aware retrieval** — low-confidence enrichments are de-weighted so unreliable metadata doesn't pollute results; confidence decays over time (180-day half-life), and stale or contradictory docs are automatically flagged as knowledge gaps
- **HyDE expansion** — hypothetical document embeddings improve recall for abstract or poorly-worded queries
- **Self-correcting retrieval** — when initial results are weak, the system reformulates and retries before falling back to the LLM
- **Response cache** — an in-memory semantic cache (LRU, TTL-based) bypasses the LLM entirely for repeated or near-duplicate queries
- **Feedback-driven quality tracking** — every response silently records which source documents contributed; over time, consistently unhelpful sources are surfaced as outliers
- **Corpus health analysis** — topic clustering, thin-topic detection, fragment consolidation, staleness scans, and contradiction detection run on schedule and surface findings for review

A folder watcher and daily refresh job re-index changed files automatically. The Remember form and `/teach` command add structured knowledge on the fly.

### Agentic chat

The web chat and Discord `/ask` both use the same RAG pipeline with real-time streaming — but the web chat is also a **command surface**:

- Token-by-token streaming with markdown rendering
- **Tool-calling** — say "create an action for..." and the system routes to the right tool via a protocol-based tool registry
- **Confirmation gate** — mutating operations require explicit user approval before execution
- **Proactive suggestions** — the system detects overdue items, trending concerns, cold leads, and pattern connections, then surfaces them as nudges in chat (zero LLM calls, template-driven, rate-limited)
- **Interaction memory** — queries are logged and clustered into concern threads; recurring concerns are detected and surfaced proactively
- Source attribution — see which documents contributed to each answer
- Inline feedback buttons that silently improve retrieval quality over time
- Multi-turn conversation with context carry-over (web) and threaded reply (Discord)
- 3-phase synthesis pipeline for deep analysis (`/deep_consult`): Initial → Critique → Synthesise, each routable to a different model
- **Answer self-assessment** — the LLM evaluates its own response quality and confidence; low-confidence answers are flagged

### Continuity routines

Lightweight scheduled sweeps run automatically to keep your operation coherent:

- Surface overdue commitments and stale opportunities
- Prompt for missing ownership or missing rationale
- Detect knowledge gaps and generate interview questions
- Escalate stalled work into a small set of concrete options
- Recover stalled inbox threads

A declarative **job registry** manages all scheduled work with named schedules, module gating, and manual trigger support. Jobs are visible and controllable from the admin console.

These are the default experience — **Continuity Only** mode. No configuration required.

### Accelerators (opt-in)

Switching to **Continuity + Accelerators** mode enables persona-driven background jobs:

- **Scout** — web research, opportunity discovery, RFP search, industry news, novel finding detection
- **Dreamer** — strategic ideation from recent signals, ambitious reframing, prototype paths
- **Rainmaker** — pipeline prospecting, follow-up nudges, lead sourcing, cold reviews, past-client check-ins
- **Steward** — engagement health, weekly self-assessment, blind spot detection, improvement recommendations
- **Curator** — corpus quality scans, deep analysis, auto-synthesis with review gates, targeted interviews, and scheduled self-interrogation to surface structural knowledge gaps

Additional prompt-based personas (**Librarian**, **Accountant**, **Coordinator**, **Shepherd**) provide specialized viewpoints for analysis and consultation without running autonomous jobs.

Accelerators are off by default. Enable them with one click in Organization → Operating Mode.

**Trust controls** keep autonomous activity in check: quiet hours prevent overnight noise, daily post budgets cap output per persona, and an artifact contract requires every autonomous post to reference a durable record or be suppressed.

### Multi-model routing

Route different tasks to different LLM backends. A visual pipeline editor in the admin console lets you assign models per task type — use Ollama or llama.cpp locally for routine work, route complex analysis to OpenAI, Anthropic, or OpenRouter when you choose.

**Pipeline presets** (Speed / Balanced / Quality) let users apply curated configurations without manually assigning models per role. An optional **VSLM router** classifies query complexity with a small local model and auto-selects the right preset — simple questions get fast answers, complex ones get the full pipeline.

**Token estimation** monitors every LLM call for silent context truncation — when prompts approach or exceed a model's context window, the system logs the event and surfaces it in the admin console. **Inference cost tracking** tallies token usage and estimated cost per backend, with configurable budgets.

### Web console

The browser-based admin console at `localhost:8000` is the primary control plane:

| Area | Pages |
|------|-------|
| **Conversations** | Chat (streaming, tool-calling, source citations), Inbox (async question threads) |
| **Artifacts** | Actions, Leads, Decisions, Meetings, Provenance |
| **Knowledge** | Knowledge Base (search, upload, ingestion status), Knowledge Gaps, Teach |
| **Operations** | Dashboard (getting-started checklist, overdue items, health snapshot), Activity Log |
| **System** | System Health (Ollama/llama.cpp status, backup/restore, sweeps), Model Config, Retrieval Log, Jobs |
| **Settings** | Setup Wizard, Secrets, Organization Profile, Bot Config |

Solo-mode users see a clean interface with no Discord-specific UI. Team-mode users get additional Discord configuration surfaces.

See [GET_STARTED.md](GET_STARTED.md) for the current page tour.

### Data safety

- **Encrypted backups** — AES-256-GCM encrypted backup and restore with PBKDF2 key derivation
- **Data retention policies** — configurable auto-purge for conversations, feedback, and inference logs
- **Audit log export** — compliance-ready event export
- **Cryptographic erasure** — secure wipe when needed
- **Secrets management** — API keys stored via OS keyring, never in plaintext config

---

## Quick start

### 1. Download and run (Windows)

**Most people should use this path.** Download `MagicKey-Beta-Release-1.0.exe` from the [latest release](https://github.com/mtrmagickey/Magic-Key-Assistant/releases) and double-click it.

No Python, no Git, no command line. On first run the exe sets everything up (virtual environment, dependencies, database) and opens the Setup Wizard in your browser automatically.

<details>
<summary>Prefer to run from source? (developers)</summary>

```powershell
# Repository name remains `LeisureCenterAssistant` for compatibility.
git clone https://github.com/mtrmagickey/LeisureCenterAssistant
cd LeisureCenterAssistant
python launcher.py
```

The launcher creates a virtual environment, installs dependencies, runs database migrations, and opens the Setup Wizard in your browser. Requires Python 3.12+.

</details>

> No API keys or config files needed to start. The wizard handles everything.

### 2. Complete the Setup Wizard

Open **http://localhost:8000** — the wizard walks you through:

1. **LLM backend** — Ollama (local, free), llama.cpp (power users), OpenAI, Anthropic, or a mix
2. **Organization identity** — name, timezone, team size
3. **Module selection** — Memory, Work, Pipeline, Health
4. **Discord** *(optional)* — bot token and channel IDs, only if you want team mode

### 3. Start working

After the wizard, start the app again to run the full assistant:

- **Exe users:** double-click `MagicKey-Beta-Release-1.0.exe` again.
- **Source users:** press `Ctrl+C` in the terminal and run the launcher again:

```powershell
python launcher.py
```

On subsequent runs the app detects that setup is complete, starts the full bot, and **opens the admin console in your browser automatically** — no URLs to remember.

Alternatively (source install), use the lightweight start script:

```powershell
python start.py              # normal start (opens browser)
python start.py --no-browser # headless / scripted start
```

On Windows, `pythonw tray.py` runs via a system tray icon with start/stop/browser controls and no visible terminal window.

### What you will have

- A running assistant with your chosen LLM backend
- Auth-protected web console with CSRF protection
- Empty knowledge base ready for your first documents
- Folder watcher monitoring `LeisureLLM/docs/` for new files
- Continuity routines scheduled and running

---

## Deployment options

| Method | Command | Best for |
|--------|---------|----------|
| **Portable release exe** | `MagicKey-Beta-Release-1.0.exe` | **Most users** — download and double-click, no Python/Git needed |
| **Launcher** | `python launcher.py` | Developers running from a source checkout |
| **Start script** | `python start.py` | Subsequent runs from source — fast restart |
| **System tray** | `pythonw tray.py` | Windows/cross-platform background operation |
| **Docker Compose** | `docker compose up -d` | Containerised deployment (bot + admin + ChromaDB) |
| **Windows installer** | `MagicKeyAssistant.exe` | Guided installer with Start Menu shortcut — see the [Windows installer guide](docs/release/windows-installer.md) |
| **CLI inspector** | `python system_cli.py status` | Read-only system status from the terminal |

> **⚠️ Security note — enable authentication before exposing the console.**
> The web console is bound to `localhost` by default, where admin authentication is intentionally **off** (single operator at the keyboard). If you expose the console beyond localhost — e.g. binding to `0.0.0.0`, running in Docker with a published port, or putting it behind a reverse proxy — you **must** enable login first by setting `ADMIN_AUTH_ENABLED=1` (or setting `ADMIN_GUI_HOST` to a non-localhost address, which enables auth automatically). Never expose an unauthenticated console to a shared or public network.

---

## Configuration

Config files live in `LeisureLLM/config/` and are managed through the web console:

| File | Purpose |
|------|---------|
| `.env` | Secrets (API keys, tokens) — written by Setup Wizard |
| `org_profile.yaml` | Organization name, mode (solo/team), industry, team size |
| `workflows.yaml` | Module gating (Memory, Work, Pipeline, Health) |
| `model_router.json` | Backend config per pipeline role (initial / critique / synthesize) |
| `bot_settings.json` | Runtime settings |
| `rail_maps.yaml` | Rail stage definitions |

The system defaults to **solo mode** when no Discord token is present. Set `OPERATION_MODE=team` or configure a token to enable Discord integration.

See [GET_STARTED.md](GET_STARTED.md#configuration-reference) for the full reference.

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| **Web framework** | FastAPI + Uvicorn + Jinja2 + Tailwind CSS |
| **Bot framework** | discord.py (optional, team mode only) |
| **LLM orchestration** | LangChain (core, OpenAI, Ollama, Anthropic, community, Chroma) |
| **Relational store** | SQLite in WAL mode via aiosqlite |
| **Vector store** | ChromaDB (local or Docker via HTTP) |
| **Embeddings** | sentence-transformers (cross-encoder reranking) |
| **Web search** | Tavily API (optional, free tier available) |
| **Token counting** | tiktoken |
| **Secrets** | OS keyring (keyring library) |
| **Retry / resilience** | tenacity + custom circuit breaker |

---

## Docker

```bash
docker compose up --build
```

Three services: **bot** (worker), **admin** (FastAPI), **chroma** (vector store). Volume mounts preserve `assistant.db`, `chroma_v3/`, `docs/`, and `config/` across container rebuilds. The console is available at `localhost:8000`.

---

## Troubleshooting

See [GET_STARTED.md](GET_STARTED.md#troubleshooting) for common issues and fixes.

**Logs:** `Get-Content leisurellm.log -Tail 100`

---

## Documentation

| Document | Description |
|----------|-------------|
| [GET_STARTED.md](GET_STARTED.md) | First-run adoption guide, admin console reference, troubleshooting |
| [INSTALLATION.md](INSTALLATION.md) | Prerequisites and platform-specific setup |
| [docs/architecture/system.md](docs/architecture/system.md) | Stable system architecture and responsibility boundaries |
| [docs/architecture/local-llm.md](docs/architecture/local-llm.md) | Local-first model routing and inference design |
| [docs/release/development-build.md](docs/release/development-build.md) | Maintainer build, validation, Docker, and packaging workflow |
| [docs/release/windows-installer.md](docs/release/windows-installer.md) | Windows packaging and installer build guide |
| [FULL_INVENTORY.md](docs/internal/FULL_INVENTORY.md) | All commands, jobs, tables, and config reference |

Internal planning docs live under [docs/internal/](docs/internal/).

---

## What it is not

- Not an autopilot — it surfaces information, you make the calls
- Not a cloud service — nothing leaves your machine unless you point an LLM at an external API
- Not a replacement for leadership judgment — it makes your decisions more visible, not less yours
- Not a generic chat wrapper — we don't compete with ChatGPT; we're the harness that makes any LLM useful for your operations

## How it compares

|                  | Magic Key Assistant            | SaaS tools          | Generic AI chat    |
| ---------------- | ------------------------------ | -------------------- | ------------------ |
| Where it runs    | Your machine                   | Vendor cloud        | Vendor cloud       |
| Architecture     | Domain-specific harness        | Monolith app        | Raw model + prompt |
| Primary input    | Natural conversation           | Manual entry        | Prompts only       |
| Memory model     | Durable records + traceability | Lists and docs      | Context window     |
| Action model     | Tools with confirmation gates  | Manual clicks       | Copy-paste output  |
| Failure handling | Logs gaps, routes interviews   | User responsibility | Fills with guesses |
| Data control     | Full local ownership           | Vendor-held         | Vendor-held        |
| Lock-in          | None by default                | High                | High               |

--
