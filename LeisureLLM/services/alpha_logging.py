"""Alpha logging helpers for end-to-end testing."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

_REDACT_PATTERNS = [
    # API keys
    (r"\b(sk-[A-Za-z0-9_-]{20,})", "[REDACTED_API_KEY]"),
    (r"\b(key-[A-Za-z0-9_-]{20,})", "[REDACTED_API_KEY]"),
    # GitHub tokens
    (r"\b(ghp_[A-Za-z0-9]{36,})", "[REDACTED_GH_TOKEN]"),
    # Slack tokens
    (r"\b(xox[bpras]-[A-Za-z0-9-]{10,})", "[REDACTED_SLACK_TOKEN]"),
    # Bearer tokens
    (r"(?i)(bearer\s+)[A-Za-z0-9_.~+/=-]{20,}", r"\1[REDACTED_BEARER]"),
    # Long hex tokens
    (r"\b([0-9a-f]{32,})\b", "[REDACTED_HEX_TOKEN]"),
    # Email addresses
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]"),
    # password = "..."
    (r"(?i)(password|passwd|secret|token|api_key|apikey)\s*[:=]\s*\S+", r"\1=[REDACTED]"),
]


def _redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for pat, repl in _REDACT_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


def _redact_obj(obj: Any) -> Any:
    if obj is None:
        return obj
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    return obj


def _get_log_path() -> Path:
    # Store alongside leisurellm.log (app root)
    root = Path(__file__).resolve().parents[1]
    return root / "alpha_session.log"


def log_alpha_event(event_type: str, payload: Dict[str, Any], *, redact: bool = True) -> None:
    """Append a JSONL alpha log entry."""
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "data": _redact_obj(payload) if redact else payload,
        }
        path = _get_log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        # Best-effort logging only
        pass
