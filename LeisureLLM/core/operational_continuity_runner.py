"""Standalone runner for operational continuity sweeps.

This runner is intentionally independent from Discord task decorators so the
same continuity logic can execute in web-only mode.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.services.operational_continuity_service import OperationalContinuityService

logger = logging.getLogger("AdminServer")


def _run_key(now: Optional[datetime] = None) -> str:
    now = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class OperationalContinuityScheduler:
    """Background runner for web-only operational continuity sweeps."""

    job_name = "operational_continuity_sweep"

    def __init__(self, db: Any, *, interval_seconds: int = 900):
        self.db = db
        self.interval_seconds = max(60, int(interval_seconds))
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
            service = OperationalContinuityService(self.db)
            result = await service.run_sweep(source_context_id=f"job:{self.job_name}:{triggered_by}:{run_key}")
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
                logger.warning("Operational continuity scheduler run failed: %s", exc)
                # Persist failure so admin dashboard can surface it
                try:
                    if hasattr(self.db, "record_job_run"):
                        fail_key = _run_key()
                        await self.db.record_job_run(self.job_name, fail_key)
                        await self.db.complete_job_run(
                            self.job_name, fail_key, error=str(exc),
                        )
                except Exception as persist_exc:
                    logger.debug("Failed to persist scheduler error: %s", persist_exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue