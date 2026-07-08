from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from admin.dependencies import (
    CONFIG_DIR,
    LEISURELLM_DIR,
    ROUTER_CONFIG_PATH,
    get_db_optional,
    get_model_router,
    is_first_run,
)
from admin.router_registry import iter_router_modules
from migrations.runner import MIGRATIONS_DIR
from services.bot_config import BOT_CONFIG_PATH, CONFIG_SECTIONS
from services.model_router import PipelineRole

from core.app_metadata import get_app_version
from core.config_loader import OrgProfile, WorkflowConfig
from core.job_registry import JOB_REGISTRY, is_gate_open


@dataclass(frozen=True)
class PathStatus:
    path: str
    exists: bool
    kind: str


@dataclass(frozen=True)
class WorkflowGateSnapshot:
    name: str
    open: bool
    reason: str
    source: str = ""


@dataclass(frozen=True)
class JobSnapshot:
    name: str
    module: str
    schedule: str
    gate: str
    gate_open: bool
    manual_trigger: bool
    requires_accelerators: bool
    cog: str
    description: str
    source: str = ""


@dataclass(frozen=True)
class RouterSnapshot:
    name: str
    module_path: str
    description: str
    surface: str
    prefix: str
    path_count: int
    source: str = ""


@dataclass(frozen=True)
class MigrationInventory:
    total_files: int
    versions: list[int]
    duplicate_versions: list[int]
    files: list[str]
    duplicate_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DatabaseInspection:
    path: str
    exists: bool
    integrity: str
    table_count: int
    schema_versions_present: bool
    applied_versions: list[int]
    pending_versions: list[int]
    latest_applied_version: int | None
    latest_discovered_version: int | None
    error: str | None = None


@dataclass(frozen=True)
class DoctorCheck:
    status: str
    code: str
    title: str
    detail: str
    source: str = ""


@dataclass(frozen=True)
class DoctorReport:
    summary: str
    healthy: bool
    status_counts: dict[str, int]
    database: DatabaseInspection
    checks: list[DoctorCheck]


@dataclass(frozen=True)
class ModelBackendSnapshot:
    name: str
    backend_type: str
    endpoint_url: str | None
    default_model: str | None
    available_models: list[str]
    client_connected: bool
    source: str = ""


@dataclass(frozen=True)
class PipelineRoleSnapshot:
    role: str
    backend_name: str
    model: str
    temperature: float
    max_tokens: int
    enabled: bool
    source: str = ""


@dataclass(frozen=True)
class PipelineSnapshot:
    configured: bool
    name: str | None
    timeout_seconds: float | None
    parallel_initial_critique: bool | None
    roles: list[PipelineRoleSnapshot]


@dataclass(frozen=True)
class SystemSnapshot:
    product_name: str
    app_version: str
    first_run: bool
    operation_mode: str
    org_name: str
    timezone: str
    paths: list[PathStatus]
    workflow_flags: dict[str, Any]
    workflow_gates: list[WorkflowGateSnapshot]
    jobs: list[JobSnapshot]
    routers: list[RouterSnapshot]
    migration_inventory: MigrationInventory
    model_backends: list[ModelBackendSnapshot]
    pipeline: PipelineSnapshot
    config_sections: list[str]
    db_connected: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _safe_app_version() -> str:
    return get_app_version()


def _safe_operation_mode() -> str:
    try:
        from config import OPERATION_MODE

        return OPERATION_MODE
    except Exception:
        return "solo"


def _path_status(path: Path, kind: str) -> PathStatus:
    return PathStatus(path=str(path), exists=path.exists(), kind=kind)


def _snapshot_path(snapshot: SystemSnapshot, kind: str) -> Path | None:
    for entry in snapshot.paths:
        if entry.kind == kind:
            return Path(entry.path)
    return None


def _workflow_gate_reason(gate: str, wf: WorkflowConfig) -> str:
    reasons = {
        "work": "work_enabled",
        "memory": "memory_enabled",
        "scout": "memory_enabled and web_search_enabled",
        "pipeline": "pipeline_enabled",
        "pipeline_past_clients": "pipeline_enabled and past_clients_enabled",
        "health": "health_enabled",
        "persona_meetings": "persona_meetings_enabled",
    }
    if is_gate_open(gate, wf):
        return f"enabled by {reasons.get(gate, gate)}"
    return f"disabled by {reasons.get(gate, gate)}"


