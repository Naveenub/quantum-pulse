"""
QUANTUM-PULSE :: main.py  (Enterprise Edition)
================================================
FastAPI entry point with full enterprise wiring:
  • Pydantic Settings configuration
  • API key + JWT authentication on all vault endpoints
  • Rate limiting (slowapi)
  • Full middleware stack (CORS, security headers, request ID, timing, GZip)
  • Prometheus metrics
  • Deep health checks (liveness / readiness / startup)
  • APScheduler background jobs
  • Audit logging on every vault operation
  • RFC 7807 structured error responses
  • Circuit breaker + bulkhead on DB calls
  • Retry logic with exponential backoff
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import msgpack
import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from core.adaptive import adaptive_dict
from core.audit import audit_logger
from core.auth import Principal, auth_router, require_scope
from core.compression import PulseCompressor
from core.config import get_settings
from core.db import PulseDB
from core.engine import QuantumEngine
from core.health import create_health_router, mark_startup_complete
from core.interface import create_interface_router, mount_manager
from core.metrics import (
    compression_ratio,
    db_operations_total,
    entropy_score,
    key_rotations_total,
    master_pulses_total,
    metrics_router,
    pulse_bytes_encrypted,
    pulse_bytes_original,
    scan_duration_ms,
    scan_files_total,
    shards_per_master,
    track_seal,
    track_unseal,
)
from core.middleware import apply_middleware
from core.retry import db_bulkhead, mongo_circuit, with_retry
from core.scanner import QuantumScanner
from core.scheduler import scheduler
from core.vault import QuantumVault
from models.pulse_models import PulseBlob, ScanMode

# ─────────────────────────────── config + rate limiter ───────────────────── #

cfg = get_settings()
limiter = Limiter(key_func=get_remote_address, enabled=cfg.rate_limit_enabled)

# ─────────────────────────────── state ────────────────────────────────────── #


class _State:
    engine: QuantumEngine
    compressor: PulseCompressor
    vault: QuantumVault
    db: PulseDB


state = _State()


# ─────────────────────────────── shared helpers ───────────────────────────── #


@with_retry()
async def _load_blob(pulse_id: str) -> tuple[bytes, PulseBlob]:
    async with db_bulkhead:
        try:
            blob, meta = await mongo_circuit.call(state.db.load_pulse, pulse_id)
            db_operations_total.labels(
                operation="load", backend="mongo" if state.db.is_mongo else "memory"
            ).inc()
            return blob, meta
        except KeyError as err:
            raise HTTPException(404, f"Pulse {pulse_id!r} not found") from err


def _identity(request: Request) -> str:
    p = getattr(request.state, "principal", None)
    return p.identity if p else "anon"


def _req_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ─────────────────────────────── lifespan ─────────────────────────────────── #


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(cfg.log_dir, exist_ok=True)
    logger.remove()
    fmt = (
        '{{"time":"{time}","level":"{level}","msg":"{message}","file":"{file}:{line}"}}'
        if cfg.log_format == "json"
        else "<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}"
    )
    logger.add(
        f"{cfg.log_dir}/quantum_pulse.log",
        format=fmt,
        rotation=cfg.log_rotation,
        retention=cfg.log_retention,
        compression="zip",
        level=cfg.log_level.value,
        enqueue=True,
    )
    logger.add(lambda m: print(m, end=""), level=cfg.log_level.value, colorize=True)
    logger.info("━━━ QUANTUM-PULSE 1.0.0 ━━━  env={}", cfg.environment.value)

    state.db = PulseDB(cfg.mongo_uri, cfg.mongo_db)
    state.engine = QuantumEngine(
        passphrase=cfg.passphrase.get_secret_value(), adaptive_dict=adaptive_dict
    )
    state.compressor = PulseCompressor(state.engine._trainer)
    state.vault = QuantumVault(
        passphrase=cfg.passphrase.get_secret_value(), cache_ttl=cfg.key_cache_ttl_s
    )

    await state.db.connect()
    await state.vault.unlock()
    audit_logger.set_db(state.db)
    mount_manager.set_engine(state.engine)

    if cfg.scheduler_enabled:
        scheduler.register_health_ping(
            lambda: state.engine, lambda: state.db, cfg.health_check_interval_s
        )
        scheduler.register_ttl_cleanup(lambda: state.db, cfg.pulse_ttl_days)
        scheduler.register_metrics_snapshot(lambda: state.engine, lambda: state.db)
        scheduler.register_dict_retrain(
            lambda: state.engine, lambda: state.db, interval_s=cfg.dict_retrain_interval_s
        )
        scheduler.start()

    mark_startup_complete()
    logger.success("Ready  host={}  port={}", cfg.host, cfg.port)
    yield

    scheduler.stop()
    state.vault.lock()
    await state.db.disconnect()
    logger.info("Shutdown complete")


# ─────────────────────────────── app ──────────────────────────────────────── #

app = FastAPI(
    title="QUANTUM-PULSE",
    description="Extreme-density data vault engine for LLM training sets",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if not cfg.is_production else None,
    redoc_url="/redoc" if not cfg.is_production else None,
    openapi_url="/openapi.json" if not cfg.is_production else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
apply_middleware(app)

app.include_router(auth_router)
app.include_router(metrics_router)
app.include_router(create_health_router(lambda: state.engine, lambda: state.db))
app.include_router(create_interface_router(_load_blob))


# ─────────────────────────────── models ───────────────────────────────────── #


class SealRequest(BaseModel):
    payload: Any = Field(...)
    parent_id: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class UnsealRequest(BaseModel):
    pulse_id: str


class BootstrapRequest(BaseModel):
    samples: list[str] = Field(..., min_length=1)


class MasterBuildRequest(BaseModel):
    master_id: str | None = None
    pulse_ids: list[str] = Field(..., min_length=1)


class ScanRequest(BaseModel):
    root_path: str
    mode: ScanMode = ScanMode.RECURSIVE
    hash_contents: bool = True
    max_depth: int = -1
    skip_hidden: bool = True


class RotateRequest(BaseModel):
    old_passphrase: str


class ChangePassRequest(BaseModel):
    new_passphrase: str
    confirm: str


# ─────────────────────────────── /health (legacy) ────────────────────────── #


@app.get("/health", tags=["health"])
async def health_legacy() -> dict:
    return {
        "status": "ok",
        "timestamp": time.time(),
        "pulse_count": await state.db.count_pulses(),
        "engine": "Zstd-L22/AES-256-GCM/MsgPack",
        "dict_id": state.engine._trainer.dict_id,
        "mongo": state.db.is_mongo,
    }


# ─────────────────────────────── vault endpoints ──────────────────────────── #

_RL = f"{cfg.rate_limit_per_minute}/minute"


@app.post("/pulse/seal", tags=["vault"])
@limiter.limit(_RL)
async def seal(
    request: Request, req: SealRequest, p: Principal = Depends(require_scope("write"))
) -> dict:
    pulse_id = str(uuid.uuid4())
    try:
        with track_seal(dict_trained=state.engine._trainer.is_trained):
            blob, meta = await state.engine.seal(
                req.payload, pulse_id=pulse_id, parent_id=req.parent_id, tags=req.tags
            )
    except Exception as exc:
        await audit_logger.seal(
            pulse_id=pulse_id,
            identity=_identity(request),
            request_id=_req_id(request),
            ip=_ip(request),
            error=str(exc),
        )
        raise HTTPException(500, str(exc)) from exc

    async with db_bulkhead:
        backend = await mongo_circuit.call(state.db.save_pulse, pulse_id, blob, meta)

    compression_ratio.observe(meta.stats.ratio)
    pulse_bytes_original.observe(meta.stats.original_bytes)
    pulse_bytes_encrypted.observe(meta.stats.encrypted_bytes)
    entropy_score.observe(meta.stats.entropy_bits_per_byte)

    await audit_logger.seal(
        pulse_id=pulse_id,
        identity=_identity(request),
        request_id=_req_id(request),
        ip=_ip(request),
        ratio=meta.stats.ratio,
        size_bytes=meta.stats.encrypted_bytes,
    )
    return {"pulse_id": pulse_id, "meta": meta.model_dump(), "stored_in": backend}


@app.post("/pulse/seal/file", tags=["vault"])
@limiter.limit(_RL)
async def seal_file(
    request: Request, file: UploadFile = File(...), p: Principal = Depends(require_scope("write"))
) -> dict:
    raw = await file.read()
    pulse_id = str(uuid.uuid4())
    payload = {"filename": file.filename, "content_type": file.content_type, "data": list(raw)}
    try:
        with track_seal(dict_trained=state.engine._trainer.is_trained):
            blob, meta = await state.engine.seal(
                payload, pulse_id=pulse_id, tags={"filename": file.filename or ""}
            )
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    async with db_bulkhead:
        backend = await state.db.save_pulse(pulse_id, blob, meta)
    compression_ratio.observe(meta.stats.ratio)
    await audit_logger.seal(
        pulse_id=pulse_id,
        identity=_identity(request),
        request_id=_req_id(request),
        ip=_ip(request),
        ratio=meta.stats.ratio,
        size_bytes=len(raw),
    )
    return {"pulse_id": pulse_id, "meta": meta.model_dump(), "stored_in": backend}


@app.post("/pulse/unseal", tags=["vault"])
@limiter.limit(_RL)
async def unseal(
    request: Request, req: UnsealRequest, p: Principal = Depends(require_scope("read"))
) -> JSONResponse:
    blob, meta = await _load_blob(req.pulse_id)
    try:
        with track_unseal():
            payload = await state.engine.unseal(blob, meta)
    except Exception as exc:
        await audit_logger.unseal(
            pulse_id=req.pulse_id,
            identity=_identity(request),
            request_id=_req_id(request),
            ip=_ip(request),
            error=str(exc),
        )
        raise HTTPException(500, f"Decryption failed: {exc}") from exc
    await audit_logger.unseal(
        pulse_id=req.pulse_id,
        identity=_identity(request),
        request_id=_req_id(request),
        ip=_ip(request),
    )
    return JSONResponse({"pulse_id": req.pulse_id, "payload": payload})


@app.get("/pulse/stream/{pulse_id}", tags=["vault"])
async def stream_pulse(
    pulse_id: str,
    request: Request,
    chunk_size: int = Query(65_536, ge=4_096, le=1_048_576),
    p: Principal = Depends(require_scope("read")),
) -> StreamingResponse:
    blob, meta = await _load_blob(pulse_id)

    async def _gen():
        with track_unseal():
            payload = await state.engine.unseal(blob, meta)
        packed = msgpack.packb(payload, use_bin_type=True)
        for i in range(0, len(packed), chunk_size):
            yield packed[i : i + chunk_size]

    await audit_logger.unseal(
        pulse_id=pulse_id, identity=_identity(request), request_id=_req_id(request), ip=_ip(request)
    )
    return StreamingResponse(
        _gen(), media_type="application/x-msgpack", headers={"X-Pulse-ID": pulse_id}
    )


@app.get("/pulse/list", tags=["vault"])
async def list_pulses(
    parent_id: str | None = None,
    limit: int = Query(50, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    p: Principal = Depends(require_scope("read")),
) -> list[dict]:
    return await state.db.list_pulses(parent_id=parent_id, limit=limit, skip=skip)


@app.delete("/pulse/{pulse_id}", tags=["vault"])
async def delete_pulse(
    pulse_id: str, request: Request, p: Principal = Depends(require_scope("admin"))
) -> dict:
    if not await state.db.delete_pulse(pulse_id):
        raise HTTPException(404, f"Pulse {pulse_id!r} not found")
    await audit_logger.delete(
        pulse_id=pulse_id, identity=_identity(request), request_id=_req_id(request)
    )
    return {"deleted": pulse_id}


@app.post("/pulse/bootstrap", tags=["ops"])
async def bootstrap_dict(
    req: BootstrapRequest, p: Principal = Depends(require_scope("admin"))
) -> dict:
    try:
        await state.engine.bootstrap_dict([s.encode() for s in req.samples])
    except Exception as exc:
        # Zstd dict training can fail if corpus is too small
        raise HTTPException(400, f"Dict training failed: {exc}") from exc
    return {"status": "trained", "dict_id": state.engine._trainer.dict_id}


@app.post("/pulse/master", tags=["vault"])
async def build_master(
    req: MasterBuildRequest, p: Principal = Depends(require_scope("write"))
) -> dict:
    master_id = req.master_id or str(uuid.uuid4())
    sub = [(await _load_blob(pid)) for pid in req.pulse_ids]
    master = QuantumEngine.build_master_pulse(master_id, sub)
    await state.db.save_master(master)
    master_pulses_total.inc()
    shards_per_master.observe(master.total_shards)
    return master.model_dump()


@app.get("/pulse/master/{master_id}", tags=["vault"])
async def get_master(master_id: str, p: Principal = Depends(require_scope("read"))) -> dict:
    try:
        return (await state.db.load_master(master_id)).model_dump()
    except KeyError as err:
        raise HTTPException(404, f"MasterPulse {master_id!r} not found") from err


@app.post("/pulse/rotate/{pulse_id}", tags=["vault"])
async def rotate_shard(
    pulse_id: str,
    req: RotateRequest,
    request: Request,
    p: Principal = Depends(require_scope("admin")),
) -> dict:
    blob, meta = await _load_blob(pulse_id)
    new_vk = await state.vault.unlock()
    try:
        new_blob, new_meta = await state.vault.rotate_shard(blob, meta, req.old_passphrase, new_vk)
    except Exception as exc:
        await audit_logger.rotate(pulse_id=pulse_id, identity=_identity(request), error=str(exc))
        raise HTTPException(500, str(exc)) from exc
    async with db_bulkhead:
        await state.db.update_pulse(pulse_id, new_blob, new_meta)
    key_rotations_total.inc()
    await audit_logger.rotate(
        pulse_id=pulse_id, identity=_identity(request), request_id=_req_id(request)
    )
    return {"rotated": pulse_id, "new_merkle": new_meta.merkle_root[:16] + "…"}


@app.get("/vault/info", tags=["vault"])
async def vault_info(p: Principal = Depends(require_scope("admin"))) -> dict:
    vk = await state.vault.unlock()
    adaptive_stats = state.engine._adaptive.stats() if state.engine._adaptive else None
    return {
        "salt_prefix": vk.salt_hex[:16] + "…",
        "key_cache_size": len(state.vault._cache),
        "kdf": "PBKDF2-SHA256",
        "iterations": cfg.kdf_iterations,
        "key_bits": 256,
        "circuit_breaker": mongo_circuit.status(),
        "bulkhead": db_bulkhead.status(),
        "adaptive_dict": adaptive_stats,
    }


@app.get("/vault/adaptive", tags=["vault"])
async def adaptive_dict_stats(p: Principal = Depends(require_scope("read"))) -> dict:
    """
    Live adaptive dictionary stats.
    Shows current version, compression ratio, seals until next retrain,
    and full version history.
    """
    if state.engine._adaptive is None:
        return {"enabled": False}
    stats = state.engine._adaptive.stats()
    versions = [
        {
            "version": dv.version,
            "dict_id": dv.dict_id,
            "trained_at": dv.trained_at,
            "sample_count": dv.sample_count,
            "baseline_ratio": round(dv.baseline_ratio, 3),
            "size_kb": round(len(dv.raw_bytes) / 1024, 1),
        }
        for dv in state.engine._adaptive._versions
    ]
    return {
        "enabled": True,
        "current_version": stats["current_version"],
        "dict_id": stats["dict_id"],
        "is_trained": stats["is_trained"],
        "total_seals": stats["total_seals"],
        "seals_since_retrain": stats["seals_since_retrain"],
        "seals_until_retrain": stats["seals_until_retrain"],
        "buffer_size": stats["buffer_size"],
        "latest_ratio": stats["latest_ratio"],
        "retrain_every_n": stats["retrain_every_n"],
        "min_improvement_pct": stats["min_improvement_pct"],
        "version_history": versions,
    }


@app.post("/vault/passphrase", tags=["vault"])
async def change_passphrase(
    req: ChangePassRequest, p: Principal = Depends(require_scope("admin"))
) -> dict:
    await state.vault.change_passphrase(req.new_passphrase, req.confirm)
    return {"status": "passphrase_changed", "action_required": "rotate_all_shards"}


@app.post("/benchmark", tags=["ops"])
async def run_benchmark(
    req: BootstrapRequest, p: Principal = Depends(require_scope("admin"))
) -> dict:
    report = await state.compressor.benchmark([s.encode() for s in req.samples])
    return {
        "vanilla_ratio": round(report.vanilla_ratio, 3),
        "dict_ratio": round(report.dict_ratio, 3),
        "improvement_pct": round(report.improvement_pct, 2),
    }


@app.post("/scan", tags=["pipeline"])
async def scan_and_seal(req: ScanRequest, p: Principal = Depends(require_scope("write"))) -> dict:
    if not os.path.isdir(req.root_path):
        raise HTTPException(400, f"Not a directory: {req.root_path!r}")
    scanner = QuantumScanner(
        req.root_path,
        mode=req.mode,
        max_depth=req.max_depth,
        skip_hidden=req.skip_hidden,
        hash_contents=req.hash_contents,
    )
    master_id = str(uuid.uuid4())
    pairs: list[tuple[bytes, PulseBlob]] = []
    t0 = time.perf_counter()
    if samples := await scanner.scan_samples(limit=200):
        await state.engine.bootstrap_dict(samples)
    async for manifest in scanner.scan():
        pid = str(uuid.uuid4())
        blob, meta = await state.engine.seal(
            manifest.model_dump(),
            pulse_id=pid,
            parent_id=master_id,
            tags={"root": manifest.root_path},
        )
        async with db_bulkhead:
            await state.db.save_pulse(pid, blob, meta)
        pairs.append((blob, meta))
    if not pairs:
        return {"master_id": master_id, "shards": 0}
    master = QuantumEngine.build_master_pulse(master_id, pairs)
    await state.db.save_master(master)
    elapsed_ms = (time.perf_counter() - t0) * 1_000
    scan_duration_ms.observe(elapsed_ms)
    scan_files_total.inc(scanner.stats.total_files)
    return {
        "master_id": master_id,
        "shards": master.total_shards,
        "total_bytes": master.total_original_bytes,
        "merkle_root": master.merkle_root,
        "scan_stats": scanner.stats.model_dump(),
        "elapsed_ms": round(elapsed_ms, 1),
    }


@app.get("/audit/recent", tags=["ops"])
async def recent_audit(
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = None,
    p: Principal = Depends(require_scope("admin")),
) -> list[dict]:
    return await audit_logger.query_recent(limit=limit, event_type=event_type)


@app.get("/scheduler/jobs", tags=["ops"])
async def list_jobs(p: Principal = Depends(require_scope("admin"))) -> list[dict]:
    return scheduler.list_jobs()


# ─────────────────────────────── entry point ─────────────────────────────── #

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.value.lower(),
        loop="uvloop",
        http="httptools",
        workers=cfg.workers,
        reload=cfg.reload,
        access_log=False,
    )
