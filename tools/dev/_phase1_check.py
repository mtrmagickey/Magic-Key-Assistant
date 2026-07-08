from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

for f in sorted((ROOT / "LeisureLLM" / "admin" / "templates").glob("*.html")):
    content = f.read_text(encoding="utf-8")
    for i, line in enumerate(content.split("\n")):
        low = line.lower()
        if "phase 1" in low or "phase1" in low:
            print(f"{f.name}:{i + 1}  {line.strip()[:120]}")