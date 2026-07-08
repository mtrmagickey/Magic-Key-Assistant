-- Migration 016: Create task_owners table for multi-owner task assignment
-- Replaces: refactor_actions_owner.py

CREATE TABLE IF NOT EXISTS task_owners (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    username TEXT,
    assigned_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_task_owners_task ON task_owners(task_id);
CREATE INDEX IF NOT EXISTS idx_task_owners_user ON task_owners(user_id);
