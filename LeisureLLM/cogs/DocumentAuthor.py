"""
Document Authoring & Live Training Module

Allows the bot to write memos/notes to the docs folder and trigger incremental
Chroma reindexing without expensive full retraining.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import discord
import yaml
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


async def _is_owner_interaction(interaction: discord.Interaction) -> bool:
    try:
        return await interaction.client.is_owner(interaction.user)
    except Exception:
        return False

# Paths (relative to LeisureLLM/)
DOCS_ROOT = Path(__file__).parent.parent / "docs"
MEMOS_DIR = DOCS_ROOT / "memos"
PROMPT_NOTES_DIR = DOCS_ROOT / "prompt" / "notes"

# ---------------------------------------------------------------------------
# LLM classification prompts for unified /remember
# ---------------------------------------------------------------------------
CLASSIFY_PROMPT = """Classify this input into exactly ONE category and extract structured fields.

Categories:
- DECISION: A choice or resolution that was made. Clues: "decided", "agreed", "going with", "the plan is".
- ACTION: A task to be done. Clues: "need to", "should", "todo", "by Friday", deadlines, assignments.
- MEETING: Notes from a meeting or conversation with multiple people. Clues: attendee lists, dialogue, agenda items, minutes.
- KNOWLEDGE: General info, facts, observations, or anything that doesn't clearly fit above.

Input:
\"\"\"{content}\"\"\"

Respond with ONLY valid JSON (no markdown fences):
{{"type": "decision|action|meeting|knowledge", "confidence": 0.0-1.0, "title": "short title max 10 words", "fields": {{}}}}

Field schemas by type:
- decision: {{"decision": "what was decided", "rationale": "why or null", "decided_by": "who or null"}}
- action: {{"assignee": "who or null", "due_date": "YYYY-MM-DD or null", "priority": "low|medium|high"}}
- meeting: {{"meeting_date": "YYYY-MM-DD or null", "attendees": ["name", ...], "summary": "1-2 sentences"}}
- knowledge: {{"tags": ["tag1", "tag2"]}}"""

CLASSIFY_EXTRACT_PROMPT = """Extract structured fields from this input. Treat it as a **{forced_type}**.

Input:
\"\"\"{content}\"\"\"

Respond with ONLY valid JSON (no markdown fences):
{{"type": "{forced_type}", "confidence": 1.0, "title": "short title max 10 words", "fields": {{}}}}

Field schema for {forced_type}:
- decision: {{"decision": "what was decided", "rationale": "why or null", "decided_by": "who or null"}}
- action: {{"assignee": "who or null", "due_date": "YYYY-MM-DD or null", "priority": "low|medium|high"}}
- meeting: {{"meeting_date": "YYYY-MM-DD or null", "attendees": ["name", ...], "summary": "1-2 sentences"}}
- knowledge: {{"tags": ["tag1", "tag2"]}}"""

TEACH_PREFILL_PROMPT = """Given this knowledge input, suggest a short title, best-fit category, and 3-8 tags.

Input:
\"\"\"{content}\"\"\"

