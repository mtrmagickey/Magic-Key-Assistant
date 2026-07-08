"""Bulk-replace raw str(e)/str(exc) in broad exception handlers with safer responses."""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTER_DIR = ROOT / "LeisureLLM" / "admin" / "routers"

FILES_TO_PROCESS = [
    "system.py",
    "settings.py",
    "continuity.py",
    "artifacts.py",
    "review_queue.py",
    "model_router_api.py",
    "retrieval_log.py",
    "moat.py",
    "knowledge.py",
    "activity.py",
]

PAT_SUCCESS = re.compile(r'^(\s*)return \{"success": False, "error": str\((e|exc)\)\}$')
PAT_VALID = re.compile(r'^(\s*)return \{"valid": False, "error": str\((e|exc)\)\}$')
PAT_MESSAGE = re.compile(r'^(\s*)return \{"success": False, "message": str\((e|exc)\)\}$')
PAT_EXCEPT_BROAD = re.compile(r"except\s+Exception\s+as\s+")

total_replaced = 0
for fname in FILES_TO_PROCESS:
    fpath = ROUTER_DIR / fname
    if not fpath.exists():
        print(f"{fname}: FILE NOT FOUND")
        continue

    lines = fpath.read_text(encoding="utf-8").split("\n")
    new_lines = []
    file_count = 0

    for i, line in enumerate(lines):
        m_success = PAT_SUCCESS.match(line)
        m_valid = PAT_VALID.match(line)
        m_message = PAT_MESSAGE.match(line)

        match = m_success or m_valid or m_message
        if match:
            is_broad = False
            for j in range(i - 1, max(i - 6, -1), -1):
                back = lines[j].strip()
                if PAT_EXCEPT_BROAD.match(back):
                    is_broad = True
                    break
                if back.startswith("except "):
                    break
            if is_broad:
                indent = match.group(1)
                if m_valid:
                    replacement = (
                        f'{indent}return {{"valid": False, "error": "validation_failed", '
                        '"message": "Validation check failed."}}'
                    )
                else:
                    replacement = (
                        f'{indent}return {{"success": False, "error": "request_failed", '
                        '"message": "Something went wrong. Please try again."}}'
                    )
                new_lines.append(replacement)
                file_count += 1
                continue

        new_lines.append(line)

    if file_count > 0:
        fpath.write_text("\n".join(new_lines), encoding="utf-8")
        print(f"{fname}: {file_count} replacements")
        total_replaced += file_count
    else:
        print(f"{fname}: no changes")

print(f"\nTotal: {total_replaced} replacements")