-- Migration 004: Meeting notes, source links, and decision linkage
-- Date: 2026-02-07
-- Milestone: M2 — Stabilise Data & Upgrades

-- Meeting notes — structured output of parsed meetings
CREATE TABLE IF NOT EXISTS meeting_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    meeting_date TEXT,                    -- ISO 8601 date of the meeting
    attendees TEXT,                       -- JSON array of names
    raw_text TEXT,                        -- original transcript / paste
    created_at TEXT DEFAULT (datetime('now')),
    created_by_user_id INTEGER,
    created_by_username TEXT,
    discord_message_id INTEGER,          -- originating /parse_meeting message
    discord_thread_id INTEGER            -- thread created for this meeting
);

CREATE INDEX IF NOT EXISTS idx_meeting_notes_date ON meeting_notes(meeting_date);

-- Source links — provenance chain tying artifacts to their origins
CREATE TABLE IF NOT EXISTS source_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,            -- 'action_item', 'decision', 'lead', 'meeting_note', 'gap'
    record_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,            -- 'discord_message', 'document', 'meeting', 'command', 'web_search'
    source_id TEXT NOT NULL,              -- message ID, doc path, meeting_note ID, URL, etc.
    created_at TEXT DEFAULT (datetime('now')),
    metadata TEXT                         -- JSON for extra context
);

CREATE INDEX IF NOT EXISTS idx_source_links_record ON source_links(record_type, record_id);
CREATE INDEX IF NOT EXISTS idx_source_links_source ON source_links(source_type, source_id);

-- Link tasks (actions) to meetings
ALTER TABLE tasks ADD COLUMN source_meeting_id INTEGER REFERENCES meeting_notes(id) ON DELETE SET NULL;

-- Link tasks to decisions
ALTER TABLE tasks ADD COLUMN source_decision_id INTEGER REFERENCES decisions(id) ON DELETE SET NULL;

-- Link decisions to meetings
ALTER TABLE decisions ADD COLUMN source_meeting_id INTEGER REFERENCES meeting_notes(id) ON DELETE SET NULL;
