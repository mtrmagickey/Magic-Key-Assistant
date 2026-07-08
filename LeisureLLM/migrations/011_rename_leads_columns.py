"""
Migration 011: Rename leads & lead_activity columns to match application code.

Schema used: title, contact_email, estimated_value, last_touched_at
Code uses:   name,  contact_info,  value_estimate,  last_activity

Also fixes lead_activity: description→summary, performed_by_*→created_by_*,
and updates the activity_type CHECK constraint to allow 'creation' instead of 'created'.

Safe to run multiple times (checks column existence before renaming).
Requires SQLite ≥ 3.25.0 (Python ≥ 3.8).
"""

import os
import sqlite3

DB_PATH = globals().get("DB_PATH") or os.path.join(os.path.dirname(__file__), "..", "assistant.db")


def get_columns(cursor, table: str) -> list[str]:
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def table_exists(cursor, table: str) -> bool:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None


def rename_column(cursor, table: str, old_name: str, new_name: str, columns: list[str]):
    """Rename a column if old_name exists and new_name does not."""
    if old_name in columns and new_name not in columns:
        print(f"  Renaming {table}.{old_name} → {new_name}")
        cursor.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")
    elif new_name in columns:
        print(f"  {table}.{new_name} already exists — skipping")
    else:
        print(f"  {table}.{old_name} not found and {new_name} not found — skipping")


def migrate():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH} — nothing to migrate (fresh install will use new schema)")
        return

    print(f"Connecting to {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA busy_timeout = 30000")

    # ── leads table ───────────────────────────────────────────────────────────
    if table_exists(cursor, "leads"):
        cols = get_columns(cursor, "leads")
        print("Migrating leads table columns...")
        rename_column(cursor, "leads", "title", "name", cols)
        rename_column(cursor, "leads", "contact_email", "contact_info", cols)
        rename_column(cursor, "leads", "estimated_value", "value_estimate", cols)
        rename_column(cursor, "leads", "last_touched_at", "last_activity", cols)

        # Recreate the index on the renamed column
        cursor.execute("DROP INDEX IF EXISTS idx_leads_touched")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_touched ON leads(last_activity DESC)")
    else:
        print("leads table does not exist — skipping (will be created with new schema)")

    # ── lead_activity table ───────────────────────────────────────────────────
    # Need to rebuild this table because SQLite can't alter CHECK constraints.
    # The old CHECK allows 'created'; the code uses 'creation'.
    if table_exists(cursor, "lead_activity"):
        cols = get_columns(cursor, "lead_activity")
        print("Migrating lead_activity table...")

        # Simple column renames first
        rename_column(cursor, "lead_activity", "description", "summary", cols)
        rename_column(cursor, "lead_activity", "performed_by_user_id", "created_by_user_id", cols)
        rename_column(cursor, "lead_activity", "performed_by_username", "created_by_username", cols)

        # Rebuild table to fix CHECK constraint (created → creation)
        # Refresh columns after renames
        cols = get_columns(cursor, "lead_activity")
        print("  Rebuilding lead_activity to update CHECK constraint...")
        cursor.execute("DROP TABLE IF EXISTS lead_activity_new")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lead_activity_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                activity_type TEXT NOT NULL CHECK (activity_type IN ('creation', 'status_change', 'note', 'outreach', 'meeting', 'proposal_sent', 'follow_up', 'nudge')),
                summary TEXT,
                old_status TEXT,
                new_status TEXT,
                created_by_user_id INTEGER,
                created_by_username TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        def pick_column(preferred: str, fallback: str | None = None) -> str:
            if preferred in cols:
                return preferred
            if fallback and fallback in cols:
                return fallback
            return "NULL"

        cursor.execute(
            f"""
            INSERT INTO lead_activity_new (
                lead_id,
                activity_type,
                summary,
                old_status,
                new_status,
                created_by_user_id,
                created_by_username,
                created_at
            )
            SELECT
                {pick_column('lead_id')},
                CASE WHEN {pick_column('activity_type')} = 'created' THEN 'creation' ELSE {pick_column('activity_type')} END,
                {pick_column('summary', 'description')},
                {pick_column('old_status')},
                {pick_column('new_status')},
                {pick_column('created_by_user_id', 'performed_by_user_id')},
                {pick_column('created_by_username', 'performed_by_username')},
                {pick_column('created_at')}
            FROM lead_activity
            """
        )
        migrated = cursor.rowcount
        print(f"  Copied {migrated} rows to lead_activity_new")

        cursor.execute("DROP TABLE lead_activity")
        cursor.execute("ALTER TABLE lead_activity_new RENAME TO lead_activity")
        print("  Rebuilt lead_activity table with updated CHECK constraint")
    else:
        print("lead_activity table does not exist — skipping")

    conn.commit()
    conn.close()
    print("Migration 011 complete.")


if __name__ == "__main__":
    migrate()
