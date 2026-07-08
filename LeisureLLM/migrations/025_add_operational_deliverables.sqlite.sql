-- Migration 025: Add deliverables column to operational_records
-- Stores explicit deliverable descriptions and acceptance criteria
-- for action items extracted from conversations.

ALTER TABLE operational_records ADD COLUMN deliverables TEXT;
