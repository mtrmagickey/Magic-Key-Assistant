-- Migration 014: Create meeting_agenda_items table
-- Replaces: add_agenda_table.py

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
);

CREATE INDEX IF NOT EXISTS idx_agenda_status ON meeting_agenda_items(status);
CREATE INDEX IF NOT EXISTS idx_agenda_expires ON meeting_agenda_items(expires_at);
CREATE INDEX IF NOT EXISTS idx_agenda_priority ON meeting_agenda_items(priority);