def _build_workflow_gates(wf: WorkflowConfig) -> list[WorkflowGateSnapshot]:
    gate_names = (
        "work",
        "memory",
        "scout",
        "pipeline",
        "pipeline_past_clients",
        "health",
        "persona_meetings",
    )
    _gate_config_keys = {
        "work": "work_enabled",
        "memory": "memory_enabled",
        "scout": "web_search_enabled",
        "pipeline": "pipeline_enabled",
        "pipeline_past_clients": "past_clients_enabled",
        "health": "health_enabled",
        "persona_meetings": "persona_meetings_enabled",
    }
    return [
        WorkflowGateSnapshot(
            name=gate,
            open=is_gate_open(gate, wf),
            reason=_workflow_gate_reason(gate, wf),
            source=f"config/workflows.yaml -> {_gate_config_keys.get(gate, gate)}",
        )
        for gate in gate_names
    ]


def _build_jobs(wf: WorkflowConfig) -> list[JobSnapshot]:
    personas_enabled = bool(getattr(wf, "personas_enabled", False))
    jobs: list[JobSnapshot] = []
    for name, meta in JOB_REGISTRY.items():
        gate_open = is_gate_open(meta.gate, wf)
        if meta.requires_accelerators and not personas_enabled:
            gate_open = False
        jobs.append(
            JobSnapshot(
                name=name,
                module=meta.module,
                schedule=meta.schedule,
                gate=meta.gate,
                gate_open=gate_open,
                manual_trigger=meta.manual_trigger,
                requires_accelerators=meta.requires_accelerators,
                cog=meta.cog,
                description=meta.description,
                source=f"core/job_registry.py -> JOB_REGISTRY['{name}']",
            )
        )
    return sorted(jobs, key=lambda item: (item.module, item.name))


def _build_routers() -> list[RouterSnapshot]:
    routers: list[RouterSnapshot] = []
    for registration, module in iter_router_modules():
        router = getattr(module, "router", None)
        prefix = getattr(router, "prefix", "") if router is not None else ""
        path_count = len(getattr(router, "routes", [])) if router is not None else 0
        routers.append(
            RouterSnapshot(
                name=registration.name,
                module_path=registration.module_path,
                description=registration.description,
                surface=registration.surface,
                prefix=prefix,
                path_count=path_count,
                source=f"admin/router_registry.py -> ROUTER_REGISTRY('{registration.name}')",
            )
        )
    return routers


def _build_migration_inventory() -> MigrationInventory:
    discovered: list[tuple[int, Path]] = []
    for file in MIGRATIONS_DIR.glob("*.sqlite.sql"):
        name = file.stem.replace(".sqlite", "")
        parts = name.split("_", 1)
        if parts[0].isdigit():
            discovered.append((int(parts[0]), file))
    for file in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.py"):
        if file.name == "runner.py" or file.name.startswith("__"):
            continue
        parts = file.stem.split("_", 1)
        if parts[0].isdigit():
            discovered.append((int(parts[0]), file))
    discovered.sort(key=lambda item: (item[0], item[1].name))
    versions = [version for version, _ in discovered]
    duplicate_versions = sorted({version for version in versions if versions.count(version) > 1})
    files = [path.name for _, path in discovered]
    dup_files = [
        path.name for version, path in discovered if version in set(duplicate_versions)
    ]
    return MigrationInventory(
        total_files=len(files),
        versions=versions,
        duplicate_versions=duplicate_versions,
        files=files,
        duplicate_files=dup_files,
    )


