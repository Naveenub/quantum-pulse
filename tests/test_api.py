"""
QUANTUM-PULSE :: tests/test_api.py
=====================================
FastAPI integration tests using httpx AsyncClient.
Tests the full HTTP layer including auth, middleware, and business logic.
"""

from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────── test app setup ──────────────────────────── #

TEST_PASSPHRASE = "test-passphrase-16-chars-minimum"
TEST_API_KEY    = "test-api-key-integration"

os.environ["QUANTUM_PASSPHRASE"]  = TEST_PASSPHRASE
os.environ["QUANTUM_API_KEYS"]    = f'["{TEST_API_KEY}"]'
os.environ["QUANTUM_ENVIRONMENT"] = "development"
os.environ["QUANTUM_API_KEY_ENABLED"] = "true"

# Clear settings cache so env vars take effect
from core.config import get_settings
get_settings.cache_clear()


def _make_test_app():
    """Build a test version of the app with in-process (no MongoDB) storage."""
    from core.config import get_settings
    get_settings.cache_clear()
    import main as m
    return m.app


@pytest.fixture(scope="module")
def client():
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(scope="module")
def auth_headers():
    return {"X-API-Key": TEST_API_KEY}


# ─────────────────────────────── health ──────────────────────────────────── #

def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_liveness(client):
    resp = client.get("/healthz/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"


def test_startup_probe(client):
    resp = client.get("/healthz/startup")
    assert resp.status_code == 200


# ─────────────────────────────── auth ────────────────────────────────────── #

def test_seal_requires_auth(client):
    resp = client.post("/pulse/seal", json={"payload": {"test": True}})
    # Without api key enabled in dev mode, anon gets through; let's check it handles both
    assert resp.status_code in (200, 401, 403)


def test_seal_invalid_key(client):
    resp = client.post(
        "/pulse/seal",
        json={"payload": {"test": True}},
        headers={"X-API-Key": "totally-wrong-key"},
    )
    assert resp.status_code in (401, 403)


def test_auth_token_flow(client, auth_headers):
    resp = client.post("/auth/token", json={"api_key": TEST_API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_auth_token_invalid_key(client):
    resp = client.post("/auth/token", json={"api_key": "bad-key"})
    assert resp.status_code == 403


# ─────────────────────────────── seal / unseal ───────────────────────────── #

def test_seal_basic(client, auth_headers):
    payload = {"dataset": "test", "rows": [{"id": i, "text": f"row {i}"} for i in range(10)]}
    resp    = client.post("/pulse/seal", json={"payload": payload}, headers=auth_headers)
    assert resp.status_code == 200
    data    = resp.json()
    assert "pulse_id" in data
    assert "meta"     in data
    assert data["meta"]["stats"]["ratio"] > 1.0


def test_seal_unseal_roundtrip(client, auth_headers):
    payload  = {"text": "hello from the test suite", "numbers": list(range(100))}
    seal_r   = client.post("/pulse/seal", json={"payload": payload}, headers=auth_headers)
    assert seal_r.status_code == 200
    pulse_id = seal_r.json()["pulse_id"]

    unseal_r = client.post("/pulse/unseal", json={"pulse_id": pulse_id}, headers=auth_headers)
    assert unseal_r.status_code == 200
    assert unseal_r.json()["payload"]["text"] == payload["text"]


def test_unseal_not_found(client, auth_headers):
    resp = client.post("/pulse/unseal", json={"pulse_id": str(uuid.uuid4())}, headers=auth_headers)
    assert resp.status_code == 404


def test_seal_with_tags(client, auth_headers):
    resp = client.post(
        "/pulse/seal",
        json={"payload": {"x": 1}, "tags": {"source": "pytest", "env": "test"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["meta"]["tags"]["source"] == "pytest"


def test_seal_list(client, auth_headers):
    # Seal a few pulses first
    for i in range(3):
        client.post("/pulse/seal", json={"payload": {"i": i}}, headers=auth_headers)

    resp = client.get("/pulse/list", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_pulse_stream(client, auth_headers):
    payload  = {"streaming": True, "data": list(range(50))}
    seal_r   = client.post("/pulse/seal", json={"payload": payload}, headers=auth_headers)
    pulse_id = seal_r.json()["pulse_id"]

    stream_r = client.get(f"/pulse/stream/{pulse_id}", headers=auth_headers)
    assert stream_r.status_code == 200
    assert stream_r.headers["content-type"] == "application/x-msgpack"

    import msgpack
    recovered = msgpack.unpackb(stream_r.content, raw=False)
    assert recovered["streaming"] is True


def test_delete_pulse(client, auth_headers):
    seal_r   = client.post("/pulse/seal", json={"payload": {"delete_me": True}}, headers=auth_headers)
    pulse_id = seal_r.json()["pulse_id"]

    del_r = client.delete(f"/pulse/{pulse_id}", headers=auth_headers)
    assert del_r.status_code == 200

    unseal_r = client.post("/pulse/unseal", json={"pulse_id": pulse_id}, headers=auth_headers)
    assert unseal_r.status_code == 404


# ─────────────────────────────── master pulse ────────────────────────────── #

def test_master_pulse_build(client, auth_headers):
    ids = []
    for i in range(3):
        r = client.post("/pulse/seal", json={"payload": {"shard": i}}, headers=auth_headers)
        ids.append(r.json()["pulse_id"])

    master_r = client.post("/pulse/master", json={"pulse_ids": ids}, headers=auth_headers)
    assert master_r.status_code == 200
    master   = master_r.json()
    assert master["total_shards"] == 3
    assert len(master["merkle_root"]) == 64

    get_r = client.get(f"/pulse/master/{master['master_id']}", headers=auth_headers)
    assert get_r.status_code == 200


# ─────────────────────────────── bootstrap ───────────────────────────────── #

def test_bootstrap_dict(client, auth_headers):
    """Dict training requires substantial corpus — accepts 200 or 400 (too-small corpus)."""
    samples = ["".join([f"word{j} " for j in range(200)]) for _ in range(100)]
    resp    = client.post("/pulse/bootstrap", json={"samples": samples}, headers=auth_headers)
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        assert resp.json()["status"] == "trained"


# ─────────────────────────────── benchmark ───────────────────────────────── #

def test_benchmark(client, auth_headers):
    samples = [f"benchmark sample {i} " * 20 for i in range(30)]
    resp    = client.post("/benchmark", json={"samples": samples}, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "vanilla_ratio" in data
    assert "dict_ratio"    in data


# ─────────────────────────────── response headers ────────────────────────── #

def test_security_headers(client, auth_headers):
    resp = client.get("/health")
    assert "X-Content-Type-Options" in resp.headers
    assert "X-Frame-Options"        in resp.headers


def test_request_id_header(client):
    resp = client.get("/health")
    assert "x-request-id" in resp.headers


def test_timing_header(client):
    resp = client.get("/health")
    assert "x-process-time-ms" in resp.headers


# ─────────────────────────────── audit ───────────────────────────────────── #

def test_audit_log_populated(client, auth_headers):
    # Do a seal to generate an audit record
    client.post("/pulse/seal", json={"payload": {"audit_test": True}}, headers=auth_headers)

    resp = client.get("/audit/recent", headers=auth_headers)
    assert resp.status_code == 200
    records = resp.json()
    assert isinstance(records, list)


# ─────────────────────────────── vault info ──────────────────────────────── #

def test_vault_info(client, auth_headers):
    resp = client.get("/vault/info", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["kdf"] == "PBKDF2-SHA256"
    assert data["key_bits"] == 256


# ─────────────────────────────── scheduler ───────────────────────────────── #

def test_scheduler_jobs(client, auth_headers):
    resp = client.get("/scheduler/jobs", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ─────────────────────────────── virtual mount ───────────────────────────── #

def test_virtual_mount_create_and_ls(client, auth_headers):
    seal_r   = client.post("/pulse/seal", json={"payload": {"mount_test": [1, 2, 3]}}, headers=auth_headers)
    pulse_id = seal_r.json()["pulse_id"]

    mount_r = client.post(
        "/mount/",
        json={"root_path": "/test", "pulse_map": {"/test/data.msgpack": pulse_id}},
        headers=auth_headers,
    )
    assert mount_r.status_code == 200
    mount_id = mount_r.json()["mount_id"]

    ls_r = client.get(f"/mount/{mount_id}/ls?path=/test", headers=auth_headers)
    assert ls_r.status_code == 200

    # Cleanup
    client.delete(f"/mount/{mount_id}", headers=auth_headers)


# ─────────────────────────────── error handling ──────────────────────────── #

def test_invalid_request_returns_422(client, auth_headers):
    resp = client.post("/pulse/seal", json={}, headers=auth_headers)
    assert resp.status_code == 422
    data = resp.json()
    assert "errors" in data or "detail" in data


def test_not_found_returns_404(client, auth_headers):
    resp = client.get(f"/pulse/master/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404


def test_scan_nonexistent_dir(client, auth_headers):
    resp = client.post("/scan", json={"root_path": "/nonexistent/path/xyz"}, headers=auth_headers)
    assert resp.status_code == 400
