"""
Database migration runner with version tracking.

Usage:
    from migrations.runner import MigrationRunner
    
    runner = MigrationRunner(db)
    await runner.run_pending_migrations()
"""

import logging
from pathlib import Path
from typing import List, Tuple

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent


class MigrationRunner:
    """Tracks and applies database migrations."""
    
    def __init__(self, connection: aiosqlite.Connection):
        self.conn = connection

    async def _schema_versions_exists(self) -> bool:
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_versions'"
        ) as cursor:
            return bool(await cursor.fetchone())

    async def _create_schema_versions_table(self):
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_versions (
                filename TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at TEXT DEFAULT (datetime('now')),
                checksum TEXT
            )
        """)
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schema_versions_version ON schema_versions(version, applied_at)"
        )

    async def _ensure_schema_versions_layout(self):
        async with self.conn.execute("PRAGMA table_info(schema_versions)") as cursor:
            columns = await cursor.fetchall()

        if not columns:
            await self._create_schema_versions_table()
            return

        has_filename_pk = any(row[1] == "filename" and row[5] == 1 for row in columns)
        has_version_col = any(row[1] == "version" for row in columns)
        if has_filename_pk and has_version_col:
            await self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_schema_versions_version ON schema_versions(version, applied_at)"
            )
            return

        await self.conn.execute("""
            CREATE TABLE schema_versions_new (
                filename TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at TEXT DEFAULT (datetime('now')),
                checksum TEXT
            )
        """)
        await self.conn.execute(
            "INSERT OR IGNORE INTO schema_versions_new (filename, version, applied_at, checksum) "
            "SELECT filename, version, applied_at, checksum FROM schema_versions"
        )
        await self.conn.execute("DROP TABLE schema_versions")
        await self.conn.execute("ALTER TABLE schema_versions_new RENAME TO schema_versions")
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schema_versions_version ON schema_versions(version, applied_at)"
        )
    
    async def ensure_schema_versions_table(self):
        """Create the schema_versions table if it doesn't exist."""
        if not await self._schema_versions_exists():
            await self._create_schema_versions_table()
        await self._ensure_schema_versions_layout()
        await self.conn.commit()
    
    async def get_applied_versions(self) -> List[int]:
        """Get list of already-applied migration versions."""
        try:
            async with self.conn.execute(
                "SELECT version FROM schema_versions ORDER BY version"
            ) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]
        except Exception as e:
            logger.warning("Could not read schema_versions (table may not exist): %s", e)
            return []

    async def get_applied_filenames(self) -> List[str]:
        """Get list of already-applied migration filenames."""
        try:
            async with self.conn.execute(
                "SELECT filename FROM schema_versions ORDER BY version, filename"
            ) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]
        except Exception as e:
            logger.warning("Could not read schema_versions filenames: %s", e)
            return []
    
    def discover_migrations(self) -> List[Tuple[int, Path]]:
        """
        Find all numbered migration files (.sqlite.sql and .py) and parse
        their version numbers.  Returns sorted list of (version, path) tuples.
        """
        migrations = []

        # SQL migrations: 001_name.sqlite.sql
        for file in MIGRATIONS_DIR.glob("*.sqlite.sql"):
            name = file.stem.replace(".sqlite", "")
            parts = name.split("_", 1)
            if parts[0].isdigit():
                version = int(parts[0])
                migrations.append((version, file))

        # Python migrations: 011_name.py (for complex DDL that needs logic)
        for file in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.py"):
            if file.name == "runner.py" or file.name.startswith("__"):
                continue
            parts = file.stem.split("_", 1)
            if parts[0].isdigit():
                version = int(parts[0])
                # Don't double-count if both .sql and .py exist for same version
                if not any(v == version for v, _ in migrations):
                    migrations.append((version, file))

        return sorted(migrations, key=lambda x: x[0])
    
    async def apply_migration(self, version: int, filepath: Path) -> bool:
        """Apply a single migration file (.sqlite.sql or .py)."""
        try:
            import hashlib

            content = filepath.read_text(encoding="utf-8")
            checksum = hashlib.md5(content.encode()).hexdigest()

            if filepath.suffix == ".py":
                await self._apply_python_migration(filepath)
            else:
                await self.conn.executescript(content)

            # Record it in schema_versions
            await self.conn.execute(
                "INSERT INTO schema_versions (filename, version, checksum) VALUES (?, ?, ?)",
                (filepath.name, version, checksum)
            )
            await self.conn.commit()

            logger.info(f"Applied migration {version}: {filepath.name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to apply migration {version} ({filepath.name}): {e}")
            # Attempt rollback
            try:
                await self.conn.rollback()
            except Exception as rb_err:
                logger.error("Rollback also failed for migration %s: %s", version, rb_err)
            return False
    
    async def _apply_python_migration(self, filepath: Path):
        """Run a Python migration that uses sync sqlite3.

        The migration module must expose a ``migrate()`` function (no args).
        We extract the database path from our async connection and pass it
        via the ``DB_PATH`` module attribute so the script targets the
        correct database.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(filepath.stem, filepath)
        module = importlib.util.module_from_spec(spec)

        # Point the script at the same database we are managing
        db_path = None
        async with self.conn.execute("PRAGMA database_list") as cursor:
            for row in await cursor.fetchall():
                if row[1] == "main":
                    db_path = row[2]
                    break
        if not db_path:
            raise RuntimeError(f"Could not resolve database path for Python migration {filepath.name}")

        spec.loader.exec_module(module)
        module.DB_PATH = db_path

        entry = getattr(module, "migrate", None) or getattr(module, "run_migration", None)
        if entry is None:
            raise RuntimeError(f"Python migration {filepath.name} has no migrate() or run_migration() function")
        entry()

    async def run_pending_migrations(self) -> Tuple[int, int]:
        """
        Run all pending migrations.
        Returns (applied_count, failed_count).
        """
        await self.ensure_schema_versions_table()
        
        applied_filenames = set(await self.get_applied_filenames())
        all_migrations = self.discover_migrations()
        
        applied = 0
        failed = 0
        
        for version, filepath in all_migrations:
            if filepath.name in applied_filenames:
                logger.debug(f"Skipping already-applied migration {filepath.name}")
                continue
            
            logger.info(f"Applying migration {version}: {filepath.name}")
            if await self.apply_migration(version, filepath):
                applied += 1
                applied_filenames.add(filepath.name)
            else:
                failed += 1
                # Stop on first failure to prevent cascading issues
                break
        
        if applied > 0:
            logger.info(f"Migrations complete: {applied} applied, {failed} failed")
        else:
            logger.debug("No pending migrations")
        
        return applied, failed
    
    async def get_current_version(self) -> int:
        """Get the highest applied migration version."""
        try:
            async with self.conn.execute(
                "SELECT MAX(version) FROM schema_versions"
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row and row[0] else 0
        except Exception as e:
            logger.warning("Could not read max version from schema_versions: %s", e)
            return 0