def _inspect_database(database_path: Path, inventory: MigrationInventory) -> DatabaseInspection:
    latest_discovered = inventory.versions[-1] if inventory.versions else None
    if not database_path.exists():
        return DatabaseInspection(
            path=str(database_path),
            exists=False,
            integrity="missing",
            table_count=0,
            schema_versions_present=False,
            applied_versions=[],
            pending_versions=inventory.versions,
            latest_applied_version=None,
            latest_discovered_version=latest_discovered,
        )

    try:
        connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception as exc:
        return DatabaseInspection(
            path=str(database_path),
            exists=True,
            integrity="unavailable",
            table_count=0,
            schema_versions_present=False,
            applied_versions=[],
            pending_versions=inventory.versions,
            latest_applied_version=None,
            latest_discovered_version=latest_discovered,
            error=str(exc),
        )

    with connection:
        try:
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
            integrity = str(integrity_row[0]) if integrity_row else "unknown"
            table_names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            schema_versions_present = "schema_versions" in table_names
            applied_versions: list[int] = []
            if schema_versions_present:
                applied_versions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_versions ORDER BY version"
                    ).fetchall()
                ]
            applied_set = set(applied_versions)
            pending_versions = [version for version in inventory.versions if version not in applied_set]
            latest_applied = applied_versions[-1] if applied_versions else None
            return DatabaseInspection(
                path=str(database_path),
                exists=True,
                integrity=integrity,
                table_count=len(table_names),
                schema_versions_present=schema_versions_present,
                applied_versions=applied_versions,
                pending_versions=pending_versions,
                latest_applied_version=latest_applied,
                latest_discovered_version=latest_discovered,
            )
        except Exception as exc:
            return DatabaseInspection(
                path=str(database_path),
                exists=True,
                integrity="error",
                table_count=0,
                schema_versions_present=False,
                applied_versions=[],
                pending_versions=inventory.versions,
                latest_applied_version=None,
                latest_discovered_version=latest_discovered,
                error=str(exc),
            )


def build_system_doctor(snapshot: Optional[SystemSnapshot] = None) -> DoctorReport:
    current = snapshot or build_system_snapshot()
    checks: list[DoctorCheck] = []

    missing_critical = [
        entry
        for entry in current.paths
        if not entry.exists and entry.kind in {"config_dir", "org_profile", "workflows", "bot_settings", "migrations_dir"}
    ]
    if missing_critical:
        missing_sources = ", ".join(f"{entry.kind} ({entry.path})" for entry in missing_critical)
        checks.append(
            DoctorCheck(
                status="warn" if current.first_run else "fail",
                code="required_paths",
                title="Required configuration paths",
                detail="Missing: " + ", ".join(entry.kind for entry in missing_critical),
                source=missing_sources,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                status="pass",
                code="required_paths",
                title="Required configuration paths",
                detail="Core config, workflow, and migration paths are present.",
                source="build_system_snapshot() -> paths",
            )
        )

    if current.migration_inventory.duplicate_versions:
        dup_file_list = current.migration_inventory.duplicate_files
        checks.append(
            DoctorCheck(
                status="fail",
                code="migration_duplicates",
                title="Migration numbering",
                detail="Duplicate migration versions detected: "
                + ", ".join(str(version) for version in current.migration_inventory.duplicate_versions),
                source="migrations/ -> " + ", ".join(dup_file_list) if dup_file_list else "migrations/",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                status="pass",
                code="migration_duplicates",
                title="Migration numbering",
                detail="No duplicate migration versions were discovered.",
                source=str(MIGRATIONS_DIR),
            )
        )

    database_path = _snapshot_path(current, "database") or (LEISURELLM_DIR / "assistant.db")
    db_source = str(database_path)
    database = _inspect_database(database_path, current.migration_inventory)
    if database.error:
        checks.append(
            DoctorCheck(
                status="fail",
                code="database_access",
                title="Database inspection",
                detail=f"Read-only inspection failed: {database.error}",
                source=db_source,
            )
        )
    elif not database.exists:
        checks.append(
            DoctorCheck(
                status="warn" if current.first_run else "fail",
                code="database_access",
                title="Database inspection",
                detail="Database file is not present yet.",
                source=db_source,
            )
        )
    elif database.integrity.lower() != "ok":
        checks.append(
            DoctorCheck(
                status="fail",
                code="database_integrity",
                title="Database integrity",
                detail=f"PRAGMA integrity_check returned {database.integrity}.",
                source=db_source,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                status="pass",
                code="database_integrity",
                title="Database integrity",
                detail=f"Integrity check passed across {database.table_count} tables.",
                source=db_source,
            )
        )

    if database.exists and not database.error:
        if not database.schema_versions_present:
            checks.append(
                DoctorCheck(
                    status="warn" if current.first_run else "fail",
                    code="schema_versions",
                    title="Migration tracking table",
                    detail="schema_versions table was not found in the database.",
                    source=f"{db_source} -> schema_versions table",
                )
            )
        elif database.pending_versions:
            preview = ", ".join(str(version) for version in database.pending_versions[:5])
            checks.append(
                DoctorCheck(
                    status="warn",
                    code="pending_migrations",
                    title="Pending migrations",
                    detail=f"Database is behind the discovered migration inventory. Pending versions include {preview}.",
                    source=f"{db_source} <-> {MIGRATIONS_DIR}",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    status="pass",
                    code="pending_migrations",
                    title="Pending migrations",
                    detail="Database migration state matches the discovered migration inventory.",
                    source=f"{db_source} <-> {MIGRATIONS_DIR}",
                )
            )

    if current.model_backends:
        detail = f"{len(current.model_backends)} model backend(s) are configured."
        status = "pass" if current.pipeline.configured else "warn"
        if not current.pipeline.configured:
            detail += " A live pipeline is not active in this process."
        checks.append(
            DoctorCheck(
                status=status,
                code="model_surface",
                title="Model routing surface",
                detail=detail,
                source="config/model_router.json -> backends",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                status="warn",
                code="model_surface",
                title="Model routing surface",
                detail="No configured model backends are active in this process.",
                source="config/model_router.json",
            )
        )

    closed_gates = [gate.name for gate in current.workflow_gates if not gate.open]
    if len(closed_gates) == len(current.workflow_gates):
        checks.append(
            DoctorCheck(
                status="warn",
                code="workflow_gates",
                title="Workflow gates",
                detail="All workflow gates are currently closed.",
                source="config/workflows.yaml",
            )
        )
    else:
        detail = f"{len(current.workflow_gates) - len(closed_gates)} workflow gate(s) are open."
        if closed_gates:
            detail += " Closed: " + ", ".join(closed_gates)
        checks.append(
            DoctorCheck(
                status="pass",
                code="workflow_gates",
                title="Workflow gates",
                detail=detail,
                source="config/workflows.yaml",
            )
        )

    checks.append(
        DoctorCheck(
            status="pass" if current.routers else "fail",
            code="admin_surface",
            title="Admin surface registration",
            detail=(
                f"{len(current.routers)} admin router modules are registered."
                if current.routers
                else "No admin routers are registered."
            ),
            source="admin/router_registry.py -> ROUTER_REGISTRY",
        )
    )

    status_counts = {
        status: sum(1 for check in checks if check.status == status)
        for status in ("pass", "warn", "fail")
    }
    healthy = status_counts["fail"] == 0
    summary = (
        "System control plane passed all doctor checks."
        if healthy and status_counts["warn"] == 0
        else "System control plane is readable but has warnings to resolve."
        if healthy
        else "System control plane has failing checks that need attention."
    )
    return DoctorReport(
        summary=summary,
        healthy=healthy,
        status_counts=status_counts,
        database=database,
        checks=checks,
    )


