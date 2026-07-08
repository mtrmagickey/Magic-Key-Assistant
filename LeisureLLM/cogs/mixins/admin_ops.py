"""Developer/admin commands — manual job triggers, ingest,
persona hiring/firing, status, and debug utilities.

Extracted from AutonomousOps.py to reduce god-object size."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path

import discord
import docsprep
from discord import app_commands
from discord.ext import commands

import config
from cogs.ingest_metadata import run_ingest
from cogs.mixins._utils import _is_owner_interaction
from cogs.ui.autonomous_ui import FirePersonaView, HirePersonaModal

logger = logging.getLogger(__name__)


class AdminOpsMixin:
    """Mixin: Developer/admin commands — manual job triggers, ingest,"""

    # DEVELOPER/DEBUG COMMANDS
    # ========================================
    
    @app_commands.command(name="admin_run", description="[Admin] Manually trigger background jobs")
    @app_commands.describe(job="The background job to execute")
    @app_commands.choices(job=[
        app_commands.Choice(name="Scout Search (Daily Opportunity Hunt)", value="scout"),
        app_commands.Choice(name="Dreamer Cycle (Ideation)", value="dreamer"),
        app_commands.Choice(name="Strategic Review (Weekly)", value="strategic"),
        app_commands.Choice(name="Rainmaker Hunt (Lead Gen)", value="rainmaker"),
        app_commands.Choice(name="Async Meeting (Weekly Flow)", value="async_meeting"),
        app_commands.Choice(name="Persona Weekly Digest", value="digest"),
    ])
    @app_commands.check(_is_owner_interaction)
    async def admin_run_job(self, interaction: discord.Interaction, job: str):
        """Manually trigger background jobs."""
        await interaction.response.defer(ephemeral=True)
        
        await self.post_to_bots_channel("manager", f"🛠️ Manual job trigger: **{job}** by {interaction.user.name}")
        
        try:
            if job == "scout":
                await self.daily_scout_search()
            elif job == "dreamer":
                await self.dreamer_ideation_cycle()
            elif job == "strategic":
                await self.weekly_strategic_review()
            elif job == "rainmaker":
                await self.rainmaker_opportunity_hunt()
            elif job == "async_meeting":
                await self._run_async_meeting()
            elif job == "digest":
                await self.weekly_persona_meeting_digest()
            
            await interaction.followup.send(f"✅ Job **{job}** triggered successfully.", ephemeral=True)
        except Exception as e:
            logger.error(f"Manual job trigger failed: {e}")
            await interaction.followup.send(f"❌ Job failed: {e}", ephemeral=True)

    @app_commands.command(name="ingest", description="[Admin] Manually trigger document ingestion logic")
    @app_commands.check(_is_owner_interaction)
    async def ingest(self, interaction: discord.Interaction):
        """Manually trigger the document ingestion pipeline (Prep -> Ingest)."""
        await interaction.response.defer(ephemeral=False)
        await interaction.followup.send("📚 **Ingestion Protocol Initiated**\n1️⃣ Prepping docs (splitting logs)...\n2️⃣ Updating vector database (embedding new content)...")

        try:
            # Step 1: Prep
            prep_count = 0
            try:
                # We need to use the path from config
                # docsprep default is hardcoded but we can pass it if we update docsprep
                # Let's trust docsprep's default for now or pass config.directory_path
                prep_count = docsprep.run_prep(Path(config.directory_path))
            except Exception as e:
                logger.error(f"Docsprep failed: {e}")
                await interaction.followup.send(f"⚠️ Docsprep failed: {e}")

            # Step 2: Ingest
            # run_ingest returns db, stats, persist_dir
            # We run it in executor to avoid blocking event loop?
            # It's synchronous code involving heavy IO/CPU.
            # Ideally: await self.bot.loop.run_in_executor(None, run_ingest)
            # But run_ingest might not be pickleable or context dependent.
            # Let's try running it directly for now (it might block for 10-20s).
            # If it blocks too long, Discord will timeout. But we already deferred.
            # Only heartbeat might fail.
            
            def _blocking_ingest():
                return run_ingest()

            # Using executor to prevent heartbeat timeout
            _, stats, _ = await self.bot.loop.run_in_executor(None, _blocking_ingest)
            
            summary = (
                f"✅ **Ingestion Complete**\n"
                f"• Prep: {prep_count} files split\n"
                f"• Ingest: {stats.get('added_files', 0)} added, {stats.get('updated_files', 0)} updated\n"
                f"• Vectors: {stats.get('chunks_written', 0)} chunks written"
            )
            await interaction.followup.send(summary)

        except Exception as e:
            logger.error(f"Ingest command failed: {e}")
            await interaction.followup.send(f"❌ Ingestion failed: {str(e)[:100]}")

    @app_commands.command(name="hire", description="Hire a custom persona to join virtual staff meetings")
    async def hire_persona(self, interaction: discord.Interaction):
        """Open a modal to design and hire a new custom persona."""
        if not self._personas_enabled:
            await interaction.response.send_message("\u26a0\ufe0f Accelerators are not enabled. Turn them on in the admin console under **Organisation \u2192 Workflow Modules \u2192 Operating Mode**.", ephemeral=True)
            return
        await interaction.response.send_modal(HirePersonaModal(bot=self.bot))

    @app_commands.command(name="fire", description="Remove a custom persona from the team")
    async def fire_persona(self, interaction: discord.Interaction):
        """Show a dropdown of custom personas that can be fired."""
        if not self._personas_enabled:
            await interaction.response.send_message("\u26a0\ufe0f Accelerators are not enabled. Turn them on in the admin console under **Organisation \u2192 Workflow Modules \u2192 Operating Mode**.", ephemeral=True)
            return
        db = getattr(self.bot, "db", None)
        if not db:
            await interaction.response.send_message("❌ Database unavailable", ephemeral=True)
            return
        
        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, key, name, emoji, personality, concerns, project_context, created_by_username
                    FROM custom_personas
                    WHERE active = 1
                    ORDER BY created_at DESC
                    """
            ) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                await interaction.response.send_message(
                    "📋 No custom personas on staff. Use `/hire` to add one!",
                    ephemeral=True
                )
                return
            
            personas = [
                {
                    'id': row[0],
                    'key': row[1],
                    'name': row[2],
                    'emoji': row[3],
                    'personality': row[4],
                    'concerns': row[5],
                    'project_context': row[6],
                    'created_by_username': row[7],
                }
                for row in rows
            ]
            
            view = FirePersonaView(personas, self.bot)
            await interaction.response.send_message(
                "👋 Select a custom persona to let go:",
                view=view,
                ephemeral=True
            )
            
        except Exception as e:
            logger.warning(f"Failed to list custom personas: {e}")
            await interaction.response.send_message(f"❌ Error: {str(e)[:100]}", ephemeral=True)

    @app_commands.command(name="staff", description="List all virtual staff (built-in + custom personas)")
    async def list_staff(self, interaction: discord.Interaction):
        """Show all personas available for meetings."""
        if not self._personas_enabled:
            await interaction.response.send_message("\u26a0\ufe0f Accelerators are not enabled. Turn them on in the admin console under **Organisation \u2192 Workflow Modules \u2192 Operating Mode**.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        
        # Built-in personas
        builtin = [
            ("📚", "Librarian", "methodical, knowledge-focused"),
            ("📋", "Coordinator", "practical, deadline-aware"),
            ("🔍", "Scout", "curious, opportunity-driven"),
            ("💭", "Dreamer", "imaginative, future-focused"),
            ("🎯💰", "Rainmaker", "results-oriented, revenue-conscious"),
            ("🪴", "Steward", "reflective, systems-thinking"),
            ("🐑", "Shepherd", "nurturing, morale-aware"),
        ]
        
        lines = ["## 👥 Virtual Staff\n", "**Built-in Team:**"]
        for emoji, name, traits in builtin:
            lines.append(f"• {emoji} **{name}** — {traits}")
        
        # Custom personas
        db = getattr(self.bot, "db", None)
        if db:
            try:
                async with db.acquire() as conn, conn.execute(
                    """
                        SELECT emoji, name, personality, project_context, created_by_username
                        FROM custom_personas
                        WHERE active = 1
                        ORDER BY created_at DESC
                        """
                ) as cursor:
                    customs = await cursor.fetchall()
                
                if customs:
                    lines.append("\n**Custom Hires:**")
                    for row in customs:
                        emoji, name, personality, project, creator = row
                        project_note = f" [{project}]" if project else ""
                        lines.append(f"• {emoji} **{name}**{project_note} — {personality} *(hired by {creator})*")
                else:
                    lines.append("\n*No custom personas yet. Use `/hire` to add one!*")
                    
            except Exception as e:
                lines.append(f"\n*Could not load custom personas: {str(e)[:50]}*")
        
        lines.append("\n*All personas may be selected for hourly virtual meetings.*")
        
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    
    @app_commands.command(name="dev_status", description="[DEV] Check all department configurations")
    @app_commands.check(_is_owner_interaction)
    async def dev_status(self, interaction: discord.Interaction):
        """Check configuration status of all systems"""
        await interaction.response.defer(ephemeral=True)
        
        status = "🔧 **Development Status Check**\n\n"
        
        # Check bots channel
        bots_channel = self.bot.get_channel(self.bots_channel_id) if self.bots_channel_id else None
        if bots_channel:
            status += f"**Bots Channel:** ✓ {bots_channel.mention} ({bots_channel.id})\n"
        else:
            status += "**Bots Channel:** ✗ Not configured\n"
        
        # Check partners channel
        partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if partners_channel:
            status += f"**Partners Channel:** ✓ {partners_channel.mention} ({partners_channel.id})\n"
        else:
            status += "**Partners Channel:** ✗ Not found (looking for weekly-meeting-threads)\n"
        
        # Check Tavily
        tavily_ready = bool(self.tavily_service and self.tavily_service.is_configured)
        status += f"**Tavily API:** {'✓ Configured' if tavily_ready else '✗ Not available (TAVILY_API_KEY not set)'}\n"
        
        # Check OpenAI
        status += f"**OpenAI API:** {'✓ Configured' if self.llm_service else '✗ Not configured'}\n"
        
        # Check scheduled tasks
        status += "\n**Scheduled Tasks:**\n"
        status += f"• Daily Digest: {'✓ Running' if self.daily_digest.is_running() else '✗ Not running'}\n"
        status += f"• Async Meeting: {'✓ Running' if self.thursday_async_meeting.is_running() else '✗ Not running'}\n"
        status += f"• Strategic Review: {'✓ Running' if self.weekly_strategic_review.is_running() else '✗ Not running'}\n"
        status += f"• Scout Search: {'✓ Running' if self.daily_scout_search.is_running() else '✗ Not running'}\n"
        status += f"• Scout Crawl: {'✓ Running' if self.scout_background_crawl.is_running() else '✗ Not running'}\n"
        status += f"• Dreamer Ideation: {'✓ Running' if self.dreamer_ideation_cycle.is_running() else '✗ Not running'}\n"
        status += f"• Rainmaker Pipeline: {'✓ Running' if self.rainmaker_morning_pipeline.is_running() else '✗ Not running'}\n"
        status += f"• Rainmaker Hunt: {'✓ Running' if self.rainmaker_opportunity_hunt.is_running() else '✗ Not running'}\n"
        status += f"• Rainmaker Nudges: {'✓ Running' if self.rainmaker_follow_up_nudges.is_running() else '✗ Not running'}\n"
        
        # Check database
        status += "\n**Database:**\n"
        if hasattr(self.bot, 'db') and self.bot.db:
            status += f"• Connection: {'✓ Database tracking enabled' if self.use_db_tracking else '⚠️ Using file fallback'}\n"
        else:
            status += "• Connection: ✗ Database not configured (file-based tracking)\n"
        
        # Next run times (approximate)
        now = datetime.now(timezone.utc)
        status += "\n**Next Scheduled Runs (EST):**\n"
        status += "• Scout: Daily at 7:00 AM\n"
        status += "• Scout Crawl: Weekdays 9:00 AM–5:00 PM (every ~45 min)\n"
        status += "• Rainmaker Pipeline: Weekdays at 8:30 AM\n"
        status += "• Rainmaker Hunt: Weekdays at 10:00 AM\n"
        status += "• Digest: Daily at 8:00 AM\n"
        status += "• Async Meeting: Thursdays at 10:00 AM\n"
        status += "• Rainmaker Cold Review: Mondays at 10:30 AM\n"
        status += "• Rainmaker Past Clients: Wednesdays at 11:00 AM\n"
        status += "• Rainmaker Nudges: Weekdays at 2:00 PM\n"
        status += "• Dreamer: Tuesdays at 2:30 PM\n"
        status += "• Strategic Review: Sundays at 8:00 PM\n"
        
        status += "\n**Test Commands:**\n"
        status += "• `/test_scout` - Test web search\n"
        status += "• `/test_digest` - Test daily digest\n"
        status += "• `/test_strategic` - Test strategic review\n"
        status += "• `/test_dreamer` - Test Dreamer ideation cycle\n"
        status += "• `/test_rainmaker_hunt` - Test Rainmaker opportunity hunt\n"
        status += "• `/test_persona_meeting` - Test hourly persona meeting\n"
        status += "• `/run_async_meeting` - Test async meeting\n"
        status += "• `/db_status` - Database statistics\n"
        
        await interaction.followup.send(status, ephemeral=True)
    
    @app_commands.command(name="set_bots_channel", description="Configure the #bots work channel")
    @app_commands.describe(channel="Channel where departments post their work")
    @app_commands.check(_is_owner_interaction)
    async def set_bots_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the channel for department back-of-house communication"""
        self.bots_channel_id = channel.id
        await interaction.response.send_message(f"✅ Bots channel set to {channel.mention}", ephemeral=True)
        await self.post_to_bots_channel("manager", f"Bots channel configured by {interaction.user.name}")
    
    @app_commands.command(name="db_status", description="View database statistics and recent activity")
    @app_commands.check(_is_owner_interaction)
    async def db_status_command(self, interaction: discord.Interaction):
        """Display database status and statistics"""
        await interaction.response.defer(ephemeral=True)
        
        if not (hasattr(self.bot, 'db') and self.bot.db):
            await interaction.followup.send("❌ Database not configured. Bot is using file-based tracking.", ephemeral=True)
            return
        
        try:
            # Get pipeline stats
            pipeline_stats = await self.bot.db.get_pipeline_stats()
            task_stats = await self.bot.db.get_task_stats()
            recent_jobs = await self.bot.db.get_recent_job_runs(limit=5)
            
            status = "📊 **Database Status**\n\n"
            
            # Pipeline
            status += "**Opportunity Pipeline:**\n"
            status += f"• New: {pipeline_stats.get('new_count', 0)}\n"
            status += f"• Qualifying: {pipeline_stats.get('qualifying_count', 0)}\n"
            status += f"• Pursuing: {pipeline_stats.get('pursuing_count', 0)}\n"
            status += f"• Proposals Sent: {pipeline_stats.get('proposal_count', 0)}\n"
            status += f"• Won: {pipeline_stats.get('won_count', 0)} | Lost: {pipeline_stats.get('lost_count', 0)}\n"
            
            avg_score = pipeline_stats.get('avg_fit_score')
            if avg_score:
                status += f"• Avg Fit Score: {float(avg_score):.1f}/100\n"
            
            overdue = pipeline_stats.get('overdue_count', 0)
            if overdue > 0:
                status += f"• ⚠️ Overdue Deadlines: {overdue}\n"
            
            # Tasks
            status += "\n**Tasks:**\n"
            status += f"• To Do: {task_stats.get('todo_count', 0)}\n"
            status += f"• In Progress: {task_stats.get('in_progress_count', 0)}\n"
            status += f"• Blocked: {task_stats.get('blocked_count', 0)}\n"
            status += f"• Done: {task_stats.get('done_count', 0)}\n"
            
            task_overdue = task_stats.get('overdue_count', 0)
            if task_overdue > 0:
                status += f"• ⚠️ Overdue: {task_overdue}\n"
            
            # Recent job runs
            if recent_jobs:
                status += "\n**Recent Job Runs:**\n"
                for job in recent_jobs[:5]:
                    emoji = "✅" if job['status'] == 'completed' else "❌" if job['status'] == 'failed' else "⏳"
                    status += f"• {emoji} {job['job_name']} - {job['run_date']}\n"
            
            await interaction.followup.send(status, ephemeral=True)
            
        except Exception as e:
            logger.error(f"db_status command failed: {e}")
            await interaction.followup.send(f"❌ Error querying database: {str(e)}", ephemeral=True)
    
    # ========================================
