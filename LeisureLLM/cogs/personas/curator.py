"""
Curator Persona — Agentic Corpus Expansion
============================================
A mixin for AutonomousOps that proactively analyses the knowledge corpus,
synthesises scattered fragments into coherent documents, surfaces thin topics,
and drives high-quality autonomous expansion.

Unlike the passive feedback loop (FeedbackView → improvement memo), the Curator
actively scans the vector store and database on a schedule and takes action:

1. **Daily corpus health check** (07:30 ET) — quick scan for thin topics and
   stale docs, posts a summary to bots-office.
2. **Weekly deep analysis** (Saturday 08:00 ET) — full health report including
   contradiction detection and fragment consolidation.
3. **Auto-synthesis** — when fragment clusters are found, the Curator drafts
   consolidated reference documents and saves them with ``needs_review`` status.
4. **Targeted interview generation** — for thin topics where NO fragments exist,
   the Curator creates high-priority knowledge gaps with focused questions designed
   to elicit the specific information needed.
5. **Corpus self-interrogation** (Wed/Sat 09:00 ET) — the bot examines its own
   knowledge base with hierarchical structural questions (strategic → detail),
   auto-researches public gaps via web search, and escalates institutional
   knowledge gaps to partners for human resolution.

All generated content goes through the review gate — nothing enters the retrieval
pipeline until a human approves it.

Required interface from host cog (AutonomousOps):
    self.bot          (with .db, .service_container)
    self.llm_service  (LLMService)
    self.post_to_bots_channel(department, message, embed=None)
    self._job_already_ran(job_name, run_date) -> bool
    self._record_job_run(job_name, run_date)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord

from .base import EASTERN, _now_utc_iso

logger = logging.getLogger(__name__)


class CuratorMixin:
    """Agentic corpus expansion: analyse, synthesise, and fill gaps."""

    # ── Helper: get org context for synthesis prompts ─────────────────────────

    def _curator_org_context(self) -> str:
        """Build org context string for synthesis prompts."""
        try:
            from core.config_loader import OrgProfile
            org = OrgProfile.load()
            parts = []
            if org.name and org.name != "My Company":
                parts.append(f"Organisation: {org.name}")
            if org.industry:
                parts.append(f"Industry: {org.industry}")
            return "\n".join(parts)
        except Exception:
            return ""

    # ── Helper: get corpus health service instance ────────────────────────────

    def _get_corpus_health_service(self):
        """Create a CorpusHealthService wired to the current bot's resources."""
        from core.chroma_factory import get_vectorstore
        from services.corpus_health import CorpusHealthService

        vectorstore = get_vectorstore()
        return CorpusHealthService(
            vectorstore=vectorstore,
            llm_service=self.llm_service,
            db=getattr(self.bot, "db", None),
        )

    # ── Helper: save a synthesis memo with needs_review ──────────────────────

    async def _curator_save_synthesis(
        self,
        topic: str,
        content: str,
        source_files: List[str],
        synthesis_type: str = "fragment_consolidation",
    ) -> Optional[Path]:
        """Save a synthesised document as a needs_review memo.

        Returns the file path, or None on failure.
        """
        doc_author = self.bot.get_cog("DocumentAuthor")
        if not doc_author:
            logger.warning("DocumentAuthor cog not loaded — cannot save synthesis")
            return None

        slug = re.sub(r"[^a-z0-9_]+", "_", topic.lower().replace(" ", "_"))[:40]
        slug = f"synthesis_{slug}"

        metadata = {
            "topic": topic,
            "doc_type": "synthesis",
            "synthesis_type": synthesis_type,
            "source_files": ", ".join(source_files[:10]),
            "status": "needs_review",
            "auto_generated": True,
            "generated_by": "curator",
            "generated_at": _now_utc_iso(),
        }

        try:
            filepath = doc_author._save_document(
                content=content,
                doc_type="memo",
                slug=slug,
                metadata=metadata,
            )
            logger.info("Curator saved synthesis memo: %s", filepath)
            return filepath
        except Exception as e:
            logger.error("Failed to save synthesis memo: %s", e)
            return None

    # ── Helper: create targeted knowledge gaps for thin topics ───────────────

    async def _curator_create_targeted_gaps(
        self,
        thin_topics: list,
        max_gaps: int = 3,
    ) -> int:
        """Create high-priority knowledge gaps with focused interview questions.

        Unlike the generic gap detection in LLM.py (which just logs "I don't know"),
        these are crafted to elicit specific, structured information:
        - What is the source of truth?
        - What are the key facts/numbers/constraints?
        - What decisions have been made?

        Returns the number of gaps created.
        """
        from cogs.KnowledgeGapTracker import classify_gap_curation, insert_gap

        db = getattr(self.bot, "db", None)
        if not db:
            return 0

        created = 0
        async with db.acquire() as conn:
            for gap_info in thin_topics[:max_gaps]:
                topic = gap_info.topic

                # Generate a focused question for this thin topic
                question = await self._curator_craft_question(topic)
                if not question:
                    question = (
                        f"What are the key facts, constraints, and decisions about: {topic}? "
                        f"Include the source of truth (file/channel/link) and any specific "
                        f"numbers, dates, or names."
                    )

                context = (
                    f"Auto-generated by Curator: topic asked {gap_info.times_asked}x "
                    f"but corpus has only {gap_info.corpus_chunks} chunk(s) "
                    f"(avg confidence {gap_info.corpus_confidence:.2f}). "
                    f"[depth:0]"
                )

                cur, reason = classify_gap_curation(f"Curator: {topic}", question, context)
                if cur == "defer":
                    logger.debug("Curator gap deferred: %s — %s", topic, reason)
                    continue

                await insert_gap(
                    conn,
                    topic=f"Curator: {topic}",
                    question=question,
                    context=context,
                    priority_score=max(5, gap_info.times_asked + 3),
                    curation_status=cur,
                    curation_reason=reason,
                )
                created += 1

            await conn.commit()

        return created

    async def _curator_craft_question(self, topic: str) -> Optional[str]:
        """Use the LLM to craft a focused interview question for a thin topic."""
        if not self.llm_service:
            return None

        prompt = f"""You are a knowledge curator for an organisation. A topic is poorly documented
in our knowledge base and users keep asking about it.

Topic: "{topic}"

Write ONE clear, specific interview question that would elicit the most useful
information from a human who knows about this topic. The question should:

1. Ask for concrete facts, numbers, dates, or constraints — not opinions
2. Ask for the source of truth (where is this information maintained?)
3. Be answerable in a single paragraph

Respond with ONLY the question text, no preamble."""

        try:
            result = await self.llm_service.complete(prompt, max_tokens=200, temperature=0.3)
            q = result.strip().strip('"').strip()
            if len(q) > 20:
                return q
        except Exception as e:
            logger.debug("Failed to craft question for %s: %s", topic, e)
        return None

    # ── Helper: proactive daily question to ops channel ──────────────────────

    async def _curator_post_daily_question(self) -> bool:
        """Post the single highest-priority unresolved gap to the partners channel.

        This is the core corpus-growth nudge: one question per day, zero friction.
        A reply from any partner triggers the /remember auto-classification pipeline
        and builds the corpus as a byproduct of natural conversation.

        Returns True if a question was posted, False otherwise.
        """
        db = getattr(self.bot, "db", None)
        if not db:
            return False

        try:
            async with db.acquire() as conn:
                # Pick the highest-priority open gap that hasn't been posted recently
                async with conn.execute("""
                    SELECT id, topic, question
                    FROM knowledge_gaps
                    WHERE status = 'open'
                      AND curation_status = 'keep'
                      AND (last_asked IS NULL OR last_asked < datetime('now', '-3 days'))
                    ORDER BY
                        CASE WHEN notes LIKE '%foundational%' THEN 0 ELSE 1 END,
                        priority_score DESC,
                        times_asked DESC
                    LIMIT 1
                """) as cur:
                    row = await cur.fetchone()

                if not row:
                    return False

                gap_id, topic, question = row[0], row[1], row[2]

                # Update last_asked timestamp
                await conn.execute(
                    "UPDATE knowledge_gaps SET last_asked = datetime('now') WHERE id = ?",
                    (gap_id,),
                )
                await conn.commit()

            # Find the partners channel (weekly-meeting-threads or ops)
            import discord
            channel = None
            for name in ("weekly-meeting-threads", "ops", "general"):
                channel = discord.utils.get(self.bot.get_all_channels(), name=name)
                if channel:
                    break

            if not channel:
                logger.warning("No suitable channel found for daily question")
                return False

            # Build the question embed
            embed = discord.Embed(
                title="\U0001f4ac Daily Knowledge Question",
                description=(
                    f"**{question}**\n\n"
                    f"*Topic: {topic}*\n\n"
                    "\U0001f4a1 Just reply here to answer — I'll auto-save it to your knowledge base.\n"
                    "Or type `/interview` for a full Q&A session."
                ),
                color=0xECB651,
            )
            embed.set_footer(text=f"Knowledge gap #{gap_id} \u2022 Reply to build your knowledge base")

            await channel.send(embed=embed)
            logger.info("Posted daily question: gap #%d — %s", gap_id, topic)
            return True

        except Exception as e:
            logger.warning("Failed to post daily question: %s", e)
            return False

    # ── Helper: get Tavily service (may be None) ─────────────────────────────

    def _curator_tavily(self):
        """Return the TavilyService from the bot's service container, or None."""
        sc = getattr(self.bot, "service_container", None)
        return getattr(sc, "tavily", None) if sc else None

    # ── Helper: web-research thin topics and save drafts ─────────────────────

    async def _curator_web_research_thin_topics(
        self,
        thin_topics: list,
        max_topics: int = 2,
    ) -> int:
        """Auto-research thin topics via web search and save draft memos.

        For each thin topic that has a clear question, runs a Tavily search,
        synthesises the results into a draft memo, and saves it with
        ``status: needs_review``.  This fills knowledge gaps *before* humans
        need to be interviewed.

        Returns the number of web-researched drafts saved.
        """
        tavily = self._curator_tavily()
        if not tavily or not getattr(tavily, "is_configured", False):
            return 0

        from services.web_research import research_knowledge_gap

        saved = 0
        for gap_info in thin_topics[:max_topics]:
            topic = gap_info.topic

            # Craft a question to research
            question = await self._curator_craft_question(topic)
            if not question:
                question = f"What are the key facts and best practices about {topic}?"

            draft = await research_knowledge_gap(
                tavily,
                topic,
                question,
                llm_service=self.llm_service,
                max_results=5,
            )
            if not draft or len(draft) < 80:
                continue

            # Save as a needs_review memo via DocumentAuthor
            path = await self._curator_save_synthesis(
                topic=topic,
                content=draft,
                source_files=["web_research"],
                synthesis_type="web_research",
            )
            if path:
                saved += 1
                logger.info("Curator web-researched thin topic: %s → %s", topic, path)

                # Auto-approve web-researched content (has real citations)
                try:
                    from core.config_loader import WorkflowConfig
                    wf = WorkflowConfig.load()
                    if wf.cq_auto_approve_web_research:
                        doc_author = self.bot.get_cog("DocumentAuthor")
                        if doc_author:
                            approved = await doc_author.auto_approve_memo(path)
                            if approved:
                                logger.info(
                                    "Auto-approved web-researched memo: %s", path
                                )
                except Exception as exc:
                    logger.debug("Auto-approve skipped: %s", exc)

        return saved

    # ── Helper: enrich a synthesis with web context ──────────────────────────

    async def _curator_enrich_with_web(self, topic: str, content: str) -> str:
        """Append external references from a web search to a synthesis memo.

        Returns the enriched content (original + addendum), or the original
        unchanged if Tavily is unavailable or search yields nothing.
        """
        tavily = self._curator_tavily()
        if not tavily or not getattr(tavily, "is_configured", False):
            return content

        from services.web_research import enrich_synthesis_with_web

        addendum = await enrich_synthesis_with_web(
            tavily, topic, content,
            llm_service=self.llm_service,
            max_results=4,
        )
        if addendum:
            return content + addendum
        return content

    # ══════════════════════════════════════════════════════════════════════════
    # SCHEDULED JOBS
    # ══════════════════════════════════════════════════════════════════════════

    async def curator_daily_scan(self):
        """Daily lightweight corpus scan: thin topics + stale docs.

        Runs at 07:30 ET.  Posts a summary to bots-office and creates targeted
        gaps for the top thin topics.
        """
        today = datetime.now(EASTERN).date().isoformat()
        if await self._job_already_ran("curator_daily_scan", today):
            return

        logger.info("Curator daily scan starting")

        try:
            svc = self._get_corpus_health_service()

            # Quick coverage map (no LLM calls)
            clusters, summary = svc.build_coverage_map()
            total = summary.get("total", 0)

            if total == 0:
                await self.post_to_bots_channel(
                    "curator",
                    "📚 Corpus is empty — no documents ingested yet. "
                    "Use `/teach` or add files to the docs/ folder."
                )
                await self._record_job_run("curator_daily_scan", today)
                return

            # Thin topics (cross-references with knowledge_gaps)
            thin = await svc.detect_thin_topics(clusters)

            # Stale docs (quick scan, no LLM)
            stale = svc.find_stale_documents()

            # Fragment candidates (quick scan, no LLM)
            fragments = svc.find_fragment_candidates(clusters)

            # ── Take action: create targeted gaps for thin topics ────
            gaps_created = 0
            if thin:
                gaps_created = await self._curator_create_targeted_gaps(thin, max_gaps=2)

            # ── Take action: web-research thin topics ────────────────
            web_researched = 0
            try:
                from core.config_loader import WorkflowConfig
                wf = WorkflowConfig.load()
                web_enabled = wf.cq_curator_web_enabled
                web_max_daily = wf.cq_curator_web_max_daily
            except Exception:
                web_enabled = True
                web_max_daily = 2

            if thin and web_enabled:
                web_researched = await self._curator_web_research_thin_topics(
                    thin, max_topics=web_max_daily,
                )

            # ── Build summary ────────────────────────────────────────
            lines = [f"📚 **Daily Corpus Scan** — {total} chunks, {summary.get('unique_sources', 0)} sources"]

            if thin:
                lines.append(f"\n🔍 **{len(thin)} thin topic(s)** (asked about but poorly documented):")
                for t in thin[:3]:
                    lines.append(
                        f"  • *{t.topic}* — asked {t.times_asked}x, "
                        f"{t.corpus_chunks} chunk(s)"
                    )
                if gaps_created:
                    lines.append(f"  → Created {gaps_created} targeted interview question(s)")
                if web_researched:
                    lines.append(f"  → 🌐 Web-researched {web_researched} topic(s) — drafts pending review")

            if fragments:
                lines.append(f"\n🧩 **{len(fragments)} topic(s)** with scattered fragments (synthesis ready):")
                for f in fragments[:3]:
                    lines.append(f"  • *{f.topic}* — {f.fragment_count} fragments across {len(f.sources)} files")

            if stale:
                lines.append(f"\n📅 **{len(stale)} stale primary source(s)** needing refresh")

            if not thin and not fragments and not stale:
                lines.append("\n✅ Corpus looks healthy — no action needed today")

            # ── Proactive daily question: post one gap to partners ───
            daily_q_posted = False
            try:
                daily_q_posted = await self._curator_post_daily_question()
                if daily_q_posted:
                    lines.append("\n💬 Posted daily knowledge question to partners channel")
            except Exception as e:
                logger.debug("Daily question post failed: %s", e)

            await self.post_to_bots_channel("curator", "\n".join(lines))
            await self._record_job_run("curator_daily_scan", today)
            logger.info("Curator daily scan complete: %d thin, %d fragments, %d stale",
                        len(thin), len(fragments), len(stale))

        except Exception as e:
            logger.error("Curator daily scan failed: %s", e)
            await self._record_job_run("curator_daily_scan", today)

    async def curator_weekly_deep_analysis(self):
        """Weekly deep analysis: full health report + auto-synthesis.

        Runs Saturday at 08:00 ET.  Includes contradiction detection (LLM-backed)
        and auto-synthesises the top fragment candidates.
        """
        today = datetime.now(EASTERN).date().isoformat()
        if await self._job_already_ran("curator_weekly_deep_analysis", today):
            return

        logger.info("Curator weekly deep analysis starting")

        try:
            svc = self._get_corpus_health_service()

            # Full health check (includes contradiction detection)
            report = await svc.run_full_health_check()

            # ── Auto-synthesis: consolidate top fragment candidates ───
            synth_count = 0
            synth_paths = []
            org_context = self._curator_org_context()

            max_synth = 2  # configurable via corpus_quality in future
            enrich_with_web = True
            try:
                from core.config_loader import WorkflowConfig
                wf = WorkflowConfig.load()
                raw_cq = wf.raw.get("corpus_quality", {})
                max_synth = raw_cq.get("auto_synthesis", {}).get("max_per_run", 2)
                enrich_with_web = wf.cq_curator_web_enrich_synthesis
            except Exception:
                wf = None

            for candidate in report.fragment_candidates[:max_synth]:
                memo_content = await svc.synthesize_fragments(candidate, org_context)
                if memo_content:
                    # Web-enrich synthesis with external references
                    if enrich_with_web:
                        memo_content = await self._curator_enrich_with_web(
                            candidate.topic, memo_content,
                        )
                    path = await self._curator_save_synthesis(
                        topic=candidate.topic,
                        content=memo_content,
                        source_files=candidate.sources,
                    )
                    if path:
                        synth_count += 1
                        synth_paths.append(str(path))

            # ── Create targeted gaps for uncovered thin topics ───────
            gaps_created = 0
            if report.thin_topics:
                gaps_created = await self._curator_create_targeted_gaps(
                    report.thin_topics, max_gaps=3
                )

            # ── Web-research thin topics (weekly gets a higher budget) ─
            web_researched = 0
            try:
                web_enabled = wf.cq_curator_web_enabled if wf else True
                web_max_weekly = wf.cq_curator_web_max_weekly if wf else 3
            except Exception:
                web_enabled = True
                web_max_weekly = 3

            if report.thin_topics and web_enabled:
                web_researched = await self._curator_web_research_thin_topics(
                    report.thin_topics, max_topics=web_max_weekly,
                )

            # ── Post the full report ─────────────────────────────────
            summary = report.summary_text()

            if synth_count:
                summary += (
                    f"\n📝 **Auto-synthesised {synth_count} document(s)** (pending review):\n"
                    + "\n".join(f"  • `{p}`" for p in synth_paths)
                    + "\n  Use `/pending_memos` to review and `/approve_memo` to ingest."
                )

            if gaps_created:
                summary += f"\n🎯 Created {gaps_created} targeted interview question(s) for thin topics"

            if web_researched:
                summary += f"\n🌐 Web-researched {web_researched} thin topic(s) — drafts pending review"

            # Truncate for Discord (2000 char limit)
            if len(summary) > 1900:
                summary = summary[:1900] + "\n… (truncated)"

            await self.post_to_bots_channel("curator", summary)

            # ── Persist health snapshot ──────────────────────────────
            db = getattr(self.bot, "db", None)
            if db:
                try:
                    async with db.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO bot_health_snapshots (snapshot_date, metrics_json)
                            VALUES (?, ?)
                        """, (today, _corpus_report_json(report, synth_count, gaps_created)))
                        await conn.commit()
                except Exception as e:
                    logger.debug("Failed to save corpus health snapshot: %s", e)

            await self._record_job_run("curator_weekly_deep_analysis", today)
            logger.info(
                "Curator deep analysis complete: %d thin, %d fragments, %d stale, "
                "%d contradictions, %d synthesised, %d gaps created",
                len(report.thin_topics), len(report.fragment_candidates),
                len(report.stale_documents), len(report.contradictions),
                synth_count, gaps_created,
            )

        except Exception as e:
            logger.error("Curator weekly deep analysis failed: %s", e)
            await self._record_job_run("curator_weekly_deep_analysis", today)

    async def curator_corpus_interrogation(self):
        """Corpus self-interrogation: structured analysis of knowledge gaps.

        The bot examines its own corpus with deep structural questions,
        auto-researches what it can via web search, and escalates
        institutional knowledge gaps to partners.

        Runs Wednesday and Saturday at 09:00 ET — spaced to let the daily
        scan (07:30) and deep analysis (Sat 08:00) feed coverage data first.

        This is the "always working to improve itself" showcase feature.
        """
        today = datetime.now(EASTERN).date().isoformat()
        if await self._job_already_ran("curator_corpus_interrogation", today):
            return

        logger.info("Curator corpus self-interrogation starting")

        try:
            from services.corpus_interrogator import run_full_interrogation

            # Gather corpus coverage data
            svc = self._get_corpus_health_service()
            clusters, summary = svc.build_coverage_map()

            if summary.get("total", 0) == 0:
                await self._record_job_run("curator_corpus_interrogation", today)
                return

            # Get org context
            org_context = self._curator_org_context()

            # Get Tavily for web research
            tavily = self._curator_tavily()

            # Get retriever for chunk lookup during drill-down
            retriever = None
            try:
                from core.chroma_factory import get_vectorstore
                vs = get_vectorstore()
                if vs:
                    retriever = vs.as_retriever(search_kwargs={"k": 4})
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            db = getattr(self.bot, "db", None)

            # Run the full interrogation cycle
            result = await run_full_interrogation(
                llm_service=self.llm_service,
                db=db,
                bot=self.bot,
                tavily=tavily,
                coverage_clusters=clusters,
                coverage_summary=summary,
                org_context=org_context,
                retriever=retriever,
            )

            # Post summary to bots-office
            summary_text = result.summary()
            if result.strategic_findings:
                await self.post_to_bots_channel("curator", summary_text)
            else:
                await self.post_to_bots_channel(
                    "curator",
                    "🔬 **Self-interrogation complete** — no structural gaps "
                    "detected. The knowledge base is in good shape."
                )

            await self._record_job_run("curator_corpus_interrogation", today)
            logger.info(
                "Curator self-interrogation complete: %d strategic, %d drilldown, "
                "%d web-researched, %d gaps created",
                len(result.strategic_findings),
                len(result.drilldown_findings),
                result.actions_taken.get("web_researched", 0),
                result.actions_taken.get("gaps_created", 0),
            )

        except Exception as e:
            logger.error("Curator corpus self-interrogation failed: %s", e)
            await self._record_job_run("curator_corpus_interrogation", today)


def _corpus_report_json(report, synth_count: int, gaps_created: int) -> str:
    """Serialise key report metrics to JSON for the health snapshot."""
    import json
    return json.dumps({
        "type": "corpus_health",
        "total_chunks": report.total_chunks,
        "total_sources": report.total_sources,
        "primary_sources": report.primary_sources,
        "generated_sources": report.generated_sources,
        "avg_confidence": round(report.avg_confidence, 3),
        "avg_actionability": round(report.avg_actionability, 3),
        "noise_ratio": round(report.noise_ratio, 3),
        "enrichment_coverage": round(report.enrichment_coverage, 3),
        "thin_topics": len(report.thin_topics),
        "fragment_candidates": len(report.fragment_candidates),
        "stale_documents": len(report.stale_documents),
        "contradictions": len(report.contradictions),
        "auto_synthesised": synth_count,
        "targeted_gaps_created": gaps_created,
    })
