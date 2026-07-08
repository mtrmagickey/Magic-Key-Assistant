"""
Corpus Self-Interrogation Service
==================================

The bot's "mirror" — a systematic framework for examining its own knowledge
corpus and asking deep, structural questions that produce actionable
improvements.  This is NOT thin-topic detection (the Curator already does
that); it is *proactive strategic analysis* of what the corpus covers,
where it's weak, and what a new hire / decision-maker / frontline user
would need but can't find.

Three-tier interrogation hierarchy
-----------------------------------

**Tier 1 — Strategic** (weekly):
    Big systemic questions about corpus shape:
    - What business-critical domains are missing entirely?
    - Where do we have breadth but no authoritative depth?
    - What knowledge would a new employee need that isn't here?
    - What are the riskiest knowledge gaps (catastrophic if wrong)?
    - What implicit assumptions does our corpus embed?

**Tier 2 — Drill-Down** (per strategic finding):
    Specific detail questions generated from each strategic finding:
    - What policy / procedure / contact governs X?
    - What are the exact numbers / dates / deadlines for Y?
    - What changed most recently about Z?

**Tier 3 — Action Routing** (per drill-down question):
    Each question is routed to the fastest resolution path:
    - ``web_research``  → public info, standards, regulations → auto-research
    - ``human_review``  → institutional knowledge, internal decisions → gap + interview queue
    - ``verification``  → we *think* we know but confidence is low → flag for partner review
    - ``auto_close``    → fragments already answer this → synthesise and close

The output feeds back into the existing Curator pipeline (web research,
targeted gap creation, daily partner questions) and is persisted in the
``corpus_interrogations`` table for audit trail and trend analysis.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Maximum budgets per run ──────────────────────────────────────────────────

MAX_STRATEGIC_QUESTIONS = 5
MAX_DRILLDOWNS_PER_STRATEGIC = 3
MAX_WEB_RESEARCH_PER_RUN = 3
MAX_GAPS_PER_RUN = 5

# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class InterrogationFinding:
    """A single finding from a self-interrogation pass."""

    question: str
    finding: str = ""
    domain: str = ""
    severity: str = "minor"  # critical | significant | minor | informational
    tier: str = "strategic"  # strategic | drill_down | verification
    action_type: str = ""  # web_research | human_review | verification | auto_close
    parent_id: Optional[int] = None
    confidence: float = 0.0  # 0-1: how confident the LLM is in this finding


@dataclass
class InterrogationResult:
    """Complete result of an interrogation run."""

    run_id: str = ""
    run_date: str = ""
    strategic_findings: List[InterrogationFinding] = field(default_factory=list)
    drilldown_findings: List[InterrogationFinding] = field(default_factory=list)
    actions_taken: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary for Discord / logging."""
        lines = [f"**Corpus Self-Interrogation** — {self.run_date}"]
        lines.append(f"Run ID: `{self.run_id[:8]}`")

        if self.strategic_findings:
            by_sev = {}
            for f in self.strategic_findings:
                by_sev.setdefault(f.severity, []).append(f)
            lines.append(f"\n🔬 **{len(self.strategic_findings)} strategic finding(s):**")
            for sev in ("critical", "significant", "minor", "informational"):
                items = by_sev.get(sev, [])
                if items:
                    icon = {"critical": "🔴", "significant": "🟠", "minor": "🟡", "informational": "🔵"}[sev]
                    for item in items:
                        lines.append(f"  {icon} *{item.domain or 'General'}*: {item.finding[:120]}")

        if self.drilldown_findings:
            lines.append(f"\n🔍 **{len(self.drilldown_findings)} detail question(s) generated**")

        if self.actions_taken:
            parts = []
            if self.actions_taken.get("web_researched", 0):
                parts.append(f"🌐 {self.actions_taken['web_researched']} web-researched")
            if self.actions_taken.get("gaps_created", 0):
                parts.append(f"🎯 {self.actions_taken['gaps_created']} gap(s) created for partners")
            if self.actions_taken.get("auto_closed", 0):
                parts.append(f"✅ {self.actions_taken['auto_closed']} auto-resolved from existing fragments")
            if parts:
                lines.append("\n**Actions:** " + " | ".join(parts))

        return "\n".join(lines)


# ── Strategic Interrogation Prompt ───────────────────────────────────────────