def _build_model_backends() -> list[ModelBackendSnapshot]:
    model_router = get_model_router()
    if model_router is None:
        return []
    backends: list[ModelBackendSnapshot] = []
    for name, config in model_router.backends.items():
        backends.append(
            ModelBackendSnapshot(
                name=name,
                backend_type=config.backend_type.value,
                endpoint_url=config.endpoint_url,
                default_model=config.default_model,
                available_models=list(config.available_models),
                client_connected=name in model_router.clients,
                source=f"config/model_router.json -> backends['{name}']",
            )
        )
    return sorted(backends, key=lambda item: item.name)


def _build_pipeline_snapshot() -> PipelineSnapshot:
    model_router = get_model_router()
    if model_router is None or model_router.pipeline is None:
        return PipelineSnapshot(
            configured=False,
            name=None,
            timeout_seconds=None,
            parallel_initial_critique=None,
            roles=[],
        )

    pipeline = model_router.pipeline
    roles: list[PipelineRoleSnapshot] = []
    for role_name in PipelineRole:
        role = pipeline.roles.get(role_name)
        if role is None:
            continue
        roles.append(
            PipelineRoleSnapshot(
                role=role.role.value,
                backend_name=role.backend_name,
                model=role.model,
                temperature=role.temperature,
                max_tokens=role.max_tokens,
                enabled=role.enabled,
                source=f"config/model_router.json -> pipeline.roles['{role.role.value}']",
            )
        )

    return PipelineSnapshot(
        configured=True,
        name=pipeline.name,
        timeout_seconds=pipeline.timeout_seconds,
        parallel_initial_critique=pipeline.parallel_initial_critique,
        roles=roles,
    )


