"""
QUANTUM-PULSE :: tests/test_s3_integration.py
===============================================
Integration tests for the S3 storage backend against LocalStack.

These tests run against a REAL LocalStack endpoint — not mocks.
They verify the full read/write/delete cycle over actual HTTP requests.

Requirements
────────────
  pip install aioboto3
  LocalStack running at http://localhost:4566

  In CI this is provided by the docker-compose service in
  .github/workflows/s3-integration.yml.

  Locally:
    docker run --rm -p 4566:4566 localstack/localstack
    pytest tests/test_s3_integration.py -v

Environment
───────────
  S3_ENDPOINT_URL   — default http://localhost:4566
  S3_TEST_BUCKET    — default quantum-pulse-test
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — use any value with LocalStack
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
import pytest_asyncio

ENDPOINT   = os.environ.get("S3_ENDPOINT_URL", "http://localhost:4566")
BUCKET     = os.environ.get("S3_TEST_BUCKET", "quantum-pulse-test")
AWS_KEY    = os.environ.get("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# ── skip entire module if aioboto3 not installed ─────────────────────────── #
aioboto3 = pytest.importorskip("aioboto3", reason="aioboto3 not installed")


# ── helpers ──────────────────────────────────────────────────────────────── #

def _make_pulse(pid: str | None = None):
    from models.pulse_models import CompressionStats, PulseBlob
    pid = pid or str(uuid.uuid4())
    stats = CompressionStats(
        original_bytes=200,
        packed_bytes=180,
        compressed_bytes=80,
        encrypted_bytes=90,
        duration_ms=3.0,
    )
    return pid, PulseBlob(
        pulse_id=pid,
        parent_id=None,
        salt="aa" * 32,
        nonce="bb" * 12,
        chunk_hash="cc" * 32,
        merkle_root="dd" * 32,
        dict_version=0,
        stats=stats,
    )


async def _ensure_bucket(s3_client) -> None:
    """Create test bucket if it doesn't exist."""
    try:
        await s3_client.head_bucket(Bucket=BUCKET)
    except Exception:
        await s3_client.create_bucket(Bucket=BUCKET)


@pytest_asyncio.fixture(scope="module")
async def s3_store():
    """Create and connect an S3Store pointed at LocalStack."""
    from core.storage_s3 import S3Store

    store = S3Store(
        bucket=BUCKET,
        prefix="qp-test",
        region=REGION,
        endpoint_url=ENDPOINT,
    )

    # Ensure bucket exists before injecting the session
    session = aioboto3.Session(
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET,
        region_name=REGION,
    )
    store._session = session

    # Create bucket if needed
    async with session.client(
        "s3",
        region_name=REGION,
        endpoint_url=ENDPOINT,
    ) as s3:
        await _ensure_bucket(s3)

    yield store

    # Teardown: delete all test objects
    async with session.client("s3", region_name=REGION, endpoint_url=ENDPOINT) as s3:
        try:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=BUCKET, Prefix="qp-test/"):
                for obj in page.get("Contents", []):
                    await s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
        except Exception:
            pass


# ── tests ─────────────────────────────────────────────────────────────────── #


