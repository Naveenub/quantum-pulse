"""
QUANTUM-PULSE :: core/health.py
================================
Kubernetes-style deep health checks.

  GET /healthz/live    — liveness:   is the process alive and not deadlocked?
  GET /healthz/ready   — readiness:  can the service handle traffic?
                         (MongoDB reachable, engine initialised, disk space ok)
  GET /healthz/startup — startup:    has the service finished initialising?
  GET /healthz         — combined JSON report for human operators

Each check has a timeout and returns PASS | WARN | FAIL.
/healthz/ready returns HTTP 503 if any critical check FAILs.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Response, status


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    latency_ms: float = 0.0
    message: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status in (CheckStatus.PASS, CheckStatus.WARN)


@dataclass
class HealthReport:
    status: CheckStatus
    timestamp: float
    version: str
    environment: str
    checks: list[CheckResult] = field(default_factory=list)
    uptime_s: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ─────────────────────────────── check runners ───────────────────────────── #

_START_TIME = time.monotonic()
_startup_complete = False


def mark_startup_complete() -> None:
    global _startup_complete
    _startup_complete = True


async def _run_check(
    name: str,
    fn: Callable[[], Coroutine[Any, Any, CheckResult]],
    timeout: float = 5.0,
) -> CheckResult:
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(fn(), timeout=timeout)
        result.latency_ms = (time.perf_counter() - t0) * 1_000
        return result
    except TimeoutError:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            latency_ms=timeout * 1_000,
            message=f"Check timed out after {timeout}s",
        )
    except Exception as exc:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            latency_ms=(time.perf_counter() - t0) * 1_000,
            message=str(exc),
        )


# ─────────────────────────────── individual checks ───────────────────────── #


async def _check_engine(engine: Any) -> CheckResult:
    """Verify QuantumEngine can seal/unseal a test payload."""
    import uuid

    pid = str(uuid.uuid4())
    payload = {"_healthcheck": True}
    blob, meta = await engine.seal(payload, pulse_id=pid)
    recovered = await engine.unseal(blob, meta)
    assert recovered == payload
    return CheckResult(
        name="engine",
        status=CheckStatus.PASS,
        message=f"Seal/unseal OK  dict_id={engine._trainer.dict_id}",
        detail={
            "zstd_dict_trained": engine._trainer.is_trained,
            "zstd_level": 22,
        },
    )


async def _check_mongo(db: Any) -> CheckResult:
    if not db.is_mongo:
        return CheckResult(
            name="mongodb",
            status=CheckStatus.WARN,
            message="Running with in-process MemoryStore (MongoDB not connected)",
        )
    await db._client.admin.command("ping")
    count = await db.count_pulses()
    return CheckResult(
        name="mongodb",
        status=CheckStatus.PASS,
        message="Ping OK",
        detail={"pulse_count": count},
    )


async def _check_disk() -> CheckResult:
    """Check that the log directory has at least 100 MiB free."""
    try:
        stat = os.statvfs(".")
        free = stat.f_bavail * stat.f_frsize
        total = stat.f_blocks * stat.f_frsize
        pct = (1 - free / total) * 100 if total else 0
        status = (
            CheckStatus.FAIL
            if free < 100 * 1024 * 1024
            else CheckStatus.WARN
            if free < 500 * 1024 * 1024
            else CheckStatus.PASS
        )
        return CheckResult(
            name="disk",
            status=status,
            message=f"{free / 1024 / 1024:.0f} MiB free ({pct:.1f}% used)",
            detail={"free_mb": free // (1024 * 1024), "used_pct": round(pct, 1)},
        )
    except AttributeError:
        # Windows doesn't have statvfs
        return CheckResult(name="disk", status=CheckStatus.PASS, message="N/A on this OS")


async def _check_memory() -> CheckResult:
    """Basic memory pressure check via /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            lines = {ln.split(":")[0]: ln.split(":")[1].strip() for ln in f}
        total_kb = int(lines["MemTotal"].split()[0])
        available_kb = int(lines["MemAvailable"].split()[0])
        used_pct = (1 - available_kb / total_kb) * 100
        status = (
            CheckStatus.FAIL
            if used_pct > 95
            else CheckStatus.WARN
            if used_pct > 85
            else CheckStatus.PASS
        )
        return CheckResult(
            name="memory",
            status=status,
            message=f"{available_kb // 1024} MiB available ({used_pct:.1f}% used)",
            detail={"used_pct": round(used_pct, 1), "available_mb": available_kb // 1024},
        )
    except Exception:
        return CheckResult(name="memory", status=CheckStatus.PASS, message="N/A on this OS")


# ─────────────────────────────── health router ───────────────────────────── #


def create_health_router(engine_ref_fn: Callable, db_ref_fn: Callable) -> APIRouter:
    """
    Factory: creates the health router with closures over the engine + DB.
    engine_ref_fn / db_ref_fn are zero-arg callables that return the live objects.
    """
    from core.config import get_settings

    router = APIRouter(prefix="/healthz", tags=["health"])

    @router.get("/live", summary="Liveness probe")
    async def liveness() -> dict:
        """Always returns 200 while the event loop is alive."""
        return {"status": "alive", "uptime_s": round(time.monotonic() - _START_TIME, 1)}

    @router.get("/startup", summary="Startup probe")
    async def startup(resp: Response) -> dict:
        if not _startup_complete:
            resp.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "starting"}
        return {"status": "ready"}

    @router.get("/ready", summary="Readiness probe")
    async def readiness(resp: Response) -> dict:
        """
        Returns 200 only when ALL critical checks pass.
        Returns 503 if any critical check FAILS (warning is ok).
        """
        engine = engine_ref_fn()
        db = db_ref_fn()

        results = await asyncio.gather(
            _run_check("engine", lambda: _check_engine(engine)),
            _run_check("mongodb", lambda: _check_mongo(db)),
            _run_check("disk", _check_disk),
            _run_check("memory", _check_memory),
        )

        overall = (
            CheckStatus.FAIL
            if any(r.status == CheckStatus.FAIL for r in results)
            else CheckStatus.WARN
            if any(r.status == CheckStatus.WARN for r in results)
            else CheckStatus.PASS
        )

        if overall == CheckStatus.FAIL:
            resp.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return {
            "status": overall.value,
            "checks": [asdict(r) for r in results],
            "uptime_s": round(time.monotonic() - _START_TIME, 1),
        }

    @router.get("/", summary="Full health report")
    async def full_report() -> dict:
        """Detailed operator report combining all checks."""
        cfg = get_settings()
        engine = engine_ref_fn()
        db = db_ref_fn()

        results = await asyncio.gather(
            _run_check("engine", lambda: _check_engine(engine)),
            _run_check("mongodb", lambda: _check_mongo(db)),
            _run_check("disk", _check_disk),
            _run_check("memory", _check_memory),
        )

        overall = (
            CheckStatus.FAIL
            if any(r.status == CheckStatus.FAIL for r in results)
            else CheckStatus.WARN
            if any(r.status == CheckStatus.WARN for r in results)
            else CheckStatus.PASS
        )

        report = HealthReport(
            status=overall,
            timestamp=time.time(),
            version="1.0.0",
            environment=cfg.environment.value,
            checks=list(results),
            uptime_s=round(time.monotonic() - _START_TIME, 1),
        )
        return report.to_dict()

    return router
