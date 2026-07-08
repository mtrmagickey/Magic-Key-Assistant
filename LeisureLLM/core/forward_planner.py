"""
forward_planner — World-model primitives for MKA.

Provides forward simulation over the operational state:

• **compute_next_due** — Given an obligation's frequency and last-completed
  date, generate the next N occurrence dates.
• **detect_collisions** — Find date ranges where multiple obligations and/or
  task due dates stack up beyond a configurable threshold.
• **propagate_slip** — Given a task that slipped, compute the cascade effect
  on all downstream dependents.

These are pure functions over DB rows — no LLM, no network — suitable
for scheduled sweeps and on-demand "what if?" queries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# 1.  Next-due computation
# ════════════════════════════════════════════════════════════════

# Frequency → approximate days.  Used when dateutil is unavailable.
_FREQ_DAYS: Dict[str, int] = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 91,
    "semi_annually": 182,
    "annually": 365,
}


def _add_frequency(base: date, frequency: str) -> date:
    """Advance *base* by one *frequency* interval.

    Uses ``dateutil.relativedelta`` for calendar-accurate months/years
    when available, falls back to integer-day approximation.
    """
    try:
        from dateutil.relativedelta import relativedelta  # type: ignore[import-untyped]

        _FREQ_DELTA = {
            "daily": relativedelta(days=1),
            "weekly": relativedelta(weeks=1),
            "biweekly": relativedelta(weeks=2),
            "monthly": relativedelta(months=1),
            "quarterly": relativedelta(months=3),
            "semi_annually": relativedelta(months=6),
            "annually": relativedelta(years=1),
        }
        delta = _FREQ_DELTA.get(frequency)
        if delta:
            return base + delta
    except ImportError:
        pass

    days = _FREQ_DAYS.get(frequency, 30)
    return base + timedelta(days=days)


def compute_next_due(
    frequency: str,
    last_completed: date | str | None = None,
    current_next_due: date | str | None = None,
    horizon: int = 6,
) -> List[date]:
    """Generate the next *horizon* occurrence dates for an obligation.

    Parameters
    ----------
    frequency : str
        One of daily, weekly, biweekly, monthly, quarterly, semi_annually,
        annually.
    last_completed : date | str | None
        When the obligation was last fulfilled.  If ``None``, falls back to
        *current_next_due*.
    current_next_due : date | str | None
        The currently-stored next-due date.  Used as anchor if
        *last_completed* is missing.
    horizon : int
        How many future occurrences to generate.

    Returns
    -------
    list[date]
        Sorted ascending.  Will be empty if no anchor date can be
        determined.
    """
    def _to_date(v: date | str | None) -> date | None:
        if v is None:
            return None
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v)[:10])
        except (ValueError, TypeError):
            return None

    anchor = _to_date(last_completed) or _to_date(current_next_due)
    if anchor is None:
        return []

    # If we have last_completed, first occurrence is one interval after.
    # If we only have next_due, start from there.
    if _to_date(last_completed):
        cursor = _add_frequency(anchor, frequency)
    else:
        cursor = anchor

    dates: List[date] = []
    for _ in range(horizon):
        dates.append(cursor)
        cursor = _add_frequency(cursor, frequency)
    return dates


# ════════════════════════════════════════════════════════════════
# 2.  Collision detection
# ════════════════════════════════════════════════════════════════

@dataclass
class Collision:
    """A date range where work items stack up."""
    window_start: date
    window_end: date
    items: List[Dict[str, Any]]  # {type, id, title, due_date}
    count: int = 0

    def __post_init__(self):
        self.count = len(self.items)


async def detect_collisions(
    db,
    *,
    lookahead_days: int = 14,
    threshold: int = 3,
    obligation_horizon: int = 4,
) -> List[Collision]:
    """Find upcoming date windows where ≥ *threshold* items are due.

    Merges task due dates with projected obligation occurrences into
    a unified timeline, then buckets by day and returns clusters.
    """
    today = date.today()
    cutoff = today + timedelta(days=lookahead_days)

    # ── Gather task due dates ────────────────────────────────
    items_by_date: Dict[date, List[Dict[str, Any]]] = {}

    try:
        async with db.connection.execute(
            """SELECT id, title, due_date, status, owner_username
               FROM tasks
               WHERE due_date IS NOT NULL
                 AND due_date <= ?
                 AND due_date >= ?
                 AND status NOT IN ('done', 'cancelled')""",
            (cutoff.isoformat(), today.isoformat()),
        ) as cur:
            for row in await cur.fetchall():
                d = date.fromisoformat(str(row["due_date"])[:10])
                entry = {
                    "type": "task",
                    "id": row["id"],
                    "title": row["title"],
                    "due_date": d.isoformat(),
                    "owner": row["owner_username"],
                }
                items_by_date.setdefault(d, []).append(entry)
    except Exception:
        logger.debug("Could not query tasks for collision detection", exc_info=True)

    # ── Project obligation occurrences ───────────────────────
    try:
        async with db.connection.execute(
            """SELECT id, title, frequency, last_completed, next_due,
                      owner_username
               FROM obligations
               WHERE status IN ('active', 'upcoming')"""
        ) as cur:
            for row in await cur.fetchall():
                future_dates = compute_next_due(
                    frequency=row["frequency"],
                    last_completed=row["last_completed"],
                    current_next_due=row["next_due"],
                    horizon=obligation_horizon,
                )
                for d in future_dates:
                    if today <= d <= cutoff:
                        entry = {
                            "type": "obligation",
                            "id": row["id"],
                            "title": row["title"],
                            "due_date": d.isoformat(),
                            "owner": row["owner_username"],
                        }
                        items_by_date.setdefault(d, []).append(entry)
    except Exception:
        logger.debug("Could not query obligations for collision detection", exc_info=True)

    # ── Build collision windows ──────────────────────────────
    collisions: List[Collision] = []
    for d in sorted(items_by_date):
        bucket = items_by_date[d]
        if len(bucket) >= threshold:
            collisions.append(Collision(
                window_start=d,
                window_end=d,
                items=bucket,
            ))

    return collisions


# ════════════════════════════════════════════════════════════════
# 3.  Dependency slip propagation
# ════════════════════════════════════════════════════════════════

@dataclass
class SlipEffect:
    """The cascade effect of a single task slipping."""
    source_task_id: int
    slip_days: int
    affected: List[Dict[str, Any]] = field(default_factory=list)
    # [{id, title, original_due, new_due, depth}]


async def propagate_slip(
    db,
    task_id: int,
    slip_days: int,
) -> SlipEffect:
    """Compute the cascade if *task_id* slips by *slip_days*.

    Walks the dependency graph breadth-first.  Every task that lists
    *task_id* (directly or transitively) in its ``dependencies`` JSON
    column has its due date pushed by *slip_days*.

    This is a **read-only simulation** — nothing is written.
    """
    effect = SlipEffect(source_task_id=task_id, slip_days=slip_days)

    # Build adjacency: task_id → list of dependents
    dependents: Dict[int, List[Dict[str, Any]]] = {}
    try:
        async with db.connection.execute(
            """SELECT id, title, due_date, dependencies
               FROM tasks
               WHERE dependencies IS NOT NULL
                 AND status NOT IN ('done', 'cancelled')"""
        ) as cur:
            for row in await cur.fetchall():
                deps = json.loads(row["dependencies"]) if row["dependencies"] else []
                for dep_id in deps:
                    dependents.setdefault(dep_id, []).append(dict(row))
    except Exception:
        logger.debug("Could not query dependencies for slip propagation", exc_info=True)
        return effect

    # BFS
    queue = [(task_id, 0)]  # (id, depth)
    visited = {task_id}

    while queue:
        current_id, depth = queue.pop(0)
        for task in dependents.get(current_id, []):
            tid = task["id"]
            if tid in visited:
                continue
            visited.add(tid)

            original_due = task.get("due_date")
            new_due = None
            if original_due:
                try:
                    d = date.fromisoformat(str(original_due)[:10])
                    new_due = (d + timedelta(days=slip_days)).isoformat()
                except (ValueError, TypeError) as e:
                    logger.warning("operation: suppressed %s", e)

            effect.affected.append({
                "id": tid,
                "title": task["title"],
                "original_due": original_due,
                "new_due": new_due,
                "depth": depth + 1,
            })
            queue.append((tid, depth + 1))

    return effect


# ════════════════════════════════════════════════════════════════
# 4.  Convenience: sweep-style summary
# ════════════════════════════════════════════════════════════════

async def forward_planning_sweep(
    db,
    *,
    lookahead_days: int = 14,
    collision_threshold: int = 3,
) -> Dict[str, Any]:
    """Run collision detection and return a digest-friendly summary."""
    collisions = await detect_collisions(
        db,
        lookahead_days=lookahead_days,
        threshold=collision_threshold,
    )
    if collisions:
        summary = (
            f"⚠️  {len(collisions)} date(s) in the next {lookahead_days} days "
            f"have ≥{collision_threshold} items due"
        )
    else:
        summary = f"✅ No date collisions in the next {lookahead_days} days"

    return {
        "collisions": [
            {
                "date": c.window_start.isoformat(),
                "count": c.count,
                "items": c.items,
            }
            for c in collisions
        ],
        "summary": summary,
    }