Respond with ONLY valid JSON (no markdown fences):
{{"title": "short title max 10 words", "category": "reference|decision|process|project|client|technical|policy|other", "tags": ["tag1", "tag2"]}}"""


# ---------------------------------------------------------------------------
# Views for /remember confirmation & reclassification
# ---------------------------------------------------------------------------
class ReclassifySelect(discord.ui.Select):
    """Dropdown to reclassify a /remember entry."""

    def __init__(self, *, current_type: str):
        options = [
            discord.SelectOption(
                label="Decision", value="decision", emoji="🏛️",
                description="A choice that was made",
                default=(current_type == "decision"),
            ),
            discord.SelectOption(
                label="Action", value="action", emoji="✅",
                description="A task to be done",
                default=(current_type == "action"),
            ),
            discord.SelectOption(
                label="Meeting Notes", value="meeting", emoji="📋",
                description="Notes from a conversation",
                default=(current_type == "meeting"),
            ),
            discord.SelectOption(
                label="Knowledge", value="knowledge", emoji="🧠",
                description="General information",
                default=(current_type == "knowledge"),
            ),
        ]
        super().__init__(placeholder="🔄 Reclassify as…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: RememberConfirmView = self.view
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "Only the original user can reclassify.", ephemeral=True
            )
            return

        new_type = self.values[0]
        if new_type == view.current_type:
            await interaction.response.send_message(
                "Already classified as that type.", ephemeral=True
            )
            return

        await interaction.response.defer()

        # 1. Delete old DB record
        db = getattr(view.cog.bot, "db", None)
        if db and view.db_record_id:
            table_map = {
                "decision": "decisions",
                "action": "tasks",
                "meeting": "meeting_notes",
            }
            old_table = table_map.get(view.current_type)
            if old_table:
                try:
                    await db.execute(
                        f"DELETE FROM {old_table} WHERE id = ?", view.db_record_id
                    )
                except Exception as exc:
                    logger.warning(
                        "Cleanup of old %s #%s failed: %s",
                        view.current_type, view.db_record_id, exc,
                    )

        # 2. Re-extract fields for new type
        classification = await view.cog._classify_input(
            view.content, force_type=new_type
        )
        fields = classification.get("fields", {})
        new_title = classification.get("title") or view.title

        # 3. Write new DB record
        new_db_id = None
        try:
            if new_type == "decision":
                new_db_id = await view.cog._write_decision(
                    interaction, new_title, fields, view.content
                )
            elif new_type == "action":
                new_db_id = await view.cog._write_action(
                    interaction, new_title, fields, view.content
                )
            elif new_type == "meeting":
                new_db_id = await view.cog._write_meeting_record(
                    interaction, new_title, fields, view.content
                )
        except Exception as exc:
            logger.warning("Reclassify DB write failed: %s", exc)

        # 4. Build updated embed
        embed = view.cog._build_remember_embed(
            new_type, new_title, fields, view.content, view.origin, 1.0
        )

        # 5. Update view state
        view.current_type = new_type
        view.db_record_id = new_db_id
        view.title = new_title
        for opt in self.options:
            opt.default = opt.value == new_type

        await interaction.edit_original_response(embed=embed, view=view)


class AddDetailsModal(discord.ui.Modal, title="Add Details"):
    """Modal to append extra context to a classified /remember entry."""

    extra = discord.ui.TextInput(
        label="Additional details",
        style=discord.TextStyle.paragraph,
        placeholder="Add context, notes, or corrections…",
        required=True,
        max_length=2000,
    )

    def __init__(self, *, cog, db_record_id, record_type):
        super().__init__()
        self.cog = cog
        self.db_record_id = db_record_id
        self.record_type = record_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = getattr(self.cog.bot, "db", None)
        if db and self.db_record_id:
            table_map = {
                "decision": "decisions",
                "action": "tasks",
                "meeting": "meeting_notes",
            }
            table = table_map.get(self.record_type)
            col_map = {
                "decision": "description",
                "action": "notes",
                "meeting": "raw_text",
            }
            col = col_map.get(self.record_type)
            if table and col:
                try:
                    row = await db.fetchone(
                        f"SELECT {col} FROM {table} WHERE id = ?",
                        self.db_record_id,
                    )
                    existing = (row[0] or "") if row else ""
                    updated = existing + f"\n\n--- Added details ---\n{self.extra.value}"
                    await db.execute(
                        f"UPDATE {table} SET {col} = ? WHERE id = ?",
                        updated,
                        self.db_record_id,
                    )
                except Exception as exc:
                    logger.warning("Add details failed: %s", exc)

        await interaction.followup.send("✅ Details added.", ephemeral=True)


class RememberConfirmView(discord.ui.View):
    """Post-classification buttons: confirm, reclassify dropdown, add details."""

    def __init__(
        self, *, cog, content, current_type, author_name, title, origin,
        db_record_id, filepath, user_id,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.content = content
        self.current_type = current_type
        self.author_name = author_name
        self.title = title
        self.origin = origin
        self.db_record_id = db_record_id
        self.filepath = filepath
        self.user_id = user_id

        self.add_item(ReclassifySelect(current_type=current_type))

    @discord.ui.button(label="✓ Looks right", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the original user can confirm.", ephemeral=True
            )
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="+ Add details", style=discord.ButtonStyle.secondary, row=1)
    async def add_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the original user can add details.", ephemeral=True
            )
            return
        modal = AddDetailsModal(
            cog=self.cog,
            db_record_id=self.db_record_id,
            record_type=self.current_type,
        )
        await interaction.response.send_modal(modal)


class TeachPrefillModal(discord.ui.Modal, title="Add Knowledge to Magic Key"):
    title_input = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        placeholder="e.g., 'Camera Interactive System Architecture'",
        required=True,
        max_length=200,
        row=0,
    )

    category_input = discord.ui.TextInput(
        label="Category",
        style=discord.TextStyle.short,
        placeholder="reference, decision, process, project, client, technical, policy, other",
        required=True,
        max_length=30,
        row=1,
    )

    content_input = discord.ui.TextInput(
        label="Content",
        style=discord.TextStyle.paragraph,
        placeholder="Write or paste the knowledge you want the bot to learn...",
        required=True,
        max_length=4000,
        row=2,
    )

    tags_field = discord.ui.TextInput(
        label="Tags (comma-separated)",
        style=discord.TextStyle.short,
        placeholder="e.g., camera, interactive, opencv, hardware",
        required=False,
        max_length=200,
        row=3,
    )

    def __init__(self, *, cog, prefill_title: str, prefill_category: str, prefill_content: str, prefill_tags: list[str]):
        super().__init__()
        self.cog = cog
        self.title_input.default = prefill_title[:200]
        self.category_input.default = prefill_category[:30]
        self.content_input.default = prefill_content[:4000]
        if prefill_tags:
            self.tags_field.default = ", ".join(prefill_tags)[:200]

    async def on_submit(self, modal_interaction: discord.Interaction):
        await modal_interaction.response.defer(ephemeral=True)
        category = str(self.category_input.value or "reference").strip().lower()
        valid_cats = {
            "reference", "decision", "process", "project",
            "client", "technical", "policy", "other",
        }
        if category not in valid_cats:
            category = "reference"
        await self.cog._process_ingest(
            modal_interaction,
            title=self.title_input.value,
            content_parts=[self.content_input.value],
            category=category,
            tags=self.tags_field.value or "",
        )


class TeachPrefillView(discord.ui.View):
    """One-click open for a prefilled teach modal."""

    def __init__(self, *, user_id: int, modal_kwargs: dict, summary: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.modal_kwargs = modal_kwargs
        self.summary = summary

    @discord.ui.button(label="Review & Add", style=discord.ButtonStyle.success)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the original user can do this.", ephemeral=True)
            return
        await interaction.response.send_modal(TeachPrefillModal(**self.modal_kwargs))
        for item in self.children:
            item.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception as e:
            logger.warning("open_modal: suppressed %s", e)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class DocumentAuthor(commands.Cog):
    """Write memos and prompt notes, then incrementally retrain Chroma"""
    
    def __init__(self, bot):
        self.bot = bot
        self.llm_service = getattr(
            getattr(bot, "service_container", None), 
            "llm", 
            None
        )
        
        # Ensure directories exist
        MEMOS_DIR.mkdir(parents=True, exist_ok=True)
        PROMPT_NOTES_DIR.mkdir(parents=True, exist_ok=True)
    
    async def _trigger_incremental_ingest(self) -> Dict[str, Any]:
        """
        Run ingest_metadata.run_ingest in executor to add new docs to Chroma.
        Returns stats dict from ingest process.
        """
        from .ingest_metadata import run_ingest
        
        loop = asyncio.get_running_loop()
        try:
            # run_ingest is synchronous and CPU/IO bound, so run in executor
            # run_ingest returns (db, stats, persist_dir)
            _, stats, persist_dir = await loop.run_in_executor(None, run_ingest)
            
            stats["persist_dir"] = persist_dir
            stats["success"] = True
            
            logger.info(f"Incremental ingest complete: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Ingest failed: {e}")
            raise RuntimeError(f"Incremental ingest failed: {e}")
    
    def _save_document(
        self, 
        content: str, 
        doc_type: str, 
        slug: str, 
        metadata: Optional[Dict[str, Any]] = None
    ) -> Path:
        """
        Save document to appropriate folder with metadata frontmatter.
        
        Args:
            content: Document body
            doc_type: "memo" or "prompt_note"
            slug: Filename slug (auto-adds timestamp)
            metadata: Optional YAML frontmatter fields
        
        Returns:
            Path to saved file
        """

        def _sanitize_slug(raw: str) -> str:
            text = (raw or "").strip()
            if not text:
                return "document"

            # Replace Windows-illegal filename characters and control chars.
            text = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", text)
            # Collapse whitespace and repeated underscores.
            text = re.sub(r"\s+", "_", text)
            text = re.sub(r"_+", "_", text)
            # Windows forbids trailing dots/spaces.
            text = text.strip(" ._")
            if not text:
                text = "document"

            # Avoid reserved device names.
            reserved = {
                "con",
                "prn",
                "aux",
                "nul",
                *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10)),
            }
            if text.lower() in reserved:
                text = f"_{text}"

            return text

        now = datetime.now()
        slug = _sanitize_slug(slug)

        # Determine directory and path structure
        if doc_type == "memo":
            # Save to docs/memos/YYYY/MM/DD_slug.md
            year_dir = MEMOS_DIR / str(now.year)
            month_dir = year_dir / f"{now.month:02d}"
            month_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{now.day:02d}_{slug}.md"
            filepath = month_dir / filename

        elif doc_type == "prompt_note":
            # Save to docs/prompt/notes/YYYY-MM-DD_slug.md
            filename = f"{now.strftime('%Y-%m-%d')}_{slug}.md"
            filepath = PROMPT_NOTES_DIR / filename

        else:
            raise ValueError(f"Unknown doc_type: {doc_type}")

        # Build YAML frontmatter
        meta: Dict[str, Any] = dict(metadata) if metadata else {}
        meta.setdefault("created_at", now.isoformat())
        meta.setdefault("doc_type", doc_type)

        frontmatter = "---\n" + yaml.safe_dump(
            meta,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ) + "---\n\n"

        # Write file
        filepath.write_text(frontmatter + (content or ""), encoding="utf-8")
        logger.info(f"Saved {doc_type} to {filepath}")
        return filepath
    
    async def _find_related_memos(self, topic: str, memo_content: str) -> list:
        """
        Use semantic search to find existing memos on similar topics.
        Returns list of (path, similarity_score) tuples for potentially superseded memos.
        """
        try:
            from core.chroma_factory import get_vectorstore

            vectorstore = get_vectorstore()
            
            # Search for semantically similar documents
            # Use topic + content preview for better matching
            search_query = f"{topic}\n\n{memo_content[:500]}"
            
            # Get top 5 most similar docs with scores
            results = vectorstore.similarity_search_with_score(
                search_query,
                k=5
            )
            
            related = []
            for doc, score in results:
                metadata = doc.metadata or {}
                source = metadata.get("source", "")
                
                # Only consider docs from memos folder
                if "memos/" not in source:
                    continue
                
                # Skip if already marked as superseded
                if metadata.get("status") == "superseded":
                    continue
                
                # Only consider high similarity (lower score = more similar)
                # Typical range: 0.0-2.0, where <0.5 is very similar
                if score < 0.6:
                    source_path = Path(source)
                    if source_path.exists():
                        related.append((source_path, score))
            
            # Sort by similarity (lower score first)
            related.sort(key=lambda x: x[1])
            
            logger.info(f"Semantic search found {len(related)} related memos")
            return related
            
        except Exception as e:
            logger.warning(f"Semantic search for related memos failed: {e}")
            # Fallback: return empty list, don't block memo creation
            return []
    
    async def _mark_as_superseded(self, old_path: Path, new_path: Path):
        """
        Update old memo's frontmatter to mark it as superseded.
        This keeps it indexed but signals it's outdated.
        """
        try:
            content = old_path.read_text(encoding="utf-8")
            
            # Parse frontmatter
            if content.startswith("---\n"):
                parts = content.split("---\n", 2)
                if len(parts) >= 3:
                    frontmatter = parts[1]
                    body = parts[2]
                    
                    # Add superseded fields
                    frontmatter += "status: superseded\n"
                    frontmatter += f"superseded_by: {new_path.relative_to(DOCS_ROOT.parent)}\n"
                    frontmatter += f"superseded_at: {datetime.now().isoformat()}\n"
                    
                    # Rewrite file
                    updated = f"---\n{frontmatter}---\n{body}"
                    old_path.write_text(updated, encoding="utf-8")
                    
                    logger.info(f"Marked {old_path} as superseded by {new_path}")
                    return True
        except Exception as e:
            logger.warning(f"Failed to mark {old_path} as superseded: {e}")
        
        return False
    
    # ---------------------------------------------------------
    # Unified /remember — classification & routing helpers
    # ---------------------------------------------------------

    async def _classify_input(self, content: str, force_type: str = None) -> dict:
        """Use LLM to classify input into decision/action/meeting/knowledge."""
        fallback_type = force_type or "knowledge"
        fallback = {
            "type": fallback_type,
            "confidence": 0.0,
            "title": None,
            "fields": {},
        }

        if not self.llm_service:
            logger.warning("No LLM service — defaulting to %s", fallback_type)
            return fallback

        try:
            if force_type:
                prompt_text = CLASSIFY_EXTRACT_PROMPT.format(
                    content=content[:2000], forced_type=force_type
                )
            else:
                prompt_text = CLASSIFY_PROMPT.format(content=content[:2000])

            raw = await self.llm_service.complete(
                prompt_text, max_tokens=400, temperature=0.0
            )

            # Extract JSON from response (handles stray markdown fences)
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if not json_match:
                logger.warning("Classification returned no JSON: %s", raw[:200])
                return fallback

            result = json.loads(json_match.group())

            valid_types = {"decision", "action", "meeting", "knowledge"}
            if result.get("type") not in valid_types:
                result["type"] = fallback_type
            if force_type:
                result["type"] = force_type
                result["confidence"] = 1.0

            result.setdefault("confidence", 0.5)
            result.setdefault("title", None)
            result.setdefault("fields", {})
            return result

        except Exception as exc:
            logger.warning("Classification failed: %s — defaulting to %s", exc, fallback_type)
            return fallback

    async def _prefill_teach(self, content: str) -> dict:
        """Suggest title, category, and tags for /teach prefill."""
        fallback = {
            "title": None,
            "category": "reference",
            "tags": [],
        }

        if not self.llm_service:
            return fallback

        try:
            prompt_text = TEACH_PREFILL_PROMPT.format(content=content[:2000])
            raw = await self.llm_service.complete(
                prompt_text, max_tokens=200, temperature=0.0
            )
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if not json_match:
                return fallback
            result = json.loads(json_match.group())

            valid_cats = {
                "reference", "decision", "process", "project",
                "client", "technical", "policy", "other",
            }
            category = result.get("category")
            if category not in valid_cats:
                category = "reference"

            tags = result.get("tags") if isinstance(result.get("tags"), list) else []
            tags = [str(t).strip()[:30] for t in tags[:8] if t]

            title = result.get("title")
            if not title:
                words = content.split()[:6]
                title = " ".join(words) + ("..." if len(content.split()) > 6 else "")

            return {
                "title": title,
                "category": category,
                "tags": tags,
            }
        except Exception as exc:
            logger.warning("Teach prefill failed: %s", exc)
            return fallback

    async def _write_decision(self, interaction, title, fields, content) -> Optional[int]:
        """Insert into decisions table, return row ID."""
        db = getattr(self.bot, "db", None)
        if not db:
            return None

        decided_by = fields.get("decided_by")
        if not decided_by:
            decided_by = interaction.user.display_name
        if isinstance(decided_by, str):
            decided_by = json.dumps([decided_by])
        elif isinstance(decided_by, list):
            decided_by = json.dumps(decided_by)

        async with db.acquire() as conn:
            cursor = await conn.execute(
                """INSERT INTO decisions
                   (title, description, decision, rationale, decided_by, category)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    title,
                    content,
                    fields.get("decision", content[:500]),
                    fields.get("rationale"),
                    decided_by,
                    "general",
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def _write_action(self, interaction, title, fields, content) -> Optional[int]:
        """Insert into tasks table, return row ID."""
        db = getattr(self.bot, "db", None)
        if not db:
            return None

        async with db.acquire() as conn:
            cursor = await conn.execute(
                """INSERT INTO tasks
                   (title, description, status, priority,
                    assigned_to_username, created_by_user_id,
                    created_by_username, due_date)
                   VALUES (?, ?, 'todo', ?, ?, ?, ?, ?)""",
                (
                    title,
                    content,
                    fields.get("priority", "medium"),
                    fields.get("assignee"),
                    interaction.user.id,
                    interaction.user.display_name,
                    fields.get("due_date"),
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def _write_meeting_record(self, interaction, title, fields, content) -> Optional[int]:
        """Insert into meeting_notes table, return row ID."""
        db = getattr(self.bot, "db", None)
        if not db:
            return None

        attendees = fields.get("attendees")
        if isinstance(attendees, list):
            attendees = json.dumps(attendees)

        async with db.acquire() as conn:
            cursor = await conn.execute(
                """INSERT INTO meeting_notes
                   (summary, meeting_date, attendees, raw_text,
                    created_by_user_id, created_by_username)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    fields.get("summary", title),
                    fields.get("meeting_date"),
                    attendees,
                    content,
                    interaction.user.id,
                    interaction.user.display_name,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    def _build_remember_embed(
        self, artifact_type, title, fields, content, origin, confidence
    ) -> discord.Embed:
        """Build the confirmation embed for a classified /remember entry."""
        type_config = {
            "decision": ("🏛️ Decision Recorded", discord.Color.purple()),
            "action": ("✅ Action Created", discord.Color.green()),
            "meeting": ("📋 Meeting Notes Saved", discord.Color.blue()),
            "knowledge": ("🧠 Knowledge Stored", discord.Color.from_rgb(100, 100, 255)),
        }
        embed_title, embed_color = type_config.get(
            artifact_type, ("🧠 Memory Stored", discord.Color.blurple())
        )

        embed = discord.Embed(
            title=embed_title, description=f"**{title}**", color=embed_color
        )

        if artifact_type == "decision":
            embed.add_field(
                name="Decision",
                value=(fields.get("decision") or content[:200])[:1024],
                inline=False,
            )
            if fields.get("rationale"):
                embed.add_field(
                    name="Rationale",
                    value=fields["rationale"][:1024],
                    inline=False,
                )
            if fields.get("decided_by"):
                embed.add_field(
                    name="Decided by",
                    value=str(fields["decided_by"])[:256],
                    inline=True,
                )
        elif artifact_type == "action":
            embed.add_field(name="Task", value=title[:1024], inline=False)
            if fields.get("assignee"):
                embed.add_field(
                    name="Assigned to",
                    value=fields["assignee"][:256],
                    inline=True,
                )
            if fields.get("due_date"):
                embed.add_field(
                    name="Due", value=fields["due_date"][:256], inline=True
                )
            embed.add_field(
                name="Priority",
                value=fields.get("priority", "medium"),
                inline=True,
            )
        elif artifact_type == "meeting":
            embed.add_field(
                name="Summary",
                value=(fields.get("summary") or content[:200])[:1024],
                inline=False,
            )
            if fields.get("attendees"):
                names = fields["attendees"]
                if isinstance(names, list):
                    names = ", ".join(names)
                embed.add_field(
                    name="Attendees", value=str(names)[:256], inline=True
                )
            if fields.get("meeting_date"):
                embed.add_field(
                    name="Date", value=fields["meeting_date"][:256], inline=True
                )
        else:
            embed.add_field(
                name="Preview",
                value=content[:300] + ("…" if len(content) > 300 else ""),
                inline=False,
            )
            tags = fields.get("tags")
            if tags:
                if isinstance(tags, list):
                    tags = ", ".join(tags)
                embed.add_field(name="Tags", value=str(tags)[:256], inline=True)

        if origin:
            embed.add_field(
                name="Origin",
                value=f"[Original message]({origin})",
                inline=False,
            )

        embed.set_footer(
            text=f"Confidence: {confidence:.0%} · Use the dropdown to reclassify"
        )
        return embed

    # ---------------------------------------------------------
    # Remember / Memory Commands
    # ---------------------------------------------------------

    @app_commands.command(name="remember", description="Save anything — decisions, actions, meeting notes, or knowledge")
    @app_commands.describe(content="What to remember — the bot classifies it automatically", title="Optional title (auto-generated if blank)")
    async def remember_slash(self, interaction: discord.Interaction, content: str, title: Optional[str] = None):
        """Save a quick text note to the docs/memos folder."""
        if not content:
            await interaction.response.send_message("❌ You must provide content to remember.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        await self._remember_logic(
            interaction, 
            content, 
            author_name=interaction.user.display_name, 
            title=title
        )

    async def cog_load(self):
        # Register context menu manually since decorators don't work in Cogs
        self.ctx_menu = app_commands.ContextMenu(
            name="Remember This",
            callback=self.remember_context,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def remember_context(self, interaction: discord.Interaction, message: discord.Message):
        """Right-click a message to save it as a memo."""
        await interaction.response.defer(ephemeral=False)
        
        content = message.content
        if not content and message.attachments:
            # Handle lone attachments cleanly
            content = "\n".join([f"[{a.filename}]({a.url})" for a in message.attachments])
        elif not content:
            await interaction.followup.send("❌ Message has no text content.", ephemeral=True)
            return

        # Add attachment links if mixed
        if message.attachments and message.content:
            content += "\n\nAttachments:\n" + "\n".join([f"- [{a.filename}]({a.url})" for a in message.attachments])

        title = f"Message from {message.author.display_name}"
        await self._remember_logic(
            interaction, 
            content, 
            author_name=message.author.display_name, 
            title=title,
            origin=message.jump_url
        )

    async def _remember_logic(self, interaction, content, author_name, title=None, origin=None):
        from ux_helpers import ProgressCard
        
        # Show progress
        progress = ProgressCard(
            title="🧠 Processing Memory",
            description="Classifying input…",
            color=discord.Color.from_rgb(100, 100, 255),
        )
        await progress.send(interaction.channel)

        # --- Step 1: Classify via LLM ---
        classification = await self._classify_input(content)
        artifact_type = classification.get("type", "knowledge")
        extracted_title = title or classification.get("title")  # user-supplied wins
        fields = classification.get("fields", {})
        confidence = classification.get("confidence", 0.0)

        if not extracted_title:
            words = content.split()[:6]
            extracted_title = " ".join(words) + ("…" if len(content.split()) > 6 else "")

        slug = re.sub(r"[^a-z0-9]+", "_", extracted_title.lower())[:50].strip("_") or "memory"

        await progress.update_status(
            f"Classified as **{artifact_type}** ({confidence:.0%} confidence)"
        )

        # --- Step 2: Save to docs/ (all paths get a file for Chroma) ---
        await progress.update_status("Saving to knowledge base…")

        metadata = {
            "author": author_name,
            "author_id": str(interaction.user.id),
            "source": "discord",
            "artifact_type": artifact_type,
            "captured_at": datetime.now().isoformat(),
        }
        if origin:
            metadata["origin_url"] = origin

        if artifact_type == "meeting":
            meetings_dir = DOCS_ROOT / "meetings"
            meetings_dir.mkdir(parents=True, exist_ok=True)
            meeting_date = fields.get("meeting_date") or datetime.now().strftime("%Y-%m-%d")
            filename = f"{meeting_date}_{slug}.md"
            filepath = meetings_dir / filename
            frontmatter = (
                "---\n"
                + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
                + "---\n\n"
            )
            filepath.write_text(
                frontmatter + f"# Meeting Notes: {extracted_title}\n\n{content}\n",
                encoding="utf-8",
            )
        else:
            filepath = self._save_document(
                content=content, doc_type="memo", slug=slug, metadata=metadata
            )

        await progress.add_field("File", filepath.name)

        # --- Step 3: Write to DB table (type-specific) ---
        db_record_id = None
        try:
            if artifact_type == "decision":
                db_record_id = await self._write_decision(
                    interaction, extracted_title, fields, content
                )
                await progress.add_field("DB", f"decisions #{db_record_id}")
            elif artifact_type == "action":
                db_record_id = await self._write_action(
                    interaction, extracted_title, fields, content
                )
                await progress.add_field("DB", f"tasks #{db_record_id}")
            elif artifact_type == "meeting":
                db_record_id = await self._write_meeting_record(
                    interaction, extracted_title, fields, content
                )
                await progress.add_field("DB", f"meeting_notes #{db_record_id}")
            else:
                await progress.add_field("DB", "—")
        except Exception as exc:
            logger.warning("DB write for %s failed (non-fatal): %s", artifact_type, exc)
            await progress.add_field("DB", f"⚠️ {str(exc)[:60]}")

        # --- Step 4: Chroma reindex ---
        await progress.update_status("Indexing into knowledge base…")
        try:
            stats = await self._trigger_incremental_ingest()
            chunks = stats.get("chunks_written", 0)
            await progress.complete(
                f"✅ Saved as **{artifact_type}** ({chunks} chunks indexed)"
            )
        except Exception:
            await progress.complete(
                f"✅ Saved as **{artifact_type}** (index will retry on schedule)"
            )

        # --- Step 5: Build confirmation embed ---
        embed = self._build_remember_embed(
            artifact_type, extracted_title, fields, content, origin, confidence
        )

        # --- Step 6: 6th Partner semantic-connection check ---
        try:
            llm_cog = interaction.client.get_cog("LLM")
            if llm_cog and hasattr(llm_cog, "safe_llm_call") and hasattr(interaction.client, "retriever"):
                await progress.update_status("Looking for connections…")
                try:
                    from cogs.LLM import run_retriever_query
                    relevant_docs = run_retriever_query(interaction.client.retriever, content)
                except Exception:
                    relevant_docs = []

                if relevant_docs:
                    context_text = "\n\n".join(
                        [d.page_content for d in relevant_docs[:3]]
                    )
                    prompt = (
                        f'A user just saved a new memory:\n"{content}"\n\n'
                        f"Existing Related Memories:\n{context_text}\n\n"
                        "Briefly (2 sentences max) identify if this SUPPORTS, "
                        "CONTRADICTS, or EXPANDS upon what we already know. "
                        "If there is a contradiction, point it out clearly. "
                        "If it's totally new, say nothing."
                    )
                    analysis = await llm_cog.safe_llm_call(
                        prompt, None, user="System"
                    )
                    if (
                        analysis
                        and len(analysis) > 10
                        and "say nothing" not in analysis.lower()
                    ):
                        embed.add_field(
                            name="🔗 Connections Found",
                            value=analysis,
                            inline=False,
                        )
        except Exception as exc:
            logger.warning("6th partner check failed: %s", exc)

        # --- Step 7: Send with reclassify buttons ---
        view = RememberConfirmView(
            cog=self,
            content=content,
            current_type=artifact_type,
            author_name=author_name,
            title=extracted_title,
            origin=origin,
            db_record_id=db_record_id,
            filepath=filepath,
            user_id=interaction.user.id,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="teach", description="Add knowledge to the bot's training corpus")
    @app_commands.describe(
        category="What type of knowledge is this?",
        quick="Paste a short paragraph to prefill category + tags"
    )
    async def ingest_knowledge(
        self,
        interaction: discord.Interaction,
        category: str = "reference",
        quick: Optional[str] = None,
    ):
        """
        Open a modal for users to add human-written knowledge to the bot's corpus.
        This is the primary mechanism for teaching the bot new information.
        """

        if quick:
            from ux_helpers import create_info_embed

            await interaction.response.defer(ephemeral=True)
            prefill = await self._prefill_teach(quick)
            final_category = category if category != "reference" else prefill.get("category", "reference")
            prefill_title = prefill.get("title") or "Untitled"
            prefill_tags = prefill.get("tags", [])

            summary = (
                f"**Title:** {prefill_title}\n"
                f"**Category:** {final_category}\n"
                f"**Tags:** {', '.join(prefill_tags) if prefill_tags else '-'}"
            )
            modal_kwargs = {
                "cog": self,
                "prefill_title": prefill_title,
                "prefill_category": final_category,
                "prefill_content": quick,
                "prefill_tags": prefill_tags,
            }
            view = TeachPrefillView(
                user_id=interaction.user.id,
                modal_kwargs=modal_kwargs,
                summary=summary,
            )
            await interaction.followup.send(
                embed=create_info_embed("Prefilled knowledge", summary),
                view=view,
                ephemeral=True,
            )
            return
        
        class IngestModal(discord.ui.Modal, title="Add Knowledge to Magic Key"):
            knowledge_title = discord.ui.TextInput(
                label="Title",
                style=discord.TextStyle.short,
                placeholder="e.g., 'Camera Interactive System Architecture'",
                required=True,
                max_length=200,
                row=0
            )
            
            content_part1 = discord.ui.TextInput(
                label="Content (Part 1)",
                style=discord.TextStyle.paragraph,
                placeholder="Write or paste the knowledge you want the bot to learn...",
                required=True,
                max_length=4000,
                row=1
            )
            
            content_part2 = discord.ui.TextInput(
                label="Content (Part 2 - Optional)",
                style=discord.TextStyle.paragraph,
                placeholder="Continue here if needed...",
                required=False,
                max_length=4000,
                row=2
            )
            
            content_part3 = discord.ui.TextInput(
                label="Content (Part 3 - Optional)",
                style=discord.TextStyle.paragraph,
                placeholder="Continue here if needed...",
                required=False,
                max_length=4000,
                row=3
            )
            
            tags_field = discord.ui.TextInput(
                label="Tags (comma-separated)",
                style=discord.TextStyle.short,
                placeholder="e.g., camera, interactive, opencv, hardware",
                required=False,
                max_length=200,
                row=4
            )
            
            def __init__(self, cog, category: str):
                super().__init__()
                self.cog = cog
                self.category = category
            
            async def on_submit(self, modal_interaction: discord.Interaction):
                await modal_interaction.response.defer(ephemeral=True)
                await self.cog._process_ingest(
                    modal_interaction,
                    title=self.knowledge_title.value,
                    content_parts=[
                        self.content_part1.value,
                        self.content_part2.value or "",
                        self.content_part3.value or ""
                    ],
                    category=self.category,
                    tags=self.tags_field.value or ""
                )
        
        modal = IngestModal(self, category)
        await interaction.response.send_modal(modal)
    
    @ingest_knowledge.autocomplete('category')
    async def category_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Provide category suggestions"""
        categories = [
            ("reference", "Reference - General knowledge, how things work"),
            ("decision", "Decision - A choice that was made and why"),
            ("process", "Process - How to do something, steps/procedures"),
            ("project", "Project - Info about a specific project"),
            ("client", "Client - Information about a client/partner"),
            ("technical", "Technical - Code, systems, architecture details"),
            ("policy", "Policy - Rules, guidelines, standards"),
            ("other", "Other - Doesn't fit other categories"),
        ]
        return [
            app_commands.Choice(name=desc, value=val)
            for val, desc in categories
            if current.lower() in val.lower() or current.lower() in desc.lower()
        ][:25]
    
    async def _process_ingest(
        self,
        interaction: discord.Interaction,
        title: str,
        content_parts: list[str],
        category: str,
        tags: str
    ):
        """Process the ingested knowledge and save to docs"""
        from ux_helpers import ProgressCard, PublishView, create_success_embed
        
        # Combine content parts
        full_content = "\n".join(part for part in content_parts if part.strip())
        
        if len(full_content.strip()) < 50:
            await interaction.followup.send(
                "❌ Content too short. Please provide meaningful knowledge (at least 50 characters).",
                ephemeral=True
            )
            return
        
        # Create progress tracker
        progress = ProgressCard(
            title="📥 Ingesting Knowledge",
            description=f"**{title}**",
            color=discord.Color.green()
        )
        await progress.send(interaction.channel)
        
        try:
            await progress.update_status("Saving to knowledge base...")
            
            # Parse tags
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]

            # --- Auto-classify & auto-tag via LLM when user left defaults ---
            auto_tagged = False
            if self.llm_service and (category == "reference" or not tag_list):
                try:
                    await progress.update_status("Auto-classifying content…")
                    auto_prompt = (
                        "Given this knowledge document, respond with ONLY valid JSON "
                        "(no markdown fences):\n"
                        '{"category": "reference|decision|process|project|client|technical|policy|other", '
                        '"tags": ["tag1", "tag2", "tag3"]}\n\n'
                        f"Title: {title}\n\nContent:\n{full_content[:3000]}"
                    )
                    raw_auto = await self.llm_service.complete(
                        auto_prompt, max_tokens=200, temperature=0.0
                    )
                    json_match = re.search(r"\{[\s\S]*\}", raw_auto)
                    if json_match:
                        auto_result = json.loads(json_match.group())
                        valid_cats = {"reference", "decision", "process", "project",
                                      "client", "technical", "policy", "other"}
                        if category == "reference" and auto_result.get("category") in valid_cats:
                            category = auto_result["category"]
                        if not tag_list and isinstance(auto_result.get("tags"), list):
                            tag_list = [str(t).strip()[:30] for t in auto_result["tags"][:8] if t]
                        auto_tagged = True
                except Exception as exc:
                    logger.warning("Auto-classify for /teach failed (non-fatal): %s", exc)
            
            # Create slug from title
            slug = re.sub(r'[^a-z0-9]+', '_', title.lower())[:50].strip('_')
            
            # Prepare metadata
            metadata = {
                "title": title,
                "category": category,
                "contributed_by": interaction.user.display_name,
                "contributor_id": str(interaction.user.id),
                "doc_type": "human_knowledge",
                "tags": tag_list,
            }
            
            # Save to docs/knowledge/{category}/
            filepath = self._save_knowledge_document(
                title=title,
                content=full_content,
                category=category,
                slug=slug,
                metadata=metadata
            )
            
            await progress.add_field("Category", f"{category}" + (" 🤖" if auto_tagged else ""))
            await progress.add_field("Length", f"{len(full_content)} chars")
            if tag_list and auto_tagged:
                await progress.add_field("Auto-Tags", ", ".join(tag_list))
            await progress.add_field("Saved To", str(filepath.relative_to(DOCS_ROOT.parent)))
            
            # Trigger immediate reindex
            await progress.update_status("Reindexing knowledge base...")
            try:
                stats = await self._trigger_incremental_ingest()
                await progress.add_field("Index", f"✅ {stats['chunks_written']} chunks added")
            except Exception as e:
                logger.warning(f"Reindex failed (non-fatal): {e}")
                await progress.add_field("Index", "⚠️ Will retry on next schedule")
            
            await progress.complete("Knowledge ingested successfully!")
            
            # Show confirmation
            tag_display = ', '.join(tag_list) if tag_list else 'none'
            auto_label = " _(auto-generated)_" if auto_tagged and tag_list else ""
            result_embed = create_success_embed(
                title=f"📥 Knowledge Added: {title[:100]}",
                description=f"**Category:** {category}\n**Tags:** {tag_display}{auto_label}\n\n**Preview:**\n{full_content[:400]}{'...' if len(full_content) > 400 else ''}"
            )
            result_embed.set_footer(text=f"Contributed by {interaction.user.display_name}")
            
            view = PublishView(content="", embeds=[result_embed])
            await interaction.followup.send(
                embed=result_embed,
                view=view,
                ephemeral=True
            )
            
            # Public acknowledgment: let the channel know someone contributed
            try:
                public_embed = discord.Embed(
                    description=f"📚 **{interaction.user.display_name}** just taught me about **{title[:80]}** ({category})",
                    color=discord.Color.green()
                )
                public_embed.set_footer(text="Use /ingest to add your knowledge too!")
                await interaction.channel.send(embed=public_embed)
            except Exception as e:
                logger.debug(f"Failed to post public acknowledgment: {e}")
            
        except Exception as e:
            logger.error(f"Knowledge ingest failed: {e}")
            await progress.fail(f"Error: {str(e)[:100]}")
    
    def _save_knowledge_document(
        self,
        title: str,
        content: str,
        category: str,
        slug: str,
        metadata: dict
    ) -> Path:
        """Save human knowledge to docs/knowledge/{category}/"""
        
        now = datetime.now()
        
        # Create directory structure
        knowledge_dir = DOCS_ROOT / "knowledge" / category
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename with date prefix
        filename = f"{now.strftime('%Y-%m-%d')}_{slug}.md"
        filepath = knowledge_dir / filename
        
        # Handle duplicate filenames
        counter = 1
        while filepath.exists():
            filename = f"{now.strftime('%Y-%m-%d')}_{slug}_{counter}.md"
            filepath = knowledge_dir / filename
            counter += 1
        
        # Build frontmatter
        meta = dict(metadata)
        meta["created_at"] = now.isoformat()
        
        frontmatter = "---\n" + yaml.safe_dump(
            meta,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ) + "---\n\n"
        
        # Format document
        document = f"{frontmatter}# {title}\n\n{content}\n"
        
        # Write file
        filepath.write_text(document, encoding="utf-8")
        logger.info(f"Saved knowledge document to {filepath}")
        
        return filepath
    
    @app_commands.command(name="prompt_note", description="Add a note to the bot's behavioral prompt")
    @app_commands.describe(
        observation="What behavioral pattern did you observe?",
        suggestion="How should the bot adjust?"
    )
    @app_commands.check(_is_owner_interaction)
    async def write_prompt_note(
        self,
        interaction: discord.Interaction,
        observation: str,
        suggestion: str
    ):
        """
        Document a prompt improvement suggestion. These accumulate in docs/prompt/notes/
        and get periodically consolidated into the main prompt by humans.
        """
        await interaction.response.defer(ephemeral=True)
        
        # Format as structured note
        content = f"""## Observation

{observation}

## Suggested Adjustment

{suggestion}

## Context

- Reported by: {interaction.user.display_name}
- Date: {datetime.now().strftime('%Y-%m-%d')}
- Channel: {interaction.channel.name if hasattr(interaction.channel, 'name') else 'DM'}

## Status

🟡 Pending human review for consolidation into main prompt
"""
        
        # Save
        slug = f"note_{interaction.user.name}_{datetime.now().strftime('%H%M')}"
        metadata = {
            "observer": interaction.user.display_name,
            "observer_id": str(interaction.user.id),
            "observation": observation[:100],
            "status": "pending_review"
        }
        
        filepath = self._save_document(
            content=content,
            doc_type="prompt_note",
            slug=slug,
            metadata=metadata
        )
        
        # Trigger immediate reindex (prompt notes are "reason documents")
        try:
            stats = await self._trigger_incremental_ingest()
            reindex_msg = f"✅ Reindexed ({stats['chunks_written']} chunks)"
        except Exception as e:
            logger.error(f"Reindex failed: {e}")
            reindex_msg = "⚠️ Reindex failed (will retry on schedule)"
        
        await interaction.followup.send(
            f"✅ Prompt note saved to `{filepath.relative_to(DOCS_ROOT.parent)}`\n{reindex_msg}\n\n"
            f"This note will be included in RAG context and consolidated by admins during next prompt review.",
            ephemeral=True
        )
    
    @app_commands.command(name="reindex_docs", description="Manually trigger incremental document reindexing")
    @app_commands.check(_is_owner_interaction)
    async def manual_reindex(self, interaction: discord.Interaction = None, silent: bool = False):
        """Trigger incremental Chroma reindex (only processes changed files)"""
        if interaction:
            await interaction.response.defer(ephemeral=True)
        
        try:
            stats = await self._trigger_incremental_ingest()
            
            if interaction and not silent:
                summary = (
                    f"✅ **Incremental reindex complete**\n\n"
                    f"📄 Added files: {stats['added_files']}\n"
                    f"🔄 Updated files: {stats['updated_files']}\n"
                    f"📦 Chunks written: {stats['chunks_written']}\n\n"
                    f"{'⚡ No changes detected - index already up to date' if stats['chunks_written'] == 0 else '✨ New content now available in RAG'}"
                )
                
                await interaction.followup.send(summary, ephemeral=True)
            
            return stats
            
        except Exception as e:
            if interaction and not silent:
                await interaction.followup.send(
                    f"❌ Reindex failed: {str(e)[:200]}\n\nCheck logs for details.",
                    ephemeral=True
                )
            raise
    
    async def auto_create_improvement_memo(
        self, 
        topic: str, 
        question: str, 
        answer: str, 
        feedback_count: int,
        detailed_feedback: str = None
    ):
        """
        Auto-create improvement memo from negative feedback.
        Called by FeedbackView when users mark responses as unhelpful.

        Quality gates (from corpus_quality config):
        - max_per_day: refuse to create more than N per day
        - exclude_bad_answer: omit the wrong answer to prevent corpus pollution
        - require_review: save the memo with status=needs_review so it is NOT
          ingested into ChromaDB until a human approves it
        """
        try:
            # ── Load corpus-quality settings ──────────────────────────
            try:
                from core.config_loader import WorkflowConfig
                wf = WorkflowConfig.load()
            except Exception:
                # Fallback defaults if config unreachable
                class _Defaults:
                    cq_auto_improvement_max_per_day = 3
                    cq_auto_improvement_exclude_bad_answer = True
                    cq_auto_improvement_require_review = True
                wf = _Defaults()

            max_per_day = wf.cq_auto_improvement_max_per_day
            exclude_bad_answer = wf.cq_auto_improvement_exclude_bad_answer
            require_review = wf.cq_auto_improvement_require_review

            # ── Daily cap check ───────────────────────────────────────
            if hasattr(self.bot, 'db') and self.bot.db:
                async with self.bot.db.acquire() as conn:
                    try:
                        async with conn.execute("""
                            SELECT COUNT(*) FROM response_feedback
                            WHERE improvement_memo_created = TRUE
                            AND created_at > datetime('now', '-1 day')
                        """) as cursor:
                            row = await cursor.fetchone()
                            today_count = row[0] if row else 0
                        if today_count >= max_per_day:
                            logger.info(
                                "Auto-improvement memo skipped: daily cap reached "
                                "(%d/%d)", today_count, max_per_day
                            )
                            return
                    except Exception as e:
                        logger.debug("Daily cap check failed (non-fatal): %s", e)

            # ── Build memo content ────────────────────────────────────
            content = f"""# Improvement Area: {topic}

## User Question

{question}

"""

            if not exclude_bad_answer:
                # Legacy behaviour: include the bad answer (risks corpus pollution)
                content += f"""## Bot's Answer (That Didn't Help)

{answer[:500]}{'...' if len(answer) > 500 else ''}

"""

            content += f"""## Feedback Summary

- **Times marked unhelpful:** {feedback_count}
- **Detection date:** {datetime.now().strftime('%Y-%m-%d')}

"""
            
            if detailed_feedback:
                content += f"""## Detailed User Feedback

{detailed_feedback}

"""
            
            content += """## Action Items

1. Review this response and identify gaps
2. Add missing context to knowledge base
3. Update relevant docs or create new ones
4. Consider if this requires prompt adjustment

## Status

\U0001f534 Needs review - users found this unhelpful
"""
            
            slug = f"improvement_{datetime.now().strftime('%Y%m%d_%H%M')}"
            status = "needs_review" if require_review else "active"
            metadata = {
                "topic": topic,
                "feedback_type": "unhelpful_response",
                "feedback_count": feedback_count,
                "status": status,
                "auto_generated": True,
            }
            
            filepath = self._save_document(
                content=content,
                doc_type="memo",
                slug=slug,
                metadata=metadata
            )
            
            # Only ingest immediately if review is NOT required
            if not require_review:
                await self._trigger_incremental_ingest()
                logger.info("Auto-improvement memo created and ingested: %s", filepath)
            else:
                logger.info(
                    "Auto-improvement memo saved (needs_review, not ingested): %s",
                    filepath,
                )
            
            # Log to database if available
            if hasattr(self.bot, 'db') and self.bot.db:
                async with self.bot.db.acquire() as conn:
                    # SQLite syntax: ? instead of $1, LIKE instead of ILIKE
                    await conn.execute("""
                        UPDATE response_feedback
                        SET improvement_memo_created = TRUE,
                            memo_path = ?
                        WHERE question LIKE ?
                        AND feedback = 'not_helpful'
                        AND improvement_memo_created = FALSE
                    """, (str(filepath), f"%{question[:100]}%"))
            
        except Exception as e:
            logger.error(f"Failed to auto-create improvement memo: {e}")

    # ------------------------------------------------------------------
    # Autonomous auto-approval for web-researched content
    # ------------------------------------------------------------------

    async def auto_approve_memo(self, filepath: Path) -> bool:
        """Auto-approve a needs_review memo  and trigger ingestion.

        Called by autonomous processes (e.g. Curator web research) when the
        content has sufficient external citations to be trusted without human
        review.  Flips ``status: needs_review`` → ``status: auto_approved``
        and triggers incremental ingest so the content is immediately
        available in retrieval.

        Returns True if the memo was approved and ingested, False otherwise.
        """
        try:
            if not filepath.exists():
                logger.warning("auto_approve_memo: file not found: %s", filepath)
                return False

            content = filepath.read_text(encoding="utf-8")
            if "status: needs_review" not in content:
                logger.debug("auto_approve_memo: not in needs_review status: %s", filepath)
                return False

            updated = content.replace(
                "status: needs_review",
                f"status: auto_approved\nauto_approved_at: {datetime.now().isoformat()}",
                1,
            )
            filepath.write_text(updated, encoding="utf-8")

            await self._trigger_incremental_ingest()
            logger.info(
                "Auto-approved and ingested memo: %s", filepath
            )
            return True

        except Exception as e:
            logger.error("auto_approve_memo failed for %s: %s", filepath, e)
            return False

    # ------------------------------------------------------------------
    # /approve_memo — review gate for auto-generated content
    # ------------------------------------------------------------------

    @app_commands.command(
        name="approve_memo",
        description="Approve a needs_review memo so it gets ingested into the knowledge base",
    )
    @app_commands.describe(
        memo_path="Relative path under docs/ (e.g. memos/2026/02/18_improvement_20260218_1430.md)",
    )
    @app_commands.check(_is_owner_interaction)
    async def approve_memo(self, interaction: discord.Interaction, memo_path: str):
        """Flip status from needs_review → active and trigger reindex."""
        await interaction.response.defer(ephemeral=True)

        full_path = DOCS_ROOT / memo_path
        if not full_path.exists():
            # Try as absolute or relative to project root
            alt = Path(__file__).parent.parent / memo_path
            if alt.exists():
                full_path = alt
            else:
                await interaction.followup.send(
                    f"❌ File not found: `{memo_path}`",
                    ephemeral=True,
                )
                return

        try:
            content = full_path.read_text(encoding="utf-8")
            if "status: needs_review" not in content:
                await interaction.followup.send(
                    "ℹ️ This memo is not in `needs_review` status — it's already eligible for ingestion.",
                    ephemeral=True,
                )
                return

            updated = content.replace(
                "status: needs_review",
                f"status: active\nreviewed_at: {datetime.now().isoformat()}\nreviewed_by: {interaction.user.display_name}",
                1,
            )
            full_path.write_text(updated, encoding="utf-8")

            stats = await self._trigger_incremental_ingest()
            await interaction.followup.send(
                f"✅ Memo approved and ingested.\n"
                f"📄 `{memo_path}`\n"
                f"📦 Chunks written: {stats.get('chunks_written', '?')}",
                ephemeral=True,
            )
        except Exception as e:
            logger.error("approve_memo failed: %s", e)
            await interaction.followup.send(
                f"❌ Failed to approve memo: {e}",
                ephemeral=True,
            )

    @app_commands.command(
        name="pending_memos",
        description="List auto-generated memos awaiting review",
    )
    @app_commands.check(_is_owner_interaction)
    async def pending_memos(self, interaction: discord.Interaction):
        """List all docs/memos with status: needs_review."""
        await interaction.response.defer(ephemeral=True)

        pending: list[str] = []
        for root, _dirs, files in os.walk(MEMOS_DIR):
            for name in files:
                if not name.endswith(".md"):
                    continue
                full = os.path.join(root, name)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        first = f.readline().strip()
                        if first != "---":
                            continue
                        for _ in range(30):
                            line = f.readline()
                            if not line:
                                break
                            stripped = line.strip()
                            if stripped == "---":
                                break
                            if stripped == "status: needs_review":
                                rel = os.path.relpath(full, DOCS_ROOT)
                                pending.append(rel.replace("\\", "/"))
                                break
                except Exception:
                    continue

        if not pending:
            await interaction.followup.send(
                "✅ No memos pending review — all auto-generated content has been approved or none exists.",
                ephemeral=True,
            )
            return

        lines = [f"**{len(pending)} memo(s) awaiting review:**\n"]
        for p in pending[:25]:
            lines.append(f"• `{p}`")
        if len(pending) > 25:
            lines.append(f"\n… and {len(pending) - 25} more")
        lines.append("\nUse `/approve_memo <path>` to approve and ingest.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(DocumentAuthor(bot))
    logger.info("DocumentAuthor cog loaded")
