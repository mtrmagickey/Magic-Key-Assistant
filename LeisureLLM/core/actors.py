"""Shared actor context helpers for web, Discord, and system paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ROLE_ORDER = {
    "member": 1,
    "manager": 2,
    "admin": 3,
}


def normalize_role(role: Optional[str]) -> str:
    normalized = str(role or "member").strip().lower()
    if normalized not in ROLE_ORDER:
        raise ValueError(f"Unsupported role: {role}")
    return normalized


def role_meets_requirement(role: Optional[str], minimum_role: str) -> bool:
    current = ROLE_ORDER.get(normalize_role(role), 0)
    required = ROLE_ORDER.get(normalize_role(minimum_role), 0)
    return current >= required


@dataclass(frozen=True)
class ActorContext:
    actor_id: int
    stable_id: str
    actor_kind: str
    external_ref: str
    display_name: Optional[str] = None
    role: Optional[str] = None
    account_id: Optional[int] = None
    username: Optional[str] = None
    session_id: Optional[int] = None
    auth_source: str = "system"

    def has_role(self, minimum_role: str) -> bool:
        return role_meets_requirement(self.role, minimum_role)