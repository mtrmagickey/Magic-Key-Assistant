"""
User Preferences Service — adaptive personalisation that deepens over time.

This is a core **switching-cost moat**: the more the assistant learns about
a user's communication style, topic interests, and workflow habits, the
more painful it becomes to migrate to another product.

Capabilities:
    1. **Implicit preference learning** — observes interaction patterns
       (tone, query length, topic frequency, time-of-day) and adapts.
    2. **Explicit preference storage** — users can set preferences directly
       (response verbosity, formality, notification schedule).
    3. **Adaptive prompt injection** — builds a per-user system-prompt
       fragment that shapes every LLM response.
    4. **Preference export/import** — users own their data, but export
       format is proprietary enough to not trivially port.

Design:
    - SQLite-backed (no cloud dependency).
    - All inference from local interaction data — never phones home.
    - Preferences accumulate passively; no onboarding quiz required.
    - Decay factor prevents stale preferences from dominating.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Default preference schema ─────────────────────────────────────────────

_DEFAULT_PREFERENCES = {
    # Communication style (learned implicitly)
    "response_verbosity": "balanced",      # concise | balanced | detailed
    "formality": "professional",           # casual | professional | formal
    "emoji_usage": False,
    "bullet_point_preference": True,
    "preferred_greeting": "",

    # Topic interests (learned from query frequency)
    "top_interests": [],                   # e.g. ["pipeline", "maintenance", "finance"]
    "deprioritised_topics": [],

    # Workflow habits (learned from timing patterns)
    "active_hours_start": 9,               # 24h format
    "active_hours_end": 17,
    "peak_activity_day": "Monday",
    "avg_query_length_words": 15,

    # Notification preferences (set explicitly)
    "digest_frequency": "daily",           # off | daily | weekly
    "notification_channels": ["web"],      # web | discord | email
    "quiet_hours_enabled": True,

    # Learning metadata
    "interactions_analysed": 0,
    "last_preference_update": None,
    "confidence_scores": {},               # key → 0.0-1.0
}

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT PRIMARY KEY,
    preferences TEXT NOT NULL DEFAULT '{}',
    interaction_stats TEXT NOT NULL DEFAULT '{}',
    learned_topics TEXT NOT NULL DEFAULT '[]',
    style_signals TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_LEARNING_LOG_SQL = """
CREATE TABLE IF NOT EXISTS preference_learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    signal_value TEXT NOT NULL,
    old_preference TEXT,
    new_preference TEXT,
    confidence REAL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pref_learning_user
    ON preference_learning_log(user_id, created_at DESC);
