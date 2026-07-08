"""
Corpus Health & Agentic Expansion Service
==========================================
Analyses the ChromaDB vector store to identify quality issues, coverage gaps,
and opportunities for autonomous document synthesis.  Designed to run
periodically (via AutonomousOps) and produce actionable recommendations that
drive high-quality corpus expansion *without* human prompting.

Capabilities
------------
- **Coverage map**:  cluster all chunks by enriched topic/content_type and score
  depth per topic (# of primary sources, recency, confidence).
- **Thin-topic detection**:  surface topics referenced in questions but backed
  by few or low-quality chunks.
- **Fragment consolidation**:  find 3+ scattered chunks on a topic that have no
  coherent long-form document — candidate for auto-synthesis.
- **Staleness scan**:  flag primary docs older than a configurable threshold.
- **Contradiction scan**:  detect semantically similar chunks with conflicting
  key claims (using LLM comparison).
- **Auto-synthesis**:  gather fragments on a thin topic, ask the LLM to draft a
  coherent memo, and save it with ``status: needs_review`` for human approval.

All generated content goes through the ``needs_review`` gate so nothing enters
the retrieval pipeline without human sign-off.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class TopicCluster:
    """A group of chunks sharing a topic / entity."""
    topic: str
    chunk_count: int = 0
    primary_count: int = 0          # source_kind == "primary"
    generated_count: int = 0        # source_kind == "generated"
    avg_confidence: float = 0.0
    avg_actionability: float = 0.0
    newest_date: str = ""           # ISO date of most recent chunk
    oldest_date: str = ""
    unique_sources: int = 0
    sample_questions: List[str] = field(default_factory=list)  # key_questions from enrichment
    source_files: List[str] = field(default_factory=list)


@dataclass
class CoverageGap:
    """A topic that users ask about but the corpus doesn't cover well."""
    topic: str
    times_asked: int = 0
    corpus_chunks: int = 0          # how many chunks mention it
    corpus_confidence: float = 0.0  # avg confidence of those chunks
    gap_ids: List[int] = field(default_factory=list)  # linked knowledge_gap IDs
    priority: float = 0.0           # composite score: high = needs attention


@dataclass
class FragmentCandidate:
    """A topic with scattered fragments that could be consolidated."""
    topic: str
    fragment_count: int = 0
    sources: List[str] = field(default_factory=list)
    summaries: List[str] = field(default_factory=list)   # LLM summaries of each fragment
    content_preview: List[str] = field(default_factory=list)  # first ~200 chars of each
    avg_actionability: float = 0.0


@dataclass
class StaleDocument:
    """A primary source that may need refreshing."""
    source: str
    doc_date: str
    days_old: int
    chunk_count: int
    doc_type: str


@dataclass
class ContradictionPair:
    """Two chunks that may contain conflicting information."""
    chunk_a_source: str
    chunk_b_source: str
    chunk_a_summary: str
    chunk_b_summary: str
    conflict_description: str       # LLM-generated explanation of the conflict
    confidence: float = 0.0         # how confident the LLM is this is a real conflict


