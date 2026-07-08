"""Shared utility functions for autonomous operations mixins.

These were extracted from AutonomousOps.py to avoid circular imports
when the mixin modules need them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import discord

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


# Reuse partner gating if available
try:
    from LeisureLLM.cogs.KnowledgeGapTracker import is_partner as _kg_is_partner

    def is_partner(interaction: discord.Interaction) -> bool:
        return _kg_is_partner(interaction)

except Exception:  # pragma: no cover

    def is_partner(interaction: discord.Interaction) -> bool:  # type: ignore
        return True


async def _is_owner_interaction(interaction: discord.Interaction) -> bool:
    try:
        return await interaction.client.is_owner(interaction.user)
    except Exception:
        return False


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _tags_json(tags: List[str]) -> str:
    try:
        return json.dumps(tags)
    except Exception:
        return "[]"


def _next_thursday_due(now_local: Optional[datetime] = None) -> str:
    now_local = now_local or datetime.now(EASTERN)
    d = now_local.date()
    delta = (3 - d.weekday()) % 7  # Thursday=3
    target = d + timedelta(days=delta or 7)
    return target.isoformat()


def _week_start_monday(now_local: Optional[datetime] = None) -> str:
    now_local = now_local or datetime.now(EASTERN)
    d = now_local.date()
    start = d - timedelta(days=d.weekday())
    return start.isoformat()