_STRATEGIC_PROMPT = """\
You are a knowledge management auditor examining an organisation's internal \
knowledge base.  Your job is to find STRUCTURAL gaps — not surface-level \
missing facts, but systemic blind spots that undermine the usefulness of \
the entire corpus.

## Corpus Coverage Map
{coverage_map}

## User Question Frequency (what people actually ask about)
{question_frequency}

## Organisation Context
{org_context}

## Your Task
Identify the {max_findings} most significant structural gaps in this \
knowledge base.  Think like:
- A new employee on Day 1 who needs to get up to speed
- A decision-maker who needs ground-truth numbers to act
- A frontline worker who needs correct procedures during a crisis
- An auditor looking for single-points-of-failure in institutional knowledge

For each gap, determine whether it can be filled via:
- **web_research**: Public information, industry standards, regulations, best practices
- **human_review**: Institutional knowledge, internal decisions, proprietary processes
- **verification**: The corpus SEEMS to have this info but it may be outdated or conflicting
- **auto_close**: Fragments already exist but aren't consolidated into a coherent document

Respond in STRICT JSON — no markdown, no commentary:
{{
  "findings": [
    {{
      "domain": "<topic area, 3-6 words>",
      "question": "<the specific question this gap represents>",
      "finding": "<1-2 sentence description of what's missing and why it matters>",
      "severity": "<critical|significant|minor|informational>",
      "action_type": "<web_research|human_review|verification|auto_close>",
      "confidence": <0.0-1.0 how confident you are this is a real gap>
    }}
  ]
}}

RULES:
1. Be SPECIFIC — "What is the emergency evacuation procedure for each building?" \
not "Safety information is incomplete"
2. Prioritise gaps where being WRONG has real consequences (safety, legal, financial)
3. Don't flag topics that are clearly out of scope for this organisation
4. Ground every finding in what you see (or don't see) in the coverage map
5. At least one finding should be something NO ONE has asked about yet but probably should
"""

# ── Drill-Down Prompt ────────────────────────────────────────────────────────

_DRILLDOWN_PROMPT = """\
You are a knowledge management auditor.  A strategic review found this gap \
in the knowledge base:

## Strategic Finding
Domain: {domain}
Question: {question}
Finding: {finding}
Action type: {action_type}

## Relevant Corpus Chunks (if any)
{relevant_chunks}

## Your Task
Generate {max_drilldowns} specific, answerable sub-questions that would \
FULLY resolve this gap.  Each question should:
1. Be answerable in 1-3 paragraphs
2. Ask for concrete facts (names, numbers, dates, procedures) — NOT opinions
3. Specify what source of truth should be consulted
4. Be independent (answering one doesn't depend on answering another)

For each sub-question, specify whether it needs:
- **web_research**: Can be answered from public sources
- **human_review**: Requires someone with institutional knowledge
- **verification**: We have fragments that might answer this — need confirmation

Respond in STRICT JSON:
{{
  "drilldowns": [
    {{
      "question": "<specific sub-question>",
      "domain": "<same or narrower domain>",
      "action_type": "<web_research|human_review|verification>",
      "rationale": "<why this specific detail matters>"
    }}
  ]
}}
"""

# ── Core Engine ──────────────────────────────────────────────────────────────


async def run_strategic_interrogation(
    llm_service: Any,
    coverage_map: Dict[str, Any],
    question_frequency: Dict[str, int],
    org_context: str = "",
    *,
    max_findings: int = MAX_STRATEGIC_QUESTIONS,
) -> List[InterrogationFinding]:
    """Tier 1: Ask the LLM to identify structural gaps in the corpus.

    Parameters
    ----------
    llm_service : LLMService
        The LLM service for generating analysis.
    coverage_map : dict
        Topic clusters with depth/confidence metrics from CorpusHealthService.
    question_frequency : dict
        Map of topic → times_asked from knowledge_gaps / chat_interactions.
    org_context : str
        Organisation name, industry, location context.

    Returns
    -------
    List of InterrogationFinding (strategic tier).
    """
    if not llm_service:
        return []

    # Format the coverage map for the prompt
    cm_text = _format_coverage_map(coverage_map)
    qf_text = _format_question_frequency(question_frequency)

    prompt = _STRATEGIC_PROMPT.format(
        coverage_map=cm_text,
        question_frequency=qf_text,
        org_context=org_context or "No organisation context available.",
        max_findings=max_findings,
    )

    try:
        raw = await llm_service.complete(prompt, max_tokens=1200, temperature=0.4)
        parsed = _parse_json_response(raw)
        findings_raw = parsed.get("findings", [])

        findings = []
        for f in findings_raw[:max_findings]:
            findings.append(InterrogationFinding(
                question=f.get("question", ""),
                finding=f.get("finding", ""),
                domain=f.get("domain", ""),
                severity=f.get("severity", "minor"),
                tier="strategic",
                action_type=f.get("action_type", "human_review"),
                confidence=float(f.get("confidence", 0.5)),
            ))

        # Filter out low-confidence findings
        findings = [f for f in findings if f.confidence >= 0.3 and f.question]
        return findings

    except Exception as exc:
        logger.warning("Strategic interrogation failed: %s", exc)
        return []


