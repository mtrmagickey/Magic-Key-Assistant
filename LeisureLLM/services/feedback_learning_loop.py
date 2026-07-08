"""
Feedback Learning Loop — closes the gap between user feedback and model behaviour.

This is the core **network effect moat**: each user's feedback makes the
system better for *all* users, creating a flywheel that accelerates with
adoption.

Capabilities:
    1. **Prompt refinement from feedback** — negative feedback on specific
       topics triggers automatic prompt adjustments (style, depth, format).
    2. **Retrieval quality scoring** — tracks which chunks lead to helpful
       vs. unhelpful answers; auto-deprioritises underperforming sources.
    3. **Feedback-driven knowledge gaps** — patterns of "not helpful" on a
       topic auto-create knowledge gaps with higher priority.
    4. **Anonymous improvement signals** — aggregates feedback into
       anonymised improvement hints that could (with opt-in) be shared
       across instances to improve the base experience.
    5. **A/B prompt testing** — tests prompt variations and automatically
       converges on the highest-performing variant.

Design:
    - All learning stays local by default (no cloud, no telemetry).
    - Opt-in aggregation produces anonymised signals only.
    - Feedback loop runs as a periodic job (not real-time).
    - Integrates with existing response_feedback table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Schema ─────────────────────────────────────────────────────

_CREATE_PROMPT_VARIANTS_SQL = """
CREATE TABLE IF NOT EXISTS prompt_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'system_prompt',
    prompt_text TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    total_uses INTEGER NOT NULL DEFAULT 0,
    helpful_count INTEGER NOT NULL DEFAULT 0,
    unhelpful_count INTEGER NOT NULL DEFAULT 0,
    helpfulness_rate REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    retired_at TEXT,
    UNIQUE(variant_name, category)
);
"""

_CREATE_CHUNK_QUALITY_SQL = """
CREATE TABLE IF NOT EXISTS chunk_quality_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL,
    source_path TEXT,
    times_retrieved INTEGER NOT NULL DEFAULT 0,
    helpful_retrievals INTEGER NOT NULL DEFAULT 0,
    unhelpful_retrievals INTEGER NOT NULL DEFAULT 0,
    quality_score REAL NOT NULL DEFAULT 0.5,
    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chunk_id)
);
"""

_CREATE_IMPROVEMENT_SIGNALS_SQL = """
CREATE TABLE IF NOT EXISTS improvement_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_type TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    signal_data TEXT NOT NULL DEFAULT '{}',
    anonymised INTEGER NOT NULL DEFAULT 1,
    opt_in_shared INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_chunk_quality_score
    ON chunk_quality_scores(quality_score);
CREATE INDEX IF NOT EXISTS idx_chunk_quality_id
    ON chunk_quality_scores(chunk_id);
CREATE INDEX IF NOT EXISTS idx_improvement_signals_type
    ON improvement_signals(signal_type, created_at DESC);
