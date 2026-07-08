from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import aiosqlite
import pytest

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))


@pytest.mark.asyncio
async def test_python_migration_uses_runner_database_path(tmp_path):
    from migrations.runner import MigrationRunner

    db_path = tmp_path / "runner-target.db"
    migration_path = tmp_path / "999_probe.py"
    migration_path.write_text(
        """
DB_PATH = 'wrong.db'
import sqlite3

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(\"CREATE TABLE IF NOT EXISTS migration_path_probe (db_path TEXT)\")
    cur.execute(\"DELETE FROM migration_path_probe\")
    cur.execute(\"INSERT INTO migration_path_probe (db_path) VALUES (?)\", (DB_PATH,))
    conn.commit()
    conn.close()
""".strip(),
        encoding="utf-8",
    )

    conn = await aiosqlite.connect(str(db_path))
    try:
        runner = MigrationRunner(conn)
        await runner._apply_python_migration(migration_path)
    finally:
        await conn.close()

    verify = sqlite3.connect(str(db_path))
    try:
        row = verify.execute("SELECT db_path FROM migration_path_probe").fetchone()
    finally:
        verify.close()

    assert row is not None
    assert row[0] == str(db_path)


@pytest.mark.asyncio
async def test_runner_applies_duplicate_numeric_prefixes_by_filename(tmp_path, monkeypatch):
    from migrations import runner as runner_module
    from migrations.runner import MigrationRunner

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "016_alpha.sqlite.sql").write_text(
        "CREATE TABLE alpha_probe (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    (migrations_dir / "016_beta.sqlite.sql").write_text(
        "CREATE TABLE beta_probe (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    (migrations_dir / "017_gamma.sqlite.sql").write_text(
        "CREATE TABLE gamma_probe (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )

    db_path = tmp_path / "duplicate-prefixes.db"
    monkeypatch.setattr(runner_module, "MIGRATIONS_DIR", migrations_dir)

    conn = await aiosqlite.connect(str(db_path))
    try:
        runner = MigrationRunner(conn)
        applied, failed = await runner.run_pending_migrations()
        current_version = await runner.get_current_version()
        async with conn.execute(
            "SELECT filename, version FROM schema_versions ORDER BY version, filename"
        ) as cursor:
            rows = await cursor.fetchall()
    finally:
        await conn.close()

    assert (applied, failed) == (3, 0)
    assert current_version == 17
    assert [(row[0], row[1]) for row in rows] == [
        ("016_alpha.sqlite.sql", 16),
        ("016_beta.sqlite.sql", 16),
        ("017_gamma.sqlite.sql", 17),
    ]

    verify = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in verify.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('alpha_probe', 'beta_probe', 'gamma_probe')"
            ).fetchall()
        }
    finally:
        verify.close()

    assert tables == {"alpha_probe", "beta_probe", "gamma_probe"}


def test_migration_011_rebuilds_lead_activity_without_check_failure(tmp_path):
    db_path = tmp_path / "migration011.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                contact_email TEXT,
                estimated_value REAL,
                last_touched_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE lead_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                activity_type TEXT NOT NULL CHECK (activity_type IN ('created', 'status_change', 'note', 'outreach', 'meeting', 'proposal_sent', 'follow_up', 'nudge')),
                description TEXT,
                old_status TEXT,
                new_status TEXT,
                performed_by_user_id INTEGER,
                performed_by_username TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO leads (title, contact_email, estimated_value, last_touched_at) VALUES (?, ?, ?, ?)",
            ("Test Lead", "lead@example.com", 10.0, "2026-03-15"),
        )
        conn.execute(
            "INSERT INTO lead_activity (lead_id, activity_type, description, created_at) VALUES (1, 'created', 'Initial create', datetime('now'))"
        )
        conn.commit()
    finally:
        conn.close()

    migration_file = ROOT_DIR / "LeisureLLM" / "migrations" / "011_rename_leads_columns.py"
    spec = importlib.util.spec_from_file_location("migration011", migration_file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.DB_PATH = str(db_path)
    module.migrate()

    verify = sqlite3.connect(str(db_path))
    try:
        lead_cols = [row[1] for row in verify.execute("PRAGMA table_info(leads)").fetchall()]
        activity_cols = [row[1] for row in verify.execute("PRAGMA table_info(lead_activity)").fetchall()]
        activity_row = verify.execute(
            "SELECT activity_type, summary FROM lead_activity"
        ).fetchone()
    finally:
        verify.close()

    assert "name" in lead_cols
    assert "contact_info" in lead_cols
    assert "summary" in activity_cols
    assert "created_by_user_id" in activity_cols
    assert activity_row is not None
    assert activity_row[0] == "creation"
    assert activity_row[1] == "Initial create"