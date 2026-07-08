"""
Targeted tests for the alpha-patch changes:

1. Guide page receives onboarding state and reflects usable-state
2. Model router save path handles persistence failures gracefully
3. Dashboard "Configure AI Models" tile wording reflects state
4. Desktop icon paths are consistent across launch artifacts
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

os.environ["ADMIN_AUTH_DISABLED"] = "1"
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")


# ── Shared DB mock ───────────────────────────────────────────────────────────

def _mock_db():
    db = MagicMock()
    conn = MagicMock()

    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=(0,))
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.__aiter__ = MagicMock(return_value=iter([]))

    class _CursorCM:
        def __await__(self):
            yield
            return cursor
        async def __aenter__(self):
            return cursor
        async def __aexit__(self, *args):
            pass

    conn.execute = MagicMock(side_effect=lambda *a, **kw: _CursorCM())
    conn.executemany = AsyncMock()
    conn.commit = AsyncMock()

    class AcquireCM:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            pass

    db.acquire = lambda: AcquireCM()
    return db


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _suppress_llamacpp_health():
    with patch("services.llamacpp_manager.LlamaCppManager._health_check",
               return_value=False):
        yield


def _make_onboarding_state(**overrides):
    """Return a plausible onboarding state dict with sensible defaults."""
    base = {
        "app_installed": True,
        "auth_initialized": True,
        "org_profile_configured": True,
        "workflow_mode_configured": True,
        "ollama_detected": False,
        "provider_detected": True,
        "provider_connected": True,
        "model_discovered": True,
        "phase1_model_selected": True,
        "phase1_saved": True,
        "phase1_backend": "ollama",
        "phase1_model": "qwen3.5",
        "starter_content_seeded": True,
        "knowledge_docs_added": True,
        "first_question_asked": False,
        "first_action_captured": False,
        "first_decision_captured": False,
        "setup_complete": True,
        "registered_backends": ["ollama"],
        "cloud_keys": {"openai": False, "anthropic": False, "openrouter": False},
        "local_models": ["qwen3.5"],
        "counts": {"docs": 1, "inbox_threads": 0, "tasks": 0, "decisions": 0,
                   "gaps_total": 0, "gaps_resolved": 0, "feedback": 0,
                   "leads": 0, "meeting_notes": 0},
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def client():
    from admin import dependencies, server

    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()

    mock_mr = MagicMock()
    mock_mr.backends = {}
    mock_mr.pipeline = None
    mock_mr.clients = {}
    mock_mr.close = AsyncMock()
    dependencies._model_router = mock_mr

    mock_bot = MagicMock()
    mock_bot.db = _mock_db()
    dependencies._bot_instance = mock_bot

    with TestClient(server.app, raise_server_exceptions=False) as c:
        # Warmup templates
        with patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}), \
             patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=_make_onboarding_state())):
            c.get("/dashboard")
            c.get("/guide")
        yield c


# =============================================================================
#  Guide page — usable-state awareness
# =============================================================================

class TestGuideUsableState:
    """The guide page should reflect onboarding/usable state rather than
    behaving as a static page blind to whether the user has finished setup."""

    def test_guide_shows_ready_banner_when_phase1_saved(self, client):
        """When the default assistant is ready, the guide should stop prompting for setup."""
        state = _make_onboarding_state(phase1_saved=True)
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": True, "models": ["qwen3.5"]}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        # Should show the normal docs experience, not a setup prompt
        assert "Overview" in html
        assert "not active yet" not in html

    def test_guide_shows_action_banner_when_provider_not_detected(self, client):
        """When no provider is detected, the guide should show a coral banner."""
        state = _make_onboarding_state(
            provider_detected=False, provider_connected=False,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        assert "border-coral" in html
        assert "Local AI is not ready yet." in html

    def test_guide_shows_action_banner_when_provider_detected_not_connected(self, client):
        """When provider is detected but not connected, guide shows gold banner."""
        state = _make_onboarding_state(
            provider_detected=True, provider_connected=False,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": False, "models": []}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        assert "border-gold" in html
        assert "not active yet" in html

    def test_guide_shows_save_banner_when_provider_ready_phase1_unsaved(self, client):
        """Provider ready but default model not chosen yet — guide says so plainly."""
        state = _make_onboarding_state(
            provider_detected=True, provider_connected=True,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": True, "models": ["qwen3.5"]}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        assert "border-teal" in html
        assert "One more step" in html

    def test_guide_still_renders_documentation(self, client):
        """Regardless of state, the guide page should still render docs."""
        state = _make_onboarding_state(phase1_saved=True)
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": True, "models": ["qwen3.5"]}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        assert "guideBody" in html
        assert "guideToc" in html


# =============================================================================
#  Model router save — controlled failure handling
# =============================================================================

_CSRF_HEADERS = {"X-CSRF-Protection": "1"}


class TestModelRouterSavePath:
    """The save path should catch persistence errors and return a controlled
    JSON response instead of a raw 500."""

    def test_pipeline_save_success(self, client):
        """Normal save flow works and returns success."""
        from admin import dependencies
        from services.model_router import BackendConfig, BackendType, PipelineRole, RoleConfig

        # Set up a mock router with a backend registered
        mock_mr = MagicMock()
        mock_mr.backends = {"ollama": MagicMock(
            backend_type=BackendType.OLLAMA,
            available_models=["qwen3.5"],
            default_model="qwen3.5",
        )}
        mock_mr.pipeline = None
        mock_mr.configure_pipeline = MagicMock()
        dependencies._model_router = mock_mr

        with patch("admin.routers.model_router_api._save_pipeline_to_file"):
            resp = client.post("/api/v1/router/pipeline/role/initial",
                               headers=_CSRF_HEADERS, json={
                "enabled": True,
                "backend_name": "ollama",
                "model": "qwen3.5",
                "temperature": 0.3,
                "max_tokens": 4000,
            })
        data = resp.json()
        assert data["success"] is True
        assert data["role"] == "initial"

    def test_pipeline_save_disk_failure_returns_controlled_error(self, client):
        """When _save_pipeline_to_file raises (e.g. disk full), the response
        should be a controlled JSON error, not a generic 500."""
        from admin import dependencies
        from services.model_router import BackendType

        mock_mr = MagicMock()
        mock_mr.backends = {"ollama": MagicMock(
            backend_type=BackendType.OLLAMA,
            available_models=["qwen3.5"],
            default_model="qwen3.5",
        )}
        mock_mr.pipeline = None
        mock_mr.configure_pipeline = MagicMock()
        dependencies._model_router = mock_mr

        with patch("admin.routers.model_router_api._save_pipeline_to_file",
                   side_effect=PermissionError("Access denied")):
            resp = client.post("/api/v1/router/pipeline/role/initial",
                               headers=_CSRF_HEADERS, json={
                "enabled": True,
                "backend_name": "ollama",
                "model": "qwen3.5",
                "temperature": 0.3,
                "max_tokens": 4000,
            })
        assert resp.status_code == 200  # Controlled JSON, not 500
        data = resp.json()
        assert data["success"] is False
        assert "could not be saved to disk" in data["error"]
        assert "Access denied" in data["error"]

    def test_single_role_save_ignores_stale_unknown_roles(self, client):
        """Saving a valid role should still work when stale pipeline roles point
        at a backend that is no longer registered."""
        from admin import dependencies
        from services.model_router import BackendType, PipelineRole, RoleConfig

        mock_mr = MagicMock()
        mock_mr.backends = {"ollama": MagicMock(
            backend_type=BackendType.OLLAMA,
            available_models=["qwen3.5"],
            default_model="qwen3.5",
        )}
        mock_mr.pipeline = MagicMock(
            roles={
                PipelineRole.CRITIQUE: RoleConfig(
                    role=PipelineRole.CRITIQUE,
                    backend_name="missing-backend",
                    model="ghost-model",
                    temperature=0.2,
                )
            }
        )
        mock_mr.configure_pipeline = MagicMock()
        dependencies._model_router = mock_mr

        with patch("admin.routers.model_router_api._save_pipeline_to_file"):
            resp = client.post("/api/v1/router/pipeline/role/initial",
                               headers=_CSRF_HEADERS, json={
                "enabled": True,
                "backend_name": "ollama",
                "model": "qwen3.5",
                "temperature": 0.3,
                "max_tokens": 4000,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        saved_pipeline = mock_mr.configure_pipeline.call_args.args[0]
        saved_roles = {role.value: cfg.backend_name for role, cfg in saved_pipeline.roles.items()}
        assert saved_roles == {"initial": "ollama"}

    def test_full_pipeline_save_disk_failure(self, client):
        """Full pipeline POST also handles disk failures gracefully."""
        from admin import dependencies
        from services.model_router import BackendType

        mock_mr = MagicMock()
        mock_mr.backends = {"ollama": MagicMock(
            backend_type=BackendType.OLLAMA,
            available_models=["qwen3.5"],
            default_model="qwen3.5",
        )}
        mock_mr.pipeline = None
        mock_mr.configure_pipeline = MagicMock()
        dependencies._model_router = mock_mr

        with patch("admin.routers.model_router_api._save_pipeline_to_file",
                   side_effect=OSError("No space left on device")):
            resp = client.post("/api/v1/router/pipeline",
                               headers=_CSRF_HEADERS, json={
                "initial": {
                    "enabled": True,
                    "backend_name": "ollama",
                    "model": "qwen3.5",
                    "temperature": 0.3,
                    "max_tokens": 4000,
                }
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "could not be saved to disk" in data["error"]

    def test_provider_not_connected_still_works(self, client):
        """The provider-not-connected error is preserved (no regression)."""
        from admin import dependencies

        mock_mr = MagicMock()
        mock_mr.backends = {}
        mock_mr.pipeline = None
        dependencies._model_router = mock_mr

        with patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}):
            resp = client.post("/api/v1/router/pipeline/role/initial",
                               headers=_CSRF_HEADERS, json={
                "enabled": True,
                "backend_name": "ollama",
                "model": "qwen3.5",
            })
        data = resp.json()
        assert data["success"] is False
        assert "not connected" in data["error"]


# =============================================================================
#  Desktop icon consistency
# =============================================================================

class TestDesktopIconPaths:
    """All launch-path artifacts should reference the same .ico file."""

    def test_installer_iss_icon_consistent(self):
        iss_path = ROOT_DIR / "installer.iss"
        if not iss_path.exists():
            pytest.skip("installer.iss not present")
        content = iss_path.read_text(encoding="utf-8")
        ico_refs = re.findall(r'[\w-]+\.ico', content)
        unique = set(ico_refs)
        assert len(unique) == 1, f"installer.iss references multiple icons: {unique}"
        assert "MTRMK-Assistant-Icon.ico" in unique

    def test_spec_icon_consistent(self):
        spec_path = ROOT_DIR / "MagicKeyAssistant.spec"
        if not spec_path.exists():
            pytest.skip("MagicKeyAssistant.spec not present")
        content = spec_path.read_text(encoding="utf-8")
        ico_refs = re.findall(r'[\w-]+\.ico', content)
        unique = set(ico_refs)
        assert len(unique) == 1, f"spec references multiple icons: {unique}"
        assert "MTRMK-Assistant-Icon.ico" in unique

    def test_tray_icon_consistent(self):
        tray_path = ROOT_DIR / "tray.py"
        if not tray_path.exists():
            pytest.skip("tray.py not present")
        content = tray_path.read_text(encoding="utf-8")
        ico_refs = re.findall(r'[\w-]+\.ico', content)
        unique = set(ico_refs)
        assert len(unique) == 1, f"tray.py references multiple icons: {unique}"
        assert "MTRMK-Assistant-Icon.ico" in unique

    def test_referenced_icon_exists(self):
        icon_path = ROOT_DIR / "MTRMK-Assistant-Icon.ico"
        assert icon_path.exists(), "MTRMK-Assistant-Icon.ico not found at project root"
        assert icon_path.stat().st_size > 0, "Icon file is empty"


# =============================================================================
#  Model setup guidance — wording accuracy
# =============================================================================

class TestModelSetupGuidance:
    """_build_model_setup_guidance should return distinct wording for each
    semantic state, never conflating 'detected' with 'configured'."""

    def test_phase1_saved_hides_banner(self):
        from admin.routers.settings import _build_model_setup_guidance
        state = _make_onboarding_state(phase1_saved=True)
        guidance = _build_model_setup_guidance(state)
        assert guidance["show_banner"] is False
        assert guidance["state_key"] == "phase1_saved"

    def test_no_provider_shows_coral(self):
        from admin.routers.settings import _build_model_setup_guidance
        state = _make_onboarding_state(
            provider_detected=False, provider_connected=False, phase1_saved=False,
        )
        guidance = _build_model_setup_guidance(state)
        assert guidance["show_banner"] is True
        assert guidance["state_key"] == "provider_not_detected"
        assert guidance["tone"] == "coral"
        assert "local ai" in guidance["title"].lower()

    def test_provider_detected_not_connected_shows_gold(self):
        from admin.routers.settings import _build_model_setup_guidance
        state = _make_onboarding_state(
            provider_detected=True, provider_connected=False, phase1_saved=False,
        )
        guidance = _build_model_setup_guidance(state)
        assert guidance["show_banner"] is True
        assert guidance["state_key"] == "provider_not_connected"
        assert guidance["tone"] == "gold"
        assert "not active yet" in guidance["detail"].lower() or "not active yet" in guidance["title"].lower()

    def test_provider_ready_phase1_unsaved_shows_teal(self):
        from admin.routers.settings import _build_model_setup_guidance
        state = _make_onboarding_state(
            provider_detected=True, provider_connected=True, phase1_saved=False,
        )
        guidance = _build_model_setup_guidance(state)
        assert guidance["show_banner"] is True
        assert guidance["state_key"] == "provider_ready_phase1_not_saved"
        assert guidance["tone"] == "teal"
        assert "default assistant" in guidance["title"].lower() or "default" in guidance["detail"].lower()

    def test_all_states_have_distinct_keys(self):
        """No two semantic states should produce the same state_key."""
        from admin.routers.settings import _build_model_setup_guidance
        states = [
            _make_onboarding_state(phase1_saved=True),
            _make_onboarding_state(provider_detected=False, provider_connected=False, phase1_saved=False),
            _make_onboarding_state(provider_detected=True, provider_connected=False, phase1_saved=False),
            _make_onboarding_state(provider_detected=True, provider_connected=True, phase1_saved=False),
        ]
        keys = [_build_model_setup_guidance(s)["state_key"] for s in states]
        assert len(set(keys)) == 4, f"Duplicate state_keys: {keys}"

    def test_all_states_have_router_tile_title(self):
        """Every guidance state should include a router_tile_title."""
        from admin.routers.settings import _build_model_setup_guidance
        states = [
            _make_onboarding_state(phase1_saved=True),
            _make_onboarding_state(provider_detected=False, provider_connected=False, phase1_saved=False),
            _make_onboarding_state(provider_detected=True, provider_connected=False, phase1_saved=False),
            _make_onboarding_state(provider_detected=True, provider_connected=True, phase1_saved=False),
        ]
        for s in states:
            guidance = _build_model_setup_guidance(s)
            assert "router_tile_title" in guidance, f"Missing router_tile_title for state_key={guidance['state_key']}"
            assert guidance["router_tile_title"], f"Empty router_tile_title for state_key={guidance['state_key']}"

    def test_router_tile_titles_are_state_specific(self):
        """provider_not_detected and provider_not_connected should NOT say 'Save Phase 1'."""
        from admin.routers.settings import _build_model_setup_guidance
        no_provider = _build_model_setup_guidance(
            _make_onboarding_state(provider_detected=False, provider_connected=False, phase1_saved=False)
        )
        not_connected = _build_model_setup_guidance(
            _make_onboarding_state(provider_detected=True, provider_connected=False, phase1_saved=False)
        )
        assert "save" not in no_provider["router_tile_title"].lower()
        assert "save" not in not_connected["router_tile_title"].lower()


# =============================================================================
#  Guide page — default doc selection
# =============================================================================

class TestGuideDefaultDocSelection:
    """The guide page should default to getting-started for users who
    haven't finished Phase 1, and overview for already-usable users."""

    def test_guide_defaults_to_getting_started_when_phase1_not_saved(self, client):
        """First-run users (Phase 1 not saved) land on Getting Started."""
        state = _make_onboarding_state(
            provider_detected=True, provider_connected=True,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": True, "models": ["qwen3.5"]}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        assert "Getting Started" in html

    def test_guide_defaults_to_overview_when_phase1_saved(self, client):
        """Already-usable users land on Overview by default."""
        state = _make_onboarding_state(phase1_saved=True)
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": True, "models": ["qwen3.5"]}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        assert "Overview" in html

    def test_guide_explicit_doc_overrides_default(self, client):
        """An explicit ?doc= param should override the smart default."""
        state = _make_onboarding_state(phase1_saved=False, provider_detected=False,
                                        provider_connected=False)
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}):
            resp = client.get("/guide?doc=architecture")
        assert resp.status_code == 200
        html = resp.text
        assert "Architecture" in html

    def test_guide_defaults_to_getting_started_when_no_provider(self, client):
        """provider_not_detected state also gets Getting Started."""
        state = _make_onboarding_state(
            provider_detected=False, provider_connected=False,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}):
            resp = client.get("/guide")
        assert resp.status_code == 200
        html = resp.text
        assert "Getting Started" in html


