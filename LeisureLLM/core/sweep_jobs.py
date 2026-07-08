"""
sweep_jobs — Scheduled background routines for continuity monitoring.

Obligation Sweep:
    Checks all active obligations for upcoming due dates and overdue items.
    Produces a summary for the daily digest or fires alerts.

SOP Drift Detector:
    Finds SOPs that haven't been exercised or reviewed within their
    expected interval. Flags them as "drifting" and suggests a review.

These are designed to be called from discord.ext.tasks loops
or from the admin API for manual triggering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """Results from a sweep run."""
    job_name: str
    items_checked: int = 0
    items_flagged: int = 0
    details: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""


# ── Obligation Sweep ──────────────────────────────────────────

async def obligation_sweep(
    db,
    *,
    upcoming_days: int = 14,
    auto_mark_overdue: bool = True,
) -> SweepResult:
    """
    Scan obligations and flag upcoming/overdue items.

    1. Any obligation with next_due < now and status active → mark overdue
    2. Obligations due within `upcoming_days` → flag as upcoming
    3. Returns summary for digest output
    """
    from core.services.obligation_service import ObligationService

    svc = ObligationService(db)
    result = SweepResult(job_name="obligation_sweep")

    # Get all active obligations
    all_obligations = await svc.list_all(status="active")
    result.items_checked = len(all_obligations)

    overdue_items = await svc.get_overdue()
    upcoming_items = await svc.get_upcoming(days=upcoming_days)

    # Auto-mark overdue
    if auto_mark_overdue:
        for item in overdue_items:
            if item["status"] != "overdue":
                await svc.mark_overdue(item["id"])
                logger.info(f"Obligation #{item['id']} '{item['title']}' marked overdue")

    # Build details
    for item in overdue_items:
        result.items_flagged += 1
        result.details.append({
            "id": item["id"],
            "title": item["title"],
            "next_due": item["next_due"],
            "severity": "overdue",
            "owner": item.get("owner_username"),
        })

    for item in upcoming_items:
        # Skip if already in overdue list
        if any(d["id"] == item["id"] for d in result.details):
            continue
        result.items_flagged += 1
        result.details.append({
            "id": item["id"],
            "title": item["title"],
            "next_due": item["next_due"],
            "severity": "upcoming",
            "owner": item.get("owner_username"),
        })

    # Summary
    n_overdue = sum(1 for d in result.details if d["severity"] == "overdue")
    n_upcoming = sum(1 for d in result.details if d["severity"] == "upcoming")
    parts = []
    if n_overdue:
        parts.append(f"🔴 {n_overdue} overdue")
    if n_upcoming:
        parts.append(f"🟡 {n_upcoming} upcoming (≤{upcoming_days}d)")
    if not parts:
        parts.append("✅ All obligations on track")
    result.summary = f"Obligation sweep: {', '.join(parts)} ({result.items_checked} checked)"

    logger.info(result.summary)
    return result


# ── SOP Drift Detector ────────────────────────────────────────

async def sop_drift_check(
    db,
    *,
    stale_days: int = 90,
) -> SweepResult:
    """
    Find SOPs that are drifting — not exercised or reviewed in `stale_days`.

    Returns list of stale SOPs with last-activity dates.
    """
    from core.services.sop_service import SOPService

    svc = SOPService(db)
    result = SweepResult(job_name="sop_drift_check")

    all_sops = await svc.list_all(status="active")
    result.items_checked = len(all_sops)

    stale_sops = await svc.get_stale(days=stale_days)

    for sop in stale_sops:
        result.items_flagged += 1
        last_activity = sop.get("last_exercised") or sop.get("last_reviewed") or sop.get("created_at")
        result.details.append({
            "id": sop["id"],
            "title": sop["title"],
            "last_exercised": sop.get("last_exercised"),
            "last_reviewed": sop.get("last_reviewed"),
            "last_activity": last_activity,
            "owner": sop.get("owner_username"),
        })

    # Summary
    if result.items_flagged:
        result.summary = (
            f"SOP drift: ⚠️ {result.items_flagged}/{result.items_checked} SOPs "
            f"not exercised/reviewed in {stale_days}+ days"
        )
    else:
        result.summary = f"SOP drift: ✅ All {result.items_checked} SOPs exercised within {stale_days}d"

    logger.info(result.summary)
    return result


# ── Rail Escalation Check ─────────────────────────────────────

async def rail_escalation_check(db) -> SweepResult:
    """
    Find rail stages that have been in_progress longer than their
    escalation_days window. These need attention.
    """
    from core.services.rails_service import RailsService

    svc = RailsService(db)
    result = SweepResult(job_name="rail_escalation_check")

    candidates = await svc.get_escalation_candidates()
    result.items_flagged = len(candidates)

    for stage in candidates:
        result.details.append({
            "stage_id": stage["id"],
            "stage_name": stage["name"],
            "rail_name": stage.get("rail_name"),
            "rail_type": stage.get("rail_type"),
            "entered_at": stage.get("entered_at"),
            "escalation_days": stage.get("escalation_days"),
        })

    if result.items_flagged:
        result.summary = (
            f"Rail escalation: ⚠️ {result.items_flagged} stage(s) overdue "
            f"for advancement"
        )
    else:
        result.summary = "Rail escalation: ✅ No overdue stages"

    logger.info(result.summary)
    return result


# ── Combined sweep (for daily digest integration) ─────────────

async def run_all_sweeps(db, *, upcoming_days: int = 14, stale_days: int = 90) -> Dict[str, SweepResult]:
    """Run all sweep jobs and return results keyed by job name."""
    results = {}
    for coro in [
        obligation_sweep(db, upcoming_days=upcoming_days),
        sop_drift_check(db, stale_days=stale_days),
        rail_escalation_check(db),
    ]:
        try:
            r = await coro
            results[r.job_name] = r
        except Exception as e:
            logger.error(f"Sweep job failed: {e}")

    # ── Forward planner: collision detection ──────────────────
    try:
        from core.forward_planner import forward_planning_sweep
        fp = await forward_planning_sweep(db, lookahead_days=upcoming_days)
        results["forward_planner"] = SweepResult(
            job_name="forward_planner",
            items_checked=sum(c["count"] for c in fp.get("collisions", [])),
            items_flagged=len(fp.get("collisions", [])),
            summary=fp.get("summary", ""),
        )
    except Exception as e:
        logger.debug(f"Forward planner sweep failed (non-fatal): {e}")

    # ── Symbolic invariant checker ────────────────────────────
    try:
        from core.symbolic_rules import check_invariants
        violations = await check_invariants(db)
        if violations:
            results["invariant_check"] = SweepResult(
                job_name="invariant_check",
                items_checked=len(violations),
                items_flagged=sum(1 for v in violations if v.severity == "error"),
                summary=f"⚠️ {len(violations)} invariant violation(s) detected",
                details=[
                    {"rule": v.rule, "entity": f"{v.entity_type}#{v.entity_id}",
                     "detail": v.detail, "severity": v.severity}
                    for v in violations
                ],
            )
    except Exception as e:
        logger.debug(f"Invariant check failed (non-fatal): {e}")

    return results
