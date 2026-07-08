"""Standalone runner for recovering stalled inbox processing threads."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from admin.routers.inbox import recover_stale_inbox_threads

logger = logging.getLogger("AdminServer")


def _run_key(now: Optional[datetime] = None) -> str:
    now = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class InboxRecoveryScheduler:
    """Background runner for web-only inbox stale-thread recovery."""

    job_name = "inbox_stalled_thread_sweep"

    def __init__(
        self,
        db: Any,
        *,
        interval_seconds: int = 600,
        stale_after_seconds: int = 900,
        batch_limit: int = 10,
    ):
        self.db = db
        self.interval_seconds = max(60, int(interval_seconds))
        self.stale_after_seconds = max(60, int(stale_after_seconds))
        self.batch_limit = max(1, int(batch_limit))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_once(self, *, triggered_by: str = "schedule") -> dict[str, Any]:
        run_key = _run_key()
        if hasattr(self.db, "record_job_run"):
            recorded = await self.db.record_job_run(self.job_name, run_key)
            if not recorded:
                return {"success": True, "skipped": True, "job_name": self.job_name, "run_key": run_key}
        try:
            result = await recover_stale_inbox_threads(
                self.db,
                stale_after_seconds=self.stale_after_seconds,
                limit=self.batch_limit,
                actor_ref=f"job:{self.job_name}:{triggered_by}:{run_key}",
            )
            if hasattr(self.db, "complete_job_run"):
                await self.db.complete_job_run(self.job_name, run_key)
            return {
                "success": True,
                "skipped": False,
                "job_name": self.job_name,
                "run_key": run_key,
                "triggered_by": triggered_by,
                "result": result,
            }
        except Exception as exc:
            if hasattr(self.db, "complete_job_run"):
                await self.db.complete_job_run(self.job_name, run_key, error=str(exc))
            raise

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once(triggered_by="schedule")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Inbox recovery scheduler run failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue