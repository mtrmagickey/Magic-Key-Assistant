# Development And Build Guide

> Maintainer guide for local development, validation, container builds, and release packaging.

This replaces the old root-level build notes without putting another maintainer document back in the repository root.

## What This Covers

Use this guide when you need to:

- run the app locally as a developer
- validate changes with the same basic checks CI uses
- build the Docker deployment
- build the offline wheelhouse bundle
- package the Windows tray app and installer

For first-run user installation, see [INSTALLATION.md](../../INSTALLATION.md).

## Local Development

### Fast Path

From the repository root:

```powershell
python launcher.py
```

The launcher creates or reuses `.venv`, installs dependencies, runs migrations, starts the app, and opens the admin console.

### Manual Environment Setup

If you want the explicit developer path:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pytest pytest-asyncio ruff
```

The root `requirements.txt` delegates to the canonical dependency list in [LeisureLLM/requirements.txt](../../LeisureLLM/requirements.txt).

### Running The App

Repository root:

```powershell
python start.py
```

Admin-only server from the application directory:

```powershell
cd LeisureLLM
python -m admin.server
```

## Validation

The baseline local validation path should match the current CI workflow in [.github/workflows/ci.yml](../../.github/workflows/ci.yml).

### Lint

```powershell
ruff check LeisureLLM/ tests/
```

### Tests

```powershell
python -m pytest tests/ -v --tb=short
```

For focused work, run the touched suite directly, for example:

```powershell
python -m pytest tests/test_chat_pipeline.py -v --tb=short
```

## Docker Build

Copy `.env.example` to `.env`, then build and start the stack from the repository root:

```powershell
docker compose up --build
```

The compose stack starts three services:

- `bot` for the main worker
- `admin` for the FastAPI control plane
- `chroma` for the vector store

The shared persistent volumes hold application data, docs, config, and Chroma state.

The image build itself is defined in [Dockerfile](../../Dockerfile), and the service wiring lives in [docker-compose.yml](../../docker-compose.yml).

## Offline Wheelhouse Build

To prebuild wheels for offline or installer-oriented flows, run:

```powershell
.\build_wheelhouse.ps1
```

This script:

- uses the active project virtual environment
- exports current constraints to `wheelhouse-constraints.txt`
- builds wheels into `LeisureLLM/wheels/`
- fails if any source archives are produced instead of wheels

## Windows Packaging

The Windows packaging flow has two layers:

1. Build the tray controller executable with PyInstaller.
2. Build the installer with Inno Setup.

See [windows-installer.md](windows-installer.md) for the exact packaging sequence.

The entry points are:

- [MagicKeyAssistant.spec](../../MagicKeyAssistant.spec)
- [installer.iss](../../installer.iss)
- [version_info.py](../../version_info.py)

## Build Files To Know

- [build_wheelhouse.ps1](../../build_wheelhouse.ps1) builds the offline wheelhouse bundle.
- [Dockerfile](../../Dockerfile) defines the container image.
- [docker-compose.yml](../../docker-compose.yml) defines the multi-service deployment.
- [MagicKeyAssistant.spec](../../MagicKeyAssistant.spec) builds the tray executable.
- [installer.iss](../../installer.iss) builds the Windows installer.

## Related Docs

- [INSTALLATION.md](../../INSTALLATION.md)
- [GET_STARTED.md](../../GET_STARTED.md)
- [windows-installer.md](windows-installer.md)