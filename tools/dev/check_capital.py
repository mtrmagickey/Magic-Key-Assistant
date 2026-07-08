"""Quick diagnostic for the Knowledge Capital dashboard."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEISURELLM_DIR = ROOT / "LeisureLLM"
DB_PATH = LEISURELLM_DIR / "assistant.db"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LEISURELLM_DIR))

if not DB_PATH.exists():
    print(f"ERROR: Database not found at {DB_PATH.resolve()}")
    raise SystemExit(1)

conn = sqlite3.connect(str(DB_PATH))

tables = [
    row[0]
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
]
print(f"Total tables: {len(tables)}")

has_kce = "knowledge_capital_events" in tables
print(f"knowledge_capital_events table exists: {has_kce}")

try:
    versions = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall()
    ]
    print(f"Applied migrations: {versions}")
except Exception as exc:
    print(f"schema_versions check failed: {exc}")

mig_path = LEISURELLM_DIR / "migrations" / "013_knowledge_capital.sqlite.sql"
print(f"Migration file exists: {mig_path.exists()}")

try:
    from admin.routers import moat

    routes = [route.path for route in moat.router.routes]
    print(f"Moat router routes: {routes}")
except Exception as exc:
    print(f"Router check failed: {exc}")

conn.close()
print("\nDone.")