"""
QUANTUM-PULSE :: core/storage_s3.py
=====================================
AWS S3 storage backend for QUANTUM-PULSE.

Layout in S3
────────────
  {prefix}/blobs/{pulse_id}          — raw encrypted blob bytes
  {prefix}/meta/{pulse_id}.json      — PulseBlob metadata (JSON)
  {prefix}/masters/{master_id}.json  — MasterPulse metadata (JSON)

Dependencies
────────────
  pip install aioboto3          (async S3 via aioboto3)

Environment variables
────────────────────
  QUANTUM_STORAGE_BACKEND=s3
  QUANTUM_S3_BUCKET=my-bucket
  QUANTUM_S3_PREFIX=quantum-pulse       (optional, default "quantum-pulse")
  QUANTUM_S3_REGION=us-east-1          (optional)
  QUANTUM_S3_ENDPOINT_URL=...          (optional, for MinIO / LocalStack)

  Standard AWS credentials apply:
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  or instance profile / ECS task role
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from loguru import logger

from models.pulse_models import MasterPulse, PulseBlob

try:
    import aioboto3  # type: ignore

    AIOBOTO3_AVAILABLE = True
except ImportError:
    AIOBOTO3_AVAILABLE = False


class S3Store:
    """
    Async S3 backend implementing the same interface as _MemoryStore and PulseDB.

    Blobs are stored as raw bytes under blobs/{pulse_id}.
    Metadata is stored as JSON under meta/{pulse_id}.json.
    MasterPulse records are stored as JSON under masters/{master_id}.json.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "quantum-pulse",
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        if not AIOBOTO3_AVAILABLE:
            raise ImportError(
                "aioboto3 is required for S3 storage. Install it with: pip install aioboto3"
            )
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._region = region
        self._endpoint_url = endpoint_url
        self._session: Any = None
        logger.info(
            "S3Store configured  bucket={}  prefix={}  endpoint={}",
            bucket,
            prefix,
            endpoint_url or "aws",
        )

    async def connect(self) -> bool:
        """Validate S3 connectivity by checking bucket access."""
        try:
            session = aioboto3.Session()
            kwargs: dict = {}
            if self._region:
                kwargs["region_name"] = self._region
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url

            async with session.client("s3", **kwargs) as s3:
                await s3.head_bucket(Bucket=self._bucket)

            self._session = session
            logger.success("S3Store connected  bucket={}", self._bucket)
            return True
        except Exception as exc:
            logger.error("S3Store connection failed: {}", exc)
            raise

    def _blob_key(self, pulse_id: str) -> str:
        return f"{self._prefix}/blobs/{pulse_id}"

    def _meta_key(self, pulse_id: str) -> str:
        return f"{self._prefix}/meta/{pulse_id}.json"

    def _master_key(self, master_id: str) -> str:
        return f"{self._prefix}/masters/{master_id}.json"

    def _s3_client(self):
        """Return a context manager for an S3 client."""
        kwargs: dict = {}
        if self._region:
            kwargs["region_name"] = self._region
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        return self._session.client("s3", **kwargs)

    # ── pulse CRUD ────────────────────────────────────────────────────────── #

    async def save_pulse(self, pulse_id: str, blob: bytes, meta: PulseBlob) -> str:
        async with self._s3_client() as s3:
            # Store encrypted blob
            await s3.put_object(
                Bucket=self._bucket,
                Key=self._blob_key(pulse_id),
                Body=blob,
                ContentType="application/octet-stream",
                Metadata={
                    "pulse-id": pulse_id,
                    "encrypted": "true",
                    "algorithm": "AES-256-GCM",
                },
            )
            # Store metadata as JSON
            await s3.put_object(
                Bucket=self._bucket,
                Key=self._meta_key(pulse_id),
                Body=meta.model_dump_json().encode(),
                ContentType="application/json",
            )

        logger.debug("S3 saved  id={}…  blob={}B", pulse_id[:8], len(blob))
        return "s3"

    async def load_pulse(self, pulse_id: str) -> tuple[bytes, PulseBlob]:
        async with self._s3_client() as s3:
            try:
                blob_resp = await s3.get_object(Bucket=self._bucket, Key=self._blob_key(pulse_id))
                blob = await blob_resp["Body"].read()

                meta_resp = await s3.get_object(Bucket=self._bucket, Key=self._meta_key(pulse_id))
                meta_json = await meta_resp["Body"].read()
            except s3.exceptions.NoSuchKey:
                raise KeyError(f"Pulse {pulse_id!r} not found in S3") from None
            except Exception as exc:
                # ClientError with 404 code
                if hasattr(exc, "response") and exc.response.get("Error", {}).get("Code") in (
                    "404",
                    "NoSuchKey",
                ):
                    raise KeyError(f"Pulse {pulse_id!r} not found in S3") from exc
                raise

        meta = PulseBlob.model_validate_json(meta_json)
        logger.debug("S3 loaded  id={}…  blob={}B", pulse_id[:8], len(blob))
        return blob, meta

    async def update_pulse(self, pulse_id: str, blob: bytes, meta: PulseBlob) -> None:
        """Overwrite blob + metadata atomically (S3 puts are atomic per-object)."""
        await self.save_pulse(pulse_id, blob, meta)
        logger.debug("S3 updated  pulse={}…", pulse_id[:8])

    async def delete_pulse(self, pulse_id: str) -> bool:
        async with self._s3_client() as s3:
            # Check existence first
            try:
                await s3.head_object(Bucket=self._bucket, Key=self._blob_key(pulse_id))
            except Exception:
                return False

            await s3.delete_object(Bucket=self._bucket, Key=self._blob_key(pulse_id))
            await s3.delete_object(Bucket=self._bucket, Key=self._meta_key(pulse_id))

        logger.debug("S3 deleted  pulse={}…", pulse_id[:8])
        return True

    async def list_pulses(
        self,
        parent_id: str | None = None,
        limit: int = 100,
        skip: int = 0,
    ) -> list[dict]:
        results = []
        async with self._s3_client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            prefix = f"{self._prefix}/meta/"

            async for page in paginator.paginate(
                Bucket=self._bucket,
                Prefix=prefix,
                PaginationConfig={"MaxItems": limit + skip},
            ):
                for obj in page.get("Contents", []):
                    meta_resp = await s3.get_object(Bucket=self._bucket, Key=obj["Key"])
                    meta_json = await meta_resp["Body"].read()
                    try:
                        doc = json.loads(meta_json)
                        if parent_id is None or doc.get("parent_id") == parent_id:
                            results.append(doc)
                    except Exception:
                        pass

        # Apply skip manually (S3 has no native skip)
        return results[skip : skip + limit]

    async def count_pulses(self) -> int:
        count = 0
        async with self._s3_client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=self._bucket, Prefix=f"{self._prefix}/meta/"
            ):
                count += len(page.get("Contents", []))
        return count

    # ── master pulse ──────────────────────────────────────────────────────── #

    async def save_master(self, master: MasterPulse) -> None:
        async with self._s3_client() as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=self._master_key(master.master_id),
                Body=master.model_dump_json().encode(),
                ContentType="application/json",
            )
        logger.debug("S3 saved master  id={}…", master.master_id[:8])

    async def load_master(self, master_id: str) -> MasterPulse:
        async with self._s3_client() as s3:
            try:
                resp = await s3.get_object(Bucket=self._bucket, Key=self._master_key(master_id))
                data = await resp["Body"].read()
            except Exception as exc:
                if hasattr(exc, "response") and exc.response.get("Error", {}).get("Code") in (
                    "404",
                    "NoSuchKey",
                ):
                    raise KeyError(f"MasterPulse {master_id!r} not found in S3") from exc
                raise
        return MasterPulse.model_validate_json(data)

    async def list_masters(self, limit: int = 50) -> list[dict]:
        results = []
        async with self._s3_client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=self._bucket,
                Prefix=f"{self._prefix}/masters/",
                PaginationConfig={"MaxItems": limit},
            ):
                for obj in page.get("Contents", []):
                    resp = await s3.get_object(Bucket=self._bucket, Key=obj["Key"])
                    data = await resp["Body"].read()
                    with contextlib.suppress(Exception):
                        results.append(json.loads(data))
        return results
