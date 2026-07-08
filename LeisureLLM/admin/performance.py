"""Small admin-only performance helpers.

Provides a tiny in-process TTL cache plus rolling timing metrics for
UI performance diagnostics. The implementation is intentionally simple
and additive so routes can adopt it without changing behavior.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Callable

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}

_METRIC_LOCK = threading.Lock()
_METRICS: dict[str, dict[str, Any]] = defaultdict(
    lambda: {
        "count": 0,
        "last_ms": None,
        "avg_ms": 0.0,
        "max_ms": 0.0,
        "last_recorded_at": None,
    }
)


def record_timing(name: str, elapsed_ms: float) -> None:
    now = time.time()
    with _METRIC_LOCK:
        metric = _METRICS[name]
        metric["count"] += 1
        metric["last_ms"] = round(elapsed_ms, 2)
        metric["max_ms"] = round(max(metric["max_ms"], elapsed_ms), 2)
        count = metric["count"]
        metric["avg_ms"] = round(
            ((metric["avg_ms"] * (count - 1)) + elapsed_ms) / count,
            2,
        )
        metric["last_recorded_at"] = now


@contextmanager
def timed(name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        record_timing(name, (time.perf_counter() - started) * 1000.0)


def peek_cache(key: str) -> Any | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if entry["expires_at"] <= now:
            _CACHE.pop(key, None)
            return None
        return entry["value"]


def get_or_set_cache(key: str, ttl_seconds: float, loader: Callable[[], Any]) -> tuple[Any, bool]:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and entry["expires_at"] > now:
            return entry["value"], True

    value = loader()
    stored_at = time.monotonic()
    with _CACHE_LOCK:
        _CACHE[key] = {
            "value": value,
            "stored_at": stored_at,
            "expires_at": stored_at + ttl_seconds,
            "ttl_seconds": ttl_seconds,
        }
    return value, False


def invalidate_cache(*keys: str) -> None:
    with _CACHE_LOCK:
        for key in keys:
            _CACHE.pop(key, None)


def describe_cache(*keys: str) -> dict[str, dict[str, Any]]:
    now = time.monotonic()
    description: dict[str, dict[str, Any]] = {}
    with _CACHE_LOCK:
        for key in keys:
            entry = _CACHE.get(key)
            if not entry or entry["expires_at"] <= now:
                description[key] = {"present": False}
                continue
            description[key] = {
                "present": True,
                "age_seconds": round(now - entry["stored_at"], 2),
                "ttl_seconds": round(entry["ttl_seconds"], 2),
                "expires_in_seconds": round(entry["expires_at"] - now, 2),
            }
    return description


def get_metric(name: str) -> dict[str, Any]:
    with _METRIC_LOCK:
        metric = _METRICS.get(name)
        if not metric:
            return {
                "count": 0,
                "last_ms": None,
                "avg_ms": None,
                "max_ms": None,
                "last_recorded_at": None,
            }
        return dict(metric)


def snapshot_metrics(names: list[str]) -> dict[str, dict[str, Any]]:
    return {name: get_metric(name) for name in names}


def get_cached_ollama_status(ttl_seconds: float = 15.0) -> dict[str, Any]:
    def _load() -> dict[str, Any]:
        from services.system_tools import SystemTools

        with timed("ollama.status_probe"):
            return SystemTools.get_ollama_status()

    value, _ = get_or_set_cache("ollama_status", ttl_seconds, _load)
    return value