@dataclass
class CorpusHealthReport:
    """Full health report for the knowledge corpus."""
    generated_at: str = ""
    total_chunks: int = 0
    total_sources: int = 0
    primary_sources: int = 0
    generated_sources: int = 0
    topic_clusters: List[TopicCluster] = field(default_factory=list)
    thin_topics: List[CoverageGap] = field(default_factory=list)
    fragment_candidates: List[FragmentCandidate] = field(default_factory=list)
    stale_documents: List[StaleDocument] = field(default_factory=list)
    contradictions: List[ContradictionPair] = field(default_factory=list)
    # Summary metrics
    avg_confidence: float = 0.0
    avg_actionability: float = 0.0
    noise_ratio: float = 0.0       # % of chunks tagged as noise
    enrichment_coverage: float = 0.0  # % of chunks that have been enriched

    def summary_text(self) -> str:
        """Human-readable summary for Discord or logging."""
        lines = [
            f"**Corpus Health Report** — {self.generated_at}",
            f"📊 {self.total_chunks} chunks across {self.total_sources} source files",
            f"   Primary: {self.primary_sources} | Generated: {self.generated_sources}",
            f"   Avg confidence: {self.avg_confidence:.2f} | Avg actionability: {self.avg_actionability:.2f}",
            f"   Enrichment coverage: {self.enrichment_coverage:.0%} | Noise: {self.noise_ratio:.0%}",
            "",
        ]
        if self.thin_topics:
            lines.append(f"🔍 **{len(self.thin_topics)} thin topic(s)** needing content:")
            for t in self.thin_topics[:5]:
                lines.append(
                    f"   • *{t.topic}* — asked {t.times_asked}x, "
                    f"only {t.corpus_chunks} chunk(s) (confidence {t.corpus_confidence:.2f})"
                )
            if len(self.thin_topics) > 5:
                lines.append(f"   … and {len(self.thin_topics) - 5} more")
            lines.append("")

        if self.fragment_candidates:
            lines.append(f"🧩 **{len(self.fragment_candidates)} fragment cluster(s)** ready for synthesis:")
            for f in self.fragment_candidates[:5]:
                lines.append(
                    f"   • *{f.topic}* — {f.fragment_count} scattered chunks across "
                    f"{len(f.sources)} files"
                )
            lines.append("")

        if self.stale_documents:
            lines.append(f"📅 **{len(self.stale_documents)} stale document(s)** (>{_STALE_DAYS}d old):")
            for s in self.stale_documents[:5]:
                lines.append(f"   • `{s.source}` — {s.days_old}d old ({s.doc_date})")
            lines.append("")

        if self.contradictions:
            lines.append(f"⚠️ **{len(self.contradictions)} potential contradiction(s):**")
            for c in self.contradictions[:3]:
                lines.append(f"   • {c.conflict_description[:120]}")
            lines.append("")

        return "\n".join(lines)


# ── Config defaults (overridden by workflows.yaml corpus_quality section) ────

_STALE_DAYS = 180           # docs older than this are flagged
_MIN_FRAGMENTS_FOR_SYNTHESIS = 3   # need this many scattered chunks to synthesize
_THIN_TOPIC_MAX_CHUNKS = 2  # a topic with ≤ this many chunks is "thin"
_MAX_CONTRADICTIONS_TO_CHECK = 20  # limit expensive LLM calls
_MAX_SYNTHESIS_PER_RUN = 2   # cap auto-synthesis memos per job run


# ── Service class ────────────────────────────────────────────────────────────

