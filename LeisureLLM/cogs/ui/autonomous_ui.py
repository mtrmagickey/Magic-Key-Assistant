"""
UI Components for AutonomousOps Cog.
Separated to reduce file size and improve maintainability.
"""

import json
import logging

import discord
from discord.ext import commands

import config

# Logger for this module
logger = logging.getLogger(__name__)

# Constants (mirrored from AutonomousOps or config)
# Ideally these should be in config.py, but falling back to defaults here
PM_WIP_LIMIT_IN_PROGRESS = getattr(config, 'PM_WIP_LIMIT_IN_PROGRESS', 3)
PARTNER_UPDATE_MAX_PER_DAY = getattr(config, 'PARTNER_UPDATE_MAX_PER_DAY', 1)

class PMCreateActionView(discord.ui.View):
    def __init__(
        self,
        *,
        cog,
        source_message_id: int,
        title: str,
        owner_user_id: int,
        owner_username: str,
        due_date: str,
    ):
        super().__init__(timeout=3600)
        self.cog = cog
        self.source_message_id = int(source_message_id)
        self.title = (title or "").strip()[:180]
        self.owner_user_id = int(owner_user_id)
        self.owner_username = (owner_username or "").strip()[:120]
        self.due_date = (due_date or "").strip()[:10]

    @discord.ui.button(label="✅ Create action", style=discord.ButtonStyle.success)
    async def create_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            # Note: Accessing protected method on cog, assume cog exposes it or is friendly
            action_id, wip_blocked = await self.cog._pm_create_action_item(
                title=self.title,
                owner_user_id=int(self.owner_user_id),
                owner_username=self.owner_username,
                created_by_user_id=int(interaction.user.id),
                created_by_username=str(interaction.user),
                due_date=self.due_date,
                source_message_id=int(self.source_message_id),
            )
            msg = f"✅ Created action **#{action_id}** for <@{self.owner_user_id}> (due {self.due_date})."
            if wip_blocked:
                msg += f"\n⛔ Note: owner is at WIP limit ({PM_WIP_LIMIT_IN_PROGRESS} in progress)."
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            logger.warning(f"PM create action failed: {e}")
            await interaction.followup.send("❌ Failed to create action", ephemeral=True)
        finally:
            self.stop()

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Dismissed.", ephemeral=True)
        self.stop()


