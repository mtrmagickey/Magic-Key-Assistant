"""
trust_controls — Noise-reduction guardrails for autonomous posts.

Provides:
- Quiet hours check (suppress posts outside operating window)
- Post-only-on-change check (suppress if nothing changed)
- Noise budget (cap posts per job per day)
- Audit logging to autonomous_posts table

Usage inside an autonomous job:

    from core.trust_controls import TrustGate
    gate = TrustGate(bot.db)

    verdict = await gate.should_post(
        job_name="daily_digest",
        changes_summary="3 new actions, 1 overdue",
        record_ids=["Action#12", "Action#45"],
    )
    if verdict.suppressed:
        logger.info(f"Post suppressed: {verdict.reason}")
        return

    # ... actually send the Discord message ...

    await gate.log_post(
        job_name="daily_digest",
        channel_id=channel.id,
        changes_summary="3 new actions, 1 overdue",
        record_ids=["Action#12", "Action#45"],
    )
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Defaults (overridden by workflows.yaml → trust_controls) ─
_DEFAULT_QUIET_START = 22  # 10 PM local
_DEFAULT_QUIET_END = 7    # 7 AM local
_DEFAULT_POSTS_PER_JOB_PER_DAY = 3
_DEFAULT_REQUIRE_CHANGE = True


@dataclass
class PostVerdict:
    """Result of a trust-gate check."""
    suppressed: bool
    reason: Optional[str] = None   # e.g. 'quiet_hours', 'no_change', 'noise_budget'


class TrustGate:
    """
    Centralised gatekeeper for autonomous bot posts.

    Reads config from workflows.yaml trust_controls section.
    Falls back to sensible defaults if config is absent.
    """

    def __init__(self, db, *, trust_config: Optional[Dict[str, Any]] = None):
        self.db = db
        cfg = trust_config or {}
        self.quiet_start: int = cfg.get("quiet_hours_start", _DEFAULT_QUIET_START)
        self.quiet_end: int = cfg.get("quiet_hours_end", _DEFAULT_QUIET_END)
        self.posts_per_job_per_day: int = cfg.get(
            "posts_per_job_per_day", _DEFAULT_POSTS_PER_JOB_PER_DAY
        )
        self.require_change: bool = cfg.get("require_change", _DEFAULT_REQUIRE_CHANGE)
        self.quiet_hours_enabled: bool = cfg.get("quiet_hours_enabled", True)

    # ── Public API ────────────────────────────────────────────

    async def should_post(
        self,
        job_name: str,
        *,
        changes_summary: Optional[str] = None,
        record_ids: Optional[List[str]] = None,
        channel_id: Optional[int] = None,
        force: bool = False,
        force_justification: Optional[str] = None,
    ) -> PostVerdict:
        """
        Determine whether an autonomous post should go through.

        Checks (in order):
        1. Quiet hours
        2. Change-required gate
        3. Noise budget
        """
        if force:
            logger.warning(
                "Trust gate force-override: job=%s justification=%s",
                job_name,
                force_justification or "<none provided>",
            )
            await self._log(
                job_name, channel_id, changes_summary, record_ids,
                suppressed=False, reason="force_override",
            )
            return PostVerdict(suppressed=False)

        # 1. Quiet hours
        if self.quiet_hours_enabled and self._in_quiet_hours():
            await self._log(
                job_name, channel_id, changes_summary, record_ids,
                suppressed=True, reason="quiet_hours",
            )
            return PostVerdict(suppressed=True, reason="quiet_hours")

        # 2. Change required
        if self.require_change and not changes_summary:
            await self._log(
                job_name, channel_id, changes_summary, record_ids,
                suppressed=True, reason="no_change",
            )
            return PostVerdict(suppressed=True, reason="no_change")

        # 3. Noise budget
        if await self._over_budget(job_name):
            await self._log(
                job_name, channel_id, changes_summary, record_ids,
                suppressed=True, reason="noise_budget",
            )
            return PostVerdict(suppressed=True, reason="noise_budget")

        return PostVerdict(suppressed=False)

    async def log_post(
        self,
        job_name: str,
        channel_id: Optional[int] = None,
        changes_summary: Optional[str] = None,
        record_ids: Optional[List[str]] = None,
    ) -> None:
        """Log a post that was actually sent (not suppressed)."""
        await self._log(
            job_name, channel_id, changes_summary, record_ids,
            suppressed=False, reason=None,
        )

    # ── Internal ──────────────────────────────────────────────

    def _in_quiet_hours(self) -> bool:
        """Check if current hour falls within quiet window."""
        now_hour = datetime.now().hour
        if self.quiet_start > self.quiet_end:
            # Wraps midnight (e.g. 22→7)
            return now_hour >= self.quiet_start or now_hour < self.quiet_end
        else:
            return self.quiet_start <= now_hour < self.quiet_end

    async def _over_budget(self, job_name: str) -> bool:
        """Check if job has exceeded its daily post budget.

        Fails closed: if the database is unavailable or the query errors,
        treat the job as over-budget (suppress) rather than allowing
        unchecked posts.
        """
        if not self.db or not self.db.connection:
            logger.warning("Noise-budget check cannot reach DB — suppressing (fail-closed)")
            return True  # Can't verify → suppress
        try:
            async with self.db.connection.execute(
                """SELECT COUNT(*) as c FROM autonomous_posts
                   WHERE job_name = ?
                     AND suppressed = 0
                     AND created_at >= date('now')""",
                (job_name,),
            ) as cursor:
                row = await cursor.fetchone()
                count = row["c"] if row else 0
                return count >= self.posts_per_job_per_day
        except Exception as exc:
            logger.warning("Noise-budget query failed — suppressing (fail-closed): %s", exc)
            return True  # Query error → suppress

    async def _log(
        self,
        job_name: str,
        channel_id: Optional[int],
        changes_summary: Optional[str],
        record_ids: Optional[List[str]],
        *,
        suppressed: bool,
        reason: Optional[str],
    ) -> None:
        """Write an audit row to autonomous_posts."""
        if not self.db or not self.db.connection:
            return
        try:
            await self.db.connection.execute(
                """INSERT INTO autonomous_posts
                   (job_name, channel_id, trigger_condition, record_ids_touched,
                    changes_summary, suppressed, suppression_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_name,
                    channel_id,
                    "scheduled",
                    json.dumps(record_ids) if record_ids else None,
                    changes_summary,
                    1 if suppressed else 0,
                    reason,
                ),
            )
            await self.db.connection.commit()
        except Exception as e:
            # Don't let audit failures break the bot
            logger.debug(f"Trust audit log failed: {e}")
