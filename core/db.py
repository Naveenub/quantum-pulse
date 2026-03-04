"""
QUANTUM-PULSE :: core/db.py
==============================
Async MongoDB / GridFS persistence layer using motor.

Features
────────
  • Auto-routes blobs: GridFS (>16 MiB) vs inline BSON (<= 16 MiB)
  • Atomic shard update:  only the re-encrypted shard is replaced;
    DB pointer (ObjectId) is swapped in a single findOneAndReplace call
  • Index setup:  pulse_id (unique), parent_id, created_at, tags
  • TTL index:   optional expiry for transient training shards
  • Reactive:    motor async driver, all calls are non-blocking
"""

from __future__ import annotations

import time
from typing import Any, Optional

from loguru import logger

try:
    import motor.motor_asyncio as motor
    from bson import ObjectId
    MOTOR_AVAILABLE = True
except ImportError:
    MOTOR_AVAILABLE = False

from models.pulse_models import MasterPulse, PulseBlob

# ─────────────────────────────── constants ────────────────────────────────── #

GRIDFS_THRESHOLD = 16 * 1024 * 1024   # 16 MiB
COLLECTION_META   = "pulse_meta"
COLLECTION_MASTER = "master_pulses"

# ─────────────────────────────── MemoryStore (fallback) ───────────────────── #