# =============================================================================
#  Dashboard tile — server-rendered title matches state
# =============================================================================

class TestDashboardTileServerRender:
    """The router tile title should be correct on first paint,
    not relying on JS to fix a generic default."""

    def test_dashboard_tile_title_shows_connect_when_no_provider(self, client):
        state = _make_onboarding_state(
            provider_detected=False, provider_connected=False,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}):
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        html = resp.text
        assert "Set Up AI" in html

    def test_dashboard_tile_title_shows_start_when_not_connected(self, client):
        state = _make_onboarding_state(
            provider_detected=True, provider_connected=False,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": False, "models": []}):
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        html = resp.text
        assert "Start Local AI" in html

    def test_dashboard_tile_title_shows_save_when_provider_ready(self, client):
        state = _make_onboarding_state(
            provider_detected=True, provider_connected=True,
            phase1_saved=False, phase1_backend=None, phase1_model=None,
        )
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value=state)), \
             patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": True, "running": True, "models": ["qwen3.5"]}):
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        html = resp.text
        assert "Choose Default AI" in html


class TestOnboardingSkipBehavior:
    def test_skip_does_not_mark_setup_complete(self, client, tmp_path):
        from admin.routers import settings

        complete_flag = tmp_path / ".setup_complete"

        with patch.object(settings, "CONFIG_DIR", tmp_path), \
             patch.object(settings, "_SETUP_COMPLETE_FLAG", complete_flag), \
             patch("admin.routers.settings._seed_workspace_once", AsyncMock(return_value={"success": True})):
            resp = client.post("/api/v1/onboarding/skip", headers={"X-CSRF-Protection": "1"})

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert not complete_flag.exists()

    def test_setup_complete_does_not_reseed_when_workspace_already_seeded(self, client, tmp_path):
        from admin.routers import settings

        complete_flag = tmp_path / ".setup_complete"
        build_state = AsyncMock(side_effect=[
            _make_onboarding_state(starter_content_seeded=False, setup_complete=False, phase1_saved=False),
            _make_onboarding_state(starter_content_seeded=True, setup_complete=True, phase1_saved=False),
        ])
        seed_once = AsyncMock(return_value={"skipped": False, "created": 3})

        with patch.object(settings, "CONFIG_DIR", tmp_path), \
             patch.object(settings, "_SETUP_COMPLETE_FLAG", complete_flag), \
             patch.object(settings, "_build_onboarding_state", build_state), \
             patch.object(settings, "_seed_workspace_once", seed_once), \
             patch("services.pipeline_presets.auto_configure_pipeline", AsyncMock(return_value={"configured": True})):
            first = client.post("/api/v1/setup/complete", headers=_CSRF_HEADERS, json={
                "seed_demo_workspace": True,
                "goal": "demo_workspace",
                "privacy_mode": "local_only",
                "operation_mode": "solo",
            })
            second = client.post("/api/v1/setup/complete", headers=_CSRF_HEADERS, json={
                "seed_demo_workspace": True,
                "goal": "demo_workspace",
                "privacy_mode": "local_only",
                "operation_mode": "solo",
            })

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["success"] is True
        assert second.json()["success"] is True
        assert first.json()["seed"]["skipped"] is False
        assert second.json()["seed"]["reason"] == "already_seeded"
        assert seed_once.await_count == 1


