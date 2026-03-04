"""
QUANTUM-PULSE :: core/interface.py
=====================================
FUSE-like Virtual Mount — models "see" sealed files as a local drive without
any plaintext ever touching disk.

Architecture
────────────
  VirtualMount        – maps virtual paths (e.g. "/corpus/shard_01.arrow")
                        to PulseBlob IDs in the DB
  InMemoryFileHandle  – holds decrypted bytes for an open virtual file;
                        supports seek/read semantics
  MountManager        – manages all active mounts; wires VirtualMount to the
                        QuantumEngine + storage backend
  FastAPI router      – exposes POSIX-like endpoints:
                          GET  /mount/{mount_id}/ls/{path}  → directory listing
                          GET  /mount/{mount_id}/cat/{path} → file contents (streaming)
                          GET  /mount/{mount_id}/stat/{path}→ file metadata
                          POST /mount                       → create a new mount
                          DELETE /mount/{mount_id}          → unmount

The LLM training loop can use the /cat endpoint as a streaming data source,
consuming decrypted Arrow/Parquet/text shards over localhost with zero disk I/O.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
import uuid
from typing import Any, AsyncIterator, Optional

import msgpack
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from models.pulse_models import (
    MountedFile,
    PulseBlob,
    VaultMount,
)

# ─────────────────────────────── constants ────────────────────────────────── #

MAX_CACHED_HANDLES   = 256          # LRU cap for open file handles
HANDLE_TTL_SECONDS   = 600          # Evict idle handles after 10 min
STREAM_CHUNK_BYTES   = 64 * 1024    # 64 KiB per streamed chunk

# ─────────────────────────────── InMemoryFileHandle ──────────────────────── #

class InMemoryFileHandle:
    """
    Holds a decrypted file's bytes in memory with file-like seek/read interface.
    Created on first open; evicted after TTL or explicit close.
    """

    __slots__ = (
        "_buf", "_path", "_pulse_id",
        "_created_at", "_last_read_at", "_read_count",
    )

    def __init__(self, path: str, pulse_id: str, data: bytes) -> None:
        self._buf          = io.BytesIO(data)
        self._path         = path
        self._pulse_id     = pulse_id
        self._created_at   = time.monotonic()
        self._last_read_at = self._created_at
        self._read_count   = 0

    def read(self, n: int = -1) -> bytes:
        self._last_read_at = time.monotonic()
        self._read_count  += 1
        return self._buf.read(n)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._buf.seek(offset, whence)

    def tell(self) -> int:
        return self._buf.tell()

    @property
    def size(self) -> int:
        return len(self._buf.getbuffer())

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_read_at

    def is_expired(self, ttl: float = HANDLE_TTL_SECONDS) -> bool:
        return self.idle_seconds > ttl

    async def stream(self, chunk_size: int = STREAM_CHUNK_BYTES) -> AsyncIterator[bytes]:
        """Yield file contents in chunks for FastAPI StreamingResponse."""
        self.seek(0)
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                break
            yield chunk
            await asyncio.sleep(0)   # yield control back to event loop


# ─────────────────────────────── VirtualMount ────────────────────────────── #

class VirtualMount:
    """
    Maps virtual path strings → MountedFile records.
    Backed by an in-memory dict; populated by MountManager.scan_and_mount().
    """

    def __init__(self, mount_id: str, root_path: str = "/") -> None:
        self._info    = VaultMount(mount_id=mount_id, root_path=root_path)
        self._handles: dict[str, InMemoryFileHandle] = {}

    @property
    def mount_id(self) -> str:
        return self._info.mount_id

    @property
    def info(self) -> VaultMount:
        self._info.read_count = sum(1 for _ in self._handles)
        return self._info

    def register_file(
        self, virtual_path: str, pulse_id: str, size: int,
        content_type: str = "application/octet-stream",
    ) -> None:
        self._info.files[virtual_path] = MountedFile(
            virtual_path = virtual_path,
            pulse_id     = pulse_id,
            size         = size,
            content_type = content_type,
        )
        logger.debug("Registered virtual file  {}  pulse={}…", virtual_path, pulse_id[:8])

    def list_dir(self, dir_path: str = "/") -> list[MountedFile]:
        """Return all files under *dir_path* (non-recursive)."""
        norm = dir_path.rstrip("/") + "/"
        result = []
        for vpath, mfile in self._info.files.items():
            if vpath.startswith(norm) and "/" not in vpath[len(norm):]:
                result.append(mfile)
        return result

    def stat(self, virtual_path: str) -> Optional[MountedFile]:
        return self._info.files.get(virtual_path)

    def get_handle(self, virtual_path: str) -> Optional[InMemoryFileHandle]:
        handle = self._handles.get(virtual_path)
        if handle and handle.is_expired():
            del self._handles[virtual_path]
            logger.debug("Evicted expired handle  {}", virtual_path)
            return None
        return handle

    def put_handle(self, virtual_path: str, handle: InMemoryFileHandle) -> None:
        # Simple LRU: evict oldest if at cap
        if len(self._handles) >= MAX_CACHED_HANDLES:
            oldest = min(self._handles, key=lambda k: self._handles[k]._last_read_at)
            del self._handles[oldest]
            logger.debug("LRU evict  {}", oldest)
        self._handles[virtual_path] = handle

    def flush_handles(self) -> int:
        n = len(self._handles)
        self._handles.clear()
        return n


# ─────────────────────────────── MountManager ────────────────────────────── #

class MountManager:
    """
    Top-level manager — creates, tracks, and tears down VirtualMounts.
    Wires VirtualMount to the QuantumEngine for on-demand decryption.
    """

    def __init__(self) -> None:
        self._mounts: dict[str, VirtualMount] = {}
        self._engine_ref: Any = None   # set by app startup

    def set_engine(self, engine: Any) -> None:
        self._engine_ref = engine
        logger.info("MountManager wired to QuantumEngine")

    def create_mount(self, root_path: str = "/") -> VirtualMount:
        mount_id = str(uuid.uuid4())
        mount    = VirtualMount(mount_id=mount_id, root_path=root_path)
        self._mounts[mount_id] = mount
        logger.info("Mount created  id={}  root={}", mount_id[:8], root_path)
        return mount

    def get_mount(self, mount_id: str) -> VirtualMount:
        mount = self._mounts.get(mount_id)
        if mount is None:
            raise KeyError(f"Mount {mount_id!r} not found")
        return mount

    def destroy_mount(self, mount_id: str) -> int:
        mount = self._mounts.pop(mount_id, None)
        if mount is None:
            return 0
        flushed = mount.flush_handles()
        logger.info("Mount destroyed  id={}  handles_flushed={}", mount_id[:8], flushed)
        return flushed

    def list_mounts(self) -> list[VaultMount]:
        return [m.info for m in self._mounts.values()]

    async def open_file(
        self,
        mount_id: str,
        virtual_path: str,
        load_blob_fn,   # async callable: (pulse_id) -> (bytes, PulseBlob)
    ) -> InMemoryFileHandle:
        """
        Open a virtual file for reading.
        Checks handle cache first; on miss, fetches + decrypts the blob.
        """
        mount = self.get_mount(mount_id)
        mfile = mount.stat(virtual_path)
        if mfile is None:
            raise FileNotFoundError(f"Virtual path not found: {virtual_path!r}")

        cached = mount.get_handle(virtual_path)
        if cached is not None:
            logger.debug("Cache hit  {}  reads={}", virtual_path, cached._read_count)
            return cached

        # Cache miss → decrypt from storage
        blob, meta = await load_blob_fn(mfile.pulse_id)
        payload    = await self._engine_ref.unseal(blob, meta)

        # Payload may be dict with 'data' key (file upload) or raw msgpack
        if isinstance(payload, dict) and "data" in payload:
            raw = bytes(payload["data"]) if isinstance(payload["data"], (list, bytearray)) else payload["data"]
        else:
            raw = msgpack.packb(payload, use_bin_type=True)

        handle = InMemoryFileHandle(
            path      = virtual_path,
            pulse_id  = mfile.pulse_id,
            data      = raw,
        )
        mount.put_handle(virtual_path, handle)
        mfile.decrypted = True
        logger.info(
            "Decrypted into memory  {}  size={}",
            virtual_path, handle.size
        )
        return handle


# ─────────────────────────────── FastAPI router ──────────────────────────── #

mount_manager = MountManager()


class MountCreateRequest(BaseModel):
    root_path:  str  = "/"
    pulse_map:  dict[str, str] = Field(
        default_factory=dict,
        description="virtual_path → pulse_id mapping to register immediately",
    )


def create_interface_router(load_blob_fn) -> APIRouter:
    """
    Factory that wires the load_blob_fn (provided by main.py) into
    the router's closure so it can fetch blobs from MongoDB/memory.
    """
    router = APIRouter(prefix="/mount", tags=["virtual-mount"])

    @router.post("/", summary="Create a new virtual mount")
    async def create_mount(req: MountCreateRequest) -> dict:
        mount = mount_manager.create_mount(req.root_path)
        for vpath, pid in req.pulse_map.items():
            mount.register_file(vpath, pid, size=0)
        return {"mount_id": mount.mount_id, "root_path": req.root_path, "files": len(req.pulse_map)}

    @router.delete("/{mount_id}", summary="Destroy a mount and flush all cached handles")
    async def destroy_mount(mount_id: str) -> dict:
        flushed = mount_manager.destroy_mount(mount_id)
        return {"mount_id": mount_id, "handles_flushed": flushed}

    @router.get("/", summary="List all active mounts")
    async def list_mounts() -> list[dict]:
        return [m.model_dump() for m in mount_manager.list_mounts()]

    @router.get("/{mount_id}/ls", summary="List virtual directory")
    async def ls(mount_id: str, path: str = "/") -> list[dict]:
        try:
            mount = mount_manager.get_mount(mount_id)
        except KeyError:
            raise HTTPException(404, f"Mount {mount_id!r} not found")
        return [f.model_dump() for f in mount.list_dir(path)]

    @router.get("/{mount_id}/stat/{vpath:path}", summary="Stat a virtual file")
    async def stat_file(mount_id: str, vpath: str) -> dict:
        try:
            mount = mount_manager.get_mount(mount_id)
        except KeyError:
            raise HTTPException(404, f"Mount {mount_id!r} not found")
        mfile = mount.stat("/" + vpath.lstrip("/"))
        if mfile is None:
            raise HTTPException(404, f"Path /{vpath} not found")
        return mfile.model_dump()

    @router.get("/{mount_id}/cat/{vpath:path}", summary="Stream virtual file contents")
    async def cat_file(mount_id: str, vpath: str) -> StreamingResponse:
        """
        Stream decrypted file bytes over HTTP.  The model training loop can read
        this as a normal file: no plaintext ever touches disk.
        """
        virtual_path = "/" + vpath.lstrip("/")
        try:
            handle = await mount_manager.open_file(
                mount_id, virtual_path, load_blob_fn
            )
        except KeyError:
            raise HTTPException(404, f"Mount {mount_id!r} not found")
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

        mfile = mount_manager.get_mount(mount_id).stat(virtual_path)
        content_type = mfile.content_type if mfile else "application/octet-stream"

        return StreamingResponse(
            handle.stream(),
            media_type = content_type,
            headers    = {
                "Content-Length":       str(handle.size),
                "X-Pulse-ID":           mfile.pulse_id if mfile else "",
                "X-Virtual-Path":       virtual_path,
                "X-QP-Decrypted-Size":  str(handle.size),
            },
        )

    @router.post("/{mount_id}/register", summary="Register a new virtual file in an existing mount")
    async def register_file(
        mount_id:    str,
        virtual_path: str,
        pulse_id:    str,
        size:        int = 0,
        content_type: str = "application/octet-stream",
    ) -> dict:
        try:
            mount = mount_manager.get_mount(mount_id)
        except KeyError:
            raise HTTPException(404, f"Mount {mount_id!r} not found")
        mount.register_file(virtual_path, pulse_id, size, content_type)
        return {"registered": virtual_path, "pulse_id": pulse_id}

    return router