async def run_drilldown(
    llm_service: Any,
    strategic_finding: InterrogationFinding,
    relevant_chunks: str = "",
    *,
    max_drilldowns: int = MAX_DRILLDOWNS_PER_STRATEGIC,
) -> List[InterrogationFinding]:
    """Tier 2: Generate specific sub-questions from a strategic finding.

    Parameters
    ----------
    llm_service : LLMService
        The LLM service for generating analysis.
    strategic_finding : InterrogationFinding
        The parent strategic finding to drill into.
    relevant_chunks : str
        Any relevant corpus chunks that partially address the finding.

    Returns
    -------
    List of InterrogationFinding (drill_down tier).
    """
    if not llm_service:
        return []

    prompt = _DRILLDOWN_PROMPT.format(
        domain=strategic_finding.domain,
        question=strategic_finding.question,
        finding=strategic_finding.finding,
        action_type=strategic_finding.action_type,
        relevant_chunks=relevant_chunks or "No relevant chunks found.",
        max_drilldowns=max_drilldowns,
    )

    try:
        raw = await llm_service.complete(prompt, max_tokens=800, temperature=0.3)
        parsed = _parse_json_response(raw)
        drilldowns_raw = parsed.get("drilldowns", [])

        drilldowns = []
        for d in drilldowns_raw[:max_drilldowns]:
            drilldowns.append(InterrogationFinding(
                question=d.get("question", ""),
                finding=d.get("rationale", ""),
                domain=d.get("domain", strategic_finding.domain),
                severity=strategic_finding.severity,
                tier="drill_down",
                action_type=d.get("action_type", "human_review"),
            ))

        return [d for d in drilldowns if d.question]

    except Exception as exc:
        logger.warning("Drill-down generation failed for %s: %s",
                        strategic_finding.domain, exc)
        return []


# ── Action Routing ───────────────────────────────────────────────────────────


async def execute_actions(
    findings: List[InterrogationFinding],
    *,
    bot: Any = None,
    db: Any = None,
    run_id: str = "",
    tavily: Any = None,
    llm_service: Any = None,
) -> Dict[str, int]:
    """Route findings to their resolution paths.

    Returns a summary dict of actions taken.
    """
    counters: Dict[str, int] = {
        "web_researched": 0,
        "gaps_created": 0,
        "verifications_flagged": 0,
        "auto_closed": 0,
        "skipped": 0,
    }

    for finding in findings:
        try:
            if finding.action_type == "web_research":
                if counters["web_researched"] >= MAX_WEB_RESEARCH_PER_RUN:
                    counters["skipped"] += 1
                    continue
                success = await _action_web_research(
                    finding, tavily=tavily, llm_service=llm_service, bot=bot,
                )
                if success:
                    counters["web_researched"] += 1

            elif finding.action_type == "human_review":
                if counters["gaps_created"] >= MAX_GAPS_PER_RUN:
                    counters["skipped"] += 1
                    continue
                success = await _action_create_gap(finding, db=db)
                if success:
                    counters["gaps_created"] += 1

            elif finding.action_type == "verification":
                success = await _action_flag_verification(finding, db=db, run_id=run_id)
                if success:
                    counters["verifications_flagged"] += 1

            elif finding.action_type == "auto_close":
                counters["auto_closed"] += 1  # synthesis handled by Curator

        except Exception as exc:
            logger.debug("Action failed for %s: %s", finding.domain, exc)
            counters["skipped"] += 1

    return counters


