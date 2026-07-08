"""
onboarding_sprint — Guided first-run capture sprint.

After setup + foundational gap seeding, this module drives a Discord-based
"Getting Started" flow that walks users through three quick captures:

  1. Capture a recent decision  → creates a decision record + doc
  2. Add an upcoming deadline   → creates an obligation record
  3. Describe a key process     → creates an SOP skeleton

Each capture uses the existing /remember and entity-creation flows.
The sprint makes the corpus-building flywheel visible from minute one:
after 3 inputs the bot can already answer real questions about the org.

Also provides an admin (web) API endpoint for triggering the sprint
guidance and tracking first-run completion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import discord

logger = logging.getLogger(__name__)


# ── Sprint steps ─────────────────────────────────────────────────────────────

@dataclass
class SprintStep:
    """One step in the guided capture sprint."""
    step_number: int
    title: str
    emoji: str
    prompt_text: str
    hint_text: str
    entity_type: str          # decision | obligation | sop | knowledge
    example: str


_SPRINT_STEPS: List[SprintStep] = [
    SprintStep(
        step_number=1,
        title="Capture a recent decision",
        emoji="⚖️",
        prompt_text=(
            "**Step 1 of 3: Capture a Decision**\n\n"
            "Think of a decision you've made recently — big or small.\n"
            "Type it as naturally as you would in a conversation.\n\n"
            "For example: *'We decided to use Discord instead of Slack because "
            "most of our team already uses it and we don't want to pay for another tool.'*\n\n"
            "💡 **Just type your decision below** — or use `/remember` to capture it."
        ),
        hint_text="What did you decide, and why?",
        entity_type="decision",
        example="We decided to switch from weekly in-person meetings to async standups because scheduling conflicts were causing us to miss every other week.",
    ),
    SprintStep(
        step_number=2,
        title="Add an upcoming deadline",
        emoji="📅",
        prompt_text=(
            "**Step 2 of 3: Add a Deadline**\n\n"
            "What's a recurring deadline or upcoming obligation you can't afford to miss?\n\n"
            "For example: *'Business insurance renewal is due July 15th every year. "
            "We need to start the renewal process 60 days beforehand.'*\n\n"
            "💡 **Just type it below** — the system will create a tracked obligation."
        ),
        hint_text="What's due, when, and what happens if you miss it?",
        entity_type="obligation",
        example="Quarterly tax filing is due every 3 months. Missing it means penalties and interest. We use our accountant but I need to send them the books 2 weeks early.",
    ),
    SprintStep(
        step_number=3,
        title="Describe a key process",
        emoji="📋",
        prompt_text=(
            "**Step 3 of 3: Describe a Process**\n\n"
            "Think of something your team does regularly that only exists in someone's head.\n\n"
            "For example: *'When a new client signs up, we create their folder in Drive, "
            "add them to our invoicing system, schedule a kickoff call, and assign a project lead.'*\n\n"
            "💡 **Just type the steps below** — this becomes a searchable SOP in your knowledge base."
        ),
        hint_text="Walk me through the steps — what happens first, then what?",
        entity_type="sop",
        example="Opening procedure: arrive at 7am, disarm alarm (code is in the key safe), turn on lights starting from the back, check overnight maintenance log, boot up the POS system, unlock the front door at 8am.",
    ),
]


# ── Sprint state tracking ────────────────────────────────────────────────────

_SPRINT_FLAG = ".capture_sprint_complete"


def _flag_path(base_dir: Path | None = None) -> Path:
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    return base_dir / _SPRINT_FLAG


def is_sprint_complete(base_dir: Path | None = None) -> bool:
    return _flag_path(base_dir).exists()


def mark_sprint_complete(base_dir: Path | None = None) -> None:
    flag = _flag_path(base_dir)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(datetime.utcnow().isoformat(), encoding="utf-8")


# ── Discord views ────────────────────────────────────────────────────────────

class SprintWelcomeView(discord.ui.View):
    """Initial welcome message with 'Start' and 'Skip' buttons."""

    def __init__(self, *, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.started = False

    @discord.ui.button(label="Start Quick Setup (3 min)", style=discord.ButtonStyle.green, emoji="🚀")
    async def start_sprint(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.started = True
        await interaction.response.defer(ephemeral=True)
        self.stop()

    @discord.ui.button(label="Skip for now", style=discord.ButtonStyle.secondary)
    async def skip_sprint(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.started = False
        await interaction.response.send_message(
            "No problem! You can start the capture sprint anytime with `/sprint` "
            "or just use `/remember` and `/teach` whenever you're ready.\n\n"
            "💡 Tip: Try `/interview` — the system already has questions ready for you.",
            ephemeral=True,
        )
        self.stop()


class SprintStepView(discord.ui.View):
    """View for each sprint step — shows the step prompt and an example button."""

    def __init__(self, step: SprintStep, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.step = step
        self.skipped = False

    @discord.ui.button(label="Show me an example", style=discord.ButtonStyle.secondary, emoji="💡")
    async def show_example(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**Example:**\n> {self.step.example}",
            ephemeral=True,
        )

    @discord.ui.button(label="Skip this step", style=discord.ButtonStyle.secondary)
    async def skip_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.skipped = True
        await interaction.response.defer(ephemeral=True)
        self.stop()


def build_welcome_embed(org_name: str = "your organisation", gap_count: int = 0) -> discord.Embed:
    """Build the welcome embed shown after setup completes."""
    embed = discord.Embed(
        title="🚀 Let's build your knowledge base",
        description=(
            f"Welcome to **{org_name}**'s new assistant!\n\n"
            "Your knowledge base is empty right now, but we can change that in about 3 minutes.\n\n"
            "I'll walk you through **3 quick captures** — a decision, a deadline, and a process — "
            "and your assistant will immediately be able to answer questions about them.\n\n"
        ),
        color=0x5865F2,
    )

    if gap_count > 0:
        embed.add_field(
            name="📝 Interview Questions Ready",
            value=(
                f"I've also prepared **{gap_count} interview questions** tailored to your org. "
                f"After this sprint, try `/interview` to keep building your knowledge base."
            ),
            inline=False,
        )

    embed.add_field(
        name="What you'll capture",
        value=(
            "1️⃣ A recent **decision** you've made\n"
            "2️⃣ An upcoming **deadline** you can't miss\n"
            "3️⃣ A **process** that lives in someone's head"
        ),
        inline=False,
    )

    embed.set_footer(text="Each answer takes about 1 minute. You can skip any step.")
    return embed


def build_sprint_complete_embed(
    captured: int,
    total: int,
    gap_count: int = 0,
) -> discord.Embed:
    """Build the completion embed shown after the sprint finishes."""
    if captured == 0:
        desc = (
            "No worries — you can capture things anytime with:\n"
            "• `/remember` — paste any text and the system auto-classifies it\n"
            "• `/teach` — add structured knowledge with categories and tags\n"
        )
    elif captured < total:
        desc = (
            f"You captured **{captured} of {total}** items — great start!\n"
            "Your assistant can already answer questions about what you've entered.\n"
        )
    else:
        desc = (
            f"**All {total} items captured!** Your knowledge base is off to a strong start.\n"
            "Your assistant can now answer questions about your decisions, deadlines, and processes.\n"
        )

    embed = discord.Embed(
        title="✅ Capture Sprint Complete",
        description=desc,
        color=0x43B581,
    )

    next_steps = []
    if gap_count > 0:
        next_steps.append(f"🎤 **`/interview`** — Answer {gap_count} tailored questions to deepen your knowledge base")
    next_steps.extend([
        "💬 **Ask me anything** — Try asking about what you just captured",
        "📝 **`/remember`** — Capture more decisions, meetings, or knowledge anytime",
        "📚 **`/teach`** — Add structured knowledge with categories and tags",
    ])

    embed.add_field(
        name="What's next",
        value="\n".join(next_steps),
        inline=False,
    )

    return embed


# ── Sprint runner (called from SetupWizard or /sprint command) ───────────────

async def run_capture_sprint(
    channel: discord.abc.Messageable,
    user: discord.User | discord.Member,
    bot,
    *,
    org_name: str = "",
    gap_count: int = 0,
) -> Dict[str, Any]:
    """Run the 3-step guided capture sprint in a Discord channel.

    Uses the bot's DocumentAuthor cog for /remember-style classification.
    Returns a summary dict.
    """
    doc_author = bot.get_cog("DocumentAuthor")
    captured = 0

    for step in _SPRINT_STEPS:
        view = SprintStepView(step)
        await channel.send(
            f"{step.emoji} {step.prompt_text}",
            view=view,
        )

        # Wait for either a text reply or a skip
        def check(m):
            return m.author.id == user.id and m.channel.id == channel.id

        try:
            reply = await bot.wait_for("message", check=check, timeout=180)
        except Exception:
            # Timeout — move to next step
            await channel.send(
                f"⏩ No response for step {step.step_number} — moving on.",
            )
            continue

        if view.skipped:
            continue

        content = reply.content.strip()
        if not content:
            continue

        # Use DocumentAuthor._classify_input if available, otherwise save as knowledge
        try:
            if doc_author and hasattr(doc_author, "_classify_and_save_remember"):
                await doc_author._classify_and_save_remember(
                    content, user, channel,
                )
                captured += 1
            elif doc_author and hasattr(doc_author, "_save_knowledge_document"):
                doc_author._save_knowledge_document(
                    title=step.title,
                    content=content,
                    category=step.entity_type,
                    tags=f"onboarding,{step.entity_type}",
                    author=user.display_name,
                )
                captured += 1
            else:
                # Fallback: save as raw doc
                _save_raw_capture(step, content, user.display_name)
                captured += 1

            await reply.add_reaction("✅")
        except Exception as e:
            logger.warning("Sprint step %d save failed: %s", step.step_number, e)
            await channel.send("⚠️ Couldn't save that one — try `/remember` later.")

    # Done!
    mark_sprint_complete()
    embed = build_sprint_complete_embed(captured, len(_SPRINT_STEPS), gap_count=gap_count)
    await channel.send(embed=embed)

    return {"captured": captured, "total": len(_SPRINT_STEPS)}


def _save_raw_capture(step: SprintStep, content: str, author: str) -> Path:
    """Fallback: save capture as a raw markdown file in docs/onboarding/."""
    import config as app_config

    docs_root = Path(app_config.directory_path)
    out_dir = docs_root / "onboarding"
    out_dir.mkdir(parents=True, exist_ok=True)

    slug = step.entity_type
    filename = f"{datetime.utcnow().strftime('%Y-%m-%d')}_{slug}.md"
    filepath = out_dir / filename

    frontmatter = "\n".join([
        "---",
        f"title: {step.title}",
        f"category: {step.entity_type}",
        "source: onboarding_sprint",
        f"author: {author}",
        f"created_at: {datetime.utcnow().isoformat()}Z",
        "---",
        "",
    ])
    body = f"## {step.title}\n\n{content}\n"
    filepath.write_text(frontmatter + body, encoding="utf-8")
    logger.info("Saved onboarding capture: %s", filepath)
    return filepath


# ── Sprint step data access (for admin UI) ───────────────────────────────────

def get_sprint_steps() -> List[Dict[str, Any]]:
    """Return sprint step metadata for the admin UI."""
    return [
        {
            "step_number": s.step_number,
            "title": s.title,
            "emoji": s.emoji,
            "hint": s.hint_text,
            "entity_type": s.entity_type,
            "example": s.example,
        }
        for s in _SPRINT_STEPS
    ]
