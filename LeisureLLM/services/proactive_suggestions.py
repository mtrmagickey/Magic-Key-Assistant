"""
Proactive Suggestions Engine — agentic nudges that surface opportunities
and ideas before the user asks.

This is the "Hey, I have an idea" muscle.  Instead of waiting for the user
to ask the right question, the engine scans operational state and generates
contextual suggestions the bot can volunteer during a chat or display on
the dashboard.

Trigger points:
    - Overdue / stale action items nobody has touched
    - Knowledge gaps asked many times but never resolved
    - Recurring concern threads trending upward
    - Pipeline leads going cold
    - Idle periods (no chat in N hours → volunteer a check-in)
    - Pattern connections (user asks about X, and Y relates)
    - Recent decisions that may need follow-up actions

Each suggestion is a short, natural-language nudge with:
    - A hook: conversational opener ("Hey, I noticed…")
    - A suggestion: concrete next step
    - A category: what operational area it relates to
    - An action_hint: optional tool/command the user can invoke

Design:
    - Zero LLM calls: all suggestions are template-driven from live DB state
    - Cheap: query once, cache for 10 minutes
    - Non-intrusive: max 2 suggestions per chat interaction
    - Deduplicated: don't repeat the same nudge within 24 hours
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_REFRESH_INTERVAL = 10 * 60  # 10 minutes
_MAX_SUGGESTIONS_PER_CHAT = 2
_NUDGE_COOLDOWN_HOURS = 24


@dataclass
class Nudge:
    """A single proactive suggestion."""
    hook: str               # "Hey, I noticed…"
    suggestion: str         # "Want me to draft a follow-up?"
    category: str           # "actions", "gaps", "pipeline", "concerns", "general"
    priority: float = 0.5   # 0.0–1.0, higher = more important
    action_hint: str = ""   # optional: tool or command the user can invoke
    artifact_id: str = ""   # optional: ID of the related record
    _hash: str = ""         # dedup key

    def __post_init__(self):
        if not self._hash:
            raw = f"{self.category}:{self.hook[:40]}:{self.artifact_id}"
            self._hash = hashlib.md5(raw.encode()).hexdigest()[:12]


class ProactiveSuggestionEngine:
    """
    Scans operational state and generates contextual nudges.

    Usage::

        engine = ProactiveSuggestionEngine(db)
        nudges = await engine.get_suggestions(query="Tell me about project X")
        # Returns up to 2 Nudge objects relevant to the query context
    """

    def __init__(self, db: Any, refresh_interval: float = _REFRESH_INTERVAL):
        self._db = db
        self._refresh = refresh_interval
        self._cached_nudges: List[Nudge] = []
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        self._recently_shown: Dict[str, float] = {}  # hash → timestamp

    async def get_suggestions(
        self,
        query: str = "",
        *,
        max_results: int = _MAX_SUGGESTIONS_PER_CHAT,
        categories: Optional[List[str]] = None,
    ) -> List[Nudge]:
        """
        Get proactive nudges, optionally filtered by relevance to a query.

        Nudges shown within the last 24 hours are suppressed.
        """
        all_nudges = await self._get_or_refresh()
        if not all_nudges:
            return []

        now = time.time()
        cooldown = _NUDGE_COOLDOWN_HOURS * 3600

        # Filter: not recently shown, matching category if specified
        candidates = []
        for n in all_nudges:
            last_shown = self._recently_shown.get(n._hash, 0)
            if (now - last_shown) < cooldown:
                continue
            if categories and n.category not in categories:
                continue
            candidates.append(n)

        if not candidates:
            return []

        # Score by priority + rudimentary query relevance
        if query:
            query_lower = query.lower()
            for n in candidates:
                # Boost nudges whose hook/suggestion text overlaps with query
                text = f"{n.hook} {n.suggestion}".lower()
                overlap = sum(1 for w in query_lower.split() if len(w) > 3 and w in text)
                n.priority = min(1.0, n.priority + overlap * 0.15)

        candidates.sort(key=lambda n: -n.priority)
        selected = candidates[:max_results]

        # Record that we've shown these
        for n in selected:
            self._recently_shown[n._hash] = now

        return selected

    async def build_nudge_context(
        self,
        query: str = "",
        max_results: int = _MAX_SUGGESTIONS_PER_CHAT,
    ) -> str:
        """
        Build a context string of proactive nudges for injection into
        the LLM system prompt.

        Returns empty string if no nudges are available.
        """
        nudges = await self.get_suggestions(query, max_results=max_results)
        if not nudges:
            return ""

        lines = [
            "\n## Proactive Suggestions",
            "You have noticed these things worth raising with the user.",
            "Work ONE of these into your response naturally — phrased as a helpful",
            "idea or observation, not a notification. Lead with curiosity or a pitch.",
            'E.g. "By the way — I had a thought about {topic}. {suggestion}"',
            "",
        ]
        for n in nudges:
            line = f"- [{n.category.upper()}] {n.hook} → {n.suggestion}"
            if n.action_hint:
                line += f" (user can: {n.action_hint})"
            lines.append(line)

        return "\n".join(lines)

    def format_nudges_for_display(self, nudges: List[Nudge]) -> List[Dict[str, str]]:
        """Format nudges for the frontend (SSE event or inbox card)."""
        return [
            {
                "hook": n.hook,
                "suggestion": n.suggestion,
                "category": n.category,
                "action_hint": n.action_hint,
            }
            for n in nudges
        ]

    def invalidate(self) -> None:
        """Force refresh on next call."""
        self._cached_at = 0.0

    # ── Internal: cache + scan ────────────────────────────────────────────

    async def _get_or_refresh(self) -> List[Nudge]:
        if self._cached_nudges and (time.time() - self._cached_at) < self._refresh:
            return self._cached_nudges

        async with self._lock:
            if self._cached_nudges and (time.time() - self._cached_at) < self._refresh:
                return self._cached_nudges

            try:
                self._cached_nudges = await self._scan_all()
                self._cached_at = time.time()
            except Exception as exc:
                logger.warning("Proactive suggestion scan failed: %s", exc)
                if not self._cached_nudges:
                    self._cached_nudges = []

        return self._cached_nudges

    async def _scan_all(self) -> List[Nudge]:
        """Run all scanners and merge results."""
        results: List[Nudge] = []

        scanners = [
            self._scan_overdue_actions,
            self._scan_stale_gaps,
            self._scan_trending_concerns,
            self._scan_cold_leads,
            self._scan_unactioned_decisions,
            self._scan_feedback_patterns,
            self._scan_idle_period,
        ]

        for scanner in scanners:
            try:
                nudges = await scanner()
                results.extend(nudges)
            except Exception as exc:
                logger.debug("Scanner %s failed: %s", scanner.__name__, exc)

        # Sort by priority descending
        results.sort(key=lambda n: -n.priority)
        return results

    # ── Scanners ──────────────────────────────────────────────────────────

    async def _scan_overdue_actions(self) -> List[Nudge]:
        """Detect overdue action items that need attention."""
        nudges = []
        try:
            rows = await self._db.fetch_dicts(
                """SELECT id, title, due_date, assignee, status
                       FROM tasks
                       WHERE status NOT IN ('done', 'cancelled')
                         AND due_date < date('now')
                       ORDER BY due_date ASC
                       LIMIT 5"""
            )

            for row in rows:
                days_overdue = (datetime.now() - datetime.fromisoformat(row["due_date"])).days
                assignee = row["assignee"] or "unassigned"

                if days_overdue >= 14:
                    hook = f'"{row["title"]}" has been overdue for {days_overdue} days'
                    suggestion = (
                        "Want me to check if this is still relevant, or should we close it out?"
                    )
                    priority = 0.9
                elif days_overdue >= 7:
                    hook = f'"{row["title"]}" is a week overdue ({assignee})'
                    suggestion = (
                        "I could draft a follow-up nudge, or we could re-scope the deadline."
                    )
                    priority = 0.75
                else:
                    hook = f'"{row["title"]}" slipped past its due date'
                    suggestion = "Want to extend the deadline or knock it out today?"
                    priority = 0.55

                nudges.append(Nudge(
                    hook=hook,
                    suggestion=suggestion,
                    category="actions",
                    priority=priority,
                    action_hint="review the action on the Actions page",
                    artifact_id=f"task-{row['id']}",
                ))
        except Exception as exc:
            logger.debug("Overdue action scan failed: %s", exc)

        return nudges

    async def _scan_stale_gaps(self) -> List[Nudge]:
        """Detect knowledge gaps that keep getting asked but never resolved."""
        nudges = []
        try:
            rows = await self._db.fetch_dicts(
                """SELECT id, topic, question, times_asked, priority_score
                       FROM knowledge_gaps
                       WHERE status = 'open'
                         AND times_asked >= 3
                       ORDER BY times_asked DESC, priority_score DESC
                       LIMIT 3"""
            )

            for row in rows:
                hook = (
                    f'People keep asking about "{row["topic"]}" '
                    f'({row["times_asked"]} times so far)'
                )
                suggestion = (
                    "I could start a quick interview to capture what you know, "
                    "or research it if web search is enabled."
                )
                priority = min(1.0, 0.5 + row["times_asked"] * 0.1)

                nudges.append(Nudge(
                    hook=hook,
                    suggestion=suggestion,
                    category="gaps",
                    priority=priority,
                    action_hint="start an interview from the Conversations page",
                    artifact_id=f"gap-{row['id']}",
                ))
        except Exception as exc:
            logger.debug("Stale gap scan failed: %s", exc)

        return nudges

    async def _scan_trending_concerns(self) -> List[Nudge]:
        """Detect concern threads with accelerating query counts."""
        nudges = []
        try:
            rows = await self._db.fetch_dicts(
                """SELECT id, topic, query_count, last_seen
                       FROM concern_threads
                       WHERE status = 'active'
                         AND query_count >= 4
                       ORDER BY query_count DESC
                       LIMIT 2"""
            )

            for row in rows:
                hook = (
                    f'"{row["topic"]}" keeps coming up — '
                    f'{row["query_count"]} interactions and counting'
                )
                suggestion = (
                    "This might deserve its own reference document or action item. "
                    "Want me to draft something?"
                )
                nudges.append(Nudge(
                    hook=hook,
                    suggestion=suggestion,
                    category="concerns",
                    priority=min(1.0, 0.4 + row["query_count"] * 0.08),
                    artifact_id=f"concern-{row['id']}",
                ))
        except Exception as exc:
            logger.debug("Trending concern scan failed: %s", exc)

        return nudges

    async def _scan_cold_leads(self) -> List[Nudge]:
        """Detect pipeline leads going cold (no activity in N days)."""
        nudges = []
        try:
            rows = await self._db.fetch_dicts(
                """SELECT l.id, l.name, l.stage, l.contact_name,
                              MAX(la.created_at) as last_activity
                       FROM leads l
                       LEFT JOIN lead_activity la ON la.lead_id = l.id
                       WHERE l.stage NOT IN ('won', 'lost')
                       GROUP BY l.id
                       HAVING last_activity < datetime('now', '-7 days')
                          OR last_activity IS NULL
                       ORDER BY last_activity ASC
                       LIMIT 3"""
            )

            for r in rows:
                contact = r["contact_name"] or r["name"]
                stage = r["stage"]

                if r["last_activity"]:
                    last = datetime.fromisoformat(r["last_activity"])
                    days_idle = (datetime.now() - last).days
                    hook = (
                        f"{contact} ({stage}) has gone quiet — "
                        f"no activity for {days_idle} days"
                    )
                else:
                    hook = f"{contact} ({stage}) was added but never had any follow-up"

                suggestion = (
                    "I could draft a check-in message, or we can review whether "
                    "this opportunity is still worth pursuing."
                )
                nudges.append(Nudge(
                    hook=hook,
                    suggestion=suggestion,
                    category="pipeline",
                    priority=0.65,
                    action_hint="review on the Leads page",
                    artifact_id=f"lead-{r['id']}",
                ))
        except Exception as exc:
            logger.debug("Cold lead scan failed: %s", exc)

        return nudges

    async def _scan_unactioned_decisions(self) -> List[Nudge]:
        """Detect recent decisions that may need follow-up action items."""
        nudges = []
        try:
            rows = await self._db.fetch_dicts(
                """SELECT d.id, d.title, d.decided_at
                       FROM decisions d
                       WHERE d.decided_at >= date('now', '-14 days')
                         AND d.superseded_by_decision_id IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM tasks t
                             WHERE t.title LIKE '%' || d.title || '%'
                               AND t.created_at >= d.decided_at
                         )
                       ORDER BY d.decided_at DESC
                       LIMIT 2"""
            )

            for row in rows:
                hook = (
                    f'You decided "{row["title"]}" on {row["decided_at"]}, '
                    f"but I don't see any action items tracking the follow-through"
                )
                suggestion = (
                    "Want me to create an action item to make sure this gets executed?"
                )
                nudges.append(Nudge(
                    hook=hook,
                    suggestion=suggestion,
                    category="actions",
                    priority=0.7,
                    action_hint="create an action item",
                    artifact_id=f"decision-{row['id']}",
                ))
        except Exception as exc:
            logger.debug("Unactioned decision scan failed: %s", exc)

        return nudges

    async def _scan_feedback_patterns(self) -> List[Nudge]:
        """Detect topics with high negative feedback — areas where the assistant is failing."""
        nudges = []
        try:
            rows = await self._db.fetch_dicts(
                """SELECT question, COUNT(*) AS neg_count
                       FROM response_feedback
                       WHERE feedback = 'not_helpful'
                         AND created_at >= datetime('now', '-14 days')
                       GROUP BY question
                       HAVING neg_count >= 2
                       ORDER BY neg_count DESC
                       LIMIT 3"""
            )

            for row in rows:
                question_preview = (row["question"] or "")[:80]
                neg_count = row["neg_count"]
                hook = (
                    f'Users have flagged my answer to "{question_preview}" '
                    f"as unhelpful {neg_count} times recently"
                )
                suggestion = (
                    "I could draft an improved answer if you teach me the right information, "
                    "or we could flag it as a knowledge gap to research."
                )
                nudges.append(Nudge(
                    hook=hook,
                    suggestion=suggestion,
                    category="gaps",
                    priority=min(1.0, 0.6 + neg_count * 0.1),
                    action_hint="teach me the correct answer or start an interview",
                    artifact_id=f"feedback-{hash(question_preview) & 0xFFFF}",
                ))
        except Exception as exc:
            logger.debug("Feedback pattern scan failed: %s", exc)

        return nudges

    async def _scan_idle_period(self) -> List[Nudge]:
        """Detect if there's been no chat activity for a while — offer a check-in."""
        nudges = []
        try:
            row = await self._db.fetch_one_dict(
                """SELECT MAX(created_at) as last_chat
                       FROM chat_interactions"""
            )

            if row and row["last_chat"]:
                last_chat = datetime.fromisoformat(row["last_chat"])
                hours_idle = (datetime.utcnow() - last_chat).total_seconds() / 3600

                if hours_idle >= 48:
                    hook = (
                        "It's been a couple of days since we last chatted"
                    )
                    suggestion = (
                        "Want a quick status update? I can pull together what's "
                        "overdue, what's progressing, and any gaps that need attention."
                    )
                    nudges.append(Nudge(
                        hook=hook,
                        suggestion=suggestion,
                        category="general",
                        priority=0.35,
                        action_hint="ask for a status update",
                    ))
        except Exception as exc:
            logger.debug("Idle period scan failed: %s", exc)

        return nudges


# ── Module-level singleton ────────────────────────────────────────────────────

_engine: Optional[ProactiveSuggestionEngine] = None


def get_proactive_engine(db: Any) -> ProactiveSuggestionEngine:
    """Get or create the module-level proactive suggestion engine singleton."""
    global _engine
    if _engine is None:
        _engine = ProactiveSuggestionEngine(db)
    return _engine
