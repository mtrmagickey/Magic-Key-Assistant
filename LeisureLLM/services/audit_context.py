"""Request-scoped audit metadata helpers."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Dict, Optional

_AUDIT_CONTEXT: ContextVar[Dict[str, Any]] = ContextVar("audit_context", default={})


def get_audit_context() -> Dict[str, Any]:
    return dict(_AUDIT_CONTEXT.get())


def set_audit_context(**values: Any) -> Token:
    context = get_audit_context()
    for key, value in values.items():
        if value is not None:
            context[key] = value
    return _AUDIT_CONTEXT.set(context)


def update_audit_context(**values: Any) -> Dict[str, Any]:
    context = get_audit_context()
    for key, value in values.items():
        if value is not None:
            context[key] = value
    _AUDIT_CONTEXT.set(context)
    return context


def clear_audit_context(token: Optional[Token]) -> None:
    if token is not None:
        _AUDIT_CONTEXT.reset(token)
