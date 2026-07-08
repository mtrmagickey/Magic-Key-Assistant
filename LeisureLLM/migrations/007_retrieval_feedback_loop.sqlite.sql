-- Migration 007: Retrieval feedback loop — invisible improvement infrastructure
-- Date: 2026-02-16

-- 1. Add result_json to job_runs for sync stats persistence
ALTER TABLE job_runs ADD COLUMN result_json TEXT;

-- 2. Add chunk_sources to response_feedback for outlier detection
ALTER TABLE response_feedback ADD COLUMN chunk_sources TEXT;
-- chunk_sources is a JSON array of source file paths that contributed to the answer
-- e.g. ["docs/meeting_notes.txt", "docs/strategy.md"]

-- 3. Chunk feedback aggregation view — surfaces "bad apple" chunks
CREATE VIEW IF NOT EXISTS v_chunk_feedback_outliers AS
SELECT
    cs.value AS source,
    SUM(CASE WHEN rf.feedback = 'not_helpful' THEN 1 ELSE 0 END) AS neg_count,
    SUM(CASE WHEN rf.feedback = 'helpful' THEN 1 ELSE 0 END) AS pos_count,
    COUNT(*) AS total_count,
    ROUND(
        100.0 * SUM(CASE WHEN rf.feedback = 'not_helpful' THEN 1 ELSE 0 END) / COUNT(*),
        1
    ) AS neg_pct,
    MAX(rf.created_at) AS last_feedback_at
FROM response_feedback rf,
     json_each(rf.chunk_sources) AS cs
WHERE rf.chunk_sources IS NOT NULL
  AND rf.chunk_sources != '[]'
GROUP BY cs.value
HAVING COUNT(*) >= 3
ORDER BY neg_pct DESC, neg_count DESC;
