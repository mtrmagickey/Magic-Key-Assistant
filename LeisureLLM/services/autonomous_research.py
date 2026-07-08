"""
Autonomous Research Service
============================

Fires immediately when a knowledge gap is detected, researching the topic
via web search and auto-ingesting the result into the corpus — no human
approval required for web-sourced content with real citations.

Also provides auto-close logic: when a previously-gapped question gets
a high-confidence answer, the gap is automatically resolved.

This service is the bridge between *detecting* a gap and *closing* it,
removing the 24-hour delay of waiting for the Curator daily scan.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Minimum confidence score to consider a gap "resolved" by a good answer
_AUTO_CLOSE_CONFIDENCE = 8

# ── Immediate Gap Research ────────────────────────────────────────────────────


async def research_gap_immediately(
    topic: str,
    question: str,
    missing_knowledge: str = "",
    *,
    bot: Any = None,
    db: Any = None,
) -> Optional[Path]:
    """Research a knowledge gap RIGHT NOW via web search.

    Called as a fire-and-forget background task when self-assessment
    detects a low-confidence answer.  If Tavily is available, searches
    the web, drafts a memo, and auto-approves it into the corpus.

    Parameters
    ----------
    topic : str
        The gap topic (short label).
    question : str
        The full user question that triggered the gap.
    missing_knowledge : str
        What the self-assessment said was missing.
    bot : optional
        Discord bot instance (for accessing service_container + cogs).
    db : optional
        Database connection pool (for standalone usage without bot).

    Returns
    -------
    Path or None
        Path to the created+ingested memo, or None if research failed.
    """
    try:
        # Load config
        from core.config_loader import WorkflowConfig
        wf = WorkflowConfig.load()
        if not wf.cq_immediate_gap_research:
            return None

        # Get Tavily service
        tavily = None
        if bot:
            sc = getattr(bot, "service_container", None)
            tavily = getattr(sc, "tavily", None) if sc else None

        if not tavily or not getattr(tavily, "is_configured", False):
            logger.debug("Immediate gap research skipped: Tavily not available")
            return None

        # Research the topic
        from services.web_research import research_knowledge_gap

        # Get LLM service for synthesis
        llm_service = None
        if bot:
            sc = getattr(bot, "service_container", None)
            llm_service = (
                getattr(sc, "llm", None) or getattr(sc, "llm_service", None)
                if sc else None
            )

        research_query = question
        if missing_knowledge:
            research_query = f"{question} — specifically: {missing_knowledge}"

        draft = await research_knowledge_gap(
            tavily,
            topic,
            research_query,
            llm_service=llm_service,
            max_results=5,
        )

        if not draft or len(draft) < 80:
            logger.debug("Immediate research yielded insufficient content for: %s", topic)
            return None

        # Save the memo
        filepath = _save_research_memo(topic, question, draft)
        if not filepath:
            return None

        # Auto-approve and ingest (web-sourced content with real citations)
        if wf.cq_auto_approve_web_research:
            if bot:
                doc_author = bot.get_cog("DocumentAuthor")
                if doc_author:
                    approved = await doc_author.auto_approve_memo(filepath)
                    if approved:
                        logger.info(
                            "Immediate gap research: auto-approved %s for topic '%s'",
                            filepath, topic,
                        )
                        return filepath
            else:
                # Standalone mode: flip status directly and trigger ingest
                approved = await _standalone_auto_approve(filepath)
                if approved:
                    return filepath

        logger.info(
            "Immediate gap research saved (needs_review): %s for topic '%s'",
            filepath, topic,
        )
        return filepath

    except Exception as exc:
        logger.warning("Immediate gap research failed for '%s': %s", topic, exc)
        return None


def _save_research_memo(topic: str, question: str, content: str) -> Optional[Path]:
    """Save a web-research draft to the memos folder."""
    try:
        docs_root = Path(__file__).resolve().parent.parent / "docs" / "memos"
        now = datetime.now()
        date_dir = docs_root / str(now.year) / f"{now.month:02d}"
        date_dir.mkdir(parents=True, exist_ok=True)

        slug = re.sub(r"[^a-z0-9_]+", "_", topic.lower().replace(" ", "_"))[:40]
        slug = f"auto_research_{slug}"
        filename = f"{now.day:02d}_{slug}.md"
        filepath = date_dir / filename

        # Don't overwrite existing research
        if filepath.exists():
            filepath = date_dir / f"{now.day:02d}_{slug}_{now.strftime('%H%M')}.md"

        frontmatter = (
            f"---\n"
            f"doc_type: auto_research\n"
            f"topic: {topic}\n"
            f"source: immediate_gap_research\n"
            f"auto_generated: true\n"
            f"generated_at: {now.isoformat()}\n"
            f"status: needs_review\n"
            f"original_question: {question[:200]}\n"
            f"---\n\n"
        )

        full_content = frontmatter + content
        filepath.write_text(full_content, encoding="utf-8")
        logger.info("Saved immediate research memo: %s", filepath)
        return filepath

    except Exception as exc:
        logger.error("Failed to save research memo: %s", exc)
        return None


async def _standalone_auto_approve(filepath: Path) -> bool:
    """Auto-approve a memo without needing the DocumentAuthor cog."""
    try:
        content = filepath.read_text(encoding="utf-8")
        if "status: needs_review" not in content:
            return False

        updated = content.replace(
            "status: needs_review",
            f"status: auto_approved\nauto_approved_at: {datetime.now().isoformat()}",
            1,
        )
        filepath.write_text(updated, encoding="utf-8")

        # Trigger ingest
        from cogs.ingest_metadata import run_ingest
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_ingest)
        logger.info("Standalone auto-approved and ingested: %s", filepath)
        return True
    except Exception as exc:
        logger.warning("Standalone auto-approve failed: %s", exc)
        return False


# ── Auto-Close Gaps ───────────────────────────────────────────────────────────


async def maybe_auto_close_gap(
    question: str,
    confidence: int,
    grounded: bool,
    *,
    db: Any = None,
    bot: Any = None,
) -> bool:
    """Auto-close a matching open knowledge gap if the answer is now high-quality.

    Called after self-assessment passes with high confidence. If we previously
    logged a gap for this question but can now answer it well, the gap is
    automatically resolved.

    Parameters
    ----------
    question : str
        The user's question.
    confidence : int
        Self-assessment confidence score (1-10).
    grounded : bool
        Whether the answer is grounded in retrieved context.
    db : optional
        Database connection pool.
    bot : optional
        Discord bot instance (for DB access).

    Returns
    -------
    bool
        True if a gap was auto-closed.
    """
    if confidence < _AUTO_CLOSE_CONFIDENCE or not grounded:
        return False

    try:
        from core.config_loader import WorkflowConfig
        wf = WorkflowConfig.load()
        if not wf.cq_auto_close_resolved_gaps:
            return False
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    # Get DB
    if not db and bot:
        db = getattr(bot, "db", None)
    if not db:
        return False

    try:
        async with db.acquire() as conn:
            # Find open gaps that match this question
            async with conn.execute(
                """SELECT id, topic, times_asked
                   FROM knowledge_gaps
                   WHERE status = 'open'
                     AND question = ?
                   LIMIT 1""",
                (question,),
            ) as cur:
                row = await cur.fetchone()

            if not row:
                # Try fuzzy match: same question words might be phrased differently
                # Use the first 60 chars as a prefix match
                prefix = question[:60].replace("'", "''")
                async with conn.execute(
                    """SELECT id, topic, times_asked
                       FROM knowledge_gaps
                       WHERE status = 'open'
                         AND question LIKE ?
                       LIMIT 1""",
                    (f"{prefix}%",),
                ) as cur:
                    row = await cur.fetchone()

            if not row:
                return False

            gap_id, topic, times_asked = row[0], row[1], row[2]

            # Auto-close the gap
            await conn.execute(
                """UPDATE knowledge_gaps
                   SET status = 'resolved',
                       resolved_at = datetime('now'),
                       resolved_via = 'auto_close_high_confidence',
                       notes = COALESCE(notes, '') || ?
                   WHERE id = ?""",
                (
                    f"\n[Auto-closed {datetime.now().strftime('%Y-%m-%d %H:%M')}] "
                    f"Confidence {confidence}/10, grounded={grounded}",
                    gap_id,
                ),
            )
            await conn.commit()

            logger.info(
                "Auto-closed knowledge gap #%d '%s' (confidence %d/10, asked %d times)",
                gap_id, topic, confidence, times_asked,
            )

            return True

    except Exception as exc:
        logger.warning("Auto-close gap failed: %s", exc)
        return False


# ── Cache Web Search Results ──────────────────────────────────────────────────

# Prevent re-caching the same topic within this many seconds
_CACHE_DEDUP_WINDOW_SECONDS = 3600  # 1 hour


async def cache_web_result(
    question: str,
    web_block: str,
    *,
    bot: Any = None,
) -> Optional[Path]:
    """Cache web search results into the corpus as a memo.

    Called after ``chat_web_augment()`` returns useful content, so the
    *next* time someone asks a similar question the answer is served
    from the local knowledge base instead of hitting the web again.

    The memo is auto-approved and ingested immediately so it appears
    in retrieval results right away.

    Parameters
    ----------
    question : str
        The user's original question.
    web_block : str
        The raw ``[Web Search Results]`` block returned by
        ``chat_web_augment()``.
    bot : optional
        Discord bot instance (for DocumentAuthor auto-approve + ingest).

    Returns
    -------
    Path or None
        Path to the cached memo, or None if caching was skipped/failed.
    """
    if not web_block or len(web_block) < 40:
        return None

    try:
        from core.config_loader import WorkflowConfig

        wf = WorkflowConfig.load()
        if not wf.cq_auto_approve_web_research:
            # If auto-approve is off, skip caching entirely — the user
            # doesn't want unsupervised corpus changes.
            return None
    except Exception:
        pass  # Proceed with caching anyway

    # ── Slug-based deduplication ─────────────────────────────────────
    topic_words = [
        w for w in question.lower().split()
        if len(w) > 3 and w not in {"what", "when", "where", "which", "does", "have", "this", "that", "with", "from", "about", "much", "many", "their", "they", "your", "there"}
    ][:6]
    slug = re.sub(r"[^a-z0-9_]+", "_", "_".join(topic_words))[:50]
    if not slug:
        slug = "web_cache"

    docs_root = Path(__file__).resolve().parent.parent / "docs" / "memos"
    now = datetime.now()
    date_dir = docs_root / str(now.year) / f"{now.month:02d}"
    date_dir.mkdir(parents=True, exist_ok=True)

    # Check for recent duplicate (same slug written in the last hour)
    filename_prefix = f"{now.day:02d}_web_cache_{slug}"
    for existing in date_dir.glob(f"*_web_cache_{slug}*"):
        try:
            age = now.timestamp() - existing.stat().st_mtime
            if age < _CACHE_DEDUP_WINDOW_SECONDS:
                logger.debug(
                    "Web result cache dedup: %s already cached %ds ago",
                    slug, int(age),
                )
                return None
        except OSError as e:
            logger.warning("operation: suppressed %s", e)

    filepath = date_dir / f"{filename_prefix}.md"
    if filepath.exists():
        filepath = date_dir / f"{filename_prefix}_{now.strftime('%H%M')}.md"

    # ── Build memo content ───────────────────────────────────────────
    frontmatter = (
        f"---\n"
        f"doc_type: web_cache\n"
        f"topic: {question[:200]}\n"
        f"source: chat_web_augment\n"
        f"auto_generated: true\n"
        f"generated_at: {now.isoformat()}\n"
        f"status: auto_approved\n"
        f"auto_approved_at: {now.isoformat()}\n"
        f"---\n\n"
    )

    # Reformat the web block into a more readable memo
    body_lines = [f"# Web Search: {question[:120]}\n"]
    for line in web_block.splitlines():
        if line.startswith("• "):
            body_lines.append(f"- {line[2:]}")
        elif line.startswith("[Web Search Results]"):
            continue  # Skip header
        else:
            body_lines.append(line)
    body_lines.append(
        f"\n\n*Cached from web search on {now.strftime('%Y-%m-%d %H:%M')}*"
    )

    full_content = frontmatter + "\n".join(body_lines)
    filepath.write_text(full_content, encoding="utf-8")
    logger.info("Cached web result: %s (%d chars)", filepath.name, len(web_block))

    # ── Trigger ingest so the memo shows up in retrieval immediately ──
    try:
        if bot:
            doc_author = bot.get_cog("DocumentAuthor")
            if doc_author:
                await doc_author.auto_approve_memo(filepath)
                logger.info("Web cache auto-approved + ingested: %s", filepath.name)
                return filepath

        # Standalone: trigger ingest directly
        from cogs.ingest_metadata import run_ingest

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_ingest)
        logger.info("Web cache ingested (standalone): %s", filepath.name)
    except Exception as exc:
        logger.debug("Web cache ingest skipped (non-fatal): %s", exc)

    return filepath
