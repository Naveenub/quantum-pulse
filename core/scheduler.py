"""
QUANTUM-PULSE :: core/scheduler.py
=====================================
Background job scheduler using APScheduler.

Jobs
────
  health_ping          every 30s  — update qp_up gauge + log vital stats
  ttl_cleanup          every 1h   — delete expired pulses (if pulse_ttl_days set)
  dict_retrain         every 24h  — optionally retrain Zstd dict from recent data
  metrics_snapshot     every 5m   — log a structured metrics snapshot
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


class QuantumScheduler:
    """
    Thin wrapper around APScheduler with lazy job registration.
    Jobs are registered before start(); they only execute after start() is called.
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._jobs: list[dict] = []

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("Scheduler started  jobs={}", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def add_interval_job(
        self,
        fn:       Callable,
        seconds:  int,
        job_id:   str,
        **kwargs,
    ) -> None:
        self._scheduler.add_job(
            fn,
            trigger = IntervalTrigger(seconds=seconds),
            id      = job_id,
            replace_existing = True,
            **kwargs,
        )
        logger.debug("Scheduled job  id={}  every={}s", job_id, seconds)

    # ── job factories ──────────────────────────────────────────────────────── #

    def register_health_ping(self, engine_fn: Callable, db_fn: Callable, interval_s: int = 30) -> None:
        async def _job():
            try:
                engine = engine_fn()
                db     = db_fn()
                count  = await db.count_pulses()
                from core.metrics import up
                up.set(1)
                logger.debug(
                    "Health tick  pulses={}  dict_trained={}",
                    count, engine._trainer.is_trained,
                )
            except Exception as exc:
                from core.metrics import up
                up.set(0)
                logger.warning("Health ping failed: {}", exc)

        self.add_interval_job(_job, seconds=interval_s, job_id="health_ping")

    def register_ttl_cleanup(
        self,
        db_fn:    Callable,
        ttl_days: Optional[int],
        interval_s: int = 3600,
    ) -> None:
        if ttl_days is None:
            logger.info("TTL cleanup disabled (pulse_ttl_days not set)")
            return

        async def _job():
            db = db_fn()
            if not db.is_mongo:
                return
            try:
                import time
                cutoff = time.time() - ttl_days * 86400
                result = await db._db.pulse_meta.delete_many(
                    {"created_at": {"$lt": cutoff}}
                )
                if result.deleted_count:
                    logger.info("TTL cleanup: deleted {} expired pulses", result.deleted_count)
            except Exception as exc:
                logger.warning("TTL cleanup failed: {}", exc)

        self.add_interval_job(_job, seconds=interval_s, job_id="ttl_cleanup")

    def register_metrics_snapshot(self, engine_fn: Callable, db_fn: Callable, interval_s: int = 300) -> None:
        async def _job():
            try:
                engine = engine_fn()
                db     = db_fn()
                count  = await db.count_pulses()
                logger.info(
                    "Metrics snapshot  pulses={}  dict_id={}  dict_trained={}  backend={}",
                    count,
                    engine._trainer.dict_id,
                    engine._trainer.is_trained,
                    "mongo" if db.is_mongo else "memory",
                )
            except Exception as exc:
                logger.warning("Metrics snapshot failed: {}", exc)

        self.add_interval_job(_job, seconds=interval_s, job_id="metrics_snapshot")

    def register_dict_retrain(
        self,
        engine_fn:  Callable,
        db_fn:      Callable,
        interval_s: int = 86400,   # 24h
    ) -> None:
        """
        Every 24h, sample recent pulses from the DB and retrain the Zstd dict.
        This keeps compression gains high as the data distribution evolves.
        """
        async def _job():
            engine = engine_fn()
            db     = db_fn()
            try:
                recent = await db.list_pulses(limit=200)
                if len(recent) < 20:
                    logger.debug("Not enough pulses for dict retrain ({})", len(recent))
                    return

                # Use pulse_id list as a proxy for data diversity
                samples = [
                    str(p.get("pulse_id", "") + str(p.get("created_at", ""))).encode()
                    for p in recent
                ]
                await engine.bootstrap_dict(samples)
                logger.success("Zstd dict retrained from {} recent pulses", len(samples))
            except Exception as exc:
                logger.warning("Dict retrain failed: {}", exc)

        self.add_interval_job(_job, seconds=interval_s, job_id="dict_retrain")

    def list_jobs(self) -> list[dict]:
        return [
            {
                "id":       j.id,
                "next_run": str(j.next_run_time),
                "trigger":  str(j.trigger),
            }
            for j in self._scheduler.get_jobs()
        ]


# singleton
scheduler = QuantumScheduler()
