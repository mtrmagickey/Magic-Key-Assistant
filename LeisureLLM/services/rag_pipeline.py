"""
Shared RAG pipeline utilities.

Extracted from ``cogs/LLM.py`` so that both the web chat and the Discord cog
can import these helpers **without** pulling in ``discord.py`` or any
Discord-specific dependencies.

Functions here are pure infrastructure — no Discord types, no UI views.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate

from services.model_router import (
    BackendConfig,
    BackendType,
    ModelRouter,
    PipelineConfig,
    PipelineRole,
    RoleConfig,
)

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_LEISURELLM_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = _LEISURELLM_DIR / "prompts" / "system_prompt.txt"
OPERATIONAL_CTX_PATH = _LEISURELLM_DIR / "prompts" / "operational_context.txt"
ROUTER_CONFIG_PATH = _LEISURELLM_DIR / "config" / "model_router.json"


# ── Gap detection indicators (single source of truth) ────────────────────────

GAP_INDICATORS: list[str] = [
    "i don't have information",
    "i'm not sure",
    "i don't know",
    "i can't find",
    "no information about",
    "unclear from",
    "not documented",
    "no docs on that",
    "knowledge base doesn't cover",
    "don't have docs",
    "no relevant documents",
    "not in the knowledge base",
    "gap in the knowledge base",
]

SPARSE_CONTEXT_THRESHOLD = 50  # word count


# ── System prompt (loaded once at import time — no Discord deps) ─────────────

def _load_system_prompt() -> str:
    """Load and compile the system prompt template, injecting operational context."""
    try:
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            tpl = f.read()
    except Exception as e:
        logging.error("Failed to load system prompt from %s: %s", PROMPT_PATH, e)
        tpl = (
            "You are {bot_name}, an operations harness for small teams. "
            "Lead with the answer, cite sources, surface risks or alternatives, "
            "then suggest next steps. No filler, no sycophancy."
        )

    # Inject operational context
    try:
        if OPERATIONAL_CTX_PATH.exists():
            with open(OPERATIONAL_CTX_PATH, "r", encoding="utf-8") as f:
                op_ctx = f.read()
            if op_ctx and "--- RETRIEVED DOCUMENTS ---" in tpl:
                block = (
                    f"\n--- OPERATIONAL FACTS (rates, portfolio, contracts) ---\n"
                    f"{op_ctx[:4000]}\n\n"
                )
                tpl = tpl.replace(
                    "--- RETRIEVED DOCUMENTS ---",
                    block + "--- RETRIEVED DOCUMENTS ---",
                )
                logging.info("Operational context loaded and injected into system prompt")
    except Exception as e:
        logging.warning("Failed to load operational context: %s", e)

    # Static substitutions (not LangChain variables)
    tpl = tpl.replace("<<TODAY>>", datetime.now().strftime("%Y-%m-%d"))

    try:
        from core.config_loader import OrgProfile as _OrgProfile
        bot_name = _OrgProfile.load().bot_name
    except Exception:
        bot_name = "Magic Key Assistant"

    tpl = tpl.replace("{bot_name}", bot_name)
    return tpl


# Module-level template and prompt (loaded once, shared by all callers)
template: str = _load_system_prompt()
prompt: ChatPromptTemplate = ChatPromptTemplate.from_template(template)


def get_bot_name() -> str:
    """Return the resolved bot display name."""
    try:
        from core.config_loader import OrgProfile
        return OrgProfile.load().bot_name
    except Exception:
        return "Magic Key Assistant"


# ── Pipeline router singleton ────────────────────────────────────────────────

_pipeline_router: Optional[ModelRouter] = None
_pipeline_init_lock = asyncio.Lock()
_pipeline_initialized = False


async def get_pipeline_router() -> Optional[ModelRouter]:
    """Lazy-initialise and return the shared pipeline router.

    This is the **single** ``ModelRouter`` instance used by both the web
    chat and the Discord cog.  The admin server's ``startup_event`` may
    replace it via :func:`set_pipeline_router` once it has registered all
    cloud backends.
    """
    global _pipeline_router, _pipeline_initialized

    if _pipeline_initialized:
        return _pipeline_router

    async with _pipeline_init_lock:
        if _pipeline_initialized:
            return _pipeline_router

        _pipeline_initialized = True

        if not ROUTER_CONFIG_PATH.exists():
            logging.info("[Pipeline] No model_router.json found — using single-model fallback")
            return None

        try:
            with open(ROUTER_CONFIG_PATH) as f:
                config = json.load(f)
            pipeline_data = config.get("pipeline", config)

            router = ModelRouter()

            # Register backends — OpenAI if key present, Ollama if reachable
            gpt_key = os.environ.get("OPENAI_API_KEY", "")
            if gpt_key:
                ok = await router.register_backend(
                    BackendConfig(backend_type=BackendType.OPENAI, name="openai", api_key=gpt_key)
                )
                if ok:
                    logging.info("[Pipeline] OpenAI backend registered")

            try:
                ok = await router.register_backend(
                    BackendConfig(backend_type=BackendType.OLLAMA, name="ollama", endpoint_url="http://localhost:11434")
                )
                if ok:
                    logging.info("[Pipeline] Ollama backend registered")
            except Exception:
                logging.debug("[Pipeline] Ollama not available")

            # Load roles
            roles = _parse_pipeline_roles(pipeline_data, router)

            if roles:
                router.configure_pipeline(PipelineConfig(name=pipeline_data.get("name", "loaded"), roles=roles))
                _pipeline_router = router
                logging.info("[Pipeline] 3-phase pipeline loaded: %s", [r.value for r in roles])
            else:
                logging.warning("[Pipeline] No valid roles configured — using single-model fallback")

            return _pipeline_router

        except Exception as e:
            logging.error("[Pipeline] Failed to initialise: %s", e)
            return None


def set_pipeline_router(router: Optional[ModelRouter]) -> None:
    """Replace the shared pipeline router (called by admin server startup)."""
    global _pipeline_router, _pipeline_initialized
    _pipeline_router = router
    _pipeline_initialized = True


def _parse_pipeline_roles(
    pipeline_data: dict, router: ModelRouter
) -> dict[PipelineRole, RoleConfig]:
    """Parse pipeline roles from config dict, validating backends exist."""
    _default_ollama_opts = {
        "num_ctx": 16384,
        "repeat_penalty": 1.1,
        "top_k": 40,
        "top_p": 0.9,
        "stop": ["\n\nUser:", "\n\nHuman:", "---END---"],
    }

    roles: dict[PipelineRole, RoleConfig] = {}
    for role_str, role_data in pipeline_data.get("roles", {}).items():
        if not role_data.get("enabled", True):
            continue

        backend_name = role_data.get("backend_name")
        if backend_name and backend_name not in router.backends:
            logging.warning("[Pipeline] Skipping role '%s' — backend '%s' not available", role_str, backend_name)
            continue

        saved_opts = role_data.get("ollama_options", {})
        merged_opts = {**_default_ollama_opts, **(saved_opts or {})}

        role = PipelineRole(role_str)
        roles[role] = RoleConfig(
            role=role,
            backend_name=role_data["backend_name"],
            model=role_data["model"],
            temperature=role_data.get("temperature", 0.3),
            max_tokens=role_data.get("max_tokens", 4000),
            system_prompt_override=role_data.get("system_prompt_override"),
            enabled=True,
            ollama_options=merged_opts,
        )
    return roles


# ── Document helpers ─────────────────────────────────────────────────────────


def coerce_document(entry) -> Optional[Document]:
    """Convert arbitrary retriever output into a LangChain Document."""
    if entry is None:
        return None
    if isinstance(entry, Document):
        return entry
    if isinstance(entry, dict):
        page_content = entry.get("page_content") or entry.get("content") or entry.get("text") or ""
        metadata = entry.get("metadata") or {}
        return Document(page_content=page_content, metadata=metadata)
    if hasattr(entry, "page_content") and hasattr(entry, "metadata"):
        try:
            return Document(page_content=entry.page_content, metadata=entry.metadata)
        except Exception as e:
            logger.warning("coerce_document: suppressed %s", e)
    return Document(page_content=str(entry), metadata={})


def run_retriever_query(retriever, question: str) -> list[Document]:
    """Return relevant documents from a retriever regardless of API shape."""
    if retriever is None:
        return []
    try:
        if hasattr(retriever, "get_relevant_documents"):
            docs = retriever.get_relevant_documents(question)
        elif hasattr(retriever, "invoke"):
            docs = retriever.invoke(question)
        elif callable(retriever):
            docs = retriever(question)
        else:
            logger.warning("Retriever object has no supported query method; skipping context fetch.")
            return []
    except Exception as exc:
        logger.error("Retriever query failed: %s", exc)
        return []

    if docs is None:
        return []
    if isinstance(docs, Document):
        return [docs]
    if isinstance(docs, dict):
        for key in ("documents", "docs", "result", "context"):
            if key in docs and docs[key]:
                docs = docs[key]
                break
        if isinstance(docs, Document):
            return [docs]
    if not isinstance(docs, list):
        try:
            docs = list(docs)
        except TypeError:
            docs = [docs]
    normalized = []
    for entry in docs:
        doc = coerce_document(entry)
        if doc and doc.page_content:
            normalized.append(doc)
    return normalized


def _normalized_source_path(meta: dict[str, Any]) -> str:
    src = meta.get("source_relpath") or meta.get("source") or ""
    return str(src).replace("\\", "/").lower()


def _effective_source_priority(meta: dict[str, Any]) -> int:
    priority = meta.get("source_priority")
    if isinstance(priority, (int, float)):
        return int(priority)

    kind = str(meta.get("source_kind") or "").lower()
    if kind == "generated":
        return -2
    if kind:
        return 1

    path = _normalized_source_path(meta)
    if path.startswith("docs/"):
        path = "/" + path

    if any(seg in path for seg in ("/docs/admin_answers/", "/docs/web_inbox/", "/docs/knowledge/", "/docs/onboarding/")):
        return 2
    if "/docs/memos/" in path:
        return -2
    if "/docs/interview/" in path:
        return -1
    if path.endswith("_lines.txt") or path.endswith("_lines.md"):
        return 2
    if "/docs/" in path:
        return 1
    return 0


def _source_quality_band(meta: dict[str, Any]) -> int:
    kind = str(meta.get("source_kind") or "").lower()
    doc_type = str(meta.get("doc_type") or "").lower()
    path = _normalized_source_path(meta)
    if path.startswith("docs/"):
        path = "/" + path
    priority = _effective_source_priority(meta)

    if doc_type == "web_cache":
        return -3
    if kind == "generated" or priority < 0 or "/docs/memos/" in path:
        return -2
    if doc_type == "discord_export" or path.endswith("_lines.txt") or path.endswith("_lines.md"):
        return 1
    if doc_type == "human_knowledge":
        return 4
    if any(seg in path for seg in ("/docs/admin_answers/", "/docs/web_inbox/", "/docs/knowledge/", "/docs/onboarding/")):
        return 4
    if kind == "primary" or priority >= 2:
        return 3
    if "/docs/" in path or doc_type == "doc":
        return 3
    return 2


def _effective_actionability(doc: Document) -> float:
    meta = doc.metadata or {}
    raw = meta.get("llm_actionability", 0.5)
    conf = meta.get("llm_confidence", 0.5)
    return raw * conf + 0.5 * (1 - conf)


def _filtered_doc_sort_key(doc: Document) -> tuple[Any, ...]:
    meta = doc.metadata or {}
    retrieval_score = meta.get("retrieval_score")
    retrieval_rank = float(retrieval_score) if isinstance(retrieval_score, (int, float)) else 999.0
    return (
        -_source_quality_band(meta),
        -_effective_actionability(doc),
        retrieval_rank,
        -_effective_source_priority(meta),
    )


def _doc_dedupe_key(doc: Document) -> tuple[str, str]:
    meta = doc.metadata or {}
    path = _normalized_source_path(meta)
    content = (doc.page_content or "").strip()
    return path, content


def count_trusted_candidates(docs: list[Document]) -> int:
    return sum(1 for doc in docs if _source_quality_band(doc.metadata or {}) >= 3)


def promote_trusted_candidates(
    docs: list[Document],
    supplemental_docs: list[Document],
    *,
    minimum_trusted: int = 6,
    max_total_docs: int = 30,
) -> list[Document]:
    result = list(docs)
    trusted_count = count_trusted_candidates(result)
    if trusted_count >= minimum_trusted:
        return sorted(result, key=_filtered_doc_sort_key)

    seen = {_doc_dedupe_key(doc) for doc in result}
    supplemental_sorted = sorted(supplemental_docs, key=_filtered_doc_sort_key)
    for doc in supplemental_sorted:
        if len(result) >= max_total_docs or trusted_count >= minimum_trusted:
            break
        if _source_quality_band(doc.metadata or {}) < 3:
            continue
        key = _doc_dedupe_key(doc)
        if key in seen:
            continue
        result.append(doc)
        seen.add(key)
        trusted_count += 1

    return sorted(result, key=_filtered_doc_sort_key)


def _extract_doc_date_value(doc: Document) -> str:
    import re as _re

    meta = doc.metadata or {}
    date_str = meta.get("doc_date") or meta.get("file_modified") or ""
    if date_str:
        return str(date_str)

    matches = _re.findall(r"\[(\d{1,2}/\d{1,2}/\d{4})\s", (doc.page_content or "")[:1000])
    if matches:
        try:
            parsed = [datetime.strptime(match, "%m/%d/%Y") for match in matches]
            return max(parsed).strftime("%Y-%m-%d")
        except Exception:
            return ""
    return ""


def _doc_context_sort_key(indexed_doc: tuple[int, Document]) -> tuple[Any, ...]:
    index, doc = indexed_doc
    meta = doc.metadata or {}
    source_priority = _effective_source_priority(meta)
    rerank_score = meta.get("rerank_score")
    retrieval_score = meta.get("retrieval_score")
    date_value = _extract_doc_date_value(doc)

    rerank_rank = -float(rerank_score) if isinstance(rerank_score, (int, float)) else 0.0
    retrieval_rank = float(retrieval_score) if isinstance(retrieval_score, (int, float)) else 999.0
    date_rank = -int(date_value.replace("-", "")) if date_value else 0

    return (
        -source_priority,
        rerank_rank,
        retrieval_rank,
        date_rank,
        index,
    )


def filter_superseded_docs(docs: list[Document]) -> list[Document]:
    """Filter out superseded documents, prioritise primary sources.

    Uses enriched metadata when available:
    - Drops chunks tagged as 'noise' by the enrichment LLM (only if confidence >= 0.3)
    - Sorts by actionability score (high → low) within each tier,
      weighted by LLM confidence to avoid trusting low-quality enrichments

    No arbitrary document count cap — the downstream ``format_docs_for_context``
    enforces a character budget (default 18 000 chars) which is the real guard
    against context-window overflow.  Capping by count before that point just
    discards potentially relevant documents while the character budget still
    has room.
    """
    current_docs = []
    superseded_docs = []
    noise_docs = []
    for doc in docs:
        metadata = doc.metadata or {}
        if metadata.get("status", "") == "superseded":
            superseded_docs.append(doc)
        elif (
            metadata.get("llm_content_type") == "noise"
            and metadata.get("llm_confidence", 0.5) >= 0.3
        ):
            noise_docs.append(doc)
        else:
            current_docs.append(doc)

    trusted_docs = []
    support_docs = []
    demoted_docs = []
    for doc in current_docs:
        meta = doc.metadata or {}
        quality_band = _source_quality_band(meta)
        if quality_band >= 3:
            trusted_docs.append(doc)
        elif quality_band >= 1:
            support_docs.append(doc)
        else:
            demoted_docs.append(doc)

    trusted_docs.sort(key=_filtered_doc_sort_key)
    support_docs.sort(key=_filtered_doc_sort_key)
    demoted_docs.sort(key=_filtered_doc_sort_key)

    support_allowance = len(support_docs)
    if trusted_docs:
        support_allowance = min(len(support_docs), max(0, 6 - len(trusted_docs)))

    result = trusted_docs + support_docs[:support_allowance]
    if result:
        demoted_allowance = max(0, 8 - len(result))
        if trusted_docs:
            demoted_allowance = min(demoted_allowance, 2)
        if demoted_allowance:
            result.extend(demoted_docs[:demoted_allowance])
    else:
        result = demoted_docs

    # Backfill with superseded/noise only when primary results are thin
    if len(result) < 5 and superseded_docs:
        result.extend(superseded_docs)
    if len(result) < 3 and noise_docs:
        result.extend(noise_docs[:6])

    logger.info(
        "Filtered retrieval: %d trusted, %d support, %d demoted, %d superseded, %d noise -> returning %d",
        len(trusted_docs),
        len(support_docs),
        len(demoted_docs),
        len(superseded_docs),
        len(noise_docs),
        len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Freshness gate — warn when docs are stale for recency-sensitive questions (#6)
# ---------------------------------------------------------------------------
_RECENCY_SIGNALS = [
    "latest", "current", "recent", "today", "this year", "this month",
    "this week", "right now", "at the moment", "as of", "up to date",
    "2025", "2026", "has anything changed", "new rules", "new guidance",
    "most recent", "latest update", "what's new", "any changes",
]

_FRESHNESS_THRESHOLD_DAYS = 180  # 6 months


def build_freshness_warning(question: str, docs: list[Document]) -> str:
    """Return a context-injection warning when docs are old and question asks
    about recent/current information.

    Returns empty string when either:
    - The question has no recency signal, OR
    - At least one doc is newer than _FRESHNESS_THRESHOLD_DAYS
    """
    if not docs:
        return ""

    low = question.lower()
    has_recency = any(sig in low for sig in _RECENCY_SIGNALS)
    if not has_recency:
        return ""

    import re as _re

    def _extract_date(doc):
        meta = doc.metadata or {}
        d = meta.get("doc_date") or meta.get("file_modified") or ""
        if d:
            return d
        matches = _re.findall(
            r"\[(\d{1,2}/\d{1,2}/\d{4})\s",
            (doc.page_content or "")[:1000],
        )
        if matches:
            try:
                parsed = [datetime.strptime(m, "%m/%d/%Y") for m in matches]
                return max(parsed).strftime("%Y-%m-%d")
            except Exception as e:
                logger.warning("_extract_date: suppressed %s", e)
        return ""

    dates = []
    for doc in docs:
        d = _extract_date(doc)
        if d:
            try:
                dates.append(datetime.strptime(d[:10], "%Y-%m-%d"))
            except Exception as e:
                logger.warning("_extract_date: suppressed %s", e)

    if not dates:
        # No dates on any doc — can't determine freshness, skip
        return ""

    newest = max(dates)
    age_days = (datetime.now() - newest).days
    if age_days <= _FRESHNESS_THRESHOLD_DAYS:
        return ""

    months = age_days // 30
    return (
        f"[FRESHNESS WARNING: The user is asking about current/recent information, "
        f"but the newest retrieved document is ~{months} months old. "
        f"Prioritise web search results and general knowledge for up-to-date answers. "
        f"If citing these documents, note that they may be outdated.]"
    )


def format_docs_for_context(docs: list[Document], max_chars: int = 28000) -> str:
    """Format retrieved docs with source headers for grounded answers.

    Prefers higher-trust sources first using existing source metadata, then
    keeps reranked relevance ahead of pure recency. Date remains a tie-breaker,
    and is still included in the [DOC] header when available.
    """
    if not docs:
        return ""

    sorted_docs = [doc for _, doc in sorted(enumerate(docs), key=_doc_context_sort_key)]

    parts, used = [], 0
    for idx, doc in enumerate(sorted_docs, start=1):
        meta = doc.metadata or {}
        src = meta.get("source_relpath") or meta.get("source") or "unknown-source"
        doc_type = meta.get("llm_content_type") or meta.get("doc_type") or "doc"
        status = meta.get("status") or ""
        date_str = (
            meta.get("llm_date_range")
            or meta.get("doc_date")
            or meta.get("file_modified")
            or _extract_doc_date_value(doc)
        )
        header = f"[DOC {idx}] source={src} type={doc_type}"
        if date_str:
            header += f" date={date_str}"
        if status:
            header += f" status={status}"

        summary = meta.get("llm_summary", "")
        confidence = meta.get("llm_confidence", 0.5)
        body = (doc.page_content or "").strip()
        if summary and confidence >= 0.3:
            body = f"[Summary (AI-extracted): {summary}]\n{body}"
        elif summary:
            body = f"[Summary (low confidence): {summary}]\n{body}"

        block = header + "\n" + body
        if used + len(block) > max_chars:
            remaining = max(0, max_chars - used)
            if remaining > len(header) + 50:
                parts.append((header + "\n" + body)[:remaining].rstrip())
            break
        parts.append(block)
        used += len(block)
    return "\n\n---\n\n".join(parts)


def extract_source_citations(docs: list[Document]) -> list[dict]:
    """Extract source metadata from docs for citation display."""
    seen: set[str] = set()
    sources: list[dict] = []
    for doc in docs:
        meta = doc.metadata or {}
        src = meta.get("source_relpath") or meta.get("source") or ""
        if not src or src in seen:
            continue
        seen.add(src)
        sources.append(
            {
                "name": os.path.basename(src) if src else "Unknown",
                "path": src,
                "type": meta.get("doc_type") or "document",
            }
        )
    return sources


def detect_knowledge_gap(
    reply_text: str,
    context_word_count: int,
) -> bool:
    """Return True if the reply indicates a knowledge gap."""
    reply_lower = reply_text.lower()
    has_indicator = any(ind in reply_lower for ind in GAP_INDICATORS)
    sparse = context_word_count < SPARSE_CONTEXT_THRESHOLD
    return has_indicator or sparse
