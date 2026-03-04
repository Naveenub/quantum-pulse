"""
QUANTUM-PULSE :: core/retry.py
================================
Resilience patterns for database and external service calls.

  • Retry with exponential backoff + jitter (via tenacity)
  • Circuit breaker: open after N consecutive failures, half-open after cooldown
  • Bulkhead: semaphore-based concurrency limiter to protect the DB connection pool
  • Timeout wrapper for async operations
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

from loguru import logger
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
    after_log,
)
import logging

F = TypeVar("F", bound=Callable[..., Any])

# ─────────────────────────────── defaults ─────────────────────────────────── #

DB_MAX_ATTEMPTS   = 3
DB_WAIT_MIN_S     = 0.5
DB_WAIT_MAX_S     = 10.0
DB_WAIT_JITTER_S  = 1.0
DB_BULKHEAD_SIZE  = 50      # max concurrent DB ops
DEFAULT_TIMEOUT_S = 30.0

# ─────────────────────────────── retryable exceptions ─────────────────────── #

try:
    from pymongo.errors import (
        ConnectionFailure,
        NetworkTimeout,
        AutoReconnect,
        ServerSelectionTimeoutError,
    )
    _MONGO_ERRORS = (ConnectionFailure, NetworkTimeout, AutoReconnect, ServerSelectionTimeoutError)
except ImportError:
    _MONGO_ERRORS = (OSError,)

_RETRYABLE = (OSError, asyncio.TimeoutError) + _MONGO_ERRORS

# ─────────────────────────────── retry decorator ──────────────────────────── #

def with_retry(
    max_attempts: int = DB_MAX_ATTEMPTS,
    wait_min:     float = DB_WAIT_MIN_S,
    wait_max:     float = DB_WAIT_MAX_S,
    jitter:       float = DB_WAIT_JITTER_S,
    exceptions:   tuple = _RETRYABLE,
):
    """
    Decorator for async functions: retry with exponential backoff + jitter.

    Usage
    -----
    @with_retry(max_attempts=3)
    async def load_from_db(pulse_id: str) -> ...:
        ...
    """
    def decorator(fn: F) -> F:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            async for attempt in AsyncRetrying(
                stop        = stop_after_attempt(max_attempts),
                wait        = wait_exponential_jitter(
                    initial = wait_min,
                    max     = wait_max,
                    jitter  = jitter,
                ),
                retry       = retry_if_exception_type(exceptions),
                before_sleep= before_sleep_log(logger, logging.WARNING),  # type: ignore[arg-type]
                reraise     = True,
            ):
                with attempt:
                    return await fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


# ─────────────────────────────── circuit breaker ──────────────────────────── #

class CircuitState(str, Enum):
    CLOSED    = "closed"      # Normal — requests pass through
    OPEN      = "open"        # Failing — requests immediately rejected
    HALF_OPEN = "half_open"   # Probe — one request let through to test


class CircuitBreaker:
    """
    Simple async circuit breaker.

    Transitions:
      CLOSED  → OPEN       after *failure_threshold* consecutive failures
      OPEN    → HALF_OPEN  after *recovery_timeout* seconds
      HALF_OPEN→ CLOSED    on successful probe
      HALF_OPEN→ OPEN      on failed probe (resets timer)
    """

    def __init__(
        self,
        name:              str,
        failure_threshold: int   = 5,
        recovery_timeout:  float = 30.0,
    ) -> None:
        self.name              = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout  = recovery_timeout
        self._failures          = 0
        self._state             = CircuitState.CLOSED
        self._opened_at:  Optional[float] = None

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - (self._opened_at or 0) >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info("CircuitBreaker[{}] → HALF_OPEN", self.name)
        return self._state

    def _on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            logger.info("CircuitBreaker[{}] → CLOSED (probe succeeded)", self.name)
        self._failures = 0
        self._state    = CircuitState.CLOSED
        self._opened_at = None

    def _on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold or self._state == CircuitState.HALF_OPEN:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    "CircuitBreaker[{}] → OPEN after {} failures",
                    self.name, self._failures,
                )
            self._state     = CircuitState.OPEN
            self._opened_at = time.monotonic()

    async def call(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Execute *fn* through the circuit breaker."""
        if self.state == CircuitState.OPEN:
            raise RuntimeError(
                f"CircuitBreaker[{self.name}] is OPEN — "
                f"retrying in {self._recovery_timeout:.0f}s"
            )
        try:
            result = await fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise

    def status(self) -> dict:
        return {
            "name":      self.name,
            "state":     self.state.value,
            "failures":  self._failures,
            "threshold": self._failure_threshold,
        }


# ─────────────────────────────── bulkhead ────────────────────────────────── #

class Bulkhead:
    """
    Semaphore-based concurrency limiter.
    Prevents DB connection pool exhaustion under heavy load.
    """

    def __init__(self, name: str, max_concurrent: int = DB_BULKHEAD_SIZE) -> None:
        self.name     = name
        self._sem     = asyncio.Semaphore(max_concurrent)
        self._max     = max_concurrent
        self._active  = 0
        self._rejected = 0

    async def __aenter__(self):
        acquired = self._sem._value > 0
        if not acquired:
            self._rejected += 1
            logger.warning(
                "Bulkhead[{}] full ({} active / {} max) — request queued",
                self.name, self._active, self._max,
            )
        await self._sem.acquire()
        self._active += 1
        return self

    async def __aexit__(self, *_):
        self._active -= 1
        self._sem.release()

    def status(self) -> dict:
        return {
            "name":        self.name,
            "active":      self._active,
            "max":         self._max,
            "available":   self._sem._value,
            "rejected_total": self._rejected,
        }


# ─────────────────────────────── timeout ─────────────────────────────────── #

async def with_timeout(
    coro,
    timeout: float = DEFAULT_TIMEOUT_S,
    name:    str   = "operation",
) -> Any:
    """Run *coro* with a timeout; raises TimeoutError with a descriptive message."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError(
            f"{name} timed out after {timeout}s"
        )


# ─────────────────────────────── global instances ─────────────────────────── #

mongo_circuit   = CircuitBreaker("mongodb", failure_threshold=5, recovery_timeout=30.0)
db_bulkhead     = Bulkhead("db", max_concurrent=DB_BULKHEAD_SIZE)
crypto_bulkhead = Bulkhead("crypto", max_concurrent=100)
