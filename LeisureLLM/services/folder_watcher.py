"""
Folder Watcher Service — auto-ingest documents when saved to watched directories.

This is a core **workflow integration moat**: the deeper the assistant embeds
into a user's local file system and daily habits, the higher the switching
cost to move to a competitor.

Capabilities:
    1. **Watched folders** — monitors one or more directories for new/changed files.
    2. **Auto-ingest** — new or modified documents are automatically chunked,
       enriched, and added to the knowledge base.
    3. **File type filtering** — configurable allowlist of extensions.
    4. **Debounced processing** — avoids re-processing files saved multiple
       times in rapid succession.
    5. **Audit trail** — every auto-ingested file is logged with timestamp
       and outcome.

Design:
    - Uses `watchdog` library for cross-platform FS events (falls back to
      polling on systems without inotify/kqueue/ReadDirectoryChangesW).
    - Runs as an asyncio background task alongside the bot.
    - All processing stays local — no cloud dependency.
    - Integrates with existing document ingestion pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────

DEFAULT_WATCH_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx",
    ".pptx", ".html", ".htm", ".rtf", ".odt", ".json", ".yaml", ".yml",
}

# Minimum seconds between re-processing the same file
DEBOUNCE_SECONDS = 5.0

# Maximum file size to auto-ingest (50 MB)
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS watched_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    extensions TEXT NOT NULL DEFAULT '[]',
    recursive INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_scan_at TEXT
);
"""

_CREATE_INGEST_LOG_SQL = """
CREATE TABLE IF NOT EXISTS auto_ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER REFERENCES watched_folders(id),
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER,
    file_modified_at TEXT,
    action TEXT NOT NULL CHECK (action IN ('ingested', 'updated', 'skipped', 'error')),
    detail TEXT,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_ingest_log_file
    ON auto_ingest_log(file_path, processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_log_folder
    ON auto_ingest_log(folder_id, processed_at DESC);
"""


