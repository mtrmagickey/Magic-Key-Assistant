"""
UX Helpers for Discord interactions
Reusable components for professional Discord UX patterns
"""

import logging
from typing import Awaitable, Callable, List, Optional

import discord

logger = logging.getLogger(__name__)

# ── Discord UI timeout defaults (seconds) ────────────────────────────────────
# Override in config/bot_settings.json → discord_ui_defaults.
DISCORD_VIEW_TIMEOUT = 300        # PublishView, general embeds
DISCORD_CONFIRM_TIMEOUT = 60      # ConfirmView default
DISCORD_PAGINATOR_TIMEOUT = 180   # PaginationView


class PublishView(discord.ui.View):
    """View with publish button to share ephemeral message to channel"""
    
    def __init__(self, content: str, embeds: Optional[List[discord.Embed]] = None):
        super().__init__(timeout=DISCORD_VIEW_TIMEOUT)
        self.content = content
        self.embeds = embeds or []
    
    @discord.ui.button(label="📤 Publish to Channel", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Publish ephemeral content to channel"""
        await interaction.channel.send(self.content[:2000], embeds=self.embeds[:10])
        await interaction.response.send_message("✅ Published to channel", ephemeral=True)
        self.stop()


class ConfirmView(discord.ui.View):
    """View with confirm/cancel buttons"""
    
    def __init__(self, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.value: Optional[bool] = None
    
    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        """User confirmed action"""
        self.value = True
        await interaction.response.send_message("Confirmed", ephemeral=True)
        self.stop()
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """User cancelled action"""
        self.value = False
        await interaction.response.send_message("Cancelled", ephemeral=True)
        self.stop()


class PaginationView(discord.ui.View):
    """Simple view for navigating multiple embeds"""
    
    def __init__(self, pages: List[discord.Embed], timeout: int = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current_page = 0
        self.total_pages = len(pages)
        self._update_buttons()
    
    def _update_buttons(self):
        self.prev_button.disabled = (self.current_page == 0)
        self.next_button.disabled = (self.current_page == self.total_pages - 1)
        self.page_counter.label = f"{self.current_page + 1}/{self.total_pages}"
    
    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass
    
    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)


class ProgressCard:
    """Editable progress card for long-running operations"""
    
    def __init__(self, title: str, description: str = "", color: discord.Color = discord.Color.blue()):
        self.embed = discord.Embed(title=title, description=description, color=color)
        self.message: Optional[discord.Message] = None
        self.current_status = "⏳ Starting..."
        self.embed.add_field(name="Status", value=self.current_status, inline=False)
    
    async def send(self, channel: discord.TextChannel) -> discord.Message:
        """Send progress card to channel"""
        self.message = await channel.send(embed=self.embed)
        return self.message
    
    async def update_status(self, status: str, color: Optional[discord.Color] = None):
        """Update status field"""
        if not self.message:
            logger.warning("Cannot update progress card - message not sent")
            return
        
        self.current_status = status
        self.embed.set_field_at(0, name="Status", value=status, inline=False)
        
        if color:
            self.embed.color = color
        
        try:
            await self.message.edit(embed=self.embed)
        except Exception as e:
            logger.error(f"Failed to update progress card: {e}")
    
    async def add_field(self, name: str, value: str, inline: bool = False):
        """Add new field to progress card"""
        if not self.message:
            logger.warning("Cannot add field to progress card - message not sent")
            return
        
        self.embed.add_field(name=name, value=value, inline=inline)
        
        try:
            await self.message.edit(embed=self.embed)
        except Exception as e:
            logger.error(f"Failed to add field to progress card: {e}")
    
    async def complete(self, final_message: str = "✅ Complete"):
        """Mark progress card as complete"""
        await self.update_status(final_message, discord.Color.green())
    
    async def fail(self, error_message: str):
        """Mark progress card as failed"""
        await self.update_status(f"❌ Failed: {error_message}", discord.Color.red())
    
    async def add_buttons(self, view: discord.ui.View):
        """Add interactive buttons to progress card"""
        if not self.message:
            logger.warning("Cannot add buttons to progress card - message not sent")
            return
        
        try:
            await self.message.edit(embed=self.embed, view=view)
        except Exception as e:
            logger.error(f"Failed to add buttons to progress card: {e}")


class ActionButton(discord.ui.Button):
    """Reusable action button with callback"""
    
    def __init__(
        self,
        label: str,
        callback: Callable[[discord.Interaction], Awaitable[None]],
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        emoji: Optional[str] = None
    ):
        super().__init__(label=label, style=style, emoji=emoji)
        self._callback = callback
    
    async def callback(self, interaction: discord.Interaction):
        """Execute custom callback"""
        await self._callback(interaction)


class PaginatedEmbed:
    """Multi-page embed with navigation buttons"""
    
    def __init__(self, pages: List[discord.Embed]):
        self.pages = pages
        self.current_page = 0
        self.message: Optional[discord.Message] = None
    
    async def send(self, interaction: discord.Interaction, ephemeral: bool = True):
        """Send first page with navigation"""
        if not self.pages:
            await interaction.followup.send("No content to display", ephemeral=ephemeral)
            return
        
        view = self._create_view()
        await interaction.followup.send(embed=self.pages[0], view=view, ephemeral=ephemeral)
    
    def _create_view(self) -> discord.ui.View:
        """Create view with navigation buttons"""
        view = discord.ui.View(timeout=300)
        
        # Previous button
        prev_button = discord.ui.Button(
            label="◀️ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page == 0
        )
        prev_button.callback = self._previous_page
        view.add_item(prev_button)
        
        # Page indicator
        page_label = discord.ui.Button(
            label=f"Page {self.current_page + 1}/{len(self.pages)}",
            style=discord.ButtonStyle.secondary,
            disabled=True
        )
        view.add_item(page_label)
        
        # Next button
        next_button = discord.ui.Button(
            label="Next ▶️",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page >= len(self.pages) - 1
        )
        next_button.callback = self._next_page
        view.add_item(next_button)
        
        return view
    
    async def _previous_page(self, interaction: discord.Interaction):
        """Navigate to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            view = self._create_view()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=view)
    
    async def _next_page(self, interaction: discord.Interaction):
        """Navigate to next page"""
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            view = self._create_view()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=view)


