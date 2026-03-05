"""
QUANTUM-PULSE :: core/metrics.py
==================================
Prometheus metrics instrumentation.

Exposed at GET /metrics (Prometheus text format).
Collected metrics:

  qp_seals_total             counter   — successful seal operations
  qp_unseals_total           counter   — successful unseal operations
  qp_seal_errors_total       counter   — failed seal operations
  qp_compression_ratio       histogram — compression ratios achieved
  qp_seal_duration_ms        histogram — seal pipeline latency
  qp_unseal_duration_ms      histogram — unseal pipeline latency
  qp_pulse_bytes_original    histogram — original payload sizes
  qp_pulse_bytes_encrypted   histogram — encrypted blob sizes
  qp_active_mounts           gauge     — live virtual mounts
  qp_db_operations_total     counter   — MongoDB operations (label: op, backend)
  qp_db_errors_total         counter   — MongoDB errors (label: op)
  qp_key_rotations_total     counter   — key rotation operations
  qp_entropy_score           histogram — payload entropy measurements
  qp_up                      gauge     — service liveness (1 = up)
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ─────────────────────────────── label values ─────────────────────────────── #

_LATENCY_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)  # ms

_SIZE_BUCKETS = (
    1_024,
    4_096,
    16_384,
    65_536,
    262_144,
    1_048_576,
    4_194_304,
    16_777_216,
)  # bytes

_RATIO_BUCKETS = (1, 2, 5, 10, 20, 30, 50, 100, 200)


# ─────────────────────────────── metric definitions ───────────────────────── #

seals_total = Counter(
    "qp_seals_total",
    "Total successful seal operations",
    ["dict_trained"],
)

unseals_total = Counter(
    "qp_unseals_total",
    "Total successful unseal operations",
)

seal_errors_total = Counter(
    "qp_seal_errors_total",
    "Total failed seal operations",
    ["error_type"],
)

unseal_errors_total = Counter(
    "qp_unseal_errors_total",
    "Total failed unseal operations",
    ["error_type"],
)

compression_ratio = Histogram(
    "qp_compression_ratio",
    "Compression ratio achieved (original / encrypted)",
    buckets=_RATIO_BUCKETS,
)

seal_duration_ms = Histogram(
    "qp_seal_duration_ms",
    "Seal pipeline end-to-end latency in milliseconds",
    buckets=_LATENCY_BUCKETS,
)

unseal_duration_ms = Histogram(
    "qp_unseal_duration_ms",
    "Unseal pipeline end-to-end latency in milliseconds",
    buckets=_LATENCY_BUCKETS,
)

pulse_bytes_original = Histogram(
    "qp_pulse_bytes_original",
    "Original payload size in bytes",
    buckets=_SIZE_BUCKETS,
)

pulse_bytes_encrypted = Histogram(
    "qp_pulse_bytes_encrypted",
    "Encrypted blob size in bytes",
    buckets=_SIZE_BUCKETS,
)

active_mounts = Gauge(
    "qp_active_mounts",
    "Number of live virtual mounts",
)

db_operations_total = Counter(
    "qp_db_operations_total",
    "Total database operations",
    ["operation", "backend"],  # operation: save/load/delete, backend: mongo/gridfs/memory
)

db_errors_total = Counter(
    "qp_db_errors_total",
    "Total database errors",
    ["operation"],
)

key_rotations_total = Counter(
    "qp_key_rotations_total",
    "Total key rotation operations completed",
)

entropy_score = Histogram(
    "qp_entropy_score",
    "Shannon entropy of compressed payloads (bits/byte)",
    buckets=[0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 7.5, 7.9, 8.0],
)

master_pulses_total = Counter(
    "qp_master_pulses_total",
    "Total MasterPulse objects built",
)

shards_per_master = Histogram(
    "qp_shards_per_master",
    "Number of shards per MasterPulse",
    buckets=[1, 2, 5, 10, 25, 50, 100, 250, 500],
)

up = Gauge(
    "qp_up",
    "QUANTUM-PULSE service liveness (1 = healthy)",
)
up.set(1)

scan_files_total = Counter(
    "qp_scan_files_total",
    "Total files scanned across all scan operations",
)

scan_duration_ms = Histogram(
    "qp_scan_duration_ms",
    "Directory scan latency in milliseconds",
    buckets=_LATENCY_BUCKETS,
)


# ─────────────────────────────── context managers ─────────────────────────── #


@contextmanager
def track_seal(dict_trained: bool) -> Generator[None, None, None]:
    """Context manager that records seal success/failure and latency."""
    t0 = time.perf_counter()
    try:
        yield
        ms = (time.perf_counter() - t0) * 1_000
        seal_duration_ms.observe(ms)
        seals_total.labels(dict_trained=str(dict_trained)).inc()
    except Exception as exc:
        seal_errors_total.labels(error_type=type(exc).__name__).inc()
        raise


@contextmanager
def track_unseal() -> Generator[None, None, None]:
    t0 = time.perf_counter()
    try:
        yield
        unseal_duration_ms.observe((time.perf_counter() - t0) * 1_000)
        unseals_total.inc()
    except Exception as exc:
        unseal_errors_total.labels(error_type=type(exc).__name__).inc()
        raise


# ─────────────────────────────── FastAPI endpoint ─────────────────────────── #

metrics_router = APIRouter(tags=["observability"])


@metrics_router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus metrics scrape endpoint."""
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
        status_code=200,
    )
