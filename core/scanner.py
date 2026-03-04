"""
QUANTUM-PULSE :: core/scanner.py
==================================
High-speed filesystem scanner using os.scandir (10× faster than os.walk)
with a multi-threaded Producer-Consumer pattern.

Architecture
────────────
  Producer thread  – walks the directory tree with scandir, pushes DirEntry
                     items into a bounded asyncio.Queue
  Consumer tasks   – N async workers drain the queue, hash files, build
                     FileEntry records, and check entropy for sharding
  Aggregator       – assembles a DirManifest (MsgPack payload for the engine)

Shard decision: if a directory's metadata (serialised as MsgPack) exceeds
ENTROPY_SHARD_THRESHOLD, it is split into sub-pulses — one per sub-directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import AsyncIterator, Callable, Optional

import aiofiles
import msgpack
from loguru import logger

from core.engine import ENTROPY_SHARD_THRESHOLD, shannon_entropy
from models.pulse_models import (
    DirManifest,
    FileEntry,
    ScanMode,
    ScanStats,
)

# ─────────────────────────────── constants ────────────────────────────────── #

SCAN_QUEUE_SIZE   = 4_096
SCAN_WORKERS      = min(32, (os.cpu_count() or 4) * 4)
HASH_CHUNK_BYTES  = 256 * 1024   # 256 KiB read chunks for content hashing
_SENTINEL         = None         # poison-pill to stop consumers

# File extensions to skip during scan (binary noise for LLM pipelines)
_SKIP_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".dylib",
    ".exe", ".bin", ".o", ".a", ".lib",
    ".DS_Store", ".git", ".gitkeep",
})

# ─────────────────────────────── sync producer ────────────────────────────── #

def _scandir_producer(
    root: str,
    queue: Queue,
    mode: ScanMode,
    max_depth: int,
    skip_hidden: bool,
) -> None:
    """
    Runs in a dedicated thread.  Uses os.scandir for 10× faster traversal
    vs os.walk (avoids stat() on every entry unless needed).
    Pushes (DirEntry, depth) tuples; sends _SENTINEL when finished.
    """
    stack = [(root, 0)]
    pushed = 0

    while stack:
        current_path, depth = stack.pop()
        if max_depth >= 0 and depth > max_depth:
            continue
        try:
            with os.scandir(current_path) as it:
                for entry in it:
                    if skip_hidden and entry.name.startswith("."):
                        continue
                    _, ext = os.path.splitext(entry.name)
                    if ext.lower() in _SKIP_EXTENSIONS:
                        continue

                    queue.put((entry, depth), block=True)
                    pushed += 1

                    if entry.is_dir(follow_symlinks=False):
                        if mode == ScanMode.RECURSIVE:
                            stack.append((entry.path, depth + 1))
        except PermissionError:
            logger.warning("Permission denied: {}", current_path)
        except OSError as exc:
            logger.warning("OSError scanning {}: {}", current_path, exc)

    queue.put(_SENTINEL, block=True)
    logger.debug("Producer done: {} entries enqueued", pushed)


# ─────────────────────────────── async content hasher ─────────────────────── #

async def _hash_file(path: str) -> str:
    """SHA3-256 of file contents, async read in 256 KiB chunks."""
    h = hashlib.sha3_256()
    try:
        async with aiofiles.open(path, "rb") as f:
            while chunk := await f.read(HASH_CHUNK_BYTES):
                h.update(chunk)
    except OSError as exc:
        logger.debug("Cannot hash {}: {}", path, exc)
        return ""
    return h.hexdigest()


# ─────────────────────────────── QuantumScanner ───────────────────────────── #

class QuantumScanner:
    """
    Producer-Consumer directory scanner.

    Usage
    ─────
    >>> scanner = QuantumScanner("/data/corpus")
    >>> async for manifest in scanner.scan():
    ...     # manifest is a DirManifest ready to be passed to engine.seal()
    ...     await engine.seal(manifest.model_dump(), pulse_id=uuid4_str())
    """

    def __init__(
        self,
        root: str,
        *,
        mode:          ScanMode = ScanMode.RECURSIVE,
        max_depth:     int      = -1,       # -1 = unlimited
        skip_hidden:   bool     = True,
        hash_contents: bool     = True,     # SHA3-256 per file (slower but enables dedup)
        shard_on_entropy: bool  = True,
        n_workers:     int      = SCAN_WORKERS,
        on_shard:      Optional[Callable[[DirManifest], None]] = None,
    ) -> None:
        self.root             = os.path.abspath(root)
        self.mode             = mode
        self.max_depth        = max_depth
        self.skip_hidden      = skip_hidden
        self.hash_contents    = hash_contents
        self.shard_on_entropy = shard_on_entropy
        self.n_workers        = n_workers
        self.on_shard         = on_shard
        self._stats           = ScanStats()

    # ── public ─────────────────────────────────────────────────────────────── #

    async def scan(self) -> AsyncIterator[DirManifest]:
        """
        Async generator yielding DirManifest objects.
        Each manifest corresponds to one directory (or sub-shard if sharded).
        """
        t0            = time.perf_counter()
        raw_queue: Queue = Queue(maxsize=SCAN_QUEUE_SIZE)
        loop          = asyncio.get_running_loop()
        results_queue: asyncio.Queue[Optional[FileEntry]] = asyncio.Queue()

        # ── start producer in background thread ──────────────────────────── #
        producer = Thread(
            target=_scandir_producer,
            args=(self.root, raw_queue, self.mode, self.max_depth, self.skip_hidden),
            daemon=True,
            name="qp-scanner-producer",
        )
        producer.start()
        logger.info("Scanner started  root={}  mode={}  workers={}", self.root, self.mode.value, self.n_workers)

        # ── consumer coroutines ───────────────────────────────────────────── #
        dir_buckets: dict[str, list[FileEntry]] = {}   # dir_path → entries

        async def consumer() -> None:
            """Pull entries from the thread-safe Queue, process, bucket by dir."""
            while True:
                try:
                    item = await loop.run_in_executor(None, raw_queue.get, True, 0.05)
                except Empty:
                    # Check if producer is done and queue is empty
                    if not producer.is_alive() and raw_queue.empty():
                        break
                    continue

                if item is _SENTINEL:
                    # Re-enqueue sentinel for sibling consumers then exit
                    raw_queue.put(_SENTINEL)
                    break

                entry, _depth = item
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    self._stats.skipped_files += 1
                    continue

                file_entry = FileEntry(
                    path    = entry.path,
                    name    = entry.name,
                    size    = stat.st_size if not entry.is_dir(follow_symlinks=False) else 0,
                    mtime   = stat.st_mtime,
                    is_dir  = entry.is_dir(follow_symlinks=False),
                )

                if self.hash_contents and not file_entry.is_dir:
                    file_entry.content_hash = await _hash_file(entry.path)
                    self._stats.total_bytes += file_entry.size
                    self._stats.total_files += 1
                else:
                    self._stats.total_dirs += 1

                dir_key = str(Path(entry.path).parent)
                if dir_key not in dir_buckets:
                    dir_buckets[dir_key] = []
                dir_buckets[dir_key].append(file_entry)

        # Run N consumer coroutines concurrently
        await asyncio.gather(*[consumer() for _ in range(self.n_workers)])
        producer.join()

        # ── build manifests ───────────────────────────────────────────────── #
        self._stats.scan_duration_ms = (time.perf_counter() - t0) * 1_000
        logger.info(
            "Scan complete  files={}  dirs={}  {:.1f} ms",
            self._stats.total_files,
            self._stats.total_dirs,
            self._stats.scan_duration_ms,
        )

        for dir_path, entries in dir_buckets.items():
            manifest = DirManifest(
                root_path  = dir_path,
                entries    = entries,
                depth      = self._count_depth(dir_path),
                stats      = self._stats,
            )
            if self.shard_on_entropy:
                async for sub in self._maybe_shard(manifest):
                    yield sub
            else:
                yield manifest

    async def scan_samples(self, limit: int = 200) -> list[bytes]:
        """
        Collect a flat list of serialised FileEntry bytes for Zstd dict training.
        Returns raw MsgPack bytes, not structured manifests.
        """
        samples: list[bytes] = []
        async for manifest in self.scan():
            for entry in manifest.entries:
                samples.append(msgpack.packb(entry.model_dump(), use_bin_type=True))
                if len(samples) >= limit:
                    return samples
        return samples

    @property
    def stats(self) -> ScanStats:
        return self._stats

    # ── private ────────────────────────────────────────────────────────────── #

    async def _maybe_shard(
        self, manifest: DirManifest
    ) -> AsyncIterator[DirManifest]:
        """
        Check if the manifest's MsgPack representation exceeds the entropy
        threshold.  If so, split it into one sub-manifest per sub-directory.
        """
        packed   = msgpack.packb(manifest.model_dump(), use_bin_type=True)
        entropy  = shannon_entropy(packed)

        if entropy < ENTROPY_SHARD_THRESHOLD:
            yield manifest
            return

        logger.debug(
            "Sharding {}  entropy={:.4f}  entries={}",
            manifest.root_path, entropy, len(manifest.entries),
        )

        # Partition entries by immediate parent → one shard per sub-directory
        sub_dirs: dict[str, list[FileEntry]] = {}
        for e in manifest.entries:
            parent = str(Path(e.path).parent)
            sub_dirs.setdefault(parent, []).append(e)

        if len(sub_dirs) <= 1:
            # Can't split further; yield as-is
            yield manifest
            return

        self._stats.shards_created += len(sub_dirs)
        for sub_path, sub_entries in sub_dirs.items():
            sub_manifest = DirManifest(
                root_path = sub_path,
                entries   = sub_entries,
                depth     = self._count_depth(sub_path),
                stats     = self._stats,
            )
            if self.on_shard:
                self.on_shard(sub_manifest)
            yield sub_manifest

    def _count_depth(self, path: str) -> int:
        try:
            rel = os.path.relpath(path, self.root)
            return 0 if rel == "." else rel.count(os.sep) + 1
        except ValueError:
            return 0
