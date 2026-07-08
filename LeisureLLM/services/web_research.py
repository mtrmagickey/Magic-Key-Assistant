"""
Web Research Service
=====================
Reusable helpers for web-augmented workflows.  Wraps TavilyService with
higher-level patterns used across the codebase:

- **Chat augmentation**: search the web when RAG retrieval is sparse and inject
  results into the prompt context.
- **Topic research**: given a topic, run a multi-query sweep and synthesise
  findings into a structured brief.
- **Gap research**: auto-research a knowledge gap and draft an answer for review.
- **Citation enrichment**: attach web-sourced references to an existing document.

All public methods accept a ``TavilyService`` (may be unconfigured) and degrade
gracefully — callers never need to guard on ``is_configured`` themselves.

Safety defaults:
    - max 3 queries per call (adjustable)
    - 1.5 s sleep between queries
    - results are snippet-capped and provenance-tagged
    - no raw HTML; only Tavily-extracted text
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class WebSnippet:
    """A single web result, normalised for internal use."""
    title: str
    url: str
    snippet: str
    score: float = 0.0
    source_query: str = ""


@dataclass
class WebResearchBrief:
    """Structured output from a multi-query research sweep."""
    topic: str
    snippets: List[WebSnippet] = field(default_factory=list)
    summary: str = ""              # LLM-synthesised if llm_service provided
    queries_used: List[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.snippets)

    def as_context_block(self, max_chars: int = 3000) -> str:
        """Format as a context block suitable for injection into an LLM prompt."""
        if not self.snippets:
            return ""
        lines = [f"[Web Research — {self.topic}]"]
        chars = len(lines[0])
        for s in self.snippets:
            entry = f"• {s.title} ({s.url}): {s.snippet}"
            if chars + len(entry) + 1 > max_chars:
                break
            lines.append(entry)
            chars += len(entry) + 1
        return "\n".join(lines)


# ── Core helpers ─────────────────────────────────────────────────────────────

async def _tavily_search_safe(
    tavily,
    query: str,
    *,
    max_results: int = 5,
    search_depth: str = "basic",
    **kwargs: Any,
) -> List[WebSnippet]:
    """Run a single Tavily search, returning [] on any failure."""
    if not tavily or not getattr(tavily, "is_configured", False):
        return []
    try:
        raw = await tavily.search(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            **kwargs,
        )
        results = raw.get("results", []) if isinstance(raw, dict) else []
        return [
            WebSnippet(
                title=(r.get("title") or "")[:200],
                url=(r.get("url") or ""),
                snippet=(r.get("content") or "")[:500],
                score=float(r.get("score", 0)),
                source_query=query,
            )
            for r in results
        ]
    except Exception as exc:
        logger.debug("Tavily search failed for %r: %s", query, exc)
        return []


# ── Public API ───────────────────────────────────────────────────────────────

async def chat_web_augment(
    tavily,
    question: str,
    *,
    max_results: int = 4,
) -> str:
    """Search the web for a user question and return a context block.

    Designed to be called from the chat pipeline when RAG retrieval is sparse.
    Returns an empty string when Tavily is unconfigured or search yields nothing
    — callers can just concatenate the result into their existing context.
    """
    snippets = await _tavily_search_safe(
        tavily,
        question,
        max_results=max_results,
        search_depth="basic",
    )
    if not snippets:
        return ""

    lines = ["[Web Search Results]"]
    for s in snippets:
        lines.append(f"• {s.title} ({s.url}): {s.snippet[:300]}")

    return "\n".join(lines)


async def research_topic(
    tavily,
    topic: str,
    *,
    llm_service=None,
    max_queries: int = 3,
    max_results_per_query: int = 4,
    search_depth: str = "advanced",
    extra_queries: Optional[List[str]] = None,
) -> WebResearchBrief:
    """Run a multi-query web research sweep on a topic.

    If ``llm_service`` is provided, generates varied search queries and
    synthesises findings into a paragraph.  Without it, falls back to
    simple keyword variants.

    Returns a ``WebResearchBrief`` (possibly empty if Tavily is unconfigured).
    """
    brief = WebResearchBrief(topic=topic)

    if not tavily or not getattr(tavily, "is_configured", False):
        return brief

    # ── Build query list ─────────────────────────────────────────────────
    queries: List[str] = []
    if extra_queries:
        queries.extend(extra_queries[:max_queries])

    if llm_service and len(queries) < max_queries:
        try:
            prompt = (
                f"Generate {max_queries - len(queries)} distinct web search queries "
                f"to research the following topic thoroughly. Each query should target "
                f"a different angle (facts, standards, best practices, recent news).\n\n"
                f"Topic: {topic}\n\n"
                f"Return ONLY the queries, one per line, no numbering."
            )
            result = await llm_service.complete(prompt, max_tokens=200, temperature=0.4)
            for line in result.strip().splitlines():
                q = line.strip().lstrip("0123456789.-) ")
                if len(q) > 10:
                    queries.append(q)
                if len(queries) >= max_queries:
                    break
        except Exception as exc:
            logger.debug("LLM query generation failed: %s", exc)

    # Fallback: use the topic itself
    if not queries:
        queries = [topic]

    # ── Execute searches ─────────────────────────────────────────────────
    seen_urls: set = set()
    for query in queries[:max_queries]:
        snippets = await _tavily_search_safe(
            tavily,
            query,
            max_results=max_results_per_query,
            search_depth=search_depth,
        )
        for s in snippets:
            if s.url not in seen_urls:
                seen_urls.add(s.url)
                brief.snippets.append(s)
        brief.queries_used.append(query)
        await asyncio.sleep(1.5)  # rate-limit

    # ── Optional LLM synthesis ───────────────────────────────────────────
    if llm_service and brief.snippets:
        try:
            context = "\n".join(
                f"- {s.title}: {s.snippet[:300]}" for s in brief.snippets[:8]
            )
            prompt = (
                f"Based on these web search results about \"{topic}\", write a concise "
                f"factual summary (3-5 sentences). Include specific facts, numbers, or "
                f"dates where available. Do NOT invent information not present in the "
                f"results.\n\nSearch Results:\n{context}"
            )
            brief.summary = await llm_service.complete(prompt, max_tokens=300, temperature=0.2)
        except Exception as exc:
            logger.debug("LLM synthesis failed: %s", exc)

    return brief


async def research_knowledge_gap(
    tavily,
    topic: str,
    question: str,
    *,
    llm_service=None,
    max_results: int = 5,
) -> Optional[str]:
    """Research a knowledge gap via web search and draft an answer.

    Returns a draft answer string suitable for saving as a needs_review memo,
    or None if Tavily is unconfigured or nothing useful was found.

    The draft is clearly marked as web-sourced and includes citations.
    """
    if not tavily or not getattr(tavily, "is_configured", False):
        return None

    # Search with the actual question + topic
    queries = [question]
    if topic.lower() not in question.lower():
        queries.append(f"{topic} {question.split('?')[0]}")

    all_snippets: List[WebSnippet] = []
    seen_urls: set = set()
    for q in queries[:2]:
        snippets = await _tavily_search_safe(
            tavily, q, max_results=max_results, search_depth="advanced",
        )
        for s in snippets:
            if s.url not in seen_urls:
                seen_urls.add(s.url)
                all_snippets.append(s)
        await asyncio.sleep(1.5)

    if not all_snippets:
        return None

    if not llm_service:
        # Without LLM, return raw snippets as a draft
        lines = [
            f"# Web Research: {topic}",
            f"\n**Question:** {question}\n",
            "**Sources found:**\n",
        ]
        for s in all_snippets[:5]:
            lines.append(f"- [{s.title}]({s.url}): {s.snippet[:300]}")
        lines.append("\n*Auto-researched from web — needs human review and verification.*")
        return "\n".join(lines)

    # With LLM, synthesise a proper draft answer
    context = "\n".join(
        f"Source: {s.title} ({s.url})\n{s.snippet}" for s in all_snippets[:6]
    )
    prompt = (
        f"You are a knowledge curator. A question was asked that our internal knowledge "
        f"base couldn't answer. Web research found these results.\n\n"
        f"Topic: {topic}\n"
        f"Question: {question}\n\n"
        f"Web Results:\n{context}\n\n"
        f"Write a clear, factual answer based ONLY on what the web results say. "
        f"Rules:\n"
        f"1. Do NOT invent facts not present in the sources\n"
        f"2. Include inline citations as [Source Title](URL)\n"
        f"3. Note any contradictions or uncertainties\n"
        f"4. End with a 'Sources' section listing all URLs used\n"
        f"5. Start with '# {topic}' as the title\n"
        f"6. Keep it concise — 150-300 words\n"
    )
    try:
        draft = await llm_service.complete(prompt, max_tokens=600, temperature=0.2)
        draft += "\n\n*Auto-researched from web — needs human review and verification.*"
        return draft
    except Exception as exc:
        logger.debug("LLM gap-research synthesis failed: %s", exc)
        return None


async def enrich_synthesis_with_web(
    tavily,
    topic: str,
    existing_content: str,
    *,
    llm_service=None,
    max_results: int = 4,
) -> str:
    """Search the web for additional context on a topic and return an addendum.

    Designed to be appended to an existing synthesis memo to provide external
    citations and broader context.  Returns empty string if nothing useful found.
    """
    if not tavily or not getattr(tavily, "is_configured", False):
        return ""

    snippets = await _tavily_search_safe(
        tavily,
        f"{topic} best practices standards",
        max_results=max_results,
        search_depth="basic",
    )
    if not snippets:
        return ""

    if not llm_service:
        lines = ["\n\n## External References (Web)\n"]
        for s in snippets[:4]:
            lines.append(f"- [{s.title}]({s.url}): {s.snippet[:200]}")
        return "\n".join(lines)

    context = "\n".join(f"- {s.title} ({s.url}): {s.snippet[:300]}" for s in snippets[:4])
    prompt = (
        f"You are enriching an internal document about \"{topic}\" with external context.\n\n"
        f"Here is the existing document (excerpt):\n{existing_content[:1500]}\n\n"
        f"Here are relevant web results:\n{context}\n\n"
        f"Write a short section titled '## External References' that:\n"
        f"1. Adds 2-4 bullet points of relevant external context with citations\n"
        f"2. Notes any industry standards or best practices that apply\n"
        f"3. Does NOT repeat information already in the document\n"
        f"4. Each bullet includes a [Source](URL) citation\n"
        f"Keep it under 150 words."
    )
    try:
        addendum = await llm_service.complete(prompt, max_tokens=300, temperature=0.2)
        if addendum and len(addendum) > 30:
            return "\n\n" + addendum
    except Exception as exc:
        logger.debug("Web enrichment synthesis failed: %s", exc)
    return ""
