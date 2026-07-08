"""Service layer for LeisureLLM bot.

Heavy dependencies (langchain, openai, etc.) are imported lazily so the admin
server can start without pulling in the full LLM stack.
"""


def __getattr__(name: str):
    """Lazy-import public symbols on first access."""
    if name == "ServiceContainer":
        from .container import ServiceContainer

        return ServiceContainer
    if name == "LLMService":
        from .llm_service import LLMService

        return LLMService
    if name == "TavilyService":
        from .tavily_service import TavilyService

        return TavilyService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LLMService",
    "TavilyService",
    "ServiceContainer",
]
