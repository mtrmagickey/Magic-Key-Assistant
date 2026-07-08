# Installation Guide

> **Target format**: All configuration happens in the admin console at `localhost:8000`. This document covers only the machine-level prerequisites and the one command that starts everything.

This guide focuses on successful first-run experience, not just successful package install.

By the end of setup, you should have:

1. A running model backend (local or cloud).
2. The web console reachable at `http://localhost:8000`.
3. A visible "Getting Started Checklist" card on the dashboard to guide first-use milestones.

---

## Prerequisites

| Requirement | Why | Get it |
|-------------|-----|--------|
| **Python 3.12+** | Runtime | [python.org/downloads](https://www.python.org/downloads/) — check "Add Python to PATH" |
| **Git** | Clone the repo | [git-scm.com](https://git-scm.com/) |
| **Ollama** *(recommended)* | Local LLM inference — free, no API key | [ollama.com](https://ollama.com) |
| **llama.cpp** *(optional)* | Alternative local inference — 2–3× faster for MoE models | Installed via the admin console (binary + GGUF model download) |
| **Discord Bot Token** *(optional)* | Team mode — the bot connects to your Discord server | [discord.com/developers](https://discord.com/developers/applications) → New Application → Bot → Token |
| **OpenAI API Key** *(optional)* | Cloud LLM fallback | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| **Tavily API Key** *(optional)* | Web search via Scout persona | [tavily.com](https://tavily.com) (free tier) |

### Hardware

Desktop-class machine. 4 GB RAM minimum, 8 GB recommended. For local LLM inference: a GPU with 6+ GB VRAM significantly improves response speed. MoE models like Qwen3.5-35B-A3B run well on 8–12 GB VRAM with llama.cpp.

---

## Install — One-Command Launcher (recommended)

```powershell
# Repository name remains `LeisureCenterAssistant` for compatibility.
git clone https://github.com/mtrmagickey/LeisureCenterAssistant
cd LeisureCenterAssistant
python launcher.py
```

The launcher opens a **visual progress page** in your browser — same glassmorphism theme as the admin console. It:

1. Creates a virtual environment (`.venv/`)
2. Installs all pip dependencies
3. Runs SQLite database migrations
4. Starts the admin server on `localhost:8000`
5. Redirects you straight into the Setup Wizard

No other commands needed. When the wizard finishes, press `Ctrl+C` and run `python launcher.py` again — it will detect setup is complete and start the full bot, opening the admin console in your browser automatically.

What to expect while this runs:

1. A browser progress screen appears quickly.
2. Dependency installation may take several minutes on a clean machine.
3. First launch may feel "empty" until you add docs and capture first actions/decisions.
4. This is normal; the dashboard checklist is designed to guide the first few sessions.

---

## Install — Manual Path (3 commands)

```powershell
# 1. Clone and enter the repo
# Repository name remains `LeisureCenterAssistant` for compatibility.
git clone https://github.com/mtrmagickey/LeisureCenterAssistant
cd LeisureCenterAssistant

# 2. Create a virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
pip install -r requirements.txt
pip install -r LeisureLLM/requirements.txt

# 3. Initialize the database
cd LeisureLLM
.\SetupDatabase_SQLite.ps1
```

---

## First-Run Setup (in your browser)

If you used the launcher, you're already in the wizard — skip this step.

Start the admin server (from `LeisureLLM/`):

```powershell
python -m admin.server
```

Open **http://localhost:8000** — you'll be redirected to the **Setup Wizard**, which walks you through:

1. **External Services** — paste your Discord token, OpenAI key, and optional Tavily/Anthropic keys
2. **Your Organization** — name, industry, team size
3. **Modules** — choose which capabilities to activate (Memory, Work, Pipeline, Health)

The wizard writes your `.env`, `config/org_profile.yaml`, and `config/workflows.yaml` automatically.

After wizard completion, open the dashboard and look for the launch checklist. It tracks practical first-use outcomes (first question, first action, first decision, first gap closed), not only setup form completion.

---

## Start the Bot

```powershell
python launcher.py
```

The launcher activates the virtual environment, installs any new dependencies, runs migrations, and launches the bot. The admin console is available at `localhost:8000` whenever the bot is running.

> **Tip:** On subsequent runs, `python launcher.py` from the repo root does the same thing — it detects setup is complete, starts the bot, and opens the admin console in your browser automatically. You can also use `python start.py` as a lightweight cross-platform alternative.
>
> On Windows, `pythonw start.py` runs the bot without a visible terminal window.

---

## What's Next

See [GET_STARTED.md](GET_STARTED.md) for:

1. Interface tour.
2. Scenario playbooks.
3. The 10-step first-use checklist explained.
4. First three session plans.

---

## Platform Notes

### macOS / Linux

Replace PowerShell activation with:

```bash
source .venv/bin/activate
```

Database setup:

```bash
cd LeisureLLM
# The launcher runs all migrations automatically.
# For manual setup, run each migration in order:
python run_migration_sqlite.py assistant.db migrations/001_initial_schema.sqlite.sql
python run_migration_sqlite.py assistant.db migrations/002_feedback_and_gaps.sqlite.sql
python run_migration_sqlite.py assistant.db migrations/003_engagement_and_escalation.sqlite.sql
python run_migration_sqlite.py assistant.db migrations/004_meeting_notes_and_source_links.sqlite.sql
python run_migration_sqlite.py assistant.db migrations/005_continuity_rails.sqlite.sql
# ... through the latest numbered migration
```

### Docker

```bash
docker compose up --build
```

Volume mounts preserve the database, chroma index, docs, and config across rebuilds.

See the `.env.example` in the repo root for the full list of environment variables — the same file works for both local and Docker deployment.