class PMCreateDecisionView(discord.ui.View):
    def __init__(
        self,
        *,
        cog,
        title: str,
        decision_text: str,
        source_message_id: int,
    ):
        super().__init__(timeout=3600)
        self.cog = cog
        self.title = (title or "Decision").strip()[:180]
        self.decision_text = (decision_text or "").strip()[:1800]
        self.source_message_id = int(source_message_id)

    @discord.ui.button(label="✅ Record decision", style=discord.ButtonStyle.success)
    async def record(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            decision_id = await self.cog._pm_create_decision(
                title=self.title,
                decision_text=self.decision_text,
                decided_by_user_id=int(interaction.user.id),
                decided_by_username=str(interaction.user),
                source_message_id=int(self.source_message_id),
            )
            await interaction.followup.send(f"✅ Recorded decision **#{decision_id}**", ephemeral=True)
        except Exception as e:
            logger.warning(f"PM record decision failed: {e}")
            await interaction.followup.send("❌ Failed to record decision", ephemeral=True)
        finally:
            self.stop()

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Dismissed.", ephemeral=True)
        self.stop()


class DidCompleteTaskSelect(discord.ui.Select):
    def __init__(self, tasks: list):
        options = []
        for t in tasks:
            # t = (id, title)
            label = f"#{t[0]} {t[1]}"
            if len(label) > 95:
                label = label[:95] + "..."
            options.append(discord.SelectOption(
                label=label,
                value=str(t[0])
            ))
        super().__init__(placeholder="Does this complete a task?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        task_id = int(self.values[0])
        try:
             # Use bot from interaction.client
             db = getattr(interaction.client, "db", None)
             if db:
                await db.execute(
                    "UPDATE tasks SET status='done', completed_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
                    (task_id,)
                    )
                await interaction.followup.send(f"✅ Marked task #{task_id} as complete!", ephemeral=True)
        except Exception as e:
             await interaction.followup.send(f"❌ Failed to close task: {e}", ephemeral=True)


class DidCompleteTaskView(discord.ui.View):
    def __init__(self, tasks: list):
        super().__init__(timeout=120)
        self.add_item(DidCompleteTaskSelect(tasks))


class DidSomethingModal(discord.ui.Modal, title="Log an Update"):
    details = discord.ui.TextInput(
        label="What did you do?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g., Sent client email; fixed a bug; shipped a doc update",
        required=True,
        max_length=1000,
    )

    link = discord.ui.TextInput(
        label="Optional link (doc/ticket/GitHub/Discord message)",
        style=discord.TextStyle.short,
        placeholder="https://...",
        required=False,
        max_length=200,
    )

    def __init__(self, *, bot: commands.Bot, category: str = "update"):
        super().__init__()
        self.bot = bot
        self.category = (category or "update").strip().lower()[:30] or "update"

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return

        details = (self.details.value or "").strip()
        link = (self.link.value or "").strip() or None
        if not details:
            await interaction.followup.send("❌ Please add a short update.", ephemeral=True)
            return

        try:
            open_tasks = []
            async with self.bot.db.acquire() as conn:
                # Strict anti-spam: max N updates per UTC day per user.
                async with conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM partner_updates
                    WHERE partner_user_id = ?
                      AND date(created_at) = date('now')
                    """,
                    (int(interaction.user.id),),
                ) as cursor:
                    row = await cursor.fetchone()
                    already = int(row[0] or 0) if row else 0

                if already >= int(PARTNER_UPDATE_MAX_PER_DAY):
                    await interaction.followup.send(
                        "⏳ You’ve already logged an update today. Try again tomorrow (anti-spam rule).",
                        ephemeral=True,
                    )
                    return

                await conn.execute(
                    """
                    INSERT INTO partner_updates (partner_user_id, partner_username, category, details, link)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(interaction.user.id),
                        getattr(interaction.user, "display_name", None) or str(interaction.user),
                        self.category,
                        details,
                        link,
                    ),
                )

                # Award +1 point for logging an update (idempotent per update via unique key)
                try:
                    async with conn.execute("SELECT last_insert_rowid()") as cursor:
                        row = await cursor.fetchone()
                    update_id = int(row[0]) if row and row[0] is not None else None
                    if update_id is not None:
                        await conn.execute(
                            """
                            INSERT OR IGNORE INTO partner_point_events (
                                partner_user_id, partner_username, entity_type, entity_id, reason, points
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                int(interaction.user.id),
                                getattr(interaction.user, "display_name", None) or str(interaction.user),
                                "partner_update",
                                int(update_id),
                                "did_logged",
                                1,
                            ),
                        )
                except Exception as e:
                    logger.warning(f"Failed awarding /did points: {e}")
                
                # Fetch open tasks
                async with conn.execute("SELECT id, title FROM tasks WHERE status IN ('todo', 'in_progress') ORDER BY updated_at DESC LIMIT 5") as cursor:
                     open_tasks = await cursor.fetchall()
                
                await conn.commit()

            msg = "✅ Logged. I’ll surface this in the next async meeting agenda."
            view = None
            if open_tasks:
                 msg += "\n\n**Does this update complete any of these tasks?**"
                 view = DidCompleteTaskView(open_tasks)

            await interaction.followup.send(msg, ephemeral=True, view=view)
            
            # Public acknowledgment: let the channel know someone shared an update
            try:
                category_emoji = {
                    "email": "📧", "bugfix": "🐛", "shipment": "📦", 
                    "ops": "⚙️", "meeting": "📅", "doc": "📝"
                }.get(self.category, "✨")
                
                public_embed = discord.Embed(
                    description=f"{category_emoji} **{interaction.user.display_name}** logged an update: *{details[:100]}{'...' if len(details) > 100 else ''}*",
                    color=discord.Color.blue()
                )
                public_embed.set_footer(text="Use /did to share your wins!")
                await interaction.channel.send(embed=public_embed)
            except Exception as e:
                logger.debug(f"Failed to post /did public acknowledgment: {e}")
        except Exception as e:
            logger.warning(f"Failed to log partner update: {e}")
            await interaction.followup.send("❌ Failed to log update", ephemeral=True)


class HirePersonaModal(discord.ui.Modal, title="Design Your New Hire"):
    """Modal for creating a custom persona that can participate in meetings."""
    
    name_input = discord.ui.TextInput(
        label="Name",
        style=discord.TextStyle.short,
        placeholder="e.g., Dino PM, Client Liaison, Tech Lead",
        required=True,
        max_length=50,
    )
    
    emoji_input = discord.ui.TextInput(
        label="Emoji (1-2 characters)",
        style=discord.TextStyle.short,
        placeholder="e.g., 🦖, 🎯, 🔧",
        required=True,
        max_length=4,
    )
    
    personality_input = discord.ui.TextInput(
        label="Personality traits",
        style=discord.TextStyle.short,
        placeholder="e.g., deadline-focused, diplomatic, technically rigorous",
        required=True,
        max_length=150,
    )
    
    concerns_input = discord.ui.TextInput(
        label="What they care about in meetings",
        style=discord.TextStyle.paragraph,
        placeholder="e.g., project milestones, client satisfaction, technical debt",
        required=True,
        max_length=300,
    )
    
    project_context_input = discord.ui.TextInput(
        label="Project/initiative focus (optional)",
        style=discord.TextStyle.short,
        placeholder="e.g., Project Alpha, client outreach, general ops",
        required=False,
        max_length=100,
    )
    
    def __init__(self, *, bot: commands.Bot):
        super().__init__()
        self.bot = bot
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        db = getattr(self.bot, "db", None)
        if not db:
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        
        name = self.name_input.value.strip()
        emoji = self.emoji_input.value.strip()[:4]
        personality = self.personality_input.value.strip()
        concerns = self.concerns_input.value.strip()
        project_context = (self.project_context_input.value or "").strip() or None
        
        # Generate a key from the name (lowercase, no spaces)
        import re
        key = re.sub(r'[^a-z0-9]', '', name.lower())[:20]
        if not key:
            key = f"custom_{int(interaction.user.id) % 10000}"
        
        try:
            async with db.acquire() as conn:
                # Check if key already exists (active)
                async with conn.execute(
                    "SELECT id FROM custom_personas WHERE key = ? AND active = 1",
                    (key,)
                ) as cursor:
                    existing = await cursor.fetchone()
                
                if existing:
                    # Append a number to make it unique
                    key = f"{key}_{existing[0] + 1}"
                
                await conn.execute(
                    """
                    INSERT INTO custom_personas (key, name, emoji, personality, concerns, project_context, created_by_user_id, created_by_username)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        name,
                        emoji,
                        personality,
                        concerns,
                        project_context,
                        int(interaction.user.id),
                        interaction.user.display_name,
                    ),
                )
                await conn.commit()
            
            # Success message
            project_note = f" (focused on *{project_context}*)" if project_context else ""
            embed = discord.Embed(
                title=f"{emoji} Welcome aboard, {name}!",
                description=f"**Personality:** {personality}\n**Cares about:** {concerns}{project_note}",
                color=discord.Color.green()
            )
            embed.set_footer(text=f"Hired by {interaction.user.display_name} • Will appear in persona meetings")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Public announcement
            try:
                public_embed = discord.Embed(
                    description=f"🎉 **{interaction.user.display_name}** just hired **{emoji} {name}** to the team!",
                    color=discord.Color.green()
                )
                public_embed.set_footer(text="Use /hire to add your own virtual staff")
                await interaction.channel.send(embed=public_embed)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
                
        except Exception as e:
            logger.warning(f"Failed to hire persona: {e}")
            await interaction.followup.send(f"❌ Failed to create persona: {str(e)[:100]}", ephemeral=True)


