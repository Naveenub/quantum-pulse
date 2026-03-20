"""
QUANTUM-PULSE :: tests/test_gcs_integration.py
================================================
Integration tests for the GCS storage backend against fake-gcs-server.

These tests run against a REAL fake-gcs-server endpoint — not mocks.
They verify the full read/write/delete cycle over actual HTTP requests.

Requirements
────────────
  pip install gcloud-aio-storage aiohttp
  fake-gcs-server running at http://localhost:4443

  In CI this is provided by the docker service in
  .github/workflows/gcs-integration.yml.

  Locally:
    docker run --rm -p 4443:4443 \\
      fsouza/fake-gcs-server:latest \\
      -scheme http -port 4443 \\
      -public-host localhost:4443
    pytest tests/test_gcs_integration.py -v

Environment
───────────
  GCS_ENDPOINT_URL  — default http://localhost:4443
  GCS_TEST_BUCKET   — default quantum-pulse-test
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

# Ensure repo root is on sys.path so `core` and `models` are importable
# regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
import pytest
import pytest_asyncio

ENDPOINT = os.environ.get("GCS_ENDPOINT_URL", "http://localhost:4443")
BUCKET   = os.environ.get("GCS_TEST_BUCKET", "quantum-pulse-test")

# ── skip entire module if gcloud-aio-storage not installed ───────────────── #
gcloud_storage = pytest.importorskip(
    "gcloud.aio.storage",
    reason="gcloud-aio-storage not installed — pip install gcloud-aio-storage aiohttp",
)
Storage = gcloud_storage.Storage


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


async def _ensure_bucket(session: aiohttp.ClientSession) -> None:
    """Create test bucket via fake-gcs-server REST API if it doesn't exist."""
    url = f"{ENDPOINT}/storage/v1/b"
    try:
        async with session.post(
            url,
            json={"name": BUCKET},
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status not in (200, 409):  # 409 = already exists
                text = await resp.text()
                raise RuntimeError(f"Bucket creation failed {resp.status}: {text}")
    except Exception as exc:
        if "already" in str(exc).lower() or "409" in str(exc):
            pass  # bucket exists — fine
        else:
            raise


@pytest_asyncio.fixture(scope="module")
async def gcs_store():
    """Create and connect a GCSStore pointed at fake-gcs-server."""
    from core.storage_gcs import GCSStore

    store = GCSStore.__new__(GCSStore)
    store._bucket = BUCKET
    store._prefix = "qp-test"
    store._service_file = None

    # fake-gcs-server uses HTTP and needs no auth — patch api_root via env
    os.environ["STORAGE_EMULATOR_HOST"] = ENDPOINT.replace("http://", "")

    # Ensure bucket exists
    async with aiohttp.ClientSession() as session:
        await _ensure_bucket(session)

    yield store

    # Teardown: delete all test objects
    # api_root = bare host; gcloud-aio-storage appends /storage/v1 internally
    async with aiohttp.ClientSession() as session:
        try:
            storage = Storage(session=session, api_root=ENDPOINT)
            response = await storage.list_objects(
                BUCKET, params={"prefix": "qp-test/"}
            )
            for item in response.get("items", []):
                try:
                    await storage.delete(BUCKET, item["name"])
                except Exception:
                    pass
        except Exception:
            pass


def _storage(session: aiohttp.ClientSession) -> Storage:
    """Return a Storage client pointed at fake-gcs-server.
    api_root must be the bare host — gcloud-aio-storage appends /storage/v1 itself.
    """
    return Storage(
        session=session,
        api_root=ENDPOINT,
    )


# ── tests ─────────────────────────────────────────────────────────────────── #


class TestGCSIntegration:
    """Full round-trip tests against a real fake-gcs-server endpoint."""

    @pytest.mark.asyncio
    async def test_save_and_load_pulse(self, gcs_store):
        """Save a pulse blob and metadata, then load and verify they match."""
        pid, meta = _make_pulse()
        blob = b"real-gcs-encrypted-blob-data-" + pid.encode()

        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            # save via direct storage call matching GCSStore internals
            await storage.upload(
                BUCKET,
                f"qp-test/blobs/{pid}",
                blob,
                content_type="application/octet-stream",
            )
            await storage.upload(
                BUCKET,
                f"qp-test/meta/{pid}.json",
                meta.model_dump_json().encode(),
                content_type="application/json",
            )

            # load back
            got_blob = await storage.download(BUCKET, f"qp-test/blobs/{pid}")
            got_meta_bytes = await storage.download(BUCKET, f"qp-test/meta/{pid}.json")

        from models.pulse_models import PulseBlob
        got_meta = PulseBlob.model_validate_json(got_meta_bytes)

        assert got_blob == blob
        assert got_meta.pulse_id == pid
        assert got_meta.merkle_root == meta.merkle_root
        assert got_meta.stats.original_bytes == 200

    @pytest.mark.asyncio
    async def test_load_nonexistent_raises(self, gcs_store):
        """Loading a key that doesn't exist raises an exception."""
        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            with pytest.raises(Exception):
                await storage.download(BUCKET, f"qp-test/blobs/does-not-exist-{uuid.uuid4()}")

    @pytest.mark.asyncio
    async def test_update_overwrites(self, gcs_store):
        """Uploading to the same key replaces the previous content."""
        pid = str(uuid.uuid4())
        key = f"qp-test/blobs/{pid}"

        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            await storage.upload(BUCKET, key, b"original", content_type="application/octet-stream")
            await storage.upload(BUCKET, key, b"updated", content_type="application/octet-stream")
            got = await storage.download(BUCKET, key)

        assert got == b"updated"

    @pytest.mark.asyncio
    async def test_delete_object(self, gcs_store):
        """Deleting an object makes it undownloadable."""
        pid = str(uuid.uuid4())
        key = f"qp-test/blobs/{pid}"

        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            await storage.upload(BUCKET, key, b"to-delete", content_type="application/octet-stream")
            await storage.delete(BUCKET, key)
            with pytest.raises(Exception):
                await storage.download(BUCKET, key)

    @pytest.mark.asyncio
    async def test_list_objects(self, gcs_store):
        """list_objects returns objects under the given prefix."""
        prefix_id = f"list-test-{int(time.time())}"
        keys = [f"qp-test/meta/{prefix_id}-{i}.json" for i in range(3)]

        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            for k in keys:
                await storage.upload(BUCKET, k, b'{"x":1}', content_type="application/json")

            response = await storage.list_objects(
                BUCKET, params={"prefix": f"qp-test/meta/{prefix_id}"}
            )
            listed = {item["name"] for item in response.get("items", [])}
            for k in keys:
                assert k in listed

            # Cleanup
            for k in keys:
                await storage.delete(BUCKET, k)

    @pytest.mark.asyncio
    async def test_large_blob(self, gcs_store):
        """512 KB blob survives upload/download intact."""
        pid = str(uuid.uuid4())
        key = f"qp-test/blobs/{pid}"
        large_blob = b"Z" * 512 * 1024

        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            await storage.upload(BUCKET, key, large_blob, content_type="application/octet-stream")
            got = await storage.download(BUCKET, key)
            await storage.delete(BUCKET, key)

        assert len(got) == len(large_blob)
        assert got == large_blob

    @pytest.mark.asyncio
    async def test_json_metadata_roundtrip(self, gcs_store):
        """PulseBlob JSON survives GCS upload/download with all fields intact."""
        pid, meta = _make_pulse()
        meta_with_parent = meta.model_copy(update={"parent_id": "parent-xyz"})
        key = f"qp-test/meta/{pid}.json"

        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            await storage.upload(
                BUCKET, key,
                meta_with_parent.model_dump_json().encode(),
                content_type="application/json",
            )
            raw = await storage.download(BUCKET, key)
            await storage.delete(BUCKET, key)

        from models.pulse_models import PulseBlob
        got = PulseBlob.model_validate_json(raw)

        assert got.parent_id == "parent-xyz"
        assert got.salt == meta.salt
        assert got.nonce == meta.nonce
        assert got.chunk_hash == meta.chunk_hash

    @pytest.mark.asyncio
    async def test_master_pulse_roundtrip(self, gcs_store):
        """MasterPulse JSON survives GCS upload/download."""
        from models.pulse_models import MasterPulse

        master = MasterPulse(
            master_id=str(uuid.uuid4()),
            shard_ids=["shard-1", "shard-2"],
            merkle_tree=["aa" * 32, "bb" * 32, "cc" * 32],
            merkle_root="cc" * 32,
            total_original_bytes=2048,
            total_shards=2,
        )
        key = f"qp-test/masters/{master.master_id}.json"

        async with aiohttp.ClientSession() as session:
            storage = _storage(session)
            await storage.upload(
                BUCKET, key,
                master.model_dump_json().encode(),
                content_type="application/json",
            )
            raw = await storage.download(BUCKET, key)
            await storage.delete(BUCKET, key)

        from models.pulse_models import MasterPulse
        got = MasterPulse.model_validate_json(raw)

        assert got.master_id == master.master_id
        assert got.shard_ids == ["shard-1", "shard-2"]
        assert got.total_shards == 2
        assert got.merkle_root == master.merkle_root

    @pytest.mark.asyncio
    async def test_gcsstore_save_and_load_full(self, gcs_store):
        """End-to-end test through GCSStore.save_pulse / load_pulse."""
        from core.storage_gcs import GCSStore, GCS_AVAILABLE

        if not GCS_AVAILABLE:
            pytest.skip("gcloud-aio-storage not available")

        # Build a real GCSStore pointed at fake-gcs-server
        store = GCSStore(bucket=BUCKET, prefix="qp-test")

        pid, meta = _make_pulse()
        blob = b"full-gcsstore-roundtrip-blob"

        backend = await store.save_pulse(pid, blob, meta)
        assert backend == "gcs"

        got_blob, got_meta = await store.load_pulse(pid)
        assert got_blob == blob
        assert got_meta.pulse_id == pid

        deleted = await store.delete_pulse(pid)
        assert deleted is True

        with pytest.raises(KeyError):
            await store.load_pulse(pid)
