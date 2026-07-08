from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULT_PATH = ROOT / "Output" / "scratch" / "_test_results3.txt"

for _ in range(60):
    if RESULT_PATH.exists():
        lines = RESULT_PATH.read_text(encoding="utf-8").strip().splitlines()
        for line in lines[-5:]:
            print(line)
        break
    time.sleep(5)
else:
    print("TIMEOUT: file never appeared")