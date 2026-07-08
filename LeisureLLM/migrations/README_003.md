# Migration 003: Engagement and Escalation Tracking

## Purpose
Add comprehensive tracking for partner engagement, escalation workflows, action-gap linking, and sprint cycle management.

## Tables Added

### 1. `partner_engagement`
Tracks partner response metrics on a weekly basis.

**Columns:**
- `partner_user_id` - Discord user ID
- `partner_username` - Display name
- `questions_asked` - Count of questions assigned
- `questions_answered` - Count of answers provided
- `last_question_at` - Timestamp of most recent question
- `last_answer_at` - Timestamp of most recent answer
- `avg_response_time_hours` - Rolling average response time
- `response_rate` - Percentage of questions answered (0-100)
- `helpful_answers` - Count of helpful answers (future use)
- `unhelpful_answers` - Count of unhelpful answers (future use)
- `week_start_date` - ISO date of week start (Monday)

**Usage:** Calculate engagement scores, identify unresponsive partners, leaderboards

---

### 2. `sprint_cycles`
Tracks weekly sprint boundaries with goals and retrospectives.

**Columns:**
- `cycle_name` - e.g., "Week 1 2026"
- `start_date` / `end_date` - ISO dates
- `focus_areas` - JSON array of planned focus areas
- `goals` - JSON array of planned goals
- `action_items_planned` / `action_items_completed` - Completion tracking
- `gaps_resolved` - Knowledge gaps closed during cycle
- `partner_engagement_score` - Average engagement percentage
- `status` - 'planning', 'active', 'completed'
- `retrospective` - Auto-generated summary at cycle end

**Usage:** Weekly sprint boundaries, retrospective generation, historical metrics

---

### 3. `action_gap_links`
Many-to-many relationship between action items and knowledge gaps.

**Columns:**
- `action_id` - References `tasks.id`
- `gap_id` - References `knowledge_gaps.id`
- `link_type` - 'resolves', 'related', 'blocks'
- `notes` - Optional context

**Usage:** Traceability (why was this action created?), auto-resolve gaps when actions complete

---

### 4. `escalations`
Audit trail for escalated items requiring attention.

**Columns:**
- `entity_type` - 'action_item' or 'knowledge_gap'
- `entity_id` - ID of escalated entity
- `reason` - 'blocked_2_weeks', 'no_response_3_questions', etc.
- `escalated_to_user_id` / `escalated_to_username` - Who should handle it
- `escalation_message` - Description of issue
- `escalated_at` - Timestamp
- `resolved_at` / `resolution_notes` - Closure tracking
- `status` - 'open', 'resolved', 'dismissed'

**Usage:** Escalation workflows, accountability tracking, historical analysis

---

## Columns Added to Existing Tables

### `tasks` (action items)
- `blocked_since` TEXT - Timestamp when status changed to 'blocked'
- `escalated` INTEGER - Boolean flag (0/1) for escalated items
- `escalation_notes` TEXT - Reason for escalation

### `knowledge_gaps`
- `probing_questions_asked` INTEGER - Count of probing questions sent to partner
- `last_probing_question_at` TEXT - Most recent question timestamp
- `response_count` INTEGER - Count of responses received
- `last_response_at` TEXT - Most recent response timestamp
- `escalated` INTEGER - Boolean flag (0/1) for escalated gaps

---

## How to Apply

### SQLite (current setup)
```powershell
cd LeisureLLM
.\venv312\Scripts\python.exe run_migration_sqlite.py
```

The migration runner will:
1. Check which migrations have been applied
2. Apply `003_engagement_and_escalation.sqlite.sql` if not already applied
3. Record the migration in a tracking table

---

## Rollback

If issues occur, you can manually drop the new tables:

```sql
DROP TABLE IF EXISTS partner_engagement;
DROP TABLE IF EXISTS sprint_cycles;
DROP TABLE IF EXISTS action_gap_links;
DROP TABLE IF EXISTS escalations;
```

And remove the added columns (SQLite doesn't support DROP COLUMN, so you'd need to recreate tables):

```sql
-- Backup data first!
-- Then recreate tables without the new columns
```

**Note:** It's safer to restore from backup than to manually rollback.

---

## Testing After Migration

### Verify tables created:
```sql
SELECT name FROM sqlite_master WHERE type='table' 
AND name IN ('partner_engagement', 'sprint_cycles', 'action_gap_links', 'escalations');
```

### Verify columns added:
```sql
PRAGMA table_info(tasks);
PRAGMA table_info(knowledge_gaps);
```

Look for:
- `tasks`: blocked_since, escalated, escalation_notes
- `knowledge_gaps`: probing_questions_asked, last_probing_question_at, response_count, last_response_at, escalated

---

## Dependencies

This migration assumes:
- Migration 001 (initial schema) applied
- Migration 002 (feedback and gaps) applied
- Tables exist: `tasks`, `knowledge_gaps`

---

## Indexes

The migration creates indexes on:
- `partner_engagement(partner_user_id)`
- `partner_engagement(week_start_date DESC)`
- `sprint_cycles(status)`
- `sprint_cycles(start_date DESC)`
- `action_gap_links(action_id)`
- `action_gap_links(gap_id)`
- `escalations(entity_type, entity_id)`
- `escalations(status)`
- `escalations(escalated_at DESC)`

These indexes optimize:
- Weekly engagement queries
- Active sprint lookups
- Gap-action relationship traversal
- Escalation filtering and sorting

---

## Impact on Existing Features

### Minimal Impact
- Existing commands continue to work unchanged
- New columns have defaults and allow NULL
- Queries selecting * will include new columns (but this is handled by code)

### New Behaviors
- Marking action as "blocked" now records `blocked_since` timestamp
- Marking action as "done" checks for linked gaps to auto-resolve
- Thursday prompts now track `probing_questions_asked`
- Interview answers now track `response_count`

---

## Performance Considerations

All new tables are small:
- `partner_engagement`: ~5-10 rows per week (one per active partner)
- `sprint_cycles`: ~52 rows per year (one per week)
- `action_gap_links`: ~10-50 rows per week (depends on interview volume)
- `escalations`: ~5-20 rows per week (depends on blocked items)

Indexes ensure fast lookups. No performance degradation expected.

---

## Future Enhancements (Post-Migration)

With these tables in place, future features could include:

1. **Engagement Leaderboard** - `/engagement leaderboard`
2. **Historical Sprint Reports** - `/sprint history`
3. **Burndown Charts** - Visual progress tracking
4. **Auto-reassignment** - Rotate gaps if partner doesn't respond
5. **Quality Scoring** - Track helpful vs. unhelpful answers
6. **Goal Setting** - Set sprint goals at cycle start
7. **Escalation Dashboard** - Admin view of all escalations

---

## Questions?

- **Why weekly cycles?** - Natural rhythm for team coordination
- **Why track probing questions?** - Escalate unresponsive partners after 3 attempts
- **Why link actions to gaps?** - Close the loop when work completes
- **Why escalation audit trail?** - Accountability and historical analysis

---

**Migration Version:** 003
**Created:** 2026-01-01
**Status:** ✅ Ready to apply
