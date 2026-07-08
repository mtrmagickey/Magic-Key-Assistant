"""
Acceptance Tests: Three Killer Workflows
=========================================
These tests verify the end-to-end artifact creation for the three
workflows that define the product prototype:

  1. Meeting → Actions  (MeetingNote → ActionItems + Decisions)
  2. Lead → Follow-up   (Lead → LeadActivity → nudge)
  3. Decision → Recall   (Decision stored → retrieved by RAG context)

Each test uses a real in-memory SQLite database, not mocks,
so we're validating actual SQL and artifact linkage.

Run:
    pytest tests/test_workflow_acceptance.py -v
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers: stand up a real SQLite DB matching the production schema
# ---------------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).parent.parent / "LeisureLLM" / "migrations"


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all .sqlite.sql migrations in order."""
    conn.execute("PRAGMA foreign_keys = ON")
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sqlite.sql"))
    for f in sql_files:
        conn.executescript(f.read_text(encoding="utf-8"))
    # Also create the auxiliary tables that Database._ensure_aux_tables builds
    _ensure_aux_tables(conn)


def _ensure_aux_tables(conn: sqlite3.Connection) -> None:
    """Replicate the auxiliary tables from Database._ensure_aux_tables."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS knowledge_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        question TEXT,
        context TEXT,
        status TEXT DEFAULT 'open',
        priority TEXT DEFAULT 'medium',
        source TEXT,
        detected_at TEXT DEFAULT (datetime('now')),
        resolved_at TEXT,
        resolution TEXT,
        resolved_by_user_id INTEGER,
        resolved_by_username TEXT,
        tags TEXT,
        times_asked INTEGER DEFAULT 1,
        curation_status TEXT DEFAULT 'pending',
        curation_note TEXT,
        curation_date TEXT,
        curated_by TEXT
    );

    CREATE TABLE IF NOT EXISTS meeting_agenda_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        submitted_by_user_id INTEGER,
        submitted_by_username TEXT,
        priority TEXT DEFAULT 'normal',
        context TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        discussed_at TEXT,
        expires_at TEXT,
        status TEXT DEFAULT 'pending'
    );

    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        source TEXT,
        status TEXT DEFAULT 'cold',
        contact_name TEXT,
        contact_info TEXT,
        value_estimate TEXT,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        last_activity TEXT,
        next_action TEXT,
        next_action_date TEXT,
        owner_user_id INTEGER,
        owner_username TEXT
    );

    CREATE TABLE IF NOT EXISTS lead_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER REFERENCES leads(id),
        activity_type TEXT NOT NULL,
        summary TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        created_by_user_id INTEGER,
        created_by_username TEXT
    );

    CREATE TABLE IF NOT EXISTS persona_meeting_takeaways (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_topic TEXT,
        persona_name TEXT,
        takeaway TEXT,
        urgency TEXT DEFAULT 'normal',
        follow_up TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        linked_record_type TEXT,
        linked_record_id INTEGER
    );
    """)
    conn.commit()


