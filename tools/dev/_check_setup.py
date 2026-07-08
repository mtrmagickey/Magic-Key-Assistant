"""Diagnose setup structural test failures."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["ADMIN_AUTH_DISABLED"] = "1"
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")

ROOT_DIR = Path(__file__).resolve().parents[2]
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
OUTPUT_DIR = ROOT_DIR / "Output" / "scratch"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RESULT_PATH = OUTPUT_DIR / "_check_result.txt"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

from admin import dependencies, server
from fastapi.testclient import TestClient

server.app.router.on_startup.clear()
server.app.router.on_shutdown.clear()
mock_mr = MagicMock()
mock_mr.backends = {}
mock_mr.pipeline = None
mock_mr.clients = {}
mock_mr.close = AsyncMock()
dependencies._model_router = mock_mr
mock_bot = MagicMock()
mock_bot.db = MagicMock()
dependencies._bot_instance = mock_bot

with TestClient(server.app, raise_server_exceptions=False) as client:
    with patch(
        "admin.routers.settings._build_onboarding_state",
        AsyncMock(
            return_value={
                "setup_complete": False,
                "phase1_saved": False,
                "provider_detected": False,
                "provider_connected": False,
            }
        ),
    ):
        resp = client.get("/setup")
    html = resp.text
    checks = {
        "tagline_updated": "Private AI Operations Assistant" in html,
        "old_tagline_gone": "Local Ops for Tiny Teams" not in html,
        "ollama_status": 'id="ollamaStatus"' in html,
        "install_button": "setupInstallOllama" in html,
        "step_nav": "goToStep" in html,
        "device_scan": "scanDevice" in html or "deviceReport" in html,
        "lucide_icons": "data-lucide" in html,
    }
    for name, val in checks.items():
        print(f"  {name}: {'OK' if val else 'FAIL'}")
    RESULT_PATH.write_text(
        "\n".join(f"{k}: {'OK' if v else 'FAIL'}" for k, v in checks.items()),
        encoding="utf-8",
    )