async def _action_web_research(
    finding: InterrogationFinding,
    *,
    tavily: Any = None,
    llm_service: Any = None,
    bot: Any = None,
) -> bool:
    """Auto-research a finding via web search and save as needs_review memo."""
    if not tavily or not getattr(tavily, "is_configured", False):
        return False

    from services.web_research import research_knowledge_gap

    draft = await research_knowledge_gap(
        tavily,
        finding.domain,
        finding.question,
        llm_service=llm_service,
        max_results=5,
    )
    if not draft or len(draft) < 80:
        return False

    # Save as needs_review memo
    from services.autonomous_research import _save_research_memo
    path = _save_research_memo(
        f"interrogation: {finding.domain}",
        finding.question,
        draft,
    )
    if not path:
        return False

    # Auto-approve if configured
    try:
        from core.config_loader import WorkflowConfig
        wf = WorkflowConfig.load()
        if wf.cq_auto_approve_web_research and bot:
            doc_author = bot.get_cog("DocumentAuthor")
            if doc_author:
                await doc_author.auto_approve_memo(path)
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    logger.info("Interrogation web-researched: %s → %s", finding.domain, path)
    return True


async def _action_create_gap(
    finding: InterrogationFinding,
    *,
    db: Any = None,
) -> bool:
    """Create a targeted knowledge gap for human resolution."""
    if not db:
        return False

    from cogs.KnowledgeGapTracker import classify_gap_curation, insert_gap

    topic = f"Self-interrogation: {finding.domain}"
    question = finding.question
    context = (
        f"Auto-generated by corpus self-interrogation | "
        f"Severity: {finding.severity} | "
        f"Finding: {finding.finding[:200]} | "
        f"[depth:0]"
    )

    cur_status, cur_reason = classify_gap_curation(topic, question, context)
    if cur_status == "defer":
        logger.debug("Interrogation gap deferred: %s — %s", finding.domain, cur_reason)
        return False

    async with db.acquire() as conn:
        # Avoid duplicates: check if a similar gap already exists
        async with conn.execute(
            """SELECT id FROM knowledge_gaps
               WHERE question LIKE ? AND status = 'open'""",
            (f"%{finding.question[:60]}%",),
        ) as cur:
            if await cur.fetchone():
                return False

        priority = {"critical": 8, "significant": 6, "minor": 4, "informational": 2}.get(
            finding.severity, 4
        )
        await insert_gap(
            conn,
            topic=topic,
            question=question,
            context=context,
            priority_score=priority,
            curation_status=cur_status,
            curation_reason=cur_reason,
        )
        await conn.commit()

    logger.info("Interrogation created gap: %s", finding.domain)
    return True


async def _action_flag_verification(
    finding: InterrogationFinding,
    *,
    db: Any = None,
    run_id: str = "",
) -> bool:
    """Flag a finding for human verification (corpus has info, but confidence is low)."""
    if not db:
        return False

    try:
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO corpus_interrogations
                   (run_id, interrogation_date, interrogation_type,
                    domain, question, finding, severity,
                    action_type, status)
                   VALUES (?, datetime('now'), 'verification',
                           ?, ?, ?, ?, 'verification', 'pending')""",
                (run_id, finding.domain, finding.question,
                 finding.finding, finding.severity),
            )
            await conn.commit()
        return True
    except Exception as exc:
        logger.debug("Failed to flag verification: %s", exc)
        return False


# ── Persistence ──────────────────────────────────────────────────────────────


async def save_interrogation_results(
    db: Any,
    run_id: str,
    findings: List[InterrogationFinding],
) -> None:
    """Persist all findings to the corpus_interrogations table."""
    if not db:
        return

    try:
        async with db.acquire() as conn:
            for f in findings:
                await conn.execute(
                    """INSERT INTO corpus_interrogations
                       (run_id, interrogation_date, interrogation_type,
                        domain, question, finding, severity,
                        action_type, status)
                       VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, 'pending')""",
                    (run_id, f.tier, f.domain, f.question,
                     f.finding, f.severity, f.action_type),
                )
            await conn.commit()
    except Exception as exc:
        logger.warning("Failed to save interrogation results: %s", exc)


async def get_interrogation_history(
    db: Any,
    *,
    days: int = 30,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve recent interrogation findings for dashboard / review.

    Parameters
    ----------
    db : database instance
    days : int
        How many days back to look.
    status : optional str
        Filter by status ('pending', 'resolved', 'dismissed').

    Returns list of finding dicts.
    """
    if not db:
        return []

    try:
        query = """
            SELECT id, run_id, interrogation_date, interrogation_type,
                   domain, question, finding, severity,
                   action_type, action_result, status,
                   resolved_by, resolved_at
            FROM corpus_interrogations
            WHERE interrogation_date >= datetime('now', ?)
        """
        params: list = [f"-{days} days"]

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY interrogation_date DESC, severity"

        async with db.acquire() as conn, conn.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as exc:
        logger.debug("Failed to get interrogation history: %s", exc)
        return []