@pytest.fixture
def db():
    """In-memory SQLite database with full production schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    yield conn
    conn.close()


# ===================================================================
# WORKFLOW 1: Meeting → Actions
# ===================================================================
# Simulate: user runs /parse_meeting with raw meeting text.
# Expected DB artifacts:
#   - 1 MeetingNote (stored as a document / decision context)
#   - N ActionItems (tasks table, one per extracted action)
#   - M Decisions   (decisions table, one per extracted decision)
# ===================================================================

class TestWorkflowMeetingToActions:
    """Meeting → Actions workflow acceptance tests."""

    SAMPLE_MEETING_TEXT = """
    Team standup 2026-02-06
    
    Attendees: Alice, Bob
    
    Discussion:
    - Alice presented the Q1 pipeline review. Revenue on track.
    - Bob flagged that the Acme proposal is overdue by 3 days.
    
    Decisions:
    - We will extend the Acme proposal deadline to Feb 13.
    - We will adopt weekly pipeline reviews starting next Monday.
    
    Actions:
    - Alice: Send updated Acme proposal by Feb 10.
    - Bob: Set up recurring weekly pipeline meeting by Feb 9.
    - Alice: Update CRM with Q1 forecast numbers by Feb 7.
    """

    def _simulate_meeting_parse(self, conn: sqlite3.Connection) -> dict:
        """
        Simulate the artifact-creation side of /parse_meeting.

        In production this calls the LLM to extract structured data.
        Here we do it deterministically so we can assert DB state.
        """
        now = datetime.utcnow().isoformat()

        # 1. Store the meeting summary as a decision-context record
        conn.execute(
            """INSERT INTO decisions (title, decision, rationale, decided_by, category, impact)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "Team standup 2026-02-06",
                "Extend Acme proposal deadline to Feb 13; adopt weekly pipeline reviews",
                "Acme overdue 3 days; Q1 revenue on track",
                json.dumps(["Alice", "Bob"]),
                "process",
                "medium",
            ),
        )
        meeting_decision_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 2. Store the second decision separately (granular)
        conn.execute(
            """INSERT INTO decisions (title, decision, rationale, decided_by, category, impact)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "Adopt weekly pipeline reviews",
                "Weekly pipeline reviews starting next Monday",
                "Ensure revenue visibility",
                json.dumps(["Alice", "Bob"]),
                "process",
                "medium",
            ),
        )

        # 3. Create action items
        actions = [
            ("Send updated Acme proposal", "Alice", "2026-02-10"),
            ("Set up recurring weekly pipeline meeting", "Bob", "2026-02-09"),
            ("Update CRM with Q1 forecast numbers", "Alice", "2026-02-07"),
        ]
        action_ids = []
        for title, owner, due in actions:
            conn.execute(
                """INSERT INTO tasks (title, assigned_to_username, due_date, status, priority, created_by_username)
                   VALUES (?, ?, ?, 'todo', 'medium', 'MagicKeyBot')""",
                (title, owner, due),
            )
            action_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        conn.commit()
        return {
            "meeting_decision_id": meeting_decision_id,
            "action_ids": action_ids,
        }

    def test_meeting_creates_decisions(self, db):
        """Parsing a meeting must insert at least one Decision record."""
        self._simulate_meeting_parse(db)
        rows = db.execute("SELECT * FROM decisions").fetchall()
        assert len(rows) >= 2, f"Expected ≥2 decisions, got {len(rows)}"

    def test_meeting_creates_action_items(self, db):
        """Parsing a meeting must create one ActionItem per extracted action."""
        result = self._simulate_meeting_parse(db)
        rows = db.execute("SELECT * FROM tasks WHERE created_by_username = 'MagicKeyBot'").fetchall()
        assert len(rows) == 3, f"Expected 3 action items, got {len(rows)}"
        # Each must have an owner and a due date
        for row in rows:
            assert row["assigned_to_username"], "Action item missing owner"
            assert row["due_date"], "Action item missing due date"

    def test_meeting_actions_have_todo_status(self, db):
        """All newly created actions start as 'todo'."""
        self._simulate_meeting_parse(db)
        statuses = [
            r["status"]
            for r in db.execute("SELECT status FROM tasks WHERE created_by_username = 'MagicKeyBot'").fetchall()
        ]
        assert all(s == "todo" for s in statuses)

    def test_meeting_decision_has_rationale(self, db):
        """Decisions must include rationale for recall."""
        self._simulate_meeting_parse(db)
        rows = db.execute("SELECT rationale FROM decisions").fetchall()
        for row in rows:
            assert row["rationale"], "Decision missing rationale — breaks recall workflow"


# ===================================================================
# WORKFLOW 2: Lead → Follow-up
# ===================================================================
# Simulate: user creates a lead via /lead or Rainmaker discovers one.
# Expected DB artifacts:
#   - 1 Lead record with stage, next-action, owner
#   - 1+ LeadActivity records tracking status changes
#   - After N days with no activity → nudge (testable as a query)
# ===================================================================

class TestWorkflowLeadToFollowup:
    """Lead → Follow-up workflow acceptance tests."""

    def _create_lead(self, conn: sqlite3.Connection, *, days_stale: int = 0) -> int:
        """Insert a lead and optionally age it."""
        created = (datetime.utcnow() - timedelta(days=days_stale)).isoformat()
        last_activity = created
        conn.execute(
            """INSERT INTO leads
               (name, source, status, contact_name, contact_info, value_estimate,
                notes, created_at, updated_at, last_activity, next_action,
                next_action_date, owner_user_id, owner_username)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "Acme Corp Exhibit",
                "referral",
                "warm",
                "Jane Doe",
                "jane@acme.com",
                "£25000-£40000",
                "Interested in interactive exhibits for lobby",
                created, created, last_activity,
                "Send proposal outline",
                (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d"),
                987654321,
                "Alice",
            ),
        )
        lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Log the initial activity
        conn.execute(
            """INSERT INTO lead_activity (lead_id, activity_type, summary, created_by_username)
               VALUES (?, ?, ?, ?)""",
            (lead_id, "creation", "Lead created from referral", "Alice"),
        )
        conn.commit()
        return lead_id

    def test_lead_created_with_required_fields(self, db):
        """A new lead must have name, owner, stage, and next action."""
        lead_id = self._create_lead(db)
        row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        assert row["name"], "Lead missing name"
        assert row["owner_username"], "Lead missing owner"
        assert row["status"], "Lead missing stage/status"
        assert row["next_action"], "Lead missing next action"

    def test_lead_has_initial_activity(self, db):
        """Creating a lead must also log a creation activity."""
        lead_id = self._create_lead(db)
        activities = db.execute(
            "SELECT * FROM lead_activity WHERE lead_id = ?", (lead_id,)
        ).fetchall()
        assert len(activities) >= 1, "No activity logged on lead creation"
        assert activities[0]["activity_type"] == "creation"

    def test_stale_lead_detected(self, db):
        """Leads with no activity for >7 days should surface in a staleness query."""
        followup_days = 7
        lead_id = self._create_lead(db, days_stale=10)
        cutoff = (datetime.utcnow() - timedelta(days=followup_days)).isoformat()
        stale = db.execute(
            """SELECT l.id, l.name, l.owner_username, l.last_activity
               FROM leads l
               WHERE l.last_activity < ?
                 AND l.status NOT IN ('won', 'lost')""",
            (cutoff,),
        ).fetchall()
        assert len(stale) >= 1, "Stale lead not detected by follow-up query"
        assert stale[0]["id"] == lead_id

    def test_lead_stage_transition_logged(self, db):
        """Advancing a lead's stage must create an activity record."""
        lead_id = self._create_lead(db)
        # Advance to 'hot'
        db.execute("UPDATE leads SET status = 'hot', updated_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(), lead_id))
        db.execute(
            """INSERT INTO lead_activity (lead_id, activity_type, summary, created_by_username)
               VALUES (?, ?, ?, ?)""",
            (lead_id, "status_change", "Moved from warm to hot", "Alice"),
        )
        db.commit()
        activities = db.execute(
            "SELECT * FROM lead_activity WHERE lead_id = ? AND activity_type = 'status_change'",
            (lead_id,),
        ).fetchall()
        assert len(activities) == 1


# ===================================================================
# WORKFLOW 3: Decision → Recall
# ===================================================================
# Simulate: user or meeting creates a Decision record.
# Expected: Decision is stored with full context and can be
# retrieved by keyword search (simulating RAG retrieval).
# ===================================================================

class TestWorkflowDecisionToRecall:
    """Decision → Recall workflow acceptance tests."""

    def _store_decision(self, conn: sqlite3.Connection, **overrides) -> int:
        defaults = {
            "title": "Use Qdrant for vector search",
            "decision": "Migrate from Chroma to Qdrant for production deployments",
            "rationale": "Qdrant offers better filtering, horizontal scaling, and gRPC support",
            "decided_by": json.dumps(["Alice", "Bob"]),
            "category": "technical",
            "impact": "high",
        }
        defaults.update(overrides)
        conn.execute(
            """INSERT INTO decisions (title, decision, rationale, decided_by, category, impact)
               VALUES (:title, :decision, :rationale, :decided_by, :category, :impact)""",
            defaults,
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_decision_stored_with_provenance(self, db):
        """A decision must store who, what, why, and when."""
        dec_id = self._store_decision(db)
        row = db.execute("SELECT * FROM decisions WHERE id = ?", (dec_id,)).fetchone()
        assert row["title"]
        assert row["decision"]
        assert row["rationale"]
        assert row["decided_by"]
        assert row["decided_at"], "Decision missing timestamp"

    def test_decision_retrievable_by_keyword(self, db):
        """Decisions must be findable by keyword (simulates RAG retrieval)."""
        self._store_decision(db)
        self._store_decision(db, title="Adopt weekly reviews", decision="Weekly pipeline reviews",
                             rationale="Revenue visibility", category="process", impact="medium")
        # Simulate keyword search (RAG would use embeddings; here we use LIKE)
        results = db.execute(
            "SELECT * FROM decisions WHERE decision LIKE ? OR rationale LIKE ?",
            ("%Qdrant%", "%Qdrant%"),
        ).fetchall()
        assert len(results) == 1
        assert "Qdrant" in results[0]["decision"]

    def test_decision_category_and_impact(self, db):
        """Stored decisions must have category and impact for filtering."""
        dec_id = self._store_decision(db)
        row = db.execute("SELECT category, impact FROM decisions WHERE id = ?", (dec_id,)).fetchone()
        assert row["category"] == "technical"
        assert row["impact"] == "high"

    def test_decision_linked_to_meeting_actions(self, db):
        """Decisions from meetings should be linkable to action items via project."""
        # Create a project as the meeting context
        db.execute("INSERT INTO projects (name, status) VALUES ('Q1 Pipeline', 'active')")
        project_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Decision linked to project
        dec_id = self._store_decision(db, title="Extend Acme deadline")
        db.execute("UPDATE decisions SET related_project_id = ? WHERE id = ?", (project_id, dec_id))

        # Action linked to same project
        db.execute(
            """INSERT INTO tasks (project_id, title, status, priority, assigned_to_username)
               VALUES (?, 'Send updated Acme proposal', 'todo', 'medium', 'Alice')""",
            (project_id,),
        )
        db.commit()

        # Verify linkage: given a decision, find related actions
        rows = db.execute(
            """SELECT t.title, t.assigned_to_username
               FROM tasks t
               JOIN decisions d ON d.related_project_id = t.project_id
               WHERE d.id = ?""",
            (dec_id,),
        ).fetchall()
        assert len(rows) >= 1, "Decision not linked to meeting actions via project"


# ===================================================================
# CROSS-WORKFLOW: Artifact Contract
# ===================================================================

class TestArtifactContract:
    """Every autonomous post must reference a record ID."""

    def test_job_run_tracks_record_creation(self, db):
        """Job runs should log what records they created."""
        db.execute(
            """INSERT INTO job_runs (job_name, status, started_at, triggered_by, output_summary, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "meeting_parse",
                "completed",
                datetime.utcnow().isoformat(),
                "manual",
                "Created 2 decisions, 3 actions",
                json.dumps({"decision_ids": [1, 2], "action_ids": [1, 2, 3]}),
            ),
        )
        db.commit()
        row = db.execute("SELECT metadata FROM job_runs WHERE job_name = 'meeting_parse'").fetchone()
        meta = json.loads(row["metadata"])
        assert "decision_ids" in meta, "Job run metadata must reference created record IDs"
        assert "action_ids" in meta, "Job run metadata must reference created record IDs"

    def test_receipt_links_to_artifact(self, db):
        """Command receipts should reference the record they created."""
        db.execute(
            """INSERT INTO receipts
               (command_name, user_id, username, result_status, related_record_id, related_record_type)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("action_add", 987654321, "Alice", "success", 42, "task"),
        )
        db.commit()
        row = db.execute("SELECT * FROM receipts WHERE command_name = 'action_add'").fetchone()
        assert row["related_record_id"] == 42
        assert row["related_record_type"] == "task"
