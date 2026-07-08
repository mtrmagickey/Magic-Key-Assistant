"""
Steward Persona - Self-monitoring and health checks.

The Steward monitors the bot's own health by:
- Daily health checks (command usage, response quality)
- Weekly self-assessments (engagement trends)
- Learning loop audits (gap resolution, feedback incorporation)

Key capabilities:
- Metrics collection and trending
- Blind spot detection
- Feature usage analysis
- Health snapshot storage
- Improvement recommendations
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .base import EASTERN

logger = logging.getLogger(__name__)


class StewardMixin:
    """
    Steward persona mixin providing self-monitoring capabilities.
    
    This is designed to be mixed into the AutonomousOps cog.
    Requires:
        - self.bot (with db attribute)
        - self.llm_service
        - self.post_to_bots_channel()
        - self._job_already_ran()
        - self._record_job_run()
    """
    
    # ========================================
    # STEWARD: Daily Metrics Collection
    # ========================================
    
    async def _steward_collect_daily_metrics(self) -> Dict[str, Any]:
        """Collect today's health metrics."""
        db = getattr(self.bot, "db", None)
        if not db:
            return {}

        today = datetime.now(EASTERN).strftime("%Y-%m-%d")
        metrics: Dict[str, Any] = {}

        try:
            async with db.acquire() as conn:
                # Questions today
                async with conn.execute(
                    "SELECT COUNT(*) FROM bot_questions WHERE date(created_at) = ?",
                    (today,)
                ) as cursor:
                    row = await cursor.fetchone()
                    metrics['questions_today'] = row[0] if row else 0

                # Unhelpful rate today
                async with conn.execute(
                    """
                    SELECT 
                        COUNT(CASE WHEN response_quality = 'unhelpful' THEN 1 END) as unhelpful,
                        COUNT(*) as total
                    FROM bot_questions 
                    WHERE date(created_at) = ? AND response_quality IS NOT NULL
                    """,
                    (today,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[1] > 0:
                        metrics['unhelpful_rate'] = row[0] / row[1]
                    else:
                        metrics['unhelpful_rate'] = 0

                # Days since last ingest
                async with conn.execute(
                    """
                    SELECT MAX(created_at) FROM learning_loop_events 
                    WHERE event_type = 'memo_ingested'
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        last_ingest = datetime.fromisoformat(row[0])
                        metrics['days_since_ingest'] = (datetime.now() - last_ingest).days
                    else:
                        metrics['days_since_ingest'] = 999

                # Open gaps without progress
                async with conn.execute(
                    """
                    SELECT COUNT(*) FROM knowledge_gaps 
                    WHERE status = 'open' 
                      AND (last_asked IS NULL OR date(last_asked) < date('now', '-7 days'))
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    metrics['open_gaps_without_progress'] = row[0] if row else 0

                # Recurring blind spots
                async with conn.execute(
                    "SELECT COUNT(*) FROM recurring_blind_spots WHERE status = 'open'"
                ) as cursor:
                    row = await cursor.fetchone()
                    metrics['recurring_blind_spots'] = row[0] if row else 0

        except Exception as e:
            logger.warning(f"Failed to collect daily metrics: {e}")

        return metrics
    
    # ========================================
    # STEWARD: Weekly Metrics Collection
    # ========================================

    async def _steward_collect_weekly_metrics(self) -> Dict[str, Any]:
        """Collect this week's metrics."""
        db = getattr(self.bot, "db", None)
        if not db:
            return {}

        now = datetime.now(EASTERN)
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        prev_week_start = (now - timedelta(days=now.weekday() + 7)).strftime("%Y-%m-%d")
        prev_week_end = (now - timedelta(days=now.weekday() + 1)).strftime("%Y-%m-%d")

        metrics: Dict[str, Any] = {}

        try:
            async with db.acquire() as conn:
                # Questions this week
                async with conn.execute(
                    "SELECT COUNT(*) FROM bot_questions WHERE date(created_at) >= ?",
                    (week_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    metrics['questions_asked'] = row[0] if row else 0

                # Helpful/unhelpful
                async with conn.execute(
                    """
                    SELECT response_quality, COUNT(*) 
                    FROM bot_questions 
                    WHERE date(created_at) >= ? AND response_quality IS NOT NULL
                    GROUP BY response_quality
                    """,
                    (week_start,)
                ) as cursor:
                    for row in await cursor.fetchall():
                        if row[0] == 'helpful':
                            metrics['questions_helpful'] = row[1]
                        elif row[0] == 'unhelpful':
                            metrics['questions_unhelpful'] = row[1]

                metrics.setdefault('questions_helpful', 0)
                metrics.setdefault('questions_unhelpful', 0)

                # Commands used
                async with conn.execute(
                    "SELECT COUNT(*) FROM bot_command_usage WHERE date(created_at) >= ?",
                    (week_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    metrics['commands_used'] = row[0] if row else 0

                # Unique users
                async with conn.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM bot_command_usage WHERE date(created_at) >= ?",
                    (week_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    metrics['unique_users'] = row[0] if row else 0

                # Previous week questions for trend
                async with conn.execute(
                    "SELECT COUNT(*) FROM bot_questions WHERE date(created_at) >= ? AND date(created_at) <= ?",
                    (prev_week_start, prev_week_end)
                ) as cursor:
                    row = await cursor.fetchone()
                    metrics['prev_week_questions'] = row[0] if row else 0

        except Exception as e:
            logger.warning(f"Failed to collect weekly metrics: {e}")

        return metrics
    
    # ========================================
    # STEWARD: Learning Loop Assessment
    # ========================================

    async def _steward_assess_learning_loop(self) -> Dict[str, Any]:
        """Assess learning loop health."""
        db = getattr(self.bot, "db", None)
        if not db:
            return {}

        now = datetime.now(EASTERN)
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        health: Dict[str, Any] = {}

        try:
            async with db.acquire() as conn:
                # Gaps opened/closed this week
                async with conn.execute(
                    "SELECT COUNT(*) FROM knowledge_gaps WHERE date(first_asked) >= ?",
                    (week_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    health['gaps_opened'] = row[0] if row else 0

                async with conn.execute(
                    "SELECT COUNT(*) FROM knowledge_gaps WHERE status = 'resolved' AND date(resolved_at) >= ?",
                    (week_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    health['gaps_closed'] = row[0] if row else 0

                # Memos written (from learning_loop_events)
                async with conn.execute(
                    "SELECT COUNT(*) FROM learning_loop_events WHERE event_type = 'memo_drafted' AND date(created_at) >= ?",
                    (week_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    health['memos_written'] = row[0] if row else 0

                # Docs ingested
                async with conn.execute(
                    "SELECT COUNT(*) FROM learning_loop_events WHERE event_type = 'memo_ingested' AND date(created_at) >= ?",
                    (week_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    health['docs_ingested'] = row[0] if row else 0

                # Closure rate (gaps closed / gaps opened, smoothed)
                total_open = health.get('gaps_opened', 0)
                total_closed = health.get('gaps_closed', 0)
                if total_open > 0:
                    health['closure_rate'] = min(total_closed / total_open, 1.0)
                else:
                    health['closure_rate'] = 1.0 if total_closed > 0 else 0.5

        except Exception as e:
            logger.warning(f"Failed to assess learning loop: {e}")

        return health
    
    # ========================================
    # STEWARD: Analysis & Detection
    # ========================================

    async def _steward_find_recurring_blind_spots(self) -> List[Dict[str, Any]]:
        """Find questions that keep coming up without good answers."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT question_pattern, occurrence_count, example_questions
                    FROM recurring_blind_spots
                    WHERE status = 'open'
                    ORDER BY occurrence_count DESC
                    LIMIT 5
                    """
            ) as cursor:
                rows = await cursor.fetchall()
                return [{'pattern': r[0], 'count': r[1], 'examples': r[2]} for r in (rows or [])]
        except Exception as e:
            logger.warning(f"Failed to find blind spots: {e}")
            return []

    async def _steward_analyze_feature_usage(self) -> List[Dict[str, Any]]:
        """Analyze which features/commands are being used."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        now = datetime.now(EASTERN)
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

        key_features = ['ask', 'gaps', 'lead', 'action', 'feedback', 'did', 'report']

        try:
            async with db.acquire() as conn:
                results = []
                for feature in key_features:
                    async with conn.execute(
                        """
                        SELECT COUNT(*) FROM bot_command_usage 
                        WHERE command_name LIKE ? AND date(created_at) >= ?
                        """,
                        (f"%{feature}%", week_start)
                    ) as cursor:
                        row = await cursor.fetchone()
                        results.append({'name': feature, 'uses': row[0] if row else 0})
                return results
        except Exception as e:
            logger.warning(f"Failed to analyze feature usage: {e}")
            return []

    async def _steward_find_stale_gaps(self, days: int = 14) -> List[Dict[str, Any]]:
        """Find knowledge gaps with no progress in X days."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        cutoff = (datetime.now(EASTERN) - timedelta(days=days)).strftime("%Y-%m-%d")

        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, topic, first_asked, last_asked
                    FROM knowledge_gaps
                    WHERE status = 'open'
                      AND (last_asked IS NULL OR date(last_asked) < ?)
                    ORDER BY first_asked ASC
                    LIMIT 10
                    """,
                (cutoff,)
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in (rows or []):
                    updated = r[3] or r[2]
                    days_stale = (datetime.now(EASTERN) - datetime.fromisoformat(updated)).days if updated else days
                    results.append({'id': r[0], 'topic': r[1], 'days_stale': days_stale})
                return results
        except Exception as e:
            logger.warning(f"Failed to find stale gaps: {e}")
            return []

    async def _steward_find_unverified_improvements(self) -> List[Dict[str, Any]]:
        """Find memos that were ingested but not verified as improvements."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT DISTINCT gap_id 
                    FROM learning_loop_events 
                    WHERE event_type = 'memo_ingested'
                      AND gap_id NOT IN (
                          SELECT gap_id FROM learning_loop_events WHERE event_type = 'improvement_verified'
                      )
                    LIMIT 5
                    """
            ) as cursor:
                rows = await cursor.fetchall()
                return [{'gap_id': r[0]} for r in (rows or [])]
        except Exception as e:
            logger.warning(f"Failed to find unverified improvements: {e}")
            return []

    async def _steward_generate_recommendations(
        self, 
        metrics: Dict, 
        blind_spots: List,
        loop_health: Dict, 
        feature_usage: List
    ) -> List[str]:
        """Generate improvement recommendations based on analysis."""
        recs = []

        # Engagement recommendations
        if metrics.get('questions_asked', 0) < 5:
            recs.append("Low question volume — consider prompting partners to ask me things!")
        
        if metrics.get('questions_unhelpful', 0) > metrics.get('questions_helpful', 0):
            recs.append("More unhelpful than helpful responses — schedule a 'feed the bot' session")

        # Learning loop recommendations
        if loop_health.get('closure_rate', 1) < 0.3:
            recs.append("Knowledge gaps aren't closing — Archivist needs to investigate or close stale gaps")

        if loop_health.get('docs_ingested', 0) == 0:
            recs.append("No new docs ingested this week — add meeting notes, decisions, or project updates")

        # Blind spot recommendations
        if blind_spots:
            top_spot = blind_spots[0]['pattern'][:40]
            recs.append(f"Recurring blind spot: \"{top_spot}...\" — create a doc or memo to address this")

        # Feature usage recommendations
        dormant = [f for f in feature_usage if f['uses'] == 0]
        if len(dormant) > 2:
            names = ', '.join(f['name'] for f in dormant[:2])
            recs.append(f"Unused features: {names} — consider training partners or deprecating")

        return recs
    
    # ========================================
    # STEWARD: Logging & Storage
    # ========================================

    async def _steward_save_health_snapshot(self, date: str, metrics: Dict[str, Any]):
        """Save a health snapshot for trend tracking."""
        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT OR REPLACE INTO bot_health_snapshots (
                        snapshot_date, questions_asked, questions_helpful, questions_unhelpful,
                        commands_used, unique_users, feedback_count, recurring_questions_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date,
                        metrics.get('questions_today', 0),
                        0,  # Will be filled by weekly
                        0,
                        metrics.get('commands_used', 0),
                        metrics.get('unique_users', 0),
                        0,
                        metrics.get('recurring_blind_spots', 0)
                    )
                )
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to save health snapshot: {e}")

    async def _steward_log_learning_event(self, gap_id: int, event_type: str, description: str):
        """Log a learning loop event."""
        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO learning_loop_events (gap_id, event_type, description) VALUES (?, ?, ?)",
                    (gap_id, event_type, description)
                )
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to log learning event: {e}")

    async def _steward_log_command_usage(
        self,
        command_name: str,
        cog_name: str,
        user_id: int,
        username: str,
        channel_id: int,
        guild_id: int,
        success: bool = True,
        error_message: str = None,
        execution_time_ms: int = None
    ):
        """Log command usage for engagement tracking."""
        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO bot_command_usage (
                        command_name, cog_name, user_id, username, channel_id, guild_id,
                        success, error_message, execution_time_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (command_name, cog_name, user_id, username, channel_id, guild_id,
                     1 if success else 0, error_message, execution_time_ms)
                )
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to log command usage: {e}")

    async def _steward_log_question(
        self,
        question_text: str,
        user_id: int,
        username: str,
        channel_id: int,
        had_sources: bool = False,
        source_count: int = 0
    ) -> Optional[int]:
        """Log a question asked to the bot."""
        db = getattr(self.bot, "db", None)
        if not db:
            return None

        # Simple hash for deduplication
        question_hash = hashlib.md5(question_text.lower().strip()[:200].encode()).hexdigest()[:16]

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO bot_questions (
                        question_text, question_hash, user_id, username, channel_id,
                        had_sources, source_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (question_text[:500], question_hash, user_id, username, channel_id,
                     1 if had_sources else 0, source_count)
                )
                await conn.commit()

                # Check for recurring pattern
                async with conn.execute(
                    "SELECT COUNT(*) FROM bot_questions WHERE question_hash = ?",
                    (question_hash,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0] >= 3:
                        await self._steward_flag_blind_spot(question_text, question_hash)

                async with conn.execute("SELECT last_insert_rowid()") as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else None
        except Exception as e:
            logger.warning(f"Failed to log question: {e}")
            return None

    async def _steward_flag_blind_spot(self, question_text: str, question_hash: str):
        """Flag a recurring question as a blind spot."""
        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            async with db.acquire() as conn:
                # Check if already exists
                async with conn.execute(
                    "SELECT id, occurrence_count, example_questions FROM recurring_blind_spots WHERE question_pattern = ?",
                    (question_text[:200],)
                ) as cursor:
                    existing = await cursor.fetchone()

                if existing:
                    # Update count
                    await conn.execute(
                        """
                        UPDATE recurring_blind_spots 
                        SET occurrence_count = occurrence_count + 1, 
                            last_asked_at = datetime('now'),
                            updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (existing[0],)
                    )
                else:
                    # Create new
                    await conn.execute(
                        """
                        INSERT INTO recurring_blind_spots (question_pattern, last_asked_at, example_questions)
                        VALUES (?, datetime('now'), ?)
                        """,
                        (question_text[:200], json.dumps([question_text[:300]]))
                    )
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to flag blind spot: {e}")

    async def _steward_update_question_feedback(self, question_id: int, quality: str):
        """Update a question's response quality based on feedback."""
        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE bot_questions SET response_quality = ?, feedback_received = 1 WHERE id = ?",
                    (quality, question_id)
                )
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update question feedback: {e}")
