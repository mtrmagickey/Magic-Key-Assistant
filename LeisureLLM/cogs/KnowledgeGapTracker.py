"""
Knowledge Gap Tracking and Interview System
Detects what the bot doesn't know, accumulates questions, enables partner Q&A sessions
"""

import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks
from ux_helpers import ConfirmView, ProgressCard, create_error_embed, create_info_embed, create_success_embed

import config
from config import PARTNER_ROLE_IDS, PARTNER_USER_IDS

logger = logging.getLogger(__name__)

# ── Local LLM defaults ──────────────────────────────────────
_OLLAMA_ENDPOINT = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


def _default_ollama_model() -> str:
    """Read the initial-role model from model_router.json, or fall back to a sensible default."""
    try:
        cfg_path = Path(__file__).parent.parent / "config" / "model_router.json"
        if cfg_path.exists():
            with open(cfg_path) as f:
                cfg = json.load(f)
            model = cfg.get("pipeline", {}).get("roles", {}).get("initial", {}).get("model")
            if model:
                return model
    except Exception as e:
        logger.warning("_default_ollama_model: suppressed %s", e)
    return "qwen2.5:32b"


_OLLAMA_MODEL = _default_ollama_model()


def _normalize_task_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


async def _call_ollama_chat(
    prompt: str, *, temperature: float = 0.7, max_tokens: int = 500
) -> str:
    """Call Ollama chat API (fully local). Used by both the Cog and UI Modals."""
    payload = {
        "model": _OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": 4096,          # Enough for gap-tracker prompts (default 2048 is tight)
            "repeat_penalty": 1.1,    # Prevent repetition common in local models
            "top_k": 40,
            "top_p": 0.9,
        },
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{_OLLAMA_ENDPOINT}/api/chat", json=payload) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"Ollama error {resp.status}: {error[:200]}")
            data = await resp.json()
            return (data.get("message", {}).get("content", "")).strip()


# Timezone for scheduled reminders
EASTERN = ZoneInfo("America/New_York")

# Bounty board (knowledge-gap curriculum)
BOUNTY_BOARD_CHANNEL_NAME = "bounty-board"
BOUNTY_THRESHOLD_TIMES_ASKED = 5
BOUNTY_POST_COOLDOWN_DAYS = 7
BOUNTY_MAX_PER_RUN = 1

# Gamification (points are awarded only on durable outcomes)
POINTS_GAP_RESOLVED_INTERVIEW = 5
POINTS_BOUNTY_CLAIM_BONUS = 3


def build_fallback_prompt(gap: Dict[str, Any]) -> Dict[str, Any]:
    topic = (gap.get("topic") or "").strip()
    base_question = (gap.get("question") or "").strip()
    context = (gap.get("context") or "").strip()
    context_line = f"Context: {context}" if context else ""

    # Mix of operational + story-driven + fun questions
    followups = [
        "What actually happens today when someone needs to do this? Walk me through it.",
        "Tell me about a time this went really well — what made it work?",
        "Tell me about a time this failed or almost failed — what did you learn?",
        "Can you show me a real example — a name, a date, a number, a file?",
        "Who's the go-to person for this, and what makes them so good at it?",
        "What would surprise a newcomer about how we handle this?",
        "If I had to do this tomorrow with zero context, what's step one?",
        "What's the unwritten rule about this that everyone knows but isn't documented?",
    ]

    interview_prompt = "\n".join(
        [
            f"Topic: {topic}" if topic else "",
            f"Primary: {base_question}" if base_question else "Primary: (no question captured)",
            context_line,
            "Follow-ups:",
            *[f"- {q}" for q in followups],
        ]
    ).strip()

    return {
        "topic": topic,
        "primary": base_question or "(no question captured)",
        "followups": followups,
        "interview_prompt": interview_prompt,
    }


def classify_gap_curation(topic: str, question: str, context: str) -> tuple[str, str]:
    """Classify whether a gap should be shown in interviews.

    Returns (curation_status, curation_reason).

    This is intentionally conservative: it prefers deferring low-signal / MBA-ish / tautological
    prompts so they don't dominate partner interview time.
    """

    t = (topic or "").strip()
    q = (question or "").strip()
    c = (context or "").strip()

    if not q:
        return ("defer", "auto:empty question")

    tl = t.lower()
    ql = q.lower()
    cl = c.lower()

    # META-DOCUMENTATION LOOP DETECTION
    # Questions about documentation maintenance/location spiral into infinite loops.
    # Defer questions that are about the documentation itself rather than the knowledge.
    meta_doc_patterns = (
        "who is the primary owner",
        "who maintains",
        "who is responsible for maintaining",
        "when was the documentation last updated",
        "when was it last updated",
        "when was the last update",
        "what is the specific location of the official documentation",
        "what is the specific file path",
        "where is the official documentation",
        "where can i find the official documentation",
        "what is the process for updating",
        "what is the process for reviewing",
        "are there any specific constraints or requirements for maintaining",
        "who else is involved in the documentation process",
        "what is the source of truth for this documentation",
        "what are the specific responsibilities",
        "can you provide the name and role of the person responsible",
    )
    for pattern in meta_doc_patterns:
        if pattern in ql:
            return ("defer", f"auto:meta-documentation loop ('{pattern[:40]}...')")

    # Also defer if topic indicates it's already a recursively-spawned question about docs
    if "open question:" in tl and any(
        kw in ql for kw in ("documentation", "file path", "primary owner", "last updated", "who maintains")
    ):
        return ("defer", "auto:recursive documentation question")

    # Grounding signals: if present, we generally keep (unless it's clearly nonsense).
    grounded = False
    if any(ch.isdigit() for ch in q):
        grounded = True
    if any(x in q for x in ("/", "\\", "#", "http://", "https://")):
        grounded = True
    if re.search(r"\.[a-z0-9]{2,5}(\b|$)", ql):
        grounded = True
    if any(
        kw in ql
        for kw in (
            "source of truth",
            "file path",
            "doc",
            "channel",
            "link",
            "error",
            "stack trace",
            "log",
            "config",
            "acceptance criteria",
            "non-negotiable",
            "constraint",
            "who owns",
        )
    ):
        grounded = True

    # If a dimension appears in the QUESTION but is absent from (topic+context), it's likely invented.
    introduced_axes = (
        "latency",
        "bandwidth",
        "throughput",
        "kpi",
        "kpis",
        "metrics",
        "impact",
        "roi",
        "synergy",
    )
    source_text = (tl + " " + cl)
    for axis in introduced_axes:
        if axis in ql and axis not in source_text:
            return ("defer", f"auto:invented axis '{axis}' (not in topic/context)")

    # Tautology / solution-fishing patterns that waste partner time.
    optimize_verbs = (
        "optimize",
        "optimise",
        "minimize",
        "minimise",
        "reduce",
        "improve",
        "increase",
        "achieve",
        "maximize",
        "maximise",
    )
    abstract_axes = (
        "latency",
        "bandwidth",
        "performance",
        "reliability",
        "efficiency",
        "quality",
        "kpi",
        "metrics",
        "impact",
        "seamless",
        "integration",
    )

    if any(v in ql for v in optimize_verbs) and any(a in ql for a in abstract_axes) and not grounded:
        return ("defer", "auto:solution-fishing (needs measured state/target/source)")

    # Classic low-signal MBA-ish phrasing.
    if (
        ("seamless" in ql and not grounded)
        or ("synergy" in ql)
        or ("holistic" in ql)
        or ("paradigm" in ql)
        or ("optimization landscape" in ql)
        or ("discuss" in ql and "challenges" in ql and not grounded)
    ):
        return ("defer", "auto:low-signal phrasing")

    # Otherwise keep.
    return ("keep", "")


async def insert_gap(
    conn,
    *,
    topic: str,
    question: str,
    context: str,
    priority_score: int,
    curation_status: str,
    curation_reason: str,
) -> None:
    """Insert a knowledge gap with best-effort curation metadata.

    Uses INSERT OR IGNORE and then best-effort UPDATE to attach curation fields.
    """

    # Base insert (newer schemas have curation_status/curation_reason; older ones might not).
    try:
        await conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_gaps
                (topic, question, context, priority_score, curation_status, curation_reason)
            VALUES
                (?, ?, ?, ?, ?, ?)
            """,
            (topic, question, context, int(priority_score), curation_status, curation_reason or None),
        )
    except Exception:
        await conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_gaps
                (topic, question, context, priority_score)
            VALUES
                (?, ?, ?, ?)
            """,
            (topic, question, context, int(priority_score)),
        )
        # Best-effort attach curation metadata if the columns exist.
        try:
            await conn.execute(
                """
                UPDATE knowledge_gaps
                SET curation_status = COALESCE(curation_status, ?),
                    curation_reason = COALESCE(curation_reason, ?)
                WHERE topic = ? AND question = ? AND context = ?
                """,
                (curation_status, curation_reason or None, topic, question, context),
            )
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

# Partner role IDs - now imported from config.py
# PARTNER_ROLE_IDS = [...]  # Centralized in config.py

# Optional explicit partner user IDs - now imported from config.py
# PARTNER_USER_IDS = [...]  # Centralized in config.py

# Check if user is a partner
def is_partner(interaction: discord.Interaction) -> bool:
    """Check if user has partner role"""
    if not interaction.user.roles:
        return False
    return any(role.id in PARTNER_ROLE_IDS for role in interaction.user.roles)


def is_admin(interaction: discord.Interaction) -> bool:
    """Basic admin gate for admin-facing slash commands."""
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and getattr(perms, "administrator", False))


async def is_owner_interaction(interaction: discord.Interaction) -> bool:
    try:
        return await interaction.client.is_owner(interaction.user)
    except Exception:
        return False


class SkipNoteModal(discord.ui.Modal, title="Skip Question"):
    """Modal for collecting an optional note when skipping an interview question.

    Note: Discord modals only support TextInput components, so curation choice is done
    via a dropdown in the view (Keep / Defer / Discard).
    """

    reason = discord.ui.TextInput(
        label="Optional note (why skip / how to fix question)",
        style=discord.TextStyle.paragraph,
        placeholder="e.g., 'Not relevant', 'Already covered in <doc>', 'Rewrite to ask about X'",
        required=False,
        max_length=500,
    )

    def __init__(self, view_ref: "SkipDispositionView"):
        super().__init__()
        self.view_ref = view_ref

    async def on_submit(self, interaction: discord.Interaction):
        note = (self.reason.value or "").strip()
        self.view_ref.note = note
        await interaction.response.send_message(
            "✅ Noted. Now pick a disposition and click **Apply**.",
            ephemeral=True,
        )


