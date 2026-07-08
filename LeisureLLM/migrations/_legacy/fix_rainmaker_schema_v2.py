
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "assistant.db")

def fix_database():
    print(f"Connecting to database at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Fix pm_threads if missing run_date
    try:
        cursor.execute("PRAGMA table_info(pm_threads)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'pm_threads' in get_tables(cursor):
            if 'run_date' not in columns:
                print("Adding missing column 'run_date' to pm_threads...")
                cursor.execute("ALTER TABLE pm_threads ADD COLUMN run_date TEXT")
            else:
                print("pm_threads already has run_date.")
    except Exception as e:
        print(f"Error checking pm_threads: {e}")

    # 2. Fix job_runs if missing run_date
    try:
        cursor.execute("PRAGMA table_info(job_runs)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'job_runs' in get_tables(cursor):
            if 'run_date' not in columns:
                print("Adding missing column 'run_date' to job_runs...")
                cursor.execute("ALTER TABLE job_runs ADD COLUMN run_date TEXT")
            else:
                print("job_runs already has run_date.")
    except Exception as e:
        print(f"Error checking job_runs: {e}")

    # 3. Create rainmaker_seen_opportunities
    print("Ensuring rainmaker_seen_opportunities exists...")
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rainmaker_seen_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                url_hash TEXT NOT NULL,  -- for fast dedup lookup
                title TEXT,
                source_query TEXT,  -- what search query found this
                assessment TEXT CHECK (assessment IN ('elevated', 'passed', 'stale')),
                assessment_reason TEXT,  -- why elevated or passed
                lead_id INTEGER,  -- if elevated to a lead
                first_seen_date TEXT NOT NULL,
                last_seen_date TEXT,
                seen_count INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_rainmaker_seen_hash ON rainmaker_seen_opportunities(url_hash);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rainmaker_seen_date ON rainmaker_seen_opportunities(first_seen_date DESC);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rainmaker_seen_assessment ON rainmaker_seen_opportunities(assessment);")
        print("rainmaker_seen_opportunities table check complete.")
    except Exception as e:
        print(f"Error creating rainmaker_seen_opportunities: {e}")

    # 4. Create leads logic tables (from database.py just in case)
    print("Ensuring leads tables exist...")
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                source TEXT NOT NULL CHECK (source IN ('scout', 'dreamer', 'manual', 'referral', 'past_client')),
                source_id TEXT,
                status TEXT NOT NULL DEFAULT 'cold' CHECK (status IN ('cold', 'warm', 'hot', 'proposal', 'won', 'lost', 'dormant')),
                priority TEXT DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
                owner_user_id INTEGER,
                owner_username TEXT,
                contact_name TEXT,
                contact_info TEXT,
                contact_org TEXT,
                value_estimate TEXT,
                next_action TEXT,
                next_action_date TEXT,
                proposal_due_date TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                last_activity TEXT DEFAULT (datetime('now')),
                closed_at TEXT,
                close_reason TEXT
            );
        """)
        # Create lead_activity
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lead_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                activity_type TEXT NOT NULL CHECK (activity_type IN ('creation', 'status_change', 'note', 'outreach', 'meeting', 'proposal_sent', 'follow_up', 'nudge')),
                summary TEXT,
                old_status TEXT,
                new_status TEXT,
                created_by_user_id INTEGER,
                created_by_username TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        print("leads tables check complete.")
    except Exception as e:
        print(f"Error creating leads tables: {e}")

    conn.commit()
    conn.close()
    print("Migration complete.")

def get_tables(cursor):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    return [row[0] for row in cursor.fetchall()]

if __name__ == "__main__":
    fix_database()