class FirePersonaSelect(discord.ui.Select):
    """Dropdown to select which custom persona to fire."""
    
    def __init__(self, personas: list, bot: commands.Bot):
        self.bot = bot
        options = [
            discord.SelectOption(
                label=f"{p['emoji']} {p['name']}",
                description=f"Created by {p['created_by_username']}"[:100] if p.get('created_by_username') else "Custom persona",
                value=str(p['id'])
            )
            for p in personas[:25]  # Discord limit
        ]
        super().__init__(
            placeholder="Select persona to fire...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        persona_id = int(self.values[0])
        db = getattr(self.bot, "db", None)
        
        if not db:
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        
        try:
            async with db.acquire() as conn:
                # Get persona details before firing
                async with conn.execute(
                    "SELECT name, emoji FROM custom_personas WHERE id = ?",
                    (persona_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                
                if not row:
                    await interaction.followup.send("❌ Persona not found", ephemeral=True)
                    return
                
                name, emoji = row[0], row[1]
                
                # Fire (soft delete)
                await conn.execute(
                    "UPDATE custom_personas SET active = 0, fired_at = datetime('now') WHERE id = ?",
                    (persona_id,)
                )
                await conn.commit()
            
            await interaction.followup.send(
                f"👋 **{emoji} {name}** has been let go. They won't appear in future meetings.",
                ephemeral=True
            )
            
        except Exception as e:
            logger.warning(f"Failed to fire persona: {e}")
            await interaction.followup.send(f"❌ Error: {str(e)[:100]}", ephemeral=True)


class FirePersonaView(discord.ui.View):
    """View containing the fire dropdown."""
    
    def __init__(self, personas: list, bot: commands.Bot):
        super().__init__(timeout=60)
        self.add_item(FirePersonaSelect(personas, bot))
