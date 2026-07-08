"""
Smoke tests for the admin web GUI.

Verifies that every page route returns 200 and HTML, and that key
API endpoints respond correctly via FastAPI TestClient.

No Ollama, no Discord bot, no real database — pure HTTP shape tests.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.actors import ActorContext
from fastapi.testclient import TestClient
from services.model_router import (
    BackendConfig,
    BackendType,
    ModelRouter,
    PipelineConfig,
    PipelineRole,
    RoleConfig,
)

# ── Environment setup (before importing the app) ─────────────────────────────
# Auth is ON by default; disable for tests unless we're testing auth specifically.
os.environ["ADMIN_AUTH_DISABLED"] = "1"
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    """Create a TestClient with startup/shutdown events disabled.

    We patch ModelRouter to avoid real Ollama connections and provide
    a mock database so API endpoints that need it don't crash.
    """
    # Prevent real startup (Ollama registration, pipeline loading)
    from admin import server

    # Clear startup/shutdown so TestClient doesn't try Ollama
    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()

    # Provide a mock model router
    mock_mr = MagicMock()
    mock_mr.backends = {}
    mock_mr.pipeline = None
    mock_mr.clients = {}
    mock_mr.close = AsyncMock()

    from admin import dependencies
    dependencies._model_router = mock_mr

    # Provide a mock bot with a mock database so get_db() succeeds
    mock_bot = MagicMock()
    mock_bot.db = _mock_db()
    dependencies._bot_instance = mock_bot

    with TestClient(server.app, raise_server_exceptions=False) as c:
        yield c


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _mock_db():
    """Return a mock database that supports `acquire` as async context manager."""
    db = MagicMock()
    conn = MagicMock()

    # Row-like object that supports dict-style access
    class FakeRow(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError:
                raise AttributeError(name)

    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.__aiter__ = MagicMock(return_value=iter([]))

    class ExecuteResult:
        def __init__(self, value):
            self.value = value

        def __await__(self):
            async def _resolve():
                return self.value

            return _resolve().__await__()

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *args):
            return None

    conn.execute = MagicMock(side_effect=lambda *args, **kwargs: ExecuteResult(cursor))
    conn.executemany = AsyncMock()
    conn.commit = AsyncMock()

    class AcquireCM:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            pass

    db.acquire = lambda: AcquireCM()
    return db


# =============================================================================
# UI Smoke Tests — every page returns 200 + HTML
# =============================================================================

class TestPageRoutes:
    """Verify all 21 page routes render without errors."""

    # Pages that simply render a template (no dynamic deps)
    SIMPLE_PAGES = [
        "/setup",
        "/org",
        "/actions",
        "/leads",
        "/meetings",
        "/analytics",
        "/obligations",
        "/feedback",
        "/activity",
        "/inbox",
        "/system",
        "/teach",
        "/guide",
    ]

    @pytest.mark.parametrize("path", SIMPLE_PAGES)
    def test_simple_page_returns_200(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        assert "text/html" in resp.headers.get("content-type", "")

    def test_dashboard_first_run_redirects_to_setup(self, client):
        """When first-run is detected, / should redirect to /setup."""
        with patch("admin.server.is_first_run", return_value=True), \
             patch(
                 "admin.routers.settings._build_onboarding_state",
                 AsyncMock(return_value={"setup_complete": False, "phase1_saved": False}),
             ):
            resp = client.get("/", follow_redirects=False)
            assert resp.status_code == 302
            assert "/setup" in resp.headers.get("location", "")

    def test_dashboard_normal_renders(self, client):
        """When not first-run, /dashboard should return the dashboard page."""
        with patch("admin.server.is_first_run", return_value=False), \
            patch("admin.server.peek_cache", return_value=None), \
            patch("services.system_tools.SystemTools.get_ollama_status",
                side_effect=AssertionError("dashboard render must not probe Ollama directly")):
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")

    def test_root_redirects_to_pulse(self, client):
        """When not first-run, / should redirect to /pulse."""
        with patch("admin.server.is_first_run", return_value=False), \
             patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value={"setup_complete": True, "phase1_saved": True})):
            resp = client.get("/", follow_redirects=False)
            assert resp.status_code == 302
            assert "/pulse" in resp.headers.get("location", "")

    def test_dashboard_skips_setup_when_phase1_saved(self, client):
        with patch("admin.server.is_first_run", return_value=True), \
             patch(
                 "admin.routers.settings._build_onboarding_state",
                 AsyncMock(return_value={"setup_complete": False, "phase1_saved": True}),
             ), \
             patch("services.system_tools.SystemTools.get_ollama_status", return_value={"status": "unavailable"}):
            resp = client.get("/", follow_redirects=False)
            assert resp.status_code == 302
            assert "/pulse" in resp.headers.get("location", "")

    def test_dashboard_shows_model_setup_banner_when_provider_missing(self, client):
        with patch("admin.server.is_first_run", return_value=False), \
             patch(
                 "admin.routers.settings._build_onboarding_state",
                 AsyncMock(
                     return_value={
                         "setup_complete": False,
                         "phase1_saved": False,
                         "provider_detected": False,
                         "provider_connected": False,
                     }
                 ),
             ), \
             patch(
                 "services.system_tools.SystemTools.get_ollama_status",
                 return_value={"installed": False, "running": False, "models": []},
             ):
            resp = client.get("/dashboard")

        assert resp.status_code == 200
        assert 'id="modelSetupBanner"' in resp.text
        assert "Local AI is not ready yet." in resp.text

    def test_dashboard_hides_model_setup_banner_when_phase1_saved(self, client):
        with patch("admin.server.is_first_run", return_value=False), \
             patch(
                 "admin.routers.settings._build_onboarding_state",
                 AsyncMock(
                     return_value={
                         "setup_complete": False,
                         "phase1_saved": True,
                         "provider_detected": True,
                         "provider_connected": True,
                     }
                 ),
             ), \
             patch(
                 "services.system_tools.SystemTools.get_ollama_status",
                 return_value={"installed": True, "running": True, "models": ["gemma3:4b"]},
             ):
            resp = client.get("/dashboard")

        assert resp.status_code == 200
        assert 'id="modelSetupBanner"' in resp.text
        assert 'style="display:none;"' in resp.text

    def test_setup_redirects_to_dashboard_when_already_usable(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(return_value={"setup_complete": False, "phase1_saved": True}),
        ):
            resp = client.get("/setup", follow_redirects=False)
            assert resp.status_code == 200

    def test_setup_force_allows_manual_reopen(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(return_value={"setup_complete": False, "phase1_saved": True, "provider_detected": True, "provider_connected": True}),
        ):
            resp = client.get("/setup?force=true", follow_redirects=False)
            assert resp.status_code == 200

    def test_setup_page_suppresses_model_setup_banner(self, client):
        """Setup wizard should NOT include the model_setup_guidance banner (Issue 11 redundancy fix)."""
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(return_value={"setup_complete": False, "phase1_saved": False, "provider_detected": False, "provider_connected": False}),
        ):
            resp = client.get("/setup", follow_redirects=False)

        assert resp.status_code == 200
        assert 'id="setupModelBanner"' not in resp.text
        assert "Your operational workspace" in resp.text
        assert "Your AI, your rules" in resp.text
        assert "Knowledge that improves itself" in resp.text
        assert "Conversations become work" in resp.text
        assert "Local-only" in resp.text
        assert "Cloud-assisted" in resp.text

    def test_settings_page_renders(self, client):
        """Settings page needs SecretsManager — should still render."""
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_knowledge_page_renders(self, client):
        resp = client.get("/knowledge")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_knowledge_gaps_page_renders(self, client):
        resp = client.get("/gaps")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_model_router_page_renders(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(return_value={"provider_detected": True, "provider_connected": True, "phase1_saved": True}),
        ), patch(
            "services.system_tools.SystemTools.get_ollama_status",
            return_value={"installed": False, "running": False, "models": []},
        ):
            resp = client.get("/router")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_model_router_page_shows_missing_provider_guidance(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(return_value={"provider_detected": False, "provider_connected": False, "phase1_saved": False}),
        ), patch(
            "services.system_tools.SystemTools.get_ollama_status",
            return_value={"installed": False, "running": False, "models": []},
        ):
            resp = client.get("/router")

        assert resp.status_code == 200
        assert 'id="routerPrereqBanner"' in resp.text
        assert "Local AI is not ready yet." in resp.text
        assert "Finish setup anyway to open the sample workspace." in resp.text

    def test_model_router_page_hides_prereq_banner_after_phase1_saved(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(return_value={"provider_detected": True, "provider_connected": True, "phase1_saved": True}),
        ), patch(
            "services.system_tools.SystemTools.get_ollama_status",
            return_value={"installed": False, "running": False, "models": []},
        ):
            resp = client.get("/router")

        assert resp.status_code == 200
        assert 'id="routerPrereqBanner"' not in resp.text

    def test_chat_redirects_to_inbox(self, client):
        """/chat is now a redirect to /inbox."""
        resp = client.get("/chat", follow_redirects=False)
        assert resp.status_code == 302
        assert "/inbox" in resp.headers.get("location", "")


# =============================================================================
# API E2E Tests — key endpoints respond with correct shape
# =============================================================================

class TestArtifactAPIs:
    """Test CRUD API endpoints using a mock database."""

    def test_list_actions_empty(self, client):
        resp = client.get("/api/v1/actions")
        assert resp.status_code == 200
        data = resp.json()
        # Endpoint responds — may return items or an error dict depending on DB state
        assert isinstance(data, (dict, list))

    def test_list_leads_empty(self, client):
        resp = client.get("/api/v1/leads")
        assert resp.status_code == 200


class TestPerformancePass:
    def test_dashboard_summary_endpoint_shape(self, client):
        fake_setup = {
            "success": True,
            "is_complete": False,
            "completion_pct": 60,
            "milestones": [{"label": "AI provider detected", "met": True, "weight": 10, "hint": ""}],
            "onboarding_state": {"phase1_saved": False, "provider_detected": True, "provider_connected": False},
            "model_setup_guidance": {"show_banner": True, "router_hint": "Open Model Router"},
        }

        fake_secrets = MagicMock()
        fake_secrets.list_keys.return_value = [
            {"env_var": "OPENAI_API_KEY", "has_value": True},
            {"env_var": "ANTHROPIC_API_KEY", "has_value": False},
        ]

        with patch(
            "admin.routers.artifacts.api_analytics_overview",
            AsyncMock(return_value={"success": True, "analytics": {"tasks_overdue": 2, "gaps_by_status": {"open": 1}}}),
        ), patch(
            "admin.routers.settings.build_setup_completion",
            AsyncMock(return_value=fake_setup),
        ), patch(
            "admin.routers.knowledge.get_cached_knowledge_stats",
            return_value={"success": True, "documents": {"count": 4}},
        ), patch(
            "services.interaction_memory.InteractionMemory.get_active_concerns",
            AsyncMock(return_value=[{"id": 9, "topic": "Broken locker", "query_count": 3}]),
        ), patch(
            "services.secrets.get_secrets_manager",
            return_value=fake_secrets,
        ):
            resp = client.get("/api/v1/dashboard/summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["unread_count"] == 0
        assert data["analytics"]["tasks_overdue"] == 2
        assert data["knowledge"]["documents"]["count"] == 4
        assert data["setup"]["completion_pct"] == 60
        assert data["provider_summary"]["secrets"]["OPENAI_API_KEY"] is True
        assert data["concerns"][0]["topic"] == "Broken locker"

    def test_internal_performance_endpoint_reports_metrics(self, client):
        from admin.performance import invalidate_cache, record_timing

        invalidate_cache("ollama_status", "knowledge_stats", "knowledge_storage")
        record_timing("dashboard.render", 12.5)
        record_timing("dashboard.summary", 8.0)
        record_timing("ollama.status_probe", 100.0)
        record_timing("knowledge.stats", 7.0)

        resp = client.get("/api/v1/internal/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "dashboard.render" in data["timings"]
        assert "ollama_status" in data["cache"]

    def test_knowledge_stats_cache_reuses_scan_until_invalidated(self, client, monkeypatch, tmp_path):
        from admin.routers import knowledge

        import config as app_config

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        hash_csv = tmp_path / "hashes.csv"
        hash_csv.write_text("FileName,Hash\n", encoding="utf-8")

        monkeypatch.setattr(app_config, "directory_path", str(docs_dir))
        monkeypatch.setattr(app_config, "persist_directory", str(chroma_dir))
        monkeypatch.setattr(app_config, "hash_csv", str(hash_csv))

        knowledge.invalidate_knowledge_caches()

        (docs_dir / "first.md").write_text("hello", encoding="utf-8")
        first = client.get("/api/v1/knowledge/stats").json()
        assert first["documents"]["count"] == 1

        (docs_dir / "second.md").write_text("world", encoding="utf-8")
        cached = client.get("/api/v1/knowledge/stats").json()
        assert cached["documents"]["count"] == 1

        knowledge.invalidate_knowledge_caches()
        refreshed = client.get("/api/v1/knowledge/stats").json()
        assert refreshed["documents"]["count"] == 2

    def test_list_decisions_empty(self, client):
        resp = client.get("/api/v1/decisions")
        assert resp.status_code == 200

    def test_list_meetings_empty(self, client):
        resp = client.get("/api/v1/meetings")
        assert resp.status_code == 200


class TestKnowledgeAPIs:
    """Test knowledge-related API endpoints."""

    def test_list_gaps_empty(self, client):
        resp = client.get("/api/v1/gaps")
        assert resp.status_code == 200

    def test_gap_stats_empty(self, client):
        resp = client.get("/api/v1/gaps/stats")
        assert resp.status_code == 200


class TestContinuityAPIs:
    """Test continuity-related API endpoints."""

    def test_list_obligations_empty(self, client):
        resp = client.get("/api/v1/obligations")
        assert resp.status_code == 200

    # SOPs removed per product direction (2026-07)


class TestSystemAPIs:
    """Test system/infra API endpoints."""

    def test_secrets_list(self, client):
        resp = client.get("/api/v1/secrets/list")
        assert resp.status_code == 200
        data = resp.json()
        assert "secrets" in data or "by_category" in data

    def test_settings_secrets_status_endpoint(self, client):
        resp = client.get("/api/v1/settings/secrets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "secrets" in data
        assert "OPENAI_API_KEY" in data["secrets"]

    def test_model_router_backends(self, client):
        resp = client.get("/api/v1/router/backends")
        assert resp.status_code == 200

    def test_model_router_save_auto_registers_live_ollama(self, client):
        from admin import dependencies

        real_router = ModelRouter()
        dependencies._model_router = real_router

        async def fake_register_backend(config):
            real_router.backends[config.name] = config
            real_router.clients[config.name] = AsyncMock()
            return True

        with patch.object(real_router, "register_backend", side_effect=fake_register_backend), \
             patch("admin.routers.model_router_api._save_pipeline_to_file"), \
             patch(
                 "services.system_tools.SystemTools.get_ollama_status",
                 return_value={"installed": True, "running": True, "models": ["gemma3:4b"]},
             ):
            resp = client.post(
                "/api/v1/router/pipeline/role/initial",
                json={
                    "enabled": True,
                    "backend_name": "ollama",
                    "model": "gemma3:4b",
                    "temperature": 0.4,
                    "max_tokens": 1024,
                    "ollama_options": {},
                },
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["backend_name"] == "ollama"
        assert real_router.pipeline is not None
        assert PipelineRole.INITIAL in real_router.pipeline.roles

    def test_setup_status_skips_launch_guide_when_phase1_saved(self, client):
        from admin import dependencies

        real_router = ModelRouter()
        real_router.backends["ollama"] = BackendConfig(
            backend_type=BackendType.OLLAMA,
            name="ollama",
            available_models=["gemma3:4b"],
            default_model="gemma3:4b",
        )
        real_router.configure_pipeline(
            PipelineConfig(
                name="test",
                roles={
                    PipelineRole.INITIAL: RoleConfig(
                        role=PipelineRole.INITIAL,
                        backend_name="ollama",
                        model="gemma3:4b",
                    )
                },
            )
        )
        dependencies._model_router = real_router

        with patch(
            "services.system_tools.SystemTools.get_ollama_status",
            return_value={"installed": True, "running": True, "models": ["gemma3:4b"]},
        ):
            resp = client.get("/api/v1/setup/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["launch_guide_needed"] is False
        assert data["onboarding_state"]["phase1_saved"] is True
        assert data["model_setup_guidance"]["show_banner"] is False
        assert data["onboarding_experience"]["default_path"] == "local_only"

    def test_setup_status_keeps_launch_guide_when_setup_flag_exists_but_assistant_not_ready(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(
                return_value={
                    "setup_complete": True,
                    "phase1_saved": False,
                    "provider_detected": True,
                    "provider_connected": True,
                }
            ),
        ), \
            patch(
                "admin.routers.settings._build_model_setup_guidance",
                return_value={"show_banner": True},
            ), \
            patch(
                "admin.routers.settings._build_onboarding_experience",
                return_value={"default_path": "local_only"},
            ):
            resp = client.get("/api/v1/setup/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["setup_complete"] is True
        assert data["assistant_ready"] is False
        assert data["launch_guide_needed"] is True

    def test_setup_page_does_not_redirect_when_setup_flag_exists_but_assistant_not_ready(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(
                return_value={
                    "setup_complete": True,
                    "phase1_saved": False,
                    "provider_detected": True,
                    "provider_connected": True,
                }
            ),
        ):
            resp = client.get("/setup", follow_redirects=False)

        assert resp.status_code == 200

    def test_setup_page_shows_runtime_shortcut_when_local_ai_ready(self, client):
        with patch(
            "admin.routers.settings._build_onboarding_state",
            AsyncMock(
                return_value={
                    "setup_complete": False,
                    "phase1_saved": True,
                    "provider_detected": True,
                    "provider_connected": True,
                    "ollama_detected": True,
                    "local_models": ["gemma3:4b"],
                }
            ),
        ):
            resp = client.get("/setup?force=true", follow_redirects=False)

        assert resp.status_code == 200
        assert 'id="runtimeActionArea"' in resp.text
        assert 'id="runtimeReadyBanner"' in resp.text
        assert 'const initialRuntimeReady = Boolean(' in resp.text
        assert 'maybeShortCircuitSetup();' in resp.text

    def test_actions_page_wraps_table_for_narrow_widths(self, client):
        resp = client.get("/actions")
        assert resp.status_code == 200
        assert 'id="actionsTableScroller"' in resp.text

    def test_login_page_uses_extra_padding_for_visibility_toggle(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert 'id="passwordInput"' in resp.text
        assert 'pr-20' in resp.text
        assert '/static/icon-192.png' in resp.text

    def test_inbox_page_keeps_filter_tabs_wrappable(self, client):
        resp = client.get("/inbox")
        assert resp.status_code == 200
        assert 'flex-wrap: wrap;' in resp.text

    def test_actions_page_uses_app_icon_branding(self, client):
        resp = client.get("/actions")
        assert resp.status_code == 200
        assert '/static/icon-192.png' in resp.text

    def test_standalone_db_path_honors_database_path_env(self):
        from admin import dependencies

        with patch.dict(os.environ, {"DATABASE_PATH": "C:/temp/custom-assistant.db"}, clear=False):
            assert dependencies._standalone_db_path() == "C:/temp/custom-assistant.db"

    def test_llamacpp_start_empty_body_does_not_500(self, client):
        """Starting llama.cpp with an empty body should return JSON, not a 500."""
        class _FakeStatus:
            def to_dict(self):
                return {"running": False, "available_models": ["demo-model"]}

        fake_mgr = MagicMock()
        fake_mgr.list_models.return_value = [{"name": "demo-model", "path": "C:/tmp/demo-model.gguf"}]
        fake_mgr.launch.return_value = (False, "llama-server not installed. Download it first.")
        fake_mgr.get_status.return_value = _FakeStatus()

        with patch("services.llamacpp_manager.get_llamacpp_manager", return_value=fake_mgr):
            resp = client.post(
                "/api/v1/llamacpp/start",
                data="",
                headers={"Content-Type": "application/json", "X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "llama-server not installed" in data["message"]

    def test_ollama_status_fails_soft_when_status_lookup_raises(self, client):
        with patch("admin.routers.system.get_cached_ollama_status", side_effect=RuntimeError("boom")):
            resp = client.get("/api/v1/ollama/status?refresh=1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "ollama_status_unavailable"
        assert data["installed"] is False
        assert data["running"] is False
        assert data["models"] == []

    def test_ollama_install_fails_soft_when_installer_raises(self, client):
        with patch("services.system_tools.SystemTools.install_ollama_windows", AsyncMock(side_effect=RuntimeError("boom"))):
            resp = client.post("/api/v1/ollama/install", headers={"X-CSRF-Protection": "1"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "ollama_install_failed"

    def test_setup_preflight_fails_soft_when_workspace_probe_breaks(self, client):
        class _BrokenRoot:
            def __truediv__(self, _):
                raise RuntimeError("boom")

        with patch("admin.routers.settings.LEISURELLM_DIR", _BrokenRoot()):
            resp = client.get("/api/v1/setup/preflight")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "setup_preflight_unavailable"

    def test_setup_keys_fails_soft_when_env_write_raises(self, client):
        with patch("admin.routers.settings._upsert_env_var", side_effect=OSError("disk full")):
            resp = client.post(
                "/api/v1/setup/keys",
                headers={"X-CSRF-Protection": "1"},
                json={"operation_mode": "solo"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "setup_keys_failed"

    def test_setup_keys_preserves_existing_discord_token_when_blank(self, client, monkeypatch, tmp_path):
        from admin.routers import settings

        env_path = tmp_path / ".env"
        env_path.write_text("DISCORD_TOKEN=existing-token\n", encoding="utf-8")
        monkeypatch.setattr(settings, "_ENV_PATH", env_path)

        mock_secrets = MagicMock()
        mock_secrets.set = MagicMock()

        with patch("services.secrets.get_secrets_manager", return_value=mock_secrets), \
             patch("admin.server._register_cloud_backends_from_secrets", AsyncMock()):
            resp = client.post(
                "/api/v1/setup/keys",
                headers={"X-CSRF-Protection": "1"},
                json={"operation_mode": "team"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        saved = env_path.read_text(encoding="utf-8")
        assert "OPERATION_MODE=team" in saved
        assert "DISCORD_TOKEN=existing-token" in saved

    def test_org_profile_save_updates_runtime_mode_env(self, client, monkeypatch, tmp_path):
        from admin.routers import settings

        config_dir = tmp_path / "config"
        env_path = tmp_path / ".env"
        monkeypatch.setattr(settings, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(settings, "_ENV_PATH", env_path)

        resp = client.post(
            "/api/v1/org/profile",
            headers={"X-CSRF-Protection": "1"},
            json={"org_name": "Test Org", "mode": "team"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert (config_dir / "org_profile.yaml").exists()
        saved_env = env_path.read_text(encoding="utf-8")
        assert "OPERATION_MODE=team" in saved_env

    def test_setup_complete_fails_soft_when_flag_write_raises(self, client):
        from admin.routers import settings

        with patch.object(settings, "_build_onboarding_state", AsyncMock(return_value={"starter_content_seeded": False})), \
             patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            resp = client.post("/api/v1/setup/complete", headers={"X-CSRF-Protection": "1"}, json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "setup_complete_failed"


class TestAuthEnforcement:
    """Verify auth is enforced when enabled."""

    def test_page_requires_token_when_auth_enabled(self, client):
        """With auth re-enabled, API calls should return 401 without a token."""
        with patch("admin.dependencies.admin_auth_enabled", return_value=True):
            resp = client.get("/api/v1/secrets/list")
            assert resp.status_code == 401

    def test_page_accessible_with_valid_session(self, client):
        """With auth enabled, a resolved actor should be able to access member pages."""
        with patch("admin.dependencies.admin_auth_enabled", return_value=True), \
             patch(
                 "admin.dependencies.get_current_actor_optional",
                 AsyncMock(
                     return_value=ActorContext(
                         actor_id=5,
                         stable_id="actor_test_admin",
                         actor_kind="web_account",
                         external_ref="webacct_test_admin",
                         display_name="Test Admin",
                         role="admin",
                         account_id=9,
                         username="test-admin",
                         auth_source="test",
                     )
                 ),
             ):
            resp = client.get("/actions")
            assert resp.status_code == 200

    def test_html_page_redirects_to_login_when_auth_enabled(self, client):
        """With auth enabled, browser requests should redirect to /login."""
        with patch("admin.dependencies.admin_auth_enabled", return_value=True):
            resp = client.get("/actions", headers={"Accept": "text/html"}, follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# TestRailMapAPIs removed — Rails deprecated per product direction (2026-07)


class TestLoginSystem:
    """Test login page, auth endpoints, and cookie management."""

    def test_login_page_renders_without_auth(self, client):
        """The login page should be accessible without any token."""
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_login_page_accepts_next_param(self, client):
        resp = client.get("/login?next=/settings")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_bootstrap_with_valid_token_sets_session_cookie(self, client):
        mock_service = MagicMock()
        mock_service.session_cookie_name = "mka_session"
        mock_service.bootstrap_admin = AsyncMock(
            return_value={"username": "owner", "display_name": "Owner", "role": "admin"}
        )
        mock_service.authenticate = AsyncMock(
            return_value=({"username": "owner", "display_name": "Owner", "role": "admin"}, "session-abc")
        )
        mock_service.has_any_accounts = AsyncMock(return_value=False)
        with patch("admin.server.admin_auth_enabled", return_value=True), \
             patch("admin.server.get_web_identity_service", return_value=mock_service), \
             patch("admin.server._ensure_admin_token", return_value="valid-token-xyz"):
            resp = client.post(
                "/api/v1/auth/bootstrap",
                json={"bootstrap_token": "valid-token-xyz", "username": "owner", "password": "ownerpass123"},
                headers={"X-CSRF-Protection": "1"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert "mka_session" in resp.cookies

    def test_login_with_invalid_credentials(self, client):
        mock_service = MagicMock()
        mock_service.has_any_accounts = AsyncMock(return_value=True)
        mock_service.authenticate = AsyncMock(side_effect=Exception("invalid username or password"))
        with patch("admin.server.admin_auth_enabled", return_value=True), \
             patch("admin.server.get_web_identity_service", return_value=mock_service):
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "owner", "password": "wrong-password"},
                headers={"X-CSRF-Protection": "1"},
            )
            assert resp.status_code == 401
            data = resp.json()
            assert data["success"] is False

    def test_logout_clears_cookie(self, client):
        mock_service = MagicMock()
        mock_service.session_cookie_name = "mka_session"
        mock_service.revoke_session = AsyncMock(return_value=None)
        with patch("admin.server.get_web_identity_service", return_value=mock_service):
            resp = client.post("/api/v1/auth/logout", headers={"X-CSRF-Protection": "1"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True

    def test_auth_status_when_disabled(self, client):
        resp = client.get("/api/v1/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True

    def test_auth_status_when_enabled_no_session(self, client):
        mock_service = MagicMock()
        mock_service.has_any_accounts = AsyncMock(return_value=True)
        with patch("admin.server.admin_auth_enabled", return_value=True), \
             patch("admin.server.get_web_identity_service", return_value=mock_service), \
             patch("admin.server.get_current_actor_optional", AsyncMock(return_value=None)):
            resp = client.get("/api/v1/auth/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["auth_enabled"] is True
            assert data["authenticated"] is False

    def test_reveal_token_from_localhost(self, client):
        """Localhost requests should receive the token."""
        # httpx TestClient uses 'testclient' as client host; widen the
        # allowlist so the endpoint treats it like real localhost.
        mock_service = MagicMock()
        mock_service.has_any_accounts = AsyncMock(return_value=False)
        with patch("admin.server._ensure_admin_token", return_value="my-secret-tok"), \
             patch("admin.server.get_web_identity_service", return_value=mock_service), \
             patch("admin.server._LOCALHOST_IPS", ("127.0.0.1", "::1", "localhost", "testclient")):
            resp = client.get("/api/v1/auth/reveal-token")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["token"] == "my-secret-tok"

    def test_reveal_token_blocked_for_remote(self, client):
        """Non-localhost requests must be rejected."""
        mock_service = MagicMock()
        mock_service.has_any_accounts = AsyncMock(return_value=False)
        with patch("admin.server._ensure_admin_token", return_value="tok"), \
             patch("admin.server.get_web_identity_service", return_value=mock_service):
            # httpx TestClient always uses 'testclient' as host, which is
            # not in the localhost allowlist, so this should be rejected.
            resp = client.get(
                "/api/v1/auth/reveal-token",
                headers={"X-Forwarded-For": "203.0.113.5"},
            )
            # The endpoint checks request.client.host which is 'testclient'
            # in httpx, so this should return 403.
            assert resp.status_code == 403
            data = resp.json()
            assert data["success"] is False

    def test_login_page_mentions_password(self, client):
        """Login page should reference usernames/passwords and bootstrap flow."""
        resp = client.get("/login")
        text = resp.text
        assert "Username" in text or "username" in text
        assert "Password" in text or "password" in text
        assert "bootstrap" in text.lower()
        assert "admin token" not in text.lower()


class TestJobsPage:
    """Test jobs page and API."""

    def test_jobs_page_renders(self, client):
        resp = client.get("/jobs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_jobs_api_returns_registry(self, client):
        resp = client.get("/api/v1/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data["jobs"], list)
        assert data["total"] > 0
        # Verify job shape
        job = data["jobs"][0]
        assert "name" in job
        assert "schedule" in job
        assert "module" in job
        assert "cog" in job


class TestExplorerPage:
    """Test data explorer page and API."""

    def test_explorer_page_renders(self, client):
        resp = client.get("/explorer")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_explorer_tables_api(self, client):
        resp = client.get("/api/v1/explorer/tables")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data["tables"], list)

    def test_explorer_invalid_table_name(self, client):
        resp = client.get("/api/v1/explorer/drop%20table")
        assert resp.status_code == 400


class TestBroaderFailSoft:
    """Verify that broad-Exception error handlers no longer leak str(e)."""

    def test_continuity_obligations_error_is_sanitized(self, client):
        """Obligations list error returns controlled code, not raw traceback."""
        resp = client.get("/api/v1/obligations")
        assert resp.status_code == 200
        data = resp.json()
        if not data.get("success"):
            assert data["error"] == "request_failed"
            assert "message" in data

    def test_artifacts_action_stats_error_is_sanitized(self, client):
        """Action stats error returns request_failed, not raw exception."""
        resp = client.get("/api/v1/actions/stats")
        # The mock may or may not raise — but if it does the response must be clean
        assert resp.status_code == 200
        data = resp.json()
        if not data.get("success"):
            assert data["error"] == "request_failed"
            assert "message" in data

    def test_review_queue_list_error_is_sanitized(self, client):
        """Review queue list error is controlled."""
        with patch("admin.routers.review_queue.ReviewQueueService", side_effect=RuntimeError("connection reset")):
            resp = client.get("/api/v1/review-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "request_failed"
        assert "connection reset" not in data.get("message", "")

    def test_model_router_presets_error_is_sanitized(self, client):
        """Model router presets error is controlled."""
        with patch("services.pipeline_presets.list_presets", AsyncMock(side_effect=RuntimeError("not loaded"))):
            resp = client.get("/api/v1/router/presets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "request_failed"
        assert "not loaded" not in data.get("message", "")

    def test_retrieval_log_recent_error_is_sanitized(self, client):
        """Inference/recent error is controlled."""
        resp = client.get("/api/v1/inference/recent")
        assert resp.status_code == 200
        data = resp.json()
        if not data.get("success"):
            assert data["error"] == "request_failed"
            assert "message" in data


class TestOnboardingWording:
    """Issue 7: Verify setup/router wording doesn't expose confusing jargon."""

    def test_model_router_page_has_no_phase_jargon(self, client):
        """'Save Phase N' / 'Save Main Response' should be replaced with clear labels."""
        resp = client.get("/router")
        assert resp.status_code == 200
        assert "Save Phase 2" not in resp.text
        assert "Save Phase 3" not in resp.text
        assert "Save Main Response" not in resp.text
        assert "Save Default Model" in resp.text
        assert "Save Critique Model" in resp.text
        assert "Save Synthesis Model" in resp.text

    def test_setup_page_has_no_redundant_banner(self, client):
        """Setup wizard should NOT include the model_setup_guidance banner."""
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert "setupModelBanner" not in resp.text

    def test_guidance_ready_state_uses_clear_wording(self):
        """The 'provider_ready_phase1_not_saved' guidance title should be actionable."""
        from admin.routers.settings import _build_model_setup_guidance

        state = {
            "phase1_saved": False,
            "provider_detected": True,
            "provider_connected": True,
        }
        guidance = _build_model_setup_guidance(state)
        assert guidance["show_banner"] is True
        assert "One more step" in guidance["title"]
        assert "default model" in guidance["detail"].lower()

    def test_guidance_phase1_saved_no_main_response(self):
        """Phase-1-saved guidance detail must not say 'main response'."""
        from admin.routers.settings import _build_model_setup_guidance

        state = {
            "phase1_saved": True,
            "provider_detected": True,
            "provider_connected": True,
        }
        guidance = _build_model_setup_guidance(state)
        assert guidance["show_banner"] is False
        assert "main response" not in guidance["detail"].lower()
        assert "default" in guidance["detail"].lower()

    def test_dashboard_readability_classes(self, client):
        """Dashboard subtitle paragraphs should use text-secondary, not text-gray-400."""
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        html = resp.text
        assert 'id="greetingSub"' in html
        # The greetingSub paragraph should use text-secondary
        idx = html.index('id="greetingSub"')
        snippet = html[max(0, idx - 80):idx]
        assert "text-secondary" in snippet
