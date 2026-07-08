"""Meeting agenda management, accomplishment tracking,
leaderboard, and meeting notes parsing.

Extracted from AutonomousOps.py to reduce god-object size."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from cogs.mixins._utils import _is_owner_interaction, is_partner
from cogs.ui.autonomous_ui import DidSomethingModal

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


class AgendaOpsMixin:
    """Mixin: Meeting agenda management, accomplishment tracking,"""

    # MEETING AGENDA COMMANDS
    # ========================================

    @app_commands.command(name="agenda", description="[Partners] Add a topic for the AI personas to discuss")
    @app_commands.describe(
        topic="What should the personas discuss? Be specific.",
        context="Optional: Why is this important? What decision needs to be made?",
        priority="How urgent is this topic?",
        expires_days="Days until this expires if not discussed (default: 7)"
    )
    @app_commands.choices(priority=[
        app_commands.Choice(name="🔴 Urgent - discuss ASAP", value="urgent"),
        app_commands.Choice(name="🟠 High - discuss soon", value="high"),
        app_commands.Choice(name="🟢 Normal", value="normal"),
        app_commands.Choice(name="⚪ Low - when there's time", value="low"),
    ])
    async def add_agenda_item(self, interaction: discord.Interaction, topic: str, context: str = "", priority: str = "normal", expires_days: int = 7):
        if not self._personas_enabled:
            await interaction.response.send_message("\u26a0\ufe0f Accelerators are not enabled. Turn them on in the admin console under **Organisation \u2192 Workflow Modules \u2192 Operating Mode**.", ephemeral=True)
            return
    async def agenda_add(
        self, 
        interaction: discord.Interaction, 
        topic: str,
        context: str = None,
        priority: str = "normal",
        expires_days: int = 30
    ):
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        db = getattr(self.bot, "db", None)
        if not db:
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        
        # Validate
        topic = topic.strip()[:500]
        if len(topic) < 10:
            await interaction.followup.send("❌ Topic too short. Be specific about what to discuss.", ephemeral=True)
            return
        
        context = (context or "").strip()[:1000]
        expires_days = max(1, min(expires_days, 90))
        expires_at = (datetime.now(EASTERN) + timedelta(days=expires_days)).strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO meeting_agenda_items 
                    (topic, context, submitted_by_user_id, submitted_by_username, priority, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (topic, context, interaction.user.id, str(interaction.user), priority, expires_at)
                )
                await conn.commit()
                
                # Get count of pending items
                async with conn.execute(
                    "SELECT COUNT(*) FROM meeting_agenda_items WHERE status = 'pending' AND datetime(expires_at) > datetime('now')"
                ) as cursor:
                    row = await cursor.fetchone()
                    pending_count = row[0] if row else 0
            
            priority_emoji = {"urgent": "🔴", "high": "🟠", "normal": "🟢", "low": "⚪"}.get(priority, "🟢")
            await interaction.followup.send(
                f"✅ **Topic added to persona meeting agenda**\n\n"
                f"{priority_emoji} **{topic}**\n"
                f"{'> ' + context if context else ''}\n\n"
                f"Expires in {expires_days} days if not discussed.\n"
                f"📋 {pending_count} topics now in queue.",
                ephemeral=True
            )
            
            # If urgent, post to bots channel
            if priority == "urgent":
                await self.post_to_bots_channel(
                    "coordinator",
                    f"🔴 **Urgent agenda item added by {interaction.user.display_name}**\n"
                    f"Topic: {topic}\n"
                    f"{'Context: ' + context if context else ''}\n"
                    f"Will be prioritized in the next persona meeting."
                )
                
        except Exception as e:
            logger.error(f"Failed to add agenda item: {e}")
            await interaction.followup.send(f"❌ Failed to add topic: {e}", ephemeral=True)

    @app_commands.command(name="agenda_list", description="[Partners] View pending discussion topics")
    @app_commands.describe(show_all="Include discussed and expired items")
    async def agenda_list(self, interaction: discord.Interaction, show_all: bool = False):
        if not self._personas_enabled:
            await interaction.response.send_message("\u26a0\ufe0f Accelerators are not enabled. Turn them on in the admin console under **Organisation \u2192 Workflow Modules \u2192 Operating Mode**.", ephemeral=True)
            return
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        
        db = getattr(self.bot, "db", None)
        if not db:
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        
        try:
            async with db.acquire() as conn:
                if show_all:
                    query = """
                        SELECT id, topic, context, submitted_by_username, priority, expires_at, status, used_in_meeting_date
                        FROM meeting_agenda_items
                        ORDER BY 
                            CASE status WHEN 'pending' THEN 0 WHEN 'discussed' THEN 1 ELSE 2 END,
                            CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                            datetime(created_at) DESC
                        LIMIT 20
                    """
                else:
                    query = """
                        SELECT id, topic, context, submitted_by_username, priority, expires_at, status, used_in_meeting_date
                        FROM meeting_agenda_items
                        WHERE status IN ('pending', 'discussed') AND datetime(expires_at) > datetime('now')
                        ORDER BY 
                            CASE status WHEN 'pending' THEN 0 WHEN 'discussed' THEN 1 ELSE 2 END,
                            CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                            datetime(created_at) DESC
                        LIMIT 20
                    """
                
                async with conn.execute(query) as cursor:
                    rows = await cursor.fetchall()
            
            if not rows:
                await interaction.followup.send("📋 No pending agenda items. Use `/agenda` to add topics for personas to discuss.", ephemeral=True)
                return
            
            priority_emoji = {"urgent": "🔴", "high": "🟠", "normal": "🟢", "low": "⚪"}
            status_emoji = {"pending": "⏳", "discussed": "✅", "expired": "⌛"}
            
            lines = ["**📋 Meeting Agenda Queue**\n"]
            for row in rows:
                item_id, topic, context, submitted_by, priority, expires_at, status, used_date = row
                p_emoji = priority_emoji.get(priority, "🟢")
                s_emoji = status_emoji.get(status, "⏳")
                
                topic_short = topic[:100] + "..." if len(topic) > 100 else topic
                line = f"{s_emoji} {p_emoji} **#{item_id}** {topic_short}"
                if status == "discussed" and used_date:
                    line += f" *(discussed {used_date})*"
                elif status == "pending":
                    line += f" — expires {expires_at[:10]}"
                lines.append(line)
            
            await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)
            
        except Exception as e:
            logger.error(f"Failed to list agenda items: {e}")
            await interaction.followup.send(f"❌ Failed to list topics: {e}", ephemeral=True)

    @app_commands.command(name="agenda_remove", description="[Partners] Remove a pending agenda item")
    @app_commands.describe(item_id="The # of the item to remove")
    async def agenda_remove(self, interaction: discord.Interaction, item_id: int):
        if not self._personas_enabled:
            await interaction.response.send_message("\u26a0\ufe0f Accelerators are not enabled. Turn them on in the admin console under **Organisation \u2192 Workflow Modules \u2192 Operating Mode**.", ephemeral=True)
            return
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        
        db = getattr(self.bot, "db", None)
        if not db:
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        
        try:
            async with db.acquire() as conn:
                # Check ownership (only submitter or owner can delete)
                async with conn.execute(
                    "SELECT submitted_by_user_id, topic FROM meeting_agenda_items WHERE id = ?",
                    (item_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                
                if not row:
                    await interaction.followup.send(f"❌ Item #{item_id} not found", ephemeral=True)
                    return
                
                owner_id, topic = row
                is_owner = await self.bot.is_owner(interaction.user)
                if owner_id != interaction.user.id and not is_owner:
                    await interaction.followup.send("❌ You can only remove your own agenda items", ephemeral=True)
                    return
                
                await conn.execute("DELETE FROM meeting_agenda_items WHERE id = ?", (item_id,))
                await conn.commit()
            
            await interaction.followup.send(f"✅ Removed agenda item #{item_id}: {topic[:80]}...", ephemeral=True)
            
        except Exception as e:
            logger.error(f"Failed to remove agenda item: {e}")
            await interaction.followup.send(f"❌ Failed to remove: {e}", ephemeral=True)

    @app_commands.command(name="did", description="[Partners] Log a quick win/update for the next meeting agenda")
    @app_commands.describe(category="Optional label like email, bugfix, shipment, ops")
    async def did(self, interaction: discord.Interaction, category: str = "update"):
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return

        await interaction.response.send_modal(DidSomethingModal(bot=self.bot, category=category))

    @app_commands.command(name="did_list", description="[Owner] List recent /did updates (audit)")
    @app_commands.check(_is_owner_interaction)
    @app_commands.describe(limit="Max rows (1-50)", include_used="Include updates already surfaced in a meeting")
    async def did_list(self, interaction: discord.Interaction, limit: int = 20, include_used: bool = False):
        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return

        limit = max(1, min(int(limit or 20), 50))

        try:
            async with self.bot.db.acquire() as conn:
                if include_used:
                    query = (
                        "SELECT id, partner_user_id, partner_username, category, details, link, created_at, used_at, used_in_meeting_date "
                        "FROM partner_updates ORDER BY datetime(created_at) DESC LIMIT ?"
                    )
                    args = (limit,)
                else:
                    query = (
                        "SELECT id, partner_user_id, partner_username, category, details, link, created_at, used_at, used_in_meeting_date "
                        "FROM partner_updates WHERE used_at IS NULL ORDER BY datetime(created_at) DESC LIMIT ?"
                    )
                    args = (limit,)

                async with conn.execute(query, args) as cursor:
                    rows = await cursor.fetchall()

            if not rows:
                await interaction.followup.send("No updates found.", ephemeral=True)
                return

            lines: List[str] = []
            lines.append(f"Recent partner updates (showing {len(rows)}):")
            lines.append("")
            for r in rows:
                update_id, partner_user_id, partner_username, category, details, link, created_at, used_at, used_in_meeting_date = r
                who = f"<@{int(partner_user_id)}>" if partner_user_id is not None else (partner_username or "Partner")
                cat = (category or "update").strip()
                prefix = f"[{cat}] " if cat and cat != "update" else ""
                used_flag = "✅ used" if used_at else "⏳ pending"
                meeting_flag = f" ({used_in_meeting_date})" if used_in_meeting_date else ""
                msg = (details or "").strip().replace("\n", " ")
                link_txt = f" | {link}" if link else ""
                lines.append(f"- #{int(update_id)} {who}: {prefix}{msg[:180]}{link_txt} — {used_flag}{meeting_flag} — {created_at}")

            await interaction.followup.send("\n".join(lines)[:1950], ephemeral=True)
        except Exception as e:
            logger.warning(f"did_list failed: {e}")
            await interaction.followup.send("❌ Failed to list updates", ephemeral=True)

    @app_commands.command(name="leaderboard", description="View the partner points leaderboard")
    @app_commands.describe(period="Time period to show")
    @app_commands.choices(period=[
        app_commands.Choice(name="This Week", value="week"),
        app_commands.Choice(name="This Month", value="month"),
        app_commands.Choice(name="All Time", value="all"),
    ])
    async def leaderboard(self, interaction: discord.Interaction, period: str = "all"):
        """Show partner points leaderboard."""
        await interaction.response.defer(ephemeral=False)

        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database unavailable")
            return

        try:
            # Determine date filter
            if period == "week":
                # Start of current week (Monday)
                today = datetime.now(EASTERN).date()
                week_start = today - timedelta(days=today.weekday())
                date_filter = f"AND date(created_at) >= date('{week_start}')"
                period_label = f"Week of {week_start.strftime('%b %d')}"
            elif period == "month":
                # Start of current month
                today = datetime.now(EASTERN).date()
                month_start = today.replace(day=1)
                date_filter = f"AND date(created_at) >= date('{month_start}')"
                period_label = today.strftime("%B %Y")
            else:
                date_filter = ""
                period_label = "All Time"

            async with self.bot.db.acquire() as conn:
                # Get leaderboard
                async with conn.execute(
                    f"""
                    SELECT partner_username, SUM(points) as pts, COUNT(*) as actions
                    FROM partner_point_events
                    WHERE 1=1 {date_filter}
                    GROUP BY partner_user_id, partner_username
                    ORDER BY pts DESC
                    LIMIT 10
                    """
                ) as cursor:
                    leaderboard = [dict(r) for r in (await cursor.fetchall() or [])]

                # Get team total
                async with conn.execute(
                    f"""
                    SELECT COALESCE(SUM(points), 0) as total
                    FROM partner_point_events
                    WHERE 1=1 {date_filter}
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    team_total = int(row[0] or 0) if row else 0

            # Build embed
            embed = discord.Embed(
                title=f"🏆 Partner Leaderboard — {period_label}",
                color=discord.Color.gold()
            )

            if leaderboard:
                lines = []
                medals = ["🥇", "🥈", "🥉"]
                for i, entry in enumerate(leaderboard):
                    name = entry.get("partner_username") or "Partner"
                    pts = int(entry.get("pts") or 0)
                    actions = int(entry.get("actions") or 0)
                    medal = medals[i] if i < 3 else f"{i+1}."
                    lines.append(f"{medal} **{name}**: {pts} pts ({actions} actions)")
                
                embed.description = "\n".join(lines)
                embed.set_footer(text=f"Team Total: {team_total} points")
            else:
                embed.description = "*No points recorded yet for this period.*"
                embed.set_footer(text="Earn points by answering /interview questions and completing actions!")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.warning(f"leaderboard failed: {e}")
            await interaction.followup.send("❌ Failed to load leaderboard")

    async def _process_meeting_notes(self, interaction: discord.Interaction, notes_text: str, meeting_date: str):
        """Helper to process meeting notes from either modal or file.

        Flow:
        1. Save raw text to docs/meetings/ for Chroma RAG
        2. Write a row to meeting_notes table
        3. If LLM available & config flags on, extract actions + decisions
        4. Write extracted items to tasks / decisions with source_meeting_id FK
        """
        import json as _json

        from ux_helpers import ProgressCard, PublishView, create_success_embed

        progress = ProgressCard(
            title="📋 Processing Meeting Notes",
            description="Saving and extracting…",
            color=discord.Color.blue(),
        )
        await progress.send(interaction.channel)

        try:
            await progress.add_field("Meeting Date", meeting_date)
            await progress.add_field("Notes Length", f"{len(notes_text)} characters")

            # ── 1. Save to docs/meetings/ for RAG ──
            try:
                docs_root = Path(__file__).parent.parent / "docs"
                meetings_dir = docs_root / "meetings"
                meetings_dir.mkdir(parents=True, exist_ok=True)

                safe_date = meeting_date.replace("/", "-").replace(" ", "_")
                filename = f"{safe_date}_meeting_notes.md"
                filepath = meetings_dir / filename

                content = (
                    f"---\ntopic: Meeting Notes {meeting_date}\n"
                    f"date: {datetime.now().strftime('%Y-%m-%d')}\n"
                    f"doc_type: meeting_notes\ntags: meeting, notes, archive\n---\n\n"
                    f"# Meeting Notes: {meeting_date}\n\n{notes_text}\n"
                )
                filepath.write_text(content, encoding="utf-8")
                await progress.add_field("Saved", f"`docs/meetings/{filename}`")

                doc_author = interaction.client.get_cog("DocumentAuthor")
                if doc_author:
                    await doc_author._trigger_incremental_ingest()
                    await progress.add_field("Index", "✅ Updated")
            except Exception as e:
                logger.error(f"Failed to save meeting notes file: {e}")
                await progress.add_field("Save Error", str(e)[:100])

            # ── 2. Write to meeting_notes table ──
            meeting_id = None
            db = getattr(self.bot, "db", None)
            if db:
                try:
                    await progress.update_status("Writing database record…")
                    async with db.acquire() as conn:
                        cursor = await conn.execute(
                            """INSERT INTO meeting_notes
                               (summary, meeting_date, raw_text,
                                created_by_user_id, created_by_username)
                               VALUES (?, ?, ?, ?, ?)""",
                            (
                                notes_text[:500],
                                meeting_date,
                                notes_text,
                                interaction.user.id,
                                interaction.user.display_name,
                            ),
                        )
                        await conn.commit()
                        meeting_id = cursor.lastrowid
                    await progress.add_field("DB", f"meeting_notes #{meeting_id}")
                except Exception as e:
                    logger.warning(f"meeting_notes INSERT failed: {e}")
                    await progress.add_field("DB", f"⚠️ {str(e)[:60]}")

            # ── 3. Extract actions & decisions via LLM ──
            extracted_actions = []
            extracted_decisions = []

            # Read config flags
            extract_actions_flag = True
            extract_decisions_flag = True
            try:
                from core.config_loader import WorkflowConfig
                _wf = WorkflowConfig.load()
                work_raw = getattr(_wf, "raw", {}).get("work", {})
                meetings_cfg = work_raw.get("meetings", {}) if isinstance(work_raw, dict) else {}
                extract_actions_flag = meetings_cfg.get("auto_extract_actions", True)
                extract_decisions_flag = meetings_cfg.get("auto_extract_decisions", True)
            except Exception:
                pass  # defaults stay True

            llm = self.llm_service
            if llm and (extract_actions_flag or extract_decisions_flag):
                await progress.update_status("Extracting actions & decisions…")
                extraction_prompt = (
                    "Extract action items and decisions from these meeting notes.\n\n"
                    f"Meeting notes:\n\"\"\"\n{notes_text[:3000]}\n\"\"\"\n\n"
                    "Respond with ONLY valid JSON (no markdown fences):\n"
                    '{"actions": [{"title": "...", "assignee": "name or null", '
                    '"due_date": "YYYY-MM-DD or null", "priority": "medium"}], '
                    '"decisions": [{"title": "...", "decision": "what was decided", '
                    '"rationale": "why or null", "decided_by": "who or null"}]}\n'
                    "If none found, return empty arrays."
                )
                try:
                    import re as _re
                    raw_resp = await llm.complete(extraction_prompt, max_tokens=800, temperature=0.0)
                    json_match = _re.search(r"\{[\s\S]*\}", raw_resp)
                    if json_match:
                        parsed = _json.loads(json_match.group())
                        if extract_actions_flag:
                            extracted_actions = parsed.get("actions", [])
                        if extract_decisions_flag:
                            extracted_decisions = parsed.get("decisions", [])
                except Exception as e:
                    logger.warning(f"Meeting extraction LLM call failed: {e}")

            # ── 4. Write extracted items to DB ──
            action_ids = []
            decision_ids = []

            if db and extracted_actions:
                await progress.update_status(f"Creating {len(extracted_actions)} action(s)…")
                for act in extracted_actions[:10]:  # cap at 10
                    try:
                        async with db.acquire() as conn:
                            cur = await conn.execute(
                                """INSERT INTO tasks
                                   (title, description, status, priority,
                                    assigned_to_username, created_by_user_id,
                                    created_by_username, due_date, source_meeting_id,
                                    tags, created_at, updated_at)
                                   VALUES (?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    act.get("title", "Untitled action")[:120],
                                    f"Extracted from meeting notes ({meeting_date})",
                                    act.get("priority", "medium"),
                                    act.get("assignee"),
                                    interaction.user.id,
                                    interaction.user.display_name,
                                    act.get("due_date"),
                                    meeting_id,
                                    _json.dumps(["action_item", "meeting_extract"]),
                                    datetime.now().isoformat(),
                                    datetime.now().isoformat(),
                                ),
                            )
                            await conn.commit()
                            action_ids.append(cur.lastrowid)
                    except Exception as e:
                        logger.warning(f"Failed to insert extracted action: {e}")

            if db and extracted_decisions:
                await progress.update_status(f"Recording {len(extracted_decisions)} decision(s)…")
                for dec in extracted_decisions[:10]:
                    try:
                        async with db.acquire() as conn:
                            decided_by = dec.get("decided_by")
                            if decided_by:
                                decided_by = _json.dumps([decided_by] if isinstance(decided_by, str) else decided_by)
                            cur = await conn.execute(
                                """INSERT INTO decisions
                                   (title, description, decision, rationale,
                                    decided_by, category, source_meeting_id)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    dec.get("title", "Untitled decision")[:200],
                                    f"Extracted from meeting notes ({meeting_date})",
                                    dec.get("decision", dec.get("title", ""))[:500],
                                    dec.get("rationale"),
                                    decided_by,
                                    "meeting",
                                    meeting_id,
                                ),
                            )
                            await conn.commit()
                            decision_ids.append(cur.lastrowid)
                    except Exception as e:
                        logger.warning(f"Failed to insert extracted decision: {e}")

            # ── Summary ──
            extract_summary_parts = []
            if action_ids:
                extract_summary_parts.append(f"✅ {len(action_ids)} action(s)")
            if decision_ids:
                extract_summary_parts.append(f"🏛️ {len(decision_ids)} decision(s)")
            if extract_summary_parts:
                await progress.add_field("Extracted", " · ".join(extract_summary_parts))

            await progress.complete("Meeting notes processed!")

            # ── Result embed ──
            desc = f"Saved {len(notes_text)} characters to knowledge base."
            if extract_summary_parts:
                desc += f"\n\n**Auto-extracted:** {' · '.join(extract_summary_parts)}"
            desc += f"\n\n**Preview:**\n{notes_text[:400]}…"

            result_embed = create_success_embed(
                title=f"📋 Meeting Notes — {meeting_date}",
                description=desc,
            )
            result_embed.set_footer(text=f"Saved by {interaction.user.display_name}")

            view = PublishView(content="", embeds=[result_embed])
            await interaction.followup.send(embed=result_embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"parse_meeting failed: {e}")
            await progress.fail(f"Error: {str(e)[:100]}")

    @app_commands.command(
        name="parse_meeting",
        description="Save meeting notes verbatim to docs/meetings/ and reindex (upload .txt or paste)"
    )
    @app_commands.describe(file="Optional: Upload a text file containing meeting notes")
    async def parse_meeting(self, interaction: discord.Interaction, file: Optional[discord.Attachment] = None):
        """Opens a modal for pasting meeting notes, or processes uploaded file"""
        
        # If file is provided, process it directly
        if file:
            if not file.filename.endswith(('.txt', '.md', '.log')):
                await interaction.response.send_message("❌ Please upload a text file (.txt, .md, .log)", ephemeral=True)
                return
            
            await interaction.response.defer(ephemeral=True)
            
            try:
                content = await file.read()
                notes_text = content.decode('utf-8')
                meeting_date = datetime.now().strftime('%Y-%m-%d') # Default to today for files
                
                await self._process_meeting_notes(interaction, notes_text, meeting_date)
                
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to read file: {e}", ephemeral=True)
            return

        # If no file, show modal
        class MeetingNotesModal(discord.ui.Modal, title="Parse Meeting Notes"):
            meeting_date = discord.ui.TextInput(
                label="Meeting Date (optional)",
                style=discord.TextStyle.short,
                placeholder="e.g., 2025-01-15 or Today",
                required=False,
                max_length=50,
                row=0
            )
            
            # Multiple fields to bypass 4000 char limit
            part1 = discord.ui.TextInput(
                label="Notes (Part 1)",
                style=discord.TextStyle.paragraph,
                placeholder="Paste start of notes here...",
                required=True,
                max_length=4000,
                row=1
            )
            
            part2 = discord.ui.TextInput(
                label="Notes (Part 2 - Optional)",
                style=discord.TextStyle.paragraph,
                placeholder="Paste more notes here...",
                required=False,
                max_length=4000,
                row=2
            )
            
            part3 = discord.ui.TextInput(
                label="Notes (Part 3 - Optional)",
                style=discord.TextStyle.paragraph,
                placeholder="Paste even more notes here...",
                required=False,
                max_length=4000,
                row=3
            )
            
            async def on_submit(self, modal_interaction: discord.Interaction):
                await modal_interaction.response.defer(ephemeral=True)
                
                # Combine all parts
                notes_text = self.part1.value
                if self.part2.value:
                    notes_text += "\n" + self.part2.value
                if self.part3.value:
                    notes_text += "\n" + self.part3.value
                
                meeting_date = self.meeting_date.value or "Today"
                
                cog = modal_interaction.client.get_cog("AutonomousOps")
                if cog:
                    await cog._process_meeting_notes(modal_interaction, notes_text, meeting_date)
                else:
                    await modal_interaction.followup.send("❌ Error: Cog not found", ephemeral=True)
        
        modal = MeetingNotesModal()
        await interaction.response.send_modal(modal)


    @app_commands.command(name="persona_meeting", description="[Admin] Manually trigger a persona meeting")
    @app_commands.describe(
        meeting_type="Type of meeting to run (general, risk, pipeline, research, brainstorming, prototyping)",
        topic="Optional topic constraint for the meeting"
    )
    @app_commands.choices(meeting_type=[
        app_commands.Choice(name="General Discussion", value="general"),
        app_commands.Choice(name="Risk Review", value="risk_review"),
        app_commands.Choice(name="Pipeline Review", value="pipeline_review"),
        app_commands.Choice(name="Research Sprint", value="research"),
        app_commands.Choice(name="Brainstorming", value="brainstorming"),
        app_commands.Choice(name="Prototyping", value="prototyping"),
    ])
    async def force_persona_meeting(self, interaction: discord.Interaction, meeting_type: str = "general", topic: Optional[str] = None):
        """Manually trigger a persona meeting."""
        if not is_partner(interaction):
            await interaction.response.send_message("🔒 Partner access only.", ephemeral=True)
            return

        # Special brainstorming mapping
        real_type = meeting_type
        if meeting_type == "brainstorming":
             real_type = "prototyping" 

        await interaction.response.defer(ephemeral=False)
        try:
            await interaction.followup.send(f"⏳ Spawning **{real_type}** meeting...", ephemeral=True)
            now = datetime.now(EASTERN)
            await self._hold_persona_meeting(now, meeting_type=real_type, topic=topic)
            await interaction.followup.send("✅ Meeting concluded! Check the persona channel.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Meeting failed: {e}", ephemeral=True)


