"""Run focused tests and write results to Output/scratch/_test_results.txt."""

from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "Output" / "scratch"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RESULT_PATH = OUTPUT_DIR / "_test_results.txt"

result = subprocess.run(
    [".venv/Scripts/python.exe", "-m", "pytest", "tests/", "-q", "--tb=short"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    cwd=str(ROOT),
)
output = result.stdout + "\n" + result.stderr
lines = output.strip().splitlines()
tail = "\n".join(lines[-30:])
RESULT_PATH.write_text(tail, encoding="utf-8")
print(f"Exit code: {result.returncode}")
print(f"Results written to {RESULT_PATH} ({len(lines)} total lines, last 30 saved)")