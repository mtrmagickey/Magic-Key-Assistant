"""
Migration: Add meeting_agenda_items table
Run this once to create the table for /agenda and /agenda_list commands.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "assistant.db"

def run_migration():
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meeting_agenda_items'")
    if cursor.fetchone():
        print("✅ meeting_agenda_items table already exists")
        conn.close()
        return
    
    # Create the table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meeting_agenda_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            context TEXT,
            submitted_by_user_id INTEGER NOT NULL,
            submitted_by_username TEXT,
            priority TEXT DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
            expires_at TEXT NOT NULL,
            used_at TEXT,
            used_in_meeting_date TEXT,
            status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'discussed', 'expired')),
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agenda_status ON meeting_agenda_items(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agenda_expires ON meeting_agenda_items(expires_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agenda_priority ON meeting_agenda_items(priority)")
    
    conn.commit()
    conn.close()
    print("✅ Created meeting_agenda_items table")

if __name__ == "__main__":
    run_migration()
