"""
Model Router Service

A backend-agnostic LLM orchestration layer that supports:
- Multiple model backends (OpenAI, Anthropic, Ollama, OpenRouter, custom)
- Configurable synthesis pipelines (Initial → Critique → Synthesize)
- Secure credential storage
- Hot-swappable model assignments

This is the core of the "stackable models" product vision.
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


# =============================================================================
# BACKEND DEFINITIONS
# =============================================================================

class BackendType(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
    LLAMACPP = "llamacpp"  # Managed llama.cpp server (OpenAI-compatible)
    CUSTOM = "custom"  # Any OpenAI-compatible endpoint


@dataclass
class BackendConfig:
    """Configuration for a single LLM backend."""
    backend_type: BackendType
    name: str  # User-friendly name, e.g. "My Ollama Server"
    
    # Authentication (cloud)
    api_key: Optional[str] = None
    
    # Endpoint (local/custom)
    endpoint_url: Optional[str] = None
    
    # Available models (can be auto-discovered for Ollama)
    available_models: List[str] = field(default_factory=list)
    
    # Default model for this backend
    default_model: Optional[str] = None
    
    # Backend-specific options
    options: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        # Set sensible defaults
        if self.backend_type == BackendType.OPENAI and not self.endpoint_url:
            self.endpoint_url = "https://api.openai.com/v1"
        elif self.backend_type == BackendType.ANTHROPIC and not self.endpoint_url:
            self.endpoint_url = "https://api.anthropic.com/v1"
        elif self.backend_type == BackendType.OLLAMA and not self.endpoint_url:
            self.endpoint_url = "http://localhost:11434"
        elif self.backend_type == BackendType.OPENROUTER and not self.endpoint_url:
            self.endpoint_url = "https://openrouter.ai/api/v1"


# =============================================================================
# PIPELINE ROLES
# =============================================================================

class PipelineRole(str, Enum):
    """The three synthesis roles in a multi-model pipeline."""
    INITIAL = "initial"       # First pass: raw answer generation
    CRITIQUE = "critique"     # Second pass: analysis, holes, devil's advocate
    SYNTHESIZE = "synthesize" # Final pass: combine into polished response


@dataclass
class RoleConfig:
    """Configuration for a single role in the synthesis pipeline."""
    role: PipelineRole
    backend_name: str        # Reference to a BackendConfig.name
    model: str               # Specific model to use
    temperature: float = 0.3
    max_tokens: int = 4000
    
    # Role-specific prompt templates
    system_prompt_override: Optional[str] = None
    
    # Whether this role is enabled (allows 1-model, 2-model, or 3-model pipelines)
    enabled: bool = True

    # Ollama-specific inference options (ignored for cloud backends)
    # These are CRITICAL for local model performance.
    ollama_options: Dict[str, Any] = field(default_factory=lambda: {
        "num_ctx": 16384,          # Context window — must fit system prompt + RAG context + output tokens
        "repeat_penalty": 1.1,    # Prevent repetition loops common in local models
        "top_k": 40,              # Limit token sampling pool for more focused answers
        "top_p": 0.9,             # Nucleus sampling — slightly tighter than default 1.0
        "stop": ["\n\nUser:", "\n\nHuman:", "---END---"],  # Prevent runaway generation
        "use_mmap": True,         # Memory-map model weights — faster cold-start, lower RAM footprint
        "num_batch": 512,         # Process tokens in larger batches for throughput (Ollama default is 512)
    })


@dataclass
class LLMTimeouts:
    """Centralised LLM operation timeouts (seconds).

    Loaded from the ``pipeline.timeouts`` key of model_router.json so
    operators can tune them without editing code.
    """
    chat: float = 60.0
    self_assessment: float = 15.0
    conversation_mining: float = 20.0
    enrichment: float = 120.0
    health_check: float = 5.0
    model_listing: float = 10.0
    benchmark_per_model: float = 2700.0


@dataclass 
class PipelineConfig:
    """Full pipeline configuration."""
    name: str = "default"
    roles: Dict[PipelineRole, RoleConfig] = field(default_factory=dict)
    
    # Global settings
    parallel_initial_critique: bool = False  # Run initial + critique in parallel?
    timeout_seconds: float = 60.0
    timeouts: LLMTimeouts = field(default_factory=LLMTimeouts)


# =============================================================================
# BACKEND CLIENTS
# =============================================================================

class BaseBackendClient(ABC):
    """Abstract base for backend clients."""
    
    @abstractmethod
    async def generate(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> str:
        """Generate a completion."""
        pass
    
    @abstractmethod
    async def list_models(self) -> List[str]:
        """List available models."""
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """Check if backend is reachable."""
        pass


class OpenAICompatibleClient(BaseBackendClient):
    """
    Client for OpenAI and OpenAI-compatible APIs.
    Works with: OpenAI, OpenRouter, vLLM, llama.cpp server, LM Studio, etc.
    """
    
    def __init__(self, endpoint_url: str, api_key: Optional[str] = None):
        self.endpoint_url = endpoint_url.rstrip("/")
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session
    
    async def generate(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> str:
        session = await self._get_session()
        
        payload = {
            "model": model,
            "messages": messages,
        }
        
        # Newer OpenAI models (o-series, gpt-5.x) use max_completion_tokens
        # and o-series doesn't support temperature
        is_reasoning_model = model.startswith(("o1", "o3"))
        is_new_model = model.startswith(("o1", "o3", "gpt-5", "gpt-4.1"))
        
        if is_new_model:
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
        
        if not is_reasoning_model:
            payload["temperature"] = temperature
        
        async with session.post(
            f"{self.endpoint_url}/chat/completions",
            json=payload
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"Backend error {resp.status}: {error_text}")
            
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
    
    async def list_models(self) -> List[str]:
        session = await self._get_session()
        async with session.get(f"{self.endpoint_url}/models") as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return [m["id"] for m in data.get("data", [])]
    
    async def health_check(self) -> bool:
        try:
            models = await self.list_models()
            return len(models) > 0
        except Exception as e:
            logger.debug("OpenAI-compatible health check failed: %s", e)
            return False
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class OllamaClient(BaseBackendClient):
    """
    Native Ollama client using Ollama's API directly.
    Ollama also supports OpenAI-compatible mode, but native gives more control.
    """

    def __init__(self, endpoint_url: str = "http://localhost:11434"):
        self.endpoint_url = endpoint_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def generate(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4000,
        ollama_options: Optional[Dict[str, Any]] = None,
    ) -> str:
        session = await self._get_session()

        # Build Ollama options: start with caller-supplied tuning, then overlay
        # temperature/num_predict so they always reflect the role config.
        opts: Dict[str, Any] = dict(ollama_options) if ollama_options else {}
        opts["temperature"] = temperature
        opts["num_predict"] = max_tokens
        # Ensure a sane context window — Ollama's default (2048) is far too
        # small for RAG workloads that send 4-8k tokens of retrieved context.
        opts.setdefault("num_ctx", 16384)

        # Extract stop sequences (Ollama expects them at top-level, not in options)
        stop_seqs = opts.pop("stop", None)

        # Ollama native chat endpoint
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": opts,
            "keep_alive": "30m",  # Keep model loaded — eliminates cold-start latency
        }
        if stop_seqs:
            payload["stop"] = stop_seqs

        async with session.post(
            f"{self.endpoint_url}/api/chat",
            json=payload,
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"Ollama error {resp.status}: {error_text}")

            data = await resp.json()
            return data["message"]["content"]

    async def list_models(self) -> List[str]:
        """Get list of locally available models."""
        session = await self._get_session()
        async with session.get(f"{self.endpoint_url}/api/tags") as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return [m["name"] for m in data.get("models", [])]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self.endpoint_url}/api/tags") as resp:
                return resp.status == 200
        except Exception as e:
            logger.debug("Ollama health check failed: %s", e)
            return False

    async def pull_model(self, model_name: str) -> bool:
        """Pull a model from Ollama registry."""
        session = await self._get_session()
        async with session.post(
            f"{self.endpoint_url}/api/pull",
            json={"name": model_name, "stream": False},
        ) as resp:
            return resp.status == 200

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class AnthropicClient(BaseBackendClient):
    """Client for Anthropic's Messages API."""

    def __init__(self, endpoint_url: str, api_key: str, anthropic_version: str = "2023-06-01"):
        self.endpoint_url = endpoint_url.rstrip("/")
        self.api_key = api_key
        self.anthropic_version = anthropic_version
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    @staticmethod
    def _split_system_and_messages(messages: List[Dict[str, str]]) -> tuple[str, List[Dict[str, str]]]:
        system_parts: List[str] = []
        anthropic_messages: List[Dict[str, str]] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            # Anthropic expects only user/assistant roles
            if role in ("user", "assistant"):
                anthropic_messages.append({"role": role, "content": content})
            elif role == "tool":
                # Ignore tool messages for now (not supported in this minimal client)
                continue
            else:
                # Default to user
                anthropic_messages.append({"role": "user", "content": content})

        return ("\n\n".join(system_parts)).strip(), anthropic_messages

    async def generate(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> str:
        session = await self._get_session()
        system, anthropic_messages = self._split_system_and_messages(messages)

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system

        async with session.post(f"{self.endpoint_url}/messages", json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"Backend error {resp.status}: {error_text}")

            data = await resp.json()
            blocks = data.get("content", [])
            if isinstance(blocks, list):
                parts: List[str] = []
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
                return "".join(parts).strip()

            if isinstance(data.get("content"), str):
                return data["content"]
            return ""

    async def list_models(self) -> List[str]:
        # Anthropic does not expose a simple public models list API.
        return [
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
            "claude-3-opus-latest",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307",
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            payload = {
                "model": "claude-3-5-haiku-latest",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            async with session.post(f"{self.endpoint_url}/messages", json=payload) as resp:
                return resp.status == 200
        except Exception as e:
            logger.debug("Anthropic health check failed: %s", e)
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# =============================================================================
# MODEL ROUTER - The Main Orchestrator
# =============================================================================

class ModelRouter:
    """
    The central orchestration service.
    
    Manages multiple backends and routes requests through configurable pipelines.
    This is the core of the "stackable models" product.
    """
    
    def __init__(self, config_path: Optional[Path] = None):
        self.backends: Dict[str, BackendConfig] = {}
        self.clients: Dict[str, BaseBackendClient] = {}
        self.pipeline: Optional[PipelineConfig] = None
        self.config_path = config_path
        self._cost_tracker: Optional[Any] = None  # InferenceCostTracker, set via set_cost_tracker()
        
    async def register_backend(self, config: BackendConfig) -> bool:
        """Register a new backend and test connectivity."""
        client = self._create_client(config)
        
        # Test connection
        if not await client.health_check():
            logger.warning(f"Backend '{config.name}' failed health check")
            await client.close() if hasattr(client, 'close') else None
            return False
        
        # Auto-discover models if empty
        if not config.available_models:
            config.available_models = await client.list_models()
            logger.info(f"Discovered {len(config.available_models)} models on '{config.name}'")
        
        self.backends[config.name] = config
        self.clients[config.name] = client
        return True
    
    def _create_client(self, config: BackendConfig) -> BaseBackendClient:
        """Factory for backend clients."""
        if config.backend_type == BackendType.OLLAMA:
            return OllamaClient(config.endpoint_url)
        if config.backend_type == BackendType.ANTHROPIC:
            if not config.api_key:
                raise ValueError("Anthropic backend requires api_key")
            return AnthropicClient(config.endpoint_url, config.api_key)
        else:
            # OpenAI, OpenRouter, llama.cpp, Custom all use OpenAI-compatible API
            return OpenAICompatibleClient(config.endpoint_url, config.api_key)
    
    def configure_pipeline(self, pipeline: PipelineConfig):
        """Set the active synthesis pipeline."""
        # Validate that all referenced backends exist
        for role_config in pipeline.roles.values():
            if role_config.backend_name not in self.backends:
                raise ValueError(f"Role '{role_config.role}' references unknown backend '{role_config.backend_name}'")
        self.pipeline = pipeline

    @property
    def timeouts(self) -> LLMTimeouts:
        """Centralised timeout values for all LLM operations."""
        if self.pipeline:
            return self.pipeline.timeouts
        return LLMTimeouts()

    def set_cost_tracker(self, tracker: Any) -> None:
        """Attach an InferenceCostTracker for automatic cost/token logging."""
        self._cost_tracker = tracker

    def _get_circuit_breaker(self, backend_name: str):
        """Get or create a circuit breaker for a backend."""
        from services.circuit_breaker import CircuitBreakerRegistry
        return CircuitBreakerRegistry.get_or_create(
            f"llm_{backend_name}",
            failure_threshold=3,
            cooldown_seconds=60.0,
            success_threshold=2,
        )

    async def _generate_with_system(
        self,
        config: "RoleConfig",
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Generate using proper system/user message separation.

        Unlike ``generate_single`` which puts everything in the user
        message, this constructs a proper multi-message payload so the
        model can distinguish instructions from content.
        """
        backend_name = config.backend_name
        if backend_name not in self.clients:
            raise ValueError(f"Unknown backend: {backend_name}")

        breaker = self._get_circuit_breaker(backend_name)
        if not breaker.allow_request():
            raise RuntimeError(
                f"Backend '{backend_name}' circuit is OPEN (recent failures). "
                f"Cooldown: {breaker.cooldown_seconds}s"
            )

        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        kwargs: Dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        if config.ollama_options and isinstance(self.clients[backend_name], OllamaClient):
            kwargs["ollama_options"] = config.ollama_options

        # Token estimation
        try:
            from services.token_estimator import get_token_estimator
            _estimator = get_token_estimator()
            _num_ctx = (config.ollama_options or {}).get("num_ctx", 16384) if isinstance(
                self.clients[backend_name], OllamaClient
            ) else 0
            _estimator.estimate_messages(
                messages=messages,
                model=config.model,
                max_tokens=config.max_tokens,
                num_ctx=_num_ctx,
                backend=backend_name,
            )
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        import time as _time
        _t0 = _time.monotonic()
        try:
            result = await self.clients[backend_name].generate(**kwargs)
        except Exception as exc:
            breaker.record_failure(str(exc)[:200])
            raise
        _latency_ms = int((_time.monotonic() - _t0) * 1000)
        breaker.record_success()

        if self._cost_tracker:
            backend_type = self.backends[backend_name].backend_type.value
            input_text = system_prompt + "\n" + user_prompt
            try:
                await self._cost_tracker.record_inference(
                    backend_type=backend_type,
                    backend_name=backend_name,
                    model=config.model,
                    pipeline_role=config.role.value,
                    input_text=input_text,
                    output_text=result,
                    latency_ms=_latency_ms,
                )
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        return result

    async def generate_single(
        self,
        backend_name: str,
        model: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
        ollama_options: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Single-shot generation on a specific backend."""
        if backend_name not in self.clients:
            raise ValueError(f"Unknown backend: {backend_name}")

        breaker = self._get_circuit_breaker(backend_name)
        if not breaker.allow_request():
            raise RuntimeError(
                f"Backend '{backend_name}' circuit is OPEN (recent failures). "
                f"Cooldown: {breaker.cooldown_seconds}s"
            )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Pass ollama_options only to Ollama backends (others ignore extra kwargs)
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if ollama_options and isinstance(self.clients[backend_name], OllamaClient):
            kwargs["ollama_options"] = ollama_options

        # ── Token estimation — catch silent context truncation ──
        try:
            from services.token_estimator import get_token_estimator
            _estimator = get_token_estimator()
            _num_ctx = (ollama_options or {}).get("num_ctx", 8192) if isinstance(
                self.clients[backend_name], OllamaClient
            ) else 0
            _estimate = _estimator.estimate_messages(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                num_ctx=_num_ctx,
                backend=backend_name,
            )
            if _estimate.truncation_risk == "certain":
                _estimate.pipeline_role = "single"  # type: ignore[attr-defined]
        except Exception:
            pass  # Never let monitoring break inference

        import time as _time
        _t0 = _time.monotonic()
        try:
            result = await self.clients[backend_name].generate(**kwargs)
        except Exception as exc:
            breaker.record_failure(str(exc)[:200])
            raise
        _latency_ms = int((_time.monotonic() - _t0) * 1000)
        breaker.record_success()

        # ── Cost tracking (non-blocking) ──
        if self._cost_tracker:
            backend_type = self.backends[backend_name].backend_type.value
            input_text = (system_prompt or "") + "\n" + prompt
            try:
                await self._cost_tracker.record_inference(
                    backend_type=backend_type,
                    backend_name=backend_name,
                    model=model,
                    pipeline_role="single",
                    input_text=input_text,
                    output_text=result,
                    latency_ms=_latency_ms,
                )
            except Exception:
                pass  # Never let tracking break inference

        return result

    async def generate_with_fallback(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
        preferred_backend: Optional[str] = None,
        ollama_options: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate with automatic fallback across all registered backends.

        Tries backends in priority order:
        1. ``preferred_backend`` (if specified and healthy)
        2. Pipeline INITIAL role backend
        3. All other registered backends, ordered by type
           (local first for cost savings, then cloud)

        Each backend is gate-checked via its circuit breaker before
        attempting a call.  On failure the next backend is tried.

        Raises RuntimeError only if **all** backends are exhausted.
        """
        # Build ordered candidate list
        candidates = self._build_fallback_order(preferred_backend)
        if not candidates:
            raise RuntimeError("No backends registered")

        last_error: Optional[Exception] = None
        for backend_name, model in candidates:
            try:
                return await self.generate_single(
                    backend_name=backend_name,
                    model=model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    ollama_options=ollama_options if isinstance(
                        self.clients.get(backend_name), OllamaClient
                    ) else None,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Fallback: backend '%s/%s' failed (%s), trying next…",
                    backend_name, model, exc,
                )
                continue

        raise RuntimeError(
            f"All {len(candidates)} backends exhausted. Last error: {last_error}"
        )

    def _build_fallback_order(
        self, preferred: Optional[str] = None,
    ) -> List[tuple]:
        """Return [(backend_name, model), …] in fallback priority order."""
        seen: set = set()
        order: List[tuple] = []

        def _add(name: str):
            if name in seen or name not in self.backends:
                return
            cfg = self.backends[name]
            model = cfg.default_model or (cfg.available_models[0] if cfg.available_models else None)
            if model:
                seen.add(name)
                order.append((name, model))

        # 1. Preferred
        if preferred:
            _add(preferred)

        # 2. Pipeline INITIAL role
        if self.pipeline:
            initial = self.pipeline.roles.get(PipelineRole.INITIAL)
            if initial and initial.enabled:
                _add(initial.backend_name)

        # 3. Local backends first (cheaper), then cloud
        local_types = {BackendType.OLLAMA, BackendType.CUSTOM}
        for name, cfg in self.backends.items():
            if cfg.backend_type in local_types:
                _add(name)
        for name, cfg in self.backends.items():
            if cfg.backend_type not in local_types:
                _add(name)

        return order
    
    async def generate_pipeline(
        self,
        user_prompt: str,
        context: str = "",
        system_prompt: str = "",
        auto_route: bool = True,
        max_generation_stages: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Run the full synthesis pipeline.

        If ``auto_route=True`` (default) and a VSLM router model is available,
        the query is first classified by complexity and the pipeline preset is
        automatically applied (Speed / Balanced / Quality).  This can be
        disabled per-call or globally via ``vslm_router.enabled = False``.

        Returns a dict with:
        - 'final': The synthesized response
        - 'stages': Dict of intermediate outputs for transparency
        - 'models_used': Which models handled each stage
        - 'vslm_classification': (optional) VSLM routing info
        """
        if not self.pipeline:
            raise RuntimeError("No pipeline configured. Call configure_pipeline() first.")

        vslm_info: Optional[Dict[str, Any]] = None

        # ── VSLM auto-routing ───────────────────────────────────────
        if auto_route:
            try:
                from services.vslm_router import get_vslm_router
                vslm = get_vslm_router()
                if vslm.enabled:
                    classification = await vslm.classify(
                        user_prompt,
                        context_snippet=context[:300] if context else "",
                    )
                    if classification.confidence > 0:
                        # Apply the matching preset
                        from services.pipeline_presets import resolve_preset
                        preset_config, err = await resolve_preset(
                            classification.preset_name,
                            backend_name=next(iter(self.backends), "ollama"),
                        )
                        if preset_config and not err:
                            # Temporarily apply the preset for this call
                            original_pipeline = self.pipeline
                            self.pipeline = preset_config
                            logger.info(
                                "VSLM routed query to '%s' preset (complexity=%s, %.0fms, model=%s)",
                                classification.preset_name,
                                classification.complexity.value,
                                classification.latency_ms,
                                classification.model_used,
                            )
                            try:
                                result = await self._run_pipeline_stages(
                                    user_prompt, context, system_prompt,
                                    max_generation_stages=max_generation_stages,
                                )
                                result["vslm_classification"] = {
                                    "complexity": classification.complexity.value,
                                    "preset": classification.preset_name,
                                    "confidence": classification.confidence,
                                    "latency_ms": classification.latency_ms,
                                    "model": classification.model_used,
                                }
                                return result
                            finally:
                                self.pipeline = original_pipeline
                        else:
                            logger.debug("VSLM preset '%s' not available: %s", classification.preset_name, err)
                    vslm_info = {
                        "complexity": classification.complexity.value,
                        "preset": classification.preset_name,
                        "confidence": classification.confidence,
                        "latency_ms": classification.latency_ms,
                        "model": classification.model_used,
                        "applied": False,
                        "reason": "preset_unavailable" if classification.confidence > 0 else "low_confidence",
                    }
            except Exception as e:
                logger.debug("VSLM auto-routing skipped: %s", e)

        # ── Standard pipeline execution ─────────────────────────────
        result = await self._run_pipeline_stages(
            user_prompt,
            context,
            system_prompt,
            max_generation_stages=max_generation_stages,
        )
        if vslm_info:
            result["vslm_classification"] = vslm_info
        return result

    async def _run_pipeline_stages(
        self,
        user_prompt: str,
        context: str = "",
        system_prompt: str = "",
        max_generation_stages: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute the configured pipeline stages (internal)."""
        
        stages = {}
        models_used = {}
        stage_limit = max(1, int(max_generation_stages)) if max_generation_stages else None
        
        # STAGE 1: Initial
        initial_config = self.pipeline.roles.get(PipelineRole.INITIAL)
        if initial_config and initial_config.enabled:
            # Use proper system/user message separation for better instruction following
            initial_system = system_prompt or ""
            initial_user = ""
            if context:
                initial_user += f"=== RETRIEVED CONTEXT ===\n{context}\n\n"
            initial_user += f"=== USER QUESTION ===\n{user_prompt}"
            
            stages["initial"] = await self._generate_with_system(
                config=initial_config,
                system_prompt=initial_system,
                user_prompt=initial_user,
            )
            models_used["initial"] = f"{initial_config.backend_name}/{initial_config.model}"
        else:
            # No initial stage - shouldn't happen but handle gracefully
            stages["initial"] = ""
        
        # STAGE 2: Critique (optional) - NOW WITH SOURCE DOCUMENTS
        critique_config = self.pipeline.roles.get(PipelineRole.CRITIQUE)
        run_critique = (
            critique_config
            and critique_config.enabled
            and stages.get("initial")
            and (stage_limit is None or stage_limit >= 3)
        )
        if run_critique:
            # Include context so critique can verify claims against source documents
            critique_prompt = f"""You are a critical reviewer with access to the SOURCE DOCUMENTS.

Your job is to verify the response against the original documents, NOT just critique the logic.

=== SOURCE DOCUMENTS (from knowledge base) ===
{context[:16000] if context else '[No source documents provided]'}

=== ORIGINAL QUESTION ===
{user_prompt}

=== RESPONSE TO VERIFY ===
{stages['initial']}

=== YOUR CRITIQUE TASKS ===
1. **Factual Verification** - Does the response accurately reflect what's in the source documents? Flag any claims not supported by the sources.
2. **Missing Information** - Are there relevant details in the sources that weren't included but should be?
3. **Misinterpretations** - Did the initial response misread or oversimplify anything from the documents?
4. **Source Gaps** - If the question asks about something NOT covered in the sources, note this clearly.

Provide specific, document-referenced feedback. Quote sources where relevant."""

            stages["critique"] = await self.generate_single(
                backend_name=critique_config.backend_name,
                model=critique_config.model,
                prompt=critique_prompt,
                temperature=critique_config.temperature,
                max_tokens=critique_config.max_tokens,
                ollama_options=critique_config.ollama_options,
            )
            models_used["critique"] = f"{critique_config.backend_name}/{critique_config.model}"
        
        # STAGE 3: Synthesize - NOW WITH SOURCE DOCUMENTS
        synth_config = self.pipeline.roles.get(PipelineRole.SYNTHESIZE)
        run_synth = (
            synth_config
            and synth_config.enabled
            and (stage_limit is None or stage_limit >= 2)
        )
        if run_synth:
            if stages.get("critique"):
                # Full 3-stage synthesis with source documents for final verification
                synth_prompt = f"""Synthesize a final, authoritative response. Lead with the corrected answer. No preamble.

=== SOURCE DOCUMENTS (from knowledge base) ===
{context[:16000] if context else '[No source documents provided]'}

=== ORIGINAL QUESTION ===
{user_prompt}

=== INITIAL RESPONSE ===
{stages['initial']}

=== DOCUMENT-VERIFIED CRITIQUE ===
{stages['critique']}

=== SYNTHESIS RULES ===
1. **Source accuracy first** - Where the critique found errors, correct them against the source documents
2. **Incorporate gaps** - Add relevant details the critique identified from the sources
3. **Go deep where it matters** - Do not truncate useful information for brevity
4. **Connect dots** - Surface relationships between sources, implications, and context the user did not ask about but would benefit from
5. **Next steps** - End with concrete actions or follow-up questions
6. **State limits plainly** - If sources do not cover something, say so. Do not speculate.

The source documents are ground truth. Defer to them for facts."""
            else:
                # 2-stage: polish with source reference
                synth_prompt = f"""Refine this response for accuracy, depth, and clarity.

=== SOURCE DOCUMENTS ===
{context[:12000] if context else '[No sources]'}

=== RESPONSE TO REFINE ===
{stages['initial']}

Verify claims against sources. Expand where depth adds value. Flag anything unsupported. End with next steps if warranted."""

            stages["synthesize"] = await self.generate_single(
                backend_name=synth_config.backend_name,
                model=synth_config.model,
                prompt=synth_prompt,
                temperature=synth_config.temperature,
                max_tokens=synth_config.max_tokens,
                ollama_options=synth_config.ollama_options,
            )
            models_used["synthesize"] = f"{synth_config.backend_name}/{synth_config.model}"
            final = stages["synthesize"]
        else:
            # No synthesis stage - use initial (or critique if that's all we have)
            final = stages.get("critique") or stages.get("initial") or ""
        
        return {
            "final": final,
            "stages": stages,
            "models_used": models_used,
            "stage_limit_applied": stage_limit,
        }
    
    # =========================================================================
    # Configuration Persistence (for future GUI)
    # =========================================================================
    
    def to_config_dict(self) -> Dict[str, Any]:
        """Export current configuration as a dict (for JSON serialization)."""
        return {
            "backends": {
                name: {
                    "backend_type": cfg.backend_type.value,
                    "name": cfg.name,
                    "endpoint_url": cfg.endpoint_url,
                    # API keys are stored via services/secrets.py (keyring)
                    # and resolved from environment variables at load time
                    "api_key_ref": f"${{{name.upper()}_API_KEY}}",  # Environment var reference
                    "available_models": cfg.available_models,
                    "default_model": cfg.default_model,
                    "options": cfg.options,
                }
                for name, cfg in self.backends.items()
            },
            "pipeline": {
                "name": self.pipeline.name if self.pipeline else "default",
                "roles": {
                    role.value: {
                        "backend_name": cfg.backend_name,
                        "model": cfg.model,
                        "temperature": cfg.temperature,
                        "max_tokens": cfg.max_tokens,
                        "enabled": cfg.enabled,
                        "ollama_options": cfg.ollama_options,
                    }
                    for role, cfg in (self.pipeline.roles.items() if self.pipeline else {})
                }
            }
        }
    
    def save_config(self, path: Optional[Path] = None):
        """Save configuration to JSON file."""
        path = path or self.config_path
        if not path:
            raise ValueError("No config path specified")
        
        config = self.to_config_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
        logger.info(f"Saved model router config to {path}")
    
    @classmethod
    async def from_config_file(cls, path: Path) -> "ModelRouter":
        """Load router from a config file."""
        import os
        
        with open(path) as f:
            config = json.load(f)
        
        router = cls(config_path=path)
        
        # Register backends
        for name, backend_data in config.get("backends", {}).items():
            # Resolve API key from environment
            api_key_ref = backend_data.get("api_key_ref", "")
            api_key = None
            if api_key_ref.startswith("${") and api_key_ref.endswith("}"):
                env_var = api_key_ref[2:-1]
                api_key = os.getenv(env_var)
            
            backend_config = BackendConfig(
                backend_type=BackendType(backend_data["backend_type"]),
                name=backend_data["name"],
                api_key=api_key,
                endpoint_url=backend_data.get("endpoint_url"),
                available_models=backend_data.get("available_models", []),
                default_model=backend_data.get("default_model"),
                options=backend_data.get("options", {}),
            )
            await router.register_backend(backend_config)
        
        # Configure pipeline
        pipeline_data = config.get("pipeline", {})
        roles = {}
        for role_str, role_data in pipeline_data.get("roles", {}).items():
            role = PipelineRole(role_str)
            # Merge saved ollama_options with defaults so new keys are always present
            _default_ollama_opts = {
                "num_ctx": 8192,
                "repeat_penalty": 1.1,
                "top_k": 40,
                "top_p": 0.9,
                "stop": ["\n\nUser:", "\n\nHuman:", "---END---"],
            }
            saved_opts = role_data.get("ollama_options", {})
            merged_opts = {**_default_ollama_opts, **(saved_opts or {})}

            roles[role] = RoleConfig(
                role=role,
                backend_name=role_data["backend_name"],
                model=role_data["model"],
                temperature=role_data.get("temperature", 0.3),
                max_tokens=role_data.get("max_tokens", 4000),
                enabled=role_data.get("enabled", True),
                ollama_options=merged_opts,
            )
        
        timeouts_data = pipeline_data.get("timeouts", {})
        timeouts = LLMTimeouts(
            chat=timeouts_data.get("chat", 60.0),
            self_assessment=timeouts_data.get("self_assessment", 15.0),
            conversation_mining=timeouts_data.get("conversation_mining", 20.0),
            enrichment=timeouts_data.get("enrichment", 120.0),
            health_check=timeouts_data.get("health_check", 5.0),
            model_listing=timeouts_data.get("model_listing", 10.0),
            benchmark_per_model=timeouts_data.get("benchmark_per_model", 2700.0),
        )

        router.configure_pipeline(PipelineConfig(
            name=pipeline_data.get("name", "default"),
            roles=roles,
            timeouts=timeouts,
        ))
        
        return router
    
    async def close(self):
        """Clean up all client connections."""
        for client in self.clients.values():
            if hasattr(client, 'close'):
                await client.close()


# =============================================================================
# Convenience factory for quick setup
# =============================================================================

async def create_hybrid_router(
    openai_api_key: str,
    ollama_endpoint: str = "http://localhost:11434",
    cloud_model: str = "gpt-5.2",
    local_model: str = "",
) -> ModelRouter:
    """
    Quick setup for a hybrid local+cloud router.
    
    Default pipeline:
    - Initial: Local (fast, free)
    - Critique: Cloud (smart)
    - Synthesize: Cloud (polished)

    If *local_model* is not specified, picks the best installed local
    model from the dynamic catalog.
    """
    if not local_model:
        # Dynamic: pick the best installed model from the catalog
        try:
            from services.model_discovery import get_installed_model_names, get_model_catalog
            installed = await get_installed_model_names()
            installed_set = {m.replace(":latest", "") for m in installed}
            catalog = get_model_catalog()
            # Pick the largest general-purpose model that's actually installed
            candidates = [
                e for e in catalog
                if e.get("category") == "general" and e["model"] in installed_set
            ]
            if candidates:
                candidates.sort(key=lambda e: e.get("param_b", 0), reverse=True)
                local_model = candidates[0]["model"]
        except Exception as e:
            logger.warning("operation: suppressed %s", e)
        if not local_model:
            local_model = "llama4:scout"  # reasonable default

    router = ModelRouter()
    
    # Register OpenAI
    await router.register_backend(BackendConfig(
        backend_type=BackendType.OPENAI,
        name="openai",
        api_key=openai_api_key,
    ))
    
    # Register Ollama
    await router.register_backend(BackendConfig(
        backend_type=BackendType.OLLAMA,
        name="ollama",
        endpoint_url=ollama_endpoint,
    ))
    
    # Configure hybrid pipeline
    router.configure_pipeline(PipelineConfig(
        name="hybrid",
        roles={
            PipelineRole.INITIAL: RoleConfig(
                role=PipelineRole.INITIAL,
                backend_name="ollama",
                model=local_model,
                temperature=0.4,
            ),
            PipelineRole.CRITIQUE: RoleConfig(
                role=PipelineRole.CRITIQUE,
                backend_name="openai",
                model=cloud_model,
                temperature=0.2,
            ),
            PipelineRole.SYNTHESIZE: RoleConfig(
                role=PipelineRole.SYNTHESIZE,
                backend_name="openai",
                model=cloud_model,
                temperature=0.3,
            ),
        }
    ))
    
    return router
