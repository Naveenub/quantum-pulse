"""
QUANTUM-PULSE :: core/storage_gcs.py
======================================
Google Cloud Storage backend for QUANTUM-PULSE.

Layout in GCS
─────────────
  {prefix}/blobs/{pulse_id}          — raw encrypted blob bytes
  {prefix}/meta/{pulse_id}.json      — PulseBlob metadata (JSON)
  {prefix}/masters/{master_id}.json  — MasterPulse metadata (JSON)

Dependencies
────────────
  pip install gcloud-aio-storage     (async GCS)

Environment variables
────────────────────
  QUANTUM_STORAGE_BACKEND=gcs
  QUANTUM_GCS_BUCKET=my-bucket
  QUANTUM_GCS_PREFIX=quantum-pulse        (optional, default "quantum-pulse")
  GOOGLE_APPLICATION_CREDENTIALS=...     (path to service account JSON)
  or use Workload Identity / ADC
"""

from __future__ import annotations

import contextlib
import json

from loguru import logger

from models.pulse_models import MasterPulse, PulseBlob

# Always define Storage at module level so patch("core.storage_gcs.Storage") works
# even when gcloud-aio-storage is not installed.
Storage = None  # type: ignore
aiohttp = None  # type: ignore

try:
    import aiohttp  # type: ignore  # noqa: F811
    from gcloud.aio.storage import Storage  # type: ignore  # noqa: F811

    GCS_AVAILABLE = True
except ImportError:
    GCS_AVAILABLE = False


class GCSStore:
    """
    Async GCS backend implementing the same interface as S3Store and _MemoryStore.

    Uses gcloud-aio-storage for fully async GCS operations.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "quantum-pulse",
        service_file: str | None = None,
    ) -> None:
        if not GCS_AVAILABLE:
            raise ImportError(
                "gcloud-aio-storage is required for GCS storage. "
                "Install it with: pip install gcloud-aio-storage"
            )
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._service_file = service_file
        logger.info("GCSStore configured  bucket={}  prefix={}", bucket, prefix)

    async def connect(self) -> bool:
        """Validate GCS connectivity."""
        try:
            async with aiohttp.ClientSession() as session:
                kwargs = {}
                if self._service_file:
                    kwargs["service_file"] = self._service_file
                storage = Storage(session=session, **kwargs)
                # List up to 1 object to confirm access
                await storage.list_objects(self._bucket, params={"maxResults": 1})
            logger.success("GCSStore connected  bucket={}", self._bucket)
            return True
        except Exception as exc:
            logger.error("GCSStore connection failed: {}", exc)
            raise

    def _blob_key(self, pulse_id: str) -> str:
        return f"{self._prefix}/blobs/{pulse_id}"

    def _meta_key(self, pulse_id: str) -> str:
        return f"{self._prefix}/meta/{pulse_id}.json"

    def _master_key(self, master_id: str) -> str:
        return f"{self._prefix}/masters/{master_id}.json"

    def _storage_kwargs(self) -> dict:
        kwargs = {}
        if self._service_file:
            kwargs["service_file"] = self._service_file
        return kwargs

    # ── pulse CRUD ────────────────────────────────────────────────────────── #

    async def save_pulse(self, pulse_id: str, blob: bytes, meta: PulseBlob) -> str:
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            await storage.upload(
                self._bucket,
                self._blob_key(pulse_id),
                blob,
                content_type="application/octet-stream",
            )
            await storage.upload(
                self._bucket,
                self._meta_key(pulse_id),
                meta.model_dump_json().encode(),
                content_type="application/json",
            )
        logger.debug("GCS saved  id={}…  blob={}B", pulse_id[:8], len(blob))
        return "gcs"

    async def load_pulse(self, pulse_id: str) -> tuple[bytes, PulseBlob]:
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            try:
                blob = await storage.download(self._bucket, self._blob_key(pulse_id))
                meta_bytes = await storage.download(self._bucket, self._meta_key(pulse_id))
            except Exception as exc:
                if "404" in str(exc) or "does not exist" in str(exc).lower():
                    raise KeyError(f"Pulse {pulse_id!r} not found in GCS") from exc
                raise
        meta = PulseBlob.model_validate_json(meta_bytes)
        logger.debug("GCS loaded  id={}…  blob={}B", pulse_id[:8], len(blob))
        return blob, meta

    async def update_pulse(self, pulse_id: str, blob: bytes, meta: PulseBlob) -> None:
        await self.save_pulse(pulse_id, blob, meta)
        logger.debug("GCS updated  pulse={}…", pulse_id[:8])

    async def delete_pulse(self, pulse_id: str) -> bool:
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            try:
                await storage.delete(self._bucket, self._blob_key(pulse_id))
                await storage.delete(self._bucket, self._meta_key(pulse_id))
            except Exception as exc:
                if "404" in str(exc) or "does not exist" in str(exc).lower():
                    return False
                raise
        logger.debug("GCS deleted  pulse={}…", pulse_id[:8])
        return True

    async def list_pulses(
        self,
        parent_id: str | None = None,
        limit: int = 100,
        skip: int = 0,
    ) -> list[dict]:
        results = []
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            response = await storage.list_objects(
                self._bucket,
                params={"prefix": f"{self._prefix}/meta/", "maxResults": limit + skip},
            )
            for item in response.get("items", []):
                meta_bytes = await storage.download(self._bucket, item["name"])
                try:
                    doc = json.loads(meta_bytes)
                    if parent_id is None or doc.get("parent_id") == parent_id:
                        results.append(doc)
                except Exception:
                    pass
        return results[skip : skip + limit]

    async def count_pulses(self) -> int:
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            response = await storage.list_objects(
                self._bucket, params={"prefix": f"{self._prefix}/meta/"}
            )
        return len(response.get("items", []))

    # ── master pulse ──────────────────────────────────────────────────────── #

    async def save_master(self, master: MasterPulse) -> None:
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            await storage.upload(
                self._bucket,
                self._master_key(master.master_id),
                master.model_dump_json().encode(),
                content_type="application/json",
            )
        logger.debug("GCS saved master  id={}…", master.master_id[:8])

    async def load_master(self, master_id: str) -> MasterPulse:
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            try:
                data = await storage.download(self._bucket, self._master_key(master_id))
            except Exception as exc:
                if "404" in str(exc) or "does not exist" in str(exc).lower():
                    raise KeyError(f"MasterPulse {master_id!r} not found in GCS") from exc
                raise
        return MasterPulse.model_validate_json(data)

    async def list_masters(self, limit: int = 50) -> list[dict]:
        results = []
        async with aiohttp.ClientSession() as session:
            storage = Storage(session=session, **self._storage_kwargs())
            response = await storage.list_objects(
                self._bucket,
                params={"prefix": f"{self._prefix}/masters/", "maxResults": limit},
            )
            for item in response.get("items", []):
                data = await storage.download(self._bucket, item["name"])
                with contextlib.suppress(Exception):
                    results.append(json.loads(data))
        return results
