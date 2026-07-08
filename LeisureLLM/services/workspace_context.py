"""
Workspace Context Builder — live organisational state summary for LLM context.

Problem:
    The LLM only sees retrieved document chunks.  It has no idea what
    actions are overdue, what the pipeline looks like, what decisions
    were made recently, or what recurring concerns exist.  This makes
    it unable to proactively connect dots or suggest next steps.

Solution:
    Periodically query the operational database and build a compact
    "workspace snapshot" that is injected into the system prompt.
    The snapshot is refreshed every N minutes (default 5) and cached
    in memory so it adds zero latency to individual chat requests.

Injected sections:
    - Active Actions  (title, status, due date, assignee)
    - Pipeline Summary (leads by stage)
    - Recent Decisions (last 14 days)
    - Open Knowledge Gaps (top 5 by priority)
    - Recurring Concerns (from interaction memory)
    - Upcoming Obligations (from forward planner)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_REFRESH_INTERVAL_SECONDS = 5 * 60   # 5 minutes
_MAX_SNAPSHOT_CHARS = 4000            # keep it concise for context budget


class WorkspaceContextBuilder:
    """
    Builds and caches a compact workspace state summary.

    Usage::

        builder = WorkspaceContextBuilder(db)
        ctx = await builder.get_context()   # returns cached or freshly built string
    """

    def __init__(self, db: Any, refresh_interval: float = _REFRESH_INTERVAL_SECONDS):
        self._db = db
        self._refresh = refresh_interval
        self._cached: Optional[str] = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_context(self) -> str:
        """Return workspace context string, refreshing if stale."""
        if self._cached and (time.time() - self._cached_at) < self._refresh:
            return self._cached

        async with self._lock:
            # Double-check after acquiring lock
            if self._cached and (time.time() - self._cached_at) < self._refresh:
                return self._cached

            try:
                self._cached = await self._build_snapshot()
                self._cached_at = time.time()
            except Exception as exc:
                logger.warning("Failed to build workspace context: %s", exc)
                if self._cached is None:
                    self._cached = ""

        return self._cached

    def invalidate(self) -> None:
        """Force refresh on next call."""
        self._cached_at = 0.0

    # ── Internal builders ─────────────────────────────────────────────────

    async def _build_snapshot(self) -> str:
        """Query DB and assemble the workspace snapshot string."""
        sections: List[str] = []

        sections.append(await self._section_actions())
        sections.append(await self._section_pipeline())
        sections.append(await self._section_decisions())
        sections.append(await self._section_gaps())
        sections.append(await self._section_recent_discoveries())
        sections.append(await self._section_health_trend())
        sections.append(await self._section_concerns())
        sections.append(await self._section_obligations())
        sections.append(await self._section_proactive_nudges())

        # Filter empties and join
        body = "\n".join(s for s in sections if s)
        if not body:
            return ""

        header = "## Workspace Snapshot (live state)\n"
        result = header + body

        # Truncate if too long (shouldn't happen with LIMIT clauses, but safety)
        if len(result) > _MAX_SNAPSHOT_CHARS:
            result = result[:_MAX_SNAPSHOT_CHARS - 20] + "\n[…truncated]"

        return result

    async def _section_actions(self) -> str:
        """Active and overdue action items."""
        try:
            rows = await self._db.fetch_dicts(
                """SELECT title, status, priority, due_date, assignee
                       FROM tasks
                       WHERE status NOT IN ('done', 'cancelled')
                       ORDER BY
                           CASE WHEN due_date < date('now') THEN 0 ELSE 1 END,
                           due_date ASC
                       LIMIT 10"""
            )
            if not rows:
                return ""
            lines = ["### Active Actions"]
            for r in rows:
                due = r["due_date"] or "no date"
                overdue = " ⚠️OVERDUE" if r["due_date"] and r["due_date"] < _today() else ""
                assignee = f" → {r['assignee']}" if r["assignee"] else ""
                lines.append(
                    f"- [{r['status']}] {r['title']} (due: {due}{overdue}{assignee})"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    async def _section_pipeline(self) -> str:
        """Lead pipeline summary by stage."""
        try:
            rows = await self._db.fetch_dicts(
                "SELECT stage, COUNT(*) as cnt FROM leads GROUP BY stage"
            )
            if not rows:
                return ""
            parts = [f"{r['stage']}: {r['cnt']}" for r in rows]
            return f"### Pipeline\n{' | '.join(parts)}"
        except Exception:
            return ""

    async def _section_decisions(self) -> str:
        """Recent recorded decisions."""
        try:
            rows = await self._db.fetch_dicts(
                """SELECT title, decided_at, decided_by
                       FROM decisions
                       WHERE decided_at >= date('now', '-14 days')
                       AND (superseded_by_decision_id IS NULL)
                       ORDER BY decided_at DESC LIMIT 5"""
            )
            if not rows:
                return ""
            lines = ["### Recent Decisions"]
            for r in rows:
                by = f" (by {r['decided_by']})" if r["decided_by"] else ""
                lines.append(f"- {r['title']}{by} — {r['decided_at']}")
            return "\n".join(lines)
        except Exception:
            return ""

    async def _section_gaps(self) -> str:
        """Top open knowledge gaps."""
        try:
            rows = await self._db.fetch_dicts(
                """SELECT topic, question, times_asked, priority_score
                       FROM knowledge_gaps
                       WHERE status = 'open'
                       ORDER BY priority_score DESC, times_asked DESC
                       LIMIT 5"""
            )
            if not rows:
                return ""
            lines = ["### Open Knowledge Gaps"]
            for r in rows:
                lines.append(
                    f"- {r['topic']}: \"{r['question']}\" (asked {r['times_asked']}x)"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    async def _section_recent_discoveries(self) -> str:
        """Recent web research discoveries (elevated opportunities)."""
        try:
            rows = await self._db.fetch_dicts(
                """SELECT rso.title, rso.url, rso.assessment_reason,
                              rso.source_query, rso.first_seen_date,
                              l.name AS lead_name, l.status AS lead_status
                       FROM rainmaker_seen_opportunities rso
                       LEFT JOIN leads l ON rso.lead_id = l.id
                       WHERE rso.first_seen_date >= date('now', '-14 days')
                         AND rso.assessment = 'elevated'
                       ORDER BY rso.first_seen_date DESC
                       LIMIT 5"""
            )
            if not rows:
                return ""

            lines = ["### Recent Discoveries"]
            for row in rows:
                title = row["title"] or row["url"] or "Untitled"
                reason = f" — {row['assessment_reason']}" if row["assessment_reason"] else ""
                lead_note = ""
                if row["lead_name"]:
                    lead_note = f" → Lead: {row['lead_name']} ({row['lead_status']})"
                lines.append(f"- {title}{reason}{lead_note} ({row['first_seen_date']})")

            return "\n".join(lines)
        except Exception:
            return ""

    async def _section_health_trend(self) -> str:
        """Recent engagement and health trend from Steward snapshots."""
        try:
            rows = await self._db.fetch_dicts(
                """SELECT snapshot_date, questions_asked, questions_helpful,
                              questions_unhelpful, unique_users, gaps_opened, gaps_closed
                       FROM bot_health_snapshots
                       ORDER BY snapshot_date DESC
                       LIMIT 7"""
            )
            if not rows:
                return ""
            latest = rows[0]
            total_q = sum(r["questions_asked"] or 0 for r in rows)
            total_helpful = sum(r["questions_helpful"] or 0 for r in rows)
            total_unhelpful = sum(r["questions_unhelpful"] or 0 for r in rows)
            total_gaps_opened = sum(r["gaps_opened"] or 0 for r in rows)
            total_gaps_closed = sum(r["gaps_closed"] or 0 for r in rows)
            helpfulness = (
                f"{round(total_helpful / total_q * 100)}%" if total_q > 0 else "N/A"
            )
            lines = [
                "### Health Trend (7-day)",
                f"Questions: {total_q} | Helpful: {helpfulness} | "
                f"Gaps opened: {total_gaps_opened}, closed: {total_gaps_closed}",
            ]
            if latest["unique_users"]:
                lines.append(f"Active users (latest): {latest['unique_users']}")
            return "\n".join(lines)
        except Exception:
            return ""

    async def _section_concerns(self) -> str:
        """Recurring concern threads from interaction memory."""
        try:
            from services.interaction_memory import InteractionMemory
            memory = InteractionMemory(self._db)
            concerns = await memory.get_active_concerns(limit=3)
            if not concerns:
                return ""
            lines = ["### Recurring Concerns"]
            for c in concerns:
                refs = ", ".join(c.get("artifact_refs", [])[:3])
                ref_note = f" — refs: {refs}" if refs else ""
                lines.append(
                    f"- {c['topic']} (asked {c['query_count']}x){ref_note}"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    async def _section_obligations(self) -> str:
        """Upcoming obligations from the forward planner."""
        try:
            rows = await self._db.fetch_dicts(
                """SELECT title, recurrence, next_due
                       FROM obligations
                       WHERE status = 'active' AND next_due IS NOT NULL
                       ORDER BY next_due ASC
                       LIMIT 5"""
            )
            if not rows:
                return ""
            lines = ["### Upcoming Obligations"]
            for r in rows:
                lines.append(f"- {r['title']} ({r['recurrence']}) — due {r['next_due']}")
            return "\n".join(lines)
        except Exception:
            return ""

    async def _section_proactive_nudges(self) -> str:
        """Proactive suggestions the assistant should weave into conversation."""
        try:
            from services.proactive_suggestions import get_proactive_engine
            engine = get_proactive_engine(self._db)
            nudges = await engine.get_suggestions(max_results=2)
            if not nudges:
                return ""
            lines = [
                "### Proactive Ideas (volunteer ONE if relevant)",
                "Pick the most relevant nudge and work it into your answer naturally.",
                'Frame it as a helpful observation: "By the way…" or "I had a thought…"',
            ]
            for n in nudges:
                lines.append(f"- **{n.category}**: {n.hook} → {n.suggestion}")
            return "\n".join(lines)
        except Exception:
            return ""


def _today() -> str:
    """ISO date string for today."""
    from datetime import date
    return date.today().isoformat()


# ── Module-level singleton ────────────────────────────────────────────────────

_builder: Optional[WorkspaceContextBuilder] = None


def get_workspace_context_builder(db: Any) -> WorkspaceContextBuilder:
    """Get or create the module-level workspace context singleton."""
    global _builder
    if _builder is None:
        _builder = WorkspaceContextBuilder(db)
    return _builder
