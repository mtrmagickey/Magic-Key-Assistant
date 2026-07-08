import os
import sqlite3

DB_PATH = "LeisureLLM/assistant.db"

def migrate():
    if not os.path.exists(DB_PATH):
        print("Database not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("Clearing existing tasks...")
    c.execute("DELETE FROM tasks")
    
    print("Creating task_owners table...")
    c.execute("""
        CREATE TABLE IF NOT EXISTS task_owners (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            username TEXT,
            assigned_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, user_id)
        )
    """)
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_task_owners_task ON task_owners(task_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_task_owners_user ON task_owners(user_id)")

    conn.commit()
    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