class TestS3Integration:
    """Full round-trip tests against a real LocalStack S3 endpoint."""

    @pytest.mark.asyncio
    async def test_save_and_load_pulse(self, s3_store):
        """Save a pulse blob and metadata, then load and verify they match."""
        pid, meta = _make_pulse()
        blob = b"real-encrypted-blob-data-" + pid.encode()

        backend = await s3_store.save_pulse(pid, blob, meta)
        assert backend == "s3"

        got_blob, got_meta = await s3_store.load_pulse(pid)
        assert got_blob == blob
        assert got_meta.pulse_id == pid
        assert got_meta.merkle_root == meta.merkle_root
        assert got_meta.stats.original_bytes == 200

    @pytest.mark.asyncio
    async def test_load_nonexistent_raises_key_error(self, s3_store):
        """Loading a pulse that doesn't exist must raise KeyError."""
        with pytest.raises(KeyError):
            await s3_store.load_pulse("does-not-exist-" + str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_update_pulse_overwrites(self, s3_store):
        """Updating a pulse replaces both the blob and metadata."""
        pid, meta = _make_pulse()
        original_blob = b"original-blob"
        updated_blob = b"updated-blob-after-key-rotation"

        await s3_store.save_pulse(pid, original_blob, meta)

        # Update with new blob
        from models.pulse_models import CompressionStats, PulseBlob
        new_stats = CompressionStats(
            original_bytes=300, packed_bytes=280,
            compressed_bytes=100, encrypted_bytes=110, duration_ms=4.0,
        )
        new_meta = PulseBlob(
            pulse_id=pid, parent_id=None,
            salt="ee" * 32, nonce="ff" * 12,
            chunk_hash="11" * 32, merkle_root="22" * 32,
            dict_version=1, stats=new_stats,
        )
        await s3_store.update_pulse(pid, updated_blob, new_meta)

        got_blob, got_meta = await s3_store.load_pulse(pid)
        assert got_blob == updated_blob
        assert got_meta.dict_version == 1
        assert got_meta.stats.original_bytes == 300

    @pytest.mark.asyncio
    async def test_delete_pulse(self, s3_store):
        """Delete a pulse and confirm it's gone."""
        pid, meta = _make_pulse()
        await s3_store.save_pulse(pid, b"blob-to-delete", meta)

        deleted = await s3_store.delete_pulse(pid)
        assert deleted is True

        with pytest.raises(KeyError):
            await s3_store.load_pulse(pid)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, s3_store):
        """Deleting a pulse that doesn't exist returns False, not an error."""
        result = await s3_store.delete_pulse("nonexistent-" + str(uuid.uuid4()))
        assert result is False

    @pytest.mark.asyncio
    async def test_count_pulses(self, s3_store):
        """count_pulses reflects the number of stored metadata objects."""
        # Store 3 pulses with unique IDs so count is predictable
        prefix_id = f"count-test-{int(time.time())}"
        pids = []
        for i in range(3):
            pid, meta = _make_pulse(f"{prefix_id}-{i}")
            await s3_store.save_pulse(pid, b"blob", meta)
            pids.append(pid)

        count = await s3_store.count_pulses()
        assert count >= 3  # may include pulses from other tests in the same run

        # Cleanup
        for pid in pids:
            await s3_store.delete_pulse(pid)

    @pytest.mark.asyncio
    async def test_list_pulses(self, s3_store):
        """list_pulses returns metadata dicts for stored pulses."""
        prefix_id = f"list-test-{int(time.time())}"
        pids = []
        for i in range(2):
            pid, meta = _make_pulse(f"{prefix_id}-{i}")
            await s3_store.save_pulse(pid, b"list-blob", meta)
            pids.append(pid)

        results = await s3_store.list_pulses(limit=100)
        listed_ids = {r["pulse_id"] for r in results}
        for pid in pids:
            assert pid in listed_ids

        # Cleanup
        for pid in pids:
            await s3_store.delete_pulse(pid)

    @pytest.mark.asyncio
    async def test_save_and_load_master(self, s3_store):
        """Save and load a MasterPulse round-trip."""
        from models.pulse_models import MasterPulse

        master = MasterPulse(
            master_id=str(uuid.uuid4()),
            shard_ids=["shard-1", "shard-2"],
            merkle_tree=["aa" * 32, "bb" * 32, "cc" * 32],
            merkle_root="cc" * 32,
            total_original_bytes=1024,
            total_shards=2,
        )

        await s3_store.save_master(master)
        got = await s3_store.load_master(master.master_id)

        assert got.master_id == master.master_id
        assert got.shard_ids == ["shard-1", "shard-2"]
        assert got.total_shards == 2
        assert got.merkle_root == master.merkle_root

    @pytest.mark.asyncio
    async def test_load_master_nonexistent_raises(self, s3_store):
        """Loading a MasterPulse that doesn't exist raises KeyError."""
        with pytest.raises(KeyError):
            await s3_store.load_master("no-such-master-" + str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_large_blob(self, s3_store):
        """Blobs larger than a few KB are stored and retrieved intact."""
        pid, meta = _make_pulse()
        large_blob = b"X" * 512 * 1024  # 512 KB

        await s3_store.save_pulse(pid, large_blob, meta)
        got_blob, _ = await s3_store.load_pulse(pid)

        assert len(got_blob) == len(large_blob)
        assert got_blob == large_blob

        await s3_store.delete_pulse(pid)

    @pytest.mark.asyncio
    async def test_metadata_survives_roundtrip(self, s3_store):
        """All PulseBlob fields survive JSON serialisation through S3."""
        pid, meta = _make_pulse()
        # Set a non-default field
        meta_with_parent = meta.model_copy(update={"parent_id": "parent-123"})

        await s3_store.save_pulse(pid, b"blob", meta_with_parent)
        _, got_meta = await s3_store.load_pulse(pid)

        assert got_meta.parent_id == "parent-123"
        assert got_meta.salt == meta.salt
        assert got_meta.nonce == meta.nonce
        assert got_meta.chunk_hash == meta.chunk_hash

        await s3_store.delete_pulse(pid)
