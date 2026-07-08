from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.system_snapshot import SystemSnapshot, build_system_doctor, build_system_snapshot


@dataclass(frozen=True)
class ExplanationSection:
    title: str
    lines: list[str]


def _require_snapshot(snapshot: SystemSnapshot | None) -> SystemSnapshot:
    return snapshot or build_system_snapshot()


def explain_mode(snapshot: SystemSnapshot | None = None) -> ExplanationSection:
    current = _require_snapshot(snapshot)
    lines = [
        f"Operation mode is {current.operation_mode}.",
        "Setup is incomplete." if current.first_run else "Setup is complete.",
        f"Organization profile is loaded for {current.org_name}.",
        f"Primary timezone is {current.timezone}.",
    ]
    return ExplanationSection(title="Mode", lines=lines)


def explain_jobs(snapshot: SystemSnapshot | None = None) -> ExplanationSection:
    current = _require_snapshot(snapshot)
    enabled = [job for job in current.jobs if job.gate_open]
    disabled = [job for job in current.jobs if not job.gate_open]
    manual = [job for job in current.jobs if job.manual_trigger]
    lines = [
        f"{len(enabled)} scheduled jobs are currently enabled by workflow gates.",
        f"{len(disabled)} scheduled jobs are currently disabled by workflow gates or accelerator requirements.",
        f"{len(manual)} jobs support manual trigger paths.",
    ]
    if disabled:
        lines.append("Disabled examples: " + ", ".join(job.name for job in disabled[:5]))
    return ExplanationSection(title="Jobs", lines=lines)


def explain_pipeline(snapshot: SystemSnapshot | None = None) -> ExplanationSection:
    current = _require_snapshot(snapshot)
    if not current.pipeline.configured:
        return ExplanationSection(
            title="Pipeline",
            lines=["No active model pipeline is configured in the current process.", "The model router may be uninitialized or only configuration files are present."],
        )

    lines = [
        f"Active pipeline is {current.pipeline.name}.",
        f"Pipeline timeout is {current.pipeline.timeout_seconds} seconds.",
        "Initial and critique stages run in parallel." if current.pipeline.parallel_initial_critique else "Initial and critique stages run sequentially.",
    ]
    for role in current.pipeline.roles:
        state = "enabled" if role.enabled else "disabled"
        lines.append(f"{role.role} uses {role.backend_name}/{role.model} and is {state}.")
    return ExplanationSection(title="Pipeline", lines=lines)


def explain_workflows(snapshot: SystemSnapshot | None = None) -> ExplanationSection:
    current = _require_snapshot(snapshot)
    lines = [
        f"{sum(1 for gate in current.workflow_gates if gate.open)} workflow gates are open.",
        f"{sum(1 for gate in current.workflow_gates if not gate.open)} workflow gates are closed.",
    ]
    for gate in current.workflow_gates:
        lines.append(f"{gate.name}: {'open' if gate.open else 'closed'} because {gate.reason}.")
    return ExplanationSection(title="Workflows", lines=lines)


def explain_storage(snapshot: SystemSnapshot | None = None) -> ExplanationSection:
    current = _require_snapshot(snapshot)
    existing = [entry for entry in current.paths if entry.exists]
    missing = [entry for entry in current.paths if not entry.exists]
    lines = [
        f"{len(existing)} tracked system paths currently exist.",
        f"{len(missing)} tracked system paths are missing.",
    ]
    if missing:
        lines.append("Missing paths: " + ", ".join(entry.kind for entry in missing))
    return ExplanationSection(title="Storage", lines=lines)


def explain_health_summary(snapshot: SystemSnapshot | None = None) -> ExplanationSection:
    current = _require_snapshot(snapshot)
    doctor = build_system_doctor(current)
    lines = [
        doctor.summary,
        "Database connection is healthy in-process." if current.db_connected else "Database connection is not currently established in-process.",
        f"Database integrity check returned {doctor.database.integrity}.",
        f"{len(doctor.database.pending_versions)} migration version(s) are still pending against the local database.",
        f"Doctor checks currently report {doctor.status_counts['warn']} warning(s) and {doctor.status_counts['fail']} failing check(s).",
        f"{len(current.routers)} admin routers are registered.",
    ]
    return ExplanationSection(title="Health", lines=lines)


def render_explanation_markdown(sections: Iterable[ExplanationSection]) -> str:
    lines = ["# System Explanation", ""]
    for section in sections:
        lines.append(f"## {section.title}")
        for line in section.lines:
            lines.append(f"- {line}")
        lines.append("")
    return "\n".join(lines).strip()