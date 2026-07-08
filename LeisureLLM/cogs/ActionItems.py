"""Action Items Cog

Partner-facing, assignable action items built on the existing `tasks` table.

Design goals:
- Fast capture via modal
- Clear ownership and due dates
- Lightweight follow-up loop (weekly check-in)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from ux_helpers import create_error_embed, create_info_embed, create_success_embed

import config as leisure_config
from config import (
    OVERDUE_FOLLOWUP_MAX_PER_OWNER_PER_RUN as CONFIG_OVERDUE_MAX,
)
from config import (
    PARTNER_EMOJI_TO_ID,
    PARTNER_EMOJIS,
    PARTNER_ID_TO_EMOJI,
    PARTNERS,
    POINTS_ACTION_DONE,
    POINTS_ACTION_DONE_RESOLVES_GAP_BONUS,
    WIP_LIMIT_IN_PROGRESS,
)

# Reuse partner gating if available
try:
    from .KnowledgeGapTracker import is_partner
except Exception:  # pragma: no cover

    def is_partner(interaction: discord.Interaction) -> bool:  # type: ignore
        return True


logger = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")

ACTION_TAG = "action_item"

RECURRENCE_DELTAS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
}

# ---------------------------------------------------------------------------
# LLM extraction prompt for smart /action add
# ---------------------------------------------------------------------------
ACTION_EXTRACT_PROMPT = """Extract action-item fields from this natural-language input.

Input:
\"\"\"{content}\"\"\"

Today's date: {today}

