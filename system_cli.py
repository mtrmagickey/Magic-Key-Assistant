from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
if str(LEISURELLM_DIR) not in sys.path:
    sys.path.insert(0, str(LEISURELLM_DIR))

from core.system_explainer import (  # noqa: E402
    explain_health_summary,
    explain_jobs,
    explain_mode,
    explain_pipeline,
    explain_storage,
    explain_workflows,
    render_explanation_markdown,
)
from core.system_snapshot import (  # noqa: E402
    build_system_doctor,
    build_system_snapshot,
    render_system_doctor_markdown,
    render_system_manifest_markdown,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only operational inspection CLI for Magic Key Assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="render a concise human-readable status summary")
    status_parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown")

    manifest_parser = subparsers.add_parser("manifest", help="render the full system manifest")
    manifest_parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown")

    explain_parser = subparsers.add_parser("explain", help="explain one part of the control plane")
    explain_parser.add_argument("topic", choices=["mode", "jobs", "pipeline", "workflows", "storage", "health", "all"])
    explain_parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown")

    doctor_parser = subparsers.add_parser("doctor", help="run a read-only diagnostic summary")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown")
    return parser


def _sections_for_topic(topic: str, snapshot):
    mapping = {
        "mode": [explain_mode(snapshot)],
        "jobs": [explain_jobs(snapshot)],
        "pipeline": [explain_pipeline(snapshot)],
        "workflows": [explain_workflows(snapshot)],
        "storage": [explain_storage(snapshot)],
        "health": [explain_health_summary(snapshot)],
        "all": [
            explain_mode(snapshot),
            explain_workflows(snapshot),
            explain_jobs(snapshot),
            explain_pipeline(snapshot),
            explain_storage(snapshot),
            explain_health_summary(snapshot),
        ],
    }
    return mapping[topic]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    snapshot = build_system_snapshot()
    doctor = build_system_doctor(snapshot)

    if args.command == "manifest":
        if args.as_json:
            print(snapshot.to_json())
        else:
            print(render_system_manifest_markdown(snapshot))
        return 0

    if args.command == "status":
        payload = {
            "product_name": snapshot.product_name,
            "app_version": snapshot.app_version,
            "operation_mode": snapshot.operation_mode,
            "first_run": snapshot.first_run,
            "db_connected": snapshot.db_connected,
            "open_workflow_gates": [gate.name for gate in snapshot.workflow_gates if gate.open],
            "configured_backends": [backend.name for backend in snapshot.model_backends],
            "pipeline_configured": snapshot.pipeline.configured,
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("# System Status\n")
            for key, value in payload.items():
                print(f"- {key}: {value}")
        return 0

    if args.command == "explain":
        sections = _sections_for_topic(args.topic, snapshot)
        if args.as_json:
            print(json.dumps([{"title": section.title, "lines": section.lines} for section in sections], indent=2))
        else:
            print(render_explanation_markdown(sections))
        return 0

    if args.command == "doctor":
        if args.as_json:
            print(json.dumps({
                "summary": doctor.summary,
                "healthy": doctor.healthy,
                "status_counts": doctor.status_counts,
                "database": {
                    "path": doctor.database.path,
                    "exists": doctor.database.exists,
                    "integrity": doctor.database.integrity,
                    "pending_versions": doctor.database.pending_versions,
                    "latest_applied_version": doctor.database.latest_applied_version,
                    "latest_discovered_version": doctor.database.latest_discovered_version,
                },
                "checks": [
                    {
                        "status": check.status,
                        "code": check.code,
                        "title": check.title,
                        "detail": check.detail,
                        "source": check.source,
                    }
                    for check in doctor.checks
                ],
            }, indent=2, sort_keys=True))
        else:
            print(render_system_doctor_markdown(doctor))
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())