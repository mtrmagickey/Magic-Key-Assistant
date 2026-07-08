-- Migration 027: Add curation columns to knowledge_gaps
-- Required by the gap curation workflow in admin/routers/knowledge.py
--
-- NOTE: ALTER TABLE ADD COLUMN is safe to re-run only when the column
-- doesn't already exist.  For databases upgraded via _ensure_aux_tables
-- these columns may already be present — the migration runner marks this
-- migration applied only on success.  If it fails because columns exist
-- the backward-compat path in database.py handles it.

ALTER TABLE knowledge_gaps ADD COLUMN curation_status TEXT DEFAULT 'pending';
ALTER TABLE knowledge_gaps ADD COLUMN curation_reason TEXT;
ALTER TABLE knowledge_gaps ADD COLUMN curated_at TEXT;
ALTER TABLE knowledge_gaps ADD COLUMN curated_by_username TEXT;
