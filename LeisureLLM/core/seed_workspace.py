"""
seed_workspace — Populate a fresh install with continuity-first sample data.

Creates a small operational continuity demo so a new user immediately sees
overdue work, decision history, and recurring obligations instead of an empty UI.

Idempotent: checks both a _seed_complete flag and existing demo records.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_SEED_FLAG = ".seed_complete"
_DEMO_ACTION_TITLES = (
    "Confirm the owner for Saturday opening checks",
    "Post next week's swim class update",
    "Write the handoff rule for urgent facility issues",
)
_DEMO_TASK_TAGS = ["starter_content", "demo_seed"]


def _flag_path(base_dir: Path | None = None) -> Path:
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    return base_dir / _SEED_FLAG


def is_seeded(base_dir: Path | None = None) -> bool:
    return _flag_path(base_dir).exists()


async def _existing_values(db, table: str, column: str) -> set[str]:
    try:
        async with db.acquire() as conn, conn.execute(
            f"SELECT {column} FROM {table}"  # noqa: S608
        ) as cur:
            rows = await cur.fetchall()
    except Exception:
        logger.warning("Failed to read existing %s.%s for dedup", table, column, exc_info=True)
        return set()

    values: set[str] = set()
    for row in rows or []:
        raw = None
        if isinstance(row, dict):
            raw = row.get(column)
        elif isinstance(row, (list, tuple)):
            raw = row[0] if row else None
        else:
            raw = getattr(row, column, None)
            if raw is None:
                try:
                    raw = row[0]
                except Exception:
                    raw = None
        if raw:
            values.add(str(raw))
    return values


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


async def has_seed_data(db) -> bool:
    if db is None or not getattr(db, "connection", None):
        return False

    normalized_demo_titles = {_normalize_text(title) for title in _DEMO_ACTION_TITLES}
    try:
        async with db.connection.execute("SELECT title, tags FROM tasks") as cur:
            rows = await cur.fetchall()
    except Exception:
        logger.warning("Failed to inspect tasks for existing demo seed data", exc_info=True)
        return False

    for row in rows or []:
        title = _normalize_text(row[0] if isinstance(row, (list, tuple)) else row["title"])
        if title in normalized_demo_titles:
            return True

        raw_tags = row[1] if isinstance(row, (list, tuple)) else row["tags"]
        if not raw_tags:
            continue
        try:
            tags = json.loads(raw_tags)
        except Exception:
            continue
        if isinstance(tags, list) and "demo_seed" in tags:
            return True

    return False


async def seed_workspace(db, *, base_dir: Path | None = None, force: bool = False) -> Dict[str, Any]:
    """
    Populate the database with sample records for every entity type.

    Returns a dict of { entity_type: [created_ids] }.
    """
    flag = _flag_path(base_dir)
    if flag.exists() and not force:
        logger.info("Workspace already seeded — skipping")
        return {"skipped": True}
    if not force and await has_seed_data(db):
        logger.info("Workspace already contains starter demo data — skipping")
        flag.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        return {"skipped": True, "reason": "seed_data_present"}

    from core.services import (
        ActionService,
        DecisionService,
        FeedbackService,
        LeadService,
        ObligationService,
        RailsService,
        SOPService,
    )

    created: Dict[str, list] = {}
    today = datetime.utcnow().date()

    # ── Action Items ──────────────────────────────────────────
    action_svc = ActionService(db)
    ids = []
    existing_action_titles = {
        _normalize_text(title) for title in await _existing_values(db, "tasks", "title")
    }
    for title, due, priority in [
        (_DEMO_ACTION_TITLES[0], str(today - timedelta(days=1)), "high"),
        (_DEMO_ACTION_TITLES[1], str(today + timedelta(days=2)), "medium"),
        (_DEMO_ACTION_TITLES[2], str(today + timedelta(days=6)), "medium"),
    ]:
        if _normalize_text(title) in existing_action_titles:
            continue
        ids.append(
            await action_svc.create(
                title,
                due_date=due,
                priority=priority,
                tags=list(_DEMO_TASK_TAGS),
                notes="Starter demo workspace task",
            )
        )
    created["actions"] = ids

    # ── Decisions ─────────────────────────────────────────────
    decision_svc = DecisionService(db)
    ids = []
    existing_decision_titles = await _existing_values(db, "decisions", "title")
    for title, decision, rationale in [
        ("Escalate urgent building issues on the same day", "Any facility issue that blocks opening or affects safety must be reviewed the same day and escalated before close",
         "This keeps obvious operational risks visible before they turn into next-day surprises"),
        ("Run a Friday follow-through review", "Review overdue, unowned, and blocked work every Friday before the weekend handoff",
         "A fixed review point helps a small team close the week without losing open commitments"),
    ]:
        if title in existing_decision_titles:
            continue
        ids.append(await decision_svc.create(title, decision, rationale=rationale))
    created["decisions"] = ids

    # ── Leads ─────────────────────────────────────────────────
    lead_svc = LeadService(db)
    ids = []
    existing_lead_names = await _existing_values(db, "leads", "name")
    for name, source in [
        ("Birthday party booking request", "manual"),
        ("Local school pool booking request", "manual"),
    ]:
        if name in existing_lead_names:
            continue
        ids.append(await lead_svc.create(name, source=source))
    created["leads"] = ids

    # ── Obligations ───────────────────────────────────────────
    obl_svc = ObligationService(db)
    ids = []
    existing_obligation_titles = await _existing_values(db, "obligations", "title")
    for title, freq, category, due_offset in [
        ("Friday follow-through review", "weekly", "operations", 7),
        ("Timesheet approval", "biweekly", "financial", 14),
        ("Fire panel test", "monthly", "compliance", 28),
        ("Quarterly membership reconciliation", "quarterly", "financial", 45),
    ]:
        if title in existing_obligation_titles:
            continue
        ids.append(await obl_svc.create(
            title,
            frequency=freq,
            category=category,
            next_due=str(today + timedelta(days=due_offset)),
        ))
    created["obligations"] = ids

    # ── SOPs ──────────────────────────────────────────────────
    sop_svc = SOPService(db)
    ids = []
    existing_sop_titles = await _existing_values(db, "sops", "title")
    for title, body, category in [
        (
            "Friday Follow-Through Review",
            "## Steps\n1. Scan overdue and unowned work\n2. Check decisions still waiting for a call\n"
            "3. Flag blockers that would affect next week\n4. Confirm every open item has a real next step\n"
            "5. Record why anything was deferred or escalated",
            "operations",
        ),
        (
            "Shift Handoff Checklist",
            "## Steps\n1. Record what is still open\n2. Name the current owner or explicitly mark it unowned\n"
            "3. Link the note, message, or evidence behind the issue\n4. State the next review time\n5. Note any risk for the next shift",
            "operations",
        ),
    ]:
        if title in existing_sop_titles:
            continue
        ids.append(await sop_svc.create(title, body=body, category=category))
    created["sops"] = ids

    # ── Rails ─────────────────────────────────────────────────
    rails_svc = RailsService(db)
    ids = []
    existing_rail_names = await _existing_values(db, "rails", "name")
    if "Weekly Operations Reset" not in existing_rail_names:
        rail_id = await rails_svc.create_rail(
            "Weekly Operations Reset",
            "validate",
            description="Example operating rhythm rail that shows how open issues move into owned, reviewed follow-through.",
            use_default_stages=True,
        )
        ids.append(rail_id)
        # Start the first stage
        stages = await rails_svc.list_stages(rail_id)
        if stages:
            await rails_svc.update_stage(stages[0]["id"], status="in_progress")
            await db.execute(
                "UPDATE rail_stages SET entered_at = datetime('now') WHERE id = ?",
                (stages[0]["id"],),
                )
    created["rails"] = ids

    # ── Feedback ──────────────────────────────────────────────
    feedback_svc = FeedbackService(db)
    ids = []
    fb_id = await feedback_svc.create(
        "Example review note: an unowned opening task was caught before the weekend handoff.",
        category="operations",
        severity="low",
        submitted_by="seed_script",
    )
    ids.append(fb_id)
    created["feedback"] = ids

    # ── Write flag ────────────────────────────────────────────
    flag.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
    logger.info(f"Workspace seeded: {json.dumps({k: len(v) for k, v in created.items()})}")

    return created
