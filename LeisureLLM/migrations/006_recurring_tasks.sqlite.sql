-- Migration 006: Recurring tasks — adds recurrence column to tasks table
-- Date: 2026-07-12

ALTER TABLE tasks ADD COLUMN recurrence TEXT CHECK (recurrence IN ('daily', 'weekly', 'biweekly', 'monthly'));