"""


class FeedbackLearningLoop:
    """
    Transforms user feedback into concrete system improvements.

    Usage::

        loop = FeedbackLearningLoop(db)
        await loop.ensure_tables()

        # After a feedback event:
        await loop.process_feedback(
            query="What's the pool maintenance schedule?",
            response="...",
            feedback="not_helpful",
            chunk_sources=["docs/maintenance.md#chunk_3"],
        )

        # Periodic job (e.g., nightly):
        improvements = await loop.run_learning_cycle()
    """

    def __init__(self, db: Any):
        self.db = db
        self._tables_ensured = False

    async def ensure_tables(self) -> None:
        if self._tables_ensured:
            return
        try:
            async with self.db.acquire() as conn:
                await conn.executescript(
                    _CREATE_PROMPT_VARIANTS_SQL
                    + _CREATE_CHUNK_QUALITY_SQL
                    + _CREATE_IMPROVEMENT_SIGNALS_SQL
                    + _CREATE_INDEX_SQL
                )
                await conn.commit()
            self._tables_ensured = True
        except Exception as exc:
            logger.warning("Failed to ensure feedback loop tables: %s", exc)

    # ── Feedback Processing ────────────────────────────────────

    async def process_feedback(
        self,
        query: str,
        response: str,
        feedback: str,  # "helpful" | "not_helpful"
        *,
        chunk_sources: Optional[List[str]] = None,
        improvement_detail: Optional[str] = None,
        variant_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process a single feedback event.

        Updates:
        - Chunk quality scores for all retrieved chunks
        - Prompt variant performance (if A/B testing is active)
        - Generates improvement signals for the learning cycle
        """
        await self.ensure_tables()
        actions: Dict[str, Any] = {}

        is_helpful = feedback == "helpful"

        # 1. Update chunk quality scores
        if chunk_sources:
            for source in chunk_sources:
                await self._update_chunk_quality(source, is_helpful)
            actions["chunks_updated"] = len(chunk_sources)

        # 2. Update prompt variant if applicable
        if variant_name:
            await self._update_variant_performance(variant_name, is_helpful)
            actions["variant_updated"] = variant_name

        # 3. Generate improvement signal
        signal = await self._generate_improvement_signal(
            query, response, feedback, improvement_detail
        )
        if signal:
            actions["signal_generated"] = signal

        # 4. Check if this topic has accumulated enough negative feedback
        #    to auto-create a knowledge gap
        if not is_helpful:
            gap_created = await self._check_feedback_gap_threshold(query)
            if gap_created:
                actions["gap_created"] = True

        return actions

    # ── Chunk Quality Tracking ─────────────────────────────────

    async def _update_chunk_quality(self, chunk_id: str, is_helpful: bool) -> None:
        """Update quality score for a retrieved chunk."""
        try:
            async with self.db.acquire() as conn:
                # Upsert chunk quality record
                await conn.execute(
                    """INSERT INTO chunk_quality_scores (chunk_id, times_retrieved, helpful_retrievals, unhelpful_retrievals)
                       VALUES (?, 1, ?, ?)
                       ON CONFLICT(chunk_id) DO UPDATE SET
                           times_retrieved = times_retrieved + 1,
                           helpful_retrievals = helpful_retrievals + ?,
                           unhelpful_retrievals = unhelpful_retrievals + ?,
                           last_updated = datetime('now')""",
                    (
                        chunk_id,
                        1 if is_helpful else 0,
                        0 if is_helpful else 1,
                        1 if is_helpful else 0,
                        0 if is_helpful else 1,
                    ),
                )

                # Recalculate quality score using Wilson score interval (lower bound)
                # This gives a pessimistic estimate that improves with more data
                async with conn.execute(
                    "SELECT helpful_retrievals, unhelpful_retrievals FROM chunk_quality_scores WHERE chunk_id = ?",
                    (chunk_id,),
                ) as cur:
                    row = await cur.fetchone()

                if row:
                    pos, neg = row[0] or 0, row[1] or 0
                    total = pos + neg
                    if total > 0:
                        # Wilson score lower bound (z=1.96 for 95% confidence)
                        z = 1.96
                        p_hat = pos / total
                        score = (
                            (p_hat + z * z / (2 * total)
                             - z * ((p_hat * (1 - p_hat) + z * z / (4 * total)) / total) ** 0.5)
                            / (1 + z * z / total)
                        )
                        score = max(0.0, min(1.0, score))
                    else:
                        score = 0.5

                    await conn.execute(
                        "UPDATE chunk_quality_scores SET quality_score = ? WHERE chunk_id = ?",
                        (round(score, 4), chunk_id),
                    )

                await conn.commit()
        except Exception as exc:
            logger.debug("Failed to update chunk quality for %s: %s", chunk_id, exc)

    async def get_low_quality_chunks(self, threshold: float = 0.3, min_retrievals: int = 3) -> List[Dict[str, Any]]:
        """Get chunks with consistently low quality scores."""
        await self.ensure_tables()
        try:
            return await self.db.fetch_dicts(
                """SELECT chunk_id, source_path, times_retrieved,
                              helpful_retrievals, unhelpful_retrievals, quality_score
                       FROM chunk_quality_scores
                       WHERE quality_score < ? AND times_retrieved >= ?
                       ORDER BY quality_score ASC""",
                threshold,
                min_retrievals,
            )
        except Exception as exc:
            logger.warning("Failed to get low quality chunks: %s", exc)
            return []

    async def get_high_quality_chunks(self, threshold: float = 0.8, min_retrievals: int = 3) -> List[Dict[str, Any]]:
        """Get chunks with consistently high quality scores."""
        await self.ensure_tables()
        try:
            return await self.db.fetch_dicts(
                """SELECT chunk_id, source_path, times_retrieved,
                              helpful_retrievals, unhelpful_retrievals, quality_score
                       FROM chunk_quality_scores
                       WHERE quality_score >= ? AND times_retrieved >= ?
                       ORDER BY quality_score DESC""",
                threshold,
                min_retrievals,
            )
        except Exception as exc:
            logger.warning("Failed to get high quality chunks: %s", exc)
            return []

    # ── Prompt A/B Testing ─────────────────────────────────────

    async def register_prompt_variant(
        self, variant_name: str, prompt_text: str, category: str = "system_prompt"
    ) -> None:
        """Register a new prompt variant for A/B testing."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO prompt_variants (variant_name, category, prompt_text)
                       VALUES (?, ?, ?)
                       ON CONFLICT(variant_name, category) DO UPDATE SET
                           prompt_text = excluded.prompt_text,
                           is_active = 1""",
                    (variant_name, category, prompt_text),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to register prompt variant: %s", exc)

    async def select_variant(self, category: str = "system_prompt") -> Optional[Tuple[str, str]]:
        """
        Select a prompt variant using Thompson Sampling.

        Returns (variant_name, prompt_text) or None if no variants exist.
        Thompson Sampling naturally balances exploration vs exploitation.
        """
        await self.ensure_tables()
        try:
            rows = await self.db.fetch_dicts(
                """SELECT variant_name, prompt_text, helpful_count, unhelpful_count
                       FROM prompt_variants
                       WHERE category = ? AND is_active = 1""",
                category,
            )

            if not rows:
                return None

            # Thompson Sampling: draw from Beta(helpful+1, unhelpful+1) for each variant
            best_score = -1.0
            best_variant = None
            for r in rows:
                alpha = (r["helpful_count"] or 0) + 1  # helpful + 1
                beta = (r["unhelpful_count"] or 0) + 1   # unhelpful + 1
                sample = random.betavariate(alpha, beta)
                if sample > best_score:
                    best_score = sample
                    best_variant = (r["variant_name"], r["prompt_text"])

            if best_variant:
                # Record usage
                await self.db.execute(
                    "UPDATE prompt_variants SET total_uses = total_uses + 1 WHERE variant_name = ? AND category = ?",
                    best_variant[0],
                    category,
                )
            return best_variant

        except Exception as exc:
            logger.warning("Failed to select variant: %s", exc)
            return None

    async def _update_variant_performance(self, variant_name: str, is_helpful: bool) -> None:
        """Update performance metrics for a prompt variant."""
        try:
            col = "helpful_count" if is_helpful else "unhelpful_count"
            await self.db.execute(
                f"""UPDATE prompt_variants
                SET {col} = {col} + 1,
                helpfulness_rate = CAST(helpful_count AS REAL) /
                NULLIF(helpful_count + unhelpful_count, 0)
                WHERE variant_name = ?""",
                variant_name,
            )
        except Exception as exc:
            logger.debug("Failed to update variant performance: %s", exc)

    async def retire_underperforming_variants(
        self, min_uses: int = 20, min_rate: float = 0.4
    ) -> List[str]:
        """Retire prompt variants with consistently low helpfulness rates."""
        await self.ensure_tables()
        retired: List[str] = []
        try:
            async with self.db.acquire() as conn:
                async with conn.execute(
                    """SELECT variant_name FROM prompt_variants
                       WHERE is_active = 1
                         AND total_uses >= ?
                         AND helpfulness_rate IS NOT NULL
                         AND helpfulness_rate < ?""",
                    (min_uses, min_rate),
                ) as cur:
                    rows = await cur.fetchall()

                for r in rows:
                    await conn.execute(
                        "UPDATE prompt_variants SET is_active = 0, retired_at = datetime('now') WHERE variant_name = ?",
                        (r[0],),
                    )
                    retired.append(r[0])
                    logger.info("Retired underperforming prompt variant: %s", r[0])

                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to retire variants: %s", exc)
        return retired

    # ── Improvement Signal Generation ──────────────────────────

    async def _generate_improvement_signal(
        self,
        query: str,
        response: str,
        feedback: str,
        detail: Optional[str] = None,
    ) -> Optional[str]:
        """Generate an anonymised improvement signal from feedback."""
        if feedback != "not_helpful":
            return None

        # Extract topic and failure mode (anonymised — no PII)
        signal_data = {
            "query_length": len(query.split()),
            "response_length": len(response.split()),
            "has_improvement_detail": bool(detail),
            "query_topic_hash": hashlib.sha256(
                " ".join(sorted(set(query.lower().split()[:5]))).encode()
            ).hexdigest()[:16],
        }

        # Classify failure mode
        if detail:
            detail_lower = detail.lower()
            if any(w in detail_lower for w in ["wrong", "incorrect", "inaccurate", "false"]):
                signal_data["failure_mode"] = "factual_error"
            elif any(w in detail_lower for w in ["missing", "didn't include", "left out"]):
                signal_data["failure_mode"] = "missing_info"
            elif any(w in detail_lower for w in ["confusing", "unclear", "hard to understand"]):
                signal_data["failure_mode"] = "clarity"
            elif any(w in detail_lower for w in ["too long", "verbose", "rambling"]):
                signal_data["failure_mode"] = "too_verbose"
            elif any(w in detail_lower for w in ["too short", "not enough", "brief"]):
                signal_data["failure_mode"] = "too_brief"
            else:
                signal_data["failure_mode"] = "unclassified"

        signal_type = signal_data.get("failure_mode", "negative_feedback")

        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO improvement_signals
                       (signal_type, signal_key, signal_data)
                       VALUES (?, ?, ?)""",
                    (signal_type, signal_data["query_topic_hash"], json.dumps(signal_data)),
                )
                await conn.commit()
            return signal_type
        except Exception as exc:
            logger.debug("Failed to generate improvement signal: %s", exc)
            return None

    async def _check_feedback_gap_threshold(self, query: str, threshold: int = 3) -> bool:
        """
        Check if negative feedback on a topic exceeds threshold.
        If so, auto-create a knowledge gap.

        Uses the topic hash (first 5 keywords, sorted, SHA-256) to group
        related queries.  A gap is only created once per hash; subsequent
        hits increment ``times_asked`` instead.
        """
        topic_hash = hashlib.sha256(
            " ".join(sorted(set(query.lower().split()[:5]))).encode()
        ).hexdigest()[:16]

        try:
            async with self.db.acquire() as conn:
                async with conn.execute(
                    """SELECT COUNT(*) FROM improvement_signals
                       WHERE signal_key = ?
                         AND created_at >= datetime('now', '-14 days')""",
                    (topic_hash,),
                ) as cur:
                    count = (await cur.fetchone())[0]

                if count < threshold:
                    return False

                # Truncated query as the gap question (first 200 chars)
                gap_question = query[:200]
                gap_topic = f"Feedback-detected gap: {query[:100]}"

                # Check if a gap already exists for this topic hash
                async with conn.execute(
                    """SELECT id, times_asked FROM knowledge_gaps
                       WHERE topic = ? AND status = 'open'
                       LIMIT 1""",
                    (gap_topic,),
                ) as cur:
                    existing = await cur.fetchone()

                if existing:
                    # Bump times_asked on the existing gap
                    await conn.execute(
                        """UPDATE knowledge_gaps
                           SET times_asked = ?,
                               last_asked = datetime('now'),
                               priority_score = ?
                           WHERE id = ?""",
                        (count, min(count * 10, 100), existing[0]),
                    )
                    await conn.commit()
                    return False  # gap exists, just updated

                # Auto-create gap using correct schema columns
                await conn.execute(
                    """INSERT OR IGNORE INTO knowledge_gaps
                       (topic, question, context, status, priority_score, times_asked)
                       VALUES (?, ?, ?, 'open', ?, ?)""",
                    (
                        gap_topic,
                        gap_question,
                        f"Auto-created from {count} negative feedback signals in 14 days. "
                        f"Topic hash: {topic_hash}",
                        min(count * 10, 100),  # priority 0-100
                        count,
                    ),
                )
                await conn.commit()
                logger.info(
                    "Auto-created knowledge gap from feedback pattern "
                    "(hash=%s, signals=%d): %s",
                    topic_hash, count, query[:60],
                )
                return True

        except Exception as exc:
            logger.debug("Failed to check feedback gap threshold: %s", exc)
            return False

    # ── Learning Cycle (periodic job) ──────────────────────────

    async def run_learning_cycle(self) -> Dict[str, Any]:
        """
        Run the feedback learning cycle.

        This is designed to run as a periodic job (e.g., nightly).
        It:
        1. Retires underperforming prompt variants
        2. Generates refined prompt variants from dominant failure modes
        3. Surfaces low-quality chunks for review
        4. Scans for feedback-driven knowledge gaps
        5. Generates aggregate improvement signals with recommendations
        6. Updates quality scores with decay
        """
        await self.ensure_tables()
        results: Dict[str, Any] = {}

        # 1. Retire bad variants
        retired = await self.retire_underperforming_variants()
        results["retired_variants"] = retired

        # 2. Generate refined prompts from failure patterns
        refined = await self._refine_prompts_from_feedback()
        results["prompt_refinements"] = refined

        # 3. Get chunk quality outliers
        low_quality = await self.get_low_quality_chunks()
        results["low_quality_chunks"] = len(low_quality)

        # 4. Scan for topics that warrant knowledge gaps
        gaps_created = await self._scan_for_feedback_gaps()
        results["gaps_created"] = gaps_created

        # 5. Aggregate improvement signals (full analysis)
        signals = await self._aggregate_signals()
        results["improvement_signals"] = signals

        # 6. Apply quality score decay (older feedback matters less)
        decayed = await self._apply_quality_decay()
        results["scores_decayed"] = decayed

        return results

    # ── Prompt Refinement from Feedback ────────────────────────

    # Rule-based refinement directives keyed by failure mode.
    # Each maps to a behavioural instruction appended to the system prompt.
    _FAILURE_MODE_DIRECTIVES: Dict[str, str] = {
        "factual_error": (
            "Users have reported factual inaccuracies.  When answering, "
            "cite the specific document and section that supports each claim.  "
            "If the knowledge base does not contain a direct answer, say so "
            "rather than inferring."
        ),
        "missing_info": (
            "Users have reported missing information.  Before finalising your "
            "answer, check whether any related documents cover adjacent aspects "
            "of the question.  Proactively surface related facts even if they "
            "were not explicitly asked for."
        ),
        "clarity": (
            "Users have reported unclear answers.  Structure your response with "
            "a one-sentence summary first, then supporting detail.  Use bullet "
            "points for multi-part answers and avoid jargon without definition."
        ),
        "too_verbose": (
            "Users have reported overly long answers.  Lead with the direct "
            "answer in 1-2 sentences.  Add supporting detail only when it "
            "changes the actionability of the answer.  Omit caveats that don't "
            "affect the user's decision."
        ),
        "too_brief": (
            "Users have reported insufficiently detailed answers.  After the "
            "direct answer, include relevant context: who is affected, what the "
            "deadline or constraint is, and what the recommended next step is.  "
            "Quote source documents when precision matters."
        ),
    }

    # Minimum signals before a failure mode triggers refinement
    _REFINEMENT_THRESHOLD = 5

    async def _refine_prompts_from_feedback(self, days: int = 30) -> List[Dict[str, str]]:
        """Generate new prompt variants that address dominant failure modes.

        For each failure mode that exceeds ``_REFINEMENT_THRESHOLD`` signals
        in the window, a new variant is created (or an existing one
        reactivated) that appends a targeted behavioural directive to the
        base system prompt.

        Returns a list of ``{"failure_mode": ..., "variant_name": ..., "action": ...}``
        entries describing what was created or already existed.
        """
        signals = await self._aggregate_signals(days)
        failure_modes = signals.get("by_failure_mode", {}) if isinstance(signals, dict) else {}
        if not failure_modes:
            return []

        results: List[Dict[str, str]] = []

        for mode, count in failure_modes.items():
            directive = self._FAILURE_MODE_DIRECTIVES.get(mode)
            if directive is None:
                continue
            if count < self._REFINEMENT_THRESHOLD:
                continue

            variant_name = f"feedback_refined_{mode}"
            category = "system_prompt_suffix"

            # Check if this variant already exists and is active
            try:
                rows = await self.db.fetch_dicts(
                    """SELECT id, is_active FROM prompt_variants
                       WHERE variant_name = ? AND category = ?""",
                    variant_name, category,
                )
            except Exception:
                rows = []

            if rows and rows[0].get("is_active"):
                results.append({
                    "failure_mode": mode,
                    "variant_name": variant_name,
                    "action": "already_active",
                })
                continue

            # Register or reactivate the variant
            await self.register_prompt_variant(variant_name, directive, category)
            results.append({
                "failure_mode": mode,
                "variant_name": variant_name,
                "action": "reactivated" if rows else "created",
                "signal_count": count,
            })
            logger.info(
                "Prompt variant '%s' %s from %d '%s' signals",
                variant_name,
                "reactivated" if rows else "created",
                count,
                mode,
            )

        return results

    async def get_active_prompt_suffix(self) -> Optional[str]:
        """Return the combined text of all active system-prompt suffix variants.

        Callers can append this to the base system prompt to apply
        feedback-driven behavioural refinements at generation time.
        Returns ``None`` when no suffix variants are active.
        """
        await self.ensure_tables()
        try:
            rows = await self.db.fetch_dicts(
                """SELECT prompt_text FROM prompt_variants
                   WHERE category = 'system_prompt_suffix'
                     AND is_active = 1
                   ORDER BY created_at""",
            )
            if not rows:
                return None
            return "\n\n".join(r["prompt_text"] for r in rows)
        except Exception:
            return None

    # ── Feedback-Driven Knowledge Gaps (batch scan) ────────────

    async def _scan_for_feedback_gaps(self, threshold: int = 3, days: int = 14) -> int:
        """Scan all recent topic hashes and create gaps where warranted.

        This is the batch counterpart of ``_check_feedback_gap_threshold``
        which runs inline during feedback ingestion.  The batch scan catches
        topics that accumulated signals across different queries that hash
        to the same key.

        Returns the number of new gaps created.
        """
        gaps_created = 0
        try:
            # Find topic hashes that exceed the threshold in the window
            rows = await self.db.fetch_dicts(
                """SELECT signal_key, COUNT(*) as cnt,
                          MIN(signal_data) as sample_data
                   FROM improvement_signals
                   WHERE created_at >= datetime('now', ? || ' days')
                   GROUP BY signal_key
                   HAVING cnt >= ?
                   ORDER BY cnt DESC""",
                f"-{days}", threshold,
            )

            for row in rows:
                topic_hash = row["signal_key"]
                count = row["cnt"]
                gap_topic = f"Feedback-detected gap (hash:{topic_hash[:8]})"

                # Check if gap already exists
                existing = await self.db.fetch_dicts(
                    """SELECT id FROM knowledge_gaps
                       WHERE topic = ? AND status = 'open' LIMIT 1""",
                    gap_topic,
                )
                if existing:
                    continue

                # Extract a representative query description from signal_data
                context_desc = f"Auto-created from {count} negative feedback signals in {days} days."
                try:
                    sample = json.loads(row.get("sample_data") or "{}")
                    fm = sample.get("failure_mode", "unclassified")
                    context_desc += f" Dominant failure mode: {fm}."
                except (json.JSONDecodeError, TypeError):
                    pass

                try:
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            """INSERT OR IGNORE INTO knowledge_gaps
                               (topic, question, context, status, priority_score, times_asked)
                               VALUES (?, ?, ?, 'open', ?, ?)""",
                            (
                                gap_topic,
                                f"Recurring negative feedback ({count} signals, hash {topic_hash[:8]})",
                                context_desc,
                                min(count * 10, 100),
                                count,
                            ),
                        )
                        await conn.commit()
                    gaps_created += 1
                    logger.info(
                        "Batch gap creation: hash=%s, signals=%d", topic_hash[:8], count,
                    )
                except Exception as exc:
                    logger.debug("Batch gap insert failed for hash %s: %s", topic_hash[:8], exc)

        except Exception as exc:
            logger.warning("Feedback gap scan failed: %s", exc)

        return gaps_created

    # ── Signal Aggregation ─────────────────────────────────────

    async def _aggregate_signals(self, days: int = 30) -> Dict[str, Any]:
        """Aggregate improvement signals into an actionable report.

        Returns a dict with:
        - ``by_failure_mode``:  {mode: count} breakdown
        - ``top_topics``:       most-signalled topic hashes with counts
        - ``chunk_correlation``: overlap between low-quality chunks and
          topics with high negative feedback
        - ``recommendations``:  plain-text action items
        """
        report: Dict[str, Any] = {
            "by_failure_mode": {},
            "top_topics": [],
            "chunk_correlation": {},
            "recommendations": [],
        }

        try:
            # Failure mode counts
            mode_rows = await self.db.fetch_dicts(
                """SELECT signal_type, COUNT(*) as count
                   FROM improvement_signals
                   WHERE created_at >= datetime('now', ? || ' days')
                   GROUP BY signal_type
                   ORDER BY count DESC""",
                f"-{days}",
            )
            report["by_failure_mode"] = {
                r["signal_type"]: r["count"] for r in mode_rows
            }

            # Top topics (by signal_key / topic hash)
            topic_rows = await self.db.fetch_dicts(
                """SELECT signal_key, COUNT(*) as count
                   FROM improvement_signals
                   WHERE created_at >= datetime('now', ? || ' days')
                   GROUP BY signal_key
                   HAVING count >= 2
                   ORDER BY count DESC
                   LIMIT 20""",
                f"-{days}",
            )
            report["top_topics"] = [
                {"topic_hash": r["signal_key"], "signal_count": r["count"]}
                for r in topic_rows
            ]

            # Chunk quality correlation: how many low-quality chunks are also
            # associated with high-signal topics
            low_chunks = await self.get_low_quality_chunks(threshold=0.35, min_retrievals=2)
            if low_chunks:
                report["chunk_correlation"] = {
                    "low_quality_chunk_count": len(low_chunks),
                    "sample_sources": [
                        c.get("source_path", c.get("chunk_id", ""))
                        for c in low_chunks[:5]
                    ],
                }

            # Generate recommendations
            recommendations: List[str] = []
            total_signals = sum(r["count"] for r in mode_rows) if mode_rows else 0

            if total_signals == 0:
                recommendations.append(f"No negative feedback signals in the last {days} days.")
            else:
                dominant = mode_rows[0] if mode_rows else None
                if dominant and dominant["count"] >= 5:
                    mode = dominant["signal_type"]
                    pct = round(100 * dominant["count"] / total_signals)
                    recommendations.append(
                        f"Dominant failure mode is '{mode}' ({pct}% of signals). "
                        f"A prompt variant has been created or is active to address this."
                    )

                if len(low_chunks) > 5:
                    recommendations.append(
                        f"{len(low_chunks)} chunks have low quality scores. "
                        f"Consider reviewing or re-ingesting the source documents."
                    )

                if len(topic_rows) > 5:
                    recommendations.append(
                        f"{len(topic_rows)} distinct topics have repeated negative feedback. "
                        f"Knowledge gaps have been created for topics exceeding the threshold."
                    )

                if report["by_failure_mode"].get("factual_error", 0) >= 3:
                    recommendations.append(
                        "Factual errors detected — audit source documents for accuracy."
                    )

            report["recommendations"] = recommendations

        except Exception as exc:
            logger.warning("Signal aggregation failed: %s", exc)

        return report

    async def _apply_quality_decay(self, decay_factor: float = 0.95) -> int:
        """
        Apply decay to quality scores so recent feedback matters more.

        Scores drift toward 0.5 (neutral) over time.
        """
        try:
            async with self.db.acquire() as conn:
                # Only decay scores that haven't been updated recently
                async with conn.execute(
                    """UPDATE chunk_quality_scores
                       SET quality_score = 0.5 + (quality_score - 0.5) * ?,
                           last_updated = datetime('now')
                       WHERE last_updated < datetime('now', '-7 days')
                         AND times_retrieved > 0""",
                    (decay_factor,),
                ) as cur:
                    count = cur.rowcount or 0
                await conn.commit()
            return count
        except Exception:
            return 0

    # ── Anonymised Signal Export (opt-in network effect) ───────

    async def export_anonymised_signals(self, days: int = 30) -> Dict[str, Any]:
        """
        Export anonymised improvement signals for potential sharing.

        This is the opt-in network effect: anonymised, aggregated
        insights that could improve the product for all users.

        Contains NO personally identifiable information, NO query text,
        NO response content — only statistical patterns.
        """
        full_signals = await self._aggregate_signals(days)
        # Extract just the mode counts for the export (backward compat)
        failure_modes = full_signals.get("by_failure_mode", {})

        try:
            async with self.db.acquire() as conn:
                # Aggregate chunk quality distribution
                async with conn.execute(
                    """SELECT
                        CASE
                            WHEN quality_score < 0.3 THEN 'low'
                            WHEN quality_score < 0.7 THEN 'medium'
                            ELSE 'high'
                        END as tier,
                        COUNT(*) as count
                       FROM chunk_quality_scores
                       WHERE times_retrieved >= 3
                       GROUP BY tier"""
                ) as cur:
                    quality_dist = {r[0]: r[1] for r in await cur.fetchall()}

                # Variant performance summary
                async with conn.execute(
                    """SELECT category, COUNT(*) as variants,
                              AVG(helpfulness_rate) as avg_rate
                       FROM prompt_variants
                       WHERE total_uses > 0
                       GROUP BY category"""
                ) as cur:
                    variant_summary = [dict(r) for r in await cur.fetchall()]

        except Exception:
            quality_dist = {}
            variant_summary = []

        return {
            "export_version": "1.0",
            "product": "magic_key_assistant",
            "exported_at": datetime.now().isoformat(),
            "period_days": days,
            "anonymised": True,
            "contains_pii": False,
            "failure_mode_distribution": failure_modes,
            "chunk_quality_distribution": quality_dist,
            "prompt_variant_summary": variant_summary,
        }