class FolderWatcherService:
    """
    Monitors directories and auto-ingests new/changed documents.

    Usage::

        watcher = FolderWatcherService(db, ingest_fn=my_ingest_callback)
        await watcher.ensure_tables()

        # Add a folder to watch
        await watcher.add_folder("C:/Users/me/Documents/LeisureDocs")

        # Start watching (runs until cancelled)
        task = asyncio.create_task(watcher.run())

        # Later...
        await watcher.stop()
    """

    def __init__(
        self,
        db: Any,
        ingest_fn: Optional[Callable[[Path], Coroutine[Any, Any, bool]]] = None,
        poll_interval: float = 10.0,
    ):
        self.db = db
        self.ingest_fn = ingest_fn
        self.poll_interval = poll_interval
        self._tables_ensured = False
        self._running = False
        self._stop_event = asyncio.Event()
        self._last_processed: Dict[str, float] = {}  # path → timestamp
        self._file_mtimes: Dict[str, float] = {}     # path → last known mtime
        self._watchdog_observer = None

    async def ensure_tables(self) -> None:
        if self._tables_ensured:
            return
        try:
            async with self.db.acquire() as conn:
                await conn.executescript(
                    _CREATE_TABLE_SQL + _CREATE_INGEST_LOG_SQL + _CREATE_INDEX_SQL
                )
                await conn.commit()
            self._tables_ensured = True
        except Exception as exc:
            logger.warning("Failed to ensure watcher tables: %s", exc)

    # ── Folder management ──────────────────────────────────────

    async def add_folder(
        self,
        folder_path: str,
        *,
        recursive: bool = True,
        extensions: Optional[Set[str]] = None,
    ) -> Optional[int]:
        """Register a folder for watching. Returns the folder ID."""
        await self.ensure_tables()
        folder = Path(folder_path).resolve()
        if not folder.is_dir():
            logger.warning("Cannot watch non-existent directory: %s", folder)
            return None

        ext_list = sorted(extensions or DEFAULT_WATCH_EXTENSIONS)
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO watched_folders (folder_path, recursive, extensions)
                       VALUES (?, ?, ?)
                       ON CONFLICT(folder_path) DO UPDATE SET
                           enabled = 1,
                           recursive = excluded.recursive,
                           extensions = excluded.extensions""",
                    (str(folder), int(recursive), str(ext_list)),
                )
                await conn.commit()
                async with conn.execute(
                    "SELECT id FROM watched_folders WHERE folder_path = ?",
                    (str(folder),),
                ) as cur:
                    row = await cur.fetchone()
                return row[0] if row else None
        except Exception as exc:
            logger.warning("Failed to add watched folder: %s", exc)
            return None

    async def remove_folder(self, folder_path: str) -> bool:
        """Stop watching a folder."""
        await self.ensure_tables()
        folder = Path(folder_path).resolve()
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE watched_folders SET enabled = 0 WHERE folder_path = ?",
                    (str(folder),),
                )
                await conn.commit()
            return True
        except Exception as exc:
            logger.warning("Failed to remove watched folder: %s", exc)
            return False

    async def list_folders(self) -> List[Dict[str, Any]]:
        """List all watched folders."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn, conn.execute(
                "SELECT id, folder_path, enabled, extensions, recursive, added_at, last_scan_at "
                "FROM watched_folders ORDER BY added_at"
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to list watched folders: %s", exc)
            return []

    # ── Core watching loop ─────────────────────────────────────

    async def run(self) -> None:
        """
        Main watch loop.  Tries to use watchdog for native OS events;
        falls back to polling if watchdog is unavailable.
        """
        await self.ensure_tables()
        self._running = True
        self._stop_event.clear()

        logger.info("Folder watcher starting (poll_interval=%.1fs)", self.poll_interval)

        try:
            # Try native watchdog first
            if await self._try_start_watchdog():
                logger.info("Using native file-system events (watchdog)")
                # Even with watchdog, do periodic full scans to catch missed events
                while not self._stop_event.is_set():
                    await self._poll_scan()
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self.poll_interval * 6
                        )
                    except asyncio.TimeoutError as e:
                        logger.warning("run: suppressed %s", e)
            else:
                # Polling fallback
                logger.info("Using polling fallback (watchdog not available)")
                while not self._stop_event.is_set():
                    await self._poll_scan()
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self.poll_interval
                        )
                    except asyncio.TimeoutError as e:
                        logger.warning("operation: suppressed %s", e)
        finally:
            self._running = False
            await self._stop_watchdog()
            logger.info("Folder watcher stopped")

    async def stop(self) -> None:
        """Signal the watcher to stop."""
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Polling scan ───────────────────────────────────────────

    async def _poll_scan(self) -> int:
        """Scan all enabled folders for new/changed files. Returns count of processed files."""
        folders = await self.list_folders()
        processed = 0

        for folder_info in folders:
            if not folder_info.get("enabled", True):
                continue

            folder_path = Path(folder_info["folder_path"])
            if not folder_path.is_dir():
                continue

            recursive = bool(folder_info.get("recursive", True))
            try:
                extensions = set(eval(folder_info.get("extensions", "[]")))  # noqa: S307
            except Exception:
                extensions = DEFAULT_WATCH_EXTENSIONS

            folder_id = folder_info["id"]

            # Enumerate files
            pattern = "**/*" if recursive else "*"
            for file_path in folder_path.glob(pattern):
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() not in extensions:
                    continue
                if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                    continue

                # Check if file is new or changed
                mtime = file_path.stat().st_mtime
                str_path = str(file_path)
                old_mtime = self._file_mtimes.get(str_path, 0)

                if mtime <= old_mtime:
                    continue   # No change

                # Debounce
                last_proc = self._last_processed.get(str_path, 0)
                if time.time() - last_proc < DEBOUNCE_SECONDS:
                    continue

                # Process
                success = await self._process_file(file_path, folder_id)
                if success:
                    self._file_mtimes[str_path] = mtime
                    self._last_processed[str_path] = time.time()
                    processed += 1

        # Update last_scan_at
        for folder_info in folders:
            if folder_info.get("enabled"):
                try:
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            "UPDATE watched_folders SET last_scan_at = datetime('now') WHERE id = ?",
                            (folder_info["id"],),
                        )
                        await conn.commit()
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

        if processed:
            logger.info("Auto-ingested %d file(s) from watched folders", processed)
        return processed

    async def _process_file(self, file_path: Path, folder_id: int) -> bool:
        """Process a single file through the ingest pipeline."""
        try:
            if self.ingest_fn:
                success = await self.ingest_fn(file_path)
            else:
                # No ingest function configured — just log the detection
                logger.info("Detected new/changed file (no ingest_fn): %s", file_path)
                success = True

            action = "ingested" if success else "error"
            await self._log_ingest(folder_id, file_path, action)
            return success

        except Exception as exc:
            logger.warning("Failed to auto-ingest %s: %s", file_path, exc)
            await self._log_ingest(folder_id, file_path, "error", detail=str(exc))
            return False

    async def _log_ingest(
        self, folder_id: int, file_path: Path, action: str, detail: str = ""
    ) -> None:
        try:
            stat = file_path.stat()
            await self.db.execute(
                """INSERT INTO auto_ingest_log
                (folder_id, file_path, file_size_bytes, file_modified_at, action, detail)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                folder_id,
                str(file_path),
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime).isoformat(),
                action,
                detail or None,
                ),
                )
        except Exception as exc:
            logger.debug("Failed to log ingest event: %s", exc)

    # ── Watchdog integration (optional native FS events) ───────

    async def _try_start_watchdog(self) -> bool:
        """Try to start watchdog observer. Returns False if unavailable."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            class _Handler(FileSystemEventHandler):
                def __init__(self, watcher: FolderWatcherService):
                    self._watcher = watcher

                def on_created(self, event):
                    if not event.is_directory:
                        self._watcher._on_fs_event(event.src_path)

                def on_modified(self, event):
                    if not event.is_directory:
                        self._watcher._on_fs_event(event.src_path)

            observer = Observer()
            folders = await self.list_folders()
            handler = _Handler(self)

            for f in folders:
                if f.get("enabled") and Path(f["folder_path"]).is_dir():
                    observer.schedule(
                        handler,
                        f["folder_path"],
                        recursive=bool(f.get("recursive", True)),
                    )

            observer.start()
            self._watchdog_observer = observer
            return True

        except ImportError:
            logger.debug("watchdog not installed — using polling fallback")
            return False
        except Exception as exc:
            logger.warning("Failed to start watchdog: %s", exc)
            return False

    def _on_fs_event(self, path: str) -> None:
        """Handle a native FS event (called from watchdog thread)."""
        # Just update the mtime cache — the poll loop will pick it up
        try:
            p = Path(path)
            if p.is_file() and p.suffix.lower() in DEFAULT_WATCH_EXTENSIONS:
                self._file_mtimes[str(p)] = 0  # Force re-check on next poll
        except Exception as e:
            logger.warning("_on_fs_event: suppressed %s", e)

    async def _stop_watchdog(self) -> None:
        if self._watchdog_observer:
            try:
                self._watchdog_observer.stop()
                self._watchdog_observer.join(timeout=5)
            except Exception as e:
                logger.warning("_stop_watchdog: suppressed %s", e)
            self._watchdog_observer = None

    # ── Stats ──────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        """Get watcher statistics."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn:
                async with conn.execute(
                    "SELECT COUNT(*) FROM watched_folders WHERE enabled = 1"
                ) as cur:
                    folder_count = (await cur.fetchone())[0]

                async with conn.execute(
                    "SELECT action, COUNT(*) FROM auto_ingest_log GROUP BY action"
                ) as cur:
                    action_counts = {row[0]: row[1] for row in await cur.fetchall()}

                async with conn.execute(
                    "SELECT COUNT(*) FROM auto_ingest_log WHERE processed_at >= datetime('now', '-24 hours')"
                ) as cur:
                    last_24h = (await cur.fetchone())[0]

            return {
                "watched_folders": folder_count,
                "total_ingested": action_counts.get("ingested", 0),
                "total_updated": action_counts.get("updated", 0),
                "total_errors": action_counts.get("error", 0),
                "last_24h_processed": last_24h,
                "is_running": self._running,
            }
        except Exception as exc:
            logger.warning("Failed to get watcher stats: %s", exc)
            return {"error": str(exc)}

    async def get_recent_activity(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent auto-ingest activity."""
        await self.ensure_tables()
        try:
            async with self.db.acquire() as conn, conn.execute(
                """SELECT l.file_path, l.file_size_bytes, l.action, l.detail,
                              l.processed_at, w.folder_path
                       FROM auto_ingest_log l
                       LEFT JOIN watched_folders w ON l.folder_id = w.id
                       ORDER BY l.processed_at DESC LIMIT ?""",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to get recent activity: %s", exc)
            return []