Respond with ONLY valid JSON (no markdown fences):
{{
  "title": "short imperative title, max 10 words",
  "details": "any extra context from the input, or empty string",
  "assignee_name": "person's first name if mentioned, or null",
  "due_date": "YYYY-MM-DD if a date/deadline is mentioned or inferable (e.g. 'Friday' = next Friday), or null",
  "priority": "low|medium|high|urgent — infer from tone/words, default medium",
  "recurrence": "daily|weekly|biweekly|monthly|null — only if explicitly repeated"
}}"""

# Staleness policy (simple + predictable)
STALE_UNTOUCHED_DAYS = 14
ABANDONED_UNASSIGNED_CANCEL_DAYS = 30

# Overdue follow-up policy (anti-spam)
# - Per-item cooldown uses the `escalations` table (reason='overdue_followup')
# - Hard caps to avoid flooding the partners channel
OVERDUE_FOLLOWUP_COOLDOWN_DAYS = 7
OVERDUE_FOLLOWUP_MAX_PER_RUN = 1
OVERDUE_FOLLOWUP_MAX_PER_OWNER_PER_RUN = 1

# Gamification (points are awarded only on real outcomes; stored idempotently)
# Now imported from config: POINTS_ACTION_DONE, POINTS_ACTION_DONE_RESOLVES_GAP_BONUS

# Partner Assignment Mapping - now centralized in config.py
PARTNER_MAPPING = PARTNER_EMOJI_TO_ID
REVERSE_PARTNER_MAPPING = PARTNER_ID_TO_EMOJI


# PM behaviors (WIP_LIMIT_IN_PROGRESS imported from config)
DAILY_TOP3_MAX_ITEMS = 3
DAILY_TOP3_LOOKBACK_LIMIT = 150
DAILY_TOP3_WEEKDAYS_ONLY = True

# Auto-assign defaults (optional)
AUTO_ASSIGN_UNOWNED_MAX_PER_RUN = 2


async def _award_partner_points(
    *,
    bot: commands.Bot,
    partner_user_id: int,
    partner_username: Optional[str],
    points: int,
    reason: str,
    entity_type: str,
    entity_id: int,
):
    if points <= 0:
        return
    if not getattr(bot, "db", None):
        return
    try:
        async with bot.db.acquire() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO partner_point_events (
                    partner_user_id,
                    partner_username,
                    entity_type,
                    entity_id,
                    reason,
                    points
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(partner_user_id),
                    (str(partner_username) if partner_username else None),
                    str(entity_type),
                    int(entity_id),
                    str(reason),
                    int(points),
                ),
            )
            await conn.commit()
    except Exception as e:
        logger.warning(f"Failed awarding partner points ({reason}) for {entity_type}#{entity_id}: {e}")


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _parse_dt_maybe(value: Optional[str]) -> Optional[datetime]:
    """Parse timestamps from either SQLite datetime('now') or ISO strings we write."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        # ISO format: 2026-01-01T12:34:56(.123)Z
        if s.endswith("Z"):
            s = s[:-1]
        # Normalize space-separated SQLite timestamps to ISO-ish
        if " " in s and "T" not in s:
            s = s.replace(" ", "T")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _parse_date_maybe(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        # Treat as midnight UTC for comparisons
        return datetime(d.year, d.month, d.day)
    except Exception:
        return None


def _append_note(existing: Optional[str], line: str) -> str:
    base = (existing or "").rstrip()
    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    entry = f"[{stamp}] {line}".strip()
    if not base:
        return entry
    return base + "\n" + entry


def _parse_due_date(due: Optional[str]) -> Optional[str]:
    if not due:
        return None
    s = due.strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        return d.isoformat()
    except Exception:
        return None


def _tags_json(extra: Optional[List[str]] = None) -> str:
    tags = [ACTION_TAG]
    if extra:
        for t in extra:
            if not t:
                continue
            if t not in tags:
                tags.append(t)
    return json.dumps(tags)


async def _spawn_next_recurrence(bot: commands.Bot, task_id: int) -> Optional[int]:
    """If a completed task has recurrence, clone it with the next due date.

    Returns the new task ID, or None if the task isn't recurring.
    """
    if not getattr(bot, "db", None):
        return None
    try:
        async with bot.db.acquire() as conn:
            async with conn.execute(
                """SELECT project_id, title, description, priority,
                          assigned_to_user_id, assigned_to_username,
                          created_by_user_id, created_by_username,
                          due_date, recurrence, tags
                   FROM tasks WHERE id = ?""",
                (int(task_id),),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return None

            recurrence = row[9]
            if not recurrence or recurrence not in RECURRENCE_DELTAS:
                return None

            delta = RECURRENCE_DELTAS[recurrence]
            old_due = row[8]
            if old_due:
                try:
                    next_due = (datetime.strptime(old_due, "%Y-%m-%d").date() + delta).isoformat()
                except Exception:
                    next_due = (datetime.now().date() + delta).isoformat()
            else:
                next_due = (datetime.now().date() + delta).isoformat()

            cur2 = await conn.execute(
                """INSERT INTO tasks (
                       project_id, title, description, status, priority,
                       assigned_to_user_id, assigned_to_username,
                       created_by_user_id, created_by_username,
                       due_date, recurrence, tags, created_at, updated_at
                   ) VALUES (?, ?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row[0], row[1], row[2], row[3],  # project, title, desc, priority
                    row[4], row[5],                    # assigned_to
                    row[6], row[7],                    # created_by
                    next_due, recurrence, row[10],     # due, recurrence, tags
                    _now_utc_iso(), _now_utc_iso(),
                ),
            )
            await conn.commit()
            new_id = cur2.lastrowid
            logger.info(
                "Spawned recurring task #%s → #%s (%s, due %s)",
                task_id, new_id, recurrence, next_due,
            )
            return new_id
    except Exception as exc:
        logger.warning("Failed to spawn recurring task from #%s: %s", task_id, exc)
        return None


async def _get_in_progress_count(*, bot: commands.Bot, owner_user_id: int) -> int:
    if not getattr(bot, "db", None):
        return 0
    try:
        async with bot.db.acquire() as conn, conn.execute(
            """
                SELECT COUNT(*)
                FROM tasks
                WHERE tags LIKE ?
                  AND status = 'in_progress'
                  AND assigned_to_user_id = ?
                """,
            (f"%{ACTION_TAG}%", int(owner_user_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0) if row else 0
    except Exception:
        return 0


async def _update_action_status(
    *,
    bot: commands.Bot,
    action_id: int,
    new_status: str,
    actor_user_id: int,
    actor_username: str,
) -> bool:
    """Update action status and run the same outcome logic used by the list view."""
    if not getattr(bot, "db", None):
        return False
    try:
        async with bot.db.acquire() as conn:
            # Load existing status (needed for blocked_since behavior)
            async with conn.execute(
                """
                SELECT status
                FROM tasks
                WHERE id = ?
                LIMIT 1
                """,
                (int(action_id),),
            ) as cursor:
                row0 = await cursor.fetchone()
                old_status = str(row0[0]) if row0 else None

            completed_at = _now_utc_iso() if new_status == "done" else None
            await conn.execute(
                """UPDATE tasks
                SET status = ?, updated_at = ?, completed_at = ?
                WHERE id = ?""",
                (str(new_status), _now_utc_iso(), completed_at, int(action_id)),
            )

            # If marking as done, resolve related knowledge gaps
            resolved_gap_count = 0
            if new_status == "done":
                async with conn.execute(
                    """
                    SELECT gap_id, link_type
                    FROM action_gap_links
                    WHERE action_id = ?
                    """,
                    (int(action_id),),
                ) as cursor:
                    linked_gaps = await cursor.fetchall()

                for gap_row in linked_gaps or []:
                    gap_id = gap_row[0]
                    link_type = gap_row[1]
                    if link_type == "resolves":
                        await conn.execute(
                            """
                            UPDATE knowledge_gaps
                            SET status = 'resolved',
                                resolved_at = ?,
                                resolved_via = 'action_item'
                            WHERE id = ? AND status = 'open'
                            """,
                            (_now_utc_iso(), int(gap_id)),
                        )
                        resolved_gap_count += 1

                # ── Auto-link: find open gaps whose topic appears in this action's title ──
                try:
                    async with conn.execute(
                        "SELECT title FROM tasks WHERE id = ?", (int(action_id),)
                    ) as tcur:
                        trow = await tcur.fetchone()
                    action_title = str(trow[0]).lower() if trow else ""
                    if action_title:
                        async with conn.execute(
                            "SELECT id, topic FROM knowledge_gaps WHERE status = 'open'"
                        ) as gcur:
                            open_gaps = await gcur.fetchall()
                        for g in open_gaps or []:
                            gap_topic = str(g[1]).lower()
                            # Match if the gap topic is a meaningful substring of the action title
                            if len(gap_topic) >= 4 and gap_topic in action_title:
                                await conn.execute(
                                    """INSERT OR IGNORE INTO action_gap_links
                                       (action_id, gap_id, link_type, notes)
                                       VALUES (?, ?, 'resolves', 'Auto-linked: action title matched gap topic')""",
                                    (int(action_id), int(g[0])),
                                )
                                await conn.execute(
                                    """UPDATE knowledge_gaps
                                       SET status = 'resolved',
                                           resolved_at = ?,
                                           resolved_via = 'action_item_auto'
                                       WHERE id = ? AND status = 'open'""",
                                    (_now_utc_iso(), int(g[0])),
                                )
                                resolved_gap_count += 1
                except Exception:
                    logger.debug("Auto gap-action linking skipped (non-critical)", exc_info=True)

                await _award_partner_points(
                    bot=bot,
                    partner_user_id=int(actor_user_id),
                    partner_username=str(actor_username) if actor_username else None,
                    points=POINTS_ACTION_DONE,
                    reason="action_done",
                    entity_type="action_item",
                    entity_id=int(action_id),
                )
                if resolved_gap_count > 0:
                    await _award_partner_points(
                        bot=bot,
                        partner_user_id=int(actor_user_id),
                        partner_username=str(actor_username) if actor_username else None,
                        points=POINTS_ACTION_DONE_RESOLVES_GAP_BONUS,
                        reason="action_done_resolves_gap_bonus",
                        entity_type="action_item",
                        entity_id=int(action_id),
                    )

            # If marking as blocked, update blocked_since timestamp
            if new_status == "blocked" and old_status != "blocked":
                try:
                    await conn.execute(
                        """UPDATE tasks SET blocked_since = ? WHERE id = ?""",
                        (_now_utc_iso(), int(action_id)),
                    )
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

            await conn.commit()
        return True
    except Exception as e:
        logger.warning(f"Failed to update action status for #{action_id}: {e}")
        return False


class SnoozeActionModal(discord.ui.Modal, title="Snooze action item"):
    def __init__(self, *, bot: commands.Bot, action_id: int, suggested_due: str):
        super().__init__()
        self.bot = bot
        self.action_id = int(action_id)
        self.reason = discord.ui.TextInput(
            label="Why snooze?",
            placeholder="One sentence on what's blocking / why it slipped",
            required=True,
            max_length=250,
        )
        self.new_due = discord.ui.TextInput(
            label="New due date (YYYY-MM-DD)",
            default=str(suggested_due),
            required=True,
            max_length=10,
        )
        self.add_item(self.reason)
        self.add_item(self.new_due)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        new_due = _parse_due_date(str(self.new_due.value))
        if not new_due:
            await interaction.followup.send("❌ Invalid date format. Use YYYY-MM-DD.", ephemeral=True)
            return
        try:
            async with self.bot.db.acquire() as conn:
                # Append note with accountability
                async with conn.execute(
                    """SELECT notes FROM tasks WHERE id = ?""",
                    (int(self.action_id),),
                ) as cursor:
                    row = await cursor.fetchone()
                    existing_notes = str(row[0]) if row and row[0] else None
                new_notes = _append_note(existing_notes, f"Snoozed to {new_due}: {str(self.reason.value).strip()}")
                await conn.execute(
                    """
                    UPDATE tasks
                    SET due_date = ?, updated_at = ?, notes = ?
                    WHERE id = ?
                    """,
                    (str(new_due), _now_utc_iso(), new_notes, int(self.action_id)),
                )
                # Record snooze event (as a dismissed escalation entry so we can audit later)
                try:
                    await conn.execute(
                        """
                        INSERT INTO escalations (entity_type, entity_id, reason, escalated_to_user_id, escalated_to_username, escalation_message, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'dismissed')
                        """,
                        (
                            "action_item",
                            int(self.action_id),
                            "snooze",
                            int(interaction.user.id),
                            str(interaction.user),
                            f"Snoozed to {new_due}: {str(self.reason.value).strip()}",
                        ),
                    )
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                await conn.commit()
            await interaction.followup.send(f"✅ Snoozed **#{self.action_id}** to **{new_due}**", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to snooze action #{self.action_id}: {e}")
            await interaction.followup.send("❌ Failed to snooze", ephemeral=True)


class DailyTopActionsView(discord.ui.View):
    def __init__(self, *, bot: commands.Bot, rows: List[Dict[str, Any]], owner_user_id: int):
        super().__init__(timeout=3600)
        self.bot = bot
        self.rows = rows
        self.owner_user_id = int(owner_user_id)

        for idx, r in enumerate(rows[:DAILY_TOP3_MAX_ITEMS]):
            action_id = int(r["id"])
            title = str(r.get("title") or "")
            due_date = (str(r.get("due_date")).strip() if r.get("due_date") else "")
            row = idx  # 0..2

            done_btn = discord.ui.Button(label=f"✅ Done #{action_id}", style=discord.ButtonStyle.success, row=row)
            snooze_btn = discord.ui.Button(label="🕒 Snooze", style=discord.ButtonStyle.secondary, row=row)
            today_btn = discord.ui.Button(label="Today", style=discord.ButtonStyle.primary, row=row)
            week_btn = discord.ui.Button(label="This week", style=discord.ButtonStyle.primary, row=row)
            month_btn = discord.ui.Button(label="This month", style=discord.ButtonStyle.primary, row=row)

            async def _done_callback(interaction: discord.Interaction, *, _id: int = action_id):
                await interaction.response.defer(ephemeral=True)
                ok = await _update_action_status(
                    bot=self.bot,
                    action_id=int(_id),
                    new_status="done",
                    actor_user_id=int(interaction.user.id),
                    actor_username=getattr(interaction.user, "display_name", None) or str(interaction.user),
                )
                await interaction.followup.send(
                    f"✅ Marked **#{_id}** done" if ok else f"❌ Failed to mark **#{_id}** done",
                    ephemeral=True,
                )

            async def _snooze_callback(interaction: discord.Interaction, *, _id: int = action_id):
                suggested = (datetime.now(EASTERN).date() + timedelta(days=3)).isoformat()
                await interaction.response.send_modal(SnoozeActionModal(bot=self.bot, action_id=int(_id), suggested_due=suggested))

            async def _triage_callback(interaction: discord.Interaction, *, _id: int = action_id, horizon: str = "week"):
                await interaction.response.defer(ephemeral=True)
                if not getattr(self.bot, "db", None):
                    await interaction.followup.send("❌ Database unavailable", ephemeral=True)
                    return
                # Convert horizon to a concrete due date
                now = datetime.now(EASTERN).date()
                if horizon == "today":
                    new_due = now
                elif horizon == "month":
                    new_due = (now.replace(day=1) + timedelta(days=32)).replace(day=1)  # first day next month
                    new_due = new_due + timedelta(days=14)  # mid-month default
                else:
                    # Align to Thursday async cadence
                    delta = (3 - now.weekday()) % 7  # Thursday=3
                    new_due = now + timedelta(days=delta or 7)
                try:
                    async with self.bot.db.acquire() as conn:
                        async with conn.execute(
                            """SELECT notes FROM tasks WHERE id = ?""",
                            (int(_id),),
                        ) as cursor:
                            row = await cursor.fetchone()
                            existing_notes = str(row[0]) if row and row[0] else None
                        new_notes = _append_note(existing_notes, f"Triage: set due to {new_due.isoformat()} ({horizon})")
                        await conn.execute(
                            """UPDATE tasks SET due_date = ?, updated_at = ?, notes = ? WHERE id = ?""",
                            (str(new_due.isoformat()), _now_utc_iso(), new_notes, int(_id)),
                        )
                        await conn.commit()
                    await interaction.followup.send(f"📅 Set **#{_id}** due to **{new_due.isoformat()}**", ephemeral=True)
                except Exception as e:
                    logger.warning(f"Failed triage update for #{_id}: {e}")
                    await interaction.followup.send("❌ Failed to update due date", ephemeral=True)

            done_btn.callback = _done_callback
            snooze_btn.callback = _snooze_callback
            today_btn.callback = lambda interaction, _id=action_id: _triage_callback(interaction, _id=int(_id), horizon="today")
            week_btn.callback = lambda interaction, _id=action_id: _triage_callback(interaction, _id=int(_id), horizon="week")
            month_btn.callback = lambda interaction, _id=action_id: _triage_callback(interaction, _id=int(_id), horizon="month")

            self.add_item(done_btn)
            self.add_item(snooze_btn)
            # Only show triage buttons if due_date is missing (reprioritization prompt)
            if not due_date:
                self.add_item(today_btn)
                self.add_item(week_btn)
                self.add_item(month_btn)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Handle view errors gracefully."""
        logger.error(f"DailyTopActionsView error: {error}", exc_info=True)
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
        logger.debug("DailyTopActionsView timed out")


def _safe_tags(tags_text: Optional[str]) -> List[str]:
    if not tags_text:
        return []
    try:
        val = json.loads(tags_text)
        if isinstance(val, list):
            return [str(x) for x in val]
        return []
    except Exception:
        return []


async def _get_task_owners(bot: commands.Bot, task_id: int) -> List[int]:
    if not getattr(bot, "db", None):
        return []
    try:
        async with bot.db.acquire() as conn:
            async with conn.execute(
                "SELECT user_id FROM task_owners WHERE task_id = ?", (int(task_id),)
            ) as cursor:
                rows = await cursor.fetchall()
            return [int(r[0]) for r in rows]
    except Exception:
        return []


async def _add_task_owner(bot: commands.Bot, task_id: int, user_id: int):
    if not getattr(bot, "db", None):
        return
    try:
        async with bot.db.acquire() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO task_owners (task_id, user_id) VALUES (?, ?)",
                (int(task_id), int(user_id))
            )
            await conn.commit()
    except Exception as e:
        logger.error(f"Failed to add owner {user_id} to task {task_id}: {e}")


async def _remove_task_owner(bot: commands.Bot, task_id: int, user_id: int):
    if not getattr(bot, "db", None):
        return
    try:
        async with bot.db.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_owners WHERE task_id = ? AND user_id = ?",
                (int(task_id), int(user_id))
            )
            await conn.commit()
    except Exception as e:
        logger.error(f"Failed to remove owner {user_id} from task {task_id}: {e}")


@dataclass
class ActionItemRow:
    id: int
    title: str
    description: str
    status: str
    priority: str
    due_date: Optional[str]
    assigned_to_user_id: Optional[int]
    assigned_to_username: Optional[str]
    created_by_user_id: Optional[int]
    created_by_username: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


class ActionAddModal(discord.ui.Modal, title="Create Action Item"):
    title_input = discord.ui.TextInput(
        label="Action (short title)",
        style=discord.TextStyle.short,
        placeholder="e.g., Draft Jan pricing memo; confirm AV vendor budget",
        required=True,
        max_length=120,
        row=0,
    )

    assignee_input = discord.ui.TextInput(
        label="Assignee (optional)",
        style=discord.TextStyle.short,
        placeholder="e.g., Alex, @alex",
        required=False,
        max_length=120,
        row=1,
    )

    due_input = discord.ui.TextInput(
        label="Due date (optional)",
        style=discord.TextStyle.short,
        placeholder="YYYY-MM-DD",
        required=False,
        max_length=20,
        row=2,
    )

    priority_input = discord.ui.TextInput(
        label="Priority (optional)",
        style=discord.TextStyle.short,
        placeholder="low, medium, high, urgent",
        required=False,
        max_length=20,
        row=3,
    )

    details_input = discord.ui.TextInput(
        label="Details / Context",
        style=discord.TextStyle.paragraph,
        placeholder="Include what 'done' means, links, constraints, and any key context.",
        required=False,
        max_length=1800,
        row=4,
    )

    def __init__(
        self,
        *,
        bot: commands.Bot,
        owner: Optional[discord.User],
        due_date: Optional[str],
        priority: str,
        recurrence: Optional[str],
        created_by: discord.User,
        prefill_title: Optional[str] = None,
        prefill_details: Optional[str] = None,
        prefill_assignee: Optional[str] = None,
        prefill_due: Optional[str] = None,
        prefill_priority: Optional[str] = None,
    ):
        super().__init__()
        self.bot = bot
        self.owner = owner
        self.due_date = due_date
        self.priority = priority
        self.recurrence = recurrence
        self.created_by = created_by

        if prefill_title:
            self.title_input.default = prefill_title[:120]
        if prefill_details:
            self.details_input.default = prefill_details[:1800]
        if prefill_assignee:
            self.assignee_input.default = prefill_assignee[:120]
        if prefill_due:
            self.due_input.default = prefill_due[:20]
        if prefill_priority:
            self.priority_input.default = prefill_priority[:20]


class ActionQuickPrefillView(discord.ui.View):
    """One-click open for a prefilled action modal."""

    def __init__(self, *, user_id: int, modal_kwargs: dict, summary: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.modal_kwargs = modal_kwargs
        self.summary = summary

    @discord.ui.button(label="Review & Create", style=discord.ButtonStyle.success)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the original user can do this.", ephemeral=True)
            return
        await interaction.response.send_modal(ActionAddModal(**self.modal_kwargs))
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

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not getattr(self.bot, "db", None):
            await interaction.followup.send(
                embed=create_error_embed("Database unavailable", "Action items require SQLite DB."),
                ephemeral=True,
            )
            return

        title = str(self.title_input.value).strip()
        description = str(self.details_input.value or "").strip()
        priority_raw = str(self.priority_input.value or "").strip().lower()
        priority_norm = priority_raw if priority_raw else (self.priority or "medium")
        if priority_norm not in {"low", "medium", "high", "urgent"}:
            priority_norm = "medium"

        assignee_text = str(self.assignee_input.value or "").strip()
        resolved_owner = self.owner
        if assignee_text:
            guild = interaction.guild
            if guild:
                name_lower = assignee_text.lower().lstrip("@")
                for member in guild.members:
                    if (
                        name_lower in (member.display_name or "").lower()
                        or name_lower in (member.name or "").lower()
                    ):
                        resolved_owner = member
                        break

        due_override = str(self.due_input.value or "").strip()
        if due_override:
            parsed = _parse_due_date(due_override)
            if not parsed:
                await interaction.followup.send(
                    embed=create_error_embed("Invalid due date", "Use YYYY-MM-DD."),
                    ephemeral=True,
                )
                return
            due_final = parsed
        else:
            due_final = self.due_date

        if not title:
            await interaction.followup.send(
                embed=create_error_embed("Missing title", "Please provide a short action title."),
                ephemeral=True,
            )
            return

        try:
            async with self.bot.db.acquire() as conn:
                cursor = await conn.execute(
                    """
                    INSERT INTO tasks (
                        project_id,
                        title,
                        description,
                        status,
                        priority,
                        assigned_to_user_id,
                        assigned_to_username,
                        created_by_user_id,
                        created_by_username,
                        due_date,
                        recurrence,
                        created_at,
                        updated_at,
                        tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        None,
                        title,
                        description or None,
                        "todo",
                        priority_norm,
                        int(resolved_owner.id) if resolved_owner else None,
                        str(resolved_owner) if resolved_owner else None,
                        int(self.created_by.id) if self.created_by else None,
                        str(self.created_by) if self.created_by else None,
                        due_final,
                        self.recurrence,
                        _now_utc_iso(),
                        _now_utc_iso(),
                        _tags_json(),
                    ),
                )
                await conn.commit()
                item_id = int(cursor.lastrowid)

            # Sync initial owner to task_owners if exists
            if resolved_owner:
                await _add_task_owner(self.bot, item_id, resolved_owner.id)

            # Post to Partners Channel via ActionItems Cog
            cog = self.bot.get_cog("ActionItems")
            if cog:
                await cog.post_new_action(item_id)
            
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads") # For mention in response

            recur_label = f" 🔁 repeats {self.recurrence}" if self.recurrence else ""
            await interaction.followup.send(
                embed=create_success_embed(
                    "Action item created",
                    f"**#{item_id}** {title}{recur_label}\nPosted to {partners_channel.mention if partners_channel else 'channel'}",
                    footer="Partners can claim/unclaim via emoji reactions.",
                ),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Failed to create action item: {e}")
            await interaction.followup.send(
                embed=create_error_embed(
                    "Create failed",
                    "Could not create the action item. Ensure migrations have been applied (tasks table exists).",
                ),
                ephemeral=True,
            )


class ActionItemSelect(discord.ui.Select):
    def __init__(self, rows: List[ActionItemRow]):
        self.rows = rows
        options: List[discord.SelectOption] = []
        for r in rows[:25]:
            due = f" due {r.due_date}" if r.due_date else ""
            owner = f" @{r.assigned_to_username}" if r.assigned_to_username else ""
            label = f"#{r.id} {r.title}"[:100]
            desc = f"{r.status}{due}{owner}"[:100]
            options.append(discord.SelectOption(label=label, description=desc, value=str(r.id)))

        super().__init__(
            placeholder="Select an action item…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: ActionListView = self.view  # type: ignore
        selected_id = int(self.values[0])
        view.selected_id = selected_id
        await view.refresh(interaction)


class ActionListView(discord.ui.View):
    def __init__(self, *, bot: commands.Bot, rows: List[ActionItemRow]):
        super().__init__(timeout=600)
        self.bot = bot
        self.rows = rows
        self.selected_id: Optional[int] = rows[0].id if rows else None

        if rows:
            self.add_item(ActionItemSelect(rows))

        self._set_buttons_enabled(bool(self.selected_id))

    def _row_by_id(self, item_id: Optional[int]) -> Optional[ActionItemRow]:
        if not item_id:
            return None
        for r in self.rows:
            if r.id == item_id:
                return r
        return None

    def _set_buttons_enabled(self, enabled: bool):
        for child in self.children:
            if isinstance(child, discord.ui.Button) and getattr(child, "custom_id", "").startswith("action_"):
                child.disabled = not enabled

    def _build_embed(self) -> discord.Embed:
        if not self.rows:
            return create_info_embed("Action items", "No action items found.")

        selected = self._row_by_id(self.selected_id)
        lines = []
        for r in self.rows[:10]:
            due = f" (due {r.due_date})" if r.due_date else ""
            owner = f" — @{r.assigned_to_username}" if r.assigned_to_username else ""
            lines.append(f"• **#{r.id}** [{r.status}] {r.title}{due}{owner}")

        embed = create_info_embed("Action items", "\n".join(lines))

        if selected:
            fields = []
            fields.append(("Selected", f"**#{selected.id}** {selected.title}", False))
            fields.append(("Status", selected.status, True))
            if selected.priority:
                fields.append(("Priority", selected.priority, True))
            if selected.due_date:
                fields.append(("Due", selected.due_date, True))
            if selected.assigned_to_username:
                fields.append(("Owner", f"@{selected.assigned_to_username}", True))
            if selected.description:
                snippet = selected.description[:700]
                fields.append(("Details", snippet, False))
            for name, value, inline in fields:
                embed.add_field(name=name, value=value, inline=inline)

            embed.set_footer(text="Use the buttons below to update status/ownership.")

        return embed

    async def refresh(self, interaction: discord.Interaction):
        embed = self._build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _update_status(self, interaction: discord.Interaction, new_status: str):
        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        row = self._row_by_id(self.selected_id)
        if not row:
            await interaction.followup.send("❌ No action item selected", ephemeral=True)
            return

        # WIP limit enforcement: prevent starting more work than the limit.
        if new_status == "in_progress":
            try:
                current_wip = await _get_in_progress_count(bot=self.bot, owner_user_id=int(interaction.user.id))
                if (row.status != "in_progress") and current_wip >= WIP_LIMIT_IN_PROGRESS:
                    await interaction.followup.send(
                        f"⛔ WIP limit reached (**{current_wip}/{WIP_LIMIT_IN_PROGRESS}** in progress). "
                        "Finish/close something first, then start a new one.",
                        ephemeral=True,
                    )
                    return
            except Exception as e:
                logger.warning("_update_status: suppressed %s", e)

        try:
            ok = await _update_action_status(
                bot=self.bot,
                action_id=int(row.id),
                new_status=str(new_status),
                actor_user_id=int(interaction.user.id),
                actor_username=getattr(interaction.user, "display_name", None) or str(interaction.user),
            )
            if not ok:
                raise RuntimeError("status update failed")

            row.status = new_status

            # If it's now done/cancelled, remove from the current list for a satisfying "vanish" effect.
            if new_status in {"done", "cancelled"}:
                self.rows = [r for r in self.rows if r.id != row.id]
                self.selected_id = self.rows[0].id if self.rows else None
                self.clear_items()
                if self.rows:
                    self.add_item(ActionItemSelect(self.rows))
                self._set_buttons_enabled(bool(self.selected_id))

            await interaction.followup.send(f"✅ Updated **#{row.id}** to `{new_status}`", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to update status: {e}")
            await interaction.followup.send("❌ Failed to update status", ephemeral=True)

    async def _assign(self, interaction: discord.Interaction, owner: Optional[discord.User]):
        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return
        row = self._row_by_id(self.selected_id)
        if not row:
            await interaction.followup.send("❌ No action item selected", ephemeral=True)
            return

        try:
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE tasks
                    SET assigned_to_user_id = ?, assigned_to_username = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(owner.id) if owner else None,
                        str(owner) if owner else None,
                        _now_utc_iso(),
                        int(row.id),
                    ),
                )
                await conn.commit()

            row.assigned_to_user_id = int(owner.id) if owner else None
            row.assigned_to_username = str(owner) if owner else None
            await interaction.followup.send(
                f"✅ Assigned **#{row.id}** to {owner.mention if owner else 'unassigned'}",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Failed to assign owner: {e}")
            await interaction.followup.send("❌ Failed to assign owner", ephemeral=True)

    @discord.ui.button(label="✅ Mark done", style=discord.ButtonStyle.success, custom_id="action_done")
    async def mark_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_status(interaction, "done")

    @discord.ui.button(label="🏃 In progress", style=discord.ButtonStyle.primary, custom_id="action_in_progress")
    async def mark_in_progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_status(interaction, "in_progress")

    @discord.ui.button(label="⛔ Blocked", style=discord.ButtonStyle.secondary, custom_id="action_blocked")
    async def mark_blocked(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_status(interaction, "blocked")

    @discord.ui.button(label="🙋 Assign to me", style=discord.ButtonStyle.secondary, custom_id="action_assign_me")
    async def assign_to_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._assign(interaction, interaction.user)

    @discord.ui.button(label="👤 Unassign", style=discord.ButtonStyle.secondary, custom_id="action_unassign")
    async def unassign(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._assign(interaction, None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Handle view errors gracefully."""
        logger.error(f"ActionListView error: {error}", exc_info=True)
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
        logger.debug("ActionListView timed out")


class ActionBulkCompleteSelect(discord.ui.Select):
    def __init__(self, rows: List[ActionItemRow]):
        self.rows = rows
        options: List[discord.SelectOption] = []
        for r in rows[:25]:
            due = f" due {r.due_date}" if r.due_date else ""
            owner = f" @{r.assigned_to_username}" if r.assigned_to_username else ""
            label = f"#{r.id} {r.title}"[:100]
            desc = f"{r.status}{due}{owner}"[:100]
            options.append(discord.SelectOption(label=label, description=desc, value=str(r.id)))

        max_values = max(1, min(25, len(options)))
        super().__init__(
            placeholder="Select completed items to mark done…",
            min_values=1,
            max_values=max_values,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: ActionBulkCompleteView = self.view  # type: ignore
        view.selected_ids = [int(v) for v in self.values]
        await view.refresh(interaction)


class ActionBulkCompleteView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        rows: List[ActionItemRow],
        owner_user_id: Optional[int] = None,
        status_filter: Tuple[str, ...] = ("todo", "in_progress", "blocked"),
        limit: int = 25,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.rows = rows
        self.owner_user_id = owner_user_id
        self.status_filter = status_filter
        self.limit = max(1, min(int(limit or 25), 25))
        self.selected_ids: List[int] = []

        if rows:
            self.add_item(ActionBulkCompleteSelect(rows))

    def _build_embed(self) -> discord.Embed:
        if not self.rows:
            return create_success_embed("Action items", "✅ Nothing left to thin out.")

        lines: List[str] = []
        for r in self.rows[:10]:
            due = f" (due {r.due_date})" if r.due_date else ""
            owner = f" — @{r.assigned_to_username}" if r.assigned_to_username else ""
            lines.append(f"• **#{r.id}** [{r.status}] {r.title}{due}{owner}")

        selected_txt = (
            ", ".join([f"#{i}" for i in self.selected_ids[:12]])
            + ("…" if len(self.selected_ids) > 12 else "")
        ) if self.selected_ids else "(none)"

        embed = create_info_embed(
            "Action items — thin out",
            (
                "Select the items you already completed, then press **Mark selected done**. "
                "They’ll disappear from this list (and from meeting rollups).\n\n"
                + "\n".join(lines)
            ),
        )
        embed.add_field(name="Selected", value=selected_txt, inline=False)
        embed.set_footer(text="Tip: rerun /action thin anytime for a fresh list.")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        embed = self._build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _reload_rows(self) -> None:
        if not getattr(self.bot, "db", None):
            return
        try:
            async with self.bot.db.acquire() as conn:
                base = (
                    "SELECT id, title, description, status, priority, due_date, "
                    "assigned_to_user_id, assigned_to_username, created_by_user_id, created_by_username, created_at, updated_at, tags "
                    "FROM tasks "
                    "WHERE tags LIKE ? "
                    "AND status IN ({}) ".format(",".join(["?"] * len(self.status_filter)))
                )
                args: List[Any] = [f"%{ACTION_TAG}%", *self.status_filter]
                if self.owner_user_id is not None:
                    base += "AND assigned_to_user_id = ? "
                    args.append(int(self.owner_user_id))
                base += "ORDER BY (due_date IS NULL) ASC, due_date ASC, updated_at DESC LIMIT ?"
                args.append(int(self.limit))
                async with conn.execute(base, tuple(args)) as cursor:
                    rows = await cursor.fetchall()

            parsed: List[ActionItemRow] = []
            for r in rows or []:
                tags = _safe_tags(r[12])
                if ACTION_TAG not in tags:
                    continue
                parsed.append(
                    ActionItemRow(
                        id=int(r[0]),
                        title=str(r[1] or ""),
                        description=str(r[2] or ""),
                        status=str(r[3] or ""),
                        priority=str(r[4] or ""),
                        due_date=(str(r[5]) if r[5] else None),
                        assigned_to_user_id=(int(r[6]) if r[6] is not None else None),
                        assigned_to_username=(str(r[7]) if r[7] else None),
                        created_by_user_id=(int(r[8]) if r[8] is not None else None),
                        created_by_username=(str(r[9]) if r[9] else None),
                        created_at=(str(r[10]) if r[10] else None),
                        updated_at=(str(r[11]) if r[11] else None),
                    )
                )
            self.rows = parsed
            # Drop selections that no longer exist
            valid_ids = {r.id for r in self.rows}
            self.selected_ids = [i for i in self.selected_ids if i in valid_ids]
        except Exception as e:
            logger.warning(f"Failed to reload action items: {e}")

    def _rebuild_controls(self) -> None:
        self.clear_items()
        if self.rows:
            self.add_item(ActionBulkCompleteSelect(self.rows))

    @discord.ui.button(label="✅ Mark selected done", style=discord.ButtonStyle.success, custom_id="action_bulk_done")
    async def bulk_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.selected_ids:
            await interaction.followup.send("Select at least one item first.", ephemeral=True)
            return
        if not getattr(self.bot, "db", None):
            await interaction.followup.send("❌ Database unavailable", ephemeral=True)
            return

        actor_username = getattr(interaction.user, "display_name", None) or str(interaction.user)
        marked = 0
        failed: List[int] = []
        for action_id in list(self.selected_ids):
            try:
                ok = await _update_action_status(
                    bot=self.bot,
                    action_id=int(action_id),
                    new_status="done",
                    actor_user_id=int(interaction.user.id),
                    actor_username=str(actor_username),
                )
                if ok:
                    marked += 1
                    self.rows = [r for r in self.rows if r.id != int(action_id)]
                else:
                    failed.append(int(action_id))
            except Exception:
                failed.append(int(action_id))

        self.selected_ids = []
        self._rebuild_controls()
        embed = self._build_embed()
        await interaction.edit_original_response(embed=embed, view=self)

        msg = f"✅ Marked **{marked}** item(s) done."
        if failed:
            msg += f" Failed: {', '.join([f'#{i}' for i in failed[:10]])}"
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="↻ Refresh", style=discord.ButtonStyle.secondary, custom_id="action_bulk_refresh")
    async def bulk_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self._reload_rows()
        self._rebuild_controls()
        embed = self._build_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Handle view errors gracefully."""
        logger.error(f"ActionBulkCompleteView error: {error}", exc_info=True)
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
        logger.debug("ActionBulkCompleteView timed out")


class ActionItems(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _smart_extract_action(self, text: str) -> dict:
        """Use LLM to extract structured action fields from natural language.

        Returns dict with keys: title, details, assignee_name, due_date, priority, recurrence.
        Falls back to text-as-title on any failure.
        """
        fallback = {
            "title": text[:120].strip(),
            "details": "",
            "assignee_name": None,
            "due_date": None,
            "priority": "medium",
            "recurrence": None,
        }
        llm = getattr(getattr(self.bot, "service_container", None), "llm", None)
        if not llm:
            return fallback
        try:
            prompt = ACTION_EXTRACT_PROMPT.format(
                content=text[:2000],
                today=datetime.now().strftime("%Y-%m-%d"),
            )
            raw = await llm.complete(prompt, max_tokens=300, temperature=0.0)
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return fallback
            result = json.loads(m.group())
            result.setdefault("title", fallback["title"])
            result.setdefault("details", "")
            result.setdefault("assignee_name", None)
            result.setdefault("due_date", None)
            result.setdefault("priority", "medium")
            result.setdefault("recurrence", None)
            # Validate priority
            if result["priority"] not in {"low", "medium", "high", "urgent"}:
                result["priority"] = "medium"
            # Validate recurrence
            if result["recurrence"] and result["recurrence"] not in RECURRENCE_DELTAS:
                result["recurrence"] = None
            # Validate due_date format
            if result["due_date"]:
                try:
                    datetime.strptime(result["due_date"], "%Y-%m-%d")
                except ValueError:
                    result["due_date"] = None
            return result
        except Exception as exc:
            logger.warning("Smart action extraction failed: %s", exc)
            return fallback

    async def _auto_assign_unowned_action_items(
        self,
        *,
        conn,
        partners_channel: discord.abc.Messageable,
    ) -> int:
        """Best-effort: auto-assign a small number of unowned action items to keep momentum.

        Uses config.DEFAULT_ACTION_ITEM_OWNER_USER_ID when set.
        """
        default_owner_id = getattr(leisure_config, "DEFAULT_ACTION_ITEM_OWNER_USER_ID", None)
        if not default_owner_id:
            return 0
        try:
            default_owner_id = int(default_owner_id)
        except Exception:
            return 0

        assigned = 0
        try:
            async with conn.execute(
                """
                SELECT id, title
                FROM tasks
                WHERE tags LIKE ?
                  AND status IN ('todo','in_progress','blocked')
                  AND assigned_to_user_id IS NULL
                ORDER BY (due_date IS NULL) ASC, due_date ASC, updated_at ASC
                LIMIT ?
                """,
                (f"%{ACTION_TAG}%", int(AUTO_ASSIGN_UNOWNED_MAX_PER_RUN) * 5),
            ) as cursor:
                candidates = await cursor.fetchall()
        except Exception:
            return 0

        if not candidates:
            return 0

        # Respect WIP limit: if default owner is already overloaded, don’t auto-assign.
        try:
            wip = await _get_in_progress_count(bot=self.bot, owner_user_id=int(default_owner_id))
            if wip >= WIP_LIMIT_IN_PROGRESS:
                return 0
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        default_user = self.bot.get_user(int(default_owner_id))
        if not default_user:
            try:
                default_user = await self.bot.fetch_user(int(default_owner_id))
            except Exception:
                default_user = None
        default_username = str(default_user)[:120] if default_user else None

        for r in candidates:
            if assigned >= AUTO_ASSIGN_UNOWNED_MAX_PER_RUN:
                break
            try:
                action_id = int(r[0])
                title = str(r[1] or "")
            except Exception:
                continue

            # Avoid re-assigning the same item repeatedly.
            try:
                async with conn.execute(
                    """
                    SELECT 1
                    FROM escalations
                    WHERE entity_type = 'action_item'
                      AND entity_id = ?
                      AND reason = 'auto_assign_default'
                    LIMIT 1
                    """,
                    (action_id,),
                ) as cursor:
                    exists = await cursor.fetchone()
                if exists:
                    continue
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            try:
                async with conn.execute(
                    """SELECT notes FROM tasks WHERE id = ?""",
                    (action_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    notes = str(row[0]) if row and row[0] else None
                new_notes = _append_note(notes, f"Auto-assigned default owner <@{default_owner_id}> (task was unowned).")
                await conn.execute(
                    """
                    UPDATE tasks
                    SET assigned_to_user_id = ?, assigned_to_username = ?, updated_at = ?, notes = ?
                    WHERE id = ? AND assigned_to_user_id IS NULL
                    """,
                    (int(default_owner_id), default_username, _now_utc_iso(), new_notes, action_id),
                )
                await conn.execute(
                    """
                    INSERT INTO escalations (
                        entity_type, entity_id, reason,
                        escalated_to_user_id, escalated_to_username,
                        escalation_message, status
                    ) VALUES ('action_item', ?, 'auto_assign_default', ?, ?, ?, 'dismissed')
                    """,
                    (action_id, int(default_owner_id), default_username, f"Auto-assigned default owner for #{action_id}"),
                )
                await conn.commit()
                assigned += 1

                # Announce (with an override path)
                allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
                await partners_channel.send(
                    (
                        f"👤 Auto-assigned unowned action item **#{action_id}** to <@{default_owner_id}>: {title[:160]}\n"
                        f"If someone else should own it, use `/action list` → **Assign to me** (or reassign)."
                    )[:1950],
                    allowed_mentions=allowed_mentions,
                )
            except Exception:
                continue

        return assigned

    async def _send_overdue_followups(
        self,
        *,
        overdue_rows: List[Tuple],
        partners_channel: Optional[discord.abc.Messageable] = None,
    ):
        """Send one contextual follow-up message for overdue items with strict anti-spam rules."""
        if not overdue_rows:
            return
        if not getattr(self.bot, "db", None):
            return
        if not partners_channel:
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if not partners_channel:
            return

        now_utc = datetime.utcnow()
        today_utc = datetime(now_utc.year, now_utc.month, now_utc.day)
        cooldown_window = f"-{int(OVERDUE_FOLLOWUP_COOLDOWN_DAYS)} days"
        allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)

        # Prefer oldest due dates first.
        def _sort_key(r: Tuple) -> Tuple:
            _id, title, status, due_date, priority, owner_id, owner_name, updated_at, created_at, notes, tags = r
            due_dt = _parse_date_maybe(due_date)
            return (due_dt or today_utc, int(_id))

        sent_total = 0
        sent_by_owner: Dict[int, int] = {}

        backup_user_id = getattr(leisure_config, "OVERDUE_BACKUP_USER_ID", None)
        try:
            backup_user_id = int(backup_user_id) if backup_user_id else None
        except Exception:
            backup_user_id = None

        for r in sorted(overdue_rows, key=_sort_key):
            if sent_total >= OVERDUE_FOLLOWUP_MAX_PER_RUN:
                break

            _id, title, status, due_date, priority, owner_id, owner_name, updated_at, created_at, notes, tags = r
            if owner_id is None:
                continue
            owner_id_int = int(owner_id)
            if sent_by_owner.get(owner_id_int, 0) >= OVERDUE_FOLLOWUP_MAX_PER_OWNER_PER_RUN:
                continue

            # Keep the follow-up narrowly scoped: only overdue actionable states.
            if str(status) not in {"todo", "in_progress"}:
                continue

            due_dt = _parse_date_maybe(due_date)
            if not due_dt:
                continue
            days_overdue = max(1, (today_utc - due_dt).days)

            # Persistent cooldown check (per item) using the `escalations` table.
            # Also check ladder markers to avoid repeatedly escalating tiers.
            try:
                async with self.bot.db.acquire() as conn:
                    async with conn.execute(
                        """
                        SELECT 1
                        FROM escalations
                        WHERE entity_type = 'action_item'
                          AND entity_id = ?
                          AND reason = 'overdue_followup'
                          AND datetime(escalated_at) >= datetime('now', ?)
                        LIMIT 1
                        """,
                        (int(_id), cooldown_window),
                    ) as cursor:
                        recent = await cursor.fetchone()
                    if recent:
                        continue

                    # Ladder history (progressive escalation)
                    async with conn.execute(
                        """
                        SELECT
                            MAX(CASE WHEN reason = 'overdue_followup' THEN escalated_at END) as last_owner_ping,
                            MAX(CASE WHEN reason = 'overdue_backup_ping' THEN escalated_at END) as last_backup_ping
                        FROM escalations
                        WHERE entity_type = 'action_item'
                          AND entity_id = ?
                        """,
                        (int(_id),),
                    ) as cursor:
                        ladder = await cursor.fetchone()
                    last_owner_ping = _parse_dt_maybe(ladder[0]) if ladder else None
                    last_backup_ping = _parse_dt_maybe(ladder[1]) if ladder else None
            except Exception as e:
                # If cooldown check fails, be conservative: do not send.
                logger.warning(f"Overdue follow-up cooldown check failed for #{_id}: {e}")
                continue

            mention = f"<@{owner_id_int}>"
            backup_ping = ""
            role_ping = ""

            # Escalation ladder:
            # - Stage 1: owner ping (default)
            # - Stage 2: if >=7 days overdue AND we already pinged the owner previously, ping backup
            # - Stage 3: if >=14 days overdue AND backup was pinged previously, ping CTO/Partners roles
            if days_overdue >= 7 and backup_user_id and last_owner_ping:
                backup_ping = f"<@{int(backup_user_id)}>"
            if days_overdue >= 14 and last_backup_ping and hasattr(partners_channel, "guild") and getattr(partners_channel, "guild", None):
                try:
                    guild = partners_channel.guild
                    cto = discord.utils.get(guild.roles, name="CTO")
                    partners_role = discord.utils.get(guild.roles, name="Partners")
                    role_ping = (cto.mention if cto else "") or (partners_role.mention if partners_role else "")
                except Exception:
                    role_ping = ""

            msg = (
                f"⏰ Overdue action item follow-up: **#{_id}** {title} "
                f"(due {due_date}, {days_overdue}d overdue). {role_ping} {backup_ping} {mention} — what’s the status? "
                f"Update via `/action list` (buttons), mark done, or propose a new due date."
            ).strip()

            try:
                await partners_channel.send(msg[:1950], allowed_mentions=allowed_mentions)
            except Exception as e:
                logger.warning(f"Failed sending overdue follow-up for #{_id}: {e}")
                continue

            # Record the follow-up so we won't re-send within the cooldown window.
            try:
                async with self.bot.db.acquire() as conn:
                    followup_note = _append_note(notes, "🔔 Overdue follow-up sent")
                    await conn.execute(
                        """
                        INSERT INTO escalations (
                            entity_type,
                            entity_id,
                            reason,
                            escalated_to_user_id,
                            escalated_to_username,
                            escalation_message,
                            status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "action_item",
                            int(_id),
                            "overdue_followup",
                            owner_id_int,
                            (str(owner_name) if owner_name else None),
                            msg[:900],
                            "dismissed",
                        ),
                    )

                    if backup_ping and backup_user_id:
                        try:
                            await conn.execute(
                                """
                                INSERT INTO escalations (
                                    entity_type, entity_id, reason,
                                    escalated_to_user_id, escalated_to_username,
                                    escalation_message, status
                                ) VALUES ('action_item', ?, 'overdue_backup_ping', ?, ?, ?, 'dismissed')
                                """,
                                (
                                    int(_id),
                                    int(backup_user_id),
                                    None,
                                    f"Backup ping included for #{_id}",
                                ),
                            )
                        except Exception as e:
                            logger.warning("operation: suppressed %s", e)

                    # Escalation ladder marker (14+ days overdue)
                    if days_overdue >= 14:
                        try:
                            async with conn.execute(
                                """
                                SELECT 1
                                FROM escalations
                                WHERE entity_type = 'action_item'
                                  AND entity_id = ?
                                  AND reason = 'overdue_escalation'
                                  AND datetime(escalated_at) >= datetime('now', '-14 days')
                                LIMIT 1
                                """,
                                (int(_id),),
                            ) as cursor:
                                recent = await cursor.fetchone()
                            if not recent:
                                await conn.execute(
                                    """
                                    INSERT INTO escalations (
                                        entity_type, entity_id, reason,
                                        escalated_to_user_id, escalated_to_username,
                                        escalation_message, status
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        "action_item",
                                        int(_id),
                                        "overdue_escalation",
                                        owner_id_int,
                                        (str(owner_name) if owner_name else None),
                                        f"Overdue escalation: {days_overdue}d overdue",
                                        "open",
                                    ),
                                )
                                # Best-effort: mark task escalated
                                try:
                                    await conn.execute(
                                        """UPDATE tasks SET escalated = 1, escalation_notes = ? WHERE id = ?""",
                                        (f"Overdue {days_overdue}d", int(_id)),
                                    )
                                except Exception as e:
                                    logger.warning("operation: suppressed %s", e)
                        except Exception as e:
                            logger.warning("operation: suppressed %s", e)
                    # Do not update `updated_at` here; this is a bot nudge, not real progress.
                    await conn.execute(
                        """
                        UPDATE tasks
                        SET notes = ?
                        WHERE id = ?
                        """,
                        (followup_note, int(_id)),
                    )
                    await conn.commit()
            except Exception as e:
                logger.warning(f"Failed recording overdue follow-up for #{_id}: {e}")

            sent_total += 1
            sent_by_owner[owner_id_int] = sent_by_owner.get(owner_id_int, 0) + 1

    async def cog_load(self):
        # Start automation loops.
        # Note: weekly_action_checkin is a lightweight Monday check-in;
        # async meeting kickoff can still act as the deeper weekly agenda.
        if not self.daily_stale_sweep.is_running():
            self.daily_stale_sweep.start()
        if not self.daily_top3.is_running():
            self.daily_top3.start()
        if not self.weekly_escalation_check.is_running():
            self.weekly_escalation_check.start()
        if not self.weekly_action_checkin.is_running():
            self.weekly_action_checkin.start()

    async def cog_unload(self):
        if self.daily_stale_sweep.is_running():
            self.daily_stale_sweep.cancel()
        if self.daily_top3.is_running():
            self.daily_top3.cancel()
        if self.weekly_escalation_check.is_running():
            self.weekly_escalation_check.cancel()
        if self.weekly_action_checkin.is_running():
            self.weekly_action_checkin.cancel()

    async def _post_action_ops_message(self, text: str):
        """Post reminders: prefer bots channel via AutonomousOps, else weekly-meeting-threads."""
        ao = self.bot.get_cog("AutonomousOps")
        if ao and hasattr(ao, "post_to_bots_channel"):
            try:
                await ao.post_to_bots_channel("coordinator", text)
                return
            except Exception as e:
                logger.warning("_post_action_ops_message: suppressed %s", e)

        channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if channel:
            await channel.send(text)

    @tasks.loop(time=dt_time(hour=10, minute=30, tzinfo=EASTERN))
    async def weekly_escalation_check(self):
        """Weekly check for blocked items and unresponsive partners requiring escalation."""
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
                recorded = await self.bot.db.record_job_run("action_items_escalation_check", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"Escalation check idempotency failed; continuing: {e}")
        
        try:
            now_utc = datetime.utcnow()
            blocked_cutoff = now_utc - timedelta(days=14)  # 2 weeks
            
            # Find action items blocked for 2+ weeks
            blocked_items = []
            async with self.bot.db.acquire() as conn:
                # First, update blocked_since timestamp for newly blocked items
                await conn.execute(
                    """
                    UPDATE tasks
                    SET blocked_since = datetime('now')
                    WHERE tags LIKE ?
                      AND status = 'blocked'
                      AND blocked_since IS NULL
                    """,
                    (f"%{ACTION_TAG}%",)
                )
                await conn.commit()
                
                # Find items blocked for 2+ weeks that haven't been escalated
                async with conn.execute(
                    """
                    SELECT id, title, status, blocked_since, assigned_to_user_id, 
                           assigned_to_username, notes, escalated
                    FROM tasks
                    WHERE tags LIKE ?
                      AND status = 'blocked'
                      AND blocked_since IS NOT NULL
                      AND datetime(blocked_since) <= datetime(?, 'unixepoch')
                      AND (escalated IS NULL OR escalated = 0)
                    ORDER BY blocked_since ASC
                    LIMIT 20
                    """,
                    (f"%{ACTION_TAG}%", blocked_cutoff.timestamp()),
                ) as cursor:
                    rows = await cursor.fetchall()
                    blocked_items = [dict(r) for r in rows]
            
            # Escalate blocked items
            escalations = []
            if blocked_items:
                partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
                
                for item in blocked_items:
                    reason = f"Blocked for 2+ weeks (since {item['blocked_since']})"
                    escalations.append({
                        "type": "action_item",
                        "id": item['id'],
                        "title": item['title'],
                        "reason": reason,
                        "owner_id": int(item['assigned_to_user_id']) if item.get('assigned_to_user_id') is not None else None,
                        "owner_username": item.get('assigned_to_username') or None,
                    })
                    
                    # Mark as escalated in database
                    async with self.bot.db.acquire() as conn:
                        escalation_note = _append_note(item.get('notes'), f"🚨 Escalated: {reason}")
                        await conn.execute(
                            """
                            UPDATE tasks
                            SET escalated = 1, escalation_notes = ?, notes = ?
                            WHERE id = ?
                            """,
                            (reason, escalation_note, item['id'])
                        )
                        
                        # Log escalation
                        await conn.execute(
                            """
                            INSERT INTO escalations (entity_type, entity_id, reason, escalated_to_username, escalation_message)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                'action_item',
                                item['id'],
                                'blocked_2_weeks',
                                item.get('assigned_to_username'),
                                f"Action item #{item['id']} has been blocked for 2+ weeks and requires intervention"
                            )
                        )
                        await conn.commit()
            
            # Post escalation summary
            if escalations:
                lines = []
                lines.append("🚨 **Action Item Escalations**")
                lines.append(f"The following {len(escalations)} items have been blocked for 2+ weeks and need attention:")
                lines.append("")
                
                for esc in escalations[:10]:
                    owner_mention = f"<@{int(esc['owner_id'])}>" if esc.get('owner_id') is not None else 'Unassigned'
                    lines.append(f"- **#{esc['id']}** {esc['title']}")
                    lines.append(f"  *{esc['reason']}* - Owner: {owner_mention}")
                
                lines.append("")
                lines.append("Please review these items and either:")
                lines.append("1. Unblock them and update status")
                lines.append("2. Cancel if no longer relevant")
                lines.append("3. Reassign if the owner is unavailable")
                
                msg = "\n".join(lines)[:1950]
                
                partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
                if partners_channel:
                    allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
                    await partners_channel.send(msg, allowed_mentions=allowed_mentions)
                
                await self._post_action_ops_message(f"Escalation check: {len(escalations)} items escalated")
            else:
                await self._post_action_ops_message("Escalation check: No items requiring escalation")
            
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_escalation_check", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        
        except Exception as e:
            logger.error(f"weekly_escalation_check failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_escalation_check", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    @tasks.loop(time=dt_time(hour=9, minute=5, tzinfo=EASTERN))
    async def daily_stale_sweep(self):
        """Daily sweep to prevent action items from hanging around."""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return

        run_date = datetime.now(EASTERN).date().isoformat()
        
        # Check if Thursday agenda was posted today (skip sweep on meeting kickoff day)
        try:
            if hasattr(self.bot.db, "record_job_run"):
                # Check if agenda posted today (thursday_async_meeting job)
                agenda_posted_today = await self.bot.db.fetchone("""
SELECT id FROM job_runs
WHERE job_name = 'thursday_async_meeting'
AND date(started_at) = date(?)
AND status IN ('running', 'completed')
LIMIT 1
""",
(run_date,))
                if agenda_posted_today:
                    logger.info(f"Stale sweep skipped: Thursday agenda posted today ({run_date})")
                    return
        except Exception as e:
            logger.warning(f"Failed to check agenda status, continuing with sweep: {e}")
        
        try:
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("action_items_stale_sweep", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"Stale sweep idempotency failed; continuing: {e}")

        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, title, status, due_date, priority, assigned_to_user_id,
                           assigned_to_username, updated_at, created_at, notes, tags
                    FROM tasks
                    WHERE tags LIKE ?
                      AND status IN ('todo','in_progress','blocked')
                    ORDER BY updated_at ASC
                    LIMIT 200
                    """,
                (f"%{ACTION_TAG}%",),
            ) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                try:
                    if hasattr(self.bot.db, "complete_job_run"):
                        await self.bot.db.complete_job_run("action_items_stale_sweep", run_date)
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                return

            now_utc = datetime.utcnow()
            today_utc = datetime(now_utc.year, now_utc.month, now_utc.day)
            stale_cutoff = now_utc - timedelta(days=STALE_UNTOUCHED_DAYS)
            abandoned_cutoff = now_utc - timedelta(days=ABANDONED_UNASSIGNED_CANCEL_DAYS)

            overdue: List[Tuple] = []
            stale: List[Tuple] = []
            cancelled: List[Tuple] = []

            for r in rows:
                _id, title, status, due_date, priority, owner_id, owner_name, updated_at, created_at, notes, tags = r
                due_dt = _parse_date_maybe(due_date)
                updated_dt = _parse_dt_maybe(updated_at) or _parse_dt_maybe(created_at)

                is_overdue = bool(due_dt and due_dt < today_utc)
                is_stale = bool(updated_dt and updated_dt <= stale_cutoff)
                is_abandoned_unassigned = bool(
                    (owner_id is None)
                    and (status == "todo")
                    and (due_dt is None)
                    and (updated_dt and updated_dt <= abandoned_cutoff)
                )

                if is_abandoned_unassigned:
                    cancelled.append(r)
                    continue
                if is_overdue:
                    overdue.append(r)
                    continue
                if is_stale:
                    stale.append(r)
                    continue

            # Auto-cancel only the truly abandoned unassigned items.
            if cancelled:
                async with self.bot.db.acquire() as conn:
                    for r in cancelled:
                        _id, title, status, due_date, priority, owner_id, owner_name, updated_at, created_at, notes, tags = r
                        new_notes = _append_note(notes, "Auto-cancelled: unassigned todo with no updates for 30+ days.")
                        await conn.execute(
                            """
                            UPDATE tasks
                            SET status = 'cancelled', updated_at = ?, notes = ?
                            WHERE id = ?
                            """,
                            (_now_utc_iso(), new_notes, int(_id)),
                        )
                    await conn.commit()

            # If nothing to report, finish quietly.
            if not overdue and not stale and not cancelled:
                try:
                    if hasattr(self.bot.db, "complete_job_run"):
                        await self.bot.db.complete_job_run("action_items_stale_sweep", run_date)
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                return

            # Compose a concise reminder. Owners are pinged in the partners channel.
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            lines: List[str] = []
            lines.append("🧹 **Action items stale sweep**")
            lines.append(f"Overdue: {len(overdue)} | Untouched {STALE_UNTOUCHED_DAYS}+ days: {len(stale)} | Auto-cancelled: {len(cancelled)}")
            lines.append("Update via `/action list` (buttons) or create new via `/action add`.")
            lines.append("")

            def _fmt_row(r: Tuple) -> str:
                _id, title, status, due_date, priority, owner_id, owner_name, updated_at, created_at, notes, tags = r
                due_txt = f" (due {due_date})" if due_date else ""
                owner_txt = f" <@{int(owner_id)}>" if owner_id is not None else ""
                return f"- **#{_id}** [{status}] {title}{due_txt}{owner_txt}"

            if overdue:
                lines.append("**Overdue**")
                for r in overdue[:8]:
                    lines.append(_fmt_row(r))
                lines.append("")
            if stale:
                lines.append(f"**Untouched ({STALE_UNTOUCHED_DAYS}+ days)**")
                for r in stale[:8]:
                    lines.append(_fmt_row(r))
                lines.append("")
            if cancelled:
                lines.append("**Auto-cancelled (abandoned + unassigned)**")
                for r in cancelled[:5]:
                    _id, title, *_ = r
                    lines.append(f"- **#{_id}** {title}")

            msg = "\n".join(lines)[:1900]

            # Stale sweep posts to weekly-meeting-threads (partner-facing)
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            if partners_channel:
                allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
                await partners_channel.send(msg, allowed_mentions=allowed_mentions)

                # Send a small number of contextual overdue follow-ups (strictly rate-limited).
                await self._send_overdue_followups(overdue_rows=overdue, partners_channel=partners_channel)

            # Also post a short ops note to bots channel for transparency.
            await self._post_action_ops_message(
                f"Action items stale sweep: overdue={len(overdue)} stale={len(stale)} auto_cancelled={len(cancelled)}"
            )

            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_stale_sweep", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        except Exception as e:
            logger.error(f"daily_stale_sweep failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_stale_sweep", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    action = app_commands.Group(name="action", description="[Partners] Create and manage action items")

    @action.command(name="add", description="Create an action item — type naturally or fill the form")
    @app_commands.describe(
        quick="Describe the task in plain English (e.g. 'Alex: draft pricing memo by Jan 15, high priority, weekly')",
        owner="Optional owner (who should do it)",
        due="Optional due date YYYY-MM-DD",
        priority="Priority: low/medium/high/urgent",
        recurrence="Repeat: daily, weekly, biweekly, or monthly",
    )
    @app_commands.choices(recurrence=[
        app_commands.Choice(name="None (one-time)", value="none"),
        app_commands.Choice(name="Daily", value="daily"),
        app_commands.Choice(name="Weekly", value="weekly"),
        app_commands.Choice(name="Biweekly", value="biweekly"),
        app_commands.Choice(name="Monthly", value="monthly"),
    ])
    async def action_add(
        self,
        interaction: discord.Interaction,
        quick: Optional[str] = None,
        owner: Optional[discord.User] = None,
        due: Optional[str] = None,
        priority: str = "medium",
        recurrence: str = "none",
    ):
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return

        # --- Smart path: user typed a natural-language sentence ---
        if quick:
            await interaction.response.defer(ephemeral=True)
            extracted = await self._smart_extract_action(quick)
            prefill_title = extracted.get("title") if extracted else None
            prefill_details = extracted.get("details") if extracted else None
            prefill_assignee = extracted.get("assignee_name") if extracted else None
            prefill_due = extracted.get("due_date") if extracted else None
            prefill_priority = extracted.get("priority") if extracted else None
        else:
            extracted = None
            prefill_title = None
            prefill_details = None
            prefill_assignee = None
            prefill_due = None
            prefill_priority = None

        # --- Classic path: open the structured modal ---
        due_parsed = _parse_due_date(due)
        if due and not due_parsed:
            await interaction.response.send_message(
                "❌ Invalid `due` format. Use YYYY-MM-DD (e.g., 2026-01-15).",
                ephemeral=True,
            )
            return

        priority_norm = (priority or "medium").strip().lower()
        if priority_norm not in {"low", "medium", "high", "urgent"}:
            priority_norm = "medium"

        recurrence_norm = (recurrence or "").strip().lower()
        if recurrence_norm in ("none", "") or recurrence_norm not in RECURRENCE_DELTAS:
            recurrence_norm = None

        if quick:
            final_priority = priority_norm if priority != "medium" else (prefill_priority or "medium")
            final_recurrence = recurrence_norm
            if extracted and not final_recurrence:
                candidate = extracted.get("recurrence")
                if candidate in RECURRENCE_DELTAS:
                    final_recurrence = candidate
            modal_kwargs = {
                "bot": self.bot,
                "owner": owner,
                "due_date": due_parsed or prefill_due,
                "priority": final_priority,
                "recurrence": final_recurrence,
                "created_by": interaction.user,
                "prefill_title": prefill_title,
                "prefill_details": prefill_details,
                "prefill_assignee": prefill_assignee,
                "prefill_due": due_parsed or prefill_due,
                "prefill_priority": final_priority,
            }
            summary = (
                f"**Title:** {prefill_title or '-'}\n"
                f"**Assignee:** {prefill_assignee or '-'}\n"
                f"**Due:** {prefill_due or '-'}\n"
                f"**Priority:** {final_priority or 'medium'}\n"
                f"**Recurrence:** {final_recurrence or 'none'}"
            )
            view = ActionQuickPrefillView(
                user_id=interaction.user.id,
                modal_kwargs=modal_kwargs,
                summary=summary,
            )
            await interaction.followup.send(
                embed=create_info_embed("Prefilled action", summary),
                view=view,
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            ActionAddModal(
                bot=self.bot,
                owner=owner,
                due_date=due_parsed,
                priority=priority_norm,
                recurrence=recurrence_norm,
                created_by=interaction.user,
            )
        )

    @action.command(name="cleanup", description="[Partners] Bulk cancel stale tasks")
    @app_commands.describe(
        days="Cancel tasks not updated in this many days (default: 30)",
        status="Target status to clean (default: todo)",
        confirm="Set to True to actually delete"
    )
    async def cleanup(
        self,
        interaction: discord.Interaction,
        days: int = 30,
        status: str = "todo",
        confirm: bool = False
    ):
        # Check permissions
        if not is_partner(interaction):
            await interaction.response.send_message("Partners only.", ephemeral=True)
            return
            
        status = status.lower()
        if status not in ("todo", "blocked"):
            await interaction.response.send_message("Can only cleanup 'todo' or 'blocked' items.", ephemeral=True)
            return
            
        cutoff_date = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff_date.isoformat()
        
        async with self.bot.db.acquire() as conn:
            # Check count first
            async with conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at < ?",
                (status, cutoff_str)
            ) as cursor:
                count = (await cursor.fetchone())[0]
                
        if count == 0:
            await interaction.response.send_message(f"No stale '{status}' tasks found older than {days} days.", ephemeral=True)
            return
            
        if not confirm:
            await interaction.response.send_message(
                f"🧹 **Cleanup Preview**\nFound **{count}** tasks in `{status}` older than {days} days.\nRun command with `confirm: True` to cancel them.",
                ephemeral=True
            )
            return
            
        # Execute
        await self.bot.db.execute(
            """
            UPDATE tasks
            SET status = 'cancelled',
            updated_at = datetime('now'),
            notes = COALESCE(notes, '') || CHAR(10) || '[Auto-cleanup] Stale task cancelled.'
            WHERE status = ? AND updated_at < ?
            """,
            (status, cutoff_str)
            )
        await interaction.response.send_message(f"🗑️ Cancelled {count} stale tasks.", ephemeral=True)

    @action.command(name="list", description="List action items (interactive)")
    @app_commands.describe(
        owner="Filter by owner",
        status="open, todo, in_progress, blocked, done",
        mine="Only items assigned to you",
        limit="Max items (1-25)",
    )
    async def action_list(
        self,
        interaction: discord.Interaction,
        owner: Optional[discord.User] = None,
        status: str = "open",
        mine: bool = False,
        limit: int = 10,
    ):
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if not getattr(self.bot, "db", None):
            await interaction.followup.send(
                embed=create_error_embed("Database unavailable", "Action items require SQLite DB."),
                ephemeral=True,
            )
            return

        limit = max(1, min(int(limit or 10), 25))
        status_norm = (status or "open").strip().lower()

        if status_norm == "open":
            status_filter = ("todo", "in_progress", "blocked")
        else:
            status_filter = (status_norm,)

        target_owner_id: Optional[int] = None
        if mine:
            target_owner_id = int(interaction.user.id)
        elif owner:
            target_owner_id = int(owner.id)

        try:
            async with self.bot.db.acquire() as conn:
                base = (
                    "SELECT id, title, description, status, priority, due_date, "
                    "assigned_to_user_id, assigned_to_username, created_by_user_id, created_by_username, created_at, updated_at, tags "
                    "FROM tasks "
                    "WHERE tags LIKE ? "
                    "AND status IN ({}) ".format(",".join(["?"] * len(status_filter)))
                )

                args: List[Any] = [f"%{ACTION_TAG}%", *status_filter]

                if target_owner_id is not None:
                    base += "AND assigned_to_user_id = ? "
                    args.append(int(target_owner_id))

                base += "ORDER BY (due_date IS NULL) ASC, due_date ASC, updated_at DESC LIMIT ?"
                args.append(int(limit))

                async with conn.execute(base, tuple(args)) as cursor:
                    rows = await cursor.fetchall()

            parsed: List[ActionItemRow] = []
            for r in rows or []:
                tags = _safe_tags(r[12])
                if ACTION_TAG not in tags:
                    continue
                parsed.append(
                    ActionItemRow(
                        id=int(r[0]),
                        title=str(r[1] or ""),
                        description=str(r[2] or ""),
                        status=str(r[3] or ""),
                        priority=str(r[4] or ""),
                        due_date=(str(r[5]) if r[5] else None),
                        assigned_to_user_id=(int(r[6]) if r[6] is not None else None),
                        assigned_to_username=(str(r[7]) if r[7] else None),
                        created_by_user_id=(int(r[8]) if r[8] is not None else None),
                        created_by_username=(str(r[9]) if r[9] else None),
                        created_at=(str(r[10]) if r[10] else None),
                        updated_at=(str(r[11]) if r[11] else None),
                    )
                )

            if not parsed:
                await interaction.followup.send(
                    embed=create_info_embed("Action items", "No matching action items."),
                    ephemeral=True,
                )
                return

            view = ActionListView(bot=self.bot, rows=parsed)
            await interaction.followup.send(embed=view._build_embed(), view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"action_list failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed("List failed", "Could not load action items."),
                ephemeral=True,
            )

    @action.command(name="done", description="Mark an action item as complete by ID or description")
    @app_commands.describe(
        id="The action item ID number to mark as done",
        what="Or describe what you finished — we'll find the best match",
    )
    async def action_done(
        self,
        interaction: discord.Interaction,
        id: Optional[int] = None,
        what: Optional[str] = None,
    ):
        """Mark an action item as done by ID, or describe what you finished and we'll fuzzy-match."""
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return

        if not getattr(self.bot, "db", None):
            await interaction.response.send_message(
                embed=create_error_embed("Database unavailable", "Action items require SQLite DB."),
                ephemeral=True,
            )
            return

        if not id and not what:
            await interaction.response.send_message(
                embed=create_error_embed("Missing input", "Provide an `id` or describe `what` you finished."),
                ephemeral=True,
            )
            return

        # --- Fuzzy match path ---
        if not id and what:
            await interaction.response.defer(ephemeral=True)
            try:
                async with self.bot.db.acquire() as conn, conn.execute(
                    """SELECT id, title, assigned_to_username, due_date
                           FROM tasks
                           WHERE tags LIKE ? AND status IN ('todo','in_progress','blocked')
                           ORDER BY updated_at DESC LIMIT 50""",
                    (f"%{ACTION_TAG}%",),
                ) as cursor:
                    candidates = await cursor.fetchall()

                if not candidates:
                    await interaction.followup.send(
                        embed=create_info_embed("No open actions", "There are no open action items to match against."),
                        ephemeral=True,
                    )
                    return

                # Score each candidate using simple word overlap
                what_words = set(what.lower().split())
                scored = []
                for row in candidates:
                    title_words = set((row[1] or "").lower().split())
                    overlap = len(what_words & title_words)
                    # Also boost for substring containment
                    if what.lower() in (row[1] or "").lower():
                        overlap += 5
                    scored.append((overlap, row))

                scored.sort(key=lambda x: x[0], reverse=True)
                top = scored[:5]

                # If there's a strong unique match, use it directly
                if top[0][0] > 0 and (len(top) == 1 or top[0][0] > top[1][0] + 1):
                    id = top[0][1][0]  # fall through to the normal ID path below
                else:
                    # Show disambiguation dropdown
                    options = []
                    for score, row in top:
                        if score == 0:
                            break
                        tid, ttitle, towner, tdue = row[0], row[1], row[2], row[3]
                        due_s = f" due {tdue}" if tdue else ""
                        own_s = f" @{towner}" if towner else ""
                        options.append(
                            discord.SelectOption(
                                label=f"#{tid} {ttitle}"[:100],
                                description=f"{due_s}{own_s}"[:100].strip(),
                                value=str(tid),
                            )
                        )
                    if not options:
                        await interaction.followup.send(
                            embed=create_info_embed("No match", f"Couldn't find an open action matching \"{what[:80]}\". Try `/action list` to browse."),
                            ephemeral=True,
                        )
                        return

                    select = discord.ui.Select(
                        placeholder="Which one did you finish?",
                        options=options,
                    )

                    async def _done_select_callback(sel_interaction: discord.Interaction):
                        chosen_id = int(select.values[0])
                        await sel_interaction.response.defer(ephemeral=True)
                        ok = await _update_action_status(
                            bot=self.bot,
                            action_id=chosen_id,
                            new_status="done",
                            actor_user_id=sel_interaction.user.id,
                            actor_username=sel_interaction.user.display_name,
                        )
                        recur_msg = ""
                        if ok:
                            try:
                                next_id = await _spawn_next_recurrence(self.bot, chosen_id)
                                if next_id:
                                    recur_msg = f"\n🔁 Next occurrence created: **#{next_id}**"
                            except Exception as e:
                                logger.warning("_done_select_callback: suppressed %s", e)
                        chosen_title = next((r[1] for s, r in top if r[0] == chosen_id), f"#{chosen_id}")
                        if ok:
                            await sel_interaction.followup.send(
                                embed=create_success_embed("Done!", f"✅ Marked **#{chosen_id}** as complete: {chosen_title}{recur_msg}"),
                                ephemeral=False,
                            )
                        else:
                            await sel_interaction.followup.send(
                                embed=create_error_embed("Failed", f"Could not mark **#{chosen_id}** as done."),
                                ephemeral=True,
                            )

                    select.callback = _done_select_callback
                    view = discord.ui.View(timeout=120)
                    view.add_item(select)
                    await interaction.followup.send(
                        embed=create_info_embed("Which one?", f"Multiple actions match \"{what[:80]}\":"),
                        view=view,
                        ephemeral=True,
                    )
                    return

            except Exception as e:
                logger.error(f"Fuzzy action_done failed: {e}")
                await interaction.followup.send(
                    embed=create_error_embed("Error", f"Smart matching failed: {e}"),
                    ephemeral=True,
                )
                return

        # --- Standard ID path ---
        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                "SELECT id, title, status, assigned_to_user_id FROM tasks WHERE id = ? AND tags LIKE ?",
                (int(id), f"%{ACTION_TAG}%"),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await interaction.response.send_message(
                    embed=create_error_embed("Not found", f"Action item **#{id}** does not exist."),
                    ephemeral=True,
                )
                return

            task_id, title, current_status, assigned_to = row[0], row[1], row[2], row[3]

            if current_status == "done":
                await interaction.response.send_message(
                    embed=create_info_embed("Already done", f"Action item **#{id}** is already marked as done."),
                    ephemeral=True,
                )
                return

            # Mark as done
            ok = await _update_action_status(
                bot=self.bot,
                action_id=task_id,
                new_status="done",
                actor_user_id=interaction.user.id,
                actor_username=interaction.user.display_name,
            )

            if ok:
                # Check for recurrence and auto-create next occurrence
                recur_msg = ""
                try:
                    next_id = await _spawn_next_recurrence(self.bot, task_id)
                    if next_id:
                        recur_msg = f"\n🔁 Next occurrence created: **#{next_id}**"
                except Exception as exc:
                    logger.warning("Recurrence spawn failed for #%s: %s", task_id, exc)

                await interaction.response.send_message(
                    embed=create_success_embed("Done!", f"✅ Marked **#{id}** as complete: {title}{recur_msg}"),
                    ephemeral=False,
                )
            else:
                await interaction.response.send_message(
                    embed=create_error_embed("Failed", f"Could not mark **#{id}** as done."),
                    ephemeral=True,
                )

        except Exception as e:
            logger.error(f"action_done failed: {e}")
            await interaction.response.send_message(
                embed=create_error_embed("Error", f"Could not complete action item: {e}"),
                ephemeral=True,
            )

    @action.command(name="thin", description="Bulk mark completed action items as done (no typing)")
    @app_commands.describe(
        owner="Filter by owner",
        mine="Only items assigned to you",
        limit="Max items (1-25)",
    )
    async def action_thin(
        self,
        interaction: discord.Interaction,
        owner: Optional[discord.User] = None,
        mine: bool = True,
        limit: int = 25,
    ):
        """Interactive bulk completion UI to quickly remove finished items from meeting rollups."""
        if not is_partner(interaction):
            await interaction.response.send_message("This command is for partners only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if not getattr(self.bot, "db", None):
            await interaction.followup.send(
                embed=create_error_embed("Database unavailable", "Action items require SQLite DB."),
                ephemeral=True,
            )
            return

        limit = max(1, min(int(limit or 25), 25))
        status_filter = ("todo", "in_progress", "blocked")

        target_owner_id: Optional[int] = None
        if mine:
            target_owner_id = int(interaction.user.id)
        elif owner:
            target_owner_id = int(owner.id)

        try:
            async with self.bot.db.acquire() as conn:
                base = (
                    "SELECT id, title, description, status, priority, due_date, "
                    "assigned_to_user_id, assigned_to_username, created_by_user_id, created_by_username, created_at, updated_at, tags "
                    "FROM tasks "
                    "WHERE tags LIKE ? "
                    "AND status IN ({}) ".format(",".join(["?"] * len(status_filter)))
                )
                args: List[Any] = [f"%{ACTION_TAG}%", *status_filter]
                if target_owner_id is not None:
                    base += "AND assigned_to_user_id = ? "
                    args.append(int(target_owner_id))
                base += "ORDER BY (due_date IS NULL) ASC, due_date ASC, updated_at DESC LIMIT ?"
                args.append(int(limit))
                async with conn.execute(base, tuple(args)) as cursor:
                    rows = await cursor.fetchall()

            parsed: List[ActionItemRow] = []
            for r in rows or []:
                tags = _safe_tags(r[12])
                if ACTION_TAG not in tags:
                    continue
                parsed.append(
                    ActionItemRow(
                        id=int(r[0]),
                        title=str(r[1] or ""),
                        description=str(r[2] or ""),
                        status=str(r[3] or ""),
                        priority=str(r[4] or ""),
                        due_date=(str(r[5]) if r[5] else None),
                        assigned_to_user_id=(int(r[6]) if r[6] is not None else None),
                        assigned_to_username=(str(r[7]) if r[7] else None),
                        created_by_user_id=(int(r[8]) if r[8] is not None else None),
                        created_by_username=(str(r[9]) if r[9] else None),
                        created_at=(str(r[10]) if r[10] else None),
                        updated_at=(str(r[11]) if r[11] else None),
                    )
                )

            if not parsed:
                scope = "your" if mine else "matching"
                await interaction.followup.send(
                    embed=create_info_embed("Action items", f"No open action items found for {scope} list."),
                    ephemeral=True,
                )
                return

            view = ActionBulkCompleteView(
                bot=self.bot,
                rows=parsed,
                owner_user_id=target_owner_id,
                status_filter=status_filter,
                limit=limit,
            )
            await interaction.followup.send(embed=view._build_embed(), view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"action_thin failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed("Thin failed", "Could not load action items."),
                ephemeral=True,
            )

    @tasks.loop(time=dt_time(hour=9, minute=15, tzinfo=EASTERN))
    async def weekly_action_checkin(self):
        """Weekly owner check-in (Mondays)."""
        await self.bot.wait_until_ready()

        now_local = datetime.now(EASTERN)
        if now_local.weekday() != 0:  # Monday
            return

        if not getattr(self.bot, "db", None):
            return

        run_date = now_local.date().isoformat()
        try:
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("action_items_weekly_checkin", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"Weekly check-in idempotency failed; continuing: {e}")

        try:
            channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            if not channel:
                return

            async with self.bot.db.acquire() as conn, conn.execute(
                """
                    SELECT id, title, status, due_date, priority, assigned_to_user_id
                    FROM tasks
                    WHERE tags LIKE ?
                      AND status IN ('todo','in_progress','blocked')
                    ORDER BY (due_date IS NULL) ASC, due_date ASC, priority DESC, updated_at DESC
                    LIMIT 60
                    """,
                (f"%{ACTION_TAG}%",),
            ) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                try:
                    if hasattr(self.bot.db, "complete_job_run"):
                        await self.bot.db.complete_job_run("action_items_weekly_checkin", run_date)
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
                return

            # Group by owner
            by_owner: Dict[Optional[int], List[Tuple]] = {}
            for r in rows:
                owner_id = int(r[5]) if r[5] is not None else None
                by_owner.setdefault(owner_id, []).append(r)

            lines: List[str] = []
            lines.append("📌 **Action items check-in (this week)**")
            lines.append("Reply in-thread with updates; you can also use `/action list`.")
            lines.append("")

            owners_sorted = [oid for oid in by_owner if oid is not None]
            owners_sorted.sort()
            if None in by_owner:
                owners_sorted.append(None)

            for oid in owners_sorted:
                items = by_owner.get(oid) or []
                header = "**Unassigned**" if oid is None else f"<@{oid}>"
                lines.append(header)
                for item in items[:6]:
                    _id, title, status, due_date, priority, owner_id = item
                    due_txt = f" (due {due_date})" if due_date else ""
                    lines.append(f"- **#{_id}** [{status}] {title}{due_txt}")
                lines.append("")

            allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
            await channel.send("\n".join(lines)[:1900], allowed_mentions=allowed_mentions)

            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_weekly_checkin", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        except Exception as e:
            logger.error(f"weekly_action_checkin failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_weekly_checkin", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    @tasks.loop(time=dt_time(hour=8, minute=45, tzinfo=EASTERN))
    async def daily_top3(self):
        """Daily Top-3 per owner: DM each owner their next actions (weekday mornings)."""
        await self.bot.wait_until_ready()
        if not getattr(self.bot, "db", None):
            return

        now_local = datetime.now(EASTERN)
        if DAILY_TOP3_WEEKDAYS_ONLY and now_local.weekday() >= 5:
            return

        run_date = now_local.date().isoformat()
        try:
            if hasattr(self.bot.db, "record_job_run"):
                recorded = await self.bot.db.record_job_run("action_items_daily_top3", run_date)
                if not recorded:
                    return
        except Exception as e:
            logger.warning(f"daily_top3 idempotency failed; continuing: {e}")

        partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        try:
            async with self.bot.db.acquire() as conn:
                if partners_channel:
                    await self._auto_assign_unowned_action_items(conn=conn, partners_channel=partners_channel)

                async with conn.execute(
                    """
                    SELECT id, title, status, due_date, priority, assigned_to_user_id, assigned_to_username, updated_at, created_at
                    FROM tasks
                    WHERE tags LIKE ?
                      AND status IN ('todo','in_progress','blocked')
                      AND assigned_to_user_id IS NOT NULL
                    ORDER BY (due_date IS NULL) ASC, due_date ASC
                    LIMIT ?
                    """,
                    (f"%{ACTION_TAG}%", int(DAILY_TOP3_LOOKBACK_LIMIT)),
                ) as cursor:
                    rows = await cursor.fetchall()

            by_owner: Dict[int, List[Dict[str, Any]]] = {}
            for r in rows or []:
                try:
                    action_id, title, status, due_date, priority, owner_id, owner_name, updated_at, created_at = r
                    if owner_id is None:
                        continue
                    owner_id_int = int(owner_id)
                    by_owner.setdefault(owner_id_int, []).append(
                        {
                            "id": int(action_id),
                            "title": str(title),
                            "status": str(status),
                            "due_date": str(due_date) if due_date else None,
                            "priority": str(priority) if priority else None,
                            "assigned_to_username": str(owner_name) if owner_name else None,
                            "updated_at": str(updated_at) if updated_at else None,
                            "created_at": str(created_at) if created_at else None,
                        }
                    )
                except Exception:
                    continue

            def _prio_rank(p: Optional[str]) -> int:
                v = (p or "").lower().strip()
                if v == "high":
                    return 3
                if v == "medium":
                    return 2
                if v == "low":
                    return 1
                return 0

            def _stale_key(item: Dict[str, Any]) -> str:
                return str(item.get("updated_at") or item.get("created_at") or "")

            for owner_id, items in by_owner.items():
                items.sort(
                    key=lambda it: (
                        0 if it.get("due_date") else 1,
                        str(it.get("due_date") or "9999-12-31"),
                        -_prio_rank(it.get("priority")),
                        _stale_key(it),
                        int(it.get("id") or 0),
                    )
                )
                top = items[:DAILY_TOP3_MAX_ITEMS]
                if not top:
                    continue

                user = self.bot.get_user(owner_id)
                if not user:
                    try:
                        user = await self.bot.fetch_user(owner_id)
                    except Exception:
                        user = None

                lines: List[str] = []
                lines.append("✅ **Your Top Actions (today)**")
                wip = await _get_in_progress_count(bot=self.bot, owner_user_id=int(owner_id))
                if wip >= WIP_LIMIT_IN_PROGRESS:
                    lines.append(f"⛔ WIP is **{wip}/{WIP_LIMIT_IN_PROGRESS}** in progress — finish/close something before starting more.")
                lines.append("")
                for r in top:
                    due_txt = f" (due {r['due_date']})" if r.get("due_date") else ""
                    lines.append(f"- **#{r['id']}** [{r['status']}] {r['title']}{due_txt}")
                lines.append("")
                lines.append("Use the buttons to mark done, snooze (requires reason), or set a timeframe when due date is missing.")

                view = DailyTopActionsView(bot=self.bot, rows=top, owner_user_id=int(owner_id))
                if user:
                    try:
                        await user.send("\n".join(lines)[:1900], view=view)
                        continue
                    except Exception as e:
                        logger.warning("operation: suppressed %s", e)

                # Fallback: post in partners channel if DM fails
                if partners_channel:
                    allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
                    await partners_channel.send(
                        f"<@{owner_id}>\n" + "\n".join(lines)[:1850],
                        allowed_mentions=allowed_mentions,
                    )

            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_daily_top3", run_date)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        except Exception as e:
            logger.error(f"daily_top3 failed: {e}")
            try:
                if hasattr(self.bot.db, "complete_job_run"):
                    await self.bot.db.complete_job_run("action_items_daily_top3", run_date, error=str(e)[:200])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        return


    async def post_new_action(self, action_id: int):
        if not getattr(self.bot, "db", None):
            return
            
        partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
        if not partners_channel:
            return

        try:
            async with self.bot.db.acquire() as conn, conn.execute(
                "SELECT title, description, priority, due_date, status FROM tasks WHERE id = ?",
                (int(action_id),)
            ) as cursor:
                row = await cursor.fetchone()
                    
            if not row:
                return
                
            title, description, priority, due_date, status = row
            
            embed = discord.Embed(
                title=f"Action Item #{action_id}",
                description=f"**{title}**\n\n{description or ''}".strip(),
                color=discord.Color.blue()
            )
            embed.add_field(name="Priority", value=str(priority).capitalize(), inline=True)
            if due_date:
                embed.add_field(name="Due Date", value=str(due_date), inline=True)
            
            # Show owners
            owners = await _get_task_owners(self.bot, action_id)
            owners_text = "Unassigned"
            if owners:
                owners_text = " ".join([f"<@{uid}>" for uid in owners])
                
            embed.add_field(name="Owners", value=owners_text, inline=False)
            embed.add_field(name="Status", value=str(status), inline=True)
            
            embed.set_footer(text="React with your letter to claim/unclaim ownership.")
            
            msg = await partners_channel.send(embed=embed)
            
            # Add 5 partner emojis
            for emoji in PARTNER_EMOJIS:
                await msg.add_reaction(emoji)
                
        except Exception as e:
            logger.error(f"Failed to post action #{action_id}: {e}")


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        emoji_str = str(payload.emoji)
        if emoji_str not in PARTNER_MAPPING:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return
        
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return

        if message.author.id != self.bot.user.id:
            return

        # Check if it's an Action Item embed
        if not message.embeds:
            return
        embed = message.embeds[0]
        if not embed.title or not embed.title.startswith("Action Item #"):
            return

        # Parse ID
        try:
            # Title format: "Action Item #123" or "Action Item #123: Title" or "#123 Title" (need to be careful with formats)
            # My code in ActionAddModal uses: "Action Item #123" for title, then description below.
            # But I should be robust.
            if "#" in embed.title:
                id_part = embed.title.split("#")[1]
                # Take untill space or end
                id_str = id_part.split()[0].replace(":", "")
                action_id = int(id_str)
            else:
                return
        except Exception:
            return

        partner_id = PARTNER_MAPPING[emoji_str]
        
        # Add owner
        await _add_task_owner(self.bot, action_id, partner_id)
        
        # Update embed
        await self._refresh_action_embed(message, action_id)


    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        emoji_str = str(payload.emoji)
        if emoji_str not in PARTNER_MAPPING:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return
        
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return
            
        if message.author.id != self.bot.user.id:
            return

        if not message.embeds:
            return
        embed = message.embeds[0]
        if not embed.title or not embed.title.startswith("Action Item #"):
            return

        try:
            if "#" in embed.title:
                id_part = embed.title.split("#")[1]
                id_str = id_part.split()[0].replace(":", "")
                action_id = int(id_str)
            else:
                return
        except Exception:
            return

        partner_id = PARTNER_MAPPING[emoji_str]
        
        # Remove owner
        await _remove_task_owner(self.bot, action_id, partner_id)
        
        # Update embed
        await self._refresh_action_embed(message, action_id)


    async def _refresh_action_embed(self, message: discord.Message, action_id: int):
        owners = await _get_task_owners(self.bot, action_id)
        
        # Update the "Owners" field in the embed
        embed = message.embeds[0]
        new_embed = embed.copy() # Copy to modify
        
        owners_text = "Unassigned"
        if owners:
            owners_text = " ".join([f"<@{uid}>" for uid in owners]) 
        
        # Find the Owners field and update it
        field_idx = -1
        for i, field in enumerate(new_embed.fields):
            if field.name == "Owners":
                field_idx = i
                break
        
        if field_idx != -1:
            new_embed.set_field_at(field_idx, name="Owners", value=owners_text, inline=False)
        else:
            new_embed.add_field(name="Owners", value=owners_text, inline=False)
            
        await message.edit(embed=new_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ActionItems(bot))
