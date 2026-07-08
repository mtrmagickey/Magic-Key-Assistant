"""Lazy-instantiated service container."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict

from .llm_service import LLMService
from .tavily_service import TavilyService


@dataclass(slots=True)
class ServiceContainer:
    """Bundle of reusable services shared across cogs."""

    llm: LLMService
    tavily: TavilyService

    @classmethod
    def build(cls) -> "ServiceContainer":
        try:
            import config  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover - environment specific
            raise RuntimeError("config.py must be on PYTHONPATH to build services") from exc

        llm_service = LLMService(api_key=getattr(config, "gpt_key", "") or "")
        tavily_key = os.getenv("TAVILY_API_KEY")
        tavily_service = TavilyService(tavily_key)
        return cls(llm=llm_service, tavily=tavily_service)

    async def health_report(self) -> Dict[str, bool]:
        return {
            "llm": await self.llm.health_check(),
            "tavily": await self.tavily.health_check() if self.tavily.is_configured else False,
        }
