"""
Interaction Memory — query logging, concern thread detection,
persistent conversation sessions, and operational continuity.

This service records every chat interaction and clusters them into
"concern threads" — recurring topics the user keeps returning to.
When a concern thread accumulates enough queries, the system can
proactively surface relevant context or suggest actions.

It also provides **persistent conversation memory**: full server-side
session storage so the LLM can reference prior conversations even
after the browser tab is closed.

Design:
    - Every chat query is logged (query, tools used, artifact refs, sources)
    - Simple keyword-based clustering detects recurring topics
    - Concern threads surface in future chat context when relevant
    - Conversation sessions are persisted and auto-summarised
    - Privacy: no raw responses stored — only summaries and metadata
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Stop words to exclude from keyword extraction
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "about", "between",
    "through", "after", "before", "during", "under", "above", "up", "down",
    "out", "off", "over", "and", "but", "or", "nor", "not", "so", "yet",
    "both", "either", "neither", "each", "every", "all", "any", "few",
    "more", "most", "other", "some", "such", "no", "than", "too", "very",
    "just", "also", "now", "here", "there", "when", "where", "why", "how",
    "what", "which", "who", "whom", "this", "that", "these", "those",
    "i", "me", "my", "we", "our", "you", "your", "it", "its", "they",
    "them", "their", "he", "she", "him", "her", "his",
})


def _extract_keywords(text: str, max_keywords: int = 8) -> List[str]:
    """Extract significant keywords from a query.

    Detects multi-word proper nouns (e.g. "Jane Doe", "Acme Corp")
    and keeps them as single keywords so downstream matching works on
    full entity names rather than individual fragments.
    """
    # ── 1. Extract multi-word proper-noun phrases ────────────────────────
    # Sequences of 2-4 capitalised words (Title Case or ALL CAPS).
    # E.g. "Acme Corp", "Jane Doe", "John Smith"
    entity_phrases: List[str] = []
    # Match runs of capitalised words (2-4 tokens), followed by a non-cap
    for m in re.finditer(
        r"\b([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*){1,3})\b", text
    ):
        phrase = m.group(1).strip()
        # Skip if the phrase is just the start of a sentence (single cap word)
        words_in_phrase = phrase.split()
        if len(words_in_phrase) >= 2:
            entity_phrases.append(phrase.lower())

    # ── 2. Standard single-word keyword extraction ───────────────────────
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    meaningful = [w for w in words if w not in _STOP_WORDS]

    # Remove individual words that are already captured inside an entity phrase
    entity_words: set = set()
    for phrase in entity_phrases:
        for w in phrase.split():
            entity_words.add(w)
    meaningful = [w for w in meaningful if w not in entity_words]

    counts = Counter(meaningful)
    single_keywords = [word for word, _ in counts.most_common(max_keywords)]

    # ── 3. Merge: entity phrases first, then fill with single keywords ───
    result: List[str] = list(dict.fromkeys(entity_phrases))  # dedup, preserve order
    for kw in single_keywords:
        if len(result) >= max_keywords:
            break
        if kw not in result:
            result.append(kw)

    return result[:max_keywords]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors (no numpy dependency)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class InteractionMemory:
    """
    Manages the chat_interactions and concern_threads tables.

    Usage:
        memory = InteractionMemory(db)
        await memory.log_interaction(query="...", ...)
        threads = await memory.get_active_concerns(limit=5)
        related = await memory.find_related_concerns("Henderson timeline")
    """

    def __init__(self, db: Any):
        self.db = db

    async def log_interaction(
        self,
        query: str,
        *,
        response_summary: str = "",
        source: str = "web",
        user_id: str = "",
        tools_invoked: Optional[List[str]] = None,
        artifact_refs: Optional[List[str]] = None,
        sources_used: Optional[List[str]] = None,
    ) -> int:
        """
        Record a chat interaction and update concern threads.

        Returns the chat_interactions row ID.
        """
        # Detect or create concern thread
        concern_thread = await self._match_or_create_concern(query, artifact_refs)

        try:
            async with self.db.acquire() as conn:
                async with conn.execute(
                    """INSERT INTO chat_interactions
                       (query, response_summary, source, user_id,
                        tools_invoked, artifact_refs, sources_used,
                        concern_thread, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        query[:1000],
                        (response_summary or "")[:500],
                        source,
                        user_id or None,
                        json.dumps(tools_invoked) if tools_invoked else None,
                        json.dumps(artifact_refs) if artifact_refs else None,
                        json.dumps(sources_used) if sources_used else None,
                        concern_thread,
                        datetime.utcnow().isoformat(),
                    ),
                ) as cur:
                    row_id = cur.lastrowid
                await conn.commit()
            return row_id or 0
        except Exception as exc:
            logger.warning("Failed to log interaction: %s", exc)
            return 0

    async def _match_or_create_concern(
        self,
        query: str,
        artifact_refs: Optional[List[str]] = None,
    ) -> Optional[str]:
        """
        Match query against existing concern threads.

        Two-phase matching:
        1. Keyword overlap (>= 2 shared keywords) — fast, exact.
        2. Embedding similarity (cosine >= 0.75) — catches synonyms
           like 'staff turnover' vs 'personnel churn'.

        If no match and the topic recurs (>= 3 similar recent queries),
        create a new concern thread.
        """
        keywords = _extract_keywords(query)
        if not keywords:
            return None

        try:
            async with self.db.acquire() as conn:
                # Check existing active concerns
                async with conn.execute(
                    "SELECT id, topic, keywords, query_count, artifact_refs FROM concern_threads WHERE status = 'active'"
                ) as cur:
                    rows = await cur.fetchall()

                best_match = None
                best_overlap = 0

                for row in rows:
                    existing_kw = json.loads(row["keywords"] or "[]")
                    overlap = len(set(keywords) & set(existing_kw))
                    if overlap >= 2 and overlap > best_overlap:
                        best_match = dict(row)
                        best_overlap = overlap

                now = datetime.utcnow().isoformat()

                if best_match:
                    # Update existing concern thread
                    merged_kw = list(set(json.loads(best_match["keywords"] or "[]") + keywords))[:15]
                    merged_refs = list(set(
                        json.loads(best_match["artifact_refs"] or "[]") +
                        (artifact_refs or [])
                    ))[:20]
                    await conn.execute(
                        """UPDATE concern_threads
                           SET last_seen = ?, query_count = query_count + 1,
                               keywords = ?, artifact_refs = ?
                           WHERE id = ?""",
                        (now, json.dumps(merged_kw), json.dumps(merged_refs), best_match["id"]),
                    )
                    await conn.commit()
                    return best_match["topic"]

                # Phase 2: Semantic similarity fallback for low-keyword-overlap threads
                semantic_match = await self._semantic_concern_match(query, rows, artifact_refs)
                if semantic_match:
                    return semantic_match

                # Check if we should create a new concern thread
                # Look at recent queries for keyword overlap
                async with conn.execute(
                    """SELECT query FROM chat_interactions
                       WHERE created_at >= datetime('now', '-14 days')
                       ORDER BY created_at DESC LIMIT 50"""
                ) as cur:
                    recent = await cur.fetchall()

                similar_count = 0
                for r in recent:
                    recent_kw = _extract_keywords(r["query"])
                    if len(set(keywords) & set(recent_kw)) >= 2:
                        similar_count += 1

                if similar_count >= 2:
                    # Recurring topic — create a concern thread
                    topic = " ".join(keywords[:3])
                    await conn.execute(
                        """INSERT INTO concern_threads
                           (topic, keywords, first_seen, last_seen, query_count, artifact_refs)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            topic,
                            json.dumps(keywords),
                            now,
                            now,
                            similar_count + 1,
                            json.dumps(artifact_refs or []),
                        ),
                    )
                    await conn.commit()
                    return topic

        except Exception as exc:
            logger.warning("Concern thread matching failed: %s", exc)

        return None

    async def _semantic_concern_match(
        self,
        query: str,
        concern_rows: list,
        artifact_refs: Optional[List[str]] = None,
        threshold: float = 0.75,
    ) -> Optional[str]:
        """Embedding-based fallback for concern threads with <2 keyword overlap.

        Uses cosine similarity between the query and each thread's topic
        text.  Only fires when keyword matching already failed.
        """
        if not concern_rows:
            return None
        try:
            from core.chroma_factory import get_embeddings

            embeddings = get_embeddings()
            if embeddings is None:
                return None

            query_vec = embeddings.embed_query(query)

            best_sim = 0.0
            best_row = None
            for row in concern_rows:
                topic = row["topic"] or ""
                kw_text = " ".join(json.loads(row["keywords"] or "[]"))
                compare_text = f"{topic} {kw_text}".strip()
                if not compare_text:
                    continue
                topic_vec = embeddings.embed_query(compare_text)
                sim = _cosine_similarity(query_vec, topic_vec)
                if sim > best_sim:
                    best_sim = sim
                    best_row = row

            if best_sim >= threshold and best_row is not None:
                row_dict = dict(best_row)
                keywords = _extract_keywords(query)
                merged_kw = list(set(json.loads(row_dict["keywords"] or "[]") + keywords))[:15]
                merged_refs = list(set(
                    json.loads(row_dict["artifact_refs"] or "[]") + (artifact_refs or [])
                ))[:20]
                now = datetime.utcnow().isoformat()
                await self.db.execute(
                    """UPDATE concern_threads
                    SET last_seen = ?, query_count = query_count + 1,
                    keywords = ?, artifact_refs = ?
                    WHERE id = ?""",
                    (now, json.dumps(merged_kw), json.dumps(merged_refs), row_dict["id"]),
                    )
                logger.debug(
                    "Semantic concern match: query='%s' → thread '%s' (sim=%.2f)",
                    query[:60], row_dict["topic"], best_sim,
                )
                return row_dict["topic"]
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("Semantic concern matching failed: %s", exc)
        return None

    async def get_active_concerns(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get active concern threads, ordered by recency."""
        try:
            async with self.db.acquire() as conn, conn.execute(
                """SELECT id, topic, keywords, first_seen, last_seen,
                              query_count, artifact_refs, status
                       FROM concern_threads
                       WHERE status = 'active'
                       ORDER BY last_seen DESC
                       LIMIT ?""",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
            return [
                {
                    **dict(r),
                    "keywords": json.loads(r["keywords"] or "[]"),
                    "artifact_refs": json.loads(r["artifact_refs"] or "[]"),
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("Failed to get concerns: %s", exc)
            return []

    async def find_related_concerns(
        self,
        query: str,
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        Find concern threads related to a query.

        Returns threads with keyword overlap, useful for injecting
        into chat context: "You've asked about X 4 times this month."
        """
        keywords = _extract_keywords(query)
        if not keywords:
            return []

        concerns = await self.get_active_concerns(limit=50)
        scored = []
        for c in concerns:
            overlap = len(set(keywords) & set(c.get("keywords", [])))
            if overlap >= 1:
                scored.append((overlap, c))

        scored.sort(key=lambda x: (-x[0], x[1].get("query_count", 0)))
        return [c for _, c in scored[:limit]]

    async def get_recent_interactions(
        self,
        limit: int = 20,
        concern_thread: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent chat interactions, optionally filtered by concern thread."""
        try:
            async with self.db.acquire() as conn:
                if concern_thread:
                    async with conn.execute(
                        """SELECT * FROM chat_interactions
                           WHERE concern_thread = ?
                           ORDER BY created_at DESC LIMIT ?""",
                        (concern_thread, limit),
                    ) as cur:
                        rows = await cur.fetchall()
                else:
                    async with conn.execute(
                        "SELECT * FROM chat_interactions ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ) as cur:
                        rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to get interactions: %s", exc)
            return []

    async def mark_concern_resolved(self, concern_id: int) -> bool:
        """Mark a concern thread as resolved."""
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE concern_threads SET status = 'resolved' WHERE id = ?",
                    (concern_id,),
                )
                await conn.commit()
            return True
        except Exception as exc:
            logger.warning("Failed to resolve concern: %s", exc)
            return False

    async def build_concern_context(self, query: str) -> str:
        """
        Build a context string about related concerns for injection
        into the LLM system prompt.

        Returns empty string if no relevant concerns found.
        """
        related = await self.find_related_concerns(query, limit=2)
        if not related:
            return ""

        lines = ["\n## Recurring Concerns"]
        for c in related:
            refs = ", ".join(c.get("artifact_refs", [])[:5])
            lines.append(
                f"- **{c['topic']}** — asked {c['query_count']} time(s), "
                f"last on {c.get('last_seen', 'unknown')}"
                + (f" — related records: {refs}" if refs else "")
            )
        lines.append(
            "Consider referencing these recurring topics if relevant to the current query."
        )
        return "\n".join(lines)


# =============================================================================
# PERSISTENT CONVERSATION MEMORY
# =============================================================================

_CONV_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS conversation_sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_active TEXT NOT NULL DEFAULT (datetime('now')),
    turn_count INTEGER DEFAULT 0,
    summary TEXT DEFAULT '',
    topics TEXT DEFAULT '[]',
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'archived'))
);
"""

_CONV_TURN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    tools_used TEXT,
    sources_used TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES conversation_sessions(id)
);
"""

_CONV_TURN_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_conv_turns_session
    ON conversation_turns(session_id, created_at);
"""

# Maximum recent sessions to inject into context
_MAX_CONTEXT_SESSIONS = 3
# Maximum turn content length stored
_MAX_TURN_CONTENT = 2000


class ConversationStore:
    """
    Server-side persistent conversation memory.

    Stores full conversation sessions (user + assistant turns) in SQLite.
    Provides recall of recent conversations for cross-session context,
    and auto-summarises old sessions to keep storage bounded.

    Usage::

        store = ConversationStore(db)
        await store.ensure_tables()

        # Start or resume a session
        session_id = await store.get_or_create_session(conversation_id)

        # Record turns
        await store.add_turn(session_id, "user", "What's overdue?")
        await store.add_turn(session_id, "assistant", "There are 3 overdue actions...")

        # Build context for the LLM
        ctx = await store.build_memory_context(current_query)
    """

    def __init__(self, db: Any):
        self.db = db
        self._tables_ensured = False

    async def ensure_tables(self) -> None:
        """Create conversation tables if they don't exist."""
        if self._tables_ensured:
            return
        try:
            async with self.db.acquire() as conn:
                await conn.executescript(
                    _CONV_TABLE_SQL + _CONV_TURN_TABLE_SQL + _CONV_TURN_INDEX_SQL
                )
                await conn.commit()
            self._tables_ensured = True
        except Exception as exc:
            logger.warning("Failed to ensure conversation tables: %s", exc)

    async def get_or_create_session(
        self, conversation_id: Optional[str] = None
    ) -> str:
        """Resume an existing session or create a new one."""
        await self.ensure_tables()

        if conversation_id:
            try:
                async with self.db.acquire() as conn:
                    async with conn.execute(
                        "SELECT id FROM conversation_sessions WHERE id = ? AND status = 'active'",
                        (conversation_id,),
                    ) as cur:
                        row = await cur.fetchone()
                    if row:
                        await conn.execute(
                            "UPDATE conversation_sessions SET last_active = datetime('now') WHERE id = ?",
                            (conversation_id,),
                        )
                        await conn.commit()
                        return conversation_id
            except Exception as e:
                logger.warning("get_or_create_session: suppressed %s", e)

        # Create new session
        new_id = uuid.uuid4().hex[:16]
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO conversation_sessions (id) VALUES (?)",
                    (new_id,),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to create conversation session: %s", exc)
        return new_id

    async def add_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        tools_used: Optional[List[str]] = None,
        sources_used: Optional[List[str]] = None,
    ) -> None:
        """Record a conversation turn."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO conversation_turns
                       (session_id, role, content, tools_used, sources_used)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        role,
                        content[:_MAX_TURN_CONTENT]
                        + (f" [TRUNCATED: {len(content) - _MAX_TURN_CONTENT} chars omitted]"
                           if len(content) > _MAX_TURN_CONTENT else ""),
                        json.dumps(tools_used) if tools_used else None,
                        json.dumps(sources_used) if sources_used else None,
                    ),
                )
                await conn.execute(
                    """UPDATE conversation_sessions
                       SET turn_count = turn_count + 1,
                           last_active = datetime('now')
                       WHERE id = ?""",
                    (session_id,),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to add conversation turn: %s", exc)

    async def get_session_turns(
        self, session_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Retrieve recent turns for a session."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn, conn.execute(
                """SELECT role, content, tools_used, sources_used, created_at
                       FROM conversation_turns
                       WHERE session_id = ?
                       ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ) as cur:
                rows = await cur.fetchall()
            # Reverse so they're in chronological order
            return [dict(r) for r in reversed(rows)]
        except Exception as exc:
            logger.warning("Failed to get session turns: %s", exc)
            return []

    async def build_memory_context(self, current_query: str) -> str:
        """
        Build a context string from recent conversation sessions.

        Searches across recent sessions for keyword-relevant turns
        and includes session summaries.  Injected into the system prompt
        to give the LLM cross-session memory.
        """
        await self.ensure_tables()
        keywords = _extract_keywords(current_query, max_keywords=5)
        if not keywords:
            return ""

        try:
            async with self.db.acquire() as conn:
                # Get recent active sessions with summaries
                async with conn.execute(
                    """SELECT id, summary, topics, last_active, turn_count
                       FROM conversation_sessions
                       WHERE status = 'active'
                         AND last_active >= datetime('now', '-7 days')
                       ORDER BY last_active DESC
                       LIMIT 10"""
                ) as cur:
                    sessions = await cur.fetchall()

                # Also pull archived session summaries for long-term recall
                async with conn.execute(
                    """SELECT id, summary, topics, last_active, turn_count
                       FROM conversation_sessions
                       WHERE status = 'archived'
                         AND summary IS NOT NULL AND summary != ''
                       ORDER BY last_active DESC
                       LIMIT 5"""
                ) as cur:
                    archived_sessions = await cur.fetchall()

            if not sessions and not archived_sessions:
                return ""

            # Score sessions by keyword relevance
            scored: List[tuple] = []
            for s in sessions:
                s_dict = dict(s)
                summary = (s_dict.get("summary") or "").lower()
                topics = (s_dict.get("topics") or "").lower()
                overlap = sum(1 for kw in keywords if kw in summary or kw in topics)
                if overlap >= 2:
                    scored.append((overlap, s_dict))

            scored.sort(key=lambda x: -x[0])
            top_sessions = [s for _, s in scored[:_MAX_CONTEXT_SESSIONS]]

            # Score archived sessions the same way
            archived_scored: List[tuple] = []
            for s in (archived_sessions or []):
                s_dict = dict(s)
                summary = (s_dict.get("summary") or "").lower()
                topics = (s_dict.get("topics") or "").lower()
                overlap = sum(1 for kw in keywords if kw in summary or kw in topics)
                if overlap >= 2:
                    archived_scored.append((overlap, s_dict))
            archived_scored.sort(key=lambda x: -x[0])
            top_archived = [s for _, s in archived_scored[:2]]

            if not top_sessions and not top_archived:
                return ""

            lines = ["\n## Previous Conversations"]
            for s in top_sessions:
                summary = s.get("summary") or "(no summary yet)"
                lines.append(
                    f"- **Session {s['id'][:8]}** ({s['last_active']}, "
                    f"{s['turn_count']} turns): {summary}"
                )

            if top_archived:
                lines.append("\n### Older Conversations (archived)")
                for s in top_archived:
                    summary = s.get("summary") or "(no summary)"
                    lines.append(
                        f"- **Session {s['id'][:8]}** ({s['last_active']}, "
                        f"summary only): {summary}"
                    )

            lines.append(
                "Use previous conversations for continuity — "
                "reference prior answers when relevant."
            )
            return "\n".join(lines)

        except Exception as exc:
            logger.debug("Failed to build memory context: %s", exc)
            return ""

    async def summarise_session(
        self,
        session_id: str,
        summary_fn: Optional[Any] = None,
    ) -> str:
        """
        Generate and store a summary for a session.

        If summary_fn is provided (an async callable that takes text
        and returns a summary string), uses it for LLM-powered
        summarisation.  Otherwise builds a simple keyword summary.
        """
        turns = await self.get_session_turns(session_id, limit=50)
        if not turns:
            return ""

        # Build a transcript
        transcript_parts = []
        for t in turns:
            prefix = "User" if t["role"] == "user" else "Assistant"
            transcript_parts.append(f"{prefix}: {t['content'][:300]}")
        transcript = "\n".join(transcript_parts)

        # Extract topics
        all_text = " ".join(t["content"] for t in turns if t["role"] == "user")
        topics = _extract_keywords(all_text, max_keywords=6)

        # Generate summary
        if summary_fn:
            try:
                transcript_block = transcript[:3000]
                if len(transcript) > 3000:
                    transcript_block += f"\n[TRUNCATED: {len(transcript) - 3000} chars omitted]"
                summary = await summary_fn(
                    f"Summarise this conversation in 2-3 sentences. "
                    f"Focus on what was asked and what was decided:\n\n{transcript_block}"
                )
            except Exception:
                summary = f"Discussed: {', '.join(topics[:4])}" if topics else "Short conversation"
        else:
            summary = f"Discussed: {', '.join(topics[:4])}" if topics else "Short conversation"

        # Store
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """UPDATE conversation_sessions
                       SET summary = ?, topics = ?
                       WHERE id = ?""",
                    (summary[:500]
                     + (" [TRUNCATED]" if len(summary) > 500 else ""),
                     json.dumps(topics), session_id),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to store session summary: %s", exc)

        return summary

    async def archive_old_sessions(self, days: int = 30) -> int:
        """Archive sessions older than N days."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn:
                async with conn.execute(
                    """UPDATE conversation_sessions
                       SET status = 'archived'
                       WHERE status = 'active'
                         AND last_active < datetime('now', ? || ' days')""",
                    (f"-{days}",),
                ) as cur:
                    count = cur.rowcount
                await conn.commit()
            if count:
                logger.info("Archived %d old conversation sessions", count)
            return count or 0
        except Exception as exc:
            logger.warning("Failed to archive old sessions: %s", exc)
            return 0