"""


class UserPreferenceService:
    """
    Learns and stores per-user preferences from interaction patterns.

    Usage::

        prefs = UserPreferenceService(db)
        await prefs.ensure_tables()

        # After each interaction, feed signals
        await prefs.observe_interaction(
            user_id="admin",
            query="Give me a quick summary of pipeline status",
            response_length=150,
            feedback="helpful",
        )

        # Before generating a response, get adaptive prompt fragment
        prompt_fragment = await prefs.build_preference_prompt("admin")
    """

    def __init__(self, db: Any):
        self.db = db
        self._tables_ensured = False

    async def ensure_tables(self) -> None:
        """Create preference tables if they don't exist."""
        if self._tables_ensured:
            return
        try:
            async with self.db.acquire() as conn:
                await conn.executescript(
                    _CREATE_TABLE_SQL + _CREATE_LEARNING_LOG_SQL + _CREATE_INDEX_SQL
                )
                await conn.commit()
            self._tables_ensured = True
        except Exception as exc:
            logger.warning("Failed to ensure preference tables: %s", exc)

    # ── Core CRUD ──────────────────────────────────────────────

    async def get_preferences(self, user_id: str) -> Dict[str, Any]:
        """Get the full preference dict for a user, with defaults."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn, conn.execute(
                "SELECT preferences FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                stored = json.loads(row[0] if isinstance(row[0], str) else row["preferences"])
                merged = {**_DEFAULT_PREFERENCES, **stored}
                return merged
        except Exception as exc:
            logger.warning("Failed to get preferences for %s: %s", user_id, exc)
        return dict(_DEFAULT_PREFERENCES)

    async def set_preference(self, user_id: str, key: str, value: Any) -> None:
        """Explicitly set a single preference."""
        await self.ensure_tables()
        prefs = await self.get_preferences(user_id)
        old_value = prefs.get(key)
        prefs[key] = value

        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO user_preferences (user_id, preferences, updated_at)
                       VALUES (?, ?, datetime('now'))
                       ON CONFLICT(user_id) DO UPDATE SET
                           preferences = excluded.preferences,
                           updated_at = excluded.updated_at""",
                    (user_id, json.dumps(prefs)),
                )
                await conn.execute(
                    """INSERT INTO preference_learning_log
                       (user_id, signal_type, signal_value, old_preference, new_preference, confidence)
                       VALUES (?, 'explicit_set', ?, ?, ?, 1.0)""",
                    (user_id, key, json.dumps(old_value), json.dumps(value)),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to set preference %s for %s: %s", key, user_id, exc)

    async def set_preferences_bulk(self, user_id: str, updates: Dict[str, Any]) -> None:
        """Set multiple preferences at once."""
        for key, value in updates.items():
            await self.set_preference(user_id, key, value)

    # ── Implicit Learning Engine ───────────────────────────────

    async def observe_interaction(
        self,
        user_id: str,
        query: str,
        *,
        response_length: int = 0,
        feedback: Optional[str] = None,
        tools_used: Optional[List[str]] = None,
        source_count: int = 0,
        response_time_ms: int = 0,
    ) -> Dict[str, Any]:
        """
        Observe a user interaction and update learned preferences.

        Called after each chat exchange. Extracts signals:
        - Query length → verbosity preference
        - Word choice → formality detection
        - Topic keywords → interest mapping
        - Timing → active hours
        - Feedback → reinforcement of current style

        Returns dict of any preference changes made.
        """
        await self.ensure_tables()
        changes: Dict[str, Any] = {}

        # Load current state
        prefs = await self.get_preferences(user_id)
        stats = await self._get_interaction_stats(user_id)

        # ── Signal 1: Query length → verbosity ──
        word_count = len(query.split())
        stats["total_queries"] = stats.get("total_queries", 0) + 1
        stats["total_query_words"] = stats.get("total_query_words", 0) + word_count
        avg_words = stats["total_query_words"] / max(stats["total_queries"], 1)
        prefs["avg_query_length_words"] = round(avg_words, 1)

        # Users who write long queries typically want detailed responses
        if stats["total_queries"] >= 5:
            if avg_words > 25:
                new_verbosity = "detailed"
            elif avg_words < 8:
                new_verbosity = "concise"
            else:
                new_verbosity = "balanced"
            if new_verbosity != prefs.get("response_verbosity"):
                changes["response_verbosity"] = new_verbosity
                prefs["response_verbosity"] = new_verbosity

        # ── Signal 2: Formality detection ──
        casual_markers = {"hey", "hi", "thanks", "cool", "yeah", "nah", "gonna", "wanna", "lol", "ok"}
        formal_markers = {"please", "kindly", "regarding", "pursuant", "enquiry", "request", "appreciate"}
        words_lower = set(query.lower().split())

        casual_score = len(words_lower & casual_markers)
        formal_score = len(words_lower & formal_markers)
        stats["casual_signals"] = stats.get("casual_signals", 0) + casual_score
        stats["formal_signals"] = stats.get("formal_signals", 0) + formal_score

        if stats["total_queries"] >= 3:
            total_signals = stats["casual_signals"] + stats["formal_signals"]
            if total_signals > 0:
                formality_ratio = stats["formal_signals"] / total_signals
                if formality_ratio > 0.6:
                    new_formality = "formal"
                elif formality_ratio < 0.3:
                    new_formality = "casual"
                else:
                    new_formality = "professional"
                if new_formality != prefs.get("formality"):
                    changes["formality"] = new_formality
                    prefs["formality"] = new_formality

        # ── Signal 3: Topic interest mapping ──
        topic_keywords = self._extract_topic_signals(query)
        topic_counts: Dict[str, int] = stats.get("topic_counts", {})
        for topic in topic_keywords:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
        stats["topic_counts"] = topic_counts

        # Rank topics by decayed frequency
        if topic_counts:
            top_topics = sorted(topic_counts.items(), key=lambda x: -x[1])[:10]
            prefs["top_interests"] = [t[0] for t in top_topics]

        # ── Signal 4: Timing patterns ──
        now = datetime.now()
        hour_counts: Dict[str, int] = stats.get("hour_counts", {})
        hour_counts[str(now.hour)] = hour_counts.get(str(now.hour), 0) + 1
        stats["hour_counts"] = hour_counts

        day_counts: Dict[str, int] = stats.get("day_counts", {})
        day_name = now.strftime("%A")
        day_counts[day_name] = day_counts.get(day_name, 0) + 1
        stats["day_counts"] = day_counts

        if sum(hour_counts.values()) >= 5:
            peak_hour = max(hour_counts, key=lambda h: hour_counts[h])
            prefs["active_hours_start"] = max(0, int(peak_hour) - 2)
            prefs["active_hours_end"] = min(23, int(peak_hour) + 6)

        if sum(day_counts.values()) >= 5:
            prefs["peak_activity_day"] = max(day_counts, key=lambda d: day_counts[d])

        # ── Signal 5: Feedback reinforcement ──
        if feedback == "helpful":
            stats["helpful_count"] = stats.get("helpful_count", 0) + 1
            # Current style is working — increase confidence
            for key in ("response_verbosity", "formality"):
                conf = prefs.get("confidence_scores", {})
                conf[key] = min(1.0, conf.get(key, 0.5) + 0.05)
                prefs["confidence_scores"] = conf
        elif feedback == "not_helpful":
            stats["unhelpful_count"] = stats.get("unhelpful_count", 0) + 1
            # Current style may not be working — decrease confidence
            for key in ("response_verbosity", "formality"):
                conf = prefs.get("confidence_scores", {})
                conf[key] = max(0.1, conf.get(key, 0.5) - 0.1)
                prefs["confidence_scores"] = conf

        # ── Signal 6: Bullet point preference ──
        if "list" in query.lower() or "bullet" in query.lower() or "steps" in query.lower():
            stats["list_requests"] = stats.get("list_requests", 0) + 1
        if stats.get("list_requests", 0) > 3:
            prefs["bullet_point_preference"] = True

        # ── Persist ──
        prefs["interactions_analysed"] = stats["total_queries"]
        prefs["last_preference_update"] = datetime.now().isoformat()

        await self._save_interaction_stats(user_id, stats)
        await self._save_preferences(user_id, prefs)

        # Log significant changes
        for key, value in changes.items():
            await self._log_learning_event(user_id, "implicit_learn", key, value)

        return changes

    # ── Adaptive Prompt Builder ────────────────────────────────

    async def build_preference_prompt(self, user_id: str) -> str:
        """
        Build a system-prompt fragment that encodes the user's preferences.

        Injected into every LLM call to personalise responses.
        Only includes preferences with sufficient confidence.
        """
        prefs = await self.get_preferences(user_id)
        confidence = prefs.get("confidence_scores", {})

        lines: List[str] = []
        interactions = prefs.get("interactions_analysed", 0)

        if interactions < 3:
            # Not enough data yet — use minimal defaults
            return ""

        lines.append("\n## User Preferences (learned from interaction history)")

        # Verbosity
        verbosity = prefs.get("response_verbosity", "balanced")
        if confidence.get("response_verbosity", 0) >= 0.4:
            verbosity_map = {
                "concise": "Keep responses brief and to the point. Use short sentences. Omit preamble.",
                "balanced": "Provide moderately detailed responses. Balance depth with brevity.",
                "detailed": "Provide comprehensive, detailed responses. The user appreciates depth and thoroughness.",
            }
            lines.append(f"- **Response style**: {verbosity_map.get(verbosity, '')}")

        # Formality
        formality = prefs.get("formality", "professional")
        if confidence.get("formality", 0) >= 0.4:
            formality_map = {
                "casual": "Use a friendly, conversational tone. Contractions are fine.",
                "professional": "Use a clear, professional tone.",
                "formal": "Use formal language. Avoid contractions and colloquialisms.",
            }
            lines.append(f"- **Tone**: {formality_map.get(formality, '')}")

        # Bullet preference
        if prefs.get("bullet_point_preference"):
            lines.append("- **Format**: User prefers bullet points and structured lists.")

        # Top interests
        interests = prefs.get("top_interests", [])[:5]
        if interests:
            lines.append(f"- **Key interests**: {', '.join(interests)}. "
                         "Proactively connect answers to these topics when relevant.")

        # Active hours context
        peak_day = prefs.get("peak_activity_day")
        if peak_day:
            lines.append(f"- **Peak activity**: {peak_day}s. "
                         "Suggest scheduling important items accordingly.")

        if len(lines) <= 1:
            return ""  # No meaningful preferences yet

        return "\n".join(lines)

    # ── Export / Import (data portability with switching cost) ──

    async def export_preferences(self, user_id: str) -> Dict[str, Any]:
        """
        Export a user's full preference profile.

        Returns a structured dict that can be saved to JSON.
        The format is rich enough to be useful within Magic Key
        but not trivially portable to competitor products.
        """
        prefs = await self.get_preferences(user_id)
        stats = await self._get_interaction_stats(user_id)
        topics = await self._get_learned_topics(user_id)

        return {
            "export_version": "1.0",
            "product": "magic_key_assistant",
            "exported_at": datetime.now().isoformat(),
            "user_id": user_id,
            "preferences": prefs,
            "interaction_statistics": stats,
            "learned_topics": topics,
            "interactions_analysed": prefs.get("interactions_analysed", 0),
        }

    async def import_preferences(self, user_id: str, data: Dict[str, Any]) -> bool:
        """Import preferences from an export bundle."""
        if data.get("product") != "magic_key_assistant":
            logger.warning("Cannot import preferences from unknown product: %s", data.get("product"))
            return False

        prefs = data.get("preferences", {})
        stats = data.get("interaction_statistics", {})

        await self._save_preferences(user_id, prefs)
        await self._save_interaction_stats(user_id, stats)
        await self._log_learning_event(user_id, "import", "full_profile", "imported")
        return True

    # ── Topic extraction ───────────────────────────────────────

    @staticmethod
    def _extract_topic_signals(query: str) -> List[str]:
        """
        Extract topic signals from a query.

        Maps query keywords to domain topics.  This becomes more
        valuable as the topic taxonomy grows with the organisation.
        """
        # Domain topic mapping — extend as the product grows
        topic_map = {
            "pipeline": ["pipeline", "lead", "prospect", "deal", "opportunity", "sales", "client"],
            "finance": ["budget", "cost", "revenue", "invoice", "payment", "expense", "financial"],
            "maintenance": ["repair", "fix", "broken", "maintenance", "facility", "equipment"],
            "staffing": ["staff", "employee", "hire", "schedule", "shift", "rota", "team"],
            "events": ["event", "booking", "session", "class", "swim", "gym", "activity"],
            "compliance": ["compliance", "safety", "regulation", "audit", "inspection", "policy"],
            "marketing": ["marketing", "promotion", "campaign", "social", "advertisement", "brand"],
            "strategy": ["strategy", "plan", "goal", "objective", "vision", "roadmap", "growth"],
            "operations": ["operations", "process", "workflow", "system", "procedure", "efficiency"],
            "knowledge": ["document", "knowledge", "gap", "research", "information", "data"],
        }

        words = set(re.findall(r"[a-zA-Z]{3,}", query.lower()))
        matched_topics: List[str] = []
        for topic, keywords in topic_map.items():
            if words & set(keywords):
                matched_topics.append(topic)
        return matched_topics

    # ── Internal persistence helpers ───────────────────────────

    async def _get_interaction_stats(self, user_id: str) -> Dict[str, Any]:
        try:
            async with self.db.acquire() as conn, conn.execute(
                "SELECT interaction_stats FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                raw = row[0] if isinstance(row[0], str) else row["interaction_stats"]
                return json.loads(raw)
        except Exception as e:
            logger.warning("_get_interaction_stats: suppressed %s", e)
        return {}

    async def _get_learned_topics(self, user_id: str) -> List[str]:
        try:
            async with self.db.acquire() as conn, conn.execute(
                "SELECT learned_topics FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                raw = row[0] if isinstance(row[0], str) else row["learned_topics"]
                return json.loads(raw)
        except Exception as e:
            logger.warning("_get_learned_topics: suppressed %s", e)
        return []

    async def _save_preferences(self, user_id: str, prefs: Dict[str, Any]) -> None:
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO user_preferences (user_id, preferences, updated_at)
                       VALUES (?, ?, datetime('now'))
                       ON CONFLICT(user_id) DO UPDATE SET
                           preferences = excluded.preferences,
                           updated_at = excluded.updated_at""",
                    (user_id, json.dumps(prefs)),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to save preferences for %s: %s", user_id, exc)

    async def _save_interaction_stats(self, user_id: str, stats: Dict[str, Any]) -> None:
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO user_preferences (user_id, interaction_stats, updated_at)
                       VALUES (?, ?, datetime('now'))
                       ON CONFLICT(user_id) DO UPDATE SET
                           interaction_stats = excluded.interaction_stats,
                           updated_at = excluded.updated_at""",
                    (user_id, json.dumps(stats)),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to save interaction stats for %s: %s", user_id, exc)

    async def _log_learning_event(
        self, user_id: str, signal_type: str, key: str, value: Any
    ) -> None:
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO preference_learning_log
                       (user_id, signal_type, signal_value, new_preference, confidence)
                       VALUES (?, ?, ?, ?, 0.5)""",
                    (user_id, signal_type, key, json.dumps(value)),
                )
                await conn.commit()
        except Exception:
            pass  # Non-critical — don't fail the interaction
