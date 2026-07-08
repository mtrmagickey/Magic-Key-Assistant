"""Centralized LLM utilities for the bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Retry configuration for transient failures
RETRY_CONFIG = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class LLMService:
    """Wrapper that routes through the shared *ModelRouter* when available,
    falling back to a direct ``ChatOpenAI`` client when no router is
    configured (e.g. tests, first-run bootstrap).

    This ensures that personas, background jobs, and one-off completions
    all honour the user's model configuration rather than being hard-coded
    to OpenAI ``gpt-4o-mini``.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model_name: str = "gpt-4o-mini",
        temperature: float = 0.2,
    ) -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._temperature = temperature
        # Lazy-init: only created when actually needed (fallback path)
        self._client: Optional[Any] = None

    # ── Private helpers ──────────────────────────────────────────────────

    def _ensure_openai_client(self):
        """Build the fallback ChatOpenAI client on first use."""
        if self._client is None:
            if not self._api_key:
                raise ValueError(
                    "LLMService has no ModelRouter and no OpenAI API key — "
                    "configure at least one backend."
                )
            from langchain_openai import ChatOpenAI
            self._client = ChatOpenAI(
                model=self._model_name,
                api_key=self._api_key,
                temperature=self._temperature,
            )
        return self._client

    @staticmethod
    async def _get_router():
        """Try to fetch the shared pipeline router (may be ``None``)."""
        try:
            from services.rag_pipeline import get_pipeline_router
            return await get_pipeline_router()
        except Exception:
            return None

    @staticmethod
    def _pick_backend(router) -> tuple[Optional[str], Optional[str]]:
        """Choose the best backend/model pair from the router.

        Strategy:
        1. Use the pipeline's INITIAL role (that's the user's configured
           "default" model).
        2. Fall back to the first registered backend + its first model.
        3. Return (None, None) if nothing is available.
        """
        try:
            if router.pipeline and router.pipeline.roles:
                from services.model_router import PipelineRole
                initial = router.pipeline.roles.get(PipelineRole.INITIAL)
                if initial:
                    return initial.backend_name, initial.model
            # Fallback: first available backend
            for name, backend in router.backends.items():
                models = getattr(backend, "available_models", [])
                if models:
                    return name, models[0]
        except Exception as e:
            logger.warning("_pick_backend: suppressed %s", e)
        return None, None

    # ── Public API ───────────────────────────────────────────────────────

    @retry(**RETRY_CONFIG)
    async def generate(
        self,
        prompt: ChatPromptTemplate,
        variables: Dict[str, Any],
        temperature: float | None = None,
    ) -> str:
        """Run a prompt template with provided variables and return text."""
        router = await self._get_router()
        if router:
            rendered = (await prompt.ainvoke(variables)).to_string()
            # Use fallback chain — tries all registered backends in priority order
            try:
                return await router.generate_with_fallback(
                    prompt=rendered,
                    temperature=temperature or self._temperature,
                )
            except RuntimeError:
                pass  # All backends failed — fall through to direct OpenAI
        # Fallback: direct OpenAI
        client = self._ensure_openai_client()
        if temperature is not None:
            client = client.bind(temperature=temperature)
        chain = prompt | client | StrOutputParser()
        return await chain.ainvoke(variables)

    @retry(**RETRY_CONFIG)
    async def complete(
        self,
        prompt_text: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Execute a raw string prompt."""
        router = await self._get_router()
        if router:
            # Use fallback chain — tries all registered backends in priority order
            try:
                return await router.generate_with_fallback(
                    prompt=prompt_text,
                    temperature=temperature or self._temperature,
                    max_tokens=max_tokens or 4000,
                )
            except RuntimeError:
                pass  # All backends failed — fall through to direct OpenAI
        # Fallback: direct OpenAI
        client = self._ensure_openai_client()
        bind_params: Dict[str, Any] = {}
        if max_tokens is not None:
            bind_params["max_tokens"] = max_tokens
        if temperature is not None:
            bind_params["temperature"] = temperature
        if bind_params:
            client = client.bind(**bind_params)
        prompt = ChatPromptTemplate.from_template("{text}")
        chain = prompt | client | StrOutputParser()
        return await chain.ainvoke({"text": prompt_text})

    async def health_check(self) -> bool:
        """Ping model metadata to ensure the client is initialised."""
        router = await self._get_router()
        if router:
            return bool(router.backends)

        loop = asyncio.get_running_loop()

        def _check() -> bool:
            try:
                return bool(self._ensure_openai_client().model)
            except Exception:
                return False

        return await loop.run_in_executor(None, _check)