class TestOnboardingReadinessGating:
    def test_phase1_saved_requires_backend_to_be_available(self):
        from admin.routers import settings
        from services.model_router import PipelineRole, RoleConfig

        mock_mr = MagicMock()
        mock_mr.backends = {}
        mock_mr.pipeline = MagicMock(
            roles={
                PipelineRole.INITIAL: RoleConfig(
                    role=PipelineRole.INITIAL,
                    backend_name="ollama",
                    model="qwen3.5",
                    temperature=0.4,
                )
            }
        )

        secrets = MagicMock()
        secrets.get.return_value = None

        with patch.object(settings, "get_model_router", return_value=mock_mr), \
             patch("admin.performance.get_cached_ollama_status", return_value={"installed": True, "running": False, "models": []}), \
             patch("services.secrets.get_secrets_manager", return_value=secrets), \
             patch("core.seed_workspace.is_seeded", return_value=False), \
             patch.object(settings, "_count_docs_in_workspace", return_value=0), \
             patch.object(settings, "_count_rows", AsyncMock(return_value=0)):
            state = asyncio.run(settings._build_onboarding_state())

        assert state["phase1_backend"] == "ollama"
        assert state["phase1_backend_available"] is False
        assert state["phase1_model_available"] is False
        assert state["phase1_saved"] is False

    def test_setup_completion_does_not_finish_without_default_assistant(self):
        from admin.routers import settings

        state = _make_onboarding_state(
            phase1_saved=False,
            knowledge_docs_added=True,
            first_question_asked=True,
            first_action_captured=True,
            first_decision_captured=True,
            starter_content_seeded=True,
        )

        with patch.object(settings, "_build_onboarding_state", AsyncMock(return_value=state)), \
             patch.object(settings, "_build_model_setup_guidance", return_value={"show_banner": True}):
            result = asyncio.run(settings.build_setup_completion())

        assert result["completion_pct"] >= 80
        assert result["is_complete"] is False


