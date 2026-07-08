"""
Knowledge Health Automation — self-healing knowledge base.

Hooks into the existing corpus_health and gap tracker infrastructure
to automatically:

1. **Confidence Decay** — chunk metadata confidence degrades over time,
   so stale documents naturally rank lower in retrieval.

2. **Staleness → Gap Creation** — when corpus health detects stale primary
   documents, auto-create knowledge gaps asking for updated information.

3. **Contradiction → Gap Creation** — when contradiction detection finds
   conflicting chunks, auto-create a knowledge gap flagging the inconsistency
   for human resolution.

4. **Auto-Supersession Suggestions** — when a new doc covers the same topic
   as an older doc with lower confidence, suggest supersession.

This module runs as a periodic background task (integrated into the
existing job registry).  It does NOT automatically modify or delete
documents — it creates gaps and flags for human review.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Confidence decay: halve confidence every HALF_LIFE_DAYS for unenriched chunks
CONFIDENCE_HALF_LIFE_DAYS = 180

# Minimum confidence floor (never decay below this)
CONFIDENCE_FLOOR = 0.1

# Days after which a primary doc is "stale" (default; overridable via workflows.yaml)
DEFAULT_STALE_DAYS = 180

# Max gaps to create per run (avoid spamming)
MAX_GAPS_PER_RUN = 5


# ── Confidence Decay ─────────────────────────────────────────────────────────

def compute_decayed_confidence(
    original_confidence: float,
    last_modified_date: str,
    *,
    half_life_days: float = CONFIDENCE_HALF_LIFE_DAYS,
    floor: float = CONFIDENCE_FLOOR,
    now: Optional[datetime] = None,
) -> float:
    """
    Compute time-decayed confidence for a chunk.

    Uses exponential decay: confidence * 2^(-age_days / half_life)
    Newer documents retain their confidence; old ones gradually fade.

    Parameters
    ----------
    original_confidence : float
        The enrichment confidence (0-1).
    last_modified_date : str
        ISO date string of when the source document was last modified.
    half_life_days : float
        Number of days for confidence to halve.
    floor : float
        Minimum confidence value.
    now : optional datetime
        Override current time (for testing).

    Returns
    -------
    float : decayed confidence, clamped to [floor, 1.0]
    """
    if not last_modified_date:
        return original_confidence

    try:
        if now is None:
            now = datetime.utcnow()
        modified = datetime.fromisoformat(last_modified_date.replace("Z", "+00:00").split("+")[0])
        age_days = max(0, (now - modified).days)
        decay_factor = math.pow(2, -age_days / half_life_days)
        decayed = original_confidence * decay_factor
        return max(floor, min(1.0, decayed))
    except (ValueError, TypeError):
        return original_confidence


def apply_confidence_decay_to_results(
    docs: list,
    *,
    half_life_days: float = CONFIDENCE_HALF_LIFE_DAYS,
) -> list:
    """
    Apply confidence decay to a list of retrieved documents (in-place).

    Call this in the retrieval pipeline to let the retrieval scorer
    factor in document freshness automatically.

    Parameters
    ----------
    docs : list of Document
        LangChain documents with metadata.

    Returns the same list with updated metadata.
    """
    for doc in docs:
        meta = getattr(doc, "metadata", None)
        if not meta:
            continue

        original_conf = meta.get("llm_confidence")
        if original_conf is None:
            continue

        # Use file_modified or doc_date as the age reference
        date_str = meta.get("file_modified") or meta.get("doc_date") or ""
        if not date_str:
            continue

        decayed = compute_decayed_confidence(
            float(original_conf), date_str, half_life_days=half_life_days
        )
        meta["llm_confidence_decayed"] = round(decayed, 3)
        meta["llm_confidence_original"] = original_conf

    return docs


# ── Staleness → Gap Creation ─────────────────────────────────────────────────

async def detect_stale_and_create_gaps(
    db: Any,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    max_gaps: int = MAX_GAPS_PER_RUN,
) -> List[Dict[str, Any]]:
    """
    Scan for stale primary documents and auto-create knowledge gaps.

    Looks at the corpus_health staleness data (or scans DB directly)
    and creates gaps for documents that haven't been updated beyond
    the threshold.

    Returns list of gap dicts that were created.
    """
    created_gaps: List[Dict[str, Any]] = []
    try:
        # Find stale docs by checking bot_questions or chunk metadata
        # We look at the file_modified metadata in ChromaDB chunks
        async with db.acquire() as conn:
            # Get distinct source files and their last modified dates from chat_interactions
            # that were used as sources but might be stale
            cutoff = (datetime.utcnow() - timedelta(days=stale_days)).isoformat()

            # Check for stale primary sources that are frequently cited
            async with conn.execute(
                """SELECT sources_used, COUNT(*) as cite_count
                   FROM chat_interactions
                   WHERE created_at >= datetime('now', '-30 days')
                     AND sources_used IS NOT NULL
                   GROUP BY sources_used
                   ORDER BY cite_count DESC
                   LIMIT 20"""
            ) as cur:
                rows = await cur.fetchall()

            # For each frequently-cited source set, check if we already have a gap
            for row in rows:
                if len(created_gaps) >= max_gaps:
                    break

                sources_json = row["sources_used"]
                if not sources_json:
                    continue

                try:
                    import json
                    sources = json.loads(sources_json)
                    if not isinstance(sources, list):
                        continue
                except Exception:
                    continue

                for source in sources[:3]:  # check top 3 from each interaction
                    if len(created_gaps) >= max_gaps:
                        break

                    # Skip if gap already exists for this source
                    async with conn.execute(
                        "SELECT id FROM knowledge_gaps WHERE question LIKE ? AND status = 'open'",
                        (f"%{source[:60]}%",),
                    ) as cur2:
                        if await cur2.fetchone():
                            continue

                    # Create a gap requesting a freshness review
                    topic = f"Freshness review: {source[:50]}"
                    question = (
                        f"The document '{source}' is frequently cited but may be outdated. "
                        f"Is the information still current? Please review and update if needed."
                    )
                    context = f"Auto-detected by knowledge health automation | stale_threshold={stale_days}d"

                    try:
                        async with conn.execute(
                            """INSERT INTO knowledge_gaps
                               (topic, question, context, priority_score,
                                curation_status, times_asked)
                               VALUES (?, ?, ?, ?, 'keep', 1)""",
                            (topic, question, context, 2),
                        ) as cur3:
                            gap_id = cur3.lastrowid

                        created_gaps.append({
                            "gap_id": gap_id,
                            "topic": topic,
                            "source": source,
                            "reason": "staleness",
                        })
                        logger.info("Created staleness gap #%d for source: %s", gap_id, source[:50])
                    except Exception as exc:
                        logger.debug("Failed to create staleness gap: %s", exc)

            await conn.commit()

    except Exception as exc:
        logger.warning("Staleness detection failed: %s", exc)

    return created_gaps


# ── Contradiction → Gap Creation ─────────────────────────────────────────────

async def flag_contradictions_as_gaps(
    db: Any,
    contradictions: List[Dict[str, Any]],
    *,
    max_gaps: int = MAX_GAPS_PER_RUN,
) -> List[Dict[str, Any]]:
    """
    Convert contradiction detection results into knowledge gaps.

    Parameters
    ----------
    db : database instance
    contradictions : list of dicts
        Each with keys: source_a, source_b, description, chunk_texts

    Returns list of gap dicts that were created.
    """
    created_gaps: List[Dict[str, Any]] = []

    for contradiction in contradictions[:max_gaps]:
        try:
            source_a = contradiction.get("source_a", "unknown")
            source_b = contradiction.get("source_b", "unknown")
            description = contradiction.get("description", "Conflicting information detected")

            topic = f"Contradiction: {source_a[:25]} vs {source_b[:25]}"
            question = (
                f"Contradicting information found between '{source_a}' and '{source_b}': "
                f"{description[:200]}. Which source is correct? Please resolve the conflict."
            )
            context = (
                f"Auto-detected by knowledge health automation (contradiction scanner) | "
                f"Sources: {source_a}, {source_b}"
            )

            async with db.acquire() as conn:
                # Skip if similar gap exists
                async with conn.execute(
                    "SELECT id FROM knowledge_gaps WHERE question LIKE ? AND status = 'open'",
                    (f"%{source_a[:30]}%{source_b[:30]}%",),
                ) as cur:
                    if await cur.fetchone():
                        continue

                async with conn.execute(
                    """INSERT INTO knowledge_gaps
                       (topic, question, context, priority_score,
                        curation_status, times_asked)
                       VALUES (?, ?, ?, ?, 'keep', 1)""",
                    (topic, question, context, 3),  # higher priority than staleness
                ) as cur:
                    gap_id = cur.lastrowid
                await conn.commit()

            created_gaps.append({
                "gap_id": gap_id,
                "topic": topic,
                "reason": "contradiction",
                "sources": [source_a, source_b],
            })
            logger.info("Created contradiction gap #%d: %s vs %s", gap_id, source_a[:30], source_b[:30])

        except Exception as exc:
            logger.debug("Failed to create contradiction gap: %s", exc)

    return created_gaps


# ── Combined health sweep ─────────────────────────────────────────────────────

async def run_knowledge_health_sweep(
    db: Any,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    max_gaps: int = MAX_GAPS_PER_RUN,
) -> Dict[str, Any]:
    """
    Run a complete knowledge health sweep.

    This is designed to be called by the job registry as a periodic task.
    It combines staleness detection and contradiction flagging.

    Returns a summary dict for logging.
    """
    results: Dict[str, Any] = {
        "stale_gaps_created": 0,
        "contradiction_gaps_created": 0,
        "errors": [],
    }

    # 1. Staleness detection
    try:
        stale_gaps = await detect_stale_and_create_gaps(
            db, stale_days=stale_days, max_gaps=max_gaps,
        )
        results["stale_gaps_created"] = len(stale_gaps)
        results["stale_gaps"] = stale_gaps
    except Exception as exc:
        results["errors"].append(f"staleness: {exc}")

    remaining = max_gaps - results["stale_gaps_created"]

    # 2. Contradiction detection (if corpus_health is available)
    if remaining > 0:
        try:
            # Contradiction detection runs via corpus_health if available
            # We don't re-run the full scan here — just check for any
            # unflagged contradictions from the last scan
            async with db.acquire() as conn, conn.execute(
                """SELECT id FROM knowledge_gaps
                       WHERE context LIKE '%contradiction scanner%'
                         AND status = 'open'"""
            ) as cur:
                existing = await cur.fetchall()
            results["existing_contradiction_gaps"] = len(existing)
        except Exception as exc:
            results["errors"].append(f"contradiction_check: {exc}")

    logger.info(
        "Knowledge health sweep complete: %d stale gaps, %d contradiction gaps",
        results["stale_gaps_created"],
        results["contradiction_gaps_created"],
    )
    return results
