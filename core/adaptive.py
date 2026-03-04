"""
QUANTUM-PULSE :: core/adaptive.py
===================================
Adaptive Zstd dictionary manager.

How it works
────────────
Every time a blob is sealed, its raw MsgPack bytes are added to a rolling
sample buffer.  When the buffer accumulates enough new samples since the last
retrain, the dict is retrained on the freshest data.

Before committing a new dict, we A/B test it against the previous version on
the 20 most recent samples. The new dict is only committed if it compresses
those samples at least MIN_IMPROVEMENT_PCT better — so the ratio can only
ever go up, never down.

Dict versions
─────────────
Each trained dict is stamped with a monotonically increasing version number
and stored in the DB under the key "dict:v<N>". PulseBlobs carry a dict_version
field so the correct dict is always used for decompression, even after several
retrains.

Configuration
─────────────
  retrain_every_n   : retrain after this many new seals since last train (default 50)
  min_improvement   : minimum ratio improvement (%) to commit a new dict (default 1.0)
  buffer_max        : max samples kept in the rolling buffer (default 500)
  min_samples       : minimum samples needed before first train (default 20)
  dict_size_bytes   : Zstd dictionary size (default 112 KiB)
"""

from __future__ import annotations

import asyncio
import collections
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import zstandard as zstd
from loguru import logger

from core.engine import ZSTD_LEVEL, ZSTD_THREADS, ZSTD_WINDOW_LOG, _CPU_POOL


# ─────────────────────────────── result types ─────────────────────────────── #

@dataclass
class DictVersion:
    version:      int
    dict_id:      int
    trained_at:   float          # Unix timestamp
    sample_count: int            # how many samples it was trained on
    baseline_ratio: float        # avg ratio on A/B test corpus when committed
    raw_bytes:    bytes          # serialised dict (for DB persistence)


@dataclass
class RetrainResult:
    old_version:   int
    new_version:   int
    old_ratio:     float
    new_ratio:     float
    improvement:   float         # percentage points
    committed:     bool
    sample_count:  int
    duration_ms:   float


# ─────────────────────────────── AdaptiveDictManager ──────────────────────── #