def _apply_fields(embed: discord.Embed, fields: Optional[List[tuple]] = None):
    if not fields:
        return embed
    for entry in fields:
        if isinstance(entry, tuple):
            if len(entry) == 3:
                name, value, inline = entry
            elif len(entry) == 2:
                name, value = entry
                inline = False
            else:
                continue
            embed.add_field(name=name, value=value, inline=inline)
    return embed


def _apply_footer(embed: discord.Embed, footer: Optional[str] = None):
    if footer:
        embed.set_footer(text=footer)
    return embed


def create_success_embed(title: str, description: str, fields: Optional[List[tuple]] = None, footer: Optional[str] = None) -> discord.Embed:
    """Create standardized success embed"""
    embed = discord.Embed(title=f"✅ {title}", description=description, color=discord.Color.green())
    _apply_fields(embed, fields)
    _apply_footer(embed, footer)
    return embed


def create_error_embed(title: str, description: str, fields: Optional[List[tuple]] = None, footer: Optional[str] = None) -> discord.Embed:
    """Create standardized error embed"""
    embed = discord.Embed(title=f"❌ {title}", description=description, color=discord.Color.red())
    _apply_fields(embed, fields)
    _apply_footer(embed, footer)
    return embed


def create_info_embed(title: str, description: str, fields: Optional[List[tuple]] = None, footer: Optional[str] = None) -> discord.Embed:
    """Create standardized info embed"""
    embed = discord.Embed(title=f"ℹ️ {title}", description=description, color=discord.Color.blue())
    _apply_fields(embed, fields)
    _apply_footer(embed, footer)
    return embed


def create_warning_embed(title: str, description: str, fields: Optional[List[tuple]] = None, footer: Optional[str] = None) -> discord.Embed:
    """Create standardized warning embed"""
    embed = discord.Embed(title=f"⚠️ {title}", description=description, color=discord.Color.orange())
    _apply_fields(embed, fields)
    _apply_footer(embed, footer)
    return embed
