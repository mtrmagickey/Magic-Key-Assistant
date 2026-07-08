"""
/setup wizard — Guided onboarding for new organisations.

Walks users through:
  1. Organisation name & mode (Solo / Small / Team)
  2. Channel mapping (ops, pipeline, reviews)
  3. Module selection (Memory, Work, Pipeline, Health)
  4. Writes org_profile.yaml and workflows.yaml

This cog is loaded like any other cog and provides a single
slash command group: /setup.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"


# ── Modals ────────────────────────────────────────────────────

class OrgInfoModal(discord.ui.Modal, title="Organisation Setup"):
    """Step 1: Collect organisation basics."""

    org_name = discord.ui.TextInput(
        label="Organisation name",
        placeholder="e.g. Acme Creative Studio",
        max_length=100,
    )
    industry = discord.ui.TextInput(
        label="Industry (optional)",
        placeholder="e.g. Creative Agency, SaaS, Consulting",
        required=False,
        max_length=60,
    )
    tagline = discord.ui.TextInput(
        label="One-line description (optional)",
        placeholder="What does your company do?",
        required=False,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Store in interaction extras for the view to pick up
        self.interaction = interaction
        await interaction.response.defer(ephemeral=True)


# ── Views ─────────────────────────────────────────────────────

class ModeSelect(discord.ui.Select):
    """Step 2: Pick operating mode."""

    def __init__(self):
        options = [
            discord.SelectOption(
                label="Solo",
                description="Single founder — all artifacts owned by you",
                value="solo",
                emoji="🧑",
            ),
            discord.SelectOption(
                label="Small (2–3)",
                description="Lightweight role mapping",
                value="small",
                emoji="👥",
            ),
            discord.SelectOption(
                label="Team (4–6)",
                description="Full role mapping and delegation",
                value="team",
                emoji="👨‍👩‍👧‍👦",
            ),
        ]
        super().__init__(placeholder="How big is your team?", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.mode = self.values[0]  # type: ignore[union-attr]
        await interaction.response.send_message(
            f"✅ Mode set to **{self.values[0]}**. Now select which modules to enable.",
            ephemeral=True,
        )


class ModuleSelect(discord.ui.Select):
    """Step 3: Pick which modules to enable."""

    def __init__(self):
        options = [
            discord.SelectOption(label="Memory", description="RAG + knowledge gaps + decision recall", value="memory", default=True),
            discord.SelectOption(label="Work", description="Actions + meetings + weekly threads", value="work", default=True),
            discord.SelectOption(label="Pipeline", description="Leads + follow-ups + nudges", value="pipeline"),
            discord.SelectOption(label="Health", description="Staleness, overdue, weekly scorecard", value="health", default=True),
        ]
        super().__init__(
            placeholder="Select modules to enable",
            options=options,
            min_values=1,
            max_values=4,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.modules = self.values  # type: ignore[union-attr]
        await interaction.response.send_message(
            f"✅ Modules: {', '.join(self.values)}",
            ephemeral=True,
        )


class SetupView(discord.ui.View):
    """Composite view for the /setup flow."""

    def __init__(self, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.mode: str = "solo"
        self.modules: list = ["memory", "work", "health"]
        self.org_name: str = ""
        self.industry: str = ""
        self.tagline: str = ""
        self.add_item(ModeSelect())
        self.add_item(ModuleSelect())

    @discord.ui.button(label="Finish Setup", style=discord.ButtonStyle.green, row=3)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Write config files, seed foundational gaps, and offer capture sprint."""
        try:
            self._write_org_profile()
            self._write_workflows()

            # ── Seed foundational knowledge gaps ──────────────────
            gap_count = 0
            try:
                db = getattr(interaction.client, "db", None)
                if db:
                    from core.seed_foundational_gaps import seed_foundational_gaps
                    result = await seed_foundational_gaps(
                        db,
                        mode=self.mode,
                        industry=self.industry,
                        org_name=self.org_name,
                    )
                    gap_count = result.get("count", 0)
                    if gap_count:
                        logger.info("Seeded %d foundational gaps during setup", gap_count)
            except Exception as e:
                logger.warning("Failed to seed foundational gaps: %s", e)

            await interaction.response.send_message(
                "🎉 **Setup complete!**\n"
                f"• Org: **{self.org_name or 'My Company'}** ({self.mode})\n"
                f"• Modules: {', '.join(self.modules)}\n"
                f"• Config written to `config/org_profile.yaml` and `config/workflows.yaml`\n"
                + (f"• 📝 **{gap_count} interview questions** seeded for your org\n" if gap_count else "")
                + "\nRun `/health` to verify everything is connected.",
                ephemeral=True,
            )

            # ── Offer the capture sprint ──────────────────────────
            try:
                from core.onboarding_sprint import (
                    SprintWelcomeView,
                    build_welcome_embed,
                    is_sprint_complete,
                    run_capture_sprint,
                )

                if not is_sprint_complete():
                    embed = build_welcome_embed(
                        org_name=self.org_name or "your organisation",
                        gap_count=gap_count,
                    )
                    welcome_view = SprintWelcomeView()
                    await interaction.followup.send(embed=embed, view=welcome_view, ephemeral=True)
                    await welcome_view.wait()

                    if welcome_view.started:
                        await run_capture_sprint(
                            interaction.channel,
                            interaction.user,
                            interaction.client,
                            org_name=self.org_name,
                            gap_count=gap_count,
                        )
            except Exception as e:
                logger.warning("Capture sprint failed: %s", e)

        except Exception as e:
            logger.error("Setup failed: %s", e, exc_info=True)
            await interaction.response.send_message(
                f"❌ Setup failed: {e}",
                ephemeral=True,
            )
        self.stop()

    # ── Config writers ────────────────────────────────────────

    def _write_org_profile(self) -> None:
        """Write org_profile.yaml from wizard answers."""
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed — writing raw text")
            yaml = None

        profile = {
            "org": {
                "name": self.org_name or "My Company",
                "tagline": self.tagline,
                "industry": self.industry,
            },
            "mode": self.mode,
            "timezone": "America/New_York",
            "members": [],
            "channels": {"ops": "", "pipeline": "", "reviews": "", "allowed": []},
            "branding": {"bot_name": "Magic Key Assistant"},
        }

        path = CONFIG_DIR / "org_profile.yaml"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if yaml:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(profile, f, default_flow_style=False, sort_keys=False)
        else:
            # Fallback: write a simple representation
            import json
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Auto-generated by /setup\n")
                f.write(json.dumps(profile, indent=2))
        logger.info("Wrote org profile to %s", path)

    def _write_workflows(self) -> None:
        """Write workflows.yaml from wizard answers."""
        try:
            import yaml
        except ImportError:
            yaml = None

        wf = {
            "memory": {"enabled": "memory" in self.modules},
            "work": {"enabled": "work" in self.modules},
            "pipeline": {"enabled": "pipeline" in self.modules},
            "health": {"enabled": "health" in self.modules},
            "persona_meetings": {"enabled": False},
            "automation": {
                "artifact_contract": {"enforce": True, "warn_only": False},
            },
        }

        path = CONFIG_DIR / "workflows.yaml"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if yaml:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(wf, f, default_flow_style=False, sort_keys=False)
        else:
            import json
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Auto-generated by /setup\n")
                f.write(json.dumps(wf, indent=2))
        logger.info("Wrote workflows config to %s", path)