def build_system_snapshot() -> SystemSnapshot:
    org = OrgProfile.load()
    workflows = WorkflowConfig.load()
    db = get_db_optional()
    paths = [
        _path_status(LEISURELLM_DIR, "workspace"),
        _path_status(CONFIG_DIR, "config_dir"),
        _path_status(CONFIG_DIR / "org_profile.yaml", "org_profile"),
        _path_status(CONFIG_DIR / "workflows.yaml", "workflows"),
        _path_status(BOT_CONFIG_PATH, "bot_settings"),
        _path_status(ROUTER_CONFIG_PATH, "model_router"),
        _path_status(LEISURELLM_DIR / "migrations", "migrations_dir"),
        _path_status(LEISURELLM_DIR / "assistant.db", "database"),
    ]
    return SystemSnapshot(
        product_name=org.bot_name,
        app_version=_safe_app_version(),
        first_run=is_first_run(),
        operation_mode=_safe_operation_mode(),
        org_name=org.name,
        timezone=org.timezone,
        paths=paths,
        workflow_flags=asdict(workflows),
        workflow_gates=_build_workflow_gates(workflows),
        jobs=_build_jobs(workflows),
        routers=_build_routers(),
        migration_inventory=_build_migration_inventory(),
        model_backends=_build_model_backends(),
        pipeline=_build_pipeline_snapshot(),
        config_sections=sorted(CONFIG_SECTIONS.keys()),
        db_connected=bool(db and getattr(db, "_is_healthy", False)),
    )


def render_system_manifest_markdown(snapshot: Optional[SystemSnapshot] = None) -> str:
    current = snapshot or build_system_snapshot()
    lines = [
        "# System Manifest",
        "",
        f"- Product: {current.product_name}",
        f"- Version: {current.app_version}",
        f"- Mode: {current.operation_mode}",
        f"- First run: {'yes' if current.first_run else 'no'}",
        f"- Organization: {current.org_name}",
        f"- Timezone: {current.timezone}",
        f"- DB connected: {'yes' if current.db_connected else 'no'}",
        "",
        "## Workflow Gates",
    ]
    for gate in current.workflow_gates:
        source_suffix = f" [{gate.source}]" if gate.source else ""
        lines.append(f"- {gate.name}: {'open' if gate.open else 'closed'} ({gate.reason}){source_suffix}")
    lines.extend([
        "",
        f"## Jobs ({len(current.jobs)})",
    ])
    for job in current.jobs:
        lines.append(
            f"- {job.name}: {job.schedule} | gate={job.gate} | {'enabled' if job.gate_open else 'disabled'} [{job.source}]"
        )
    lines.extend([
        "",
        f"## Routers ({len(current.routers)})",
    ])
    for router in current.routers:
        lines.append(f"- {router.name}: prefix={router.prefix or '/'} paths={router.path_count}")
    lines.extend([
        "",
        f"## Migrations ({current.migration_inventory.total_files})",
        f"- Duplicate versions: {current.migration_inventory.duplicate_versions or 'none'}",
        "",
        f"## Model Backends ({len(current.model_backends)})",
    ])
    for backend in current.model_backends:
        lines.append(
            f"- {backend.name}: {backend.backend_type} | models={len(backend.available_models)} | connected={'yes' if backend.client_connected else 'no'}"
        )
    if current.pipeline.configured:
        lines.extend([
            "",
            f"## Pipeline ({current.pipeline.name})",
        ])
        for role in current.pipeline.roles:
            lines.append(f"- {role.role}: {role.backend_name} / {role.model} ({'enabled' if role.enabled else 'disabled'})")
    return "\n".join(lines)


def render_system_doctor_markdown(report: Optional[DoctorReport] = None) -> str:
    current = report or build_system_doctor()
    lines = [
        "# System Doctor",
        "",
        f"- Summary: {current.summary}",
        f"- Healthy: {'yes' if current.healthy else 'no'}",
        f"- Checks: pass={current.status_counts['pass']} warn={current.status_counts['warn']} fail={current.status_counts['fail']}",
        f"- Database integrity: {current.database.integrity}",
        f"- Pending migrations: {len(current.database.pending_versions)}",
        "",
        "## Checks",
    ]
    for check in current.checks:
        source_suffix = f" (source: {check.source})" if check.source else ""
        lines.append(f"- [{check.status}] {check.title}: {check.detail}{source_suffix}")
    return "\n".join(lines)