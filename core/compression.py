"""
QUANTUM-PULSE :: core/compression.py
======================================
Standalone Zstd compression layer with:
  • Level 22 (Ultra) + 128 MiB sliding window
  • Pre-trained dictionary support (ZstdDictTrainer from engine)
  • Streaming compress/decompress for large blobs (avoids full in-memory copy)
  • Async wrappers — all heavy work runs in ThreadPoolExecutor
  • Benchmark harness to compare vanilla vs dict-assisted compression
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import zstandard as zstd
from loguru import logger

from core.engine import (
    _CPU_POOL,
    ZSTD_LEVEL,
    ZSTD_THREADS,
    ZSTD_WINDOW_LOG,
    ZstdDictTrainer,
    _sizeof_fmt,
)

# ─────────────────────────────── constants ────────────────────────────────── #

STREAM_CHUNK_BYTES = 256 * 1024  # 256 KiB streaming chunks
MIN_DICT_SAMPLE_BYTES = 1_024  # Skip samples smaller than 1 KiB

# ─────────────────────────────── result types ─────────────────────────────── #


@dataclass
class CompressResult:
    original_bytes: int
    compressed_bytes: int
    ratio: float
    duration_ms: float
    dict_id: int | None = None

    @property
    def throughput_mb_s(self) -> float:
        if self.duration_ms <= 0:
            return 0.0
        return (self.original_bytes / 1024 / 1024) / (self.duration_ms / 1000)


@dataclass
class BenchmarkReport:
    vanilla_ratio: float
    dict_ratio: float
    improvement_pct: float
    vanilla_ms: float
    dict_ms: float
    sample_count: int
    sample_bytes: int


# ─────────────────────────────── PulseCompressor ──────────────────────────── #


class PulseCompressor:
    """
    High-level async compression interface wrapping ZstdDictTrainer.

    Designed to be a singleton per engine instance; the trained dictionary
    is kept in memory for the lifetime of the process.
    """

    def __init__(self, trainer: ZstdDictTrainer | None = None) -> None:
        self._trainer = trainer or ZstdDictTrainer()

    # ── dictionary lifecycle ───────────────────────────────────────────────── #

    async def train_from_samples(self, samples: list[bytes]) -> None:
        """Train Zstd dict from raw byte samples (async, non-blocking)."""
        valid = [s for s in samples if len(s) >= MIN_DICT_SAMPLE_BYTES]
        if not valid:
            logger.warning("All samples below min size {}; dict not trained", MIN_DICT_SAMPLE_BYTES)
            return
        await self._trainer.train_async(valid)

    async def train_from_text(self, texts: list[str], encoding: str = "utf-8") -> None:
        await self.train_from_samples([t.encode(encoding) for t in texts])

    @property
    def dict_id(self) -> int | None:
        return self._trainer.dict_id

    @property
    def is_dict_trained(self) -> bool:
        return self._trainer.is_trained

    # ── one-shot compress / decompress ────────────────────────────────────── #

    async def compress(self, data: bytes) -> tuple[bytes, CompressResult]:
        """Compress *data* using current dict (if trained) at Zstd L22."""
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        compressed: bytes = await loop.run_in_executor(_CPU_POOL, self._sync_compress, data)
        ms = (time.perf_counter() - t0) * 1_000
        result = CompressResult(
            original_bytes=len(data),
            compressed_bytes=len(compressed),
            ratio=len(data) / max(len(compressed), 1),
            duration_ms=ms,
            dict_id=self._trainer.dict_id,
        )
        logger.debug(
            "Compress  {}→{}  ratio={:.2f}×  {:.1f} ms  {:.1f} MB/s",
            _sizeof_fmt(result.original_bytes),
            _sizeof_fmt(result.compressed_bytes),
            result.ratio,
            result.duration_ms,
            result.throughput_mb_s,
        )
        return compressed, result

    async def decompress(self, data: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_CPU_POOL, self._sync_decompress, data)

    # ── streaming compress ─────────────────────────────────────────────────── #

    async def compress_stream(self, source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """
        Stream-compress an async byte iterator.
        Yields compressed chunks as they are produced (lower peak memory).
        """
        loop = asyncio.get_running_loop()
        self._trainer.compressor()
        buf = io.BytesIO()

        # Collect stream into buffer (zstd streaming requires synchronous writes)
        async for chunk in source:
            buf.write(chunk)

        raw = buf.getvalue()
        compressed = await loop.run_in_executor(_CPU_POOL, self._sync_compress, raw)

        # Yield in STREAM_CHUNK_BYTES pieces
        for i in range(0, len(compressed), STREAM_CHUNK_BYTES):
            yield compressed[i : i + STREAM_CHUNK_BYTES]

    async def decompress_stream(self, source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Stream-decompress an async byte iterator."""
        loop = asyncio.get_running_loop()
        buf = io.BytesIO()
        async for chunk in source:
            buf.write(chunk)
        raw = await loop.run_in_executor(_CPU_POOL, self._sync_decompress, buf.getvalue())
        for i in range(0, len(raw), STREAM_CHUNK_BYTES):
            yield raw[i : i + STREAM_CHUNK_BYTES]

    # ── benchmark ──────────────────────────────────────────────────────────── #

    async def benchmark(self, samples: list[bytes]) -> BenchmarkReport:
        """
        Compare vanilla Zstd L22 vs dictionary-assisted Zstd L22.
        Useful for proving ROI of the 5 % corpus sampling step.
        """
        if not samples:
            raise ValueError("No samples for benchmark")

        loop = asyncio.get_running_loop()

        # Vanilla (no dict)
        vanilla_cctx = zstd.ZstdCompressor(
            compression_params=zstd.ZstdCompressionParameters.from_level(
                ZSTD_LEVEL, window_log=ZSTD_WINDOW_LOG, threads=ZSTD_THREADS
            )
        )
        t0 = time.perf_counter()
        vanilla_compressed = await loop.run_in_executor(
            _CPU_POOL,
            lambda: b"".join(vanilla_cctx.compress(s) for s in samples),
        )
        vanilla_ms = (time.perf_counter() - t0) * 1_000

        vanilla_original = sum(len(s) for s in samples)
        vanilla_ratio = vanilla_original / max(len(vanilla_compressed), 1)

        # Dict-assisted
        if not self._trainer.is_trained:
            logger.warning("Dict not yet trained; training from benchmark samples now")
            await self.train_from_samples(samples)

        t0 = time.perf_counter()
        dict_compressed = await loop.run_in_executor(
            _CPU_POOL,
            lambda: b"".join(self._sync_compress(s) for s in samples),
        )
        dict_ms = (time.perf_counter() - t0) * 1_000

        dict_ratio = vanilla_original / max(len(dict_compressed), 1)
        improvement = ((dict_ratio - vanilla_ratio) / max(vanilla_ratio, 1)) * 100

        report = BenchmarkReport(
            vanilla_ratio=vanilla_ratio,
            dict_ratio=dict_ratio,
            improvement_pct=improvement,
            vanilla_ms=vanilla_ms,
            dict_ms=dict_ms,
            sample_count=len(samples),
            sample_bytes=vanilla_original,
        )
        logger.success(
            "Benchmark  vanilla={:.2f}×  dict={:.2f}×  improvement=+{:.1f}%",
            vanilla_ratio,
            dict_ratio,
            improvement,
        )
        return report

    # ── internals ─────────────────────────────────────────────────────────── #

    def _sync_compress(self, data: bytes) -> bytes:
        return self._trainer.compressor().compress(data)

    def _sync_decompress(self, data: bytes) -> bytes:
        return self._trainer.decompressor().decompress(data)

    # ── frame inspection ──────────────────────────────────────────────────── #

    @staticmethod
    def inspect_frame(data: bytes) -> dict:
        """
        Read Zstd frame header without decompressing.
        Returns dict with content_size, dict_id, window_size, etc.
        """
        try:
            params = zstd.get_frame_parameters(data)
            return {
                "content_size": params.content_size,
                "window_size": params.window_size,
                "dict_id": params.dict_id,
                "has_checksum": params.has_checksum,
            }
        except Exception as exc:
            return {"error": str(exc)}
