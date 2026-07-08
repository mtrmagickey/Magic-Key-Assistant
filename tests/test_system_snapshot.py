from __future__ import annotations

import subprocess
import sys

from core.system_explainer import explain_jobs, explain_pipeline, render_explanation_markdown
from core.system_snapshot import build_system_doctor, build_system_snapshot, render_system_manifest_markdown


def test_system_snapshot_has_core_sections() -> None:
    snapshot = build_system_snapshot()
    assert snapshot.product_name
    assert snapshot.operation_mode in {"solo", "team", "small"}
    assert snapshot.jobs
    assert snapshot.routers
    assert snapshot.migration_inventory.total_files >= 1
    assert "schedule" in snapshot.config_sections
    # Source hints populated on inventory items
    for job in snapshot.jobs:
        assert job.source, f"JobSnapshot '{job.name}' is missing source"
    for router in snapshot.routers:
        assert router.source, f"RouterSnapshot '{router.name}' is missing source"
    for gate in snapshot.workflow_gates:
        assert gate.source, f"WorkflowGateSnapshot '{gate.name}' is missing source"


def test_manifest_markdown_mentions_system_shape() -> None:
    snapshot = build_system_snapshot()
    manifest = render_system_manifest_markdown(snapshot)
    assert "# System Manifest" in manifest
    assert "## Workflow Gates" in manifest
    assert "## Routers" in manifest


def test_explainer_renders_multiple_sections() -> None:
    snapshot = build_system_snapshot()
    rendered = render_explanation_markdown([explain_jobs(snapshot), explain_pipeline(snapshot)])
    assert "# System Explanation" in rendered
    assert "## Jobs" in rendered
    assert "## Pipeline" in rendered


def test_cli_manifest_json_runs() -> None:
    result = subprocess.run(
        [sys.executable, "system_cli.py", "manifest", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"product_name"' in result.stdout
    assert '"routers"' in result.stdout


def test_cli_doctor_runs() -> None:
    result = subprocess.run(
        [sys.executable, "system_cli.py", "doctor"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "# System Doctor" in result.stdout
    assert "## Checks" in result.stdout


def test_system_doctor_reports_checks() -> None:
    report = build_system_doctor(build_system_snapshot())
    assert report.checks
    assert report.status_counts["pass"] >= 1
    assert report.database.path
    # Every doctor check must carry a source hint
    for check in report.checks:
        assert check.source, f"DoctorCheck '{check.code}' is missing a source hint"


def test_cli_doctor_json_runs() -> None:
    result = subprocess.run(
        [sys.executable, "system_cli.py", "doctor", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"checks"' in result.stdout
    assert '"status_counts"' in result.stdout
    assert '"source"' in result.stdout


def test_smoke_system_surface_script_runs() -> None:
    result = subprocess.run(
        [sys.executable, "LeisureLLM/scripts/smoke_system_surface.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"doctor_excerpt"' in result.stdout
    assert '"manifest_excerpt"' in result.stdout


def test_system_manifest_api(solo_web_client, bootstrap_admin_session) -> None:
    client = solo_web_client["client"]
    bootstrap_admin_session(client)
    response = client.get("/api/v1/system/manifest")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert "manifest" in payload
    assert payload["manifest"]["routers"]


def test_system_explain_api_topic(solo_web_client, bootstrap_admin_session) -> None:
    client = solo_web_client["client"]
    bootstrap_admin_session(client)
    response = client.get("/api/v1/system/explain?topic=jobs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["topic"] == "jobs"
    assert payload["sections"][0]["title"] == "Jobs"


def test_system_doctor_api(solo_web_client, bootstrap_admin_session) -> None:
    client = solo_web_client["client"]
    bootstrap_admin_session(client)
    response = client.get("/api/v1/system/doctor")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert "doctor" in payload
    assert payload["doctor"]["checks"]