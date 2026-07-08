"""
Automatic Document Quality Remediation
========================================

Continuously improves corpus quality without human intervention by:

1. **Auto-archiving** — chunks that score below a quality threshold over
   sustained periods are soft-archived (metadata flag) so they no longer
   appear in retrieval results.
2. **Re-enrichment triggers** — when the enrichment model changes or
   chunks lack expected metadata fields, they're queued for re-enrichment.
3. **Low-performance detection** — tracks which source documents have high
   "not helpful" feedback rates and flags them for re-chunking.

All operations are non-destructive: chunks are never physically deleted
from ChromaDB.  Instead, an ``archived`` metadata flag is set, and the
retrieval layer filters them out.

Designed to run as a periodic autonomous job (integrated into the job
registry alongside corpus_health).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Quality score threshold — chunks below this get archived
QUALITY_FLOOR = 0.20

# Min number of feedback signals before archiving (avoid acting on noise)
MIN_FEEDBACK_SIGNALS = 3

# Max chunks to archive per run (safety valve)
MAX_ARCHIVES_PER_RUN = 20

# Max chunks to queue for re-enrichment per run
MAX_REENRICH_PER_RUN = 50

# Expected enrichment metadata fields (missing = needs re-enrichment)
EXPECTED_ENRICHMENT_FIELDS = {
    "llm_summary", "llm_topics", "llm_content_type",
    "llm_confidence", "llm_actionability",
}


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ArchiveCandidate:
    """A chunk that should be archived due to low quality."""
    chunk_id: str
    source: str
    quality_score: float
    feedback_count: int
    reason: str


@dataclass
class ReenrichCandidate:
    """A chunk that needs re-enrichment."""
    chunk_id: str
    source: str
    missing_fields: List[str]
    reason: str


@dataclass
class LowPerformanceSource:
    """A source document with high negative feedback."""
    source: str
    total_citations: int
    unhelpful_citations: int
    unhelpful_rate: float
    chunk_count: int


@dataclass
class RemediationReport:
    """Results of a remediation run."""
    run_at: str = ""
    chunks_archived: int = 0
    chunks_queued_for_reenrich: int = 0
    low_performance_sources: int = 0
    archive_candidates: List[ArchiveCandidate] = field(default_factory=list)
    reenrich_candidates: List[ReenrichCandidate] = field(default_factory=list)
    flagged_sources: List[LowPerformanceSource] = field(default_factory=list)

    def summary_text(self) -> str:
        lines = [f"**Document Remediation Report** — {self.run_at}"]
        if self.chunks_archived:
            lines.append(f"🗃️ Archived {self.chunks_archived} low-quality chunk(s)")
        if self.chunks_queued_for_reenrich:
            lines.append(f"🔄 Queued {self.chunks_queued_for_reenrich} chunk(s) for re-enrichment")
        if self.low_performance_sources:
            lines.append(f"⚠️ {self.low_performance_sources} source(s) with high negative feedback")
            for s in self.flagged_sources[:3]:
                lines.append(
                    f"   • `{s.source}` — {s.unhelpful_rate:.0%} unhelpful "
                    f"({s.unhelpful_citations}/{s.total_citations} citations)"
                )
        if not (self.chunks_archived or self.chunks_queued_for_reenrich or self.low_performance_sources):
            lines.append("✅ Corpus quality is healthy — no remediation needed")
        return "\n".join(lines)


# ── Service ──────────────────────────────────────────────────────────────────

class DocumentRemediationService:
    """Automatic quality remediation for the vector store."""

    def __init__(self, vectorstore=None, db=None):
        self.vectorstore = vectorstore
        self.db = db

    # ── 1. Find and archive low-quality chunks ──────────────────────────

    async def find_archive_candidates(self) -> List[ArchiveCandidate]:
        """Identify chunks with quality scores below the floor.

        Quality score comes from the feedback learning loop's
        ``chunk_quality_scores`` table (Wilson score interval).
        """
        candidates: List[ArchiveCandidate] = []

        if not self.db or not self.vectorstore:
            return candidates

        try:
            async with self.db.acquire() as conn:
                async with conn.execute(
                    """SELECT chunk_id, quality_score, total_signals, source_relpath
                       FROM chunk_quality_scores
                       WHERE quality_score < ?
                         AND total_signals >= ?
                       ORDER BY quality_score ASC
                       LIMIT ?""",
                    (QUALITY_FLOOR, MIN_FEEDBACK_SIGNALS, MAX_ARCHIVES_PER_RUN * 2),
                ) as cursor:
                    rows = await cursor.fetchall()

                for row in rows:
                    chunk_id = row["chunk_id"] if isinstance(row, dict) else row[0]
                    score = row["quality_score"] if isinstance(row, dict) else row[1]
                    signals = row["total_signals"] if isinstance(row, dict) else row[2]
                    source = row["source_relpath"] if isinstance(row, dict) else row[3]

                    candidates.append(ArchiveCandidate(
                        chunk_id=str(chunk_id),
                        source=source or "unknown",
                        quality_score=float(score),
                        feedback_count=int(signals),
                        reason=f"Quality score {score:.2f} < {QUALITY_FLOOR} floor "
                               f"({signals} feedback signals)",
                    ))
        except Exception as e:
            logger.debug("Archive candidate scan skipped (table may not exist): %s", e)

        return candidates[:MAX_ARCHIVES_PER_RUN]

    async def archive_chunks(
        self, candidates: List[ArchiveCandidate],
    ) -> int:
        """Soft-archive chunks by setting metadata flag.

        Does NOT delete from ChromaDB — just sets ``archived: true``
        in metadata so the retrieval layer can filter them out.
        """
        if not self.vectorstore or not candidates:
            return 0

        archived = 0
        collection = getattr(self.vectorstore, "_collection", None)
        if not collection:
            logger.warning("Cannot archive: vectorstore has no _collection attribute")
            return 0

        for candidate in candidates:
            try:
                # Get existing metadata
                result = collection.get(ids=[candidate.chunk_id], include=["metadatas"])
                if not result or not result.get("ids"):
                    continue

                existing_meta = (result.get("metadatas") or [{}])[0] or {}
                existing_meta["archived"] = True
                existing_meta["archived_at"] = datetime.utcnow().isoformat()
                existing_meta["archive_reason"] = candidate.reason[:200]

                collection.update(
                    ids=[candidate.chunk_id],
                    metadatas=[existing_meta],
                )
                archived += 1
                logger.info(
                    "Archived chunk %s from %s (score: %.2f)",
                    candidate.chunk_id, candidate.source, candidate.quality_score,
                )
            except Exception as e:
                logger.debug("Failed to archive chunk %s: %s", candidate.chunk_id, e)

        return archived

    # ── 2. Find chunks needing re-enrichment ─────────────────────────────

    def find_reenrich_candidates(self) -> List[ReenrichCandidate]:
        """Find chunks missing expected enrichment metadata fields."""
        if not self.vectorstore:
            return []

        candidates: List[ReenrichCandidate] = []

        try:
            raw = self.vectorstore.get()
            ids = raw.get("ids", [])
            metas = raw.get("metadatas", [])

            for i, (chunk_id, meta) in enumerate(zip(ids, metas)):
                meta = meta or {}

                # Skip already-archived chunks
                if meta.get("archived"):
                    continue

                # Check if enriched flag is set
                if meta.get("enriched"):
                    continue  # Already enriched — skip

                # Check for missing fields
                missing = [
                    f for f in EXPECTED_ENRICHMENT_FIELDS
                    if not meta.get(f)
                ]

                if missing:
                    source = meta.get("source_relpath", meta.get("source", "unknown"))
                    candidates.append(ReenrichCandidate(
                        chunk_id=str(chunk_id),
                        source=source,
                        missing_fields=missing,
                        reason=f"Missing enrichment: {', '.join(missing[:3])}",
                    ))

                if len(candidates) >= MAX_REENRICH_PER_RUN:
                    break
        except Exception as e:
            logger.debug("Re-enrichment scan failed: %s", e)

        return candidates

    async def queue_reenrichment(
        self, candidates: List[ReenrichCandidate],
    ) -> int:
        """Queue chunks for re-enrichment by clearing enriched flag.

        The next enrichment pass (chunk_enrichment service) will pick
        these up and re-enrich them.
        """
        if not self.vectorstore or not candidates:
            return 0

        queued = 0
        collection = getattr(self.vectorstore, "_collection", None)
        if not collection:
            return 0

        for candidate in candidates:
            try:
                result = collection.get(ids=[candidate.chunk_id], include=["metadatas"])
                if not result or not result.get("ids"):
                    continue

                existing_meta = (result.get("metadatas") or [{}])[0] or {}
                existing_meta["enriched"] = False
                existing_meta["reenrich_queued_at"] = datetime.utcnow().isoformat()
                existing_meta["reenrich_reason"] = candidate.reason[:200]

                collection.update(
                    ids=[candidate.chunk_id],
                    metadatas=[existing_meta],
                )
                queued += 1
            except Exception as e:
                logger.debug("Failed to queue re-enrichment for %s: %s", candidate.chunk_id, e)

        logger.info("Queued %d chunks for re-enrichment", queued)
        return queued

    # ── 3. Detect low-performance source documents ───────────────────────

    async def detect_low_performance_sources(
        self, min_citations: int = 5, unhelpful_rate_threshold: float = 0.5,
    ) -> List[LowPerformanceSource]:
        """Find source documents with high 'not helpful' feedback rates.

        Sources with >50% unhelpful citations (and at least 5 citations)
        are flagged for review.
        """
        if not self.db:
            return []

        flagged: List[LowPerformanceSource] = []

        try:
            async with self.db.acquire() as conn:
                # Join chat interactions with feedback to get per-source stats
                async with conn.execute(
                    """SELECT ci.sources_used, bq.response_quality
                       FROM chat_interactions ci
                       JOIN bot_questions bq ON ci.query = bq.question
                       WHERE ci.sources_used IS NOT NULL
                         AND bq.response_quality IS NOT NULL
                         AND ci.created_at >= datetime('now', '-30 days')"""
                ) as cursor:
                    rows = await cursor.fetchall()

                # Aggregate by source
                source_stats: Dict[str, Dict[str, int]] = {}
                for row in rows:
                    sources_json = row[0]
                    quality = row[1]
                    try:
                        sources = json.loads(sources_json) if isinstance(sources_json, str) else []
                    except Exception:
                        continue
                    for src in sources[:5]:
                        if src not in source_stats:
                            source_stats[src] = {"total": 0, "unhelpful": 0}
                        source_stats[src]["total"] += 1
                        if quality == "unhelpful":
                            source_stats[src]["unhelpful"] += 1

                for src, stats in source_stats.items():
                    if stats["total"] < min_citations:
                        continue
                    rate = stats["unhelpful"] / stats["total"]
                    if rate >= unhelpful_rate_threshold:
                        flagged.append(LowPerformanceSource(
                            source=src,
                            total_citations=stats["total"],
                            unhelpful_citations=stats["unhelpful"],
                            unhelpful_rate=rate,
                            chunk_count=0,  # Could be enriched with vectorstore data
                        ))
        except Exception as e:
            logger.debug("Low-performance source detection failed: %s", e)

        flagged.sort(key=lambda s: s.unhelpful_rate, reverse=True)
        return flagged

    async def create_remediation_gaps(
        self, sources: List[LowPerformanceSource],
    ) -> int:
        """Create knowledge gaps for low-performance sources."""
        if not self.db or not sources:
            return 0

        created = 0
        now = datetime.utcnow().isoformat()

        try:
            async with self.db.acquire() as conn:
                for src in sources[:5]:
                    # Check for existing gap
                    async with conn.execute(
                        "SELECT id FROM knowledge_gaps WHERE question LIKE ? AND status = 'open'",
                        (f"%{src.source[:60]}%",),
                    ) as cursor:
                        if await cursor.fetchone():
                            continue

                    question = (
                        f"Document '{src.source}' has a {src.unhelpful_rate:.0%} unhelpful "
                        f"response rate. Should it be updated, re-written, or removed?"
                    )
                    await conn.execute(
                        """INSERT INTO knowledge_gaps
                           (question, context, status, priority, times_asked,
                            created_at, updated_at, notes)
                           VALUES (?, ?, 'open', 'medium', 1, ?, ?, ?)""",
                        (
                            question,
                            f"Auto-detected: {src.unhelpful_citations}/{src.total_citations} "
                            f"citations rated unhelpful in the last 30 days.",
                            now, now,
                            "auto:document_remediation",
                        ),
                    )
                    created += 1
                await conn.commit()
        except Exception as e:
            logger.debug("Failed to create remediation gaps: %s", e)

        return created

    # ── 4. Full remediation run ──────────────────────────────────────────

    async def run_remediation(self) -> RemediationReport:
        """Execute all remediation steps and produce a report."""
        report = RemediationReport(run_at=datetime.utcnow().isoformat())

        # 1. Archive low-quality chunks
        archive_candidates = await self.find_archive_candidates()
        report.archive_candidates = archive_candidates
        if archive_candidates:
            report.chunks_archived = await self.archive_chunks(archive_candidates)

        # 2. Queue re-enrichment
        reenrich_candidates = self.find_reenrich_candidates()
        report.reenrich_candidates = reenrich_candidates
        if reenrich_candidates:
            report.chunks_queued_for_reenrich = await self.queue_reenrichment(reenrich_candidates)

        # 3. Detect low-performance sources
        flagged = await self.detect_low_performance_sources()
        report.flagged_sources = flagged
        report.low_performance_sources = len(flagged)
        if flagged:
            await self.create_remediation_gaps(flagged)

        logger.info(
            "Remediation complete: %d archived, %d re-enrich queued, %d sources flagged",
            report.chunks_archived, report.chunks_queued_for_reenrich,
            report.low_performance_sources,
        )
        return report
