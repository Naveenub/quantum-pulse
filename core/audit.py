"""
QUANTUM-PULSE :: core/audit.py
================================
Append-only audit log for every vault operation.

Each event is a JSON-L record written to:
  • Local file  (logs/audit.jsonl)  — always
  • MongoDB     (audit_log coll.)   — when DB is available

Fields per event
────────────────
  timestamp     ISO-8601 UTC
  event_type    seal | unseal | stream | rotate | delete | bootstrap | scan | auth_fail
  pulse_id      affected pulse (if applicable)
  identity      principal identity from auth layer
  request_id    HTTP request ID
  ip_address    client IP
  outcome       success | failure
  error         error message (on failure)
  meta          extra structured context (sizes, ratios, etc.)

Audit records are NEVER modified or deleted; this is enforced at the
MongoDB level via a capped collection (optional) or application logic.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger


class AuditEvent(StrEnum):
    SEAL = "seal"
    UNSEAL = "unseal"
    STREAM = "stream"
    ROTATE = "rotate"
    DELETE = "delete"
    BOOTSTRAP = "bootstrap"
    SCAN = "scan"
    MASTER = "master"
    AUTH_FAIL = "auth_fail"
    KEY_DERIVED = "key_derived"
    MOUNT_CREATE = "mount_create"
    MOUNT_DESTROY = "mount_destroy"
    FILE_ACCESS = "file_access"


@dataclass
class AuditRecord:
    event_type: str
    outcome: str  # "success" | "failure"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    pulse_id: str | None = None
    identity: str = "anon"
    request_id: str = ""
    ip_address: str = ""
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


class AuditLogger:
    """
    Append-only audit log.

    Thread-safe: file writes use line-atomic append mode.
    Non-blocking: failures are logged as warnings but never raise.
    """

    def __init__(self, log_file: str = "logs/audit.jsonl") -> None:
        self._log_file = Path(log_file)
        self._db: Any = None
        self._enabled = True
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info("AuditLogger initialised  file={}", log_file)

    def set_db(self, db: Any) -> None:
        """Wire in the async DB handle after startup."""
        self._db = db

    def disable(self) -> None:
        self._enabled = False

    # ── low-level emit ─────────────────────────────────────────────────────── #

    def emit_sync(self, record: AuditRecord) -> None:
        """Synchronous write — safe to call from sync contexts."""
        if not self._enabled:
            return
        try:
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        except Exception as exc:
            logger.warning("Audit file write failed: {}", exc)

    async def emit(self, record: AuditRecord) -> None:
        """Async write — file + optional MongoDB."""
        self.emit_sync(record)
        if self._db is not None and self._db.is_mongo:
            try:
                await self._db._db.audit_log.insert_one(
                    {**asdict(record), "_ttl_marker": time.time()}
                )
            except Exception as exc:
                logger.warning("Audit MongoDB write failed: {}", exc)

    # ── convenience builders ───────────────────────────────────────────────── #

    async def seal(
        self,
        *,
        pulse_id: str,
        identity: str = "anon",
        request_id: str = "",
        ip: str = "",
        ratio: float = 0.0,
        size_bytes: int = 0,
        error: str | None = None,
    ) -> None:
        await self.emit(
            AuditRecord(
                event_type=AuditEvent.SEAL,
                outcome="failure" if error else "success",
                pulse_id=pulse_id,
                identity=identity,
                request_id=request_id,
                ip_address=ip,
                error=error,
                meta={"ratio": ratio, "size_bytes": size_bytes},
            )
        )

    async def unseal(
        self,
        *,
        pulse_id: str,
        identity: str = "anon",
        request_id: str = "",
        ip: str = "",
        error: str | None = None,
    ) -> None:
        await self.emit(
            AuditRecord(
                event_type=AuditEvent.UNSEAL,
                outcome="failure" if error else "success",
                pulse_id=pulse_id,
                identity=identity,
                request_id=request_id,
                ip_address=ip,
                error=error,
            )
        )

    async def auth_fail(
        self,
        *,
        ip: str = "",
        request_id: str = "",
        reason: str = "",
    ) -> None:
        await self.emit(
            AuditRecord(
                event_type=AuditEvent.AUTH_FAIL,
                outcome="failure",
                identity="anon",
                request_id=request_id,
                ip_address=ip,
                error=reason,
            )
        )

    async def rotate(
        self,
        *,
        pulse_id: str,
        identity: str = "anon",
        request_id: str = "",
        error: str | None = None,
    ) -> None:
        await self.emit(
            AuditRecord(
                event_type=AuditEvent.ROTATE,
                outcome="failure" if error else "success",
                pulse_id=pulse_id,
                identity=identity,
                request_id=request_id,
                error=error,
            )
        )

    async def delete(
        self,
        *,
        pulse_id: str,
        identity: str = "anon",
        request_id: str = "",
    ) -> None:
        await self.emit(
            AuditRecord(
                event_type=AuditEvent.DELETE,
                outcome="success",
                pulse_id=pulse_id,
                identity=identity,
                request_id=request_id,
            )
        )

    async def file_access(
        self,
        *,
        pulse_id: str,
        virtual_path: str,
        identity: str = "anon",
        request_id: str = "",
        ip: str = "",
        cache_hit: bool = False,
    ) -> None:
        await self.emit(
            AuditRecord(
                event_type=AuditEvent.FILE_ACCESS,
                outcome="success",
                pulse_id=pulse_id,
                identity=identity,
                request_id=request_id,
                ip_address=ip,
                meta={"virtual_path": virtual_path, "cache_hit": cache_hit},
            )
        )

    # ── query helpers ──────────────────────────────────────────────────────── #

    async def query_recent(
        self,
        limit: int = 100,
        event_type: str | None = None,
        identity: str | None = None,
    ) -> list[dict]:
        """Read most recent records from MongoDB (if available) else file tail."""
        if self._db is not None and self._db.is_mongo:
            query: dict[str, Any] = {}
            if event_type:
                query["event_type"] = event_type
            if identity:
                query["identity"] = identity
            cursor = (
                self._db._db.audit_log.find(query, {"_id": 0, "_ttl_marker": 0})
                .sort("timestamp", -1)
                .limit(limit)
            )
            return await cursor.to_list(length=limit)

        # Fallback: read last N lines from file
        try:
            lines = self._log_file.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines[-limit:] if line]
            if event_type:
                records = [r for r in records if r.get("event_type") == event_type]
            if identity:
                records = [r for r in records if r.get("identity") == identity]
            return list(reversed(records))
        except Exception:
            return []


# ── singleton ──────────────────────────────────────────────────────────────── #
audit_logger = AuditLogger()