# ── Cog ───────────────────────────────────────────────────────

class SetupWizard(commands.Cog):
    """Guided onboarding wizard for new organisations."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Run the onboarding wizard for your organisation")
    async def setup_command(self, interaction: discord.Interaction):
        """Launch the /setup wizard."""
        # Step 1: collect org info via modal
        modal = OrgInfoModal()
        await interaction.response.send_modal(modal)
        await modal.wait()

        # Step 2 + 3: mode & module selection via view
        view = SetupView()
        view.org_name = modal.org_name.value or ""
        view.industry = modal.industry.value or ""
        view.tagline = modal.tagline.value or ""

        await modal.interaction.followup.send(
            "**Step 2: Choose your operating mode and modules**\n"
            "Select your team size and which modules to enable, then click **Finish Setup**.",
            view=view,
            ephemeral=True,
        )


    @app_commands.command(name="sprint", description="Run the 3-minute knowledge capture sprint")
    async def sprint_command(self, interaction: discord.Interaction):
        """Launch the capture sprint — 3 guided captures to bootstrap the knowledge base."""
        from core.onboarding_sprint import (
            SprintWelcomeView,
            build_welcome_embed,
            run_capture_sprint,
        )

        # Count open foundational gaps for the embed
        gap_count = 0
        db = getattr(self.bot, "db", None)
        if db:
            try:
                async with db.acquire() as conn, conn.execute(
                    "SELECT COUNT(*) FROM knowledge_gaps WHERE status='open'"
                ) as cur:
                    gap_count = (await cur.fetchone())[0]
            except Exception as e:
                logger.warning("sprint_command: suppressed %s", e)

        # Load org name
        org_name = ""
        try:
            from core.config_loader import OrgProfile
            org = OrgProfile.load()
            org_name = org.name if org.name != "My Company" else ""
        except Exception as e:
            logger.warning("sprint_command: suppressed %s", e)

        embed = build_welcome_embed(org_name=org_name or "your organisation", gap_count=gap_count)
        welcome_view = SprintWelcomeView()
        await interaction.response.send_message(embed=embed, view=welcome_view, ephemeral=True)
        await welcome_view.wait()

        if welcome_view.started:
            await run_capture_sprint(
                interaction.channel,
                interaction.user,
                self.bot,
                org_name=org_name,
                gap_count=gap_count,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupWizard(bot))
