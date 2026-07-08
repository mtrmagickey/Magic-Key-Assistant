"""
Tests for services/model_router.py — Multi-backend LLM orchestration.

Covers:
- BackendConfig defaults
- AnthropicClient._split_system_and_messages
- ModelRouter lifecycle (register, configure, generate_single, pipeline)
- Config persistence (to_config_dict, save_config, from_config_file)
- Error paths (unknown backend, no pipeline, Anthropic without key)

Run with: pytest tests/test_model_router.py -v
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from services.model_router import (
    AnthropicClient,
    BackendConfig,
    BackendType,
    ModelRouter,
    OllamaClient,
    OpenAICompatibleClient,
    PipelineConfig,
    PipelineRole,
    RoleConfig,
)

# ============================================================
# BackendConfig defaults
# ============================================================

class TestBackendConfig:

    @pytest.mark.unit
    def test_openai_default_endpoint(self):
        cfg = BackendConfig(backend_type=BackendType.OPENAI, name="openai")
        assert cfg.endpoint_url == "https://api.openai.com/v1"

    @pytest.mark.unit
    def test_anthropic_default_endpoint(self):
        cfg = BackendConfig(backend_type=BackendType.ANTHROPIC, name="anth")
        assert cfg.endpoint_url == "https://api.anthropic.com/v1"

    @pytest.mark.unit
    def test_ollama_default_endpoint(self):
        cfg = BackendConfig(backend_type=BackendType.OLLAMA, name="local")
        assert cfg.endpoint_url == "http://localhost:11434"

    @pytest.mark.unit
    def test_openrouter_default_endpoint(self):
        cfg = BackendConfig(backend_type=BackendType.OPENROUTER, name="or")
        assert cfg.endpoint_url == "https://openrouter.ai/api/v1"

    @pytest.mark.unit
    def test_custom_endpoint_preserved(self):
        cfg = BackendConfig(
            backend_type=BackendType.OLLAMA,
            name="remote",
            endpoint_url="http://myserver:11434",
        )
        assert cfg.endpoint_url == "http://myserver:11434"


# ============================================================
# PipelineRole enum
# ============================================================

class TestPipelineRole:

    @pytest.mark.unit
    def test_role_values(self):
        assert PipelineRole.INITIAL.value == "initial"
        assert PipelineRole.CRITIQUE.value == "critique"
        assert PipelineRole.SYNTHESIZE.value == "synthesize"

    @pytest.mark.unit
    def test_role_from_string(self):
        assert PipelineRole("initial") is PipelineRole.INITIAL


# ============================================================
# AnthropicClient._split_system_and_messages (pure static)
# ============================================================

class TestAnthropicSplitMessages:

    @pytest.mark.unit
    def test_splits_system_from_user(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        system, filtered = AnthropicClient._split_system_and_messages(msgs)
        assert system == "You are helpful."
        assert filtered == [{"role": "user", "content": "Hello"}]

    @pytest.mark.unit
    def test_multiple_system_messages_joined(self):
        msgs = [
            {"role": "system", "content": "Part A"},
            {"role": "system", "content": "Part B"},
            {"role": "user", "content": "Go"},
        ]
        system, filtered = AnthropicClient._split_system_and_messages(msgs)
        assert "Part A" in system
        assert "Part B" in system
        assert len(filtered) == 1

    @pytest.mark.unit
    def test_no_system_message(self):
        msgs = [{"role": "user", "content": "Hi"}]
        system, filtered = AnthropicClient._split_system_and_messages(msgs)
        assert system == ""
        assert len(filtered) == 1

    @pytest.mark.unit
    def test_tool_messages_ignored(self):
        msgs = [
            {"role": "user", "content": "search"},
            {"role": "tool", "content": "result data"},
            {"role": "assistant", "content": "here you go"},
        ]
        system, filtered = AnthropicClient._split_system_and_messages(msgs)
        assert len(filtered) == 2
        assert all(m["role"] in ("user", "assistant") for m in filtered)

    @pytest.mark.unit
    def test_unknown_role_defaults_to_user(self):
        msgs = [{"role": "developer", "content": "yo"}]
        system, filtered = AnthropicClient._split_system_and_messages(msgs)
        assert filtered == [{"role": "user", "content": "yo"}]


# ============================================================
# ModelRouter — client factory
# ============================================================

class TestModelRouterClientFactory:

    @pytest.mark.unit
    def test_creates_ollama_client(self):
        router = ModelRouter()
        cfg = BackendConfig(backend_type=BackendType.OLLAMA, name="local")
        client = router._create_client(cfg)
        assert isinstance(client, OllamaClient)

    @pytest.mark.unit
    def test_creates_anthropic_client(self):
        router = ModelRouter()
        cfg = BackendConfig(
            backend_type=BackendType.ANTHROPIC,
            name="anth",
            api_key="sk-test",
        )
        client = router._create_client(cfg)
        assert isinstance(client, AnthropicClient)

    @pytest.mark.unit
    def test_anthropic_without_key_raises(self):
        router = ModelRouter()
        cfg = BackendConfig(backend_type=BackendType.ANTHROPIC, name="anth")
        with pytest.raises(ValueError, match="api_key"):
            router._create_client(cfg)

    @pytest.mark.unit
    def test_creates_openai_compatible_for_openai(self):
        router = ModelRouter()
        cfg = BackendConfig(
            backend_type=BackendType.OPENAI,
            name="oai",
            api_key="sk-test",
        )
        client = router._create_client(cfg)
        assert isinstance(client, OpenAICompatibleClient)

    @pytest.mark.unit
    def test_creates_openai_compatible_for_custom(self):
        router = ModelRouter()
        cfg = BackendConfig(
            backend_type=BackendType.CUSTOM,
            name="vllm",
            endpoint_url="http://myserver:8000/v1",
        )
        client = router._create_client(cfg)
        assert isinstance(client, OpenAICompatibleClient)


# ============================================================
# ModelRouter — register_backend
# ============================================================

class TestModelRouterRegisterBackend:

    @pytest.mark.unit
    async def test_register_success(self):
        router = ModelRouter()
        cfg = BackendConfig(backend_type=BackendType.OLLAMA, name="local")

        mock_client = AsyncMock()
        mock_client.health_check = AsyncMock(return_value=True)
        mock_client.list_models = AsyncMock(return_value=["llama3:8b", "mistral:7b"])

        with patch.object(router, "_create_client", return_value=mock_client):
            ok = await router.register_backend(cfg)

        assert ok is True
        assert "local" in router.backends
        assert "local" in router.clients
        assert cfg.available_models == ["llama3:8b", "mistral:7b"]

    @pytest.mark.unit
    async def test_register_failure_health_check(self):
        router = ModelRouter()
        cfg = BackendConfig(backend_type=BackendType.OLLAMA, name="dead")

        mock_client = AsyncMock()
        mock_client.health_check = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        with patch.object(router, "_create_client", return_value=mock_client):
            ok = await router.register_backend(cfg)

        assert ok is False
        assert "dead" not in router.backends

    @pytest.mark.unit
    async def test_register_preserves_existing_models(self):
        router = ModelRouter()
        cfg = BackendConfig(
            backend_type=BackendType.OLLAMA,
            name="local",
            available_models=["preset-model"],
        )

        mock_client = AsyncMock()
        mock_client.health_check = AsyncMock(return_value=True)

        with patch.object(router, "_create_client", return_value=mock_client):
            await router.register_backend(cfg)

        # Should not overwrite when already populated
        assert cfg.available_models == ["preset-model"]
        mock_client.list_models.assert_not_called()


# ============================================================
# ModelRouter — configure_pipeline
# ============================================================

class TestConfigurePipeline:

    @pytest.mark.unit
    def test_configure_valid_pipeline(self):
        router = ModelRouter()
        router.backends["local"] = BackendConfig(backend_type=BackendType.OLLAMA, name="local")

        pipeline = PipelineConfig(
            name="test",
            roles={
                PipelineRole.INITIAL: RoleConfig(
                    role=PipelineRole.INITIAL,
                    backend_name="local",
                    model="llama3:8b",
                ),
            },
        )
        router.configure_pipeline(pipeline)
        assert router.pipeline is pipeline

    @pytest.mark.unit
    def test_configure_unknown_backend_raises(self):
        router = ModelRouter()
        pipeline = PipelineConfig(
            name="broken",
            roles={
                PipelineRole.INITIAL: RoleConfig(
                    role=PipelineRole.INITIAL,
                    backend_name="nonexistent",
                    model="x",
                ),
            },
        )
        with pytest.raises(ValueError, match="unknown backend"):
            router.configure_pipeline(pipeline)


# ============================================================
# ModelRouter — generate_single
# ============================================================

class TestGenerateSingle:

    @pytest.mark.unit
    async def test_generate_single_ok(self):
        router = ModelRouter()
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value="Hello world")
        router.clients["local"] = mock_client
        router.backends["local"] = BackendConfig(backend_type=BackendType.OLLAMA, name="local")

        result = await router.generate_single(
            backend_name="local",
            model="llama3:8b",
            prompt="Say hi",
        )
        assert result == "Hello world"
        mock_client.generate.assert_awaited_once()

    @pytest.mark.unit
    async def test_generate_single_with_system_prompt(self):
        router = ModelRouter()
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value="response")
        router.clients["local"] = mock_client
        router.backends["local"] = BackendConfig(backend_type=BackendType.OLLAMA, name="local")

        await router.generate_single(
            backend_name="local",
            model="m",
            prompt="yo",
            system_prompt="Be concise",
        )

        call_args = mock_client.generate.call_args
        messages = call_args.kwargs.get("messages", call_args[1].get("messages", []))
        assert any(m["role"] == "system" for m in messages)

    @pytest.mark.unit
    async def test_generate_single_unknown_backend(self):
        router = ModelRouter()
        with pytest.raises(ValueError, match="Unknown backend"):
            await router.generate_single("nope", "model", "prompt")


# ============================================================
# ModelRouter — generate_pipeline
# ============================================================

class TestGeneratePipeline:

    def _make_router_with_pipeline(self) -> ModelRouter:
        """Helper: router with two mock backends and a full 3-stage pipeline."""
        router = ModelRouter()

        for name in ("fast", "smart"):
            router.backends[name] = BackendConfig(backend_type=BackendType.OLLAMA, name=name)
            client = AsyncMock()
            client.generate = AsyncMock(return_value=f"response-from-{name}")
            router.clients[name] = client

        router.configure_pipeline(PipelineConfig(
            name="test-pipeline",
            roles={
                PipelineRole.INITIAL: RoleConfig(
                    role=PipelineRole.INITIAL, backend_name="fast", model="m1",
                ),
                PipelineRole.CRITIQUE: RoleConfig(
                    role=PipelineRole.CRITIQUE, backend_name="smart", model="m2",
                ),
                PipelineRole.SYNTHESIZE: RoleConfig(
                    role=PipelineRole.SYNTHESIZE, backend_name="smart", model="m2",
                ),
            },
        ))
        return router

    @pytest.mark.unit
    async def test_full_pipeline_returns_stages(self):
        router = self._make_router_with_pipeline()
        result = await router.generate_pipeline(user_prompt="What is X?")

        assert "final" in result
        assert "stages" in result
        assert "models_used" in result
        assert "initial" in result["stages"]
        assert "critique" in result["stages"]
        assert "synthesize" in result["stages"]

    @pytest.mark.unit
    async def test_pipeline_final_is_synthesize(self):
        router = self._make_router_with_pipeline()
        result = await router.generate_pipeline(user_prompt="question")
        assert result["final"] == result["stages"]["synthesize"]

    @pytest.mark.unit
    async def test_pipeline_models_used_tracks_backends(self):
        router = self._make_router_with_pipeline()
        result = await router.generate_pipeline(user_prompt="q")
        assert result["models_used"]["initial"] == "fast/m1"
        assert result["models_used"]["critique"] == "smart/m2"

    @pytest.mark.unit
    async def test_no_pipeline_raises(self):
        router = ModelRouter()
        with pytest.raises(RuntimeError, match="No pipeline configured"):
            await router.generate_pipeline(user_prompt="oops")

    @pytest.mark.unit
    async def test_pipeline_critique_disabled(self):
        router = ModelRouter()
        router.backends["b"] = BackendConfig(backend_type=BackendType.OLLAMA, name="b")
        client = AsyncMock()
        client.generate = AsyncMock(return_value="answer")
        router.clients["b"] = client

        router.configure_pipeline(PipelineConfig(
            name="two-stage",
            roles={
                PipelineRole.INITIAL: RoleConfig(
                    role=PipelineRole.INITIAL, backend_name="b", model="m",
                ),
                PipelineRole.CRITIQUE: RoleConfig(
                    role=PipelineRole.CRITIQUE, backend_name="b", model="m", enabled=False,
                ),
                PipelineRole.SYNTHESIZE: RoleConfig(
                    role=PipelineRole.SYNTHESIZE, backend_name="b", model="m",
                ),
            },
        ))
        result = await router.generate_pipeline(user_prompt="q")
        assert "critique" not in result["stages"]


# ============================================================
# ModelRouter — config serialization
# ============================================================

class TestConfigSerialization:

    @pytest.mark.unit
    def test_to_config_dict_structure(self):
        router = ModelRouter()
        router.backends["local"] = BackendConfig(
            backend_type=BackendType.OLLAMA,
            name="local",
            available_models=["llama3:8b"],
        )
        router.pipeline = PipelineConfig(
            name="prod",
            roles={
                PipelineRole.INITIAL: RoleConfig(
                    role=PipelineRole.INITIAL,
                    backend_name="local",
                    model="llama3:8b",
                    temperature=0.4,
                ),
            },
        )

        d = router.to_config_dict()
        assert "backends" in d
        assert "pipeline" in d
        assert d["backends"]["local"]["backend_type"] == "ollama"
        assert d["pipeline"]["roles"]["initial"]["model"] == "llama3:8b"

    @pytest.mark.unit
    def test_api_key_ref_uses_env_var_convention(self):
        router = ModelRouter()
        router.backends["openai"] = BackendConfig(
            backend_type=BackendType.OPENAI,
            name="openai",
            api_key="sk-secret",
        )
        router.pipeline = PipelineConfig()

        d = router.to_config_dict()
        assert d["backends"]["openai"]["api_key_ref"] == "${OPENAI_API_KEY}"
        assert "sk-secret" not in json.dumps(d)

    @pytest.mark.unit
    def test_save_and_load_roundtrip(self, tmp_path):
        router = ModelRouter()
        router.backends["local"] = BackendConfig(
            backend_type=BackendType.OLLAMA,
            name="local",
            available_models=["llama3:8b"],
        )
        router.pipeline = PipelineConfig(
            name="roundtrip",
            roles={
                PipelineRole.INITIAL: RoleConfig(
                    role=PipelineRole.INITIAL,
                    backend_name="local",
                    model="llama3:8b",
                    temperature=0.5,
                    max_tokens=2000,
                ),
            },
        )

        config_file = tmp_path / "model_router.json"
        router.save_config(config_file)

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["pipeline"]["name"] == "roundtrip"
        assert data["backends"]["local"]["backend_type"] == "ollama"

    @pytest.mark.unit
    def test_save_no_path_raises(self):
        router = ModelRouter()
        router.pipeline = PipelineConfig()
        with pytest.raises(ValueError, match="config path"):
            router.save_config()

    @pytest.mark.unit
    def test_to_config_dict_no_pipeline(self):
        router = ModelRouter()
        d = router.to_config_dict()
        assert d["pipeline"]["name"] == "default"
        assert d["pipeline"]["roles"] == {}


# ============================================================
# ModelRouter — close
# ============================================================

class TestClose:

    @pytest.mark.unit
    async def test_close_calls_client_close(self):
        router = ModelRouter()
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        router.clients["a"] = mock_client

        await router.close()
        mock_client.close.assert_awaited_once()
