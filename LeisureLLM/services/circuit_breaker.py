"""
Circuit Breaker Pattern
========================

Protects the system against cascading failures from degraded external
services (LLM backends, Tavily, Ollama).  When a service fails
repeatedly, the circuit breaker "opens" and short-circuits requests
instead of waiting for timeouts.  After a cooldown period it enters
HALF_OPEN state in which a single probe request decides whether the
service has recovered.

States
------
- **CLOSED** — Normal operation.  Requests pass through.
- **OPEN** — Service is down.  Requests fail immediately with no network call.
- **HALF_OPEN** — Cooldown elapsed.  One probe request is allowed through.

Usage::

    breaker = CircuitBreakerRegistry.get_or_create("ollama", failure_threshold=3)

    if not breaker.allow_request():
        raise ServiceUnavailable("ollama circuit is open")

    try:
        result = await call_ollama()
        breaker.record_success()
    except Exception:
        breaker.record_failure()
        raise
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── State machine ─────────────────────────────────────────────────────────────

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ── Breaker ───────────────────────────────────────────────────────────────────

@dataclass
class CircuitBreaker:
    """Per-service circuit breaker with configurable thresholds."""

    name: str
    failure_threshold: int = 3        # consecutive failures to trip
    cooldown_seconds: float = 60.0    # wait before probing
    success_threshold: int = 2        # successes in half-open to close

    # ── Internal state (not constructor args) ──
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _failure_count: int = field(default=0, init=False, repr=False)
    _success_count: int = field(default=0, init=False, repr=False)
    _last_failure_time: float = field(default=0.0, init=False, repr=False)
    _total_trips: int = field(default=0, init=False, repr=False)
    _last_error: Optional[str] = field(default=None, init=False, repr=False)

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """Current state, with automatic OPEN → HALF_OPEN transition."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.info(
                    "Circuit breaker '%s': OPEN → HALF_OPEN (%.0fs cooldown elapsed)",
                    self.name, elapsed,
                )
        return self._state

    def allow_request(self) -> bool:
        """Should the caller attempt a request?"""
        current = self.state
        if current == CircuitState.CLOSED:
            return True
        if current == CircuitState.HALF_OPEN:
            return True  # allow exactly one probe
        return False  # OPEN → fail fast

    def record_success(self) -> None:
        """Record a successful request."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info(
                    "Circuit breaker '%s': HALF_OPEN → CLOSED (recovered after %d probes)",
                    self.name, self._success_count,
                )
        elif self._state == CircuitState.CLOSED:
            # Reset consecutive failure counter on any success
            self._failure_count = 0

    def record_failure(self, error: Optional[str] = None) -> None:
        """Record a failed request."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        self._last_error = error

        if self._state == CircuitState.HALF_OPEN:
            # Probe failed — back to OPEN
            self._state = CircuitState.OPEN
            self._total_trips += 1
            logger.warning(
                "Circuit breaker '%s': HALF_OPEN → OPEN (probe failed: %s)",
                self.name, error or "unknown",
            )
        elif (
            self._state == CircuitState.CLOSED
            and self._failure_count >= self.failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._total_trips += 1
            logger.warning(
                "Circuit breaker '%s': CLOSED → OPEN (%d consecutive failures, last: %s)",
                self.name, self._failure_count, error or "unknown",
            )

    def reset(self) -> None:
        """Manually reset to CLOSED."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_error = None

    def status(self) -> Dict[str, Any]:
        """Snapshot for health dashboards."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "total_trips": self._total_trips,
            "last_error": self._last_error,
            "cooldown_seconds": self.cooldown_seconds,
        }


# ── Registry (shared singleton) ──────────────────────────────────────────────

class CircuitBreakerRegistry:
    """Global registry of named circuit breakers.

    Thread-safe enough for asyncio (single-threaded event loop).
    """

    _breakers: Dict[str, CircuitBreaker] = {}

    @classmethod
    def get_or_create(
        cls,
        name: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        success_threshold: int = 2,
    ) -> CircuitBreaker:
        """Return existing breaker or create a new one with given params."""
        if name not in cls._breakers:
            cls._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                success_threshold=success_threshold,
            )
        return cls._breakers[name]

    @classmethod
    def get(cls, name: str) -> Optional[CircuitBreaker]:
        return cls._breakers.get(name)

    @classmethod
    def all_status(cls) -> Dict[str, Dict[str, Any]]:
        return {name: b.status() for name, b in cls._breakers.items()}

    @classmethod
    def reset_all(cls) -> None:
        for b in cls._breakers.values():
            b.reset()

    @classmethod
    def clear(cls) -> None:
        """Remove all breakers (for testing)."""
        cls._breakers.clear()
