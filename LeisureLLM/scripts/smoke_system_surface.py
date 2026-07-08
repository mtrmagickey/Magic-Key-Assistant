from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
if str(LEISURELLM_DIR) not in sys.path:
    sys.path.insert(0, str(LEISURELLM_DIR))

from core.system_explainer import explain_health_summary, render_explanation_markdown  # noqa: E402
from core.system_snapshot import (  # noqa: E402
    build_system_doctor,
    build_system_snapshot,
    render_system_doctor_markdown,
    render_system_manifest_markdown,
)


def main() -> int:
    snapshot = build_system_snapshot()
    doctor = build_system_doctor(snapshot)
    explanation = render_explanation_markdown([explain_health_summary(snapshot)])
    payload = {
        "manifest_excerpt": render_system_manifest_markdown(snapshot).splitlines()[:8],
        "doctor_excerpt": render_system_doctor_markdown(doctor).splitlines()[:8],
        "explanation_excerpt": explanation.splitlines()[:8],
        "healthy": doctor.healthy,
        "warnings": doctor.status_counts["warn"],
        "failures": doctor.status_counts["fail"],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())