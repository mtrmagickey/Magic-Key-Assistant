-- Migration 010: Add dependencies column to tasks table.
--
-- The ActionItem entity model has always had a `dependencies` field
-- (List[int] of blocking task IDs), but it was never persisted.
-- This migration adds the column so the forward planner and
-- causal graph modules can operate on real dependency data.

ALTER TABLE tasks ADD COLUMN dependencies TEXT;  -- JSON array of task IDs

-- Index for fast lookup of "what does task X block?"
-- (requires application-level JSON parsing, but the index helps
--  sequential scans considerably)
CREATE INDEX IF NOT EXISTS idx_tasks_dependencies
    ON tasks(dependencies)
    WHERE dependencies IS NOT NULL;