class AdaptiveDictManager:
    """
    Manages a versioned, self-improving Zstd compression dictionary.

    Thread-safe for async use — all blocking work runs in _CPU_POOL.
    """

    def __init__(
        self,
        retrain_every_n:  int   = 50,
        min_improvement:  float = 1.0,   # percent
        buffer_max:       int   = 500,
        min_samples:      int   = 20,
        dict_size_bytes:  int   = 112 * 1024,
    ) -> None:
        self._retrain_every_n  = retrain_every_n
        self._min_improvement  = min_improvement
        self._buffer_max       = buffer_max
        self._min_samples      = min_samples
        self._dict_size        = dict_size_bytes

        # Rolling ring-buffer of raw MsgPack samples
        self._buffer: collections.deque[bytes] = collections.deque(maxlen=buffer_max)

        # Version history — index 0 is current (latest), -1 is oldest kept
        self._versions: list[DictVersion]   = []
        self._current:  Optional[zstd.ZstdCompressionDict] = None

        # How many seals have happened since the last retrain
        self._seals_since_retrain: int = 0

        # Prevent concurrent retrains
        self._retrain_lock = asyncio.Lock()

        # Seal counter across lifetime
        self._total_seals: int = 0

    # ── public API ────────────────────────────────────────────────────────── #

    async def record_seal(self, msgpack_bytes: bytes) -> Optional[RetrainResult]:
        """
        Called after every seal with the raw MsgPack bytes (pre-compression).

        Adds the sample to the rolling buffer.
        Triggers a retrain if enough new samples have accumulated.
        Returns a RetrainResult if a retrain happened, else None.
        """
        self._buffer.append(msgpack_bytes)
        self._seals_since_retrain += 1
        self._total_seals += 1

        if self._should_retrain():
            return await self._retrain()
        return None

    async def load_dict_bytes(self, raw: bytes, version: int) -> None:
        """
        Restore a previously persisted dict (e.g. from DB on server restart).
        """
        loop = asyncio.get_running_loop()
        cdict = await loop.run_in_executor(
            _CPU_POOL, lambda: zstd.ZstdCompressionDict(raw)
        )
        dv = DictVersion(
            version      = version,
            dict_id      = cdict.dict_id(),
            trained_at   = time.time(),
            sample_count = 0,
            baseline_ratio = 0.0,
            raw_bytes    = raw,
        )
        self._versions.insert(0, dv)
        self._current = cdict
        logger.info("Loaded dict v{}  id={}  size={:.1f} KiB",
                    version, cdict.dict_id(), len(raw) / 1024)

    def compressor(self) -> zstd.ZstdCompressor:
        """Return a ZstdCompressor using the current best dict (or vanilla if untrained)."""
        params = zstd.ZstdCompressionParameters.from_level(
            ZSTD_LEVEL, window_log=ZSTD_WINDOW_LOG, threads=ZSTD_THREADS
        )
        return zstd.ZstdCompressor(compression_params=params, dict_data=self._current)

    def compressor_for_version(self, version: int) -> zstd.ZstdCompressor:
        """Return a ZstdCompressor for a specific dict version (for historical unseals)."""
        dv = self._version_by_id(version)
        if dv is None:
            logger.warning("Dict v{} not found — using current", version)
            return self.compressor()
        params = zstd.ZstdCompressionParameters.from_level(
            ZSTD_LEVEL, window_log=ZSTD_WINDOW_LOG, threads=ZSTD_THREADS
        )
        cdict = zstd.ZstdCompressionDict(dv.raw_bytes)
        return zstd.ZstdCompressor(compression_params=params, dict_data=cdict)

    def decompressor_for_version(self, version: int) -> zstd.ZstdDecompressor:
        """Return a ZstdDecompressor for a specific dict version."""
        dv = self._version_by_id(version)
        if dv is None:
            return zstd.ZstdDecompressor()
        cdict = zstd.ZstdCompressionDict(dv.raw_bytes)
        return zstd.ZstdDecompressor(dict_data=cdict)

    @property
    def current_version(self) -> int:
        return self._versions[0].version if self._versions else 0

    @property
    def is_trained(self) -> bool:
        return self._current is not None

    @property
    def dict_id(self) -> Optional[int]:
        return self._current.dict_id() if self._current else None

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    @property
    def total_seals(self) -> int:
        return self._total_seals

    @property
    def seals_until_retrain(self) -> int:
        return max(0, self._retrain_every_n - self._seals_since_retrain)

    def stats(self) -> dict:
        return {
            "current_version":      self.current_version,
            "dict_id":              self.dict_id,
            "is_trained":           self.is_trained,
            "total_seals":          self._total_seals,
            "seals_since_retrain":  self._seals_since_retrain,
            "seals_until_retrain":  self.seals_until_retrain,
            "buffer_size":          self.buffer_size,
            "buffer_max":           self._buffer_max,
            "retrain_every_n":      self._retrain_every_n,
            "min_improvement_pct":  self._min_improvement,
            "versions_kept":        len(self._versions),
            "latest_ratio":         self._versions[0].baseline_ratio if self._versions else None,
        }

    async def force_retrain(self, extra_samples: Optional[list[bytes]] = None) -> Optional[RetrainResult]:
        """
        Force an immediate retrain (e.g. called by /pulse/bootstrap endpoint).
        Optionally inject extra samples beyond the rolling buffer.
        """
        if extra_samples:
            for s in extra_samples:
                self._buffer.append(s)
        return await self._retrain(force=True)

    # ── internals ─────────────────────────────────────────────────────────── #

    def _should_retrain(self) -> bool:
        return (
            len(self._buffer) >= self._min_samples
            and self._seals_since_retrain >= self._retrain_every_n
        )

    async def _retrain(self, force: bool = False) -> Optional[RetrainResult]:
        """
        Train a candidate dict on the current buffer.
        A/B test it against the previous dict.
        Commit only if it's at least _min_improvement% better.
        """
        if self._retrain_lock.locked() and not force:
            logger.debug("Retrain already in progress — skipping")
            return None

        async with self._retrain_lock:
            samples = list(self._buffer)
            if len(samples) < self._min_samples:
                return None

            t0 = time.perf_counter()
            loop = asyncio.get_running_loop()

            # Train candidate dict
            try:
                candidate_raw: bytes = await loop.run_in_executor(
                    _CPU_POOL,
                    lambda: zstd.train_dictionary(self._dict_size, samples).as_bytes(),
                )
            except Exception as exc:
                logger.warning("Dict training failed: {} — keeping current dict", exc)
                return None

            # A/B test on last 20 samples
            test_corpus = samples[-20:]
            old_ratio, new_ratio = await loop.run_in_executor(
                _CPU_POOL,
                self._ab_test,
                test_corpus,
                self._current,
                candidate_raw,
            )

            improvement = ((new_ratio - old_ratio) / max(old_ratio, 0.001)) * 100
            old_version = self.current_version
            new_version = old_version + 1
            committed   = force or improvement >= self._min_improvement

            ms = (time.perf_counter() - t0) * 1000

            if committed:
                cdict = zstd.ZstdCompressionDict(candidate_raw)
                dv = DictVersion(
                    version        = new_version,
                    dict_id        = cdict.dict_id(),
                    trained_at     = time.time(),
                    sample_count   = len(samples),
                    baseline_ratio = new_ratio,
                    raw_bytes      = candidate_raw,
                )
                self._versions.insert(0, dv)
                self._current = cdict
                # Keep last 3 versions (for unsealing old blobs)
                if len(self._versions) > 3:
                    self._versions = self._versions[:3]

                logger.success(
                    "Dict upgraded v{}→v{}  ratio {:.2f}×→{:.2f}×  +{:.1f}%  "
                    "{} samples  {:.0f} ms  id={}",
                    old_version, new_version,
                    old_ratio, new_ratio, improvement,
                    len(samples), ms, cdict.dict_id(),
                )
            else:
                logger.info(
                    "Dict retrain: candidate not better  {:.2f}×→{:.2f}×  {:.1f}%  "
                    "(need +{:.1f}%)  keeping v{}",
                    old_ratio, new_ratio, improvement,
                    self._min_improvement, old_version,
                )

            self._seals_since_retrain = 0

            return RetrainResult(
                old_version  = old_version,
                new_version  = new_version if committed else old_version,
                old_ratio    = old_ratio,
                new_ratio    = new_ratio,
                improvement  = improvement,
                committed    = committed,
                sample_count = len(samples),
                duration_ms  = ms,
            )

    @staticmethod
    def _ab_test(
        corpus:        list[bytes],
        old_cdict:     Optional[zstd.ZstdCompressionDict],
        candidate_raw: bytes,
    ) -> tuple[float, float]:
        """
        Compare old dict vs candidate dict on corpus.
        Returns (old_avg_ratio, new_avg_ratio).
        Runs in CPU pool — no async.
        """
        params = zstd.ZstdCompressionParameters.from_level(
            ZSTD_LEVEL, window_log=ZSTD_WINDOW_LOG, threads=ZSTD_THREADS
        )
        cctx_new = zstd.ZstdCompressor(
            compression_params=params,
            dict_data=zstd.ZstdCompressionDict(candidate_raw),
        )
        cctx_old = zstd.ZstdCompressor(
            compression_params=params,
            dict_data=old_cdict,   # None → vanilla
        )

        old_sizes, new_sizes = 0, 0
        total_orig = 0
        for sample in corpus:
            old_sizes  += len(cctx_old.compress(sample))
            new_sizes  += len(cctx_new.compress(sample))
            total_orig += len(sample)

        old_ratio = total_orig / max(old_sizes, 1)
        new_ratio = total_orig / max(new_sizes, 1)
        return old_ratio, new_ratio

    def _version_by_id(self, version: int) -> Optional[DictVersion]:
        for dv in self._versions:
            if dv.version == version:
                return dv
        return None


# ── singleton ─────────────────────────────────────────────────────────────── #
# Instantiated once and wired into the engine in main.py lifespan.
adaptive_dict = AdaptiveDictManager(
    retrain_every_n = 50,    # retrain after every 50 new seals
    min_improvement = 1.0,   # only commit if at least 1% better
    buffer_max      = 500,   # keep last 500 samples in rolling buffer
    min_samples     = 20,    # need at least 20 before first train
    dict_size_bytes = 112 * 1024,
)