class TestDashboardSummaryResilience:
    def test_dashboard_summary_returns_partial_payload_when_subsystems_fail(self, client):
        with patch("admin.routers.artifacts.api_analytics_overview", AsyncMock(side_effect=RuntimeError("analytics exploded"))), \
             patch("admin.routers.settings.build_setup_completion", AsyncMock(side_effect=RuntimeError("setup exploded"))), \
             patch("admin.routers.knowledge.get_cached_knowledge_stats", side_effect=RuntimeError("knowledge exploded")):
            resp = client.get("/api/v1/dashboard/summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["analytics"] == {}
        assert data["knowledge"] == {}
        assert data["setup"]["success"] is False
        assert data["setup"]["error"] == "setup_summary_unavailable"


class TestOnboardingCompletionSemantics:
    def test_onboarding_status_not_complete_until_assistant_ready(self, client):
        state = _make_onboarding_state(
            setup_complete=True,
            phase1_saved=False,
            provider_detected=True,
            provider_connected=True,
        )

        with patch("admin.routers.settings._build_onboarding_state", AsyncMock(return_value=state)), \
             patch("core.onboarding_sprint.is_sprint_complete", return_value=True):
            resp = client.get("/api/v1/onboarding/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["setup_complete"] is True
        assert data["assistant_ready"] is False
        assert data["phase"] != "complete"


class TestSeedAndInboxResilience:
    def test_seed_route_is_idempotent_when_workspace_already_seeded(self, client):
        with patch("core.seed_workspace.is_seeded", return_value=True), \
             patch("core.seed_workspace.seed_workspace", AsyncMock(side_effect=AssertionError("should not reseed"))):
            resp = client.post("/api/v1/seed", headers=_CSRF_HEADERS)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["created"]["skipped"] is True
        assert data["created"]["reason"] == "already_seeded"

    def test_inbox_threads_fails_soft_when_db_listing_breaks(self, client):
        from admin import dependencies

        broken_db = MagicMock()

        class BrokenAcquire:
            async def __aenter__(self):
                raise RuntimeError("missing inbox tables")

            async def __aexit__(self, *args):
                return None

        broken_db.acquire = lambda: BrokenAcquire()
        dependencies._bot_instance.db = broken_db

        try:
            resp = client.get("/api/v1/inbox/threads")
        finally:
            dependencies._bot_instance.db = _mock_db()

        assert resp.status_code == 200
        data = resp.json()
        assert data["threads"] == []
        assert data["unread_count"] == 0
        assert data["success"] is False
        assert data["error"] == "threads_unavailable"