class CorpusHealthService:
    """Analyse the vector store and produce actionable recommendations."""

    def __init__(
        self,
        vectorstore,           # LangChain Chroma instance
        llm_service=None,      # LLMService for synthesis / contradiction detection
        db=None,               # Database for gap/feedback queries
    ):
        self.vectorstore = vectorstore
        self.llm_service = llm_service
        self.db = db

    # ── 1. Build topic coverage map ──────────────────────────────────────────

    def build_coverage_map(self) -> Tuple[List[TopicCluster], Dict[str, Any]]:
        """Scan all chunks and cluster by enriched topics.

        Returns (topic_clusters, summary_stats).
        """
        raw = self.vectorstore.get()
        ids = raw.get("ids", [])
        metas = raw.get("metadatas", [])
        docs = raw.get("documents", [])

        total = len(ids)
        if total == 0:
            return [], {"total": 0}

        # Gather per-topic stats
        topic_data: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "chunks": [],
            "sources": set(),
            "confidences": [],
            "actionabilities": [],
            "dates": [],
            "primary": 0,
            "generated": 0,
            "questions": [],
        })

        enriched_count = 0
        noise_count = 0
        all_confidences = []
        all_actionabilities = []

        for i, meta in enumerate(metas):
            meta = meta or {}
            enriched = meta.get("enriched", False)
            if enriched:
                enriched_count += 1

            confidence = float(meta.get("llm_confidence", 0.5))
            actionability = float(meta.get("llm_actionability", 0.5))
            all_confidences.append(confidence)
            all_actionabilities.append(actionability)

            content_type = meta.get("llm_content_type", "")
            if content_type == "noise":
                noise_count += 1

            source = meta.get("source_relpath", meta.get("source", ""))
            kind = (meta.get("source_kind") or "").lower()
            doc_date = meta.get("doc_date", meta.get("file_modified", ""))

            # Extract topics from enrichment
            raw_topics = meta.get("llm_topics", "")
            topics = [t.strip() for t in raw_topics.split("|") if t.strip()] if raw_topics else []

            # Also use entities as topic signals
            raw_entities = meta.get("llm_entities", "")
            entities = [e.strip() for e in raw_entities.split("|") if e.strip()] if raw_entities else []

            # If no enrichment, use doc_type as a coarse topic
            if not topics and not entities:
                doc_type = meta.get("doc_type", "unknown")
                topics = [doc_type]

            # Key questions
            raw_questions = meta.get("llm_key_questions", "")
            questions = [q.strip() for q in raw_questions.split("|") if q.strip()] if raw_questions else []

            # Distribute this chunk's data to each topic it belongs to
            all_topics = set(t.lower() for t in topics + entities)
            for topic in all_topics:
                td = topic_data[topic]
                td["chunks"].append(i)
                td["sources"].add(source)
                td["confidences"].append(confidence)
                td["actionabilities"].append(actionability)
                if doc_date:
                    td["dates"].append(doc_date)
                if kind == "primary":
                    td["primary"] += 1
                elif kind == "generated":
                    td["generated"] += 1
                td["questions"].extend(questions[:2])  # sample, not all

        # Build TopicCluster objects
        clusters = []
        for topic, td in sorted(topic_data.items(), key=lambda x: len(x[1]["chunks"]), reverse=True):
            dates = sorted(td["dates"]) if td["dates"] else []
            clusters.append(TopicCluster(
                topic=topic,
                chunk_count=len(td["chunks"]),
                primary_count=td["primary"],
                generated_count=td["generated"],
                avg_confidence=sum(td["confidences"]) / len(td["confidences"]) if td["confidences"] else 0,
                avg_actionability=sum(td["actionabilities"]) / len(td["actionabilities"]) if td["actionabilities"] else 0,
                newest_date=dates[-1] if dates else "",
                oldest_date=dates[0] if dates else "",
                unique_sources=len(td["sources"]),
                sample_questions=list(dict.fromkeys(td["questions"]))[:4],
                source_files=list(td["sources"])[:10],
            ))

        summary = {
            "total": total,
            "enriched": enriched_count,
            "noise": noise_count,
            "avg_confidence": sum(all_confidences) / len(all_confidences) if all_confidences else 0,
            "avg_actionability": sum(all_actionabilities) / len(all_actionabilities) if all_actionabilities else 0,
            "unique_sources": len({(m or {}).get("source", "") for m in metas}),
            "primary_sources": sum(1 for m in metas if (m or {}).get("source_kind") == "primary"),
            "generated_sources": sum(1 for m in metas if (m or {}).get("source_kind") == "generated"),
        }

        return clusters, summary

    # ── 2. Detect thin topics (asked about but poorly covered) ───────────────

    async def detect_thin_topics(
        self,
        topic_clusters: List[TopicCluster],
        max_chunks: int = _THIN_TOPIC_MAX_CHUNKS,
    ) -> List[CoverageGap]:
        """Cross-reference knowledge gaps with corpus coverage.

        A topic is "thin" if:
        - It appears in knowledge_gaps with times_asked >= 2
        - AND the corpus has <= max_chunks chunks on it (or avg confidence < 0.4)
        """
        if not self.db:
            return []

        thin: List[CoverageGap] = []
        # Build a quick lookup: topic -> cluster
        cluster_by_topic = {c.topic.lower(): c for c in topic_clusters}

        try:
            async with self.db.acquire() as conn:
                async with conn.execute("""
                    SELECT topic, question, times_asked, id
                    FROM knowledge_gaps
                    WHERE status = 'open'
                    AND curation_status = 'keep'
                    ORDER BY times_asked DESC, priority_score DESC
                    LIMIT 100
                """) as cursor:
                    rows = await cursor.fetchall()

                for row in rows:
                    gap_topic = (row[0] or "").lower().strip()
                    gap_question = row[1] or ""
                    times_asked = int(row[2] or 1)
                    gap_id = int(row[3])

                    if times_asked < 2:
                        continue

                    # Find matching cluster (fuzzy: check if gap topic words appear in any cluster)
                    gap_words = set(w for w in gap_topic.split() if len(w) > 3)
                    best_cluster = None
                    best_overlap = 0
                    for ct, cluster in cluster_by_topic.items():
                        cluster_words = set(w for w in ct.split() if len(w) > 3)
                        overlap = len(gap_words & cluster_words)
                        if overlap > best_overlap:
                            best_overlap = overlap
                            best_cluster = cluster

                    corpus_chunks = best_cluster.chunk_count if best_cluster else 0
                    corpus_conf = best_cluster.avg_confidence if best_cluster else 0

                    is_thin = corpus_chunks <= max_chunks or corpus_conf < 0.4

                    if is_thin:
                        # Check if we already have this topic in thin list
                        existing = next((t for t in thin if t.topic == gap_topic), None)
                        if existing:
                            existing.times_asked = max(existing.times_asked, times_asked)
                            existing.gap_ids.append(gap_id)
                        else:
                            priority = (
                                times_asked * 2.0
                                + (1.0 - corpus_conf) * 3.0
                                + (1.0 if corpus_chunks == 0 else 0.5)
                            )
                            thin.append(CoverageGap(
                                topic=gap_topic,
                                times_asked=times_asked,
                                corpus_chunks=corpus_chunks,
                                corpus_confidence=corpus_conf,
                                gap_ids=[gap_id],
                                priority=priority,
                            ))
        except Exception as e:
            logger.warning("Failed to detect thin topics: %s", e)

        thin.sort(key=lambda x: x.priority, reverse=True)
        return thin

    # ── 3. Find fragment consolidation candidates ────────────────────────────

    def find_fragment_candidates(
        self,
        topic_clusters: List[TopicCluster],
        min_fragments: int = _MIN_FRAGMENTS_FOR_SYNTHESIS,
    ) -> List[FragmentCandidate]:
        """Find topics with scattered fragments across multiple sources.

        A topic is a fragment candidate if:
        - It has >= min_fragments chunks
        - Spread across >= 2 different source files
        - No single source has > 60% of the chunks (i.e. it's truly scattered)
        - The topic isn't just a doc_type label (like "doc" or "memo")
        """
        raw = self.vectorstore.get()
        metas = raw.get("metadatas", [])
        texts = raw.get("documents", [])

        # Skip generic/noise labels
        skip_topics = {"doc", "memo", "discord_export", "interview_memo",
                       "reference", "noise", "unknown", "casual_discussion"}

        candidates = []
        for cluster in topic_clusters:
            if cluster.topic.lower() in skip_topics:
                continue
            if cluster.chunk_count < min_fragments:
                continue
            if cluster.unique_sources < 2:
                continue

            # Check concentration: if one source has >60% of chunks, it's not "scattered"
            source_counts = Counter(cluster.source_files)
            if source_counts and source_counts.most_common(1)[0][1] > cluster.chunk_count * 0.6:
                continue

            # Gather previews from the actual chunk texts
            previews = []
            summaries = []
            for i, meta in enumerate(metas):
                meta = meta or {}
                raw_topics = meta.get("llm_topics", "")
                raw_entities = meta.get("llm_entities", "")
                all_t = (raw_topics + "|" + raw_entities).lower()
                if cluster.topic.lower() in all_t:
                    if texts and i < len(texts) and texts[i]:
                        previews.append(texts[i][:200])
                    summary = meta.get("llm_summary", "")
                    if summary:
                        summaries.append(summary)
                    if len(previews) >= 8:  # enough for synthesis
                        break

            candidates.append(FragmentCandidate(
                topic=cluster.topic,
                fragment_count=cluster.chunk_count,
                sources=cluster.source_files,
                summaries=summaries[:8],
                content_preview=previews[:8],
                avg_actionability=cluster.avg_actionability,
            ))

        # Sort by fragment count * actionability (most impactful first)
        candidates.sort(key=lambda c: c.fragment_count * c.avg_actionability, reverse=True)
        return candidates

    # ── 4. Staleness scan ────────────────────────────────────────────────────

    def find_stale_documents(self, stale_days: int = _STALE_DAYS) -> List[StaleDocument]:
        """Find primary source documents older than stale_days."""
        raw = self.vectorstore.get()
        metas = raw.get("metadatas", [])

        cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")
        source_info: Dict[str, Dict[str, Any]] = {}

        for meta in metas:
            meta = meta or {}
            kind = (meta.get("source_kind") or "").lower()
            if kind != "primary":
                continue

            source = meta.get("source_relpath", meta.get("source", ""))
            if source in source_info:
                source_info[source]["chunk_count"] += 1
                continue

            doc_date = meta.get("doc_date", meta.get("file_modified", ""))
            if not doc_date or doc_date > cutoff:
                continue

            try:
                date_obj = datetime.strptime(doc_date[:10], "%Y-%m-%d")
                days_old = (datetime.now() - date_obj).days
            except (ValueError, TypeError):
                continue

            source_info[source] = {
                "doc_date": doc_date,
                "days_old": days_old,
                "chunk_count": 1,
                "doc_type": meta.get("doc_type", "unknown"),
            }

        stale = [
            StaleDocument(
                source=src,
                doc_date=info["doc_date"],
                days_old=info["days_old"],
                chunk_count=info["chunk_count"],
                doc_type=info["doc_type"],
            )
            for src, info in source_info.items()
        ]
        stale.sort(key=lambda s: s.days_old, reverse=True)
        return stale

    # ── 5. Contradiction detection ───────────────────────────────────────────

    async def detect_contradictions(
        self,
        max_checks: int = _MAX_CONTRADICTIONS_TO_CHECK,
    ) -> List[ContradictionPair]:
        """Find chunks that may contain conflicting information.

        Strategy: for each high-actionability chunk, search for semantically
        similar chunks from *different* sources, then ask the LLM whether they
        conflict.
        """
        if not self.llm_service:
            return []

        raw = self.vectorstore.get()
        metas = raw.get("metadatas", [])
        texts = raw.get("documents", [])

        if not texts:
            return []

        # Pick high-actionability, high-confidence chunks as seeds
        seeds = []
        for i, meta in enumerate(metas):
            meta = meta or {}
            actionability = float(meta.get("llm_actionability", 0))
            confidence = float(meta.get("llm_confidence", 0))
            if actionability >= 0.7 and confidence >= 0.5:
                content_type = meta.get("llm_content_type", "")
                # Focus on decision/strategy/reference — types most likely to contradict
                if content_type in ("decision", "strategy", "reference", "operational_guidance", "financial"):
                    seeds.append(i)

        # Limit seeds
        seeds = seeds[:max_checks]
        contradictions: List[ContradictionPair] = []

        for seed_idx in seeds:
            seed_text = texts[seed_idx] if seed_idx < len(texts) else ""
            seed_meta = metas[seed_idx] or {}
            seed_source = seed_meta.get("source_relpath", seed_meta.get("source", ""))
            seed_summary = seed_meta.get("llm_summary", seed_text[:150])

            if not seed_text:
                continue

            # Find similar chunks from different sources
            try:
                similar = self.vectorstore.similarity_search_with_score(
                    seed_text[:500], k=5
                )
            except Exception:
                continue

            for doc, score in similar:
                other_meta = doc.metadata or {}
                other_source = other_meta.get("source_relpath", other_meta.get("source", ""))

                # Same source = not a contradiction
                if other_source == seed_source:
                    continue

                # Only check close matches (similarity score varies by embedding model)
                if score > 1.0:  # too distant
                    continue

                other_summary = other_meta.get("llm_summary", doc.page_content[:150])

                # Ask LLM to check for contradiction
                try:
                    conflict = await self._check_contradiction(
                        seed_text[:600], doc.page_content[:600],
                        seed_source, other_source,
                    )
                    if conflict:
                        contradictions.append(ContradictionPair(
                            chunk_a_source=seed_source,
                            chunk_b_source=other_source,
                            chunk_a_summary=seed_summary[:200],
                            chunk_b_summary=other_summary[:200],
                            conflict_description=conflict["description"],
                            confidence=float(conflict.get("confidence", 0.5)),
                        ))
                except Exception as e:
                    logger.debug("Contradiction check failed: %s", e)

            if len(contradictions) >= 10:
                break

        return contradictions

    async def _check_contradiction(
        self, text_a: str, text_b: str, source_a: str, source_b: str,
    ) -> Optional[Dict[str, Any]]:
        """Ask the LLM whether two chunks contradict each other."""
        prompt = f"""You are a fact-checker for an organisational knowledge base.

Compare these two document excerpts and determine if they CONTRADICT each other.
A contradiction is when they make **incompatible factual claims** about the same thing
(different dates, prices, policies, decisions, specifications, etc.).

Differences in tone, detail level, or scope are NOT contradictions.
One document being newer/more detailed is NOT a contradiction.

Document A (from {source_a}):
\"\"\"
{text_a}
\"\"\"

Document B (from {source_b}):
\"\"\"
{text_b}
\"\"\"

If they DO contradict, respond with ONLY this JSON:
{{"contradiction": true, "confidence": 0.0-1.0, "description": "Brief explanation of the specific conflict"}}

If they do NOT contradict, respond with ONLY:
{{"contradiction": false}}"""

        result = await self.llm_service.complete(prompt, max_tokens=200, temperature=0.0)

        import json
        try:
            match = re.search(r"\{[\s\S]*\}", result)
            if not match:
                return None
            data = json.loads(match.group())
            if data.get("contradiction"):
                return {
                    "description": data.get("description", "Unknown conflict"),
                    "confidence": data.get("confidence", 0.5),
                }
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning("operation: suppressed %s", e)
        return None

    # ── 6. Auto-synthesis: turn fragments into coherent documents ────────────

    async def synthesize_fragments(
        self,
        candidate: FragmentCandidate,
        org_context: str = "",
    ) -> Optional[str]:
        """Synthesize scattered fragments into a coherent document.

        Returns the memo content (Markdown), or None if synthesis fails.
        The caller is responsible for saving with needs_review status.
        """
        if not self.llm_service:
            logger.warning("Cannot synthesize: no LLM service")
            return None

        # Gather actual chunk content for this topic
        raw = self.vectorstore.get()
        metas = raw.get("metadatas", [])
        texts = raw.get("documents", [])

        fragments = []
        for i, meta in enumerate(metas):
            meta = meta or {}
            raw_topics = meta.get("llm_topics", "")
            raw_entities = meta.get("llm_entities", "")
            all_t = (raw_topics + "|" + raw_entities).lower()
            if candidate.topic.lower() in all_t and texts and i < len(texts):
                source = meta.get("source_relpath", meta.get("source", ""))
                fragments.append({
                    "text": texts[i][:800],
                    "source": source,
                    "date": meta.get("doc_date", meta.get("file_modified", "")),
                    "type": meta.get("llm_content_type", ""),
                })
                if len(fragments) >= 12:
                    break

        if len(fragments) < _MIN_FRAGMENTS_FOR_SYNTHESIS:
            return None

        # Build the synthesis prompt
        fragment_block = ""
        for j, frag in enumerate(fragments, 1):
            fragment_block += f"\n--- Fragment {j} (from: {frag['source']}, date: {frag['date'] or 'unknown'}) ---\n"
            fragment_block += frag["text"] + "\n"

        prompt = f"""You are a knowledge base curator for an organisation.
{org_context}

I have {len(fragments)} scattered fragments of information about: **{candidate.topic}**

These fragments are spread across different documents and chat logs. Your job is to
synthesise them into ONE coherent, well-structured reference document.

RULES:
- ONLY include facts that are explicitly stated in the fragments below
- Do NOT infer, assume, or add information from your training data
- If fragments contradict each other, note the contradiction and cite both sources
- Include a "## Source of Truth" section listing which source files contributed
- Include a "## Key Points" section with concrete bullets
- Include a "## Decisions / Defaults" section if any decisions are mentioned
- Include a "## Open Questions" section for anything unclear or missing
- Keep it factual and concise — no marketing language or filler

FRAGMENTS:
{fragment_block}

Write the consolidated document in Markdown format:"""

        try:
            memo = await self.llm_service.complete(prompt, max_tokens=2000, temperature=0.1)
            # Validate minimum quality
            if len(memo.split()) < 30:
                logger.warning("Synthesis produced too-short output for %s", candidate.topic)
                return None
            return memo
        except Exception as e:
            logger.error("Synthesis LLM call failed for %s: %s", candidate.topic, e)
            return None

    # ── 7. Growth dashboard (user-facing progress view) ────────────────────

    async def build_growth_dashboard(self) -> Dict[str, Any]:
        """Build a user-facing corpus growth dashboard.

        Returns a dict with:
        - total_facts: approximate fact count (chunks)
        - total_topics: number of distinct topics
        - strongest: top 5 topics by chunk count
        - weakest: bottom 5 topics (or thin topics) that need content
        - recent_additions: docs added in the last 7 days
        - gap_stats: open vs resolved knowledge gaps
        - suggested_actions: concrete next steps for the user
        - growth_trend: dict with week-over-week growth estimate
        """
        clusters, summary = self.build_coverage_map()
        total = summary.get("total", 0)

        # Sort clusters by chunk count
        strongest = sorted(clusters, key=lambda c: c.chunk_count, reverse=True)[:5]
        # Get weakest non-trivial topics
        weakest_all = sorted(
            [c for c in clusters if c.chunk_count >= 1],
            key=lambda c: c.chunk_count,
        )[:5]

        # Recent additions (last 7 days)
        cutoff_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        raw = self.vectorstore.get()
        metas = raw.get("metadatas", [])
        recent_sources = set()
        for meta in metas:
            meta = meta or {}
            doc_date = meta.get("doc_date", meta.get("file_modified", ""))
            if doc_date and doc_date >= cutoff_7d:
                source = meta.get("source_relpath", meta.get("source", ""))
                if source:
                    recent_sources.add(source)

        # Knowledge gap stats
        gap_stats = {"open": 0, "resolved": 0, "total": 0, "foundational_remaining": 0}
        if self.db:
            try:
                async with self.db.acquire() as conn:
                    async with conn.execute(
                        "SELECT status, COUNT(*) FROM knowledge_gaps GROUP BY status"
                    ) as cur:
                        for row in await cur.fetchall():
                            gap_stats[row[0]] = row[1]
                            gap_stats["total"] += row[1]
                    # Count remaining foundational gaps
                    async with conn.execute(
                        "SELECT COUNT(*) FROM knowledge_gaps WHERE status='open' "
                        "AND notes LIKE '%foundational%'"
                    ) as cur:
                        gap_stats["foundational_remaining"] = (await cur.fetchone())[0]
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        # Thin topics
        thin = await self.detect_thin_topics(clusters) if self.db else []

        # Build suggested actions
        actions = []
        if total == 0:
            actions.append({
                "action": "Start the capture sprint",
                "description": "Use /sprint or /teach to add your first documents",
                "priority": "high",
            })
        if gap_stats.get("foundational_remaining", 0) > 0:
            n = gap_stats["foundational_remaining"]
            actions.append({
                "action": f"Answer {n} foundational question{'s' if n != 1 else ''}",
                "description": "Use /interview to answer tailored questions about your org",
                "priority": "high",
            })
        if thin:
            top_thin = thin[0]
            actions.append({
                "action": f"Add content about: {top_thin.topic}",
                "description": (
                    f"Asked {top_thin.times_asked}x but only {top_thin.corpus_chunks} "
                    f"chunk(s) in the knowledge base"
                ),
                "priority": "medium",
            })
        stale = self.find_stale_documents()
        if stale:
            actions.append({
                "action": f"Review {len(stale)} stale document(s)",
                "description": f"Oldest: {stale[0].source} ({stale[0].days_old} days old)",
                "priority": "low",
            })

        return {
            "total_facts": total,
            "total_topics": len(clusters),
            "total_sources": summary.get("unique_sources", 0),
            "enrichment_coverage": round(
                summary.get("enriched", 0) / total * 100 if total > 0 else 0, 1
            ),
            "avg_confidence": round(summary.get("avg_confidence", 0) * 100, 1),
            "strongest": [
                {"topic": c.topic, "chunks": c.chunk_count, "sources": c.unique_sources}
                for c in strongest
            ],
            "weakest": [
                {"topic": c.topic, "chunks": c.chunk_count, "sources": c.unique_sources}
                for c in weakest_all
            ],
            "thin_topics": [
                {
                    "topic": t.topic,
                    "times_asked": t.times_asked,
                    "corpus_chunks": t.corpus_chunks,
                }
                for t in thin[:5]
            ],
            "recent_additions": len(recent_sources),
            "recent_sources": sorted(recent_sources)[:10],
            "gap_stats": gap_stats,
            "suggested_actions": actions,
            "stale_count": len(stale),
        }

    # ── 8. Full health check (orchestrator) ──────────────────────────────────

    async def run_full_health_check(self) -> CorpusHealthReport:
        """Run all analyses and produce a comprehensive health report."""
        report = CorpusHealthReport(generated_at=datetime.now().isoformat())

        # 1. Coverage map
        clusters, summary = self.build_coverage_map()
        report.total_chunks = summary.get("total", 0)
        report.total_sources = summary.get("unique_sources", 0)
        report.primary_sources = summary.get("primary_sources", 0)
        report.generated_sources = summary.get("generated_sources", 0)
        report.avg_confidence = summary.get("avg_confidence", 0)
        report.avg_actionability = summary.get("avg_actionability", 0)
        report.noise_ratio = (
            summary.get("noise", 0) / summary["total"]
            if summary.get("total", 0) > 0 else 0
        )
        report.enrichment_coverage = (
            summary.get("enriched", 0) / summary["total"]
            if summary.get("total", 0) > 0 else 0
        )
        report.topic_clusters = clusters

        # 2. Thin topics
        report.thin_topics = await self.detect_thin_topics(clusters)

        # 3. Fragment candidates
        report.fragment_candidates = self.find_fragment_candidates(clusters)

        # 4. Stale documents
        report.stale_documents = self.find_stale_documents()

        # 5. Contradictions (expensive — only if LLM available)
        if self.llm_service:
            report.contradictions = await self.detect_contradictions()

        return report