async def resolve_interrogation(
    db: Any,
    interrogation_id: int,
    *,
    resolved_by: str = "auto",
    action_result: str = "",
) -> bool:
    """Mark an interrogation finding as resolved."""
    if not db:
        return False

    try:
        async with db.acquire() as conn:
            await conn.execute(
                """UPDATE corpus_interrogations
                   SET status = 'resolved',
                       resolved_by = ?,
                       resolved_at = datetime('now'),
                       action_result = ?
                   WHERE id = ?""",
                (resolved_by, action_result, interrogation_id),
            )
            await conn.commit()
        return True
    except Exception as exc:
        logger.debug("Failed to resolve interrogation: %s", exc)
        return False


# ── Full Interrogation Orchestrator ──────────────────────────────────────────


async def run_full_interrogation(
    *,
    llm_service: Any,
    db: Any = None,
    bot: Any = None,
    tavily: Any = None,
    coverage_clusters: List[Any] = None,
    coverage_summary: Optional[Dict[str, Any]] = None,
    org_context: str = "",
    retriever: Any = None,
) -> InterrogationResult:
    """Run a complete interrogation cycle: strategic → drilldown → action.

    This is the main entry point, called by the CuratorMixin on schedule.

    Parameters
    ----------
    llm_service : LLMService
    db : database pool
    bot : Discord bot instance
    tavily : TavilyService
    coverage_clusters : list of TopicCluster from CorpusHealthService
    coverage_summary : dict with total counts / metrics
    org_context : str
    retriever : optional vectorstore retriever for finding relevant chunks

    Returns an InterrogationResult with all findings and actions taken.
    """
    run_id = uuid.uuid4().hex[:12]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    result = InterrogationResult(run_id=run_id, run_date=today)

    if not llm_service:
        result.errors.append("No LLM service available")
        return result

    # ── Build inputs ─────────────────────────────────────────────────────
    coverage_map = _clusters_to_map(coverage_clusters or [], coverage_summary)
    question_freq = await _get_question_frequency(db)

    # ── Tier 1: Strategic interrogation ──────────────────────────────────
    logger.info("Interrogation [%s] Tier 1: strategic analysis", run_id[:8])
    strategic = await run_strategic_interrogation(
        llm_service, coverage_map, question_freq,
        org_context=org_context,
        max_findings=MAX_STRATEGIC_QUESTIONS,
    )
    result.strategic_findings = strategic

    if not strategic:
        logger.info("Interrogation [%s] no strategic findings — corpus looks solid", run_id[:8])
        return result

    # ── Tier 2: Drill down into top findings ─────────────────────────────
    logger.info("Interrogation [%s] Tier 2: drilling into %d findings",
                run_id[:8], len(strategic))

    all_drilldowns: List[InterrogationFinding] = []
    for sf in strategic:
        # Get relevant chunks for context
        chunks_text = ""
        if retriever:
            try:
                docs = retriever.invoke(sf.question)
                if docs:
                    chunks_text = "\n---\n".join(
                        f"[{getattr(d, 'metadata', {}).get('source', 'unknown')}]\n"
                        f"{d.page_content[:300]}"
                        for d in docs[:4]
                    )
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        drilldowns = await run_drilldown(
            llm_service, sf,
            relevant_chunks=chunks_text,
            max_drilldowns=MAX_DRILLDOWNS_PER_STRATEGIC,
        )
        all_drilldowns.extend(drilldowns)

    result.drilldown_findings = all_drilldowns

    # ── Tier 3: Execute actions ──────────────────────────────────────────
    # Combine all actionable findings (drilldowns have priority over strategic)
    actionable = all_drilldowns + [
        sf for sf in strategic
        if sf.action_type in ("web_research", "auto_close")
        and sf.domain not in {d.domain for d in all_drilldowns}
    ]

    logger.info("Interrogation [%s] Tier 3: executing %d actions",
                run_id[:8], len(actionable))

    actions = await execute_actions(
        actionable,
        bot=bot,
        db=db,
        run_id=run_id,
        tavily=tavily,
        llm_service=llm_service,
    )
    result.actions_taken = actions

    # ── Persist everything ───────────────────────────────────────────────
    all_findings = strategic + all_drilldowns
    await save_interrogation_results(db, run_id, all_findings)

    logger.info(
        "Interrogation [%s] complete: %d strategic, %d drilldowns, "
        "%d web-researched, %d gaps, %d verifications",
        run_id[:8],
        len(strategic), len(all_drilldowns),
        actions.get("web_researched", 0),
        actions.get("gaps_created", 0),
        actions.get("verifications_flagged", 0),
    )

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _clusters_to_map(
    clusters: list,
    summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert TopicCluster list to a dict suitable for prompt injection."""
    topics = {}
    for c in clusters:
        topic = getattr(c, "topic", str(c))
        topics[topic] = {
            "chunks": getattr(c, "chunk_count", 0),
            "primary_sources": getattr(c, "primary_count", 0),
            "avg_confidence": round(getattr(c, "avg_confidence", 0), 2),
            "newest": getattr(c, "newest_date", ""),
            "unique_sources": getattr(c, "unique_sources", 0),
        }

    return {
        "topics": topics,
        "summary": summary or {},
    }


def _format_coverage_map(cm: Dict[str, Any]) -> str:
    """Format coverage map for LLM prompt."""
    topics = cm.get("topics", {})
    summary = cm.get("summary", {})

    if not topics:
        return "The corpus is empty — no topics indexed."

    lines = [
        f"Total: {summary.get('total', '?')} chunks, "
        f"{summary.get('unique_sources', '?')} source files",
        "",
        "Topics (sorted by chunk count):",
    ]
    sorted_topics = sorted(topics.items(), key=lambda x: x[1].get("chunks", 0), reverse=True)
    for topic, info in sorted_topics[:30]:
        lines.append(
            f"  • {topic}: {info.get('chunks', 0)} chunks, "
            f"{info.get('primary_sources', 0)} primary, "
            f"confidence {info.get('avg_confidence', 0)}, "
            f"newest: {info.get('newest', 'unknown')}"
        )
    if len(sorted_topics) > 30:
        lines.append(f"  … and {len(sorted_topics) - 30} more topics")

    return "\n".join(lines)


def _format_question_frequency(qf: Dict[str, int]) -> str:
    """Format question frequency for LLM prompt."""
    if not qf:
        return "No question frequency data available yet."

    lines = ["Most frequently asked topics:"]
    sorted_qf = sorted(qf.items(), key=lambda x: x[1], reverse=True)
    for topic, count in sorted_qf[:20]:
        lines.append(f"  • {topic}: asked {count} time(s)")

    return "\n".join(lines)


async def _get_question_frequency(db: Any) -> Dict[str, int]:
    """Get question frequency from knowledge_gaps and chat_interactions."""
    freq: Dict[str, int] = {}
    if not db:
        return freq

    try:
        async with db.acquire() as conn:
            # From knowledge gaps (topics people have asked about)
            async with conn.execute(
                """SELECT topic, times_asked
                   FROM knowledge_gaps
                   WHERE times_asked > 0
                   ORDER BY times_asked DESC
                   LIMIT 50"""
            ) as cur:
                rows = await cur.fetchall()
                for row in rows:
                    topic = row[0] or ""
                    # Strip internal prefixes
                    for prefix in ("Curator: ", "Self-interrogation: ", "Open question: "):
                        if topic.startswith(prefix):
                            topic = topic[len(prefix):]
                    if topic:
                        freq[topic] = freq.get(topic, 0) + (row[1] or 1)
    except Exception as exc:
        logger.debug("Question frequency query failed: %s", exc)

    return freq


def _parse_json_response(raw: str) -> dict:
    """Parse LLM JSON response, handling markdown fences and trailing text."""
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last ``` line
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Find the JSON object
    start = text.find("{")
    if start == -1:
        return {}

    # Find matching closing brace
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                text = text[start : i + 1]
                break

    return json.loads(text)
