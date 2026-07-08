"""
Database module for Magic Key Assistant (SQLite)
Provides async SQLite connectivity and query helpers

Usage:
    from database import Database
    
    db = Database("assistant.db")
    await db.connect()
    
    async with db.acquire() as conn:
        async with conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)) as cursor:
            result = await cursor.fetchone()
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# SQLite connection/busy timeouts — kept in one place for easy tuning.
DB_CONNECT_TIMEOUT = 30.0        # seconds — aiosqlite connect timeout
DB_BUSY_TIMEOUT_MS = 30000       # milliseconds — PRAGMA busy_timeout


def _normalize_query_args(args: tuple[Any, ...]) -> Any:
    """Support either unpacked params or a single tuple/list/dict payload."""
    if len(args) != 1:
        return args
    value = args[0]
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, (tuple, dict)):
        return value
    return args


class Database:
    """Async SQLite database wrapper"""
    
    def __init__(self, database_path: str = "assistant.db"):
        self.database_path = Path(database_path)
        self.connection: Optional[aiosqlite.Connection] = None
        self._is_healthy = False
        logger.info(f"Database instance created: {self.database_path}")
    
    async def connect(self):
        """Initialize database connection and run pending migrations."""
        try:
            # Ensure parent directory exists
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            
            self.connection = await aiosqlite.connect(str(self.database_path), timeout=DB_CONNECT_TIMEOUT)
            self.connection.row_factory = aiosqlite.Row  # Return rows as dict-like objects
            
            # Enable foreign keys and WAL mode for concurrent reads
            await self.connection.execute("PRAGMA foreign_keys = ON")
            await self.connection.execute("PRAGMA journal_mode = WAL")
            await self.connection.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
            await self.connection.execute("PRAGMA synchronous = NORMAL")
            await self.connection.commit()

            # Run any pending migrations
            await self._run_migrations()

            # Best-effort: ensure newer auxiliary tables exist.
            # Some deployments may have been created before later migrations were applied.
            await self._ensure_aux_tables()
            
            self._is_healthy = True
            logger.info(f"Connected to SQLite database: {self.database_path}")
            
            # Test connection
            async with self.connection.execute("SELECT sqlite_version()") as cursor:
                version = await cursor.fetchone()
                logger.info(f"SQLite version: {version[0]}")

            async with self.connection.execute("PRAGMA journal_mode") as cursor:
                journal_mode = await cursor.fetchone()
            async with self.connection.execute("PRAGMA locking_mode") as cursor:
                locking_mode = await cursor.fetchone()
            async with self.connection.execute("PRAGMA busy_timeout") as cursor:
                busy_timeout = await cursor.fetchone()
            logger.info(
                "SQLite pragmas: journal_mode=%s locking_mode=%s busy_timeout_ms=%s",
                journal_mode[0] if journal_mode else "unknown",
                locking_mode[0] if locking_mode else "unknown",
                busy_timeout[0] if busy_timeout else "unknown",
            )
                
        except Exception as e:
            self._is_healthy = False
            logger.error(f"Failed to connect to database: {e}")
            raise

    async def _run_migrations(self):
        """Run pending database migrations."""
        if not self.connection:
            return
        try:
            from migrations.runner import MigrationRunner
            runner = MigrationRunner(self.connection)
            applied, failed = await runner.run_pending_migrations()
            if failed > 0:
                logger.warning(f"Some migrations failed: {failed}")
        except ImportError:
            logger.debug("Migration runner not available, skipping")
        except Exception as e:
            logger.warning(f"Migration runner failed: {e}")

    async def health_check(self) -> bool:
        """Check if database connection is healthy."""
        if not self.connection:
            return False
        try:
            async with self.connection.execute("SELECT 1") as cursor:
                await cursor.fetchone()
            self._is_healthy = True
            return True
        except Exception as e:
            logger.warning(f"Database health check failed: {e}")
            self._is_healthy = False
            return False

    async def ensure_connected(self):
        """Ensure database is connected, reconnecting if needed."""
        if not await self.health_check():
            logger.info("Database connection lost, reconnecting...")
            await self.connect()

    async def _ensure_aux_tables(self):
        """Backward-compat column fixups for older databases.

        All CREATE TABLE statements have been moved to migrations
        (001–026).  This method now only applies column-level patches
        for databases created before those migrations existed.
        """
        if not self.connection:
            return

        # ── Column additions for older DBs ───────────────────────────
        _col_fixes = [
            ("job_runs",       "run_date",       "TEXT"),
            ("pm_threads",     "run_date",       "TEXT DEFAULT ''"),
            ("open_questions", "author_user_id",  "INTEGER DEFAULT 0"),
            ("meeting_notes",  "author_user_id",  "INTEGER"),
            ("meeting_notes",  "title",           "TEXT DEFAULT ''"),
            ("pm_proposals",   "author_user_id",  "INTEGER"),
            ("knowledge_gaps", "curation_status",      "TEXT DEFAULT 'pending'"),
            ("knowledge_gaps", "curation_reason",       "TEXT"),
            ("knowledge_gaps", "curated_at",            "TEXT"),
            ("knowledge_gaps", "curated_by_username",   "TEXT"),
        ]
        for _tbl, _col, _type in _col_fixes:
            try:
                async with self.connection.execute(
                    f"PRAGMA table_info({_tbl})"
                ) as cur:
                    cols = {r[1] for r in (await cur.fetchall() or [])}
                if cols and _col not in cols:
                    await self.connection.execute(
                        f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_type}"
                    )
                    logger.info("Added missing column %s.%s", _tbl, _col)
            except Exception:
                pass  # table doesn't exist yet – migrations will create it

        # ── Fix legacy source_links schema (pre-migration 004) ───────
        try:
            async with self.connection.execute(
                "PRAGMA table_info(source_links)"
            ) as cur:
                sl_cols = {r[1] for r in (await cur.fetchall() or [])}
            if sl_cols and "artifact_type" not in sl_cols and "record_type" in sl_cols:
                await self.connection.execute(
                    "ALTER TABLE source_links RENAME TO source_links_legacy"
                )
                logger.info("Renamed legacy source_links table (old schema)")
        except Exception as e:
            logger.warning("_ensure_aux_tables: suppressed %s", e)

        # ── Add retrieval feedback columns if missing ────────────────
        try:
            async with self.connection.execute("PRAGMA table_info(request_traces)") as cursor:
                rt_cols = {r[1] for r in (await cursor.fetchall() or [])}
            if rt_cols and "policy_reason" not in rt_cols:
                await self.connection.execute(
                    "ALTER TABLE request_traces ADD COLUMN policy_reason TEXT"
                )
            if rt_cols and "routing_flags_json" not in rt_cols:
                await self.connection.execute(
                    "ALTER TABLE request_traces ADD COLUMN routing_flags_json TEXT"
                )
            async with self.connection.execute("PRAGMA table_info(response_feedback)") as cursor:
                rf_cols = {r[1] for r in (await cursor.fetchall() or [])}
            if rf_cols and "chunk_sources" not in rf_cols:
                await self.connection.execute(
                    "ALTER TABLE response_feedback ADD COLUMN chunk_sources TEXT"
                )
        except Exception as e:
            logger.warning("_ensure_aux_tables: column fixup failed: %s", e)

        try:
            await self.connection.commit()
        except Exception as e:
            logger.warning("_ensure_aux_tables: commit failed: %s", e)
    
    async def close(self):
        """Close database connection"""
        if self.connection:
            await self.connection.close()
            logger.info("Database connection closed")
    
    @asynccontextmanager
    async def acquire(self):
        """Get the database connection (for compatibility with pool-based code)"""
        if not self.connection:
            raise RuntimeError("Database not initialized. Call connect() first.")
        
        yield self.connection
    
    # Helper methods for common patterns
    async def execute(self, query: str, *args):
        """Execute a query with parameters"""
        params = _normalize_query_args(args)
        async with self.acquire() as conn:
            await conn.execute(query, params)
            await conn.commit()
    
    async def fetchone(self, query: str, *args):
        """Fetch single row"""
        params = _normalize_query_args(args)
        async with self.acquire() as conn, conn.execute(query, params) as cursor:
            return await cursor.fetchone()
    
    async def fetchall(self, query: str, *args):
        """Fetch all rows"""
        params = _normalize_query_args(args)
        async with self.acquire() as conn, conn.execute(query, params) as cursor:
            return await cursor.fetchall()
    
    async def fetchval(self, query: str, *args):
        """Fetch single value"""
        row = await self.fetchone(query, *args)
        return row[0] if row else None

    async def fetch_dicts(self, query: str, *args) -> list[dict]:
        """Fetch all rows as a list of dicts (column-name keyed)."""
        params = _normalize_query_args(args)
        async with self.acquire() as conn, conn.execute(query, params) as cur:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]

    async def fetch_one_dict(self, query: str, *args) -> dict | None:
        """Fetch a single row as a dict, or None."""
        params = _normalize_query_args(args)
        async with self.acquire() as conn, conn.execute(query, params) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    async def execute_insert(self, query: str, *args) -> int:
        """Execute an INSERT and return the lastrowid."""
        params = _normalize_query_args(args)
        async with self.acquire() as conn:
            async with conn.execute(query, params) as cur:
                row_id = cur.lastrowid
            await conn.commit()
            return row_id

    async def execute_update(self, query: str, *args) -> int:
        """Execute an UPDATE/DELETE, commit, and return rowcount."""
        params = _normalize_query_args(args)
        async with self.acquire() as conn:
            async with conn.execute(query, params) as cur:
                count = cur.rowcount
            await conn.commit()
            return count

    @asynccontextmanager
    async def transaction(self):
        """Context manager for multi-statement transactions.

        Usage::

            async with db.transaction() as conn:
                await conn.execute("INSERT ...", (...))
                await conn.execute("UPDATE ...", (...))
            # auto-commits on success, auto-rollbacks on exception
        """
        async with self.acquire() as conn:
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    # ========================================
    # Job Run Methods (Idempotency)
    # ========================================
    
    async def record_job_run(self, job_name: str, run_date: str) -> bool:
        """
        Record job start, return False if already ran today
        
        Args:
            job_name: Name of the scheduled job
            run_date: Date in YYYY-MM-DD format
            
        Returns:
            True if job recorded (first run), False if already exists
        """
        async with self.acquire() as conn:
            # Prefer idempotency by (job_name, run_date) to avoid parsing issues.
            # Some callers use hour keys like 'YYYY-MM-DD-HH' which are not SQLite datetime() parseable.
            run_key = (run_date or "").strip()

            # If the schema is old and lacks run_date, we fall back to date(started_at) checks.
            try:
                async with conn.execute(
                    """
                    SELECT id FROM job_runs
                    WHERE job_name = ?
                      AND run_date = ?
                      AND status IN ('running', 'completed')
                    LIMIT 1
                    """,
                    (job_name, run_key),
                ) as cursor:
                    existing = await cursor.fetchone()

                if existing:
                    logger.info(f"{job_name} already ran for key {run_key}")
                    return False

                await conn.execute(
                    """
                    INSERT INTO job_runs (job_name, run_date, started_at, status, triggered_by)
                    VALUES (?, ?, datetime('now'), 'running', 'schedule')
                    """,
                    (job_name, run_key),
                )
                await conn.commit()
                logger.info(f"Job run recorded: {job_name} key={run_key}")
                return True
            except Exception as e:
                logger.debug("run_date insert failed (%s), falling back to legacy schema", e)
                # Fallback: legacy behavior based on started_at date.
                async with conn.execute(
                    """
                    SELECT id FROM job_runs
                    WHERE job_name = ?
                      AND date(started_at) = date(?)
                      AND status IN ('running', 'completed')
                    LIMIT 1
                    """,
                    (job_name, run_key),
                ) as cursor:
                    existing = await cursor.fetchone()

                if existing:
                    logger.info(f"{job_name} already ran on {run_key}")
                    return False

                await conn.execute(
                    """
                    INSERT INTO job_runs (job_name, started_at, status, triggered_by)
                    VALUES (?, datetime('now'), 'running', 'schedule')
                    """,
                    (job_name,),
                )
                await conn.commit()
                logger.info(f"Job run recorded: {job_name} on {run_key}")
                return True
    
    async def complete_job_run(self, job_name: str, run_date: str, error: Optional[str] = None):
        """
        Mark job as completed or failed
        
        Args:
            job_name: Name of the job
            run_date: Date in YYYY-MM-DD format
            error: Error message if failed
        """
        async with self.acquire() as conn:
            status = 'failed' if error else 'completed'

            run_key = (run_date or "").strip()

            # Prefer selecting by run_date key; fallback to date(started_at)
            job = None
            try:
                async with conn.execute(
                    """
                    SELECT id, started_at FROM job_runs
                    WHERE job_name = ?
                      AND run_date = ?
                      AND status = 'running'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (job_name, run_key),
                ) as cursor:
                    job = await cursor.fetchone()
            except Exception as e:
                logger.warning("Job lookup by run_date failed for %s: %s", job_name, e)
                job = None

            if not job:
                async with conn.execute(
                    """
                    SELECT id, started_at FROM job_runs
                    WHERE job_name = ?
                      AND date(started_at) = date(?)
                      AND status = 'running'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (job_name, run_key),
                ) as cursor:
                    job = await cursor.fetchone()
            
            if not job:
                logger.warning(f"No running job found for {job_name} on {run_date}")
                return
            
            # Calculate duration
            # Note: If backfilling, this duration might be inaccurate relative to 'now',
            # but we calculate it relative to the stored started_at.
            try:
                started_at = datetime.fromisoformat(job['started_at'])
                # If started_at is naive, assume local/UTC consistency. 
                # If backfilling, we might want to use a fixed end time, but 'now' is the execution time.
                duration = (datetime.now() - started_at).total_seconds()
            except Exception as e:
                logger.warning("Could not parse started_at for %s: %s", job_name, e)
                duration = 0
            
            # Update job (duration_seconds is not present in all schemas)
            try:
                await conn.execute(
                    """
                    UPDATE job_runs
                    SET status = ?,
                        completed_at = datetime('now'),
                        duration_seconds = ?,
                        error_message = ?
                    WHERE id = ?
                    """,
                    (status, duration, error, job['id']),
                )
            except Exception as e:
                logger.debug("duration_seconds column update failed (%s), using legacy schema", e)
                await conn.execute(
                    """
                    UPDATE job_runs
                    SET status = ?,
                        completed_at = datetime('now'),
                        error_message = ?
                    WHERE id = ?
                    """,
                    (status, error, job['id']),
                )
            await conn.commit()
            
            logger.info(f"Job completed: {job_name} - {status} ({duration:.1f}s)")
    
    async def record_sync_result(self, stats: dict, triggered_by: str = "api"):
        """Record a document sync run with its stats in job_runs.

        This is a convenience wrapper for the sync endpoint — creates the
        start and completion in one call since sync is synchronous.
        """
        import json as _json
        async with self.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO job_runs
                       (job_name, run_date, started_at, completed_at, status,
                        triggered_by, result_json)
                       VALUES ('doc_sync', date('now'), datetime('now'),
                               datetime('now'), 'completed', ?, ?)""",
                    (triggered_by, _json.dumps(stats)),
                )
                await conn.commit()
            except Exception as e:
                logger.warning("Failed to record sync result: %s", e)

    async def get_chunk_feedback_outliers(self, min_appearances: int = 3):
        """Return chunks that appear disproportionately in negative feedback.

        Only surfaces sources that have appeared in at least `min_appearances`
        feedback records, preventing small-sample-size overreaction.

        Returns list of dicts: {source, neg_count, pos_count, total, neg_pct}
        """
        async with self.acquire() as conn:
            try:
                async with conn.execute(
                    """SELECT
                        cs.value AS source,
                        SUM(CASE WHEN rf.feedback = 'not_helpful' THEN 1 ELSE 0 END) AS neg_count,
                        SUM(CASE WHEN rf.feedback = 'helpful' THEN 1 ELSE 0 END) AS pos_count,
                        COUNT(*) AS total_count,
                        ROUND(
                            100.0 * SUM(CASE WHEN rf.feedback = 'not_helpful' THEN 1 ELSE 0 END) / COUNT(*),
                            1
                        ) AS neg_pct
                    FROM response_feedback rf,
                         json_each(rf.chunk_sources) AS cs
                    WHERE rf.chunk_sources IS NOT NULL
                      AND rf.chunk_sources != '[]'
                    GROUP BY cs.value
                    HAVING COUNT(*) >= ?
                    ORDER BY neg_pct DESC, neg_count DESC
                    LIMIT 20""",
                    (min_appearances,),
                ) as cursor:
                    rows = await cursor.fetchall()
                return [
                    {"source": r[0], "neg_count": r[1], "pos_count": r[2],
                     "total": r[3], "neg_pct": r[4]}
                    for r in rows
                ]
            except Exception as e:
                logger.warning("Failed to query chunk feedback outliers: %s", e)
                return []

    # ========================================
    # Helper Methods for JSON Arrays
    # ========================================
    
    def json_append(self, existing_json: Optional[str], value: Any) -> str:
        """Append value to JSON array stored as text"""
        try:
            arr = json.loads(existing_json) if existing_json else []
            if value not in arr:
                arr.append(value)
            return json.dumps(arr)
        except (json.JSONDecodeError, TypeError):
            return json.dumps([value])
    
    def json_parse(self, json_text: Optional[str]) -> List:
        """Parse JSON array from text, return empty list if invalid"""
        try:
            return json.loads(json_text) if json_text else []
        except (json.JSONDecodeError, TypeError):
            return []
    
    # ========================================
    # Stats Methods (used by /db_status command)
    # ========================================
    
    async def get_pipeline_stats(self) -> Dict[str, int]:
        """Get counts of opportunities by status"""
        async with self.acquire() as conn:
            stats = {}
            async with conn.execute("""
                SELECT status, COUNT(*) as count
                FROM opportunities
                GROUP BY status
            """) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    stats[row['status']] = row['count']
            return stats
    
    async def get_task_stats(self) -> Dict[str, int]:
        """Get counts of tasks by status"""
        async with self.acquire() as conn:
            stats = {}
            async with conn.execute("""
                SELECT status, COUNT(*) as count
                FROM tasks
                GROUP BY status
            """) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    stats[row['status']] = row['count']
            return stats
    
    # ========================================
    # Opportunity Methods
    # ========================================
    
    async def create_opportunity(self, **kwargs) -> int:
        """Create new opportunity, return ID"""
        fields = []
        placeholders = []
        values = []
        
        for key, value in kwargs.items():
            fields.append(key)
            placeholders.append('?')
            values.append(value)
        
        query = f"""
            INSERT INTO opportunities ({', '.join(fields)})
            VALUES ({', '.join(placeholders)})
        """
        
        async with self.acquire() as conn:
            cursor = await conn.execute(query, values)
            await conn.commit()
            return cursor.lastrowid
    
    async def get_opportunity(self, opportunity_id: int) -> Optional[Dict]:
        """Get opportunity by ID"""
        row = await self.fetchone("""
            SELECT * FROM opportunities WHERE id = ?
        """, opportunity_id)
        return dict(row) if row else None
    
    async def update_opportunity(self, opportunity_id: int, **kwargs):
        """Update opportunity fields"""
        if not kwargs:
            return
        
        sets = [f"{key} = ?" for key in kwargs]
        values = list(kwargs.values())
        values.append(opportunity_id)
        
        query = f"""
            UPDATE opportunities
            SET {', '.join(sets)}, updated_at = datetime('now')
            WHERE id = ?
        """
        
        await self.execute(query, *values)
    
    # ========================================
    # Task Methods
    # ========================================
    
    async def create_task(self, **kwargs) -> int:
        """Create new task, return ID"""
        fields = []
        placeholders = []
        values = []
        
        for key, value in kwargs.items():
            fields.append(key)
            placeholders.append('?')
            values.append(value)
        
        query = f"""
            INSERT INTO tasks ({', '.join(fields)})
            VALUES ({', '.join(placeholders)})
        """
        
        async with self.acquire() as conn:
            cursor = await conn.execute(query, values)
            await conn.commit()
            return cursor.lastrowid
    
    async def get_tasks_by_status(self, status: str) -> List[Dict]:
        """Get all tasks with given status"""
        rows = await self.fetchall("""
            SELECT * FROM tasks 
            WHERE status = ?
            ORDER BY created_at DESC
        """, status)
        return [dict(row) for row in rows]
    
    # ========================================
    # Receipt Methods (Command Audit Trail)
    # ========================================
    
    async def create_receipt(self, **kwargs) -> int:
        """Create command execution receipt"""
        fields = []
        placeholders = []
        values = []
        
        for key, value in kwargs.items():
            fields.append(key)
            placeholders.append('?')
            # Convert dicts to JSON
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            values.append(value)
        
        query = f"""
            INSERT INTO receipts ({', '.join(fields)})
            VALUES ({', '.join(placeholders)})
        """
        
        async with self.acquire() as conn:
            cursor = await conn.execute(query, values)
            await conn.commit()
            return cursor.lastrowid


# Compatibility: Keep old PostgreSQL-style method signatures working
# but route to SQLite implementations
