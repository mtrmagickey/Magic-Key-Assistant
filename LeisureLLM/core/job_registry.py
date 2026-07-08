"""
Autonomous job registry — single source of truth for scheduled tasks.

Replaces the three ad-hoc lists (cog_load starts, cog_unload cancels,
/admin_run choices) with one declarative registry.  AutonomousOps imports
``JOB_REGISTRY`` + ``is_gate_open`` and iterates instead of maintaining
independent lists.

Gating model:
    Each job carries a ``gate`` string (e.g. "work", "pipeline", "scout")
    that maps to a combination of WorkflowConfig flags via ``is_gate_open``.
    The separate ``requires_accelerators`` flag adds a personas-enabled
    check on top of the gate.

    Full start condition:
        is_gate_open(meta.gate, wf) AND (not meta.requires_accelerators OR personas_enabled)

Usage in AutonomousOps.__init__:
    from core.job_registry import JOB_REGISTRY, is_gate_open
    for name, meta in JOB_REGISTRY.items():
        if meta.cog != "AutonomousOps":
            continue
        if not is_gate_open(meta.gate, _wf):
            continue
        if meta.requires_accelerators and not _personas_on:
            continue
        getattr(self, name).start()

Usage in cog_unload:
    for name in JOB_REGISTRY:
        task = getattr(self, name, None)
        if task and task.is_running():
            task.cancel()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class JobMeta:
    """Metadata for one scheduled task."""

    schedule: str  # human-readable: "daily@08:00ET", "monday@10:00ET", "45m/weekday9-17"
    module: str  # workflow group: "Work", "Memory", "Pipeline", "Health", "Persona meetings"
    gate: str = "work"  # workflow gate key — see GATE_EVALUATORS below
    cog: str = "AutonomousOps"  # which cog owns the task method
    manual_trigger: bool = False  # show in /admin_run
    description: str = ""
    requires_accelerators: bool = False  # only runs when accelerators (personas) are enabled


# ── Full registry ────────────────────────────────────────────────────────────
# Keys MUST match the method name on the cog class exactly.

JOB_REGISTRY: dict[str, JobMeta] = {
    # ── Work module ──────────────────────────────────────────────────────────
    "monday_planning_kickoff": JobMeta(
        schedule="monday@10:00ET",
        module="Work",
        gate="work",
        manual_trigger=True,
        description="Start-of-week planning: goals, priorities, blockers",
    ),
    "friday_closeout": JobMeta(
        schedule="friday@16:00ET",
        module="Work",
        gate="work",
        manual_trigger=True,
        description="End-of-week wrap-up: done, carry-forward, learnings",
    ),
    "thursday_async_meeting": JobMeta(
        schedule="thursday@10:00ET",
        module="Work",
        gate="work",
        description="Async team standup: progress updates and blockers",
        requires_accelerators=True,
    ),
    "end_of_week_reflection": JobMeta(
        schedule="thursday@18:00ET",
        module="Work",
        gate="work",
        description="Individual reflection on the week's work",
        requires_accelerators=True,
    ),
    "weekly_strategic_review": JobMeta(
        schedule="sunday@20:00ET",
        module="Work",
        gate="work",
        manual_trigger=True,
        description="Strategic review: market signals, pivots, priorities",
        requires_accelerators=True,
    ),
    "monthly_partners_meeting": JobMeta(
        schedule="1st-tuesday@10:00ET",
        module="Work",
        gate="work",
        description="Monthly partners meeting agenda and minutes",
        requires_accelerators=True,
    ),
    # ── Memory module (continuity routines — gated on work_enabled) ────────
    "daily_knowledge_refresh": JobMeta(
        schedule="daily@06:00ET",
        module="Memory",
        gate="work",
        description="Re-ingest changed documents into vector store",
    ),
    "question_watchdog": JobMeta(
        schedule="daily@11:00ET",
        module="Memory",
        gate="work",
        description="Scan for unanswered questions → knowledge gaps",
    ),
    "daily_digest": JobMeta(
        schedule="daily@08:00ET",
        module="Memory",
        gate="work",
        manual_trigger=True,
        description="Morning digest of yesterday's key events",
    ),
    "weekly_dashboard_update": JobMeta(
        schedule="daily@09:00ET",
        module="Memory",
        gate="work",
        description="Refresh cached analytics / dashboard metrics",
    ),
    # ── Pipeline / Rainmaker module ──────────────────────────────────────────
    "rainmaker_morning_pipeline": JobMeta(
        schedule="weekdays@08:30ET",
        module="Pipeline",
        gate="pipeline",
        manual_trigger=True,
        description="Morning pipeline review: new leads, stage changes",
        requires_accelerators=True,
    ),
    "rainmaker_opportunity_hunt": JobMeta(
        schedule="weekdays@10:00ET",
        module="Pipeline",
        gate="pipeline",
        description="Automated lead sourcing from configured channels",
        requires_accelerators=True,
    ),
    "rainmaker_follow_up_nudges": JobMeta(
        schedule="weekdays@14:00ET",
        module="Pipeline",
        gate="pipeline",
        description="Follow-up reminders for stale leads",
        requires_accelerators=True,
    ),
    "rainmaker_weekly_cold_review": JobMeta(
        schedule="monday@10:30ET",
        module="Pipeline",
        gate="pipeline",
        description="Review cold leads for re-engagement or archival",
        requires_accelerators=True,
    ),
    "rainmaker_past_client_checkin": JobMeta(
        schedule="wednesday@11:00ET",
        module="Pipeline",
        gate="pipeline_past_clients",
        description="Check-in prompts for past clients (relationship nurture)",
        requires_accelerators=True,
    ),
    # ── Scout module ─────────────────────────────────────────────────────────
    "daily_scout_search": JobMeta(
        schedule="daily@07:00ET",
        module="Memory",
        gate="scout",
        manual_trigger=True,
        description="Broad internet search for new knowledge seeds",
        requires_accelerators=True,
    ),
    "scout_background_crawl": JobMeta(
        schedule="45m/weekday9-17",
        module="Memory",
        gate="scout",
        description="Background crawl of seeded URLs (rate-limited)",
        requires_accelerators=True,
    ),
    # ── Dreamer module ───────────────────────────────────────────────────────
    "dreamer_ideation_cycle": JobMeta(
        schedule="tuesday@14:30ET",
        module="Work",
        gate="pipeline",
        description="Creative brainstorm: new ideas from recent signals",
        requires_accelerators=True,
    ),
    # ── Health / Steward module ──────────────────────────────────────────────
    "steward_daily_health_check": JobMeta(
        schedule="daily@18:00ET",
        module="Health",
        gate="health",
        description="System health: DB size, backup age, error rates",
    ),
    "steward_weekly_self_assessment": JobMeta(
        schedule="sunday@17:00ET",
        module="Health",
        gate="health",
        description="Self-assessment: what's working, what needs attention",
    ),
    "steward_learning_loop_audit": JobMeta(
        schedule="wednesday@09:30ET",
        module="Health",
        gate="health",
        description="Audit the learning loop: gaps closed, new patterns",
    ),
    # ── Corpus Quality / Curator module ──────────────────────────────────────
    "curator_daily_scan_task": JobMeta(
        schedule="daily@07:30ET",
        module="Memory",
        gate="memory",
        manual_trigger=True,
        description="Corpus scan: thin topics, stale docs, fragment detection",
    ),
    "curator_weekly_deep_analysis_task": JobMeta(
        schedule="saturday@08:00ET",
        module="Memory",
        gate="memory",
        manual_trigger=True,
        description="Deep corpus analysis: contradictions, auto-synthesis, coverage gaps",
    ),
    "curator_corpus_interrogation_task": JobMeta(
        schedule="wed+sat@09:00ET",
        module="Memory",
        gate="memory",
        manual_trigger=True,
        description="Self-interrogation: structural gap analysis, web research, human escalation",
    ),
    # ── Persona meetings ─────────────────────────────────────────────────────
    "weekly_persona_meeting_digest": JobMeta(
        schedule="friday@16:30ET",
        module="Persona meetings",
        gate="persona_meetings",
        description="Digest of the week's persona meeting outputs",
        requires_accelerators=True,
    ),
    "hourly_persona_meeting": JobMeta(
        schedule="2h/daily8-20",
        module="Persona meetings",
        gate="persona_meetings",
        description="Rotating persona meeting every 2 hours",
        requires_accelerators=True,
    ),
    # ── Moat Infrastructure ──────────────────────────────────────────────────
    "feedback_learning_cycle": JobMeta(
        schedule="daily@02:00ET",
        module="Health",
        gate="health",
        cog="AdminServer",
        manual_trigger=True,
        description="Run feedback learning loop: retire bad prompts, score chunks, surface signals",
    ),
    "data_retention_enforcement": JobMeta(
        schedule="daily@03:00ET",
        module="Health",
        gate="health",
        cog="AdminServer",
        description="Enforce data retention policies — purge expired data",
    ),
    "cost_savings_snapshot": JobMeta(
        schedule="daily@23:00ET",
        module="Health",
        gate="health",
        cog="AdminServer",
        description="Snapshot daily inference costs, savings, and token usage",
    ),
    "folder_watcher_health": JobMeta(
        schedule="daily@07:00ET",
        module="Memory",
        gate="memory",
        cog="AdminServer",
        description="Check watched folder health: missing dirs, stale scans",
    ),
    "operational_continuity_sweep": JobMeta(
        schedule="15m",
        module="Work",
        gate="work",
        cog="AdminServer",
        manual_trigger=True,
        description="Compute overdue, stale, unowned, unresolved, and escalated operational continuity states",
    ),
    "inbox_stalled_thread_sweep": JobMeta(
        schedule="10m",
        module="Work",
        gate="work",
        cog="AdminServer",
        manual_trigger=True,
        description="Recover inbox question threads stuck in processing and requeue their response generation",
    ),
}


def get_manual_triggerable() -> dict[str, JobMeta]:
    """Return only jobs that can be triggered via /admin_run."""
    return {k: v for k, v in JOB_REGISTRY.items() if v.manual_trigger}


def get_jobs_for_cog(cog_name: str) -> dict[str, JobMeta]:
    """Return jobs owned by a specific cog."""
    return {k: v for k, v in JOB_REGISTRY.items() if v.cog == cog_name}


def get_jobs_by_module(module: str) -> dict[str, JobMeta]:
    """Return jobs in a specific workflow module."""
    return {k: v for k, v in JOB_REGISTRY.items() if v.module == module}


# ── Gate evaluation ──────────────────────────────────────────────────────────
# Gate strings map to combinations of WorkflowConfig flags.
# ``requires_accelerators`` is handled separately by the caller.

def is_gate_open(gate: str, wf: Any) -> bool:
    """Return True if the workflow flags for *gate* are satisfied.

    *wf* is a ``WorkflowConfig`` instance (or None → use defaults).
    """
    def _flag(attr: str, default: bool = True) -> bool:
        return getattr(wf, attr, default) if wf else default

    evaluators: dict[str, bool] = {
        "work": _flag("work_enabled", True),
        "memory": _flag("memory_enabled", True),
        "scout": _flag("memory_enabled", True) and _flag("web_search_enabled", False),
        "pipeline": _flag("pipeline_enabled", True),
        "pipeline_past_clients": (
            _flag("pipeline_enabled", True) and _flag("past_clients_enabled", False)
        ),
        "health": _flag("health_enabled", True),
        "persona_meetings": _flag("persona_meetings_enabled", False),
    }
    return evaluators.get(gate, False)
