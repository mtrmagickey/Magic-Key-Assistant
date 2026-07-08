"""Tavily search helper."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

try:
    from tavily import TavilyClient
except ImportError:  # pragma: no cover - optional dependency
    TavilyClient = None  # type: ignore


class TavilyService:
    """Async-friendly wrapper around Tavily's search API."""

    def __init__(self, api_key: Optional[str]) -> None:
        self._api_key = api_key
        self._client = TavilyClient(api_key=api_key) if api_key and TavilyClient else None

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    async def search(self, *, query: str, **kwargs: Any) -> Dict[str, Any]:
        if not self._client:
            raise RuntimeError("TavilyService not configured. Set TAVILY_API_KEY.")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._client.search(query=query, **kwargs))

    async def health_check(self) -> bool:
        if not self._client:
            return False
        loop = asyncio.get_running_loop()

        def _check() -> bool:
            try:
                # Lightweight metadata call by performing a no-op request with depth=basic and zero results
                self._client.search(query="ping", max_results=1, search_depth="basic")
                return True
            except Exception:
                return False

        return await loop.run_in_executor(None, _check)
