"""
Autonomous Operations Cog
Multi-department bot system for business intelligence and operations automation

Departments:
- Manager: Partner-facing interface, summarizes other departments
- Chief: Strategic analysis, weekly business health checks  
- Coordinator: Daily operations, async meeting facilitation, reminders
- Scout: Web research, opportunity discovery, industry monitoring

All departments post work-in-progress to #bots channel for transparency
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import discord
import docsprep
from discord import app_commands
from discord.ext import commands, tasks
from langchain_core.prompts import ChatPromptTemplate
from services import ServiceContainer

import config
from cogs.ingest_metadata import run_ingest
from cogs.mixins import AdminOpsMixin, AgendaOpsMixin, PersonaMeetingsMixin

# Persona mixins (Phase 3 architecture refactoring)
from cogs.personas import CuratorMixin, DreamerMixin, RainmakerMixin, ScoutMixin, StewardMixin
from cogs.ui.autonomous_ui import (
    DidSomethingModal,
    FirePersonaView,
    HirePersonaModal,
    PMCreateActionView,
    PMCreateDecisionView,
)
from config import (
    PARTNER_UPDATE_LOOKBACK_DAYS,
    PARTNER_UPDATE_MAX_SURFACE,
    PM_WIP_LIMIT_IN_PROGRESS,
)

# Reuse partner gating if available
try:
    from LeisureLLM.cogs.KnowledgeGapTracker import is_partner
except Exception:  # pragma: no cover

    def is_partner(interaction: discord.Interaction) -> bool:  # type: ignore
        return True

logger = logging.getLogger(__name__)

# Shared utilities — also importable from cogs.mixins._utils
from cogs.mixins._utils import (  # noqa: E402
    _is_owner_interaction,
    _next_thursday_due,
    _now_utc_iso,
    _tags_json,
    _week_start_monday,
)

# Timezone for scheduled tasks
EASTERN = ZoneInfo("America/New_York")


class AutonomousOps(PersonaMeetingsMixin, AdminOpsMixin, AgendaOpsMixin, ScoutMixin, DreamerMixin, RainmakerMixin, StewardMixin, CuratorMixin, commands.Cog):
    """Autonomous business operations with departmental structure.
    
    Inherits persona mixins for modular organization:
    - ScoutMixin: Web research and opportunity discovery
    - DreamerMixin: Ideation and blue-sky exploration
    - RainmakerMixin: Lead management and pipeline
    - StewardMixin: Self-monitoring and health checks
    - CuratorMixin: Agentic corpus expansion and quality
    """
    
    def __init__(self, bot):
        self.bot = bot
        self.bots_channel_id = config.BOTS_CHANNEL_ID  # partners-assistant channel
        # Backoffice channel for persona-to-persona chatter / internal meetings.
        self.bots_office_channel_id = getattr(config, "BOTS_OFFICE_CHANNEL_ID", None)

        # Shared services (LLM, Tavily, etc.)
        self.services: Optional[ServiceContainer] = getattr(bot, "service_container", None)
        self.llm_service = getattr(self.services, "llm", None) if self.services else None
        self.tavily_service = getattr(self.services, "tavily", None) if self.services else None
        if not self.llm_service:
            logger.warning("LLM service is not configured; summaries will fail")
        if not (self.tavily_service and self.tavily_service.is_configured):
            logger.warning("Tavily service not configured - Scout search disabled")
        
        # Job idempotency tracking (database only)
        if not getattr(bot, 'db', None):
            logger.warning("Database not available - Job tracking will fail")
        else:
             logger.info("Using database for job tracking")
        
        # Department "personalities" for different functions
        self.departments = {
            "manager": {
                "name": "Manager",
                "emoji": "📊",
                "role": "Partner-facing interface, coordinates departments"
            },
            "chief": {
                "name": "Chief",
                "emoji": "🎯",
                "role": "Strategic analysis, business health monitoring"
            },
            "coordinator": {
                "name": "Coordinator",
                "emoji": "📋",
                "role": "Daily operations, meeting facilitation, reminders"
            },
            "scout": {
                "name": "Scout",
                "emoji": "🔍",
                "role": "Web research, opportunity discovery"
            },
            "dreamer": {
                "name": "Dreamer",
                "emoji": "💭",
                "role": "Wild ideas, unconventional thinking, blue-sky exploration"
            },
            "rainmaker": {
                "name": "Rainmaker",
                "emoji": "🎯💰",
                "role": "Business development, lead tracking, pipeline management, closing deals"
            },
            "steward": {
                "name": "Steward",
                "emoji": "🪴",
                "role": "Bot self-monitoring, engagement tracking, learning loop health, improvement advocacy"
            },
            "curator": {
                "name": "Curator",
                "emoji": "📚",
                "role": "Corpus quality analysis, auto-synthesis, coverage gap detection, knowledge expansion"
            }
        }
        
        # State tracking
        self.async_meeting_active = False
        self.async_meeting_threads = {}

        # PM routine thread cache (thread_id -> purpose)
        self._pm_thread_purpose: Dict[int, str] = {}
        
        # ── Load workflow config (falls back to sensible defaults) ──
        try:
            from core.config_loader import WorkflowConfig
            _wf = WorkflowConfig.load()
        except Exception:
            _wf = None
            logger.info("workflows.yaml not loaded — using built-in defaults for all modules")

        # ── Top-level: Personas (optional acceleration layer) ────
        _personas_on = getattr(_wf, "personas_enabled", False) if _wf else False
        self._personas_enabled = _personas_on  # expose for runtime command checks

        # ── Registry-driven job start ────────────────────────────
        from core.job_registry import JOB_REGISTRY, is_gate_open

        started_by_module: Dict[str, list] = {}
        for name, meta in JOB_REGISTRY.items():
            if meta.cog != "AutonomousOps":
                continue
            if not is_gate_open(meta.gate, _wf):
                continue
            if meta.requires_accelerators and not _personas_on:
                continue
            getattr(self, name).start()
            started_by_module.setdefault(meta.module, []).append(name)

        for mod, jobs in sorted(started_by_module.items()):
            logger.info("Module [%s] started %d job(s): %s", mod, len(jobs), ", ".join(jobs))

        all_modules = {m.module for m in JOB_REGISTRY.values() if m.cog == "AutonomousOps"}
        for mod in sorted(all_modules - set(started_by_module)):
            logger.info("Module [%s] DISABLED", mod)
    
    def cog_unload(self):
        """Clean shutdown of background tasks — driven by JOB_REGISTRY."""
        from core.job_registry import JOB_REGISTRY

        for name in JOB_REGISTRY:
            task = getattr(self, name, None)
            if task and task.is_running():
                task.cancel()

    def _is_first_tuesday(self, now_local: datetime) -> bool:
        """Return True if now_local is the first Tuesday of its month."""
        try:
            if now_local.weekday() != 1:  # Tuesday
                return False
            first_day = now_local.replace(day=1)
            delta = (1 - first_day.weekday()) % 7
            first_tuesday = first_day + timedelta(days=delta)
            return now_local.date() == first_tuesday.date()
        except Exception:
            return False

    @tasks.loop(time=dt_time(hour=10, minute=0, tzinfo=EASTERN))
    async def monthly_partners_meeting(self):
        """Run on the first Tuesday of each month at 10am ET: post monthly partners meeting agenda."""
        await self.bot.wait_until_ready()

        now_local = datetime.now(EASTERN)
        if not self._is_first_tuesday(now_local):
            return

        run_date = now_local.strftime("%Y-%m-%d")
        if await self._job_already_ran("monthly_partners_meeting", run_date):
            logger.info(f"monthly_partners_meeting already ran for {run_date}, skipping")
            return

        partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if not partners_channel:
            await self.post_to_bots_channel("coordinator", "⚠️ Could not find weekly-meeting-threads channel")
            return

        month_label = now_local.strftime("%B %Y")
        lines: List[str] = []
        lines.append(f"🗓️ **Monthly Partners Meeting — {month_label}**")
        lines.append("Quick agenda snapshot (auto-generated):")
        lines.append("")

        # Action items snapshot (top open items)
        try:
            if getattr(self.bot, "db", None):
                async with self.bot.db.acquire() as conn, conn.execute(
                    """
                        SELECT id, title, status, due_date, priority, assigned_to_user_id
                        FROM tasks
                        WHERE tags LIKE ?
                          AND status IN ('todo','in_progress','blocked')
                        ORDER BY (due_date IS NULL) ASC, due_date ASC, updated_at DESC
                        LIMIT 25
                        """,
                    ("%action_item%",),
                ) as cursor:
                    rows = await cursor.fetchall()

                lines.append("📌 **Action items (top open)**")
                if rows:
                    for r in rows[:12]:
                        _id, title, status, due_date, priority, owner_id = r
                        due_txt = f" (due {due_date})" if due_date else ""
                        owner_txt = f" <@{int(owner_id)}>" if owner_id is not None else ""
                        lines.append(f"- **#{_id}** [{status}] {title}{due_txt}{owner_txt}")
                else:
                    lines.append("- *No open action items found* ")
                lines.append("")
        except Exception as e:
            logger.warning(f"Monthly meeting: failed to load action items: {e}")

        # Recent partner updates (last 30 days, even if already surfaced weekly)
        try:
            if getattr(self.bot, "db", None):
                lookback = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                async with self.bot.db.acquire() as conn, conn.execute(
                    """
                        SELECT partner_user_id, partner_username, category, details, link, created_at
                        FROM partner_updates
                        WHERE datetime(created_at) >= datetime(?)
                        ORDER BY datetime(created_at) DESC
                        LIMIT 10
                        """,
                    (lookback,),
                ) as cursor:
                    updates = [dict(r) for r in (await cursor.fetchall() or [])]

                lines.append("✨ **Recent partner updates (last 30 days)**")
                if updates:
                    for u in updates:
                        uid = u.get("partner_user_id")
                        mention = f"<@{int(uid)}>" if uid else (u.get("partner_username") or "Partner")
                        category = (u.get("category") or "update").strip()
                        details = (u.get("details") or "").strip().replace("\n", " ")
                        link = (u.get("link") or "").strip()
                        link_txt = f" ({link})" if link else ""
                        prefix = f"[{category}] " if category and category != "update" else ""
                        lines.append(f"- {mention}: {prefix}{details[:220]}{link_txt}")
                else:
                    lines.append("- *No partner updates logged yet* ")
                lines.append("")
        except Exception as e:
            logger.warning(f"Monthly meeting: failed to load partner updates: {e}")

        # Monthly scoreboard (last 30 days)
        try:
            if getattr(self.bot, "db", None):
                lookback = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                async with self.bot.db.acquire() as conn:
                    async with conn.execute(
                        """
                        SELECT partner_user_id,
                               COALESCE(partner_username, '') as partner_username,
                               SUM(points) as pts
                        FROM partner_point_events
                        WHERE datetime(created_at) >= datetime(?)
                        GROUP BY partner_user_id, partner_username
                        ORDER BY pts DESC
                        LIMIT 5
                        """,
                        (lookback,),
                    ) as cursor:
                        leaderboard = [dict(r) for r in (await cursor.fetchall() or [])]
                    async with conn.execute(
                        """
                        SELECT COALESCE(SUM(points), 0) as pts
                        FROM partner_point_events
                        WHERE datetime(created_at) >= datetime(?)
                        """,
                        (lookback,),
                    ) as cursor:
                        row = await cursor.fetchone()
                        team_points = int(row[0] or 0) if row else 0

                lines.append("🏆 **Impact scoreboard (last 30 days)**")
                if leaderboard:
                    for i, entry in enumerate(leaderboard, start=1):
                        uid = entry.get('partner_user_id')
                        pts = entry.get('pts') or 0
                        mention = f"<@{int(uid)}>" if uid else (entry.get('partner_username') or 'Partner')
                        lines.append(f"- {i}. {mention}: **{int(pts)}** points")
                    lines.append(f"- Team total: **{int(team_points)}** points")
                else:
                    lines.append("- *No points recorded in the last 30 days* ")
                lines.append("")
        except Exception as e:
            logger.warning(f"Monthly meeting: failed to load scoreboard: {e}")

        lines.append("Use `/action list` for actions | `/did` to log wins | `/gaps list` for open questions")

        allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
        await partners_channel.send("\n".join(lines)[:1950], allowed_mentions=allowed_mentions)
        await self.post_to_bots_channel("coordinator", f"✅ Monthly partners meeting agenda posted ({month_label})")

        await self._record_job_run("monthly_partners_meeting", run_date)
    
    async def post_to_bots_channel(self, department: str, message: str, embed: Optional[discord.Embed] = None):
        """Post department work-in-progress to the backoffice channel.

        This is intended for internal persona chatter (Coordinator/Chief/Scout/Manager/etc).
        Partner-facing posts should continue to go to their dedicated channels.

        Artifact contract: if enforcement is enabled (workflows.yaml / config),
        posts without record-ID references are suppressed to maintain trust.
        """
        # ── Artifact contract check ──────────────────────────────
        try:
            from core.artifact_contract import validate_post
            full_text = message + (f" {embed.description}" if embed and embed.description else "")
            if embed and embed.footer and embed.footer.text:
                full_text += f" {embed.footer.text}"
            if not validate_post(
                full_text,
                enforce=getattr(config, "ARTIFACT_CONTRACT_ENFORCE", False),
                warn_only=getattr(config, "ARTIFACT_CONTRACT_WARN_ONLY", True),
                context=f"{department}",
            ):
                return  # suppressed
        except Exception as e:
            logger.debug(f"Artifact contract check skipped: {e}")

        target_channel_id = int(self.bots_office_channel_id) if self.bots_office_channel_id else None
        if not target_channel_id:
            # Conservative fallback: if office channel isn't configured, use the legacy bots channel.
            target_channel_id = int(self.bots_channel_id) if self.bots_channel_id else None

        if not target_channel_id:
            logger.warning("Bots office channel not configured")
            return

        channel = self.bot.get_channel(target_channel_id)
        if not channel:
            logger.error(f"Could not find bots office channel: {target_channel_id}")
            return

        dept_info = self.departments.get(department, {})
        # Bot presents as one entity — emoji provides functional differentiation
        # without exposing internal persona names to users.
        prefix = f"{dept_info.get('emoji', '🤖')} "

        allowed_mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
        try:
            if embed:
                await channel.send(content=prefix + message, embed=embed, allowed_mentions=allowed_mentions)
            else:
                await channel.send(prefix + message, allowed_mentions=allowed_mentions)
        except Exception as e:
            logger.error(f"Failed to post to bots office channel: {e}")
    
    async def _record_job_run(self, job_name: str, run_date: str):
        """Mark job as completed for this date via Database only."""
        if not getattr(self.bot, 'db', None):
            return
            
        try:
            await self.bot.db.complete_job_run(job_name, run_date)
            logger.info(f"Recorded job run: {job_name} on {run_date}")
        except Exception as e:
            logger.error(f"Failed to record job run: {e}")
    
    async def _job_already_ran(self, job_name: str, run_date: str) -> bool:
        """Check if job completed today via Database only."""
        if not getattr(self.bot, 'db', None):
             return False # If no DB, assume not ran so we at least try (or maybe safe fail?)
             
        try:
            # record_job_run returns True if it's a NEW run (and inserts 'pending'), False if already exists
            # Wait, `record_job_run` naming is confusing in db.py normally. 
            # Usually it returns TRUE if we can proceed?
            # The original code: `return not await self.bot.db.record_job_run(job_name, run_date)`
            # This implies `record_job_run` returns True if "I successfully recorded it just now (so go ahead)".
            # So `not True` = False (not already ran).
            # If `record_job_run` returns False (failed to record because exists), then `not False` = True (already ran).
            
            can_run = await self.bot.db.record_job_run(job_name, run_date)
            return not can_run
        except Exception as e:
            logger.error(f"Failed to check job run: {e}")
            return False

    # ========================================
    # PM AUTOMATION: Proposals, routines, watchdog, dashboard
    # ========================================

    def _pm_channel_ok(self, channel: discord.abc.Messageable) -> bool:
        try:
            if isinstance(channel, discord.Thread):
                parent = channel.parent
                return bool(parent and getattr(parent, "name", "") == config.PM_CHANNEL_NAME)
            return getattr(channel, "name", "") == config.PM_CHANNEL_NAME
        except Exception:
            return False

    async def _pm_get_thread_purpose(self, thread_id: int) -> Optional[str]:
        tid = int(thread_id)
        if tid in self._pm_thread_purpose:
            return self._pm_thread_purpose.get(tid)
        if not getattr(self.bot, "db", None):
            return None
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """SELECT purpose FROM pm_threads WHERE thread_id = ? LIMIT 1""",
                (tid,),
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0]:
                purpose = str(row[0])
                self._pm_thread_purpose[tid] = purpose
                return purpose
        except Exception:
            return None
        return None

    async def _pm_record_thread(self, *, thread: discord.Thread, purpose: str, run_date: str):
        if not getattr(self.bot, "db", None):
            return
        try:
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT OR REPLACE INTO pm_threads (thread_id, guild_id, channel_id, purpose, run_date)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(thread.id),
                        int(thread.guild.id) if thread.guild else None,
                        int(thread.parent_id) if thread.parent_id else None,
                        str(purpose),
                        str(run_date),
                    ),
                )
                await conn.commit()
            self._pm_thread_purpose[int(thread.id)] = str(purpose)
        except Exception as e:
            logger.warning(f"Failed recording pm thread: {e}")

    async def _pm_owner_wip(self, owner_user_id: int) -> int:
        if not getattr(self.bot, "db", None):
            return 0
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT COUNT(*)
                    FROM tasks
                    WHERE tags LIKE ?
                      AND status = 'in_progress'
                      AND assigned_to_user_id = ?
                    """,
                ("%action_item%", int(owner_user_id)),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    async def _pm_create_action_item(
        self,
        *,
        title: str,
        owner_user_id: int,
        owner_username: str,
        created_by_user_id: int,
        created_by_username: str,
        due_date: str,
        source_message_id: int,
        extra_tags: Optional[List[str]] = None,
    ) -> Tuple[int, bool]:
        if not getattr(self.bot, "db", None):
            raise RuntimeError("Database unavailable")

        wip = await self._pm_owner_wip(int(owner_user_id))
        wip_blocked = wip >= int(PM_WIP_LIMIT_IN_PROGRESS)

        tags = ["action_item"]
        if extra_tags:
            for t in extra_tags:
                tt = (t or "").strip()
                if tt and tt not in tags:
                    tags.append(tt)

        notes = f"Auto-captured from Discord message {int(source_message_id)}"
        if wip_blocked:
            notes += f"\nWIP limit reached at capture time ({wip}/{PM_WIP_LIMIT_IN_PROGRESS})."

        async with self.bot.db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tasks (
                    title,
                    description,
                    status,
                    priority,
                    assigned_to_user_id,
                    assigned_to_username,
                    created_by_user_id,
                    created_by_username,
                    due_date,
                    created_at,
                    updated_at,
                    tags,
                    notes
                ) VALUES (?, ?, 'todo', 'medium', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(title).strip()[:180],
                    None,
                    int(owner_user_id),
                    str(owner_username)[:120] if owner_username else None,
                    int(created_by_user_id),
                    str(created_by_username)[:120] if created_by_username else None,
                    str(due_date)[:10],
                    _now_utc_iso(),
                    _now_utc_iso(),
                    _tags_json(tags),
                    notes[:900],
                ),
            )
            await conn.commit()
            async with conn.execute("SELECT last_insert_rowid()") as cursor:
                row = await cursor.fetchone()
                action_id = int(row[0]) if row else 0
        
        # Post via ActionItems to ensure emoji UI
        try:
            items_cog = self.bot.get_cog("ActionItems")
            if items_cog and hasattr(items_cog, "post_new_action"):
                # If we have an owner_user_id, we should sync it to task_owners first
                # But we can't easily import _add_task_owner here.
                # However, ActionItems.post_new_action reads from DB. 
                # If we want the owner to show up, we need to insert into task_owners.
                assigned_id = int(owner_user_id)
                if assigned_id:
                     # Direct insert into task_owners to bridge the gap
                    await self.bot.db.execute(
                        "INSERT OR IGNORE INTO task_owners (task_id, user_id) VALUES (?, ?)",
                        (action_id, assigned_id)
                        )
                await items_cog.post_new_action(action_id)
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        return action_id, wip_blocked

    async def _pm_create_decision(
        self,
        *,
        title: str,
        decision_text: str,
        decided_by_user_id: int,
        decided_by_username: str,
        source_message_id: int,
    ) -> int:
        if not getattr(self.bot, "db", None):
            raise RuntimeError("Database unavailable")

        async with self.bot.db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO decisions (title, decision, decided_by, tags)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(title).strip()[:180],
                    str(decision_text).strip()[:1800],
                    json.dumps([int(decided_by_user_id)]),
                    _tags_json(["auto_recorded", "discord"]),
                ),
            )
            await conn.commit()
            async with conn.execute("SELECT last_insert_rowid()") as cursor:
                row = await cursor.fetchone()
                decision_id = int(row[0]) if row else 0

            # Dedupe marker
            try:
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO pm_proposals (guild_id, channel_id, source_message_id, author_user_id, author_username, proposal_type, proposed_title)
                    VALUES (?, ?, ?, ?, ?, 'decision', ?)
                    """,
                    (
                        int(getattr(self.bot, "guild_id", 0) or 0) or None,
                        None,
                        int(source_message_id),
                        int(decided_by_user_id),
                        str(decided_by_username)[:120] if decided_by_username else None,
                        str(title).strip()[:180],
                    ),
                )
                await conn.commit()
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        return decision_id

    async def _pm_record_proposal_once(
        self,
        *,
        guild_id: Optional[int],
        channel_id: int,
        source_message_id: int,
        author_user_id: int,
        author_username: str,
        proposal_type: str,
        proposed_title: str,
        proposed_body: Optional[str] = None,
    ) -> bool:
        if not getattr(self.bot, "db", None):
            return False
        try:
            async with self.bot.db.acquire() as conn:
                async with conn.execute(
                    """SELECT 1 FROM pm_proposals WHERE source_message_id = ? LIMIT 1""",
                    (int(source_message_id),),
                ) as cursor:
                    exists = await cursor.fetchone()
                if exists:
                    return False
                await conn.execute(
                    """
                    INSERT INTO pm_proposals (guild_id, channel_id, source_message_id, author_user_id, author_username, proposal_type, proposed_title, proposed_body)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(guild_id) if guild_id else None,
                        int(channel_id),
                        int(source_message_id),
                        int(author_user_id),
                        str(author_username)[:120] if author_username else None,
                        str(proposal_type),
                        str(proposed_title)[:180] if proposed_title else None,
                        str(proposed_body)[:900] if proposed_body else None,
                    ),
                )
                await conn.commit()
            return True
        except Exception:
            return False

    async def _pm_track_open_question(self, message: discord.Message):
        if not getattr(self.bot, "db", None):
            return
        content = (message.content or "").strip()
        if not content or "?" not in content:
            return
        if len(content) > 400:
            return
        if content.count("?") > 3:
            return
        try:
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO open_questions (
                        guild_id, channel_id, message_id, author_user_id, author_username, question_text
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(message.guild.id) if message.guild else None,
                        int(message.channel.id),
                        int(message.id),
                        int(message.author.id),
                        str(message.author)[:120],
                        content[:400],
                    ),
                )
                await conn.commit()
        except Exception:
            return

    async def _pm_resolve_open_question_if_reply(self, message: discord.Message):
        if not getattr(self.bot, "db", None):
            return
        if not message.reference or not message.reference.message_id:
            return
        ref_id = int(message.reference.message_id)
        try:
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE open_questions
                    SET resolved_at = ?
                    WHERE message_id = ? AND resolved_at IS NULL
                    """,
                    (_now_utc_iso(), ref_id),
                )
                await conn.commit()
        except Exception:
            return

    def _pm_extract_action_title(self, content: str) -> Optional[str]:
        s = (content or "").strip()
        if not s:
            return None
        lowered = s.lower()
        triggers = (
            "i will ",
            "i'll ",
            "we should ",
            "let's ",
            "todo:",
            "action:",
            "can you ",
        )
        if not any(t in lowered[:40] for t in triggers):
            return None
        # Title = first line, strip common prefixes
        first = s.splitlines()[0].strip()
        first = re.sub(r"^(decision:|todo:|action:|i will|i'll|we should|let's|can you)\s+", "", first, flags=re.I).strip()
        if not first:
            return None
        return first[:180]

    def _pm_extract_decision(self, content: str) -> Optional[Tuple[str, str]]:
        s = (content or "").strip()
        if not s:
            return None
        lowered = s.lower()
        if lowered.startswith("decision:"):
            body = s[len("decision:") :].strip()
            title = body.splitlines()[0].strip()[:180] if body else "Decision"
            return (title or "Decision"), (body or s)
        if "we decided" in lowered or lowered.startswith("final:"):
            title = s.splitlines()[0].strip()[:180]
            return (title or "Decision"), s
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message or not message.guild:
            return
        if message.author.bot:
            return
            
        # Route to schemes-n-dreams listener (handles its own channel check)
        await self.on_message_dreamer_schemes(message)
        
        if not self._pm_channel_ok(message.channel):
            return

        # Resolve open questions when a reply arrives, then track new questions
        await self._pm_resolve_open_question_if_reply(message)
        await self._pm_track_open_question(message)

        # If this is a PM routine thread, parse replies into actions automatically
        if isinstance(message.channel, discord.Thread):
            purpose = await self._pm_get_thread_purpose(int(message.channel.id))
            if purpose:
                await self._pm_handle_pm_thread_reply(message, purpose)
                return

        # Otherwise: propose action/decision capture (one proposal per message)
        title = self._pm_extract_action_title(message.content or "")
        if title:
            due = _next_thursday_due(datetime.now(EASTERN))
            owner = message.mentions[0] if message.mentions else message.author
            recorded = await self._pm_record_proposal_once(
                guild_id=int(message.guild.id),
                channel_id=int(message.channel.id),
                source_message_id=int(message.id),
                author_user_id=int(message.author.id),
                author_username=str(message.author),
                proposal_type="action_item",
                proposed_title=title,
            )
            if recorded:
                view = PMCreateActionView(
                    cog=self,
                    source_message_id=int(message.id),
                    title=title,
                    owner_user_id=int(owner.id),
                    owner_username=str(owner),
                    due_date=due,
                )
                allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
                await message.reply(
                    f"I can create an action item: **{title}**\nOwner: <@{int(owner.id)}> | Due: **{due}**",
                    view=view,
                    allowed_mentions=allowed_mentions,
                )
            return

        dec = self._pm_extract_decision(message.content or "")
        if dec:
            dec_title, dec_text = dec
            recorded = await self._pm_record_proposal_once(
                guild_id=int(message.guild.id),
                channel_id=int(message.channel.id),
                source_message_id=int(message.id),
                author_user_id=int(message.author.id),
                author_username=str(message.author),
                proposal_type="decision",
                proposed_title=dec_title,
            )
            if recorded:
                view = PMCreateDecisionView(cog=self, title=dec_title, decision_text=dec_text, source_message_id=int(message.id))
                await message.reply(f"I can record this as a decision: **{dec_title}**", view=view)

    async def _pm_handle_pm_thread_reply(self, message: discord.Message, purpose: str):
        if not getattr(self.bot, "db", None):
            return
        content = (message.content or "").strip()
        if not content:
            return

        now_local = datetime.now(EASTERN)
        due = _next_thursday_due(now_local)

        if purpose == "weekly_planning":
            # Create 1-3 outcome tasks
            lines = [ln.strip("-• \t").strip() for ln in content.splitlines()]
            outcomes = [ln for ln in lines if ln][:3]
            if not outcomes:
                return
            created = 0
            for outcome in outcomes:
                try:
                    await self._pm_create_action_item(
                        title=outcome,
                        owner_user_id=int(message.author.id),
                        owner_username=str(message.author),
                        created_by_user_id=int(message.author.id),
                        created_by_username=str(message.author),
                        due_date=due,
                        source_message_id=int(message.id),
                        extra_tags=["weekly_outcome"],
                    )
                    created += 1
                except Exception:
                    continue
            if created:
                try:
                    await message.add_reaction("✅")
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                await message.reply(f"Captured **{created}** weekly outcome(s) as action items.")
            return

        if purpose == "midweek_risk":
            lines = [ln.strip("-• \t").strip() for ln in content.splitlines()]
            blockers = [ln for ln in lines if ln][:3]
            if not blockers:
                return
            created = 0
            for b in blockers:
                title = b
                if not title.lower().startswith("unblock"):
                    title = f"Unblock: {title}"
                try:
                    action_id, _ = await self._pm_create_action_item(
                        title=title,
                        owner_user_id=int(message.author.id),
                        owner_username=str(message.author),
                        created_by_user_id=int(message.author.id),
                        created_by_username=str(message.author),
                        due_date=due,
                        source_message_id=int(message.id),
                        extra_tags=["risk", "blocked"],
                    )
                    # Best-effort: set status to blocked
                    async with self.bot.db.acquire() as conn:
                        try:
                            await conn.execute("UPDATE tasks SET status = 'blocked', blocked_since = ?, updated_at = ? WHERE id = ?", (_now_utc_iso(), _now_utc_iso(), int(action_id)))
                        except Exception:
                            await conn.execute("UPDATE tasks SET status = 'blocked', updated_at = ? WHERE id = ?", (_now_utc_iso(), int(action_id)))
                        await conn.commit()
                    created += 1
                except Exception:
                    continue
            if created:
                try:
                    await message.add_reaction("⛔")
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                await message.reply(f"Logged **{created}** blocker(s) as blocked action items.")
            return

        if purpose == "friday_closeout":
            # Update statuses based on references like "done #12" or "slipped #12 2026-01-09"
            ids = [int(m.group(1)) for m in re.finditer(r"#(\d+)", content)]
            if not ids:
                return
            done_ids: List[int] = []
            slipped_ids: List[int] = []
            lowered = content.lower()
            if "done" in lowered or "shipped" in lowered:
                done_ids = ids
            if "slipped" in lowered:
                slipped_ids = ids

            updated_done = 0
            updated_slip = 0
            async with self.bot.db.acquire() as conn:
                for i in done_ids:
                    try:
                        await conn.execute(
                            "UPDATE tasks SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ?",
                            (_now_utc_iso(), _now_utc_iso(), int(i)),
                        )
                        updated_done += 1
                    except Exception:
                        continue

                # If slipped, set due date forward 7 days unless a date is present
                mdate = re.search(r"(20\d{2}-\d{2}-\d{2})", content)
                new_due = mdate.group(1) if mdate else (datetime.now(EASTERN).date() + timedelta(days=7)).isoformat()
                for i in slipped_ids:
                    try:
                        await conn.execute(
                            "UPDATE tasks SET due_date = ?, updated_at = ?, notes = COALESCE(notes, '') || ? WHERE id = ?",
                            (str(new_due), _now_utc_iso(), f"\n[{datetime.utcnow().strftime('%Y-%m-%d')}] Slipped to {new_due} (Friday closeout)", int(i)),
                        )
                        updated_slip += 1
                    except Exception:
                        continue
                await conn.commit()

            await message.reply(f"✅ Updated: **{updated_done}** done, **{updated_slip}** slipped.")

    @tasks.loop(time=dt_time(hour=10, minute=0, tzinfo=EASTERN))
    async def monday_planning_kickoff(self):
        await self.bot.wait_until_ready()
        now_local = datetime.now(EASTERN)
        if now_local.weekday() != 0:
            return
        run_date = now_local.date().isoformat()
        if await self._job_already_ran("pm_monday_planning", run_date):
            return

        channel = discord.utils.get(self.bot.get_all_channels(), name=config.PM_CHANNEL_NAME)
        if not channel:
            await self.post_to_bots_channel("coordinator", f"⚠️ PM kickoff: missing channel {config.PM_CHANNEL_NAME}")
            return

        week_start = _week_start_monday(now_local)
        msg = await channel.send(
            f"📅 **Weekly Planning (Week of {week_start})**\nPick your **1–3 outcomes** for the week. Reply in the thread with **one outcome per line** — I’ll convert them into action items tagged `weekly_outcome`."
        )
        thread = await msg.create_thread(name=f"Weekly outcomes — {week_start}")
        await self._pm_record_thread(thread=thread, purpose="weekly_planning", run_date=run_date)
        await self._record_job_run("pm_monday_planning", run_date)

    @tasks.loop(time=dt_time(hour=16, minute=0, tzinfo=EASTERN))
    async def friday_closeout(self):
        await self.bot.wait_until_ready()
        now_local = datetime.now(EASTERN)
        if now_local.weekday() != 4:
            return
        run_date = now_local.date().isoformat()
        if await self._job_already_ran("pm_friday_closeout", run_date):
            return

        channel = discord.utils.get(self.bot.get_all_channels(), name=config.PM_CHANNEL_NAME)
        if not channel:
            await self.post_to_bots_channel("coordinator", f"⚠️ Closeout: missing channel {config.PM_CHANNEL_NAME}")
            return

        msg = await channel.send(
            "✅ **Friday Closeout**\nReply in the thread with:\n- `done #123` (or `shipped #123`)\n- `slipped #123 2026-01-09` (date optional; defaults +7d)\nI’ll update task statuses/due dates."
        )
        thread = await msg.create_thread(name=f"Friday closeout — {run_date}")
        await self._pm_record_thread(thread=thread, purpose="friday_closeout", run_date=run_date)
        await self._record_job_run("pm_friday_closeout", run_date)

    @tasks.loop(time=dt_time(hour=11, minute=0, tzinfo=EASTERN))
    async def question_watchdog(self):
        """Unanswered question watchdog: pings stale unanswered questions after 36h."""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return

        run_date = datetime.now(EASTERN).date().isoformat()
        if await self._job_already_ran("pm_question_watchdog", run_date):
            return

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, guild_id, channel_id, message_id, author_user_id, question_text, asked_at,
                           assigned_to_user_id, ping_count, last_pinged_at
                    FROM open_questions
                    WHERE resolved_at IS NULL
                      AND datetime(asked_at) <= datetime(?)
                      AND (last_pinged_at IS NULL OR datetime(last_pinged_at) <= datetime(?))
                      AND COALESCE(ping_count, 0) < 3
                    ORDER BY datetime(asked_at) ASC
                    LIMIT 5
                    """,
                (cutoff, (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()),
            ) as cursor:
                questions = [dict(r) for r in (await cursor.fetchall() or [])]

            if not questions:
                await self._record_job_run("pm_question_watchdog", run_date)
                return

            for q in questions:
                channel = self.bot.get_channel(int(q["channel_id"]))
                if not channel:
                    continue
                gid = q.get("guild_id")
                msg_link = None
                try:
                    if gid:
                        msg_link = f"https://discord.com/channels/{int(gid)}/{int(q['channel_id'])}/{int(q['message_id'])}"
                except Exception:
                    msg_link = None

                mention = None
                if q.get("assigned_to_user_id"):
                    mention = f"<@{int(q['assigned_to_user_id'])}>"
                else:
                    # Fallback mention: CTO role else Partners role
                    role_mention = None
                    if hasattr(channel, "guild") and channel.guild:
                        cto = discord.utils.get(channel.guild.roles, name="CTO")
                        partners = discord.utils.get(channel.guild.roles, name="Partners")
                        role_mention = (cto.mention if cto else None) or (partners.mention if partners else None)
                    mention = role_mention or "@here"

                text = f"❓ Unanswered question (36h+): {mention}\n{q.get('question_text') or ''}"
                if msg_link:
                    text += f"\nLink: {msg_link}"

                allowed_mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
                await channel.send(text[:1900], allowed_mentions=allowed_mentions)

                # Update ping tracking
                try:
                    async with self.bot.db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE open_questions
                            SET last_pinged_at = ?, ping_count = COALESCE(ping_count, 0) + 1
                            WHERE id = ?
                            """,
                            (_now_utc_iso(), int(q["id"])),
                        )
                        await conn.commit()
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

            await self._record_job_run("pm_question_watchdog", run_date)
        except Exception as e:
            logger.warning(f"question_watchdog failed: {e}")
            try:
                await self.bot.db.complete_job_run("pm_question_watchdog", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    @tasks.loop(time=dt_time(hour=9, minute=0, tzinfo=EASTERN))
    async def weekly_dashboard_update(self):
        """Maintain a pinned weekly dashboard message in the partners channel."""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return

        run_date = datetime.now(EASTERN).date().isoformat()
        if await self._job_already_ran("pm_weekly_dashboard_update", run_date):
            return

        channel = discord.utils.get(self.bot.get_all_channels(), name=config.PM_CHANNEL_NAME)
        if not channel:
            return

        week_start = _week_start_monday(datetime.now(EASTERN))
        message_id: Optional[int] = None

        # Load or create dashboard message
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """SELECT message_id FROM pm_dashboard_state WHERE week_start_date = ? LIMIT 1""",
                (str(week_start),),
            ) as cursor:
                row = await cursor.fetchone()
                message_id = int(row[0]) if row and row[0] else None
        except Exception:
            message_id = None

        dashboard_text = await self._pm_build_dashboard_text(week_start=str(week_start))

        msg_obj: Optional[discord.Message] = None
        if message_id:
            try:
                msg_obj = await channel.fetch_message(int(message_id))
            except Exception:
                msg_obj = None

        if not msg_obj:
            msg_obj = await channel.send(dashboard_text[:1950])
            try:
                await msg_obj.pin(reason=f"Weekly dashboard {week_start}")
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
            try:
                async with self.bot.db.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT OR REPLACE INTO pm_dashboard_state (week_start_date, guild_id, channel_id, message_id, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(week_start),
                            int(channel.guild.id) if channel.guild else None,
                            int(channel.id),
                            int(msg_obj.id),
                            _now_utc_iso(),
                        ),
                    )
                    await conn.commit()
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        else:
            try:
                await msg_obj.edit(content=dashboard_text[:1950])
                await self.bot.db.execute(
                    """UPDATE pm_dashboard_state SET updated_at = ? WHERE week_start_date = ?""",
                    (_now_utc_iso(), str(week_start)),
                    )
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        await self._record_job_run("pm_weekly_dashboard_update", run_date)

    async def _pm_build_dashboard_text(self, *, week_start: str) -> str:
        """Build the weekly dashboard snapshot text."""
        lines: List[str] = []
        lines.append(f"📌 **Weekly Dashboard — Week of {week_start}**")
        lines.append("")

        # Top risks (blocked)
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, title, assigned_to_user_id
                    FROM tasks
                    WHERE tags LIKE ?
                      AND status = 'blocked'
                    ORDER BY updated_at ASC
                    LIMIT 6
                    """,
                ("%action_item%",),
            ) as cursor:
                blocked = await cursor.fetchall()
            lines.append("⛔ **Top risks (blocked)**")
            if blocked:
                for r in blocked:
                    _id, title, owner_id = r
                    owner_txt = f" <@{int(owner_id)}>" if owner_id is not None else ""
                    lines.append(f"- **#{_id}** {title}{owner_txt}")
            else:
                lines.append("- *None* ")
            lines.append("")
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # This week's commitments (weekly_outcome)
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, title, status, assigned_to_user_id, due_date
                    FROM tasks
                    WHERE tags LIKE ?
                      AND tags LIKE ?
                      AND status IN ('todo','in_progress','blocked')
                    ORDER BY (due_date IS NULL) ASC, due_date ASC, updated_at DESC
                    LIMIT 10
                    """,
                ("%action_item%", "%weekly_outcome%"),
            ) as cursor:
                outcomes = await cursor.fetchall()
            lines.append("🎯 **This week’s commitments**")
            if outcomes:
                for r in outcomes:
                    _id, title, status, owner_id, due_date = r
                    owner_txt = f" <@{int(owner_id)}>" if owner_id is not None else ""
                    due_txt = f" (due {due_date})" if due_date else ""
                    lines.append(f"- **#{_id}** [{status}] {title}{due_txt}{owner_txt}")
            else:
                lines.append("- *None captured yet — reply in the Weekly outcomes thread on Monday* ")
            lines.append("")
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # Overdue hotlist
        try:
            today = datetime.now(EASTERN).date().isoformat()
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, title, due_date, assigned_to_user_id
                    FROM tasks
                    WHERE tags LIKE ?
                      AND status IN ('todo','in_progress','blocked')
                      AND due_date IS NOT NULL
                      AND due_date < ?
                    ORDER BY due_date ASC
                    LIMIT 6
                    """,
                ("%action_item%", str(today)),
            ) as cursor:
                overdue = await cursor.fetchall()
            lines.append("⏰ **Overdue hotlist**")
            if overdue:
                for r in overdue:
                    _id, title, due_date, owner_id = r
                    owner_txt = f" <@{int(owner_id)}>" if owner_id is not None else ""
                    lines.append(f"- **#{_id}** {title} (due {due_date}){owner_txt}")
            else:
                lines.append("- *None* ")
            lines.append("")
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # Knowledge gaps trending
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, topic, times_asked
                    FROM knowledge_gaps
                    WHERE status = 'open'
                    ORDER BY times_asked DESC, created_at DESC
                    LIMIT 5
                    """
            ) as cursor:
                gaps = await cursor.fetchall()
            lines.append("🧠 **Knowledge gaps trending**")
            if gaps:
                for r in gaps:
                    _id, topic, times_asked = r
                    lines.append(f"- **#{_id}** {topic} (x{int(times_asked or 0)})")
            else:
                lines.append("- *None* ")
            lines.append("")
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # Points leaderboard (week to date)
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT partner_user_id, COALESCE(partner_username, '') as partner_username, SUM(points) as pts
                    FROM partner_point_events
                    WHERE date(created_at) >= date(?)
                    GROUP BY partner_user_id, partner_username
                    ORDER BY pts DESC
                    LIMIT 5
                    """,
                (str(week_start),),
            ) as cursor:
                leaderboard = [dict(r) for r in (await cursor.fetchall() or [])]
            lines.append("🏆 **Points leaderboard (week-to-date)**")
            if leaderboard:
                for i, entry in enumerate(leaderboard, start=1):
                    uid = entry.get("partner_user_id")
                    pts = int(entry.get("pts") or 0)
                    mention = f"<@{int(uid)}>" if uid else (entry.get("partner_username") or "Partner")
                    lines.append(f"- {i}. {mention}: **{pts}**")
            else:
                lines.append("- *No points yet this week* ")
            lines.append("")
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        lines.append("Commands: `/action list` | `/did` | `/gaps list`")
        return "\n".join(lines)
    
    # ========================================
    # COORDINATOR: Daily Digest
    # ========================================
    
    @tasks.loop(time=dt_time(hour=8, minute=0, tzinfo=EASTERN))
    async def daily_digest(self):
        """Run daily at 8am Eastern: scan last 24h activity, post summary"""
        await self.bot.wait_until_ready()
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        
        # Idempotency check
        today = datetime.now(EASTERN).strftime("%Y-%m-%d")
        if await self._job_already_ran("daily_digest", today):
            logger.info(f"daily_digest already ran for {today}, skipping")
            return
        
        await self.post_to_bots_channel("coordinator", "Starting daily digest compilation...")
        
        try:
            # Scan partners channel for last 24h messages
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            if not partners_channel:
                await self.post_to_bots_channel("coordinator", "⚠️ Could not find weekly-meeting-threads channel")
                return
            
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            messages = []
            
            async for message in partners_channel.history(after=yesterday, limit=500):
                if not message.author.bot:
                    messages.append({
                        "author": message.author.name,
                        "content": message.content,
                        "timestamp": message.created_at.isoformat(),
                        "jump_url": message.jump_url
                    })
            
            if not messages:
                await self.post_to_bots_channel("coordinator", "No activity in last 24h, skipping digest")
                return
            
            # Generate digest using LLM
            summary = await self._generate_daily_summary(messages)
            
            # Post to partners channel
            embed = discord.Embed(
                title="📰 Daily Digest",
                description=summary,
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"{org['org_name']} • Daily Operations")
            
            await partners_channel.send(embed=embed)
            await self.post_to_bots_channel("coordinator", f"✅ Daily digest posted ({len(messages)} messages analyzed)")
            
            # Record successful completion
            await self._record_job_run("daily_digest", today)
            
        except Exception as e:
            logger.error(f"Daily digest failed: {e}")
            await self.post_to_bots_channel("coordinator", f"❌ Daily digest failed: {str(e)}")
    
    async def _generate_daily_summary(self, messages: List[Dict]) -> str:
        """Use LLM to summarize daily activity"""
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        prompt = ChatPromptTemplate.from_template(f"""
You are the Coordinator department of {org['org_name']}'s operations bot.
Summarize the last 24 hours of partner communication into a concise daily digest.

Focus on:
- Key decisions or action items
- Project updates and status changes
- Questions that need answers
- Upcoming deadlines or events
- Financial or client matters

Format as bullet points. Be direct and actionable. Max 200 words.

Messages from last 24h:
{{messages}}

Daily Digest:""")
        
        messages_text = "\n\n".join([
            f"[{m['timestamp']}] {m['author']}: {m['content'][:200]}"
            for m in messages[-30:]  # Last 30 messages max
        ])
        
        if not self.llm_service:
            raise RuntimeError("LLM service is not configured")
        return await self.llm_service.generate(prompt, {"messages": messages_text})
    
    # ========================================
    # COORDINATOR: Thursday Async Meeting
    # ========================================
    
    @tasks.loop(time=dt_time(hour=10, minute=0, tzinfo=EASTERN))
    async def thursday_async_meeting(self):
        """Run every Thursday at 10am Eastern: facilitate async meeting"""
        await self.bot.wait_until_ready()
        
        now = datetime.now(EASTERN)
        
        # Only run on Thursdays
        if now.weekday() != 3:
            return
        
        # Idempotency check
        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("thursday_async_meeting", today):
            logger.info(f"thursday_async_meeting already ran for {today}, skipping")
            return
        
        await self.post_to_bots_channel("coordinator", "🗓️ Initiating Thursday Async Meeting...")
        
        try:
            await self._run_async_meeting()
            await self._record_job_run("thursday_async_meeting", today)
        except Exception as e:
            logger.error(f"Async meeting failed: {e}")
            await self.post_to_bots_channel("coordinator", f"❌ Async meeting failed: {str(e)}")
    
    async def _run_async_meeting(self):
        """Execute async meeting workflow"""
        partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if not partners_channel:
            await self.post_to_bots_channel("coordinator", "⚠️ Could not find weekly-meeting-threads channel")
            return
        bot_member = partners_channel.guild.me
        if not bot_member:
            raise RuntimeError("Bot member not found in guild; cannot verify permissions")
        channel_perms = partners_channel.permissions_for(bot_member)
        required_perms = {
            "send_messages": "Send Messages",
            "create_public_threads": "Create Public Threads",
            "send_messages_in_threads": "Send Messages in Threads",
            "manage_threads": "Manage Threads",
        }
        missing = [label for attr, label in required_perms.items() if not getattr(channel_perms, attr, False)]
        if missing:
            missing_list = ", ".join(missing)
            raise PermissionError(
                f"Missing permissions in {partners_channel.mention}: {missing_list}. "
                "Grant the bot role thread creation and messaging rights."
            )
        
        # Get today's date for thread naming
        today = datetime.now(timezone.utc)
        date_str = today.strftime("%m/%d")
        
        # Thread templates
        thread_templates = {
            "active_projects": {
                "name": f"Active Project Coordination: {date_str}",
                "starter": "**Post updates on project status for active projects**\n• Use this thread for any needed coordination on tasks/deliverables for active projects"
            },
            "business_dev": {
                "name": f"Business Development: {date_str}",
                "starter": "**Post updates on pipeline, proposals, and business development activities**\n• New opportunities\n• Proposal status\n• Client relationships"
            },
            "c_suite": {
                "name": f"C-Suite Items: {date_str}",
                "starter": "**Post updates or initiate discussion related to company management**\n• Financials\n• Administrative matters\n• Strategic decisions"
            },
            "sandbox": {
                "name": f"Sandbox: {date_str}",
                "starter": "**Space for experiments, ideas, and non-urgent topics**\n• R&D updates\n• Technology exploration\n• Wild ideas"
            }
        }
        
        # Create announcement message
        announcement = f"""🗓️ **Async Thursday Meeting - {date_str}**

Threads created below. Please review and post updates in relevant threads by end of day.

@Partners - See threads for specific areas needing input."""
        
        announcement_msg = await partners_channel.send(announcement)

        # Action agenda (single canonical post at kickoff)
        try:
            if getattr(self.bot, "db", None):
                async with self.bot.db.acquire() as conn, conn.execute(
                    """
                        SELECT id, title, status, due_date, priority, assigned_to_user_id
                        FROM tasks
                        WHERE tags LIKE ?
                          AND status IN ('todo','in_progress','blocked')
                        ORDER BY (due_date IS NULL) ASC, due_date ASC, updated_at DESC
                        LIMIT 50
                        """,
                    ("%action_item%",),
                ) as cursor:
                    rows = await cursor.fetchall()

                if rows:
                    by_owner = {}
                    for r in rows:
                        owner_id = int(r[5]) if r[5] is not None else None
                        by_owner.setdefault(owner_id, []).append(r)

                    lines = []
                    lines.append("📌 **Action items agenda (this week)**")
                    lines.append("Use `/action list` to update status/ownership.")
                    lines.append("")

                    owners = [oid for oid in by_owner if oid is not None]
                    owners.sort()
                    if None in by_owner:
                        owners.append(None)

                    # Keep kickoff tight: max ~4 owners + 4 items each, then truncate.
                    for oid in owners[:5]:
                        items = by_owner.get(oid) or []
                        header = "**Unassigned**" if oid is None else f"<@{oid}>"
                        lines.append(header)
                        for item in items[:4]:
                            _id, title, status, due_date, priority, owner_id = item
                            due_txt = f" (due {due_date})" if due_date else ""
                            lines.append(f"- **#{_id}** [{status}] {title}{due_txt}")
                        lines.append("")

                    allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
                    await partners_channel.send("\n".join(lines)[:1900], allowed_mentions=allowed_mentions)
        except Exception as e:
            logger.warning(f"Failed to post action agenda: {e}")

        # Partner updates agenda ("did something" log)
        try:
            if getattr(self.bot, "db", None):
                lookback = (datetime.now(timezone.utc) - timedelta(days=int(PARTNER_UPDATE_LOOKBACK_DAYS))).isoformat()
                async with self.bot.db.acquire() as conn, conn.execute(
                    """
                        SELECT id, partner_user_id, partner_username, category, details, link, created_at
                        FROM partner_updates
                        WHERE used_at IS NULL
                          AND datetime(created_at) >= datetime(?)
                        ORDER BY datetime(created_at) ASC
                        LIMIT ?
                        """,
                    (lookback, int(PARTNER_UPDATE_MAX_SURFACE)),
                ) as cursor:
                    updates = [dict(r) for r in (await cursor.fetchall() or [])]

                if updates:
                    lines = []
                    lines.append("✨ **Partner updates since last meeting**")
                    lines.append("Log yours with `/did` (1/day anti-spam).")
                    lines.append("")
                    for u in updates:
                        uid = u.get("partner_user_id")
                        mention = f"<@{int(uid)}>" if uid else (u.get("partner_username") or "Partner")
                        category = (u.get("category") or "update").strip()
                        details = (u.get("details") or "").strip().replace("\n", " ")
                        link = (u.get("link") or "").strip()
                        link_txt = f" ({link})" if link else ""
                        prefix = f"[{category}] " if category and category != "update" else ""
                        lines.append(f"- {mention}: {prefix}{details[:240]}{link_txt}")

                    allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
                    await partners_channel.send("\n".join(lines)[:1900], allowed_mentions=allowed_mentions)

                    # Mark surfaced updates as consumed so we don't repost.
                    try:
                        ids = [int(u["id"]) for u in updates if u.get("id") is not None]
                        if ids:
                            placeholders = ",".join(["?"] * len(ids))
                            await self.bot.db.execute(
                                f"UPDATE partner_updates SET used_at = datetime('now'), used_in_meeting_date = ? WHERE id IN ({placeholders})",
                                date_str,
                                *ids,
                            )
                    except Exception as e:
                        logger.warning(f"Failed to mark partner updates as used: {e}")
        except Exception as e:
            logger.warning(f"Failed to post partner updates agenda: {e}")
        
        # Create threads
        self.async_meeting_threads = {}
        for key, template in thread_templates.items():
            try:
                thread = await announcement_msg.create_thread(
                    name=template["name"],
                    auto_archive_duration=1440  # 24 hours
                )
                await thread.send(template["starter"])
                self.async_meeting_threads[key] = thread.id
                await asyncio.sleep(1)  # Rate limit protection
            except Exception as e:
                logger.error(f"Failed to create thread {key}: {e}")
        
        await self.post_to_bots_channel("coordinator", f"✅ Async meeting initiated: {len(self.async_meeting_threads)} threads created")
        
        # Schedule EOD summary for 6pm EST (11pm UTC)
        now_utc = datetime.now(timezone.utc)
        hours_until_eod = (23 - now_utc.hour) % 24
        asyncio.create_task(self._async_meeting_eod_summary(hours_until_eod))
    
    # ========================================
    # COORDINATOR: End-of-Week Reflection
    # ========================================
    
    @tasks.loop(time=dt_time(hour=18, minute=0, tzinfo=EASTERN))
    async def end_of_week_reflection(self):
        """Run every Thursday at 6pm Eastern: close the loop on the week"""
        await self.bot.wait_until_ready()
        
        now = datetime.now(EASTERN)
        
        # Only run on Thursdays
        if now.weekday() != 3:
            return
        
        # Idempotency check
        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("end_of_week_reflection", today):
            logger.info(f"end_of_week_reflection already ran for {today}, skipping")
            return
        
        await self.post_to_bots_channel("coordinator", "📝 Generating end-of-week reflection...")
        
        try:
            await self._run_weekly_reflection()
            await self._record_job_run("end_of_week_reflection", today)
        except Exception as e:
            logger.error(f"End-of-week reflection failed: {e}")
            await self.post_to_bots_channel("coordinator", f"❌ Reflection failed: {str(e)}")
    
    async def _run_weekly_reflection(self):
        """Generate weekly reflection on completed work and gaps"""
        partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if not partners_channel:
            await self.post_to_bots_channel("coordinator", "⚠️ Could not find weekly-meeting-threads channel")
            return
        
        # Gather data from last 7 days
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        
        # Get completed action items
        completed_actions = []
        if getattr(self.bot, "db", None):
            try:
                async with self.bot.db.acquire() as conn, conn.execute(
                    """
                        SELECT id, title, completed_at, assigned_to_username, notes
                        FROM tasks
                        WHERE tags LIKE ?
                          AND status = 'done'
                          AND completed_at >= ?
                        ORDER BY completed_at DESC
                        LIMIT 50
                        """,
                    ("%action_item%", week_ago.isoformat()),
                ) as cursor:
                    rows = await cursor.fetchall()
                    completed_actions = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"Failed to fetch completed actions: {e}")
        
        # Get resolved knowledge gaps
        resolved_gaps = []
        if getattr(self.bot, "db", None):
            try:
                async with self.bot.db.acquire() as conn, conn.execute(
                    """
                        SELECT id, topic, question, resolved_at, resolved_via
                        FROM knowledge_gaps
                        WHERE status = 'resolved'
                          AND resolved_at >= ?
                        ORDER BY resolved_at DESC
                        LIMIT 30
                        """,
                    (week_ago.isoformat(),),
                ) as cursor:
                    rows = await cursor.fetchall()
                    resolved_gaps = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"Failed to fetch resolved gaps: {e}")
        
        # Get open action items that should roll forward
        rollforward_actions = []
        if getattr(self.bot, "db", None):
            try:
                async with self.bot.db.acquire() as conn, conn.execute(
                    """
                        SELECT id, title, status, priority, assigned_to_username, due_date
                        FROM tasks
                        WHERE tags LIKE ?
                          AND status IN ('todo', 'in_progress', 'blocked')
                          AND (due_date IS NULL OR due_date >= date('now'))
                        ORDER BY priority DESC, (due_date IS NULL) ASC, due_date ASC
                        LIMIT 20
                        """,
                    ("%action_item%",),
                ) as cursor:
                    rows = await cursor.fetchall()
                    rollforward_actions = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"Failed to fetch rollforward actions: {e}")

        # Get partner scoreboard (last 7 days)
        leaderboard = []
        team_points = 0
        if getattr(self.bot, "db", None):
            try:
                async with self.bot.db.acquire() as conn:
                    async with conn.execute(
                        """
                        SELECT partner_user_id,
                               COALESCE(partner_username, '') as partner_username,
                               SUM(points) as pts
                        FROM partner_point_events
                        WHERE datetime(created_at) >= datetime(?)
                        GROUP BY partner_user_id, partner_username
                        ORDER BY pts DESC
                        LIMIT 5
                        """,
                        (week_ago.isoformat(),),
                    ) as cursor:
                        rows = await cursor.fetchall()
                        leaderboard = [dict(r) for r in rows] if rows else []

                    async with conn.execute(
                        """
                        SELECT COALESCE(SUM(points), 0) as pts
                        FROM partner_point_events
                        WHERE datetime(created_at) >= datetime(?)
                        """,
                        (week_ago.isoformat(),),
                    ) as cursor:
                        row = await cursor.fetchone()
                        team_points = int(row[0] or 0) if row else 0
            except Exception as e:
                logger.warning(f"Failed to fetch partner scoreboard: {e}")
        
        # Build reflection message
        lines = []
        lines.append("# 📝 End-of-Week Reflection")
        lines.append(f"*Week ending {datetime.now(EASTERN).strftime('%B %d, %Y')}*")
        lines.append("")
        
        lines.append("## ✅ What Got Completed This Week")
        if completed_actions:
            for action in completed_actions[:10]:
                owner = action.get('assigned_to_username') or 'Unassigned'
                lines.append(f"- **#{action['id']}** {action['title']} ({owner})")
        else:
            lines.append("- *No action items completed this week*")
        lines.append("")
        
        lines.append("## 🔍 Knowledge Gaps Answered/Closed")
        if resolved_gaps:
            for gap in resolved_gaps[:8]:
                method = gap.get('resolved_via') or 'unknown'
                lines.append(f"- {gap['topic']}: {gap['question'][:80]}... (via {method})")
        else:
            lines.append("- *No knowledge gaps resolved this week*")
        lines.append("")
        
        lines.append("## ➡️ Rolling Into Next Week")
        if rollforward_actions:
            lines.append("*These action items remain active:*")
            for action in rollforward_actions[:12]:
                status_emoji = {"todo": "⏹️", "in_progress": "🔄", "blocked": "🚫"}.get(action['status'], "❓")
                owner = action.get('assigned_to_username') or 'Unassigned'
                due_txt = f" (due {action['due_date']})" if action.get('due_date') else ""
                lines.append(f"- {status_emoji} **#{action['id']}** {action['title']}{due_txt} - {owner}")
        else:
            lines.append("- *No open action items*")
        lines.append("")

        lines.append("## 🏆 Partner Scoreboard (Last 7 Days)")
        if leaderboard:
            for i, entry in enumerate(leaderboard, start=1):
                uid = entry.get('partner_user_id')
                pts = entry.get('pts') or 0
                mention = f"<@{int(uid)}>" if uid else (entry.get('partner_username') or 'Partner')
                lines.append(f"- {i}. {mention}: **{int(pts)}** impact points")
            lines.append(f"- Team total: **{int(team_points)}** impact points")
        else:
            lines.append("- *No impact points recorded this week yet*")
        lines.append("")
        
        lines.append("---")
        lines.append("Use `/action list` to manage items | `/gaps list` to view open questions")
        
        reflection_text = "\n".join(lines)[:1950]
        
        # Post reflection
        await partners_channel.send(reflection_text)
        await self.post_to_bots_channel("coordinator", f"✅ Weekly reflection posted: {len(completed_actions)} completed, {len(resolved_gaps)} gaps closed, {len(rollforward_actions)} rolling forward")
    
    async def _async_meeting_eod_summary(self, hours_delay: int):
        """Generate end-of-day summary for async meeting"""
        await asyncio.sleep(hours_delay * 3600)
        
        await self.post_to_bots_channel("coordinator", "Generating async meeting summary...")
        
        try:
            # Collect messages from all threads
            thread_summaries = {}
            
            for key, thread_id in self.async_meeting_threads.items():
                thread = self.bot.get_channel(thread_id)
                if not thread:
                    continue
                
                messages = []
                async for msg in thread.history(limit=100):
                    if not msg.author.bot:
                        messages.append(f"{msg.author.name}: {msg.content}")
                
                thread_summaries[key] = messages
            
            # Generate summary with LLM
            summary = await self._generate_meeting_summary(thread_summaries)
            
            # Post summary to main channel
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            if partners_channel:
                embed = discord.Embed(
                    title="📋 Async Meeting Summary",
                    description=summary,
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_footer(text="Meeting Summary")
                await partners_channel.send(embed=embed)
                
            await self.post_to_bots_channel("coordinator", "✅ Async meeting summary posted")
            
        except Exception as e:
            logger.error(f"EOD summary failed: {e}")
            await self.post_to_bots_channel("coordinator", f"❌ EOD summary failed: {str(e)}")
        finally:
            self.async_meeting_active = False
            self.async_meeting_threads = {}
    
    async def _generate_meeting_summary(self, thread_summaries: Dict[str, List[str]]) -> str:
        """Generate summary of async meeting threads"""
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        prompt = ChatPromptTemplate.from_template(f"""
You are the Coordinator department of {org['org_name']}'s operations bot.
Summarize today's async meeting threads into key takeaways and action items.

Thread discussions:
{{threads}}

Generate a concise summary with:
- Key decisions made
- Action items (with owners if mentioned)
- Open questions
- Next steps

Max 300 words, bullet format.

Summary:""")
        
        threads_text = "\n\n".join([
            f"## {key.upper().replace('_', ' ')}\n" + "\n".join(msgs[:10])
            for key, msgs in thread_summaries.items()
        ])
        
        if not self.llm_service:
            raise RuntimeError("LLM service is not configured")
        return await self.llm_service.generate(prompt, {"threads": threads_text})
    
    # ========================================
    # CHIEF: Weekly Strategic Review
    # ========================================
    
    @tasks.loop(time=dt_time(hour=20, minute=0, tzinfo=EASTERN))
    async def weekly_strategic_review(self):
        """Run every Sunday at 8pm Eastern: strategic business analysis"""
        await self.bot.wait_until_ready()
        
        now = datetime.now(EASTERN)
        
        # Only run on Sundays
        if now.weekday() != 6:
            return
        
        # Idempotency check
        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("weekly_strategic_review", today):
            logger.info(f"weekly_strategic_review already ran for {today}, skipping")
            return
        
        await self.post_to_bots_channel("chief", "🎯 Beginning weekly strategic review...")
        
        try:
            # Scan last week's activity
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            if not partners_channel:
                return
            
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            messages = []
            
            async for message in partners_channel.history(after=week_ago, limit=1000):
                if not message.author.bot:
                    messages.append({
                        "author": message.author.name,
                        "content": message.content,
                        "timestamp": message.created_at.isoformat()
                    })
            
            # Generate strategic analysis
            analysis = await self._generate_strategic_analysis(messages)
            
            # Post to bots channel for review
            embed = discord.Embed(
                title="🎯 Weekly Strategic Review",
                description=analysis,
                color=discord.Color.purple(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text="Strategic Analysis")
            
            await self.post_to_bots_channel("chief", "Strategic review complete", embed=embed)
            
            # Record successful completion
            await self._record_job_run("weekly_strategic_review", today)
            
        except Exception as e:
            logger.error(f"Strategic review failed: {e}")
            await self.post_to_bots_channel("chief", f"❌ Strategic review failed: {str(e)}")
    
    async def _generate_strategic_analysis(self, messages: List[Dict]) -> str:
        """Generate strategic business analysis"""
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        prompt = ChatPromptTemplate.from_template(f"""
You are the Chief department of {org['org_name']}'s operations bot - responsible for strategic business health.

Analyze the last week of partner communication and identify:
- Pipeline health (new opportunities, stalled deals, wins/losses)
- Resource/capacity issues (overload signals, availability gaps)
- Financial signals (budget discussions, cash flow mentions)
- Strategic risks or opportunities
- Recommendations for next week

Last week's activity:
{{messages}}

Be direct and actionable. Flag concerns clearly. Max 250 words.

Strategic Analysis:""")
        
        messages_text = "\n\n".join([
            f"[{m['timestamp']}] {m['author']}: {m['content'][:300]}"
            for m in messages[-50:]  # Last 50 messages
        ])
        
        if not self.llm_service:
            raise RuntimeError("LLM service is not configured")
        return await self.llm_service.generate(prompt, {"messages": messages_text})

    # ========================================
    # SCOUT: Daily Opportunity Search
    # (Helper methods now inherited from ScoutMixin)
    # ========================================
    
    @tasks.loop(time=dt_time(hour=7, minute=0, tzinfo=EASTERN))
    async def daily_scout_search(self):
        """Run daily at 7am Eastern: search for opportunities and intel"""
        await self.bot.wait_until_ready()
        
        # Idempotency check
        now = datetime.now(EASTERN)
        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("daily_scout_search", today):
            logger.info(f"daily_scout_search already ran for {today}, skipping")
            return
        
        if not (self.tavily_service and self.tavily_service.is_configured):
            logger.warning("Scout search skipped - Tavily not configured")
            return
        
        await self.post_to_bots_channel("scout", "🔍 Starting daily opportunity search...")
        
        try:
            day_signature = f"{now.date().isoformat()}-W{now.isocalendar()[1]}"
            plan = await self._generate_scout_plan(day_signature)
            findings = []
            plan_usage: List[Dict[str, str]] = []

            for idx, mission in enumerate(plan, start=1):
                query = mission.get("query")
                if not query:
                    continue
                rationale = mission.get("rationale", "")
                perspective = mission.get("perspective", "")
                tag = mission.get("tag", f"Path {idx}")
                note = f"{tag}: {perspective} — {rationale}".strip(" -")
                await self.post_to_bots_channel("scout", f"{note}\nSearching: {query[:100]}...")

                try:
                    # Tavily search tuned per mission
                    max_results = int(mission.get("max_results", 5))
                    per_query_cap = max(1, min(max_results, 5))
                    results = await self.tavily_service.search(
                        query=query,
                        search_depth=mission.get("depth", "advanced"),
                        max_results=max_results,
                        include_domains=mission.get("domains") or None,
                        topic=mission.get("topic") or None,
                        time_range=mission.get("time_range") or None,
                        country=mission.get("country") or None,
                        auto_parameters=mission.get("auto_parameters", True),
                    )
                    
                    if results.get("results"):
                        for result in results["results"][:per_query_cap]:  # Cap per query to avoid noise
                            findings.append({
                                "title": result.get("title", "No title"),
                                "url": result.get("url", ""),
                                "snippet": result.get("content", "")[:300],
                                "score": result.get("score", 0),
                                "origin": tag
                            })
                        plan_usage.append(mission)
                    
                    await asyncio.sleep(2)  # Rate limit protection
                    
                except Exception as e:
                    logger.error(f"Search failed for '{query}': {e}")
                    continue
            
            if not findings:
                await self.post_to_bots_channel("scout", "No significant opportunities found today")
                return

            # Novelty loop: record what we saw, then generate follow-up search seeds.
            try:
                state = self._load_scout_state()
                seen_urls = state.get("seen_urls")
                if not isinstance(seen_urls, dict):
                    seen_urls = {}
                seen_domains = state.get("seen_domains")
                if not isinstance(seen_domains, dict):
                    seen_domains = {}

                for f in findings:
                    url = (f.get("url") or "").strip()
                    if url:
                        seen_urls[url] = today
                    d = self._domain_from_url(url) if url else None
                    if d:
                        seen_domains[d] = int(seen_domains.get(d, 0) or 0) + 1

                state["seen_urls"] = seen_urls
                state["seen_domains"] = seen_domains
                state = self._scout_cleanup_state(state, today)
                self._save_scout_state(state)

                novel = self._select_novel_findings(findings, today)
                if novel:
                    await self._enqueue_followup_queries_from_findings(novel, today)
            except Exception as exc:
                logger.warning(f"Scout novelty loop failed (non-fatal): {exc}")
            
            # Filter and rank findings
            sorted_findings = sorted(findings, key=lambda f: float(f.get("score") or 0), reverse=True)
            high_value_findings = [f for f in sorted_findings if (f.get("score") or 0) > 0.7]

            # Tavily scores are often < 0.7; fall back to a softer threshold and finally to top-N.
            threshold_note = ""
            if not high_value_findings:
                high_value_findings = [f for f in sorted_findings if (f.get("score") or 0) > 0.5]
                threshold_note = " (soft threshold)"
            if not high_value_findings:
                high_value_findings = sorted_findings[:5]
                threshold_note = " (top results)"
            if not high_value_findings:
                await self.post_to_bots_channel("scout", f"Found {len(findings)} results but none were usable")
                return
            
            # Generate summary with LLM
            summary = await self._generate_scout_summary(high_value_findings, plan_usage or plan)
            
            # Post to bots channel
            embed = discord.Embed(
                title="🔍 Daily Scout Report",
                description=summary,
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            plan_overview = "\n".join([
                f"• **{mission.get('tag', f'Path {i+1}')}**: {mission.get('perspective', '')}"
                for i, mission in enumerate(plan_usage or plan)
            ])[:1000]
            if plan_overview:
                embed.add_field(name="Research Paths", value=plan_overview, inline=False)
            
            # Add top findings as fields
            for finding in high_value_findings[:3]:
                embed.add_field(
                    name=f"{finding.get('origin', '')} • {finding['title'][:80]}",
                    value=f"[Link]({finding['url']})\n{finding['snippet'][:150]}...",
                    inline=False
                )
            
            embed.set_footer(text="Web Intelligence")
            
            await self.post_to_bots_channel("scout", f"✅ Found {len(high_value_findings)} high-value opportunities", embed=embed)
            if threshold_note:
                await self.post_to_bots_channel("scout", f"Scout note: selection used{threshold_note}.")

            # ===== RAINMAKER INTEGRATION: Auto-create leads from high-value findings =====
            leads_created = 0
            created_leads = []
            for finding in high_value_findings[:2]:  # Top 2 findings become leads
                score = finding.get('score', 0)
                if score >= 0.7:  # Only high-confidence findings
                    lead_id = await self._rainmaker_create_lead(
                        title=finding['title'][:100],
                        source='scout',
                        description=f"{finding.get('snippet', '')[:300]}\n\nURL: {finding.get('url', '')}",
                        source_id=finding.get('url', ''),
                        priority='medium' if score < 0.85 else 'high'
                    )
                    if lead_id:
                        leads_created += 1
                        created_leads.append({
                            'id': lead_id,
                            'title': finding['title'][:80],
                            'url': finding.get('url', ''),
                            'priority': 'high' if score >= 0.85 else 'medium'
                        })

            if leads_created:
                # Build a compelling Rainmaker-style alert
                embed = discord.Embed(
                    title="💰 New Business Opportunities Detected",
                    description="Scout found these prospects worth pursuing. The story writes itself - we just need to tell it.",
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc)
                )
                for lead in created_leads:
                    priority_emoji = "🔥" if lead['priority'] == 'high' else "📌"
                    embed.add_field(
                        name=f"{priority_emoji} {lead['name']}",
                        value=f"[Explore →]({lead['url']})\nLead ID: `{lead['id']}`",
                        inline=False
                    )
                embed.add_field(
                    name="Next Steps",
                    value="• `/lead list` - Review all leads\n• `/lead update <id>` - Add notes\n• `/lead convert <id>` - Mark as opportunity",
                    inline=False
                )
                embed.set_footer(text="Pipeline • Turning opportunities into closers")
                await self.post_to_bots_channel("rainmaker", embed=embed)
            
            # Record successful completion
            await self._record_job_run("daily_scout_search", today)
            
        except Exception as e:
            logger.error(f"Scout search failed: {e}")
            await self.post_to_bots_channel("scout", f"❌ Search failed: {str(e)}")

    # _scout_pop_seed_queries and _scout_default_crawl_queries
    # are now inherited from ScoutMixin

    @tasks.loop(minutes=45)
    async def scout_background_crawl(self):
        """Lightweight crawler-like web browsing: runs throughout the day, consuming novelty seeds and surfacing fresh opportunities."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        # Weekdays only, business hours only (avoid overnight spam)
        if now.weekday() >= 5:
            return
        if now.hour < 9 or now.hour > 17:
            return

        today = now.strftime("%Y-%m-%d")
        run_key = f"{today}T{now.hour:02d}:{(now.minute // 15) * 15:02d}"
        if await self._job_already_ran("scout_background_crawl", run_key):
            return

        if not (self.tavily_service and self.tavily_service.is_configured):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            seeds = self._scout_pop_seed_queries(max_items=2)
            missions = seeds if seeds else self._scout_default_crawl_queries()[:2]
            if not missions:
                await self._record_job_run("scout_background_crawl", run_key)
                return

            findings: List[Dict[str, Any]] = []
            search_log: List[str] = []

            for mission in missions:
                query = (mission.get("query") or "").strip()
                if not query:
                    continue
                tag = (mission.get("tag") or "Crawl")
                search_log.append(f"🔎 **{tag}**: {query[:70]}…")

                try:
                    results = await self.tavily_service.search(
                        query=query,
                        search_depth=mission.get("depth", "advanced"),
                        max_results=int(mission.get("max_results", 6)),
                        topic=mission.get("topic") or None,
                        time_range=mission.get("time_range") or None,
                        country=mission.get("country") or None,
                        auto_parameters=mission.get("auto_parameters", True),
                    )
                    for r in (results.get("results") or [])[:6]:
                        findings.append(
                            {
                                "title": r.get("title", "No title"),
                                "url": r.get("url", ""),
                                "snippet": (r.get("content", "") or "")[:400],
                                "score": float(r.get("score") or 0),
                                "query": query,
                                "origin": tag,
                            }
                        )
                except Exception as exc:
                    logger.warning(f"scout_background_crawl search failed: {exc}")

                await asyncio.sleep(1.2)

            if not findings:
                await self._record_job_run("scout_background_crawl", run_key)
                return

            # Deduplicate against Rainmaker's seen-opportunities table to prove freshness.
            fresh: List[Dict[str, Any]] = []
            stale_count = 0
            async with db.acquire() as conn:
                for f in findings:
                    url = (f.get("url") or "").strip()
                    if not url:
                        continue
                    url_hash = hashlib.md5(url.encode()).hexdigest()
                    async with conn.execute(
                        "SELECT id FROM rainmaker_seen_opportunities WHERE url_hash = ?",
                        (url_hash,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row:
                        stale_count += 1
                        await conn.execute(
                            "UPDATE rainmaker_seen_opportunities SET last_seen_date = ?, seen_count = seen_count + 1 WHERE url_hash = ?",
                            (today, url_hash),
                        )
                        continue

                    await conn.execute(
                        """INSERT INTO rainmaker_seen_opportunities
                           (url, url_hash, title, source_query, first_seen_date, last_seen_date)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (url, url_hash, (f.get("title") or "")[:200], (f.get("query") or "")[:200], today, today),
                    )
                    fresh.append(f)

                await conn.commit()

            # Show work (searches performed + freshness stats).
            total = len(findings)
            fresh_count = len(fresh)
            header = f"🕸️ **Crawl pulse** ({run_key} ET) — found {total} URLs ({fresh_count} fresh, {stale_count} repeats)"
            await self.post_to_bots_channel("scout", header)
            if search_log:
                await self.post_to_bots_channel("scout", "**Searches:**\n" + "\n".join(search_log[:6]))

            if not fresh:
                await self._record_job_run("scout_background_crawl", run_key)
                return

            # Assess a capped set to keep costs sane.
            fresh_sorted = sorted(fresh, key=lambda x: float(x.get("score") or 0), reverse=True)
            to_assess = fresh_sorted[:4]
            assessed = await self._rainmaker_assess_opportunities(to_assess)
            elevated = [a for a in assessed if a.get("verdict") == "elevate"]
            passed = [a for a in assessed if a.get("verdict") == "pass"]

            # Post quick pass list (evidence of filtering)
            if passed:
                lines = []
                for p in passed[:4]:
                    lines.append(f"• ~~{(p.get('title') or '')[:70]}~~ — {(p.get('reason') or 'Not a fit')[:80]}")
                await self.post_to_bots_channel("rainmaker", f"🧹 **Crawl filtered out ({len(passed)}):**\n" + "\n".join(lines))

            leads_created = 0
            if elevated:
                embed = discord.Embed(
                    title="🕸️ Crawl Elevations",
                    description="Fresh opportunities surfaced by background browsing (showing work + reasons).",
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc),
                )

                # Cap lead creation per pulse
                for opp in elevated[:2]:
                    lead_id = await self._rainmaker_create_lead(
                        title=(opp.get("title") or "")[:100],
                        source="scout",
                        description=(
                            f"**Origin query:** {(opp.get('query') or '')[:140]}\n"
                            f"**Why this matters:** {(opp.get('reason') or '')[:220]}\n\n"
                            f"{(opp.get('snippet') or '')[:300]}\n\nURL: {(opp.get('url') or '')}"
                        ),
                        source_id=opp.get("url") or "",
                        priority="high" if float(opp.get("confidence") or 0) >= 0.8 else "medium",
                    )
                    if lead_id:
                        leads_created += 1
                        url = (opp.get("url") or "").strip()
                        if url:
                            url_hash = hashlib.md5(url.encode()).hexdigest()
                            await db.execute(
                                "UPDATE rainmaker_seen_opportunities SET assessment = 'elevated', assessment_reason = ?, lead_id = ? WHERE url_hash = ?",
                                ((opp.get("reason") or "")[:200], int(lead_id), url_hash),
                                )
                        embed.add_field(
                            name=f"🔥 {(opp.get('title') or '')[:80]}",
                            value=f"**Why:** {(opp.get('reason') or 'Strong fit')[:140]}\n[Open →]({opp.get('url') or ''})\nLead ID: `{lead_id}`",
                            inline=False,
                        )

                embed.set_footer(text=f"Crawl pulse • leads created: {leads_created}")
                await self.post_to_bots_channel("rainmaker", embed=embed)

            # Mark passed assessments in seen-opportunities table
            for opp in passed:
                url = (opp.get("url") or "").strip()
                if not url:
                    continue
                url_hash = hashlib.md5(url.encode()).hexdigest()
                try:
                    async with db.acquire() as conn:
                        await conn.execute(
                            "UPDATE rainmaker_seen_opportunities SET assessment = 'passed', assessment_reason = ? WHERE url_hash = ?",
                            ((opp.get("reason") or "")[:200], url_hash),
                        )
                        await conn.commit()
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

            await self._record_job_run("scout_background_crawl", run_key)

        except Exception as e:
            logger.error(f"scout_background_crawl failed: {e}")
            try:
                await self._record_job_run("scout_background_crawl", run_key)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
    
    @tasks.loop(time=dt_time(hour=6, minute=0, tzinfo=EASTERN))
    async def daily_knowledge_refresh(self):
        """
        Run incremental Chroma reindexing daily at 6am EST.
        Only processes changed/new files from docs/ folder - no expensive full rebuild.
        """
        today = datetime.now(EASTERN).date().isoformat()
        
        if await self._job_already_ran("daily_knowledge_refresh", today):
            logger.info("Knowledge refresh already ran today, skipping")
            return
        
        logger.info("Starting daily knowledge refresh (incremental reindex)")
        await self.post_to_bots_channel("coordinator", "🔄 Knowledge refresh starting...")
        
        try:
            import sys
            from pathlib import Path
            
            # Path to ingest script
            ingest_script = Path(__file__).parent / "ingest_metadata.py"
            
            # Run incremental ingest as subprocess
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(ingest_script), "--verbose",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ingest_script.parent.parent)
            )
            
            stdout, stderr = await proc.communicate()
            
            if proc.returncode != 0:
                error_msg = stderr.decode()[:200]
                logger.error(f"Knowledge refresh failed: {error_msg}")
                await self.post_to_bots_channel("coordinator", f"❌ Knowledge refresh failed: {error_msg}")
                return
            
            # Parse stats from output
            output = stdout.decode()
            stats = {"added": 0, "updated": 0, "chunks": 0}
            
            for line in output.split("\n"):
                if "Added files:" in line:
                    stats["added"] = int(line.split(":")[-1].strip())
                elif "Updated files:" in line:
                    stats["updated"] = int(line.split(":")[-1].strip())
                elif "Chunks written:" in line:
                    stats["chunks"] = int(line.split(":")[-1].strip())
            
            # Report results
            if stats["chunks"] > 0:
                msg = (
                    f"✅ Knowledge refresh complete:\n"
                    f"📄 +{stats['added']} new files, ~{stats['updated']} updated\n"
                    f"📦 {stats['chunks']} chunks reindexed\n"
                    f"🧠 RAG now includes latest memos and docs"
                )
            else:
                msg = "✅ Knowledge refresh complete: No changes detected (index up to date)"
            
            await self.post_to_bots_channel("coordinator", msg)
            logger.info(f"Knowledge refresh complete: {stats}")
            
            # Record completion
            await self._record_job_run("daily_knowledge_refresh", today)
            
        except Exception as e:
            logger.error(f"Knowledge refresh error: {e}")
            await self.post_to_bots_channel("coordinator", f"❌ Knowledge refresh error: {str(e)[:100]}")
    
    @daily_knowledge_refresh.before_loop
    async def before_knowledge_refresh(self):
        await self.bot.wait_until_ready()

    # ========================================
    # CURATOR: Agentic Corpus Quality & Expansion
    # (Core logic inherited from CuratorMixin)
    # ========================================

    @tasks.loop(time=dt_time(hour=7, minute=30, tzinfo=EASTERN))
    async def curator_daily_scan_task(self):
        """Daily lightweight corpus scan: thin topics, stale docs, fragment detection."""
        await self.curator_daily_scan()

    @curator_daily_scan_task.before_loop
    async def before_curator_daily_scan(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=8, minute=0, tzinfo=EASTERN))
    async def curator_weekly_deep_analysis_task(self):
        """Weekly deep analysis: full health report + auto-synthesis. Saturdays only."""
        now = datetime.now(EASTERN)
        if now.weekday() != 5:  # Saturday
            return
        await self.curator_weekly_deep_analysis()

    @curator_weekly_deep_analysis_task.before_loop
    async def before_curator_deep_analysis(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=9, minute=0, tzinfo=EASTERN))
    async def curator_corpus_interrogation_task(self):
        """Self-interrogation: structural gap analysis, web research, human escalation.

        Runs Wednesday and Saturday — spaced after the daily scan (07:30) and
        deep analysis (Sat 08:00) so coverage data is fresh.
        """
        now = datetime.now(EASTERN)
        if now.weekday() not in (2, 5):  # Wednesday, Saturday
            return
        await self.curator_corpus_interrogation()

    @curator_corpus_interrogation_task.before_loop
    async def before_curator_interrogation(self):
        await self.bot.wait_until_ready()

    # _generate_scout_summary is now inherited from ScoutMixin

    # ========================================
    # DREAMER: Wild Ideas & Blue-Sky Exploration
    # (Helper methods now inherited from DreamerMixin)
    # ========================================

    @tasks.loop(time=dt_time(hour=14, minute=30, tzinfo=EASTERN))
    async def dreamer_ideation_cycle(self):
        """Run Tuesdays at 2:30pm Eastern: generate and investigate wild ideas.

        The Dreamer is a creative persona that:
        1. Conjures unconventional ideas (new products, ventures, partnerships)
        2. Dispatches Scout and Archivist to gather evidence
        3. Refines ideas based on findings
        4. Escalates the most promising ones to the Manager
        """
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        # Only run on Tuesdays (bi-weekly feel, but weekly simplifies logic)
        if now.weekday() != 1:
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("dreamer_ideation_cycle", today):
            logger.info(f"dreamer_ideation_cycle already ran for {today}, skipping")
            return

        await self.post_to_bots_channel("dreamer", "💭 *The Dreamer awakens, mind churning with strange possibilities...*")

        try:
            # ------------------------------------------------------------------
            # 0. Context Gathering: ground the Dreamer in current business reality
            # ------------------------------------------------------------------
            await self.post_to_bots_channel("dreamer", "📋 Reviewing what the company is actually working on...")
            business_context = await self._dreamer_gather_business_context()

            ctx_summary = []
            if business_context.get("active_projects"):
                ctx_summary.append(f"{len(business_context['active_projects'])} active projects")
            if business_context.get("open_gaps"):
                ctx_summary.append(f"{len(business_context['open_gaps'])} open knowledge gaps")
            if business_context.get("recent_wins"):
                ctx_summary.append(f"{len(business_context['recent_wins'])} recent updates")
            if ctx_summary:
                await self.post_to_bots_channel("dreamer", f"🔗 Grounded in: {', '.join(ctx_summary)}")

            # ------------------------------------------------------------------
            # 1. Dream Phase: generate 2-3 ideas anchored to real context
            # ------------------------------------------------------------------
            ideas = await self._dreamer_generate_ideas(today, business_context)
            if not ideas:
                await self.post_to_bots_channel("dreamer", "🌫️ The mists are thick today; no visions came through.")
                await self._record_job_run("dreamer_ideation_cycle", today)
                return

            await self.post_to_bots_channel(
                "dreamer",
                f"✨ {len(ideas)} nascent visions emerged:\n" + "\n".join(f"• {i['title']}" for i in ideas[:3])
            )

            # ------------------------------------------------------------------
            # 2. Investigation Phase: deploy Scout (web) and Archivist (internal)
            # ------------------------------------------------------------------
            investigated: List[Dict[str, Any]] = []
            for idea in ideas[:2]:  # Investigate top 2 to conserve API budget
                await self.post_to_bots_channel("dreamer", f"🔮 Sending scouts to explore: *{idea['title']}*")

                scout_findings = await self._dreamer_dispatch_scout(idea)
                archivist_findings = await self._dreamer_dispatch_archivist(idea)

                idea["scout_findings"] = scout_findings
                idea["archivist_findings"] = archivist_findings
                investigated.append(idea)

                await asyncio.sleep(2)  # Breathing room between API calls

            # ------------------------------------------------------------------
            # 3. Refinement Phase: synthesize findings and score viability
            # ------------------------------------------------------------------
            refined = await self._dreamer_refine_ideas(investigated)

            # ------------------------------------------------------------------
            # 4. Escalation Phase: pass best idea(s) to the Manager
            # ------------------------------------------------------------------
            # Require BOTH viability >= 6 AND grounding >= 5
            promising = [
                r for r in refined
                if r.get("viability_score", 0) >= 6 and r.get("grounding_score", 0) >= 5
            ]
            if promising:
                best = promising[0]
                await self._dreamer_escalate_to_manager(best)
            else:
                # Check if ideas were viable but ungrounded
                viable_but_ungrounded = [
                    r for r in refined
                    if r.get("viability_score", 0) >= 6 and r.get("grounding_score", 0) < 5
                ]
                if viable_but_ungrounded:
                    await self.post_to_bots_channel(
                        "dreamer",
                        "🌙 Some visions were exciting but too disconnected from current work. "
                        "Filing them away for when the business evolves..."
                    )
                else:
                    await self.post_to_bots_channel(
                        "dreamer",
                        "🌙 The visions were intriguing but not yet ripe. They drift back into the ether..."
                    )

            await self._record_job_run("dreamer_ideation_cycle", today)

        except Exception as e:
            logger.error(f"Dreamer ideation cycle failed: {e}")
            await self.post_to_bots_channel("dreamer", f"💫 The vision shattered: {str(e)[:100]}")

    @dreamer_ideation_cycle.before_loop
    async def before_dreamer_ideation(self):
        await self.bot.wait_until_ready()

    # Dreamer helper methods (_dreamer_gather_business_context, _dreamer_generate_ideas,
    # _dreamer_dispatch_scout, _dreamer_dispatch_archivist, _dreamer_refine_ideas,
    # _dreamer_escalate_to_manager) are now inherited from DreamerMixin

    # ========================================
    # DREAMER: Schemes-n-Dreams Channel Listener
    # ========================================
    
    @commands.Cog.listener()
    async def on_message_dreamer_schemes(self, message: discord.Message):
        """Listen to #schemes-n-dreams and occasionally escalate ideas wildly."""
        # Only respond in schemes-n-dreams channel
        schemes_channel_id = getattr(config, "SCHEMES_DREAMS_CHANNEL_ID", None)
        if not schemes_channel_id or message.channel.id != schemes_channel_id:
            return
        
        # Ignore bot messages
        if message.author.bot:
            return
        
        # Ignore short messages or commands
        content = message.content.strip()
        if len(content) < 30 or content.startswith("/"):
            return
        
        # Random chance to respond (roughly 1 in 4 messages)
        if random.random() > 0.25:
            return
        
        # Check if we've responded in the last hour (rate limit)
        if not hasattr(self, '_last_schemes_response'):
            self._last_schemes_response = None
        
        now = datetime.now(EASTERN)
        if self._last_schemes_response and (now - self._last_schemes_response).total_seconds() < 3600:
            return
        
        if not self.llm_service:
            return
        
        try:
            # Generate a wild escalation
            prompt = ChatPromptTemplate.from_template("""
Someone posted a business idea. Riff on it — escalate it, find an unexpected angle, 
or ask a question that pushes it further. 

Keep it to 1-3 sentences. Playful, not corporate. End with a question if it fits naturally.

The idea:
"{idea}"

Your take:
""")
            
            response = await self.llm_service.generate(
                prompt,
                {"idea": content[:800]},
                temperature=0.9,
            )
            
            # Post the response
            await message.reply(response[:500])
            self._last_schemes_response = now
            
            logger.info(f"Schemes-n-dreams response to {message.author.name}")
            
        except Exception as e:
            logger.warning(f"Schemes response failed: {e}")

    # Note: on_message_dreamer_schemes is called from the main on_message handler
    # at the top of this class (see @commands.Cog.listener() async def on_message)

    # ========================================
    # RAINMAKER: Business Development & Pipeline
    # ========================================

    @tasks.loop(time=dt_time(hour=8, minute=30, tzinfo=EASTERN))
    async def rainmaker_morning_pipeline(self):
        """Daily 8:30am: Pipeline status report and today's priorities."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        # Skip weekends
        if now.weekday() >= 5:
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("rainmaker_morning_pipeline", today):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        await self.post_to_bots_channel("rainmaker", "☀️ *Good morning! Let's hunt some deals...*")

        try:
            pipeline_stats = await self._rainmaker_get_pipeline_stats()
            stale_leads = await self._rainmaker_get_stale_leads(days=7)
            hot_leads = await self._rainmaker_get_leads_by_status("hot")
            proposals = await self._rainmaker_get_leads_by_status("proposal")
            today_actions = await self._rainmaker_get_leads_with_action_today()
            recent_discoveries = await self._rainmaker_get_recent_discoveries(limit=4)

            lines = ["🎯💰 **Morning Pipeline Report**", ""]

            # Pipeline summary
            active_pursuits = pipeline_stats.get('hot', 0) + pipeline_stats.get('proposal', 0)
            prospects = pipeline_stats.get('warm', 0) + pipeline_stats.get('cold', 0)
            
            lines.append("**🎯 Boutique Strategy Targets**")
            lines.append("• Annual Goal: **3 New Clients** (High Impact)")
            lines.append(f"• Active Potential: **${(pipeline_stats.get('weighted_value', 0) / 1000):.1f}k** (Weighted)") 
            lines.append("")
            
            lines.append(f"**Pipeline Health ({active_pursuits + prospects} active):**")
            if active_pursuits > 0:
                lines.append(f"• 🔥 **Focus:** {pipeline_stats.get('proposal', 0)} Proposals / {pipeline_stats.get('hot', 0)} Hot Leads")
            else:
                lines.append("• 🔥 **Focus:** No active chases — *Time to hunt*")
                
            if prospects > 0:
                lines.append(f"• 🌱 **Nurturing:** {pipeline_stats.get('warm', 0)} Warm / {pipeline_stats.get('cold', 0)} Cold")
             
            if pipeline_stats.get('won_this_month', 0) > 0:
                lines.append(f"• 🏆 **Wins (Month):** {pipeline_stats.get('won_this_month', 0)}")
            
            lines.append("")

            # Recent Discoveries (The Hunt)
            if recent_discoveries:
                lines.append("**🔍 Recent Discoveries (Automated Hunt):**")
                for disc in recent_discoveries:
                    lines.append(f"• [{disc['title'][:40]}...]({disc['url']})")
                lines.append("")
            
            # Today's action items
            if today_actions:
                lines.append("**🎯 Today's Follow-ups:**")
                for lead in today_actions[:5]:
                    owner = f"<@{lead['owner_user_id']}>" if lead.get('owner_user_id') else "Unassigned"
                    lines.append(f"• **#{lead['id']}** {lead['name'][:40]} — {lead.get('next_action', 'Follow up')} {owner}")
                lines.append("")

            # Hot leads needing attention
            if hot_leads:
                lines.append("**🔥 Hot Leads (don't let these cool off!):**")
                for lead in hot_leads[:3]:
                    owner = f"<@{lead['owner_user_id']}>" if lead.get('owner_user_id') else "Unassigned"
                    days_since = self._days_since(lead.get('last_activity'))
                    lines.append(f"• **#{lead['id']}** {lead['name'][:40]} — {days_since}d since last touch {owner}")
                lines.append("")

            # Proposals pending
            if proposals:
                lines.append("**📝 Proposals in Flight:**")
                for lead in proposals[:3]:
                    due = lead.get('proposal_due_date', 'No due date')
                    lines.append(f"• **#{lead['id']}** {lead['name'][:40]} — Due: {due}")
                lines.append("")

            # Stale leads warning
            if stale_leads:
                lines.append(f"**⚠️ {len(stale_leads)} leads untouched for 7+ days** — use `/lead list stale` to review")
                lines.append("")

            lines.append("Commands: `/lead add` | `/lead list` | `/lead update` | `/lead pipeline`")

            await self.post_to_bots_channel("rainmaker", "\n".join(lines))
            await self._record_job_run("rainmaker_morning_pipeline", today)

        except Exception as e:
            logger.error(f"Rainmaker morning pipeline failed: {e}")
            await self.post_to_bots_channel("rainmaker", f"❌ Pipeline report failed: {str(e)[:100]}")

    @rainmaker_morning_pipeline.before_loop
    async def before_rainmaker_morning(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=10, minute=0, tzinfo=EASTERN))
    async def rainmaker_opportunity_hunt(self):
        """
        Daily 10am: Rainmaker actively hunts for RFPs, vendor calls, and procurement opportunities.
        Shows its work: what was searched, what was found, what was assessed, what was elevated.
        """
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        # Skip weekends
        if now.weekday() >= 5:
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("rainmaker_opportunity_hunt", today):
            return

        if not (self.tavily_service and self.tavily_service.is_configured):
            logger.warning("Rainmaker hunt skipped - Tavily not configured")
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        await self.post_to_bots_channel("rainmaker", "🎯 *Time to hunt. Looking for RFPs, vendor calls, and procurement opportunities...*")

        try:
            # Define opportunity-focused search queries
            hunt_queries = await self._generate_rainmaker_hunt_queries(today)
            
            all_findings = []
            search_log = []  # Track what we searched for transparency

            # ========================================
            # DIRECT SOURCE: NC e-Vendor Portal (evp.nc.gov)
            # ========================================
            try:
                search_log.append("🏛️ **NC eVP**: Scraping NC e-Vendor Portal for open solicitations...")
                nc_evp_results = await self._scrape_nc_evp_solicitations()
                
                if nc_evp_results:
                    all_findings.extend(nc_evp_results)
                    search_log.append(f"   → Found {len(nc_evp_results)} relevant NC government RFPs")
                else:
                    search_log.append("   → No matching RFPs on NC eVP today")
                    
            except Exception as e:
                logger.warning(f"NC eVP scrape failed in hunt loop: {e}")
                search_log.append("   → NC eVP scrape encountered an issue")

            # ========================================
            # WEB SEARCH: Tavily for broader opportunities
            # ========================================
            for query_info in hunt_queries:
                query = query_info.get("query", "")
                category = query_info.get("category", "General")
                rationale = query_info.get("rationale", "")
                
                if not query:
                    continue

                search_log.append(f"🔎 **{category}**: {query[:60]}...")
                
                try:
                    results = await self.tavily_service.search(
                        query=query,
                        search_depth="advanced",
                        max_results=8,
                        topic="news",
                        time_range="week",  # Focus on recent opportunities
                    )
                    
                    if results.get("results"):
                        for result in results["results"]:
                            all_findings.append({
                                "title": result.get("title", "No title"),
                                "url": result.get("url", ""),
                                "snippet": result.get("content", "")[:400],
                                "score": result.get("score", 0),
                                "category": category,
                                "query": query,
                            })
                    
                    await asyncio.sleep(1.5)  # Rate limit protection
                    
                except Exception as e:
                    logger.error(f"Rainmaker search failed for '{query}': {e}")
                    continue

            if not all_findings:
                await self.post_to_bots_channel("rainmaker", "📭 No opportunities found today. The hunt continues tomorrow.")
                await self._record_job_run("rainmaker_opportunity_hunt", today)
                return

            # Check for staleness - filter out URLs we've seen before
            fresh_findings = []
            stale_count = 0
            
            async with db.acquire() as conn:
                for finding in all_findings:
                    url = finding.get("url", "").strip()
                    if not url:
                        continue
                    
                    url_hash = hashlib.md5(url.encode()).hexdigest()
                    
                    # Check if we've seen this URL before
                    async with conn.execute(
                        "SELECT id, seen_count, first_seen_date FROM rainmaker_seen_opportunities WHERE url_hash = ?",
                        (url_hash,)
                    ) as cursor:
                        existing = await cursor.fetchone()
                    
                    if existing:
                        # Update seen count but mark as stale
                        await conn.execute(
                            "UPDATE rainmaker_seen_opportunities SET last_seen_date = ?, seen_count = seen_count + 1 WHERE id = ?",
                            (today, existing[0])
                        )
                        stale_count += 1
                    else:
                        # New finding!
                        await conn.execute(
                            """INSERT INTO rainmaker_seen_opportunities 
                               (url, url_hash, title, source_query, first_seen_date, last_seen_date) 
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (url, url_hash, finding.get("title", "")[:200], finding.get("query", "")[:200], today, today)
                        )
                        fresh_findings.append(finding)
                
                await conn.commit()

            # Post search transparency
            search_summary = "\n".join(search_log[:6])  # Cap at 6 lines
            await self.post_to_bots_channel("rainmaker", f"**Searches Performed:**\n{search_summary}")

            # Post findings summary
            total_found = len(all_findings)
            fresh_count = len(fresh_findings)
            
            status_msg = f"📊 **Hunt Results:** Found {total_found} opportunities"
            if stale_count > 0:
                status_msg += f" ({stale_count} already seen, {fresh_count} fresh)"
            await self.post_to_bots_channel("rainmaker", status_msg)

            if not fresh_findings:
                await self.post_to_bots_channel("rainmaker", "🔄 All findings today were repeats. Need new hunting grounds tomorrow.")
                await self._record_job_run("rainmaker_opportunity_hunt", today)
                return

            # Assess fresh findings with LLM
            assessed = await self._rainmaker_assess_opportunities(fresh_findings)
            
            elevated = [a for a in assessed if a.get("verdict") == "elevate"]
            passed = [a for a in assessed if a.get("verdict") == "pass"]

            # Show what was passed on (with reasons)
            if passed:
                passed_summary = []
                for p in passed[:5]:  # Show up to 5
                    passed_summary.append(f"• ~~{p['title'][:50]}~~ — {p.get('reason', 'Not a fit')[:60]}")
                await self.post_to_bots_channel(
                    "rainmaker",
                    f"**❌ Passed On ({len(passed)}):**\n" + "\n".join(passed_summary)
                )

            # Elevate the good ones
            if elevated:
                embed = discord.Embed(
                    title=f"💰 {len(elevated)} Opportunities Worth Pursuing",
                    description="Fresh leads from today's hunt. These cleared the bar.",
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc)
                )
                
                leads_created = []
                for opp in elevated[:3]:  # Max 3 leads per day from hunt
                    # Create lead
                    lead_id = await self._rainmaker_create_lead(
                        title=opp['title'][:100],
                        source='scout',  # Use 'scout' source for consistency
                        description=f"**Why this matters:** {opp.get('reason', 'High potential')}\n\n{opp.get('snippet', '')[:300]}\n\nURL: {opp.get('url', '')}",
                        source_id=opp.get('url', ''),
                        priority='high' if opp.get('confidence', 0) > 0.8 else 'medium'
                    )
                    
                    if lead_id:
                        leads_created.append(lead_id)
                        
                        # Update the seen_opportunities table to mark as elevated
                        url_hash = hashlib.md5(opp.get('url', '').encode()).hexdigest()
                        await db.execute(
                            "UPDATE rainmaker_seen_opportunities SET assessment = 'elevated', assessment_reason = ?, lead_id = ? WHERE url_hash = ?",
                            (opp.get('reason', '')[:200], lead_id, url_hash)
                            )
                        embed.add_field(
                            name=f"🔥 {opp['title'][:60]}",
                            value=f"**Why:** {opp.get('reason', 'Strong fit')[:100]}\n[View Opportunity →]({opp.get('url', '')})\nLead ID: `{lead_id}`",
                            inline=False
                        )

                # Mark passed opportunities in DB
                for opp in passed:
                    url_hash = hashlib.md5(opp.get('url', '').encode()).hexdigest()
                    await db.execute(
                        "UPDATE rainmaker_seen_opportunities SET assessment = 'passed', assessment_reason = ? WHERE url_hash = ?",
                        (opp.get('reason', '')[:200], url_hash)
                        )
                embed.add_field(
                    name="📋 Next Steps",
                    value="• Review with `/lead list`\n• Assign owners with `/lead update`\n• Track progress with `/lead pipeline`",
                    inline=False
                )
                embed.set_footer(text=f"Opportunity Hunt • {len(leads_created)} leads created")
                
                await self.post_to_bots_channel("rainmaker", embed=embed)
            else:
                await self.post_to_bots_channel("rainmaker", "🤔 Found fresh opportunities but none cleared the bar today. Staying picky.")

            await self._record_job_run("rainmaker_opportunity_hunt", today)

        except Exception as e:
            logger.error(f"Rainmaker opportunity hunt failed: {e}")
            await self.post_to_bots_channel("rainmaker", f"❌ Hunt failed: {str(e)[:100]}")

    @rainmaker_opportunity_hunt.before_loop
    async def before_rainmaker_hunt(self):
        await self.bot.wait_until_ready()

    async def _generate_rainmaker_hunt_queries(self, today: str) -> List[Dict[str, str]]:
        """Generate targeted search queries for RFPs, vendor calls, and procurement opportunities.
        
        All queries MUST include geographic focus from org_profile.
        We are a small firm - international and distant US opportunities are not realistic.
        """
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        day_of_week = datetime.now(EASTERN).weekday() # 0=Mon, 6=Sun
        
        # Daily Search Strategy Rotation - ALL queries include NC/Southeast focus
        # This prevents search fatigue and ensures deep dives into different verticals
        strategies = {
            0: { # Monday
                "theme": "Museums & Heritage (Core) - NC/Southeast",
                "focus": "NC/Southeast History museums, Science centers, Children's museums, Heritage sites",
                "base": [
                    {"category": "RFPs", "query": "North Carolina museum interactive exhibit RFP", "rationale": "Mon: NC museum market"},
                    {"category": "RFPs", "query": "Southeast science center exhibit design RFP", "rationale": "Mon: Southeast science centers"}
                ]
            },
            1: { # Tuesday
                "theme": "Nature & Living Collections - NC/Southeast",
                "focus": "NC/Southeast Zoos, Aquariums, Botanical gardens, Park visitor centers",
                "base": [
                    {"category": "RFPs", "query": "North Carolina zoo aquarium interactive RFP", "rationale": "Tue: NC zoo/aquarium market"},
                    {"category": "RFPs", "query": "Southeast botanical garden visitor center upgrade", "rationale": "Tue: Regional nature venues"}
                ] 
            },
            2: { # Wednesday
                "theme": "Public Space & Government - NC Focus",
                "focus": "NC state/municipal procurement, Transit centers, Libraries, Public kiosks",
                "base": [
                    {"category": "Procurement", "query": "North Carolina state procurement interactive kiosk RFP", "rationale": "Wed: NC Gov procurement"},
                    {"category": "Bids", "query": "Raleigh Charlotte library digital signage bid", "rationale": "Wed: NC library market"}
                ]
            },
            3: { # Thursday
                "theme": "Corporate & Universities - Triangle NC",
                "focus": "NC corporate visitor centers, University exhibits, Research Triangle institutions",
                "base": [
                    {"category": "Corporate", "query": "North Carolina corporate visitor center interactive exhibit", "rationale": "Thu: NC corporate market"},
                    {"category": "University", "query": "NC State Duke UNC visitor center exhibit upgrade", "rationale": "Thu: Triangle university market"}
                ]
            },
            4: { # Friday
                "theme": "Construction & New Builds - NC/VA/SC",
                "focus": "New museum construction, Capital projects in NC and adjacent states",
                "base": [
                    {"category": "Construction", "query": "North Carolina museum construction project 2026", "rationale": "Fri: NC new builds"},
                    {"category": "Capital Projects", "query": "Virginia South Carolina visitor center renovation bid", "rationale": "Fri: Adjacent state projects"}
                ]
            }
        }
        
        # Fallback to general (Monday) if weekend
        strategy = strategies.get(day_of_week, strategies[0])
        queries = list(strategy['base'])

        # Load operational context for better query generation
        operational_ctx = ""
        try:
            ops_path = Path(__file__).parent.parent / "prompts" / "operational_context.txt"
            if ops_path.exists():
                operational_ctx = ops_path.read_text(encoding='utf-8')[:1500]
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # Try to get dynamic queries from LLM based on recent context AND daily theme
        try:
            prompt = f"""Generate 3 *specific* search queries to find business opportunities for {org['org_name']}, based in {org['location']}.
            
Today is {today}.
Daily Theme: {strategy['theme']}
Target Verticals: {strategy['focus']}

=== {org['org_name'].upper()} CAPABILITIES (use for query targeting) ===
{operational_ctx}

CRITICAL GEOGRAPHIC CONSTRAINT:
- {org['org_name']} is a SMALL FIRM based in {org['location']}
- ALL queries MUST focus on {org['region']}
- NEVER generate queries for international opportunities (UK, Europe, Asia)
- NEVER generate queries for distant US regions outside our region unless highly specific

INSTRUCTIONS:
1. Create queries that finding *active* RFPs, bids, or lead announcements in NC/Southeast
2. Include geographic terms: "North Carolina", "NC", "Raleigh", "Charlotte", "Southeast", "Virginia", "South Carolina"
3. Focus on our actual capabilities per the context above
4. Avoid generic terms like "marketing", "branding", "data center", "manufacturing"

Return JSON array with objects containing: category, query, rationale.
"""
            response = await self._call_llm(
                system_prompt="You are a business development assistant for a small NC-based firm. Geographic focus is CRITICAL. Return only valid JSON.",
                user_prompt=prompt,
                max_tokens=400
            )
            
            # Try to parse additional queries
            if response:
                import json
                try:
                    # Extract JSON from response
                    json_match = re.search(r'\[.*\]', response, re.DOTALL)
                    if json_match:
                        additional = json.loads(json_match.group())
                        if isinstance(additional, list):
                            queries.extend(additional[:3])
                except (json.JSONDecodeError, AttributeError) as e:
                    logger.debug(f"Failed to parse additional queries: {e}")
        except Exception as e:
            logger.warning(f"Failed to generate LLM queries: {e}")

        return queries

    # _rainmaker_assess_opportunities is now inherited from RainmakerMixin

    @tasks.loop(time=dt_time(hour=14, minute=0, tzinfo=EASTERN))
    async def rainmaker_follow_up_nudges(self):
        """Daily 2pm: Nudge partners about overdue follow-ups."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        if now.weekday() >= 5:
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("rainmaker_follow_up_nudges", today):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            overdue = await self._rainmaker_get_overdue_actions()
            if not overdue:
                await self.post_to_bots_channel("rainmaker", "✅ No overdue follow-ups. Pipeline is moving!")
                await self._record_job_run("rainmaker_follow_up_nudges", today)
                return

            await self.post_to_bots_channel("rainmaker", f"⏰ **{len(overdue)} leads need attention!**")

            # Group by owner
            by_owner: Dict[int, List[Dict]] = {}
            for lead in overdue:
                owner_id = lead.get('owner_user_id') or 0
                if owner_id not in by_owner:
                    by_owner[owner_id] = []
                by_owner[owner_id].append(lead)

            for owner_id, leads in list(by_owner.items())[:3]:  # Max 3 owners per run
                if owner_id:
                    mention = f"<@{owner_id}>"
                    lead_list = "\n".join(f"  • **#{l['id']}** {l['name'][:35]} (action: {l.get('next_action', 'follow up')[:30]})" for l in leads[:3])
                    msg = f"{mention} — these leads are waiting:\n{lead_list}"
                    await self.post_to_bots_channel("rainmaker", msg)

                    # Log nudge activity
                    for lead in leads[:3]:
                        await self._rainmaker_log_activity(lead['id'], 'nudge', "Automated follow-up nudge")

            await self._record_job_run("rainmaker_follow_up_nudges", today)

        except Exception as e:
            logger.error(f"Rainmaker nudges failed: {e}")

    @rainmaker_follow_up_nudges.before_loop
    async def before_rainmaker_nudges(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=10, minute=30, tzinfo=EASTERN))
    async def rainmaker_weekly_cold_review(self):
        """Mondays 10:30am: Review cold leads, suggest promotions or drops."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        if now.weekday() != 0:  # Monday only
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("rainmaker_weekly_cold_review", today):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            cold_leads = await self._rainmaker_get_leads_by_status("cold")
            if not cold_leads:
                await self.post_to_bots_channel("rainmaker", "🧊 No cold leads in pipeline. Time to fill the top of the funnel!")
                await self._record_job_run("rainmaker_weekly_cold_review", today)
                return

            await self.post_to_bots_channel("rainmaker", f"🧊 **Weekly Cold Lead Review** — {len(cold_leads)} leads to evaluate")

            # Categorize cold leads
            ancient = []  # 30+ days old
            stale = []    # 14-30 days
            fresh = []    # < 14 days

            for lead in cold_leads:
                age = self._days_since(lead.get('created_at'))
                if age >= 30:
                    ancient.append(lead)
                elif age >= 14:
                    stale.append(lead)
                else:
                    fresh.append(lead)

            lines = []
            if ancient:
                lines.append("**🪦 Ancient (30+ days) — consider closing or re-engaging:**")
                for lead in ancient[:3]:
                    lines.append(f"• **#{lead['id']}** {lead['name'][:40]} — {self._days_since(lead.get('created_at'))}d old")
                lines.append("")

            if stale:
                lines.append("**😴 Stale (14-30 days) — needs a push:**")
                for lead in stale[:3]:
                    lines.append(f"• **#{lead['id']}** {lead['name'][:40]}")
                lines.append("")

            if fresh:
                lines.append(f"**🌱 Fresh ({len(fresh)} leads) — work these this week!**")
                lines.append("")

            lines.append("💡 Pro tip: Cold leads that sit too long should be marked `dormant` or `lost`")

            await self.post_to_bots_channel("rainmaker", "\n".join(lines))
            await self._record_job_run("rainmaker_weekly_cold_review", today)

        except Exception as e:
            logger.error(f"Rainmaker cold review failed: {e}")

    @rainmaker_weekly_cold_review.before_loop
    async def before_rainmaker_cold_review(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=11, minute=0, tzinfo=EASTERN))
    async def rainmaker_past_client_checkin(self):
        """Wednesdays 11am: Remind about past clients due for re-engagement."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        if now.weekday() != 2:  # Wednesday only
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("rainmaker_past_client_checkin", today):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            due_clients = await self._rainmaker_get_past_clients_due()
            if not due_clients:
                await self.post_to_bots_channel("rainmaker", "📞 No past clients due for check-in this week.")
                await self._record_job_run("rainmaker_past_client_checkin", today)
                return

            await self.post_to_bots_channel("rainmaker", f"📞 **Past Client Check-ins Due** — {len(due_clients)} relationships to nurture")

            lines = ["*The best new business often comes from happy old clients!*", ""]
            for client in due_clients[:5]:
                last_project = client.get('last_project_date', 'Unknown')
                lines.append(f"• **{client['org_name']}** — last project: {last_project}")
                if client.get('contact_name'):
                    lines.append(f"  Contact: {client['contact_name']}")
                lines.append("")

            lines.append("Use `/lead add` with source `past_client` to track re-engagement!")

            await self.post_to_bots_channel("rainmaker", "\n".join(lines))
            await self._record_job_run("rainmaker_past_client_checkin", today)

        except Exception as e:
            logger.error(f"Rainmaker past client checkin failed: {e}")

    @rainmaker_past_client_checkin.before_loop
    async def before_rainmaker_past_client(self):
        await self.bot.wait_until_ready()

    # Rainmaker helper methods (_days_since, _rainmaker_get_recent_discoveries,
    # _rainmaker_get_pipeline_stats, _rainmaker_get_leads_by_status, _rainmaker_get_stale_leads,
    # _rainmaker_get_leads_with_action_today, _rainmaker_get_overdue_actions,
    # _rainmaker_get_past_clients_due, _rainmaker_log_activity, _rainmaker_create_lead)
    # are now inherited from RainmakerMixin

    # ========================================
    # MANAGER: Partner Interface Commands
    # ========================================
    
    @app_commands.command(name="report", description="Get report from operations modules")
    @app_commands.describe(department="Which module: chief, coordinator, scout, or all")
    async def get_report(self, interaction: discord.Interaction, department: str = "all"):
        """Partners can request reports from specific operations modules"""
        await interaction.response.defer()
        
        await self.post_to_bots_channel("manager", f"Report requested by {interaction.user.name} for: {department}")
        
        if department.lower() == "all":
            report = "📊 **Operations Status Report**\n\n"
            report += "🎯 Strategic review runs Sundays 8pm EST\n"
            report += "📋 Daily digest at 8am EST, Async meetings Thursdays 10am EST\n"
            tavily_ready = bool(self.tavily_service and self.tavily_service.is_configured)
            report += f"🔍 Daily search at 7am EST {'✓ Tavily configured' if tavily_ready else '✗ Tavily not available'}\n"
            report += "💭 Ideation cycle runs Tuesdays 2:30pm EST\n"
            report += f"\nBots channel: {'✓ Configured' if self.bots_channel_id else '✗ Not configured'}"
        else:
            dept = self.departments.get(department.lower())
            if dept:
                report = f"{dept['emoji']} **{dept['role']}**\n\n"
                if department.lower() == "scout":
                    tavily_ready = bool(self.tavily_service and self.tavily_service.is_configured)
                    report += f"Tavily: {'✓ Available' if tavily_ready else '✗ Not configured'}"
            else:
                report = f"Unknown module: {department}"
        
        await interaction.followup.send(report, ephemeral=True)

    # ========================================
    # STEWARD: Bot Self-Monitoring & Health
    # ========================================

    @tasks.loop(time=dt_time(hour=18, minute=0, tzinfo=EASTERN))
    async def steward_daily_health_check(self):
        """Daily 6pm: Quick health pulse and engagement check."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        today = now.strftime("%Y-%m-%d")

        if await self._job_already_ran("steward_daily_health_check", today):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            metrics = await self._steward_collect_daily_metrics()
            
            # Quick pulse - only alert if something is concerning
            alerts = []
            auto_fixes = []  # Track corrective actions taken
            
            if metrics.get('questions_today', 0) == 0 and now.weekday() < 5:
                alerts.append("📉 No questions asked today — am I being useful?")
            
            if metrics.get('unhelpful_rate', 0) > 0.3:
                alerts.append(f"⚠️ {int(metrics.get('unhelpful_rate', 0)*100)}% unhelpful responses today")
                # AUTO-FIX: High unhelpful rate → ensure web augmentation is enabled
                try:
                    from core.config_loader import WorkflowConfig
                    wf = WorkflowConfig.load()
                    if not wf.cq_web_chat_enabled:
                        wf.update({"corpus_quality": {"web_augmented_chat": {"enabled": True}}})
                        auto_fixes.append("🔧 Enabled web-augmented chat (high unhelpful rate)")
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
            
            if metrics.get('days_since_ingest', 999) > 7:
                alerts.append(f"🧠 No new knowledge in {metrics.get('days_since_ingest')} days — I'm getting stale!")
                # AUTO-FIX: Stale knowledge → trigger knowledge refresh
                try:
                    refresh_ran = await self._job_already_ran("daily_knowledge_refresh", today)
                    if not refresh_ran:
                        asyncio.create_task(self.daily_knowledge_refresh())
                        auto_fixes.append("🔧 Triggered knowledge refresh (stale)")
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
            
            if metrics.get('open_gaps_without_progress', 0) > 5:
                alerts.append(f"🔓 {metrics.get('open_gaps_without_progress')} knowledge gaps open with no progress")

            if metrics.get('recurring_blind_spots', 0) > 0:
                alerts.append(f"🔁 {metrics.get('recurring_blind_spots')} recurring questions I can't answer well")

            # AUTO-FIX: Check Ollama health and auto-start if down
            try:
                from services.system_tools import SystemTools
                loop = asyncio.get_running_loop()
                ollama_ok = await loop.run_in_executor(None, SystemTools._ollama_server_reachable)
                if not ollama_ok:
                    started = await loop.run_in_executor(None, SystemTools.ensure_ollama_running)
                    if started:
                        auto_fixes.append("🔧 Auto-started Ollama (was down)")
                    else:
                        alerts.append("⚠️ Ollama is down and could not be auto-started")
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            # AUTO-FIX: Run document quality remediation
            try:
                from core.chroma_factory import get_vectorstore
                from services.document_remediation import DocumentRemediationService
                vs = get_vectorstore()
                remediation = DocumentRemediationService(vectorstore=vs, db=db)
                report = await remediation.run_remediation()
                if report.chunks_archived or report.low_performance_sources:
                    auto_fixes.append(report.summary_text())
            except Exception as exc:
                logger.debug("Document remediation skipped: %s", exc)

            # AUTO-FIX: Run feedback learning cycle — retire bad prompt
            # variants, decay quality scores, surface improvement signals
            try:
                from services.feedback_learning_loop import FeedbackLearningLoop
                fll = FeedbackLearningLoop(db)
                await fll.ensure_tables()
                cycle_results = await fll.run_learning_cycle()
                retired = cycle_results.get("retired_variants", 0)
                low_q = cycle_results.get("low_quality_chunks", 0)
                if retired or low_q:
                    auto_fixes.append(
                        f"🔧 Learning cycle: {retired} variant(s) retired, "
                        f"{low_q} low-quality chunk(s) flagged"
                    )
            except Exception as exc:
                logger.debug("Feedback learning cycle skipped: %s", exc)

            # AUTO-FIX: Mine recent conversations for extractable knowledge
            try:
                from services.conversation_miner import mine_recent_conversations
                mine_results = await mine_recent_conversations(
                    db=db, bot=self.bot,
                )
                mined = mine_results.get("proposals_created", mine_results.get("extracts_saved", 0))
                scanned = mine_results.get("sessions_scanned", 0)
                if mined:
                    auto_fixes.append(
                        f"🔧 Conversation mining: {mined} extraction proposal(s) "
                        f"queued for review from {scanned} session(s)"
                    )
            except Exception as exc:
                logger.debug("Conversation mining skipped: %s", exc)

            # Build final message
            message_parts = []
            if alerts:
                message_parts.append("🪴 **Daily Health Pulse**\n" + "\n".join(alerts))
            if auto_fixes:
                message_parts.append("\n**Auto-Corrective Actions Taken:**\n" + "\n".join(auto_fixes))

            if message_parts:
                await self.post_to_bots_channel("steward", "\n".join(message_parts))
            else:
                # Occasional positive reinforcement
                if now.weekday() == 4:  # Friday
                    await self.post_to_bots_channel("steward", "🪴 Systems nominal. Have a good weekend!")

            # Save snapshot
            await self._steward_save_health_snapshot(today, metrics)
            await self._record_job_run("steward_daily_health_check", today)

        except Exception as e:
            logger.error(f"Steward daily health check failed: {e}")

    @steward_daily_health_check.before_loop
    async def before_steward_daily(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=17, minute=0, tzinfo=EASTERN))
    async def steward_weekly_self_assessment(self):
        """Sundays 5pm: Comprehensive weekly self-assessment."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        if now.weekday() != 6:  # Sunday only
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("steward_weekly_self_assessment", today):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            await self.post_to_bots_channel("steward", "🪴 *Conducting weekly self-assessment...*")

            metrics = await self._steward_collect_weekly_metrics()
            blind_spots = await self._steward_find_recurring_blind_spots()
            loop_health = await self._steward_assess_learning_loop()
            feature_usage = await self._steward_analyze_feature_usage()

            lines = ["🪴 **Weekly Self-Assessment Report**", ""]

            # Engagement Summary
            lines.append("**📊 Engagement This Week:**")
            q_asked = metrics.get('questions_asked', 0)
            q_helpful = metrics.get('questions_helpful', 0)
            q_unhelpful = metrics.get('questions_unhelpful', 0)
            helpful_rate = (q_helpful / max(q_asked, 1)) * 100
            lines.append(f"• Questions asked: {q_asked}")
            lines.append(f"• Helpful responses: {q_helpful} ({helpful_rate:.0f}%)")
            lines.append(f"• Unhelpful responses: {q_unhelpful}")
            lines.append(f"• Commands used: {metrics.get('commands_used', 0)}")
            lines.append(f"• Unique users: {metrics.get('unique_users', 0)}")

            # Trend comparison
            prev_questions = metrics.get('prev_week_questions', 0)
            if prev_questions > 0:
                change = ((q_asked - prev_questions) / prev_questions) * 100
                trend = "↑" if change > 0 else "↓" if change < 0 else "→"
                lines.append(f"• Trend: {trend} {abs(change):.0f}% vs last week")
            lines.append("")

            # Learning Loop Health
            lines.append("**🔄 Learning Loop Health:**")
            lines.append(f"• Gaps opened: {loop_health.get('gaps_opened', 0)}")
            lines.append(f"• Gaps closed: {loop_health.get('gaps_closed', 0)}")
            lines.append(f"• Memos written: {loop_health.get('memos_written', 0)}")
            lines.append(f"• Docs ingested: {loop_health.get('docs_ingested', 0)}")
            
            closure_rate = loop_health.get('closure_rate', 0)
            if closure_rate < 0.2:
                lines.append(f"• ⚠️ Loop closure rate: {closure_rate*100:.0f}% — gaps aren't being resolved!")
            elif closure_rate < 0.5:
                lines.append(f"• Loop closure rate: {closure_rate*100:.0f}% — room for improvement")
            else:
                lines.append(f"• ✅ Loop closure rate: {closure_rate*100:.0f}%")
            lines.append("")

            # Recurring Blind Spots
            if blind_spots:
                lines.append("**🔁 Recurring Blind Spots** (questions I can't answer well):")
                for spot in blind_spots[:3]:
                    lines.append(f"• \"{spot['pattern'][:50]}...\" — asked {spot['count']}x")
                lines.append("")

            # Feature Usage
            if feature_usage:
                lines.append("**🛠️ Feature Health:**")
                active = [f for f in feature_usage if f['uses'] > 0]
                dormant = [f for f in feature_usage if f['uses'] == 0]
                
                if active:
                    top_3 = sorted(active, key=lambda x: x['uses'], reverse=True)[:3]
                    lines.append(f"• Most used: {', '.join(f['name'] for f in top_3)}")
                
                if dormant:
                    lines.append(f"• ⚠️ Unused this week: {', '.join(f['name'] for f in dormant[:3])}")
                lines.append("")

            # Recommendations
            recs = await self._steward_generate_recommendations(metrics, blind_spots, loop_health, feature_usage)
            if recs:
                lines.append("**💡 Recommendations:**")
                for i, rec in enumerate(recs[:3], 1):
                    lines.append(f"{i}. {rec}")
                lines.append("")

            # Partner prompt
            lines.append("*Partners: Use `/feedback` to help me improve, or `/gaps add` to log things I should learn.*")

            await self.post_to_bots_channel("steward", "\n".join(lines))
            await self._record_job_run("steward_weekly_self_assessment", today)

        except Exception as e:
            logger.error(f"Steward weekly assessment failed: {e}")
            await self.post_to_bots_channel("steward", f"❌ Self-assessment failed: {str(e)[:100]}")

    @steward_weekly_self_assessment.before_loop
    async def before_steward_weekly(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=9, minute=30, tzinfo=EASTERN))
    async def steward_learning_loop_audit(self):
        """Wednesdays 9:30am: Audit learning loop progress and nudge stale gaps."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        if now.weekday() != 2:  # Wednesday only
            return

        today = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("steward_learning_loop_audit", today):
            return

        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            stale_gaps = await self._steward_find_stale_gaps(days=14)
            unverified_memos = await self._steward_find_unverified_improvements()

            if not stale_gaps and not unverified_memos:
                await self.post_to_bots_channel("steward", "🪴 Learning loop audit: All systems flowing! ✅")
                await self._record_job_run("steward_learning_loop_audit", today)
                return

            lines = ["🪴 **Learning Loop Audit**", ""]

            if stale_gaps:
                lines.append(f"**🔓 {len(stale_gaps)} Stale Gaps** (no progress in 14+ days):")
                for gap in stale_gaps[:5]:
                    lines.append(f"• **#{gap['id']}** {gap['topic'][:40]} — {gap.get('days_stale', '?')}d stale")
                    # Log staleness event
                    await self._steward_log_learning_event(gap['id'], 'gap_stale', 'Flagged as stale by Steward audit')
                lines.append("")
                lines.append("👉 **Archivist**: Time to investigate these or close them!")
                lines.append("")

            if unverified_memos:
                lines.append(f"**📝 {len(unverified_memos)} Unverified Improvements:**")
                lines.append("Memos were written but we haven't confirmed they actually help.")
                lines.append("Try asking related questions and see if responses improved!")
                lines.append("")

            await self.post_to_bots_channel("steward", "\n".join(lines))
            await self._record_job_run("steward_learning_loop_audit", today)

        except Exception as e:
            logger.error(f"Steward learning loop audit failed: {e}")

    @steward_learning_loop_audit.before_loop
    async def before_steward_audit(self):
        await self.bot.wait_until_ready()

    # Steward helper methods (_steward_collect_daily_metrics, _steward_collect_weekly_metrics,
    # _steward_assess_learning_loop, _steward_find_recurring_blind_spots, _steward_analyze_feature_usage,
    # _steward_find_stale_gaps, _steward_find_unverified_improvements, _steward_generate_recommendations,
    # _steward_save_health_snapshot, _steward_log_learning_event, _steward_log_command_usage,
    # _steward_log_question, _steward_flag_blind_spot, _steward_update_question_feedback)
    # are now inherited from StewardMixin
    
    # ========================================
async def setup(bot):
    await bot.add_cog(AutonomousOps(bot))