class SkipDispositionView(discord.ui.View):
    def __init__(self, gap_id: int, gap_topic: str, session_id: int, db_pool, user: discord.User):
        super().__init__(timeout=600)
        self.gap_id = gap_id
        self.gap_topic = gap_topic
        self.session_id = session_id
        self.db_pool = db_pool
        self.user = user
        self.disposition = "keep"
        self.note: str = ""

        self.select = discord.ui.Select(
            placeholder="Choose what happens to this gap",
            options=[
                discord.SelectOption(label="Keep (ask again later)", value="keep"),
                discord.SelectOption(label="Defer (hide from interviews)", value="defer"),
                discord.SelectOption(label="Discard (bad question / irrelevant)", value="discard"),
            ],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        """Handle errors gracefully instead of showing 'This interaction failed'"""
        logger.error(f"SkipDispositionView error: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Something went wrong. Please try again.", ephemeral=True)
        except Exception as e:
            logger.warning("on_error: suppressed %s", e)

    async def on_timeout(self):
        """Handle view timeout gracefully"""
        pass  # View expired, no action needed

    async def _on_select(self, interaction: discord.Interaction):
        self.disposition = (self.select.values[0] if self.select.values else "keep")
        await interaction.response.send_message(
            f"Selected: **{self.disposition}**. Add an optional note or click **Apply**.",
            ephemeral=True,
        )

    @discord.ui.button(label="📝 Add note", style=discord.ButtonStyle.secondary)
    async def add_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != getattr(self.user, "id", None):
            await interaction.response.send_message("This control isn't for you.", ephemeral=True)
            return
        await interaction.response.send_modal(SkipNoteModal(self))

    @discord.ui.button(label="✅ Apply", style=discord.ButtonStyle.primary)
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != getattr(self.user, "id", None):
            await interaction.response.send_message("This control isn't for you.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        disp = (self.disposition or "keep").strip().lower()
        if disp not in ("keep", "defer", "discard"):
            disp = "keep"

        now_ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        note_text = (self.note or "").strip()

        # Map to legacy status column (which is CHECK constrained in some DBs).
        # - keep/defer => open
        # - discard => resolved (dismissed)
        if disp == "discard":
            new_status = "resolved"
            resolved_via = "dismissed"
            resolved_at = datetime.utcnow()
            status_msg = "discarded (won't ask again)"
        else:
            new_status = "open"
            resolved_via = None
            resolved_at = None
            status_msg = "deferred (hidden)" if disp == "defer" else "kept open"

        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE knowledge_gaps
                    SET status = ?,
                        resolved_at = COALESCE(?, resolved_at),
                        resolved_via = COALESCE(?, resolved_via),
                        curation_status = ?,
                        curation_reason = COALESCE(?, curation_reason),
                        curated_at = ?,
                        curated_by_user_id = ?,
                        curated_by_username = ?,
                        notes = COALESCE(notes, '') || ?
                    WHERE id = ?
                    """,
                    (
                        new_status,
                        resolved_at,
                        resolved_via,
                        disp,
                        note_text if note_text else None,
                        now_ts,
                        interaction.user.id,
                        str(interaction.user),
                        f"\n\n[Curated by {interaction.user.display_name} on {now_ts}] disposition={disp} note={note_text or '—'}",
                        self.gap_id,
                    ),
                )

                # If we skipped an in-progress gap, ensure it won't remain stuck.
                # (Next Question puts it into in_progress.)
                if disp in ("keep", "defer"):
                    await conn.execute(
                        "UPDATE knowledge_gaps SET status = 'open' WHERE id = ? AND status = 'in_progress'",
                        (self.gap_id,),
                    )

                # Nudge priority down for defer/discard so it doesn't keep resurfacing.
                if disp in ("defer", "discard"):
                    await conn.execute(
                        """
                        UPDATE knowledge_gaps
                        SET priority_score = CASE WHEN priority_score >= 5 THEN priority_score - 5 ELSE 0 END
                        WHERE id = ?
                        """,
                        (self.gap_id,),
                    )

                await conn.commit()

            await interaction.followup.send(
                f"✅ Curated gap **#{self.gap_id}** ({self.gap_topic[:60]}): {status_msg}.\n"
                "Click **Next Question** to continue.",
                ephemeral=True,
            )
            self.stop()
        except Exception as e:
            logger.error(f"Failed to curate skipped gap: {e}")
            await interaction.followup.send(
                f"❌ Failed to curate gap: {str(e)[:200]}",
                ephemeral=True,
            )


class InterviewModal(discord.ui.Modal, title="Answer Knowledge Gap"):
    """Modal for collecting detailed answers during interviews"""
    
    answer = discord.ui.TextInput(
        label="Your Answer",
        style=discord.TextStyle.paragraph,
        # Discord hard limit: placeholder must be <= 100 chars.
        placeholder=(
            "Include names, dates, numbers, constraints, and the source of truth (file/path/channel/link)."
        ),
        required=True,
        max_length=4000,
    )
    
    def __init__(self, question: str, interview_prompt: str, context: str, gap_id: int, session_id: int):
        # Keep the question visible without violating component limits.
        # Modal title limit is 45 chars; TextInput label mutation is deprecated in newer discord.py.
        question_text = (question or "").strip().replace("\n", " ")
        title_prefix = "Answer: "
        max_question_len = max(0, 45 - len(title_prefix))
        if len(question_text) > max_question_len:
            question_text = question_text[: max(0, max_question_len - 3)].rstrip() + "..."
        modal_title = (title_prefix + question_text)[:45] if question_text else "Answer Knowledge Gap"

        super().__init__(title=modal_title)
        self.question = question
        self.interview_prompt = interview_prompt
        self.context = context
        self.gap_id = gap_id
        self.session_id = session_id
    
    async def _detect_and_create_action_items(
        self, 
        bot, 
        conn, 
        memo_content: str, 
        gap_id: int, 
        user: discord.User,
        memo_path: str
    ):
        """Detect actionable tasks in memo content and auto-create action items"""
        try:
            existing_gap_task_count = 0
            async with conn.execute(
                """
                SELECT COUNT(*)
                FROM tasks t
                JOIN action_gap_links agl ON agl.action_id = t.id
                WHERE agl.gap_id = ?
                  AND t.status IN ('todo', 'in_progress', 'blocked')
                """,
                (gap_id,),
            ) as cur:
                row = await cur.fetchone()
                existing_gap_task_count = int(row[0] or 0) if row else 0

            if existing_gap_task_count > 0:
                logger.info(
                    "Skipping auto-generated action items for gap #%s because open linked tasks already exist",
                    gap_id,
                )
                return

            action_detection_prompt = f"""Analyze this interview memo and identify any clear, actionable tasks that should be done.

MEMO CONTENT:
{memo_content}

Extract ONLY tasks that are:
- Explicit commitments or operational follow-ups stated in the memo
- Concrete and actionable (not vague intentions, aspirations, reminders, or ideas)
- Relevant for immediate or near-term action
- Important enough that someone would be annoyed if it were missing from the task list

Do NOT invent tasks from implied next steps. If the memo is mostly reference knowledge, return [].
Return at most 2 tasks.

Format each task as a JSON object with:
- "title": Brief task description (under 100 chars)
- "description": More detail if needed
- "priority": "low", "medium", "high", or "urgent"
- "suggested_owner": Username if mentioned in memo, or null

Return a JSON array of tasks. If no actionable tasks found, return empty array [].

Example:
[
  {{"title": "Update pricing doc with new rates", "description": "Reflect 2026 pricing in client proposal template", "priority": "high", "suggested_owner": "Alex"}},
  {{"title": "Schedule follow-up with Acme Corp", "description": "Discuss Q1 deliverables timeline", "priority": "medium", "suggested_owner": null}}
]

Tasks:"""
            
            result_text = await _call_ollama_chat(
                action_detection_prompt, temperature=0.3, max_tokens=500
            )
            
            # Parse JSON response
            import json
            # Remove markdown code blocks if present
            if result_text.startswith("```"):
                result_text = "\n".join(result_text.split("\n")[1:-1])
            
            tasks = json.loads(result_text)
            
            if not isinstance(tasks, list):
                return

            existing_open_titles: set[str] = set()
            async with conn.execute(
                "SELECT title FROM tasks WHERE status IN ('todo', 'in_progress', 'blocked')"
            ) as cur:
                rows = await cur.fetchall()
                existing_open_titles = {
                    _normalize_task_title((row[0] if isinstance(row, (list, tuple)) else row["title"]))
                    for row in (rows or [])
                    if (row[0] if isinstance(row, (list, tuple)) else row["title"])
                }
            
            # Create action items for each detected task
            created_count = 0
            seen_titles: set[str] = set()
            for task in tasks[:2]:
                title = task.get('title', '').strip()
                if not title or len(title) < 10:
                    continue
                normalized_title = _normalize_task_title(title)
                if normalized_title in seen_titles or normalized_title in existing_open_titles:
                    continue
                
                description = task.get('description', '')
                priority = task.get('priority', 'medium')
                if priority not in ('low', 'medium', 'high', 'urgent'):
                    priority = 'medium'
                if priority == 'low':
                    continue
                
                # Create action item
                now_utc = datetime.utcnow().isoformat() + "Z"
                notes = f"Auto-generated from knowledge gap #{gap_id} interview\nMemo: {memo_path}\nCreated by: {user.display_name}"
                
                cursor = await conn.execute(
                    """
                    INSERT INTO tasks (
                        title, description, status, priority, 
                        created_by_user_id, created_by_username,
                        tags, notes, created_at, updated_at
                    ) VALUES (?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        description or f"From interview: {title}",
                        priority,
                        user.id,
                        user.display_name,
                        json.dumps(["action_item", f"gap_{gap_id}", "auto_generated"]),
                        notes,
                        now_utc,
                        now_utc
                    )
                )
                
                action_id = cursor.lastrowid
                
                # Link action item to knowledge gap
                await conn.execute(
                    """
                    INSERT INTO action_gap_links (action_id, gap_id, link_type, notes)
                    VALUES (?, ?, 'resolves', 'Auto-generated from interview answer')
                    """,
                    (action_id, gap_id)
                )
                
                seen_titles.add(normalized_title)
                created_count += 1
            
            if created_count > 0:
                logger.info(f"Auto-created {created_count} action items from gap #{gap_id} interview")
        
        except Exception as e:
            logger.warning(f"Failed to auto-detect action items from memo: {e}")
    
    async def on_submit(self, interaction: discord.Interaction):
        """Process the answer and generate memo"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # Get cogs
        bot = interaction.client
        doc_author = bot.get_cog("DocumentAuthor")
        
        if not doc_author:
            await interaction.followup.send("❌ DocumentAuthor cog not loaded", ephemeral=True)
            return
        
        # Create progress card
        progress = ProgressCard(
            title="Processing Your Answer",
            description=f"**Question:** {self.question[:100]}...",
            color=discord.Color.blue()
        )
        
        # Send to thread/channel
        if isinstance(interaction.channel, discord.Thread):
            msg = await interaction.channel.send(embed=progress.embed)
            progress.message = msg
        else:
            await progress.send(interaction.channel)
        
        try:
            # Generate memo from answer
            await progress.update_status("Generating memo from your answer...")
            
            memo_topic = f"Knowledge: {self.question[:80]}"
            
            # Use LLM to structure the memo
            structuring_prompt = f"""You are helping document durable operational knowledge from a partner interview.

CRITICAL FIDELITY RULES (non-negotiable):
- Do NOT add new facts, names, dates, numbers, tools, outcomes, motivations, or conclusions that are not explicitly present in the text below.
- You may rephrase for clarity, but you must not generalize beyond what is stated.
- If something would be useful but is not explicitly stated, put it in ## Open Questions.
- In ## Key Points, every bullet must include a short verbatim quote from the PARTNER ANSWER in parentheses as evidence.

INTERVIEW PROMPT (what we asked):
{self.interview_prompt}

KNOWN CONTEXT:
{self.context}

PARTNER ANSWER:
{self.answer.value}

Write a professional memo suitable for future retrieval with EXACTLY these headings:

## Summary
<1–2 unambiguous sentences>

## Key Points
- <bullets with concrete details: names/roles, systems/tools, dates, numbers, constraints>

## Source of Truth
- <where to find canonical doc/channel/file/link>

## Decisions / Defaults
- <what we do unless stated otherwise>

## Open Questions
- <only truly unresolved OPERATIONAL questions about the topic itself; if none, write: - None>
- Phrase questions in a "childlike" ground-level way: "What actually happens if X breaks?", "Why do we do it this way?", "Walk me through step one."
- NEVER ask meta-documentation questions like: "Who maintains this documentation?", "When was it last updated?", "What is the file path of the documentation?", "Who is responsible for maintaining this?"
- These create infinite loops. Focus on actionable knowledge gaps about the actual work, not about the documentation process.

## Tags
- <3–6 short keywords>

Keep it under 450 words. Be factual and actionable."""
            
            memo_content = await _call_ollama_chat(
                structuring_prompt, temperature=0.2, max_tokens=800
            )
            
            if not memo_content:
                await progress.fail("LLM failed to generate memo")
                return
            
            # Save memo using DocumentAuthor
            await progress.update_status("Saving memo to knowledge base...")
            
            # Directly save without approval (interview answers are trusted)
            slug = re.sub(r"[^a-z0-9_]+", "_", memo_topic.lower().replace(" ", "_"))[:50]
            docs_root = Path(__file__).resolve().parent.parent / "docs"
            date_path = docs_root / "interview" / datetime.now().strftime("%Y/%m")
            date_path.mkdir(parents=True, exist_ok=True)
            
            filename = f"{datetime.now().strftime('%Y-%m-%d')}_{slug}.md"
            memo_path = date_path / filename

            def _extract_tags(text: str) -> List[str]:
                m = re.search(
                    r"^##\s+Tags\s*(.*?)(?=^##\s+|\Z)",
                    text or "",
                    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
                )
                if not m:
                    return []
                block = (m.group(1) or "").strip()
                out: List[str] = []
                for raw in block.splitlines():
                    s = raw.strip()
                    if not s:
                        continue
                    if s.startswith(("- ", "* ", "• ")):
                        s = s[2:].strip()
                    parts = [p.strip() for p in s.split(",") if p.strip()]
                    for p in parts:
                        if p and p.lower() != "none" and p not in out:
                            out.append(p)
                return out

            def _derive_tags_from_key_points(text: str) -> List[str]:
                m = re.search(
                    r"^##\s+Key Points\s*(.*?)(?=^##\s+|\Z)",
                    text or "",
                    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
                )
                block = (m.group(1) or "") if m else ""

                tokens: List[str] = []
                for raw in block.splitlines():
                    s = raw.strip()
                    if not s:
                        continue
                    if s.startswith(("- ", "* ", "• ")):
                        s = s[2:].strip()
                    tokens.extend(re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-/]{2,}", s))

                stop = {
                    "the","and","for","with","from","this","that","are","was","were","will","have","has","had",
                    "into","onto","over","under","when","where","what","why","how","who","we","our","you","your",
                    "not","but","use","used","using","keep","make","made","should","must","can","may","then",
                }
                counts: Dict[str, int] = {}
                for t in tokens:
                    key = t.strip("_-/").lower()
                    if len(key) < 4 or key in stop:
                        continue
                    counts[key] = counts.get(key, 0) + 1

                ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                return [k for k, _ in ranked[:6]]

            tags = _extract_tags(memo_content)
            if not tags:
                tags = _derive_tags_from_key_points(memo_content)

            meta = {
                "topic": memo_topic,
                "source": "partner_interview",
                "interview_session_id": int(self.session_id),
                "gap_id": int(self.gap_id),
                "answered_by": interaction.user.display_name,
                "created_at": f"{datetime.utcnow().isoformat()}Z",
                "status": "current",
                "tags": tags,
            }

            frontmatter = "---\n" + yaml.safe_dump(
                meta,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            ) + "---\n\n"
            
            full_content = frontmatter + memo_content
            
            with open(memo_path, 'w', encoding='utf-8') as f:
                f.write(full_content)
            
            await progress.add_field("Memo Path", str(memo_path.relative_to(docs_root)))

            # Parse open questions into follow-up gaps (self-healing)
            def _extract_open_questions(text: str) -> List[str]:
                lines = [ln.rstrip() for ln in (text or "").splitlines()]
                start = None
                for i, ln in enumerate(lines):
                    if ln.strip().lower() == "## open questions":
                        start = i + 1
                        break
                if start is None:
                    return []
                out: List[str] = []
                for ln in lines[start:]:
                    s = ln.strip()
                    if s.startswith("## "):
                        break
                    if not s:
                        continue
                    if s.startswith(("- ", "• ", "* ")):
                        q = s[2:].strip() if s[1] == " " else s[1:].strip()
                        if q and q.lower() != "none":
                            out.append(q)
                # Deduplicate, preserve order
                seen = set()
                uniq: List[str] = []
                for q in out:
                    key = q.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append(q)
                return uniq

            open_questions = _extract_open_questions(memo_content)

            # ── Corpus-quality: configurable sufficiency thresholds ──
            try:
                from core.config_loader import WorkflowConfig
                _wf_suf = WorkflowConfig.load()
                _min_words = _wf_suf.cq_memo_min_word_count
                _req_sot = _wf_suf.cq_memo_require_source_of_truth
                _req_kp = _wf_suf.cq_memo_require_key_points
            except Exception:
                _min_words = 50
                _req_sot = True
                _req_kp = 3

            def _is_sufficient(text: str) -> tuple[bool, List[str]]:
                missing: List[str] = []
                lower = (text or "").lower()
                if _req_sot and "## source of truth" not in lower:
                    missing.append("Source of Truth section")
                if "## decisions / defaults" not in lower:
                    missing.append("Decisions / Defaults section")

                # require at least N bullets under Key Points
                key_points_match = re.search(r"## key points\s*(.*?)\n## ", text, flags=re.IGNORECASE | re.DOTALL)
                key_points_block = key_points_match.group(1) if key_points_match else ""
                bullets = [ln.strip() for ln in key_points_block.splitlines() if ln.strip().startswith(("- ", "• ", "* "))]
                if len(bullets) < _req_kp:
                    missing.append(f"At least {_req_kp} concrete key-point bullets")

                # require minimum word count
                word_count = len((text or "").split())
                if word_count < _min_words:
                    missing.append(f"More detail (memo is {word_count} words, minimum {_min_words})")

                return (len(missing) == 0), missing

                return (len(missing) == 0), missing

            sufficient, missing_bits = _is_sufficient(memo_content)
            
            # Update gap + record follow-ups in database
            await progress.update_status("Updating knowledge gaps...")
            
            async with bot.db.acquire() as conn:
                now = datetime.utcnow().isoformat() + "Z"

                # Fetch times_asked once for scoring (if column exists)
                times_asked = 1
                try:
                    async with conn.execute(
                        "SELECT times_asked FROM knowledge_gaps WHERE id = ?",
                        (self.gap_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row and row[0] is not None:
                            times_asked = int(row[0])
                except Exception:
                    times_asked = 1

                if sufficient:
                    await conn.execute(
                        """
                        UPDATE knowledge_gaps
                        SET status = 'resolved',
                            resolved_at = ?,
                            resolved_via = 'interview',
                            memo_path = ?
                        WHERE id = ?
                        """,
                        (now, str(memo_path), self.gap_id),
                    )

                    # Award points for resolving a gap via interview (idempotent)
                    try:
                        await conn.execute(
                            """
                            INSERT OR IGNORE INTO partner_point_events (
                                partner_user_id, partner_username, entity_type, entity_id, reason, points
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                int(interaction.user.id),
                                getattr(interaction.user, "display_name", None) or str(interaction.user),
                                "knowledge_gap",
                                int(self.gap_id),
                                "gap_resolved_interview",
                                int(POINTS_GAP_RESOLVED_INTERVIEW),
                            ),
                        )

                        if times_asked >= int(BOUNTY_THRESHOLD_TIMES_ASKED):
                            await conn.execute(
                                """
                                INSERT OR IGNORE INTO partner_point_events (
                                    partner_user_id, partner_username, entity_type, entity_id, reason, points
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    int(interaction.user.id),
                                    getattr(interaction.user, "display_name", None) or str(interaction.user),
                                    "knowledge_gap",
                                    int(self.gap_id),
                                    "bounty_claimed_bonus",
                                    int(POINTS_BOUNTY_CLAIM_BONUS),
                                ),
                            )
                    except Exception as e:
                        logger.warning(f"Failed awarding points for resolved gap #{self.gap_id}: {e}")
                else:
                    # Re-open gap and bump priority if the memo is missing key elements.
                    await conn.execute(
                        """
                        UPDATE knowledge_gaps
                        SET status = 'open',
                            last_asked = ?,
                            priority_score = priority_score + 2,
                            memo_path = ?
                        WHERE id = ?
                        """,
                        (now, str(memo_path), self.gap_id),
                    )

                    # ── Corpus-quality: recursion depth & open-question cap ──
                    try:
                        from core.config_loader import WorkflowConfig
                        _wf = WorkflowConfig.load()
                        _max_depth = _wf.cq_followup_max_recursion_depth
                        _max_oq = _wf.cq_followup_max_open_questions
                    except Exception:
                        _max_depth = 3
                        _max_oq = 5

                    # Parse current depth from the parent gap's context string.
                    _depth_match = re.search(r"\[depth:(\d+)\]", self.context or "")
                    _current_depth = int(_depth_match.group(1)) if _depth_match else 0
                    _child_depth = _current_depth + 1

                    # Split into targeted follow-up gaps based on what's missing.
                    followup_questions: List[str] = []
                    if any("Source of Truth" in m for m in missing_bits):
                        followup_questions.append(
                            f"Where is the source of truth for: {self.question}? (doc/file path/channel/link)"
                        )
                    if any("key-point" in m.lower() for m in missing_bits):
                        followup_questions.append(
                            f"Provide 3+ concrete details for: {self.question} (names, dates, numbers, constraints)."
                        )
                    if any("Decisions" in m for m in missing_bits):
                        followup_questions.append(
                            f"What are the default decisions / rules of thumb for: {self.question}?"
                        )

                    if _child_depth > _max_depth:
                        logger.info(
                            "Skipping %d follow-up gaps for gap #%s: recursion depth %d > max %d",
                            len(followup_questions), self.gap_id, _child_depth, _max_depth,
                        )
                        followup_questions = []

                    for fq in followup_questions[:3]:
                        follow_topic = ("Follow-up: " + (self.question[:80] if self.question else "Interview"))
                        follow_context = (
                            f"Auto-generated follow-up from interview memo {memo_path} "
                            f"[depth:{_child_depth}]"
                        )
                        cur, reason = classify_gap_curation(follow_topic, fq, follow_context)
                        await insert_gap(
                            conn,
                            topic=follow_topic,
                            question=fq,
                            context=follow_context,
                            priority_score=(2 if cur == "keep" else 0),
                            curation_status=cur,
                            curation_reason=reason,
                        )

                # Follow-up gap generation from the memo's Open Questions section
                # Apply both recursion-depth cap and open-question-per-memo cap.
                _oq_to_create = open_questions[:_max_oq]
                if _child_depth > _max_depth:
                    logger.info(
                        "Skipping %d open-question gaps for gap #%s: recursion depth %d > max %d",
                        len(_oq_to_create), self.gap_id, _child_depth, _max_depth,
                    )
                    _oq_to_create = []

                for oq in _oq_to_create:
                    gap_topic = f"Open Question: {self.question[:80]}"
                    gap_context = (
                        f"Generated from Open Questions in interview memo {memo_path} "
                        f"[depth:{_child_depth}]"
                    )
                    cur, reason = classify_gap_curation(gap_topic, oq, gap_context)
                    await insert_gap(
                        conn,
                        topic=gap_topic,
                        question=oq,
                        context=gap_context,
                        priority_score=(1 if cur == "keep" else 0),
                        curation_status=cur,
                        curation_reason=reason,
                    )
                
                # Record in interview_questions
                await conn.execute("""
                    INSERT INTO interview_questions
                    (session_id, gap_id, question, answer, answered_at, memo_generated, memo_path, order_index)
                    VALUES (?, ?, ?, ?, ?, 1, ?, 
                        (SELECT COALESCE(MAX(order_index), 0) + 1 FROM interview_questions WHERE session_id = ?))
                """, (self.session_id, self.gap_id, self.question, 
                self.answer.value, now, str(memo_path), self.session_id))
                
                # Update session stats
                await conn.execute("""
                    UPDATE interview_sessions
                    SET questions_answered = questions_answered + 1,
                        memos_created = memos_created + 1
                    WHERE id = ?
                """, (self.session_id,))
                
                # Auto-detect actionable items from memo and create action items
                await self._detect_and_create_action_items(
                    bot, conn, memo_content, self.gap_id, interaction.user, str(memo_path)
                )
                
                await conn.commit()
            
            # Track partner engagement
            gap_tracker = bot.get_cog("KnowledgeGapTracker")
            if gap_tracker:
                await gap_tracker._track_answer_received(
                    interaction.user.id, 
                    interaction.user.display_name
                )
            
            # Trigger reindex
            await progress.update_status("Reindexing knowledge base...")

            reindexed_ok = False
            if doc_author and hasattr(doc_author, "_trigger_incremental_ingest"):
                try:
                    await doc_author._trigger_incremental_ingest()
                    reindexed_ok = True
                except Exception as e:
                    logger.warning(f"Interview reindex failed (will retry later): {e}")

            if sufficient:
                await progress.complete(
                    "✅ Knowledge captured and reindexed!" if reindexed_ok else "✅ Memo saved; reindex queued"
                )
            else:
                await progress.complete(
                    "✅ Memo saved; follow-ups generated (gap remains open)"
                    + ("; reindexed" if reindexed_ok else "; reindex queued")
                )

            if sufficient:
                msg = (
                    "✅ Thanks! Memo created and successfully reindexed."
                    if reindexed_ok
                    else "✅ Thanks! Memo created; reindex will retry shortly."
                )
            else:
                missing_list = "\n".join([f"- {m}" for m in missing_bits[:6]])
                msg = (
                    "✅ Saved what you shared, but I need a bit more to fully close this gap.\n\n"
                    "Missing elements:\n"
                    f"{missing_list}\n\n"
                    + (
                        "Reindexed successfully. "
                        if reindexed_ok
                        else "Memo saved; reindex will retry shortly. "
                    )
                    + "I created follow-up gaps automatically; you can keep going with **Next Question**."
                )

            gap_tracker = bot.get_cog("KnowledgeGapTracker")
            partner_display = (
                getattr(interaction.user, "display_name", None)
                or getattr(interaction.user, "name", None)
                or "partner"
            )
            view = InterviewView(
                self.session_id,
                bot.db,
                prompt_builder=getattr(gap_tracker, "_build_probing_prompt", None) if gap_tracker else None,
                partner_display_name=partner_display,
            )

            await interaction.followup.send(
                msg + "\n\n**Next Question?** Use the button below.",
                view=view,
                ephemeral=True,
            )
            
        except Exception as e:
            logger.error(f"Failed to process interview answer: {e}")
            await progress.fail(f"Error: {str(e)[:100]}")
            await interaction.followup.send(
                f"❌ Failed to process answer: {str(e)[:200]}",
                ephemeral=True
            )


class InterviewView(discord.ui.View):
    """Interactive interview session UI"""
    
    def __init__(
        self,
        session_id: int,
        db_pool,
        prompt_builder: Optional[
            Callable[[Dict[str, Any], str], Awaitable[Dict[str, Any]]]
        ] = None,
        partner_display_name: str = "partner",
    ):
        super().__init__(timeout=3600)  # 1 hour timeout
        self.session_id = session_id
        self.db_pool = db_pool
        self.prompt_builder = prompt_builder
        self.partner_display_name = partner_display_name
        self.current_gap = None

    async def _present_next_question(self, interaction: discord.Interaction) -> None:
        """Present the next knowledge gap question and update session state.

        Note: caller should have already deferred the interaction response.
        """
        # Get next unanswered gap
        async with self.db_pool.acquire() as conn:
            async with conn.execute(
                """
                SELECT id, topic, question, context, times_asked, priority_score
                FROM knowledge_gaps
                WHERE status = 'open'
                  AND COALESCE(curation_status, 'keep') = 'keep'
                ORDER BY
                    (last_asked IS NULL) DESC,
                    (
                        CASE
                            WHEN question GLOB '*[0-9]*' THEN 2
                            WHEN question LIKE '%/%' OR question LIKE '%.%' OR question LIKE '%#%' OR question LIKE '%:%' THEN 1
                            ELSE 0
                        END
                    ) DESC,
                    priority_score DESC,
                    times_asked DESC,
                    last_asked DESC
                LIMIT 15
                """
            ) as cursor:
                candidates = await cursor.fetchall()

            # Selection logic: avoid immediately repeating the same gap or topic if possible
            gap = None
            current_id = self.current_gap[0] if self.current_gap else -1
            current_topic = self.current_gap[1] if self.current_gap else ""

            # Filter out the immediately previous gap (if it was skipped/kept)
            valid_candidates = [r for r in (candidates or []) if r[0] != current_id]

            if valid_candidates:
                # 1. Prefer a different topic to keep the interview fresh
                for c in valid_candidates:
                    if c[1] != current_topic:
                        gap = c
                        break
                
                # 2. Fallback to highest priority available
                if not gap:
                    gap = valid_candidates[0]

            if not gap:
                await interaction.followup.send(
                    "🎉 No more knowledge gaps! You've answered everything I was curious about.",
                    ephemeral=True,
                )

                # Mark session complete
                await conn.execute(
                    """
                    UPDATE interview_sessions
                    SET status = 'completed',
                        completed_at = ?
                    WHERE id = ?
                    """,
                    (datetime.utcnow(), self.session_id),
                )
                await conn.commit()

                # Disable buttons
                for child in self.children:
                    child.disabled = True

                return

            # Mark gap as in_progress
            await conn.execute(
                """
                UPDATE knowledge_gaps
                SET status = 'in_progress'
                WHERE id = ?
                """,
                (gap[0],),
            )

            # Update session
            await conn.execute(
                """
                UPDATE interview_sessions
                SET questions_asked = questions_asked + 1
                WHERE id = ?
                """,
                (self.session_id,),
            )

            await conn.commit()

        # Show question in modal
        self.current_gap = gap

        # Convert tuple to dict for easier access
        gap_dict = {
            'id': gap[0],
            'topic': gap[1],
            'question': gap[2],
            'context': gap[3],
            'times_asked': gap[4],
            'priority_score': gap[5]
        }

        # Build a probing interview prompt (LLM-assisted if available, deterministic fallback).
        prompt_payload: Dict[str, Any]
        if self.prompt_builder:
            try:
                prompt_payload = await self.prompt_builder(gap_dict, self.partner_display_name)
            except Exception as e:
                logger.warning(f"Prompt builder failed; using fallback: {e}")
                prompt_payload = self._fallback_prompt(gap_dict)
        else:
            prompt_payload = self._fallback_prompt(gap_dict)

        context_text = gap_dict['context'] or "No additional context"
        times_text = f"(Asked {gap_dict['times_asked']} time{'s' if gap_dict['times_asked'] != 1 else ''})"

        followups = prompt_payload.get("followups") or []
        followup_block = "\n".join([f"- {q}" for q in followups[:6]])
        interview_prompt_text = prompt_payload.get("interview_prompt") or prompt_payload.get("primary") or gap_dict['question']
        primary_question = prompt_payload.get("primary") or gap_dict['question']

        embed = create_info_embed(
            title="Knowledge Gap Question",
            description=(
                f"**Topic:** {gap_dict['topic']}\n\n"
                f"**Primary:** {primary_question}\n\n"
                f"**Follow-ups (answer any that you can):**\n{followup_block}\n\n"
                f"{times_text}\n\n"
                "*Click 'Answer' below to share your knowledge.*"
            )
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

        modal = InterviewModal(
            question=primary_question,
            interview_prompt=interview_prompt_text,
            context=context_text,
            gap_id=gap_dict['id'],
            session_id=self.session_id
        )
        await interaction.followup.send(
            "Click to answer:",
            view=AnswerButtonView(
                modal,
                gap_id=gap_dict['id'],
                session_id=self.session_id,
                db_pool=self.db_pool,
                parent_view=self,
            ),
            ephemeral=True,
        )

    def _fallback_prompt(self, gap: Dict[str, Any]) -> Dict[str, Any]:
        return build_fallback_prompt(gap)
    
    @discord.ui.button(label="📝 Next Question", style=discord.ButtonStyle.primary)
    async def next_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Present next knowledge gap question"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self._present_next_question(interaction)
            
        except Exception as e:
            logger.error(f"Failed to get next question: {e}")
            await interaction.followup.send(
                f"❌ Error loading next question: {str(e)[:200]}",
                ephemeral=True
            )
    
    @discord.ui.button(label="⏭️ Skip Question", style=discord.ButtonStyle.secondary)
    async def skip_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip the current question with optional reason"""
        if not self.current_gap:
            await interaction.response.send_message(
                "No question loaded yet. Click 'Next Question' first.",
                ephemeral=True
            )
            return
        
        # Show explicit curation controls (dropdown). Modals only support text inputs.
        view = SkipDispositionView(
            gap_id=self.current_gap[0],
            gap_topic=self.current_gap[1],
            session_id=self.session_id,
            db_pool=self.db_pool,
            user=interaction.user,
        )
        await interaction.response.send_message(
            f"⏭️ Skipping **#{self.current_gap[0]}** — {self.current_gap[1][:80]}\n\n"
            "Pick what to do with this question:",
            view=view,
            ephemeral=True,
        )
    
    @discord.ui.button(label="⏸️ Pause Session", style=discord.ButtonStyle.secondary)
    async def pause_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pause interview session"""
        await interaction.response.send_message(
            "Session paused. Use `/interview` again to resume anytime!",
            ephemeral=True
        )
        
        # Mark any in_progress gaps back to open
        await self.db_pool.execute("""
            UPDATE knowledge_gaps
            SET status = 'open'
            WHERE status = 'in_progress'
            """)
        self.stop()
    
    @discord.ui.button(label="🛑 End Interview", style=discord.ButtonStyle.danger)
    async def end_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        """End interview session"""
        async with self.db_pool.acquire() as conn:
            async with conn.execute("""
                SELECT questions_asked, questions_answered, memos_created
                FROM interview_sessions
                WHERE id = ?
            """, (self.session_id,)) as cursor:
                stats = await cursor.fetchone()
            
            await conn.execute("""
                UPDATE interview_sessions
                SET status = 'completed',
                    completed_at = ?
                WHERE id = ?
            """, (datetime.utcnow(), self.session_id))
            
            # Mark any in_progress gaps back to open
            await conn.execute("""
                UPDATE knowledge_gaps
                SET status = 'open'
                WHERE status = 'in_progress'
            """)
            await conn.commit()
        
        # Convert stats tuple to values
        questions_asked = stats[0] if stats else 0
        questions_answered = stats[1] if stats else 0
        memos_created = stats[2] if stats else 0
        
        await interaction.response.send_message(
            f"✅ Interview session complete!\n\n"
            f"**Questions Presented:** {questions_asked}\n"
            f"**Answered:** {questions_answered}\n"
            f"**Memos Created:** {memos_created}\n\n"
            f"Thanks for filling in the knowledge gaps! 🙏",
            ephemeral=True
        )
        
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Handle view errors gracefully."""
        logger.error(f"InterviewView error: {error}", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "⚠️ Something went wrong. Please try again or start a new interview session.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "⚠️ Something went wrong. Please try again or start a new interview session.",
                    ephemeral=True
                )
        except Exception as e:
            logger.warning("on_error: suppressed %s", e)

    async def on_timeout(self) -> None:
        """Handle view timeout - clean up session state."""
        logger.info(f"InterviewView timed out for session {self.session_id}")
        try:
            async with self.db_pool.acquire() as conn:
                # Mark any in_progress gaps back to open
                await conn.execute("""
                    UPDATE knowledge_gaps
                    SET status = 'open'
                    WHERE status = 'in_progress'
                """)
                await conn.commit()
        except Exception as e:
            logger.error(f"Error cleaning up on timeout: {e}")


class AnswerButtonView(discord.ui.View):
    """Simple view to show answer button"""
    
    def __init__(
        self,
        modal: InterviewModal,
        gap_id: int,
        session_id: int,
        db_pool,
        parent_view: Optional[InterviewView] = None,
    ):
        super().__init__(timeout=600)
        self.modal = modal
        self.gap_id = int(gap_id)
        self.session_id = int(session_id)
        self.db_pool = db_pool
        self.parent_view = parent_view
    
    @discord.ui.button(label="✍️ Answer", style=discord.ButtonStyle.success)
    async def answer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show answer modal"""
        await interaction.response.send_modal(self.modal)
        self.stop()

    @discord.ui.button(label="🗑️ Discard question", style=discord.ButtonStyle.danger)
    async def discard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Discard this interview question so it stops appearing."""
        await interaction.response.defer(ephemeral=True)

        try:
            async with self.db_pool.acquire() as conn:
                # Mark as resolved/dismissed so it stops appearing in all queries.
                await conn.execute(
                    """
                    UPDATE knowledge_gaps
                    SET status = 'resolved',
                        resolved_at = ?,
                        resolved_via = 'dismissed',
                        curation_status = 'discard',
                        curation_reason = ?,
                        curated_at = ?,
                        curated_by_user_id = ?,
                        curated_by_username = ?
                    WHERE id = ?
                    """,
                    (
                        datetime.utcnow(),
                        "manual:discard from interview",
                        datetime.utcnow(),
                        str(getattr(interaction.user, "id", "")),
                        getattr(interaction.user, "display_name", None)
                        or getattr(interaction.user, "name", None)
                        or "unknown",
                        self.gap_id,
                    ),
                )
                await conn.commit()

            await interaction.followup.send(
                "🗑️ Discarded. Auto-loading the next question…",
                ephemeral=True,
            )

            # Auto-advance to the next question (reusing the parent InterviewView so Skip stays in sync).
            if self.parent_view:
                await self.parent_view._present_next_question(interaction)
            else:
                await interaction.followup.send(
                    "Click **Next Question** to continue.",
                    ephemeral=True,
                )
        except Exception as e:
            logger.error(f"Failed to discard interview question gap_id={self.gap_id}: {e}")
            await interaction.followup.send(
                f"❌ Failed to discard question: {str(e)[:200]}",
                ephemeral=True,
            )
        finally:
            self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Handle view errors gracefully."""
        logger.error(f"AnswerButtonView error: {error}", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "⚠️ Something went wrong processing your action. Please try again.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "⚠️ Something went wrong processing your action. Please try again.",
                    ephemeral=True
                )
        except Exception as e:
            logger.warning("on_error: suppressed %s", e)

    async def on_timeout(self) -> None:
        """Handle view timeout."""
        logger.debug(f"AnswerButtonView timed out for gap {self.gap_id}")


class KnowledgeGapTracker(commands.Cog):
    """Tracks knowledge gaps and enables partner interview sessions"""
    
    gap = app_commands.Group(name="gap", description="[Partners] Manage knowledge gaps")
    
    def __init__(self, bot):
        self.bot = bot

        # Department identity (used in background posts)
        self.department = {
            "name": "Archivist",
            "emoji": "📚",
            "role": "Librarian who audits the collection, finds weak spots, and writes probing questions that improve the records.",
        }

    async def _call_llm(self, prompt: str, *, temperature: float = 0.7, max_tokens: int = 500) -> str:
        """Call Ollama for question generation (fully local)."""
        return await _call_ollama_chat(prompt, temperature=temperature, max_tokens=max_tokens)

    async def cog_load(self):
        if not self.gap_reminder.is_running():
            self.gap_reminder.start()

        if not self.thursday_partner_prompt.is_running():
            self.thursday_partner_prompt.start()
        
        if not self.weekly_gap_escalation_check.is_running():
            self.weekly_gap_escalation_check.start()

        if not self.bounty_board_post.is_running():
            self.bounty_board_post.start()

        if not self.archivist_shelf_check.is_running():
            self.archivist_shelf_check.start()

        if not self.gap_hygiene_sweep.is_running():
            self.gap_hygiene_sweep.start()

    async def cog_unload(self):
        if self.gap_reminder.is_running():
            self.gap_reminder.cancel()

        if self.thursday_partner_prompt.is_running():
            self.thursday_partner_prompt.cancel()
        
        if self.weekly_gap_escalation_check.is_running():
            self.weekly_gap_escalation_check.cancel()

        if self.bounty_board_post.is_running():
            self.bounty_board_post.cancel()

        if self.archivist_shelf_check.is_running():
            self.archivist_shelf_check.cancel()

        if self.gap_hygiene_sweep.is_running():
            self.gap_hygiene_sweep.cancel()

    def _normalize_gap_text(self, text: str) -> str:
        import re

        t = (text or "").strip().lower()
        t = re.sub(r"\s+", " ", t)
        t = re.sub(r"[^a-z0-9\s\-_/.:]", "", t)

        # Collapse common low-signal lead-ins that create duplicates.
        for prefix in (
            "open question:",
            "open question",
            "question:",
            "q:",
        ):
            if t.startswith(prefix):
                t = t[len(prefix):].strip()
                break

        return t

    def _is_low_signal_gap(self, topic: str, question: str) -> bool:
        import re

        q = (question or "").strip()
        t = (topic or "").strip()
        if not q:
            return True

        ql = q.lower()

        # Very short questions are often unusable without more context.
        if len(q) < 35:
            return True

        # Explicit vague patterns we’ve seen recur.
        vague_markers = [
            "we need data",
            "need more data",
            "need more info",
            "need information",
            "any updates",
            "status update",
            "what's the status",
            "whats the status",
            "open question",
            "unclear",
            "tbd",
        ]
        if any(m in ql for m in vague_markers):
            return True

        # If there are no “concreteness” signals, treat as low-signal.
        has_digits = any(ch.isdigit() for ch in q)
        has_pathish = ("/" in q) or ("\\" in q)
        has_file_ext = bool(re.search(r"\.[a-z0-9]{2,4}(\b|$)", ql))
        has_proper_noun_hint = any(tok[:1].isupper() and tok[1:].islower() for tok in q.split()[:12])
        has_topic_signal = len(t) >= 6

        if not (has_digits or has_pathish or has_file_ext or has_proper_noun_hint or has_topic_signal):
            return True

        return False

    async def _run_gap_hygiene_sweep(self, run_date: str, *, dry_run: bool = False) -> Dict[str, int]:
        """Conservatively reduce interview noise by deferring duplicates/low-signal gaps.

        Policy:
        - Only touches gaps where status='open' and curation_status is (NULL|'keep')
        - Prefer defer (reversible) vs discard
        - Never modifies gaps that have been asked repeatedly (times_asked >= 3)
        """
        db = getattr(self.bot, "db", None)
        if not db:
            return {"scanned": 0, "deferred_duplicates": 0, "deferred_low_signal": 0}

        async with db.acquire() as conn, conn.execute(
            """
                SELECT id, topic, question, priority_score, times_asked, last_asked
                FROM knowledge_gaps
                WHERE status = 'open'
                  AND COALESCE(curation_status, 'keep') = 'keep'
                ORDER BY priority_score DESC, times_asked DESC, datetime(last_asked) DESC
                """
        ) as cur:
            rows = [dict(r) for r in (await cur.fetchall() or [])]

        scanned = len(rows)
        if not rows:
            return {"scanned": 0, "deferred_duplicates": 0, "deferred_low_signal": 0}

        # === Pass 1: exact/near-exact duplicates by normalized question ===
        by_norm: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            norm = self._normalize_gap_text(str(r.get("question") or ""))
            if not norm:
                continue
            by_norm.setdefault(norm, []).append(r)

        defer_duplicate_ids: List[int] = []
        for norm, group in by_norm.items():
            if len(group) < 2:
                continue

            # Keep the “best” one, defer the rest.
            def score(g: Dict[str, Any]) -> tuple:
                return (
                    int(g.get("priority_score") or 0),
                    int(g.get("times_asked") or 1),
                    str(g.get("last_asked") or ""),
                    -int(g.get("id") or 0),
                )

            group_sorted = sorted(group, key=score, reverse=True)
            keep = group_sorted[0]
            keep_id = int(keep.get("id") or 0)
            for g in group_sorted[1:]:
                gid = int(g.get("id") or 0)
                if gid and gid != keep_id:
                    defer_duplicate_ids.append(gid)

        # === Pass 2: low-signal gaps (but only if they haven't been asked much) ===
        defer_low_signal_ids: List[int] = []
        for r in rows:
            gid = int(r.get("id") or 0)
            if not gid or gid in defer_duplicate_ids:
                continue

            times_asked = int(r.get("times_asked") or 1)
            if times_asked >= 3:
                continue

            if self._is_low_signal_gap(str(r.get("topic") or ""), str(r.get("question") or "")):
                defer_low_signal_ids.append(gid)

        if dry_run:
            return {
                "scanned": scanned,
                "deferred_duplicates": len(defer_duplicate_ids),
                "deferred_low_signal": len(defer_low_signal_ids),
            }

        # Apply updates (chunk to avoid SQLite variable limits)
        async def _chunked(ids: List[int], chunk_size: int = 200) -> List[List[int]]:
            return [ids[i:i + chunk_size] for i in range(0, len(ids), chunk_size)]

        deferred_duplicates = 0
        deferred_low_signal = 0

        db = getattr(self.bot, "db", None)
        if not db:
            return {"scanned": scanned, "deferred_duplicates": 0, "deferred_low_signal": 0}

        async with db.acquire() as conn:
            try:
                for batch in await _chunked(defer_duplicate_ids):
                    if not batch:
                        continue
                    placeholders = ",".join(["?"] * len(batch))
                    await conn.execute(
                        f"""
                        UPDATE knowledge_gaps
                        SET curation_status = 'defer',
                            curation_reason = COALESCE(curation_reason, 'auto:hygiene duplicate'),
                            curated_at = datetime('now'),
                            curated_by_username = 'Steward',
                            priority_score = CASE WHEN priority_score >= 2 THEN priority_score - 2 ELSE 0 END
                        WHERE id IN ({placeholders})
                          AND status = 'open'
                          AND COALESCE(curation_status, 'keep') = 'keep'
                        """,
                        tuple(int(x) for x in batch),
                    )
                    deferred_duplicates += len(batch)

                for batch in await _chunked(defer_low_signal_ids):
                    if not batch:
                        continue
                    placeholders = ",".join(["?"] * len(batch))
                    await conn.execute(
                        f"""
                        UPDATE knowledge_gaps
                        SET curation_status = 'defer',
                            curation_reason = COALESCE(curation_reason, 'auto:hygiene low-signal'),
                            curated_at = datetime('now'),
                            curated_by_username = 'Steward',
                            priority_score = CASE WHEN priority_score >= 2 THEN priority_score - 2 ELSE 0 END
                        WHERE id IN ({placeholders})
                          AND status = 'open'
                          AND COALESCE(curation_status, 'keep') = 'keep'
                        """,
                        tuple(int(x) for x in batch),
                    )
                    deferred_low_signal += len(batch)

                await conn.commit()
            except Exception as e:
                logger.warning(f"Gap hygiene sweep failed: {e}")

        return {
            "scanned": scanned,
            "deferred_duplicates": deferred_duplicates,
            "deferred_low_signal": deferred_low_signal,
        }

    @tasks.loop(time=dt_time(hour=11, minute=10, tzinfo=EASTERN))
    async def gap_hygiene_sweep(self):
        """Daily: auto-defer duplicate/low-signal gaps to keep interviews high-signal."""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return

        now_local = datetime.now(EASTERN)
        run_date = now_local.date().isoformat()

        try:
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("knowledge_gap_hygiene_sweep", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"Gap hygiene idempotency failed; continuing: {e}")

        try:
            stats = await self._run_gap_hygiene_sweep(run_date=run_date)
            logger.info(
                "Gap hygiene sweep: scanned=%s deferred_duplicates=%s deferred_low_signal=%s",
                stats.get("scanned"),
                stats.get("deferred_duplicates"),
                stats.get("deferred_low_signal"),
            )
        except Exception as e:
            logger.warning(f"Gap hygiene sweep crashed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_hygiene_sweep", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
            return
        finally:
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_hygiene_sweep", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    @gap.command(
        name="cleanup",
        description="[Partners] Run automated knowledge-gap cleanup (duplicates/low-signal)",
    )
    @app_commands.describe(
        confirm="Must be true to apply changes (otherwise shows a preview)",
    )
    async def gaps_cleanup(self, interaction: discord.Interaction, confirm: bool = False):
        if not (is_partner(interaction) or is_admin(interaction)):
            await interaction.response.send_message(
                "This command is for partners/admins.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database is not available.", ephemeral=True)
            return

        run_date = datetime.now(EASTERN).date().isoformat()
        try:
            stats = await self._run_gap_hygiene_sweep(run_date=run_date, dry_run=not confirm)
            scanned = int(stats.get("scanned") or 0)
            dupes = int(stats.get("deferred_duplicates") or 0)
            low = int(stats.get("deferred_low_signal") or 0)

            if not confirm:
                await interaction.followup.send(
                    f"Preview (no changes applied):\n"
                    f"- scanned: {scanned}\n"
                    f"- would defer duplicates: {dupes}\n"
                    f"- would defer low-signal: {low}\n\n"
                    "Re-run with `confirm:true` to apply.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                f"✅ Cleanup applied:\n"
                f"- scanned: {scanned}\n"
                f"- deferred duplicates: {dupes}\n"
                f"- deferred low-signal: {low}",
                ephemeral=True,
            )
        except Exception as e:
            logger.warning(f"gaps_cleanup failed: {e}")
            await interaction.followup.send(
                f"❌ Cleanup failed: {str(e)[:200]}",
                ephemeral=True,
            )

    def _get_archivist_channel(self) -> Optional[discord.TextChannel]:
        """Where the Archivist posts background health checks.

        Per your preference: keep Archivist chatter in #partners-assistant.
        We resolve by ID first, then by name as a fallback.
        """
        try:
            bots_id = getattr(config, "BOTS_OFFICE_CHANNEL_ID", None)
            if bots_id:
                ch = self.bot.get_channel(int(bots_id))
                if ch:
                    return ch
        except Exception as e:
            logger.warning("_get_archivist_channel: suppressed %s", e)

        try:
            return discord.utils.get(self.bot.get_all_channels(), name="bots-office")
        except Exception:
            return None

    def _archivist_probe_pool(self) -> List[str]:
        """A rotating bank of probing questions (designed to produce durable records)."""
        return [
            "Where is the source of truth (file path / doc / channel / link)?",
            "What is the default process we follow, in 3-5 steps?",
            "What is the main exception case, and how do we handle it?",
            "What are the non-negotiables / constraints / failure modes?",
            "What does 'done' look like (acceptance criteria / deliverables)?",
            "Who owns this area today (primary + backup)?",
            "What is the current state as of today (dates if relevant)?",
            "Give one concrete example (project + date + tooling + outcome).",
            "What decision changed this most recently (and when)?",
            "What is the terminology we use internally (exact names/labels)?",
            "What should never be assumed here (common misconception to avoid)?",
        ]

    def _select_archivist_probes(self, *, gap_id: int, run_date: str) -> List[str]:
        """Choose 3 probes deterministically to avoid weekly repetition."""
        pool = self._archivist_probe_pool()
        must = pool[0]
        rest = pool[1:]
        rng = random.Random(f"archivist:{run_date}:{gap_id}")
        picks = rng.sample(rest, k=min(2, len(rest)))
        return [must, *picks]

    async def _build_archivist_report(self, *, run_date: Optional[str] = None) -> Dict[str, Any]:
        """Compute a compact collection-health report + probing questions.

        Returns a dict with:
        - text: the message body
        - probed_gap_ids: list[int]
        - probes_by_gap_id: dict[int, list[str]]
        """
        if run_date is None:
            run_date = datetime.now(EASTERN).date().isoformat()

        if not getattr(self.bot, "db", None):
            return {
                "text": f"{self.department['emoji']} **Archivist Shelf Check**\nDatabase not available.",
                "probed_gap_ids": [],
                "probes_by_gap_id": {},
            }

        async with self.bot.db.acquire() as conn:
            async with conn.execute(
                "SELECT COUNT(*) AS n FROM knowledge_gaps WHERE status = 'open'"
            ) as cur:
                open_gaps = int((await cur.fetchone() or {"n": 0})[0])

            async with conn.execute(
                """
                SELECT COUNT(*)
                FROM knowledge_gaps
                WHERE datetime(first_asked) >= datetime('now', '-7 days')
                """
            ) as cur:
                new_7d = int((await cur.fetchone() or [0])[0])

            async with conn.execute(
                """
                SELECT COUNT(*)
                FROM knowledge_gaps
                WHERE status = 'resolved'
                  AND resolved_at IS NOT NULL
                  AND datetime(resolved_at) >= datetime('now', '-7 days')
                """
            ) as cur:
                resolved_7d = int((await cur.fetchone() or [0])[0])

            async with conn.execute(
                """
                SELECT COUNT(*)
                FROM response_feedback
                WHERE feedback = 'not_helpful'
                  AND datetime(created_at) >= datetime('now', '-7 days')
                """
            ) as cur:
                not_helpful_7d = int((await cur.fetchone() or [0])[0])

            # Avoid tautology: don't keep surfacing the same gap if Archivist recently probed it.
            # We use the escalations table as a generic cooldown ledger.
            async with conn.execute(
                """
                SELECT g.id, g.topic, g.question, g.context, g.times_asked, g.last_asked
                FROM knowledge_gaps g
                WHERE g.status = 'open'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM escalations e
                    WHERE e.entity_type = 'knowledge_gap'
                      AND e.entity_id = g.id
                      AND e.reason = 'archivist_probe'
                      AND datetime(e.escalated_at) >= datetime('now', '-21 days')
                  )
                ORDER BY g.priority_score DESC, g.times_asked DESC, datetime(g.last_asked) DESC
                LIMIT 8
                """
            ) as cur:
                top_open = [dict(r) for r in (await cur.fetchall() or [])]

        lines: List[str] = []
        lines.append(f"{self.department['emoji']} **Archivist Shelf Check**")
        lines.append("I’m auditing our records for weak spots and missing canon.")
        lines.append("")
        lines.append(f"- Open knowledge gaps: **{open_gaps}**")
        lines.append(f"- New gaps (7d): **{new_7d}** | Resolved (7d): **{resolved_7d}**")
        lines.append(f"- ‘Not helpful’ answers (7d): **{not_helpful_7d}**")

        probed_gap_ids: List[int] = []
        probes_by_gap_id: Dict[int, List[str]] = {}

        if top_open:
            lines.append("")
            lines.append("**Top gaps to patch (with probing questions):**")
            for g in top_open[:3]:
                gap_id = int(g.get("id") or 0)
                topic = (g.get("topic") or "").strip() or "(untitled topic)"
                q = (g.get("question") or "").strip()
                times_asked = int(g.get("times_asked") or 1)

                probes = self._select_archivist_probes(gap_id=gap_id, run_date=run_date)
                probed_gap_ids.append(gap_id)
                probes_by_gap_id[gap_id] = probes
                lines.append(f"- **{topic}** (asked {times_asked}x)")
                for p in probes:
                    lines.append(f"  - Probe: {p}")
                if q:
                    lines.append(f"  - Original: {q[:180]}")

        lines.append("")
        lines.append("If you reply with sources/paths, I’ll turn it into an extractive memo and re-index.")
        return {
            "text": "\n".join(lines)[:1950],
            "probed_gap_ids": probed_gap_ids,
            "probes_by_gap_id": probes_by_gap_id,
        }

    @tasks.loop(time=dt_time(hour=11, minute=40, tzinfo=EASTERN))
    async def archivist_shelf_check(self):
        """Weekly (Mon): post a compact record-strength audit + probing questions."""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return

        now_local = datetime.now(EASTERN)
        # Monday only (weekday: Mon=0)
        if now_local.weekday() != 0:
            return

        run_date = now_local.date().isoformat()
        try:
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("archivist_shelf_check", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"Archivist idempotency failed; continuing: {e}")

        channel = self._get_archivist_channel()
        if not channel:
            return

        try:
            report = await self._build_archivist_report(run_date=run_date)
            await channel.send(str(report.get("text") or ""), allowed_mentions=discord.AllowedMentions.none())

            # Record which gaps we probed so we don't repeat ourselves.
            try:
                gap_ids = [int(x) for x in (report.get("probed_gap_ids") or []) if int(x) > 0]
                probes_by_id = report.get("probes_by_gap_id") or {}
                if gap_ids:
                    async with self.bot.db.acquire() as conn:
                        for gid in gap_ids:
                            payload = {
                                "run_date": run_date,
                                "probes": probes_by_id.get(gid, []),
                            }
                            await conn.execute(
                                """
                                INSERT INTO escalations (
                                    entity_type, entity_id, reason,
                                    escalated_to_user_id, escalated_to_username,
                                    escalation_message, escalated_at, status
                                ) VALUES (
                                    'knowledge_gap', ?, 'archivist_probe',
                                    NULL, 'Archivist',
                                    ?, datetime('now'), 'open'
                                )
                                """,
                                (int(gid), json.dumps(payload, ensure_ascii=False)[:1800]),
                            )
                        await conn.commit()
            except Exception as e:
                logger.warning(f"Failed recording archivist probes: {e}")
        except Exception as e:
            logger.warning(f"Archivist shelf check post failed: {e}")
        finally:
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("archivist_shelf_check", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    @app_commands.command(name="archivist_report", description="Archivist: summarize knowledge gaps + propose probing questions.")
    @app_commands.check(is_admin)
    async def archivist_report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            report = await self._build_archivist_report()
            await interaction.followup.send(str(report.get("text") or ""), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Archivist report failed: {str(e)[:120]}", ephemeral=True)

    def _get_bounty_channel(self) -> Optional[discord.TextChannel]:
        try:
            return discord.utils.get(self.bot.get_all_channels(), name=BOUNTY_BOARD_CHANNEL_NAME)
        except Exception:
            return None

    def _get_bounty_target_mention(self, channel: discord.abc.GuildChannel, gap: Dict[str, Any]) -> str:
        """Prefer explicit assignee, else @CTO role, else @Partners."""
        try:
            assigned = gap.get("assigned_to_user")
            if assigned:
                return f"<@{int(assigned)}>"
        except Exception as e:
            logger.warning("_get_bounty_target_mention: suppressed %s", e)

        guild = getattr(channel, "guild", None)
        if guild:
            try:
                role = discord.utils.find(lambda r: (r.name or "").strip().lower() == "cto", guild.roles)
                if role:
                    return role.mention
            except Exception as e:
                logger.warning("_get_bounty_target_mention: suppressed %s", e)

            try:
                partner_role = discord.utils.get(guild.roles, id=PARTNER_ROLE_IDS[0]) if PARTNER_ROLE_IDS else None
                if partner_role:
                    return partner_role.mention
            except Exception as e:
                logger.warning("_get_bounty_target_mention: suppressed %s", e)

        return "@Partners"

    @tasks.loop(time=dt_time(hour=11, minute=20, tzinfo=EASTERN))
    async def bounty_board_post(self):
        """Daily: post the top repeated open knowledge gap to a Bounty Board channel."""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return

        now_local = datetime.now(EASTERN)
        run_date = now_local.date().isoformat()

        try:
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("knowledge_gap_bounty_board", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"Bounty board idempotency failed; continuing: {e}")

        channel = self._get_bounty_channel() or discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if not channel:
            return

        cooldown_window = f"-{int(BOUNTY_POST_COOLDOWN_DAYS)} days"

        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, topic, question, context, times_asked, last_asked, assigned_to_user
                    FROM knowledge_gaps
                    WHERE status = 'open'
                      AND times_asked >= ?
                    ORDER BY times_asked DESC, datetime(last_asked) DESC
                    LIMIT 15
                    """,
                (int(BOUNTY_THRESHOLD_TIMES_ASKED),),
            ) as cursor:
                candidates = [dict(r) for r in (await cursor.fetchall() or [])]

            if not candidates:
                try:
                    if hasattr(self.bot.db, "complete_job_run"):
                        await self.bot.db.complete_job_run("knowledge_gap_bounty_board", run_date)
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                return

            posted = 0
            for gap in candidates:
                if posted >= BOUNTY_MAX_PER_RUN:
                    break

                gap_id = int(gap["id"])

                # Cooldown per-gap via escalations table (reason=bounty_board_post)
                try:
                    async with self.bot.db.acquire() as conn:
                        async with conn.execute(
                            """
                            SELECT 1
                            FROM escalations
                            WHERE entity_type = 'knowledge_gap'
                              AND entity_id = ?
                              AND reason = 'bounty_board_post'
                              AND datetime(escalated_at) >= datetime('now', ?)
                            LIMIT 1
                            """,
                            (gap_id, cooldown_window),
                        ) as cursor:
                            recent = await cursor.fetchone()
                        if recent:
                            continue
                except Exception as e:
                    # If cooldown check fails, be conservative: skip posting.
                    logger.warning(f"Bounty board cooldown check failed for gap #{gap_id}: {e}")
                    continue

                topic = (gap.get("topic") or "").strip() or "(untitled topic)"
                question = (gap.get("question") or "").strip()
                times_asked = int(gap.get("times_asked") or 1)

                prompt_seed = {
                    "topic": topic,
                    "question": question,
                    "context": (gap.get("context") or "").strip(),
                }
                followups = build_fallback_prompt(prompt_seed).get("followups", [])
                bullets = [q for q in (followups or []) if q][:3]
                if len(bullets) < 3:
                    bullets = [
                        "What is the current state (as of today)?",
                        "What are the key constraints / risks / failure modes?",
                        "Where is the source of truth (doc/file path/channel/link)?",
                    ]

                target = self._get_bounty_target_mention(channel, gap)

                lines: List[str] = []
                lines.append("🏹 **Knowledge Gap Bounty Board**")
                lines.append(f"I’ve been asked **{times_asked}** times about **{topic}**, but I can’t find a document that answers it.")
                lines.append(f"{target}, can you answer these 3 bullet points so I can learn:")
                lines.extend([f"- {b}" for b in bullets])
                if question:
                    lines.append("")
                    lines.append(f"Original question: {question[:220]}")
                lines.append("Reply in-thread or run `/interview` to capture the answer as a memo.")

                msg = "\n".join(lines)[:1950]

                allowed_mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
                try:
                    await channel.send(msg, allowed_mentions=allowed_mentions)
                except Exception as e:
                    logger.warning(f"Failed to post bounty board message for gap #{gap_id}: {e}")
                    continue

                # Record post so we won't re-post within cooldown.
                try:
                    escalated_to_user_id = None
                    try:
                        assigned = gap.get("assigned_to_user")
                        if assigned:
                            escalated_to_user_id = int(assigned)
                    except Exception:
                        escalated_to_user_id = None

                    await self.bot.db.execute(
                        """
                        INSERT INTO escalations (
                        entity_type,
                        entity_id,
                        reason,
                        escalated_to_user_id,
                        escalation_message,
                        status
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                        "knowledge_gap",
                        gap_id,
                        "bounty_board_post",
                        escalated_to_user_id,
                        msg[:900],
                        "dismissed",
                        ),
                        )
                except Exception as e:
                    logger.warning(f"Failed to record bounty board post for gap #{gap_id}: {e}")

                posted += 1

            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_bounty_board", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        except Exception as e:
            logger.error(f"bounty_board_post failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_bounty_board", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    async def _post_reminder(self, text: str):
        """Post reminders to #bots if configured, else weekly-meeting-threads if present."""
        bot = self.bot
        ao = bot.get_cog("AutonomousOps")
        if ao and hasattr(ao, "post_to_bots_channel"):
            try:
                await ao.post_to_bots_channel("manager", text)
                return
            except Exception as e:
                logger.warning(f"Failed to post to bots channel via AutonomousOps: {e}")

        partners_channel = discord.utils.get(bot.get_all_channels(), name="weekly-meeting-threads")
        if partners_channel:
            await partners_channel.send(text)
            return

        logger.info("No reminder channel available (bots channel not configured and weekly-meeting-threads not found).")
    
    async def _track_question_asked(self, partner_user_id: int, partner_username: str):
        """Track that a question was asked to a partner for engagement metrics"""
        if not getattr(self.bot, "db", None):
            return
        
        try:
            from datetime import datetime
            now = datetime.utcnow()
            week_start = (now - timedelta(days=now.weekday())).date().isoformat()
            
            await self.bot.db.execute(
                """
                INSERT INTO partner_engagement (
                partner_user_id, partner_username, questions_asked,
                last_question_at, week_start_date
                ) VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(partner_user_id, week_start_date) DO UPDATE SET
                questions_asked = questions_asked + 1,
                last_question_at = excluded.last_question_at,
                updated_at = datetime('now')
                """,
                (partner_user_id, partner_username, now.isoformat() + "Z", week_start)
                )
        except Exception as e:
            logger.warning(f"Failed to track question asked: {e}")
    
    async def _track_answer_received(self, partner_user_id: int, partner_username: str, response_time_hours: float = None):
        """Track that a partner answered a question for engagement metrics"""
        if not getattr(self.bot, "db", None):
            return
        
        try:
            from datetime import datetime
            now = datetime.utcnow()
            week_start = (now - timedelta(days=now.weekday())).date().isoformat()
            
            async with self.bot.db.acquire() as conn:
                # Update engagement metrics
                await conn.execute(
                    """
                    INSERT INTO partner_engagement (
                        partner_user_id, partner_username, questions_answered, 
                        last_answer_at, week_start_date
                    ) VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(partner_user_id, week_start_date) DO UPDATE SET
                        questions_answered = questions_answered + 1,
                        last_answer_at = excluded.last_answer_at,
                        updated_at = datetime('now')
                    """,
                    (partner_user_id, partner_username, now.isoformat() + "Z", week_start)
                )
                
                # Update response time and rate
                if response_time_hours is not None:
                    await conn.execute(
                        """
                        UPDATE partner_engagement
                        SET avg_response_time_hours = (
                            COALESCE(avg_response_time_hours * (questions_answered - 1), 0) + ?
                        ) / questions_answered,
                        response_rate = (questions_answered * 100.0) / NULLIF(questions_asked, 0)
                        WHERE partner_user_id = ? AND week_start_date = ?
                        """,
                        (response_time_hours, partner_user_id, week_start)
                    )
                
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to track answer received: {e}")

    def _pick_random_partner(self, guild: Optional[discord.Guild]) -> Optional[discord.Member]:
        """Pick a random non-bot member with one of the configured partner roles."""
        if not guild:
            return None
        candidates: List[discord.Member] = []
        for rid in PARTNER_ROLE_IDS:
            role = guild.get_role(rid)
            if not role:
                continue
            for m in getattr(role, "members", []) or []:
                if getattr(m, "bot", False):
                    continue
                candidates.append(m)
        if not candidates:
            return None
        # Deduplicate by id
        unique = {m.id: m for m in candidates}
        return random.choice(list(unique.values()))

    async def _pick_partner_member(self, guild: Optional[discord.Guild]) -> Optional[discord.Member]:
        """Best-effort partner picker: role members first, then curated user ID list via fetch."""
        if not guild:
            return None

        picked = self._pick_random_partner(guild)
        if picked:
            return picked

        # Fallback: try to resolve curated partner IDs even if member cache is cold.
        resolved: List[discord.Member] = []
        for uid in PARTNER_USER_IDS:
            m = guild.get_member(int(uid))
            if m is None:
                try:
                    m = await guild.fetch_member(int(uid))
                except Exception:
                    m = None
            if m and not getattr(m, "bot", False):
                resolved.append(m)

        if not resolved:
            return None

        unique = {m.id: m for m in resolved}
        return random.choice(list(unique.values()))

    def _partners_role_mention(self, guild: Optional[discord.Guild]) -> Optional[str]:
        """Return a role mention string for partners if available."""
        if not guild:
            return None
        for rid in PARTNER_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                return role.mention
        return None

    async def _run_thursday_prompt_once(
        self,
        *,
        now_local: datetime,
        force_mode: str = "auto",
        dry_run: bool = False,
        override_channel: Optional[discord.abc.Messageable] = None,
        allow_non_thursday: bool = False,
        record_idempotency: bool = True,
    ) -> Dict[str, Any]:
        """Core Thursday prompt logic, shared by the scheduler and admin test command."""
        if not getattr(self.bot, "db", None):
            return {"ok": False, "error": "Database not available"}

        if (not allow_non_thursday) and now_local.weekday() != 3:
            return {"ok": False, "error": "Not Thursday (and allow_non_thursday=False)"}

        run_date = now_local.date().isoformat()

        if record_idempotency:
            try:
                if hasattr(self.bot.db, "record_job_run"):
                    recorded = await self.bot.db.record_job_run("knowledge_gap_thursday_prompt", run_date)
                    if not recorded:
                        return {"ok": False, "skipped": True, "error": "Already ran for this date"}
            except Exception as e:
                logger.warning(f"Thursday prompt idempotency check failed; continuing anyway: {e}")

        # Determine rotation mode
        week = int(now_local.isocalendar().week)
        if force_mode not in {"auto", "stale", "priority"}:
            force_mode = "auto"
        mode = ("stale" if (week % 2 == 0) else "priority") if force_mode == "auto" else force_mode

        # Select a gap
        row = None
        async with self.bot.db.acquire() as conn:
            if mode == "stale":
                async with conn.execute(
                    """
                    SELECT id, topic, question, context, times_asked, priority_score
                    FROM knowledge_gaps
                    WHERE status = 'open'
                      AND (last_asked IS NULL OR julianday('now') - julianday(last_asked) >= 7)
                    ORDER BY (last_asked IS NOT NULL) ASC, last_asked ASC, priority_score DESC
                    LIMIT 10
                    """
                ) as cursor:
                    rows = await cursor.fetchall()
                row = random.choice(rows) if rows else None
            else:
                async with conn.execute(
                    """
                    SELECT id, topic, question, context, times_asked, priority_score
                    FROM knowledge_gaps
                    WHERE status = 'open'
                    ORDER BY priority_score DESC, times_asked DESC, last_asked DESC
                    LIMIT 1
                    """
                ) as cursor:
                    row = await cursor.fetchone()

        if not row:
            return {"ok": False, "error": f"No open gaps available for mode={mode}"}

        gap: Dict[str, Any] = {
            "id": row[0],
            "topic": row[1],
            "question": row[2],
            "context": row[3],
            "times_asked": row[4],
            "priority_score": row[5],
        }

        # Deterministic style permutation
        styles = ["operator", "socratic", "red_team", "checklist", "numbers"]
        style_index = (int(gap["id"]) + int(now_local.isocalendar().week)) % len(styles)
        gap["prompt_style"] = styles[style_index]

        # Determine guild + partner mention
        guild: Optional[discord.Guild] = None
        try:
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            if partners_channel and hasattr(partners_channel, "guild"):
                guild = partners_channel.guild
        except Exception:
            guild = None

        partner = await self._pick_partner_member(guild)
        partner_name = getattr(partner, "display_name", None) or "partner"
        prompt_payload = await self._build_probing_prompt(gap, partner_name)

        primary = prompt_payload.get("primary") or gap["question"]
        followups = prompt_payload.get("followups") or []
        followup_block = "\n".join([f"- {q}" for q in followups[:7]])

        role_mention = self._partners_role_mention(guild)
        mention = partner.mention if partner else (role_mention or "@Partners")

        # Reflection snippet: pull recent interview questions and filter in Python for robustness.
        recent_lines: List[str] = []
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT question, answered_at
                    FROM interview_questions
                    WHERE memo_generated = 1 AND answered_at IS NOT NULL
                    ORDER BY answered_at DESC
                    LIMIT 25
                    """
            ) as cursor:
                recent = await cursor.fetchall()

            cutoff = datetime.utcnow() - timedelta(days=14)
            picked: List[str] = []
            for q, answered_at in recent or []:
                if not q or not answered_at:
                    continue
                s = str(answered_at).strip()
                try:
                    if s.endswith("Z"):
                        s = s[:-1]
                    dt = datetime.fromisoformat(s.replace(" ", "T"))
                except Exception:
                    continue
                if dt >= cutoff:
                    picked.append(str(q).strip())
                if len(picked) >= 2:
                    break
            if picked:
                recent_lines.append("Recent learnings (now in RAG):")
                for q in picked:
                    recent_lines.append(f"- {q[:140]}")
        except Exception as e:
            logger.warning(f"Failed to fetch recent learnings: {e}")

        msg = (
            f"🧠 Thursday knowledge capture prompt ({mode} • {gap.get('prompt_style','operator')}) {mention}\n\n"
            f"**Topic:** {gap['topic']}\n"
            f"**Primary:** {primary}\n\n"
            f"**Follow-ups (answer any you can):**\n{followup_block}\n\n"
            + ("\n".join(recent_lines) + "\n\n" if recent_lines else "")
            + "Reply by running `/interview` and clicking **Next Question** (this gap will be in the rotation)."
        )

        if not dry_run:
            # Post to override channel, else weekly-meeting-threads, else fallback reminder path
            posted = False
            if override_channel is not None:
                await override_channel.send(msg)
                posted = True
            if not posted:
                partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
                if partners_channel:
                    await partners_channel.send(msg)
                    posted = True
            if not posted:
                await self._post_reminder(msg)

            # Record assignment + bump urgency slightly
            await self.bot.db.execute(
                """
                UPDATE knowledge_gaps
                SET assigned_to_user = ?,
                last_asked = ?,
                times_asked = times_asked + 1,
                priority_score = priority_score + 1,
                probing_questions_asked = probing_questions_asked + 1,
                last_probing_question_at = ?
                WHERE id = ?
                """,
                (
                int(partner.id) if partner else None,
                datetime.utcnow().isoformat() + "Z",
                datetime.utcnow().isoformat() + "Z",
                int(gap["id"]),
                ),
                )
            # Track question asked for engagement metrics
            if partner:
                await self._track_question_asked(partner.id, partner.display_name)

        return {
            "ok": True,
            "mode": mode,
            "gap_id": int(gap["id"]),
            "topic": gap["topic"],
            "style": gap.get("prompt_style"),
            "selected_partner_id": int(partner.id) if partner else None,
            "message": msg,
        }

    @tasks.loop(time=dt_time(hour=9, minute=0, tzinfo=EASTERN))
    async def gap_reminder(self):
        """Daily reminder for stale open knowledge gaps."""
        if not getattr(self.bot, "db", None):
            return

        today = datetime.now(EASTERN).date().isoformat()
        # Idempotency via job_runs if available
        try:
            already = False
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("knowledge_gap_reminder", today)
                already = not recorded
            if already:
                return
        except Exception as e:
            logger.warning(f"Reminder idempotency check failed; continuing anyway: {e}")

        try:
            async with self.bot.db.acquire() as conn:
                async with conn.execute(
                    """
                    SELECT id, topic, question, last_asked, times_asked, priority_score
                    FROM knowledge_gaps
                    WHERE status IN ('open', 'in_progress')
                      AND COALESCE(curation_status, 'keep') = 'keep'
                      AND julianday('now') - julianday(last_asked) >= 7
                    ORDER BY priority_score DESC, last_asked ASC
                    LIMIT 10
                    """
                ) as cursor:
                    stale = await cursor.fetchall()

                async with conn.execute(
                    """SELECT COUNT(*) FROM knowledge_gaps 
                       WHERE status IN ('open', 'in_progress')
                         AND COALESCE(curation_status, 'keep') = 'keep'"""
                ) as cursor:
                    row = await cursor.fetchone()
                    total_open = int(row[0]) if row else 0

            if not stale:
                # Still complete the job run record
                try:
                    if hasattr(self.bot.db, "complete_job_run"):
                        await self.bot.db.complete_job_run("knowledge_gap_reminder", today)
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                return

            lines = [
                f"🧠 Knowledge gaps reminder: {len(stale)} stale gap(s) (7+ days since last asked).",
                f"Total open/in-progress gaps: {total_open}.",
                "Run `/interview` to answer and auto-generate memos.",
                "",
                "Top stale gaps:",
            ]
            for g in stale[:7]:
                topic = g[1]
                q = g[2]
                score = g[5]
                lines.append(f"- ({score}) {topic}: {q[:160]}")

            await self._post_reminder("\n".join(lines))

            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_reminder", today)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        except Exception as e:
            logger.error(f"gap_reminder failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_reminder", today, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    @tasks.loop(time=dt_time(hour=10, minute=30, tzinfo=EASTERN))
    async def thursday_partner_prompt(self):
        """On Thursdays, proactively ask a probing knowledge-gap question and @ a random partner."""
        now_local = datetime.now(EASTERN)
        run_date = now_local.date().isoformat()
        try:
            result = await self._run_thursday_prompt_once(
                now_local=now_local,
                force_mode="auto",
                dry_run=False,
                override_channel=None,
                allow_non_thursday=False,
                record_idempotency=True,
            )

            if not result.get("ok"):
                if result.get("skipped"):
                    return
                return

            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_thursday_prompt", run_date)
            except Exception as e:
                logger.warning("thursday_partner_prompt: suppressed %s", e)
        except Exception as e:
            logger.error(f"thursday_partner_prompt failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_thursday_prompt", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("thursday_partner_prompt: suppressed %s", e)
    
    @tasks.loop(time=dt_time(hour=11, minute=0, tzinfo=EASTERN))
    async def weekly_gap_escalation_check(self):
        """Weekly check for knowledge gaps with 3+ unanswered probing questions"""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return
        
        now = datetime.now(EASTERN)
        # Only run on Mondays
        if now.weekday() != 0:
            return
        
        run_date = now.date().isoformat()
        try:
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("knowledge_gap_escalation_check", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"Gap escalation check idempotency failed; continuing: {e}")
        
        try:
            # Find gaps with 3+ probing questions but no responses
            escalation_gaps = []
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, topic, question, probing_questions_asked, 
                           last_probing_question_at, response_count, assigned_to_user, escalated
                    FROM knowledge_gaps
                    WHERE status = 'open'
                      AND probing_questions_asked >= 3
                      AND (response_count = 0 OR response_count IS NULL)
                      AND (escalated IS NULL OR escalated = 0)
                    ORDER BY probing_questions_asked DESC, last_probing_question_at ASC
                    LIMIT 15
                    """
            ) as cursor:
                rows = await cursor.fetchall()
                escalation_gaps = [dict(r) for r in rows]
            
            # Escalate unresponsive gaps
            if escalation_gaps:
                partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
                
                lines = []
                lines.append("🚨 **Knowledge Gap Escalations**")
                lines.append(f"The following {len(escalation_gaps)} gaps have received 3+ questions with no partner response:")
                lines.append("")
                
                for gap in escalation_gaps[:10]:
                    assigned = gap.get('assigned_to_user')
                    assigned_txt = f"<@{assigned}>" if assigned else "@Partners"
                    lines.append(f"- **Gap #{gap['id']}** {gap['topic']}")
                    lines.append(f"  *{gap['question'][:100]}...*")
                    lines.append(f"  {gap['probing_questions_asked']} questions asked, 0 responses - Assigned: {assigned_txt}")
                    
                    # Mark as escalated
                    async with self.bot.db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE knowledge_gaps
                            SET escalated = 1
                            WHERE id = ?
                            """,
                            (gap['id'],)
                        )
                        
                        # Log escalation
                        await conn.execute(
                            """
                            INSERT INTO escalations (entity_type, entity_id, reason, escalated_to_user_id, escalation_message)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                'knowledge_gap',
                                gap['id'],
                                'no_response_3_questions',
                                gap.get('assigned_to_user'),
                                f"Knowledge gap #{gap['id']} has 3+ unanswered questions and needs partner attention"
                            )
                        )
                        await conn.commit()
                
                lines.append("")
                lines.append("Please review these gaps via `/gaps list` and provide answers")
                
                msg = "\n".join(lines)[:1950]
                
                if partners_channel:
                    allowed_mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
                    await partners_channel.send(msg, allowed_mentions=allowed_mentions)
                
                await self._post_reminder(f"Gap escalation check: {len(escalation_gaps)} gaps escalated")
            else:
                await self._post_reminder("Gap escalation check: No gaps requiring escalation")
            
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_escalation_check", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        
        except Exception as e:
            logger.error(f"weekly_gap_escalation_check failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("knowledge_gap_escalation_check", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)


    @app_commands.command(
        name="thursday_prompt",
        description="[Admin] Run the Thursday knowledge prompt now (preview or post)",
    )
    @app_commands.describe(
        dry_run="If true, only preview (do not post / do not update DB)",
        mode="auto (weekly rotation), stale, or priority",
        channel="Optional channel override for posting",
    )
    @app_commands.check(is_owner_interaction)
    async def thursday_prompt_admin(
        self,
        interaction: discord.Interaction,
        dry_run: bool = True,
        mode: str = "auto",
        channel: Optional[discord.TextChannel] = None,
    ):
        """Admin helper to test the Thursday prompt end-to-end without waiting for Thursday."""
        await interaction.response.defer(ephemeral=True)

        now_local = datetime.now(EASTERN)
        result = await self._run_thursday_prompt_once(
            now_local=now_local,
            force_mode=(mode or "auto").strip().lower(),
            dry_run=dry_run,
            override_channel=channel,
            allow_non_thursday=True,
            record_idempotency=False,
        )

        if not result.get("ok"):
            await interaction.followup.send(
                f"❌ Thursday prompt test failed: {result.get('error', 'unknown error')}",
                ephemeral=True,
            )
            return

        action = "Preview" if dry_run else "Posted"
        meta = (
            f"**{action}** Thursday prompt\n"
            f"Mode: `{result.get('mode')}` | Style: `{result.get('style')}` | Gap ID: `{result.get('gap_id')}`\n"
            f"Topic: {result.get('topic')}"
        )

        # Keep preview compact (Discord message length safety)
        preview = result.get("message") or "(no message generated)"
        if len(preview) > 1800:
            preview = preview[:1800] + "\n…(truncated)"

        await interaction.followup.send(meta + "\n\n" + preview, ephemeral=True)

    async def _build_probing_prompt(self, gap: Dict[str, Any], partner_display_name: str) -> Dict[str, Any]:
        """Generate a sharp, probing interview prompt for a specific gap (with safe fallback)."""
        fallback = build_fallback_prompt(gap)

        topic = (gap.get("topic") or "").strip()
        base_question = (gap.get("question") or "").strip()
        context = (gap.get("context") or "").strip()

        if not base_question:
            return fallback

        style = (gap.get("prompt_style") or "operator").strip().lower()
        style_instructions = {
            "operator": "Be direct, practical, and concrete. Optimize for durable operational truth.",
            "socratic": "Be curious and clarifying. Seek definitions, boundaries, and examples.",
            "red_team": "Probe for failure modes, edge cases, and what breaks.",
            "checklist": "Phrase follow-ups like an implementation checklist: inputs, outputs, owners, timelines.",
            "numbers": "Press for quantification: costs, time, thresholds, success criteria, typical ranges.",
        }.get(style, "Be direct, practical, and concrete. Optimize for durable operational truth.")

        def _anti_tautology_primary(primary_text: str) -> str:
            """Rewrite common 'solution fishing' primaries into grounded clarifiers.

            Goal: avoid questions that assume a problem exists (e.g., 'minimize latency')
            when the missing knowledge is actually 'is this a problem' + 'what are the numbers'
            + 'where is the source of truth'.
            """

            p = (primary_text or "").strip()
            if not p:
                return p

            pl = p.lower()
            optimize_verbs = (
                "optimize",
                "optimise",
                "minimize",
                "minimise",
                "reduce",
                "improve",
                "increase",
                "achieve",
                "lower",
            )
            abstract_axes = (
                "latency",
                "bandwidth",
                "throughput",
                "performance",
                "reliability",
                "efficiency",
                "quality",
                "kpi",
                "metrics",
                "impact",
            )

            if any(v in pl for v in optimize_verbs) and any(a in pl for a in abstract_axes):
                axis = next((a for a in abstract_axes if a in pl), "this")
                topic_hint = topic if topic else "this area"
                return (
                    f"Is {axis} actually a blocker for {topic_hint} right now? If yes, what is the current measured {axis} "
                    "(include numbers + where measured), what is the target/requirement, and what component dominates?"
                )

            # If the question is framed as 'how to' with a generic goal, push toward grounding.
            if pl.startswith("how to ") and any(a in pl for a in abstract_axes):
                axis = next((a for a in abstract_axes if a in pl), "this")
                topic_hint = topic if topic else "this area"
                return (
                    f"Before we change anything: what is the current measured {axis} for {topic_hint}, what is the target, "
                    "and where is the source-of-truth doc/log/dashboard for those numbers?"
                )

            return p

        prompt = f"""You are an expert operator interviewing a partner ({partner_display_name}) to capture durable business/engineering knowledge.

    Style instruction:
    {style_instructions}

Given the knowledge gap below, produce:
1) A single crisp PRIMARY question, phrased to elicit specifics.
2) 5–7 FOLLOW-UP questions that are probing, practical, and "childlike" (see below).

Rules:
- Make questions answerable: ask for names/roles, dates, numbers, constraints, examples.
- Always include one follow-up asking for the source of truth (doc/file path/channel/link).
- Always include one follow-up asking for a concrete example.
- Avoid generic advice. Avoid fluff.

"CHILDLIKE" QUESTION STYLE (critical — use this tone):
- Instead of "What are the success metrics?", ask "What actually happens if this breaks?"
- Instead of "Who are the stakeholders?", ask "Who actually does this work?"
- Instead of "What is the process?", ask "Walk me through what happens step by step."
- Instead of "What are the requirements?", ask "Why do we do it this way?"
- Ask questions a curious newcomer would ask: "But why?", "What does that look like?", "Show me an example."
- Cut through jargon. If something sounds abstract, ask "What does that actually mean in practice?"

Grounding rules (non-negotiable):
- Do NOT introduce new problem dimensions that are not explicitly present in the Topic, Base question, or Context.
    (Example: do not ask about 'latency' unless latency is mentioned.)
- If the Base question is 'solution fishing' (optimize/minimize/improve), rewrite the PRIMARY into a grounding clarifier:
    current measured state, target/requirement, evidence it is a problem, and source of truth.
- Prefer clarifying questions that prevent wasted partner time.

ANTI-LOOP RULES (critical):
- Do NOT ask meta-documentation questions like: "Who maintains this documentation?", "When was documentation last updated?", "What is the file path of the documentation?", "Who is responsible for maintaining docs?"
- Focus on the TOPIC itself, not on documentation about the topic.
- These meta-questions create infinite loops. If source-of-truth is already answered, do not keep asking about it.

Knowledge gap:
Topic: {topic}
Base question: {base_question}
Context: {context}

Return plain text in exactly this format:
PRIMARY: <one sentence>
FOLLOWUPS:
- <q1>
- <q2>
- ...
"""

        try:
            text = await self._call_llm(prompt)
            if not text:
                return fallback

            primary = ""
            followups: List[str] = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line.upper().startswith("PRIMARY:"):
                    primary = line.split(":", 1)[1].strip()
                    continue
                if line.startswith("-"):
                    q = line.lstrip("-").strip()
                    if q:
                        followups.append(q)

            if not primary:
                primary = fallback["primary"]

            # Anti-tautology safety pass: turn 'how do we minimize X' into 'is X a blocker + what are the numbers'.
            primary = _anti_tautology_primary(primary)
            if len(followups) < 3:
                followups = fallback["followups"]

            interview_prompt = "\n".join(
                [
                    f"Topic: {topic}" if topic else "",
                    f"Primary: {primary}",
                    (f"Context: {context}" if context else ""),
                    "Follow-ups:",
                    *[f"- {q}" for q in followups],
                ]
            ).strip()

            return {
                "topic": topic,
                "primary": primary,
                "followups": followups,
                "interview_prompt": interview_prompt,
                "prompt_style": style,
            }
        except Exception as e:
            logger.warning(f"LLM probing prompt generation failed: {e}")
            return fallback
    
    async def log_knowledge_gap(self, topic: str, question: str, context: str, user_id: int):
        """Log when bot doesn't know something"""
        if not hasattr(self.bot, 'db') or not self.bot.db:
            logger.warning("Database not available for gap tracking")
            return
        
        try:
            async with self.bot.db.acquire() as conn:
                # Check if gap already exists
                async with conn.execute("""
                    SELECT id, times_asked, asked_by_users
                    FROM knowledge_gaps
                    WHERE topic = ? AND question = ? AND status = 'open'
                """, (topic, question)) as cursor:
                    existing = await cursor.fetchone()
                
                if existing:
                    # Update existing gap - existing is a tuple (id, times_asked, asked_by_users_json)
                    import json
                    asked_by_users = json.loads(existing[2]) if existing[2] else []
                    new_users = list(set(asked_by_users + [user_id]))
                    await conn.execute("""
                        UPDATE knowledge_gaps
                        SET times_asked = times_asked + 1,
                            last_asked = ?,
                            asked_by_users = ?,
                            priority_score = times_asked + 1
                        WHERE id = ?
                    """, (datetime.utcnow(), json.dumps(new_users), existing[0]))
                    
                    logger.info(f"Updated knowledge gap: {topic} (now asked {existing[1] + 1} times)")
                else:
                    # Create new gap
                    import json
                    cur, reason = classify_gap_curation(topic, question, context)

                    # If we auto-defer, keep it out of interview rotation by default.
                    # Still store it so it can be reviewed/undeferred if it's actually important.
                    try:
                        await conn.execute(
                            """
                            INSERT INTO knowledge_gaps
                                (topic, question, context, asked_by_users, priority_score, curation_status, curation_reason)
                            VALUES
                                (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                topic,
                                question,
                                context,
                                json.dumps([user_id]),
                                1 if cur == "keep" else 0,
                                cur,
                                reason or None,
                            ),
                        )
                    except Exception:
                        # Backward-compatible insert
                        await conn.execute(
                            """
                            INSERT INTO knowledge_gaps
                                (topic, question, context, asked_by_users, priority_score)
                            VALUES
                                (?, ?, ?, ?, ?)
                            """,
                            (
                                topic,
                                question,
                                context,
                                json.dumps([user_id]),
                                1 if cur == "keep" else 0,
                            ),
                        )

                        # Best-effort attach curation metadata.
                        try:
                            await conn.execute(
                                """
                                UPDATE knowledge_gaps
                                SET curation_status = COALESCE(curation_status, ?),
                                    curation_reason = COALESCE(curation_reason, ?)
                                WHERE topic = ? AND question = ? AND context = ?
                                """,
                                (cur, reason or None, topic, question, context),
                            )
                        except Exception as e:
                            logger.warning("operation: suppressed %s", e)
                    
                    logger.info(f"Logged new knowledge gap: {topic}")
                await conn.commit()
        
        except Exception as e:
            logger.error(f"Failed to log knowledge gap: {e}")

    async def _generate_preemptive_questions(
        self,
        topic: str,
        partner_display_name: str,
        seed_count: int,
    ) -> List[str]:
        """Generate a starter set of interview questions for a topic (safe fallback)."""

        topic = (topic or "").strip()
        seed_count = max(3, min(int(seed_count or 0), 12))

        # Mix of operational, story-driven, and human-interest questions
        fallback = [
            f"What is the source of truth for {topic} (file path / doc / channel / link)?",
            f"Tell me about a time {topic} went surprisingly well - what made it work?",
            f"Tell me about a time {topic} failed or almost failed - what did we learn?",
            f"Who's the go-to person for {topic}, and what makes them uniquely good at it?",
            f"What's a fact about {topic} that would surprise someone new to the team?",
            f"What's the 'unwritten rule' about {topic} that everyone knows but isn't documented?",
            f"If you had to explain {topic} to a smart 12-year-old, how would you describe it?",
        ]

        if not topic:
            return fallback[:seed_count]

        prompt = f"""You are an expert interviewer capturing institutional knowledge for the team.

You're interviewing {partner_display_name} about: {topic}

Generate exactly {seed_count} interview questions that will surface INTERESTING, HUMAN knowledge - not just dry procedures.

Mix these question types:
1. STORIES: "Tell me about a time..." / "What's the craziest thing that happened with..." / "Describe the most memorable..."
2. SURPRISES: "What would surprise a newcomer about..." / "What's the unwritten rule about..."
3. PEOPLE: "Who's the go-to person for..." / "Which partner would you trust most to..." / "Who has the best stories about..."
4. SPECIFICS: "Where's the source of truth?" / "What's the actual file/doc/channel?"
5. FAILURES: "When did this almost go wrong?" / "What lesson did we learn the hard way?"
6. FUN: "Let's play word association - when you think of {topic}, what comes to mind?" / "What would you name a cocktail inspired by {topic}?"

Rules:
- Make 60% story/human questions, 40% operational
- Be conversational, not corporate
- Ask for names, specifics, examples
- Include at least ONE fun/creative question

Return plain text as exactly {seed_count} lines, each starting with "- ".
"""

        try:
            text = await self._call_llm(prompt)
            if not text:
                return fallback[:seed_count]

            questions: List[str] = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("-"):
                    q = line.lstrip("-").strip()
                    if q:
                        questions.append(q)

            # De-dupe while preserving order
            seen = set()
            deduped: List[str] = []
            for q in questions:
                key = q.lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(q)

            if len(deduped) < 3:
                return fallback[:seed_count]

            return deduped[:seed_count]
        except Exception as e:
            logger.warning(f"Preemptive question generation failed; using fallback: {e}")
            return fallback[:seed_count]

    async def _seed_preemptive_gaps(
        self,
        *,
        topic: str,
        questions: List[str],
        asker_user_id: int,
    ) -> int:
        """Insert preemptive gaps so the existing InterviewView can run normally."""

        if not getattr(self.bot, "db", None):
            return 0

        topic = (topic or "").strip()
        if not topic or not questions:
            return 0

        try:
            import json

            inserted = 0
            asked_by_users = json.dumps([int(asker_user_id)])
            context = "Partner-initiated preemptive interview (seed question)"

            async with self.bot.db.acquire() as conn:
                for q in questions:
                    cursor = await conn.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_gaps
                            (topic, question, context, asked_by_users, priority_score, times_asked)
                        VALUES
                            (?, ?, ?, ?, 1, 1)
                        """,
                        (topic, q, context, asked_by_users),
                    )
                    # rowcount is 1 when inserted, 0 when ignored
                    if getattr(cursor, "rowcount", 0) == 1:
                        inserted += 1

                await conn.commit()

            return inserted
        except Exception as e:
            logger.error(f"Failed to seed preemptive gaps: {e}")
            return 0
    
    @app_commands.command(name="interview", description="[Partners] Start Q&A session to fill knowledge gaps")
    @app_commands.describe(
        topic="Optional: run a preemptive interview on a topic (seeds starter questions if no gaps exist)",
        seed_count="How many starter questions to seed when topic is provided (3–12)",
    )
    async def interview(
        self,
        interaction: discord.Interaction,
        topic: Optional[str] = None,
        seed_count: int = 6,
    ):
        """Start interview session for partners to answer accumulated questions"""
        
        # Check if user is partner (you'll need to define PARTNER_ROLE_IDS)
        if not is_partner(interaction):
            await interaction.response.send_message(
                "This command is for team members only.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            if not getattr(self.bot, "db", None):
                await interaction.followup.send(
                    "❌ Database is not available; interviews require the SQLite database to be configured.",
                    ephemeral=True,
                )
                return

            # Check how many gaps exist
            # Use fetchval helper from Database class
            gap_count = await self.bot.db.fetchval(
                """
                SELECT COUNT(*)
                FROM knowledge_gaps
                WHERE status = 'open'
                    AND COALESCE(curation_status, 'keep') = 'keep'
                """
            )
                
            seeded_mode = False
            requested_topic = (topic or "").strip()

            if gap_count == 0 and requested_topic:
                partner_display = getattr(interaction.user, "display_name", None) or getattr(
                    interaction.user, "name", "partner"
                )

                questions = await self._generate_preemptive_questions(
                    requested_topic,
                    partner_display_name=partner_display,
                    seed_count=seed_count,
                )
                inserted = await self._seed_preemptive_gaps(
                    topic=requested_topic,
                    questions=questions,
                    asker_user_id=interaction.user.id,
                )

                # Refresh gap count after seeding.
                gap_count = await self.bot.db.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM knowledge_gaps
                    WHERE status = 'open'
                        AND COALESCE(curation_status, 'keep') = 'keep'
                    """
                )
                seeded_mode = True if inserted > 0 else False

            if gap_count == 0:
                tip = "\n\nTip: you can run `/interview topic:<topic>` to do a proactive interview." \
                    if requested_topic == "" else ""
                await interaction.followup.send(
                    "🎉 Great news! There are no knowledge gaps right now.\n\n"
                    "I'll let you know when I encounter topics I need clarity on."
                    + tip,
                    ephemeral=True,
                )
                return
            
            # Create interview session
            async with self.bot.db.acquire() as conn:
                cursor = await conn.execute("""
                    INSERT INTO interview_sessions
                    (interviewer_user_id, interviewer_username, channel_id, thread_id)
                    VALUES (?, ?, ?, ?)
                """,
                (interaction.user.id,
                str(interaction.user),
                interaction.channel_id,
                interaction.channel.id if isinstance(interaction.channel, discord.Thread) else None))
                session_id = cursor.lastrowid
                await conn.commit()
            
            # Send welcome message with interview controls
            title = "📋 Knowledge Gap Interview Session"
            if seeded_mode and requested_topic:
                title = f"📋 Preemptive Interview: {requested_topic}"

            embed = create_success_embed(
                title=title,
                description=(
                    f"I have **{gap_count} question{'s' if gap_count != 1 else ''}** where your expertise would help!\n\n"
                    "I'll ask them one at a time. Take your time with answers — I'll turn them into memos for the knowledge base.\n\n"
                    "**Ready?** Click 'Next Question' to start."
                ),
            )
            
            partner_display = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "partner")
            view = InterviewView(
                session_id,
                self.bot.db,
                prompt_builder=self._build_probing_prompt,
                partner_display_name=partner_display,
            )
            
            await interaction.followup.send(
                embed=embed,
                view=view,
                ephemeral=True
            )
            
            logger.info(f"Started interview session {session_id} with {interaction.user} ({gap_count} gaps)")
            
        except Exception as e:
            logger.error(f"Failed to start interview: {e}")
            await interaction.followup.send(
                f"❌ Failed to start interview: {str(e)[:200]}",
                ephemeral=True
            )
    
    @gap.command(name="list", description="[Admin] View accumulated knowledge gaps")
    @app_commands.describe(
        status="Filter by status (open, in_progress, resolved)",
        limit="Number of gaps to show"
    )
    @app_commands.check(is_owner_interaction)
    async def knowledge_gaps(
        self,
        interaction: discord.Interaction,
        status: Optional[str] = "open",
        limit: int = 10
    ):
        """View current knowledge gaps"""
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Use fetchall helper from Database class
            query = """
                SELECT id, topic, question, times_asked, priority_score, 
                       first_asked, last_asked, resolved_at, resolved_via
                FROM knowledge_gaps
                WHERE status = ?
                ORDER BY priority_score DESC, last_asked DESC
                LIMIT ?
            """
            
            gaps = await self.bot.db.fetchall(query, status, limit)
            
            if not gaps:
                await interaction.followup.send(
                    f"No {status} knowledge gaps found.",
                    ephemeral=True
                )
                return
            
            # Build embed
            embed = create_info_embed(
                title=f"Knowledge Gaps ({status.title()})",
                description=f"Showing top {len(gaps)} of {len(gaps)} gaps"
            )
            
            for gap in gaps[:5]:  # Show first 5 in detail
                # Parse last_asked from string if needed
                last_asked = gap['last_asked']
                if isinstance(last_asked, str):
                    try:
                        last_asked = datetime.fromisoformat(last_asked.replace('Z', '+00:00'))
                        # Make it timezone-naive for comparison with utcnow()
                        if last_asked.tzinfo is not None:
                            last_asked = last_asked.replace(tzinfo=None)
                    except (ValueError, TypeError):
                        last_asked = datetime.utcnow()  # Fallback
                elif last_asked is not None and hasattr(last_asked, 'tzinfo') and last_asked.tzinfo is not None:
                    # Handle case where it's already a datetime object with timezone
                    last_asked = last_asked.replace(tzinfo=None)
                elif last_asked is None:
                    last_asked = datetime.utcnow()
                ago = datetime.utcnow() - last_asked
                ago_text = f"{ago.days}d ago" if ago.days > 0 else f"{ago.seconds // 3600}h ago"

                field_value = f"**Q:** {gap['question'][:150]}{'...' if len(gap['question']) > 150 else ''}\n"
                field_value += f"Asked {gap['times_asked']}x, Priority: {gap['priority_score']}, Last: {ago_text}"

                if gap['resolved_via']:
                    field_value += f"\n✅ Resolved via {gap['resolved_via']}"

                embed.add_field(
                    name=gap['topic'][:100],
                    value=field_value,
                    inline=False
                )

            if len(gaps) > 5:
                remaining = len(gaps) - 5
                embed.add_field(
                    name="",
                    value=f"*...and {remaining} more*",
                    inline=False
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        
        except Exception as e:
            logger.error(f"Failed to retrieve knowledge gaps: {e}")
            await interaction.followup.send(
                f"❌ Error: {str(e)[:200]}",
                ephemeral=True
            )

    @gap.command(name="curate", description="[Partners] Curate a knowledge gap (keep/defer/discard)")
    @app_commands.describe(
        gap_id="The knowledge_gaps.id",
        disposition="keep (ask again), defer (hide), discard (irrelevant)",
        reason="Optional note about why / rewrite suggestion",
    )
    async def gap_curate(
        self,
        interaction: discord.Interaction,
        gap_id: int,
        disposition: str,
        reason: Optional[str] = None,
    ):
        if not (is_partner(interaction) or is_admin(interaction)):
            await interaction.response.send_message(
                "This command is for partners/admins.",
                ephemeral=True,
            )
            return

        disp = (disposition or "").strip().lower()
        if disp not in ("keep", "defer", "discard"):
            await interaction.response.send_message(
                "Disposition must be one of: keep, defer, discard.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        now_ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        note_text = (reason or "").strip()

        if disp == "discard":
            new_status = "resolved"
            resolved_via = "dismissed"
            resolved_at = datetime.utcnow()
        else:
            new_status = "open"
            resolved_via = None
            resolved_at = None

        try:
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE knowledge_gaps
                    SET status = ?,
                        resolved_at = COALESCE(?, resolved_at),
                        resolved_via = COALESCE(?, resolved_via),
                        curation_status = ?,
                        curation_reason = COALESCE(?, curation_reason),
                        curated_at = ?,
                        curated_by_user_id = ?,
                        curated_by_username = ?,
                        notes = COALESCE(notes, '') || ?
                    WHERE id = ?
                    """,
                    (
                        new_status,
                        resolved_at,
                        resolved_via,
                        disp,
                        note_text if note_text else None,
                        now_ts,
                        interaction.user.id,
                        str(interaction.user),
                        f"\n\n[Curated by {interaction.user.display_name} on {now_ts}] disposition={disp} note={note_text or '—'}",
                        gap_id,
                    ),
                )

                if disp in ("defer", "discard"):
                    await conn.execute(
                        """
                        UPDATE knowledge_gaps
                        SET priority_score = CASE WHEN priority_score >= 5 THEN priority_score - 5 ELSE 0 END
                        WHERE id = ?
                        """,
                        (gap_id,),
                    )

                await conn.commit()

            await interaction.followup.send(
                f"✅ Curated gap **#{gap_id}** → **{disp}**.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"gap_curate failed: {e}")
            await interaction.followup.send(
                f"❌ Failed to curate gap: {str(e)[:200]}",
                ephemeral=True,
            )

    @gap.command(
        name="bulk",
        description="[Partners] Bulk curate knowledge gaps (fast cleanup)",
    )
    @app_commands.describe(
        action="discard (close), defer (hide), keep (ask again)",
        scope="Which set of gaps to target",
        topic_contains="Optional filter: only gaps whose topic contains this text",
        question_contains="Optional filter: only gaps whose question contains this text",
        older_than_days="Optional filter: only gaps not asked/updated in this many days",
        max_rows="How many gaps to affect (ignored if apply_all is true)",
        apply_all="If true, apply to all matches (can be large)",
        reason="Optional note stored as curation_reason",
        confirm="Must be true to apply changes",
    )
    async def gaps_bulk(
        self,
        interaction: discord.Interaction,
        action: Literal["discard", "defer", "keep"],
        scope: Literal["kept_open", "all_open", "deferred", "discarded"] = "kept_open",
        topic_contains: Optional[str] = None,
        question_contains: Optional[str] = None,
        older_than_days: Optional[int] = None,
        max_rows: int = 200,
        apply_all: bool = False,
        reason: Optional[str] = None,
        confirm: bool = False,
    ):
        """Bulk curate many gaps at once (designed for fast pruning)."""

        if not (is_partner(interaction) or is_admin(interaction)):
            await interaction.response.send_message(
                "This command is for partners/admins.",
                ephemeral=True,
            )
            return

        act = (action or "").strip().lower()
        if act not in ("discard", "defer", "keep"):
            await interaction.response.send_message(
                "Action must be one of: discard, defer, keep.",
                ephemeral=True,
            )
            return

        sc = (scope or "kept_open").strip().lower()
        allowed_scopes = {"kept_open", "deferred", "discarded", "all_open"}
        if sc not in allowed_scopes:
            await interaction.response.send_message(
                "Scope must be one of: kept_open, all_open, deferred, discarded.",
                ephemeral=True,
            )
            return

        max_rows = int(max_rows or 0)
        if max_rows < 1:
            max_rows = 200
        if max_rows > 2000:
            max_rows = 2000

        await interaction.response.defer(ephemeral=True)

        if not getattr(self.bot, "db", None):
            await interaction.followup.send(
                "❌ Database is not available.",
                ephemeral=True,
            )
            return

        where = []
        params: List[Any] = []

        # Base status + curation filters
        if sc == "kept_open":
            where.append("status IN ('open','in_progress')")
            where.append("COALESCE(curation_status, 'keep') = 'keep'")
        elif sc == "all_open":
            where.append("status IN ('open','in_progress')")
        elif sc == "deferred":
            where.append("status IN ('open','in_progress')")
            where.append("COALESCE(curation_status, 'keep') = 'defer'")
        elif sc == "discarded":
            where.append("(COALESCE(curation_status, 'keep') = 'discard' OR status = 'resolved')")

        if topic_contains:
            where.append("LOWER(topic) LIKE ?")
            params.append(f"%{topic_contains.strip().lower()}%")
        if question_contains:
            where.append("LOWER(question) LIKE ?")
            params.append(f"%{question_contains.strip().lower()}%")
        
        if older_than_days:
            # Safely calculate cutoff in python
            cutoff_dt = datetime.now() - timedelta(days=older_than_days)
            # last_asked is stored as ISO string in 'YYYY-MM-DD HH:MM:SS' or similar
            # SQLite string comparison works for ISO dates
            where.append("last_asked < ?")
            params.append(cutoff_dt.isoformat())

        where_sql = " AND ".join(where) if where else "1=1"

        try:
            async with self.bot.db.acquire() as conn:
                # Find ids to update
                limit_sql = "" if apply_all else " LIMIT ?"
                id_params = list(params)
                if not apply_all:
                    id_params.append(max_rows)

                async with conn.execute(
                    f"""
                    SELECT id
                    FROM knowledge_gaps
                    WHERE {where_sql}
                    ORDER BY priority_score DESC, times_asked DESC, last_asked DESC
                    {limit_sql}
                    """,
                    tuple(id_params),
                ) as cursor:
                    rows = await cursor.fetchall()

                ids = [int(r[0]) for r in (rows or [])]
                if not ids:
                    await interaction.followup.send(
                        "No matching gaps found.",
                        ephemeral=True,
                    )
                    return

                # Dry run by default
                if not confirm:
                    preview = ", ".join([f"#{i}" for i in ids[:12]])
                    more = "" if len(ids) <= 12 else f" … (+{len(ids) - 12} more)"
                    await interaction.followup.send(
                        f"Dry run: would update **{len(ids)}** gap(s) in scope **{sc}** → **{act}**.\n"
                        f"Examples: {preview}{more}\n\n"
                        "Re-run with `confirm:true` to apply.",
                        ephemeral=True,
                    )
                    return

                now_ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                note_text = (reason or "").strip()

                if act == "discard":
                    new_status = "resolved"
                    resolved_via = "dismissed"
                    resolved_at = datetime.utcnow()
                    new_curation = "discard"
                elif act == "defer":
                    new_status = "open"
                    resolved_via = None
                    resolved_at = None
                    new_curation = "defer"
                else:  # keep
                    new_status = "open"
                    resolved_via = None
                    resolved_at = None
                    new_curation = "keep"

                # Chunk updates to avoid SQLite variable limits
                total_updated = 0
                chunk_size = 400
                for i in range(0, len(ids), chunk_size):
                    chunk = ids[i : i + chunk_size]
                    placeholders = ",".join(["?"] * len(chunk))
                    await conn.execute(
                        f"""
                        UPDATE knowledge_gaps
                        SET status = ?,
                            resolved_at = COALESCE(?, resolved_at),
                            resolved_via = COALESCE(?, resolved_via),
                            curation_status = ?,
                            curation_reason = COALESCE(?, curation_reason),
                            curated_at = ?,
                            curated_by_user_id = ?,
                            curated_by_username = ?
                        WHERE id IN ({placeholders})
                        """,
                        (
                            new_status,
                            resolved_at,
                            resolved_via,
                            new_curation,
                            note_text if note_text else None,
                            now_ts,
                            interaction.user.id,
                            str(interaction.user),
                            *chunk,
                        ),
                    )
                    total_updated += len(chunk)

                    # Avoid leaving gaps stuck in_progress.
                    if new_status == "open":
                        await conn.execute(
                            f"UPDATE knowledge_gaps SET status='open' WHERE id IN ({placeholders}) AND status='in_progress'",
                            tuple(chunk),
                        )

                    # Nudge priority down when hiding/closing.
                    if act in ("defer", "discard"):
                        await conn.execute(
                            f"""
                            UPDATE knowledge_gaps
                            SET priority_score = CASE WHEN priority_score >= 5 THEN priority_score - 5 ELSE 0 END
                            WHERE id IN ({placeholders})
                            """,
                            tuple(chunk),
                        )

                await conn.commit()

            await interaction.followup.send(
                f"✅ Updated **{total_updated}** gap(s) → **{act}** (scope: **{sc}**).",
                ephemeral=True,
            )

        except Exception as e:
            logger.error(f"gaps_bulk failed: {e}")
            await interaction.followup.send(
                f"❌ Bulk update failed: {str(e)[:200]}",
                ephemeral=True,
            )


async def setup(bot):
    await bot.add_cog(KnowledgeGapTracker(bot))