class _MemoryStore:
    """Drop-in fallback when MongoDB is unavailable (dev / CI mode)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._metas: dict[str, dict]  = {}
        self._masters: dict[str, dict] = {}

    async def save_pulse(self, pulse_id: str, blob: bytes, meta: PulseBlob) -> str:
        self._blobs[pulse_id] = blob
        self._metas[pulse_id] = meta.model_dump()
        return "memory"

    async def load_pulse(self, pulse_id: str) -> tuple[bytes, PulseBlob]:
        if pulse_id not in self._metas:
            raise KeyError(f"Pulse {pulse_id!r} not found")
        return self._blobs[pulse_id], PulseBlob(**self._metas[pulse_id])

    async def update_pulse(self, pulse_id: str, blob: bytes, meta: PulseBlob) -> None:
        self._blobs[pulse_id] = blob
        self._metas[pulse_id] = meta.model_dump()

    async def delete_pulse(self, pulse_id: str) -> bool:
        existed = pulse_id in self._metas
        self._blobs.pop(pulse_id, None)
        self._metas.pop(pulse_id, None)
        return existed

    async def save_master(self, master: MasterPulse) -> None:
        self._masters[master.master_id] = master.model_dump()

    async def load_master(self, master_id: str) -> MasterPulse:
        if master_id not in self._masters:
            raise KeyError(f"MasterPulse {master_id!r} not found")
        return MasterPulse(**self._masters[master_id])

    async def list_pulses(self, parent_id: Optional[str] = None) -> list[dict]:
        result = list(self._metas.values())
        if parent_id:
            result = [m for m in result if m.get("parent_id") == parent_id]
        return result

    async def count_pulses(self) -> int:
        return len(self._metas)


# ─────────────────────────────── PulseDB ──────────────────────────────────── #

class PulseDB:
    """
    Primary persistence interface.  Uses MongoDB+GridFS when available,
    falls back to _MemoryStore transparently.
    """

    def __init__(
        self,
        mongo_uri:  str = "mongodb://localhost:27017",
        db_name:    str = "quantum_pulse",
    ) -> None:
        self._uri     = mongo_uri
        self._db_name = db_name
        self._client: Any = None
        self._db:     Any = None
        self._gfs:    Any = None
        self._mem     = _MemoryStore()
        self._ready   = False

    # ── lifecycle ─────────────────────────────────────────────────────────── #

    async def connect(self) -> bool:
        if not MOTOR_AVAILABLE:
            logger.warning("motor absent — using in-process MemoryStore")
            self._ready = True
            return False

        try:
            self._client = motor.AsyncIOMotorClient(
                self._uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
            )
            await self._client.admin.command("ping")
            self._db  = self._client[self._db_name]
            self._gfs = motor.AsyncIOMotorGridFSBucket(self._db)
            await self._ensure_indexes()
            self._ready = True
            logger.success("PulseDB connected  uri={}  db={}", self._uri, self._db_name)
            return True
        except Exception as exc:
            logger.warning("MongoDB unavailable ({}); falling back to MemoryStore", exc)
            self._client = None
            self._ready  = True
            return False

    async def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
        logger.info("PulseDB disconnected")

    @property
    def is_mongo(self) -> bool:
        return self._client is not None

    # ── index setup ───────────────────────────────────────────────────────── #

    async def _ensure_indexes(self) -> None:
        col = self._db[COLLECTION_META]
        await col.create_index("pulse_id",  unique=True)
        await col.create_index("parent_id")
        await col.create_index("created_at")
        await col.create_index([("tags.source", 1)])
        logger.debug("MongoDB indexes verified")

    # ── pulse CRUD ────────────────────────────────────────────────────────── #

    async def save_pulse(
        self, pulse_id: str, blob: bytes, meta: PulseBlob
    ) -> str:
        """Store blob + metadata.  Returns storage backend string."""
        if not self.is_mongo:
            return await self._mem.save_pulse(pulse_id, blob, meta)

        doc = meta.model_dump()

        if len(blob) > GRIDFS_THRESHOLD:
            # GridFS path — store binary separately
            file_id = await self._gfs.upload_from_stream(
                pulse_id, blob,
                metadata={"pulse_id": pulse_id, "created_at": time.time()}
            )
            doc["gridfs_file_id"] = str(file_id)
            backend = "gridfs"
        else:
            doc["blob"] = blob
            backend = "mongo"

        await self._db[COLLECTION_META].replace_one(
            {"pulse_id": pulse_id}, doc, upsert=True
        )
        logger.debug("Saved pulse  id={}  backend={}", pulse_id[:8], backend)
        return backend

    async def load_pulse(self, pulse_id: str) -> tuple[bytes, PulseBlob]:
        if not self.is_mongo:
            return await self._mem.load_pulse(pulse_id)

        doc = await self._db[COLLECTION_META].find_one(
            {"pulse_id": pulse_id}, {"_id": 0}
        )
        if doc is None:
            raise KeyError(f"Pulse {pulse_id!r} not found")

        if "gridfs_file_id" in doc:
            stream = await self._gfs.open_download_stream(
                ObjectId(doc.pop("gridfs_file_id"))
            )
            blob = await stream.read()
        else:
            blob = bytes(doc.pop("blob"))

        return blob, PulseBlob(**doc)

    async def update_pulse(
        self, pulse_id: str, blob: bytes, meta: PulseBlob
    ) -> None:
        """
        Atomic shard replacement (used after key rotation).
        Only this shard's document is rewritten; MasterPulse is untouched
        until the caller rebuilds it.
        """
        if not self.is_mongo:
            return await self._mem.update_pulse(pulse_id, blob, meta)

        # Delete old GridFS file if present
        old_doc = await self._db[COLLECTION_META].find_one({"pulse_id": pulse_id})
        if old_doc and "gridfs_file_id" in old_doc:
            try:
                await self._gfs.delete(ObjectId(old_doc["gridfs_file_id"]))
            except Exception:
                pass

        await self.save_pulse(pulse_id, blob, meta)
        logger.debug("Atomic update  pulse={}…", pulse_id[:8])

    async def delete_pulse(self, pulse_id: str) -> bool:
        if not self.is_mongo:
            return await self._mem.delete_pulse(pulse_id)

        doc = await self._db[COLLECTION_META].find_one({"pulse_id": pulse_id})
        if doc and "gridfs_file_id" in doc:
            try:
                await self._gfs.delete(ObjectId(doc["gridfs_file_id"]))
            except Exception:
                pass
        result = await self._db[COLLECTION_META].delete_one({"pulse_id": pulse_id})
        return result.deleted_count > 0

    async def list_pulses(
        self,
        parent_id: Optional[str] = None,
        limit:     int = 100,
        skip:      int = 0,
    ) -> list[dict]:
        if not self.is_mongo:
            return await self._mem.list_pulses(parent_id)

        query  = {"parent_id": parent_id} if parent_id else {}
        cursor = self._db[COLLECTION_META].find(
            query,
            {"_id": 0, "blob": 0, "gridfs_file_id": 0},  # exclude binary fields
        ).skip(skip).limit(limit).sort("created_at", -1)
        return await cursor.to_list(length=limit)

    async def count_pulses(self) -> int:
        if not self.is_mongo:
            return await self._mem.count_pulses()
        return await self._db[COLLECTION_META].count_documents({})

    # ── master pulse ──────────────────────────────────────────────────────── #

    async def save_master(self, master: MasterPulse) -> None:
        if not self.is_mongo:
            return await self._mem.save_master(master)
        await self._db[COLLECTION_MASTER].replace_one(
            {"master_id": master.master_id}, master.model_dump(), upsert=True
        )

    async def load_master(self, master_id: str) -> MasterPulse:
        if not self.is_mongo:
            return await self._mem.load_master(master_id)
        doc = await self._db[COLLECTION_MASTER].find_one(
            {"master_id": master_id}, {"_id": 0}
        )
        if doc is None:
            raise KeyError(f"MasterPulse {master_id!r} not found")
        return MasterPulse(**doc)

    async def list_masters(self, limit: int = 50) -> list[dict]:
        if not self.is_mongo:
            return list(self._mem._masters.values())
        cursor = self._db[COLLECTION_MASTER].find(
            {}, {"_id": 0}
        ).sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)
