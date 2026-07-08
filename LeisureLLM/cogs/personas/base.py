"""
Base persona class with shared utilities.

All personas inherit from this and gain access to:
- LLM service
- Database connection
- Posting to bots channel
- Job tracking/idempotency
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import discord

logger = logging.getLogger(__name__)

# Timezone for scheduled tasks
EASTERN = ZoneInfo("America/New_York")
PERSONA_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "personas"


def load_persona_prompt(persona_key: str, org_name: Optional[str] = None) -> str:
    """Load persona prompt overrides from disk."""
    path = PERSONA_PROMPTS_DIR / f"{persona_key}.txt"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if org_name:
        content = content.replace("{org_name}", org_name)
    return content


def _now_utc_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.utcnow().isoformat() + "Z"


def _week_start_monday(now_local: Optional[datetime] = None) -> str:
    """Get the Monday of the current week as ISO date string."""
    now_local = now_local or datetime.now(EASTERN)
    d = now_local.date()
    start = d - timedelta(days=d.weekday())
    return start.isoformat()


class BasePersona:
    """
    Base class providing shared functionality for all personas.
    
    This is designed to be used as a mixin with discord.ext.commands.Cog.
    The actual cog (AutonomousOps) will inherit from multiple persona mixins.
    """
    
    # Each persona should override these
    PERSONA_NAME = "Base"
    PERSONA_EMOJI = "🤖"
    PERSONA_ROLE = "Base persona"
    
    @property
    def db(self):
        """Access to database connection."""
        return getattr(self.bot, 'db', None)
    
    @property
    def llm_service(self):
        """Access to LLM service."""
        services = getattr(self.bot, 'service_container', None)
        return getattr(services, 'llm', None) if services else None
    
    @property
    def tavily_service(self):
        """Access to Tavily web search service."""
        services = getattr(self.bot, 'service_container', None)
        tavily = getattr(services, 'tavily', None) if services else None
        if tavily and getattr(tavily, 'is_configured', False):
            return tavily
        return None
    
    async def post_to_bots_channel(
        self, 
        department: str, 
        message: str, 
        embed: Optional[discord.Embed] = None
    ) -> Optional[discord.Message]:
        """
        Post a message to the bots channel for transparency.
        
        Args:
            department: The persona/department posting (e.g., "scout", "dreamer")
            message: The message content
            embed: Optional embed to include
            
        Returns:
            The sent message, or None if failed
        """
        bots_channel_id = getattr(self, 'bots_channel_id', None)
        if not bots_channel_id:
            logger.warning(f"[{department}] No bots channel configured")
            return None
            
        channel = self.bot.get_channel(bots_channel_id)
        if not channel:
            logger.warning(f"[{department}] Could not find bots channel {bots_channel_id}")
            return None
        
        dept_info = self.departments.get(department, {})
        emoji = dept_info.get("emoji", "🤖")
        name = dept_info.get("name", department.title())
        
        formatted_message = f"{emoji} **{name}**: {message}"
        
        try:
            if embed:
                return await channel.send(formatted_message, embed=embed)
            else:
                return await channel.send(formatted_message)
        except Exception as e:
            logger.error(f"[{department}] Failed to post to bots channel: {e}")
            return None
    
    async def _record_job_run(self, job_name: str, run_date: str) -> bool:
        """
        Record that a job has run for idempotency.
        
        Args:
            job_name: Unique identifier for the job
            run_date: Date string (YYYY-MM-DD) for the run
            
        Returns:
            True if recorded successfully, False otherwise
        """
        if not self.db:
            logger.warning("Cannot record job run - no database")
            return False
            
        try:
            await self.db.connection.execute(
                """
                INSERT OR REPLACE INTO job_runs (job_name, run_date, status, completed_at)
                VALUES (?, ?, 'completed', datetime('now'))
                """,
                (job_name, run_date)
            )
            await self.db.connection.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to record job run {job_name}: {e}")
            return False

    async def _audit_autonomous_tool(
        self,
        conn,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        success: bool,
        message: str = "",
        artifact_refs: Optional[List[str]] = None,
    ) -> None:
        """Write an audit row to tool_executions for autonomous mutations.

        Mirrors the schema used by :class:`core.tool_registry.ToolRegistry` so
        that persona-initiated writes appear in the same audit trail as
        interactive tool calls.
        """
        try:
            await conn.execute(
                """INSERT INTO tool_executions
                   (tool_name, arguments, success, message, artifact_refs,
                    source, confirmed_by_user, executed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tool_name,
                    json.dumps(arguments, default=str),
                    success,
                    message,
                    json.dumps(artifact_refs or []),
                    "autonomous",
                    False,
                    datetime.utcnow().isoformat(),
                ),
            )
            await conn.commit()
        except Exception as exc:
            logger.debug("Autonomous tool audit log failed: %s", exc)
    
    async def _job_already_ran(self, job_name: str, run_date: str) -> bool:
        """
        Check if a job has already run for idempotency.
        
        Args:
            job_name: Unique identifier for the job
            run_date: Date string (YYYY-MM-DD) to check
            
        Returns:
            True if job already ran, False otherwise
        """
        if not self.db:
            return False
            
        try:
            async with self.db.connection.execute(
                """
                SELECT 1 FROM job_runs 
                WHERE job_name = ? AND run_date = ? AND status = 'completed'
                """,
                (job_name, run_date)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None
        except Exception as e:
            logger.error(f"Failed to check job run {job_name}: {e}")
            return False
    
    def _days_since(self, date_str: Optional[str]) -> int:
        """
        Calculate days since a given date string.
        
        Args:
            date_str: ISO date string or None
            
        Returns:
            Number of days since date, or 9999 if date is None/invalid
        """
        if not date_str:
            return 9999
        try:
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EASTERN)
            now = datetime.now(EASTERN)
            return (now - dt).days
        except Exception:
            return 9999
