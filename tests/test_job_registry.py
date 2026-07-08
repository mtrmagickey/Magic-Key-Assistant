"""Tests for core.job_registry — declarative job registry and gate evaluation.

Covers:
- Registry integrity (all keys match expected set, every entry is a JobMeta)
- Gate evaluation (is_gate_open) for all gate strings
- requires_accelerators semantics
- Helper functions (get_manual_triggerable, get_jobs_for_cog, get_jobs_by_module)
- Equivalence with old ad-hoc AutonomousOps logic for every workflow flag combo
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Set

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "LeisureLLM"))

from core.job_registry import (
    JOB_REGISTRY,
    JobMeta,
    get_jobs_by_module,
    get_jobs_for_cog,
    get_manual_triggerable,
    is_gate_open,
)

# ── Registry integrity ──────────────────────────────────────────────────────

ALL_EXPECTED_JOBS = {
    "monday_planning_kickoff",
    "friday_closeout",
    "thursday_async_meeting",
    "end_of_week_reflection",
    "weekly_strategic_review",
    "monthly_partners_meeting",
    "daily_knowledge_refresh",
    "question_watchdog",
    "daily_digest",
    "weekly_dashboard_update",
    "rainmaker_morning_pipeline",
    "rainmaker_opportunity_hunt",
    "rainmaker_follow_up_nudges",
    "rainmaker_weekly_cold_review",
    "rainmaker_past_client_checkin",
    "daily_scout_search",
    "scout_background_crawl",
    "dreamer_ideation_cycle",
    "steward_daily_health_check",
    "steward_weekly_self_assessment",
    "steward_learning_loop_audit",
    "curator_daily_scan_task",
    "curator_weekly_deep_analysis_task",
    "curator_corpus_interrogation_task",
    "weekly_persona_meeting_digest",
    "hourly_persona_meeting",
    # Moat infrastructure (AdminServer cog)
    "feedback_learning_cycle",
    "data_retention_enforcement",
    "cost_savings_snapshot",
    "folder_watcher_health",
    "operational_continuity_sweep",
    "inbox_stalled_thread_sweep",
}


def test_registry_has_all_expected_jobs():
    assert set(JOB_REGISTRY.keys()) == ALL_EXPECTED_JOBS


def test_all_entries_are_jobmeta():
    for name, meta in JOB_REGISTRY.items():
        assert isinstance(meta, JobMeta), f"{name} is not a JobMeta"


def test_every_job_has_gate():
    valid_gates = {"work", "memory", "scout", "pipeline", "pipeline_past_clients", "health", "persona_meetings"}
    for name, meta in JOB_REGISTRY.items():
        assert meta.gate in valid_gates, f"{name} has unknown gate '{meta.gate}'"


def test_every_job_has_module():
    for name, meta in JOB_REGISTRY.items():
        assert meta.module, f"{name} has no module"


def test_count_is_31():
    """32 scheduled tasks — bump this if new jobs are added."""
    assert len(JOB_REGISTRY) == 32, f"Expected 32 jobs, got {len(JOB_REGISTRY)}"


# ── Gate evaluation ─────────────────────────────────────────────────────────

@dataclass
class FakeWF:
    """Minimal WorkflowConfig stand-in for gate tests."""
    work_enabled: bool = True
    memory_enabled: bool = True
    pipeline_enabled: bool = True
    health_enabled: bool = True
    web_search_enabled: bool = False
    personas_enabled: bool = False
    persona_meetings_enabled: bool = False
    past_clients_enabled: bool = False


class TestIsGateOpen:
    """Exhaustive gate evaluation tests."""

    def test_work_gate_default(self):
        assert is_gate_open("work", FakeWF()) is True

    def test_work_gate_disabled(self):
        assert is_gate_open("work", FakeWF(work_enabled=False)) is False

    def test_memory_gate_default(self):
        assert is_gate_open("memory", FakeWF()) is True

    def test_memory_gate_disabled(self):
        assert is_gate_open("memory", FakeWF(memory_enabled=False)) is False

    def test_scout_gate_needs_web_and_memory(self):
        assert is_gate_open("scout", FakeWF()) is False  # web_search_enabled=False
        assert is_gate_open("scout", FakeWF(web_search_enabled=True)) is True
        assert is_gate_open("scout", FakeWF(web_search_enabled=True, memory_enabled=False)) is False

    def test_pipeline_gate_default(self):
        assert is_gate_open("pipeline", FakeWF()) is True

    def test_pipeline_gate_disabled(self):
        assert is_gate_open("pipeline", FakeWF(pipeline_enabled=False)) is False

    def test_pipeline_past_clients_gate(self):
        assert is_gate_open("pipeline_past_clients", FakeWF()) is False  # past_clients OFF
        assert is_gate_open("pipeline_past_clients", FakeWF(past_clients_enabled=True)) is True
        assert is_gate_open("pipeline_past_clients", FakeWF(past_clients_enabled=True, pipeline_enabled=False)) is False

    def test_health_gate_default(self):
        assert is_gate_open("health", FakeWF()) is True

    def test_health_gate_disabled(self):
        assert is_gate_open("health", FakeWF(health_enabled=False)) is False

    def test_persona_meetings_gate(self):
        assert is_gate_open("persona_meetings", FakeWF()) is False  # default OFF
        assert is_gate_open("persona_meetings", FakeWF(persona_meetings_enabled=True)) is True

    def test_unknown_gate_returns_false(self):
        assert is_gate_open("nonexistent", FakeWF()) is False

    def test_none_workflow_uses_defaults(self):
        """When wf is None (no workflows.yaml), default-ON gates should open."""
        assert is_gate_open("work", None) is True
        assert is_gate_open("memory", None) is True
        assert is_gate_open("pipeline", None) is True
        assert is_gate_open("health", None) is True
        # Default-OFF gates stay closed
        assert is_gate_open("scout", None) is False  # web_search_enabled defaults False
        assert is_gate_open("persona_meetings", None) is False
        assert is_gate_open("pipeline_past_clients", None) is False


# ── Equivalence with old ad-hoc logic ───────────────────────────────────────

def _simulate_old_logic(wf: FakeWF) -> Set[str]:
    """Reproduce the OLD ad-hoc cog_load start logic to verify equivalence."""
    started = set()

    work_on = wf.work_enabled
    personas_on = wf.personas_enabled
    memory_on = wf.memory_enabled
    web_on = wf.web_search_enabled
    pipeline_on = wf.pipeline_enabled
    past_clients_on = wf.past_clients_enabled
    health_on = wf.health_enabled
    persona_mtg = personas_on and wf.persona_meetings_enabled

    if work_on:
        started |= {"daily_digest", "daily_knowledge_refresh", "monday_planning_kickoff",
                     "friday_closeout", "question_watchdog", "weekly_dashboard_update"}
        if personas_on:
            started |= {"thursday_async_meeting", "monthly_partners_meeting",
                         "end_of_week_reflection", "weekly_strategic_review"}

    if memory_on and web_on and personas_on:
        started |= {"daily_scout_search", "scout_background_crawl"}

    if pipeline_on and personas_on:
        started |= {"dreamer_ideation_cycle", "rainmaker_morning_pipeline",
                     "rainmaker_opportunity_hunt", "rainmaker_follow_up_nudges",
                     "rainmaker_weekly_cold_review"}
        if past_clients_on:
            started.add("rainmaker_past_client_checkin")

    if health_on:
        started |= {"steward_daily_health_check", "steward_weekly_self_assessment",
                     "steward_learning_loop_audit"}

    if memory_on:
        started |= {"curator_daily_scan_task", "curator_weekly_deep_analysis_task", "curator_corpus_interrogation_task"}

    if persona_mtg:
        started |= {"hourly_persona_meeting", "weekly_persona_meeting_digest"}

    return started


def _simulate_new_logic(wf: FakeWF) -> Set[str]:
    """Reproduce the NEW registry-driven logic."""
    started = set()
    personas_on = wf.personas_enabled
    for name, meta in JOB_REGISTRY.items():
        if meta.cog != "AutonomousOps":
            continue
        if not is_gate_open(meta.gate, wf):
            continue
        if meta.requires_accelerators and not personas_on:
            continue
        started.add(name)
    return started


# All meaningful flag combos for equivalence testing
_FLAG_COMBOS = [
    # (work, memory, pipeline, health, personas, web, past_clients, persona_mtg)
    (True, True, True, True, False, False, False, False),   # defaults
    (True, True, True, True, True, False, False, False),    # personas on
    (True, True, True, True, True, True, False, False),     # + web
    (True, True, True, True, True, True, True, False),      # + past clients
    (True, True, True, True, True, True, True, True),       # everything on
    (False, True, True, True, False, False, False, False),  # work off
    (True, False, True, True, False, False, False, False),  # memory off
    (True, True, False, True, False, False, False, False),  # pipeline off
    (True, True, True, False, False, False, False, False),  # health off
    (False, False, False, False, False, False, False, False),  # everything off
    (True, True, True, True, True, False, False, True),     # personas+meetings, no web
]


@pytest.mark.parametrize(
    "work,memory,pipeline,health,personas,web,past,mtg",
    _FLAG_COMBOS,
    ids=[
        "defaults",
        "personas_on",
        "personas+web",
        "personas+web+past",
        "everything_on",
        "work_off",
        "memory_off",
        "pipeline_off",
        "health_off",
        "everything_off",
        "personas+meetings",
    ],
)
def test_old_new_equivalence(work, memory, pipeline, health, personas, web, past, mtg):
    """The new registry-driven code must start the exact same set of jobs
    as the old ad-hoc code for every flag combination."""
    wf = FakeWF(
        work_enabled=work,
        memory_enabled=memory,
        pipeline_enabled=pipeline,
        health_enabled=health,
        personas_enabled=personas,
        web_search_enabled=web,
        past_clients_enabled=past,
        persona_meetings_enabled=mtg,
    )
    old = _simulate_old_logic(wf)
    new = _simulate_new_logic(wf)
    assert new == old, f"Mismatch — new-only: {new - old}, old-only: {old - new}"


# ── Helper functions ────────────────────────────────────────────────────────

def test_get_manual_triggerable():
    manual = get_manual_triggerable()
    assert all(v.manual_trigger for v in manual.values())
    assert "daily_digest" in manual
    assert "thursday_async_meeting" not in manual  # manual_trigger=False


def test_get_jobs_for_cog():
    aops = get_jobs_for_cog("AutonomousOps")
    assert len(aops) == 26
    assert get_jobs_for_cog("Nonexistent") == {}


def test_get_jobs_by_module():
    work = get_jobs_by_module("Work")
    assert "monday_planning_kickoff" in work
    assert "daily_digest" not in work  # module="Memory"

    health = get_jobs_by_module("Health")
    assert "steward_daily_health_check" in health
