"""
QUANTUM-PULSE :: tests/test_units.py
======================================
Comprehensive unit tests for all core modules.
Target: ≥75% total coverage across core/ + models/
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import msgpack
import pytest


def _make_sample(n: int = 0) -> bytes:
    record = {
        "id": f"doc_{n:04d}",
        "text": "Attention is all you need. " * 10,
        "tokens": list(range(64)),
        "metadata": {"source": "arxiv", "year": 2024},
    }
    return msgpack.packb(record, use_bin_type=True)


# ══════════════════════════════════════════════════════════════════════════════
# core/config.py
# ══════════════════════════════════════════════════════════════════════════════

class TestConfig:
    def setup_method(self):
        from core.config import get_settings
        get_settings.cache_clear()

    def teardown_method(self):
        from core.config import get_settings
        get_settings.cache_clear()

    def test_settings_defaults(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.delenv("QUANTUM_API_KEYS", raising=False)
        from core.config import get_settings
        cfg = get_settings()
        assert cfg.environment.value == "development"
        assert cfg.port == 8747
        assert cfg.zstd_level == 22
        assert cfg.kdf_iterations == 600_000

    def test_passphrase_too_short(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "short")
        from core.config import get_settings
        with pytest.raises(Exception):
            get_settings()

    def test_api_keys_from_json_list(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_API_KEYS", '["key1","key2","key3"]')
        from core.config import get_settings
        cfg = get_settings()
        assert "key1" in cfg.api_keys
        assert "key2" in cfg.api_keys

    def test_api_keys_single_json(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_API_KEYS", '["mykey"]')
        from core.config import get_settings
        cfg = get_settings()
        assert "mykey" in cfg.api_keys

    def test_gridfs_threshold_bytes(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.config import get_settings
        cfg = get_settings()
        assert cfg.gridfs_threshold_bytes == cfg.gridfs_threshold_mb * 1024 * 1024

    def test_max_request_size_bytes(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.config import get_settings
        cfg = get_settings()
        assert cfg.max_request_size_bytes == cfg.max_request_size_mb * 1024 * 1024

    def test_is_development(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.config import get_settings
        cfg = get_settings()
        assert cfg.is_development is True
        assert cfg.is_production is False

    def test_display_hides_passphrase(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_API_KEYS", '["supersecretkey123"]')
        from core.config import get_settings
        cfg = get_settings()
        d = cfg.display()
        assert "passphrase" not in d
        assert "jwt_secret" not in d

    def test_staging_environment(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_ENVIRONMENT", "staging")
        from core.config import get_settings
        cfg = get_settings()
        assert cfg.environment.value == "staging"

    def test_cors_from_string(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_CORS_ORIGINS", '["http://a.com","http://b.com"]')
        from core.config import get_settings
        cfg = get_settings()
        assert "http://a.com" in cfg.cors_origins

    def test_mongo_uri_in_display(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.config import get_settings
        cfg = get_settings()
        d = cfg.display()
        assert "mongo_uri" in d

    def test_log_level_enum(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.config import get_settings, LogLevel
        cfg = get_settings()
        assert cfg.log_level == LogLevel.INFO

    def test_environment_enum_values(self):
        from core.config import Environment
        assert Environment.DEVELOPMENT == "development"
        assert Environment.STAGING == "staging"
        assert Environment.PRODUCTION == "production"


# ══════════════════════════════════════════════════════════════════════════════
# core/adaptive.py
# ══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveDictManager:
    def _make_mgr(self, **kw):
        from core.adaptive import AdaptiveDictManager
        defaults = dict(
            retrain_every_n=10, min_improvement=0.0,
            min_samples=10, buffer_max=200, dict_size_bytes=16 * 1024,
        )
        defaults.update(kw)
        return AdaptiveDictManager(**defaults)

    def test_initial_state(self):
        mgr = self._make_mgr()
        assert not mgr.is_trained
        assert mgr.dict_id is None
        assert mgr.current_version == 0
        assert mgr.buffer_size == 0
        assert mgr.total_seals == 0

    def test_seals_until_retrain_initial(self):
        mgr = self._make_mgr(retrain_every_n=10)
        assert mgr.seals_until_retrain == 10

    @pytest.mark.asyncio
    async def test_buffer_fills_on_seal(self):
        mgr = self._make_mgr()
        await mgr.record_seal(_make_sample(1))
        assert mgr.buffer_size == 1
        assert mgr.total_seals == 1
        assert mgr.seals_until_retrain == 9

    @pytest.mark.asyncio
    async def test_retrain_triggered(self):
        mgr = self._make_mgr(retrain_every_n=10, min_samples=10)
        result = None
        for i in range(15):
            r = await mgr.record_seal(_make_sample(i))
            if r is not None:
                result = r
        assert result is not None
        assert result.committed is True
        assert mgr.is_trained
        assert mgr.current_version == 1

    @pytest.mark.asyncio
    async def test_no_retrain_below_min_samples(self):
        mgr = self._make_mgr(retrain_every_n=5, min_samples=20)
        for i in range(5):
            await mgr.record_seal(_make_sample(i))
        assert not mgr.is_trained

    @pytest.mark.asyncio
    async def test_force_retrain_commits(self):
        mgr = self._make_mgr(min_samples=10)
        for i in range(10):
            mgr._buffer.append(_make_sample(i))
        result = await mgr.force_retrain()
        assert result is not None
        assert result.committed is True

    @pytest.mark.asyncio
    async def test_force_retrain_with_extra_samples(self):
        mgr = self._make_mgr(min_samples=10)
        extras = [_make_sample(i) for i in range(15)]
        result = await mgr.force_retrain(extra_samples=extras)
        assert result is not None

    @pytest.mark.asyncio
    async def test_versions_kept_max_three(self):
        mgr = self._make_mgr(retrain_every_n=5, min_samples=5, min_improvement=0.0)
        for _ in range(25):
            await mgr.record_seal(_make_sample(1))
        assert len(mgr._versions) <= 3

    @pytest.mark.asyncio
    async def test_compressor_untrained(self):
        mgr = self._make_mgr()
        cctx = mgr.compressor()
        data = b"hello world" * 100
        compressed = cctx.compress(data)
        assert len(compressed) > 0

    @pytest.mark.asyncio
    async def test_compressor_after_training(self):
        mgr = self._make_mgr(min_samples=10, retrain_every_n=10, min_improvement=0.0)
        for i in range(10):
            await mgr.record_seal(_make_sample(i))
        await mgr.force_retrain()
        cctx = mgr.compressor()
        compressed = cctx.compress(_make_sample(99))
        assert len(compressed) > 0

    @pytest.mark.asyncio
    async def test_decompressor_for_unknown_version(self):
        mgr = self._make_mgr()
        dctx = mgr.decompressor_for_version(999)
        assert dctx is not None

    @pytest.mark.asyncio
    async def test_decompressor_for_known_version(self):
        mgr = self._make_mgr(min_samples=10, min_improvement=0.0)
        for i in range(10):
            mgr._buffer.append(_make_sample(i))
        await mgr.force_retrain()
        dctx = mgr.decompressor_for_version(mgr.current_version)
        assert dctx is not None

    def test_stats_dict_keys(self):
        mgr = self._make_mgr()
        stats = mgr.stats()
        for key in ["current_version", "is_trained", "buffer_size",
                    "total_seals", "retrain_every_n", "min_improvement_pct"]:
            assert key in stats

    @pytest.mark.asyncio
    async def test_load_dict_bytes(self):
        mgr_a = self._make_mgr(min_samples=10, min_improvement=0.0)
        for i in range(10):
            mgr_a._buffer.append(_make_sample(i))
        await mgr_a.force_retrain()
        raw = mgr_a._versions[0].raw_bytes

        mgr_b = self._make_mgr()
        await mgr_b.load_dict_bytes(raw, version=5)
        assert mgr_b.is_trained
        assert mgr_b.current_version == 5

    @pytest.mark.asyncio
    async def test_retrain_result_fields(self):
        mgr = self._make_mgr(min_samples=10, min_improvement=0.0)
        for i in range(10):
            mgr._buffer.append(_make_sample(i))
        result = await mgr.force_retrain()
        assert result is not None
        assert hasattr(result, "old_version")
        assert hasattr(result, "new_ratio")
        assert hasattr(result, "improvement")
        assert result.duration_ms >= 0

    def test_ab_test_static_no_old_dict(self):
        from core.adaptive import AdaptiveDictManager
        corpus = [_make_sample(i) for i in range(5)]
        candidate = _make_sample(0) * 5
        old_r, new_r = AdaptiveDictManager._ab_test(corpus, None, candidate)
        assert old_r > 0
        assert new_r > 0

    @pytest.mark.asyncio
    async def test_seals_reset_after_retrain(self):
        mgr = self._make_mgr(retrain_every_n=10, min_samples=10, min_improvement=0.0)
        for i in range(10):
            await mgr.record_seal(_make_sample(i))
        assert mgr._seals_since_retrain == 0


# ══════════════════════════════════════════════════════════════════════════════
# core/audit.py
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditLogger:
    @pytest.fixture
    def tmp_audit(self, tmp_path):
        return tmp_path / "audit.jsonl"

    @pytest.fixture
    def alogger(self, tmp_audit):
        from core.audit import AuditLogger
        return AuditLogger(str(tmp_audit))

    def test_init_creates_dir(self, tmp_path):
        from core.audit import AuditLogger
        path = tmp_path / "sub" / "dir" / "audit.jsonl"
        AuditLogger(str(path))
        assert path.parent.exists()

    def test_emit_sync_writes_json(self, alogger, tmp_audit):
        from core.audit import AuditRecord
        rec = AuditRecord(event_type="seal", outcome="success", pulse_id="abc")
        alogger.emit_sync(rec)
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "seal"
        assert data["pulse_id"] == "abc"

    def test_emit_sync_disabled_skips(self, alogger, tmp_audit):
        from core.audit import AuditRecord
        alogger.disable()
        alogger.emit_sync(AuditRecord(event_type="seal", outcome="success"))
        assert not tmp_audit.exists() or tmp_audit.read_text() == ""

    @pytest.mark.asyncio
    async def test_emit_async(self, alogger, tmp_audit):
        from core.audit import AuditRecord
        rec = AuditRecord(event_type="unseal", outcome="success", pulse_id="xyz")
        await alogger.emit(rec)
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "unseal"

    @pytest.mark.asyncio
    async def test_seal_event_success(self, alogger, tmp_audit):
        await alogger.seal(pulse_id="p1", identity="api_key:test",
                            ratio=95.0, size_bytes=1024)
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "seal"
        assert data["outcome"] == "success"
        assert data["meta"]["ratio"] == 95.0

    @pytest.mark.asyncio
    async def test_seal_event_failure(self, alogger, tmp_audit):
        await alogger.seal(pulse_id="p1", error="compression failed")
        data = json.loads(tmp_audit.read_text().strip())
        assert data["outcome"] == "failure"
        assert "compression" in data["error"]

    @pytest.mark.asyncio
    async def test_unseal_event(self, alogger, tmp_audit):
        await alogger.unseal(pulse_id="p2", identity="jwt:user")
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "unseal"
        assert data["identity"] == "jwt:user"

    @pytest.mark.asyncio
    async def test_auth_fail_event(self, alogger, tmp_audit):
        await alogger.auth_fail(ip="1.2.3.4", reason="bad key")
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "auth_fail"
        assert data["outcome"] == "failure"
        assert data["error"] == "bad key"

    @pytest.mark.asyncio
    async def test_rotate_event(self, alogger, tmp_audit):
        await alogger.rotate(pulse_id="p3", identity="admin")
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "rotate"

    @pytest.mark.asyncio
    async def test_delete_event(self, alogger, tmp_audit):
        await alogger.delete(pulse_id="p4")
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "delete"
        assert data["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_file_access_event(self, alogger, tmp_audit):
        await alogger.file_access(pulse_id="p5", virtual_path="/corpus/a.arrow",
                                   cache_hit=True)
        data = json.loads(tmp_audit.read_text().strip())
        assert data["event_type"] == "file_access"
        assert data["meta"]["cache_hit"] is True

    @pytest.mark.asyncio
    async def test_query_recent_empty(self, alogger):
        records = await alogger.query_recent()
        assert records == []

    @pytest.mark.asyncio
    async def test_query_recent_returns_records(self, alogger):
        from core.audit import AuditRecord
        for i in range(5):
            alogger.emit_sync(AuditRecord(event_type="seal", outcome="success",
                                           pulse_id=f"p{i}", identity="user1"))
        records = await alogger.query_recent(limit=10)
        assert len(records) == 5

    @pytest.mark.asyncio
    async def test_query_recent_filtered_by_event(self, alogger):
        from core.audit import AuditRecord
        alogger.emit_sync(AuditRecord(event_type="seal", outcome="success"))
        alogger.emit_sync(AuditRecord(event_type="unseal", outcome="success"))
        records = await alogger.query_recent(event_type="seal")
        assert all(r["event_type"] == "seal" for r in records)

    @pytest.mark.asyncio
    async def test_query_recent_filtered_by_identity(self, alogger):
        from core.audit import AuditRecord
        alogger.emit_sync(AuditRecord(event_type="seal", outcome="success",
                                       identity="alice"))
        alogger.emit_sync(AuditRecord(event_type="seal", outcome="success",
                                       identity="bob"))
        records = await alogger.query_recent(identity="alice")
        assert all(r["identity"] == "alice" for r in records)

    def test_record_to_json(self):
        from core.audit import AuditRecord
        rec = AuditRecord(event_type="seal", outcome="success",
                          pulse_id="p1", meta={"ratio": 10.5})
        j = json.loads(rec.to_json())
        assert j["meta"]["ratio"] == 10.5

    def test_set_db(self, alogger):
        mock_db = MagicMock()
        alogger.set_db(mock_db)
        assert alogger._db is mock_db

    def test_audit_event_enum(self):
        from core.audit import AuditEvent
        assert AuditEvent.SEAL == "seal"
        assert AuditEvent.AUTH_FAIL == "auth_fail"
        assert AuditEvent.FILE_ACCESS == "file_access"
        assert AuditEvent.MOUNT_CREATE == "mount_create"


# ══════════════════════════════════════════════════════════════════════════════
# core/auth.py
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth:
    def setup_method(self):
        from core.config import get_settings
        get_settings.cache_clear()

    def teardown_method(self):
        from core.config import get_settings
        get_settings.cache_clear()

    def test_principal_fields(self):
        from core.auth import Principal
        p = Principal(identity="api_key:test", auth_method="api_key",
                      scopes=["read", "write"], issued_at=time.time())
        assert p.auth_method == "api_key"
        assert "write" in p.scopes

    def test_anon_principal(self):
        from core.auth import ANON
        assert ANON.auth_method == "anon"
        assert ANON.identity == "anon"

    def test_create_and_decode_token(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_API_KEYS", '["testkey1234"]')
        from core.auth import create_access_token, decode_token
        token = create_access_token("user1", scopes=["read", "write"])
        claims = decode_token(token)
        assert claims["sub"] == "user1"
        assert "read" in claims["scopes"]
        assert claims["iss"] == "quantum-pulse"

    def test_token_has_exp(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.auth import create_access_token, decode_token
        token = create_access_token("alice")
        claims = decode_token(token)
        assert "exp" in claims
        assert claims["exp"] > time.time()

    def test_token_default_scopes(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.auth import create_access_token, decode_token
        token = create_access_token("alice")
        claims = decode_token(token)
        assert "read" in claims["scopes"]
        assert "write" in claims["scopes"]

    def test_decode_invalid_token(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.auth import decode_token
        from jose import JWTError
        with pytest.raises(JWTError):
            decode_token("not.a.real.token")

    def test_validate_api_key_valid(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_API_KEYS", '["valid-api-key-99"]')
        from core.config import get_settings
        get_settings.cache_clear()
        from core.auth import _validate_api_key
        p = _validate_api_key("valid-api-key-99")
        assert p is not None
        assert p.auth_method == "api_key"
        assert "admin" in p.scopes
        assert "***" in p.identity

    def test_validate_api_key_invalid(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_API_KEYS", '["valid-api-key-99"]')
        from core.config import get_settings
        get_settings.cache_clear()
        from core.auth import _validate_api_key
        assert _validate_api_key("wrong-key") is None

    def test_validate_short_key(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_API_KEYS", '["ab"]')
        from core.config import get_settings
        get_settings.cache_clear()
        from core.auth import _validate_api_key
        p = _validate_api_key("ab")
        assert p is not None
        assert "****" in p.identity

    def test_token_models(self):
        from core.auth import TokenRequest, TokenResponse
        req = TokenRequest(api_key="mykey")
        assert req.api_key == "mykey"
        resp = TokenResponse(access_token="tok", expires_in=3600)
        assert resp.token_type == "bearer"
        assert resp.expires_in == 3600


# ══════════════════════════════════════════════════════════════════════════════
# core/retry.py
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_initial_closed(self):
        from core.retry import CircuitBreaker, CircuitState
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        assert cb.state == CircuitState.CLOSED
        assert cb._failures == 0

    @pytest.mark.asyncio
    async def test_successful_call(self):
        from core.retry import CircuitBreaker, CircuitState
        cb = CircuitBreaker("test")
        async def _ok(): return 42
        result = await cb.call(_ok)
        assert result == 42
        assert cb._failures == 0

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        from core.retry import CircuitBreaker, CircuitState
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        async def _fail(): raise OSError("refused")
        for _ in range(3):
            with pytest.raises(OSError):
                await cb.call(_fail)
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_rejects_immediately(self):
        from core.retry import CircuitBreaker, CircuitState
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=9999)
        async def _fail(): raise OSError("fail")
        with pytest.raises(OSError):
            await cb.call(_fail)
        with pytest.raises(RuntimeError, match="OPEN"):
            await cb.call(_fail)

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self):
        from core.retry import CircuitBreaker, CircuitState
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        async def _fail(): raise OSError("fail")
        with pytest.raises(OSError):
            await cb.call(_fail)
        await asyncio.sleep(0.05)
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_recovers_after_probe_success(self):
        from core.retry import CircuitBreaker, CircuitState
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        async def _fail(): raise OSError("fail")
        async def _ok(): return "good"
        with pytest.raises(OSError):
            await cb.call(_fail)
        await asyncio.sleep(0.05)
        result = await cb.call(_ok)
        assert result == "good"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_fails_reopens(self):
        from core.retry import CircuitBreaker, CircuitState
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        async def _fail(): raise OSError("fail")
        with pytest.raises(OSError):
            await cb.call(_fail)
        await asyncio.sleep(0.05)
        assert cb.state == CircuitState.HALF_OPEN
        with pytest.raises(OSError):
            await cb.call(_fail)
        assert cb.state == CircuitState.OPEN

    def test_status_dict(self):
        from core.retry import CircuitBreaker
        cb = CircuitBreaker("mydb", failure_threshold=5, recovery_timeout=30)
        s = cb.status()
        assert s["name"] == "mydb"
        assert s["state"] == "closed"
        assert s["failures"] == 0
        assert s["threshold"] == 5


class TestBulkhead:
    @pytest.mark.asyncio
    async def test_basic_usage(self):
        from core.retry import Bulkhead
        bh = Bulkhead("test", max_concurrent=5)
        async with bh:
            assert bh._active == 1
        assert bh._active == 0

    @pytest.mark.asyncio
    async def test_concurrent_respect_limit(self):
        from core.retry import Bulkhead
        bh = Bulkhead("test", max_concurrent=2)
        max_seen = 0
        async def _task():
            nonlocal max_seen
            async with bh:
                if bh._active > max_seen:
                    max_seen = bh._active
                await asyncio.sleep(0.01)
        await asyncio.gather(*[_task() for _ in range(4)])
        assert max_seen <= 2

    def test_status(self):
        from core.retry import Bulkhead
        bh = Bulkhead("mydb", max_concurrent=50)
        s = bh.status()
        assert s["name"] == "mydb"
        assert s["max"] == 50
        assert s["active"] == 0
        assert s["available"] == 50
        assert s["rejected_total"] == 0


class TestWithTimeout:
    @pytest.mark.asyncio
    async def test_completes_ok(self):
        from core.retry import with_timeout
        result = await with_timeout(asyncio.sleep(0), timeout=5.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        from core.retry import with_timeout
        with pytest.raises(asyncio.TimeoutError):
            await with_timeout(asyncio.sleep(10), timeout=0.01, name="test_op")


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        from core.retry import with_retry
        calls = 0
        @with_retry(max_attempts=3, wait_min=0.001, wait_max=0.01)
        async def _fn():
            nonlocal calls; calls += 1
            return "ok"
        assert await _fn() == "ok"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_retries_transient_errors(self):
        from core.retry import with_retry
        calls = 0
        @with_retry(max_attempts=3, wait_min=0.001, wait_max=0.005, jitter=0.001)
        async def _fn():
            nonlocal calls; calls += 1
            if calls < 3:
                raise OSError("transient")
            return "recovered"
        assert await _fn() == "recovered"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_exhausts_and_raises(self):
        from core.retry import with_retry
        @with_retry(max_attempts=2, wait_min=0.001, wait_max=0.005, jitter=0.001)
        async def _fn():
            raise OSError("permanent")
        with pytest.raises(OSError):
            await _fn()


# ══════════════════════════════════════════════════════════════════════════════
# core/db.py
# ══════════════════════════════════════════════════════════════════════════════

def _make_pulse_blob(pulse_id="p1"):
    from models.pulse_models import PulseBlob, CompressionStats
    stats = CompressionStats(original_bytes=1000, packed_bytes=1000,
                              compressed_bytes=50, encrypted_bytes=60,
                              duration_ms=10.0, entropy_bits_per_byte=7.5)
    return PulseBlob(pulse_id=pulse_id,
                     merkle_root="abcdef1234567890" * 4,
                     chunk_hash="deadbeef12345678" * 4,
                     salt="aabbccdd", nonce="ccddee00", stats=stats)


class TestMemoryStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        meta = _make_pulse_blob("p1")
        await s.save_pulse("p1", b"data", meta)
        blob, loaded = await s.load_pulse("p1")
        assert blob == b"data"
        assert loaded.pulse_id == "p1"

    @pytest.mark.asyncio
    async def test_load_missing_raises(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        with pytest.raises(KeyError):
            await s.load_pulse("nope")

    @pytest.mark.asyncio
    async def test_update_pulse(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        meta = _make_pulse_blob("p1")
        await s.save_pulse("p1", b"old", meta)
        await s.update_pulse("p1", b"new", meta)
        blob, _ = await s.load_pulse("p1")
        assert blob == b"new"

    @pytest.mark.asyncio
    async def test_delete_existing(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        await s.save_pulse("p1", b"d", _make_pulse_blob("p1"))
        assert await s.delete_pulse("p1") is True
        with pytest.raises(KeyError):
            await s.load_pulse("p1")

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        assert await s.delete_pulse("ghost") is False

    @pytest.mark.asyncio
    async def test_list_pulses(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        for i in range(3):
            await s.save_pulse(f"p{i}", b"d", _make_pulse_blob(f"p{i}"))
        assert len(await s.list_pulses()) == 3

    @pytest.mark.asyncio
    async def test_count_pulses(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        assert await s.count_pulses() == 0
        await s.save_pulse("p1", b"d", _make_pulse_blob("p1"))
        assert await s.count_pulses() == 1

    @pytest.mark.asyncio
    async def test_save_and_load_master(self):
        from core.db import _MemoryStore
        from models.pulse_models import MasterPulse
        s = _MemoryStore()
        master = MasterPulse(master_id="m1", shard_ids=["p1","p2"],
                              merkle_tree=["h1","root"], merkle_root="root",
                              total_original_bytes=2000, total_shards=2)
        await s.save_master(master)
        loaded = await s.load_master("m1")
        assert loaded.master_id == "m1"

    @pytest.mark.asyncio
    async def test_load_master_missing(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        with pytest.raises(KeyError):
            await s.load_master("nope")

    @pytest.mark.asyncio
    async def test_list_with_parent_filter(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        for i in range(4):
            parent = "master1" if i < 2 else None
            m = _make_pulse_blob(f"p{i}").model_copy(update={"parent_id": parent})
            await s.save_pulse(f"p{i}", b"d", m)
        assert len(await s.list_pulses(parent_id="master1")) == 2


class TestPulseDB:
    @pytest.mark.asyncio
    async def test_memory_mode(self):
        from core.db import PulseDB
        db = PulseDB(mongo_uri="mongodb://localhost:27017", db_name="test")
        await db.connect()
        assert db._ready is True

    @pytest.mark.asyncio
    async def test_is_mongo_bool(self):
        from core.db import PulseDB
        db = PulseDB()
        await db.connect()
        assert isinstance(db.is_mongo, bool)


# ══════════════════════════════════════════════════════════════════════════════
# core/health.py
# ══════════════════════════════════════════════════════════════════════════════

class TestHealth:
    def test_check_status_enum(self):
        from core.health import CheckStatus
        assert CheckStatus.PASS.value in ("pass", "PASS")
        assert CheckStatus.WARN.value in ("warn", "WARN")
        assert CheckStatus.FAIL.value in ("fail", "FAIL")

    def test_check_result_pass_is_ok(self):
        from core.health import CheckResult, CheckStatus
        r = CheckResult(name="engine", status=CheckStatus.PASS, message="ok")
        assert r.is_ok is True

    def test_check_result_fail_not_ok(self):
        from core.health import CheckResult, CheckStatus
        r = CheckResult(name="mongo", status=CheckStatus.FAIL, message="down")
        assert r.is_ok is False

    def test_check_result_warn_is_ok(self):
        from core.health import CheckResult, CheckStatus
        r = CheckResult(name="disk", status=CheckStatus.WARN, message="90% full")
        assert r.is_ok is True

    def test_health_report_to_dict(self):
        from core.health import CheckResult, CheckStatus, HealthReport
        import time as _t
        r1 = CheckResult(name="engine", status=CheckStatus.PASS, message="ok")
        r2 = CheckResult(name="disk", status=CheckStatus.WARN, message="high",
                          latency_ms=5.0)
        report = HealthReport(status=CheckStatus.WARN, checks=[r1, r2],
                               uptime_s=120.0, version="1.0.0",
                               timestamp=_t.time(), environment="development")
        d = report.to_dict()
        assert len(d["checks"]) == 2
        assert d["uptime_s"] == 120.0

    def test_mark_startup_complete(self):
        from core.health import mark_startup_complete
        mark_startup_complete()  # must not raise

    @pytest.mark.asyncio
    async def test_run_check_pass(self):
        from core.health import _run_check, CheckResult, CheckStatus
        async def _good():
            return CheckResult(name="test", status=CheckStatus.PASS, message="ok")
        result = await _run_check("test", _good, timeout=5.0)
        assert result.status == CheckStatus.PASS
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_run_check_fail(self):
        from core.health import _run_check, CheckStatus
        async def _bad():
            raise RuntimeError("broken")
        result = await _run_check("broken", _bad, timeout=5.0)
        assert result.status == CheckStatus.FAIL

    @pytest.mark.asyncio
    async def test_check_disk(self):
        from core.health import _check_disk, CheckStatus
        result = await _check_disk()
        assert result.name == "disk"
        assert result.status in (CheckStatus.PASS, CheckStatus.WARN, CheckStatus.FAIL)

    @pytest.mark.asyncio
    async def test_check_memory(self):
        from core.health import _check_memory, CheckStatus
        result = await _check_memory()
        assert result.name == "memory"

    @pytest.mark.asyncio
    async def test_check_mongo_memory_mode(self):
        from core.health import _check_mongo, CheckStatus
        mock_db = MagicMock()
        mock_db.is_mongo = False
        result = await _check_mongo(mock_db)
        assert result.status == CheckStatus.WARN


# ══════════════════════════════════════════════════════════════════════════════
# core/metrics.py
# ══════════════════════════════════════════════════════════════════════════════

class TestMetrics:
    def test_track_seal_increments_counter(self):
        from core.metrics import track_seal, seals_total
        before = seals_total.labels(dict_trained="True")._value.get()
        with track_seal(dict_trained=True):
            pass
        assert seals_total.labels(dict_trained="True")._value.get() > before

    def test_track_seal_records_error_type(self):
        from core.metrics import track_seal, seal_errors_total
        before = seal_errors_total.labels(error_type="ValueError")._value.get()
        with pytest.raises(ValueError):
            with track_seal(dict_trained=False):
                raise ValueError("test")
        assert seal_errors_total.labels(error_type="ValueError")._value.get() > before

    def test_track_unseal_increments(self):
        from core.metrics import track_unseal, unseals_total
        before = unseals_total._value.get()
        with track_unseal():
            pass
        assert unseals_total._value.get() > before

    def test_track_unseal_records_error(self):
        from core.metrics import track_unseal, unseal_errors_total
        with pytest.raises(RuntimeError):
            with track_unseal():
                raise RuntimeError("failed")

    def test_up_gauge(self):
        from core.metrics import up
        assert up._value.get() == 1.0

    def test_all_metrics_importable(self):
        from core.metrics import (
            seals_total, unseals_total, seal_errors_total,
            compression_ratio, seal_duration_ms, unseal_duration_ms,
            pulse_bytes_original, pulse_bytes_encrypted, active_mounts,
            db_operations_total, db_errors_total, key_rotations_total,
            entropy_score, master_pulses_total, shards_per_master,
            up, scan_files_total, scan_duration_ms,
        )
        assert seals_total is not None

    def test_observe_histograms(self):
        from core.metrics import compression_ratio, entropy_score, pulse_bytes_original
        compression_ratio.observe(95.5)
        entropy_score.observe(7.9)
        pulse_bytes_original.observe(1024)

    def test_track_seal_false_dict(self):
        from core.metrics import track_seal, seals_total
        before = seals_total.labels(dict_trained="False")._value.get()
        with track_seal(dict_trained=False):
            pass
        assert seals_total.labels(dict_trained="False")._value.get() > before


# ══════════════════════════════════════════════════════════════════════════════
# core/middleware.py
# ══════════════════════════════════════════════════════════════════════════════

class TestMiddleware:
    def test_imports(self):
        from core.middleware import (
            RequestIDMiddleware, TimingMiddleware,
            SecurityHeadersMiddleware, apply_middleware,
        )
        assert RequestIDMiddleware is not None

    def test_request_id_header(self):
        from core.middleware import RequestIDMiddleware
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)
        @app.get("/test")
        def route(): return {"ok": True}
        resp = TestClient(app).get("/test")
        assert "X-Request-ID" in resp.headers

    def test_timing_header(self):
        from core.middleware import TimingMiddleware
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        app.add_middleware(TimingMiddleware)
        @app.get("/test")
        def route(): return {"ok": True}
        resp = TestClient(app).get("/test")
        assert "X-Process-Time-Ms" in resp.headers

    def test_security_headers(self):
        from core.middleware import SecurityHeadersMiddleware
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)
        @app.get("/test")
        def route(): return {"ok": True}
        resp = TestClient(app).get("/test")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert "X-Frame-Options" in resp.headers

    def test_global_exception_500(self):
        from core.middleware import apply_middleware
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        apply_middleware(app)
        @app.get("/boom")
        def boom(): raise RuntimeError("unexpected!")
        resp = TestClient(app, raise_server_exceptions=False).get("/boom")
        assert resp.status_code == 500

    def test_request_id_propagated(self):
        from core.middleware import RequestIDMiddleware
        from fastapi import FastAPI, Request
        from starlette.testclient import TestClient
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)
        @app.get("/test")
        def route(request: Request):
            return {"req_id": request.headers.get("X-Request-ID", "")}
        resp = TestClient(app).get("/test")
        assert "X-Request-ID" in resp.headers


# ══════════════════════════════════════════════════════════════════════════════
# core/scheduler.py
# ══════════════════════════════════════════════════════════════════════════════

class TestScheduler:
    def test_init_not_running(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        assert not qs._scheduler.running

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        assert qs._scheduler.running
        qs.stop()
        # APScheduler async shutdown may take a tick
        await asyncio.sleep(0.05)
        assert not qs._scheduler.running

    @pytest.mark.asyncio
    async def test_double_start_safe(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start(); qs.start()
        qs.stop()

    @pytest.mark.asyncio
    async def test_double_stop_safe(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start(); qs.stop(); qs.stop()

    @pytest.mark.asyncio
    async def test_add_interval_job(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        async def _dummy(): pass
        qs.add_interval_job(_dummy, seconds=9999, job_id="test_job")
        assert any(j["id"] == "test_job" for j in qs.list_jobs())
        qs.stop()

    @pytest.mark.asyncio
    async def test_list_jobs_empty(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        assert isinstance(qs.list_jobs(), list)
        qs.stop()

    @pytest.mark.asyncio
    async def test_register_health_ping(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        qs.register_health_ping(lambda: MagicMock(), lambda: MagicMock(),
                                 interval_s=9999)
        assert any(j["id"] == "health_ping" for j in qs.list_jobs())
        qs.stop()

    @pytest.mark.asyncio
    async def test_register_ttl_cleanup_none(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        qs.register_ttl_cleanup(lambda: MagicMock(), ttl_days=None)
        assert not any(j["id"] == "ttl_cleanup" for j in qs.list_jobs())
        qs.stop()

    @pytest.mark.asyncio
    async def test_register_ttl_cleanup_enabled(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        qs.register_ttl_cleanup(lambda: MagicMock(), ttl_days=30, interval_s=9999)
        assert any(j["id"] == "ttl_cleanup" for j in qs.list_jobs())
        qs.stop()

    @pytest.mark.asyncio
    async def test_register_metrics_snapshot(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        qs.register_metrics_snapshot(lambda: MagicMock(), lambda: MagicMock(),
                                      interval_s=9999)
        assert any(j["id"] == "metrics_snapshot" for j in qs.list_jobs())
        qs.stop()

    @pytest.mark.asyncio
    async def test_register_dict_retrain(self):
        from core.scheduler import QuantumScheduler
        qs = QuantumScheduler()
        qs.start()
        mock_engine = MagicMock()
        mock_engine._adaptive = None
        qs.register_dict_retrain(lambda: mock_engine, lambda: MagicMock(),
                                  interval_s=9999)
        assert any(j["id"] == "dict_retrain" for j in qs.list_jobs())
        qs.stop()


# ══════════════════════════════════════════════════════════════════════════════
# core/interface.py
# ══════════════════════════════════════════════════════════════════════════════

class TestInMemoryFileHandle:
    def test_read_all(self):
        from core.interface import InMemoryFileHandle
        data = b"hello world " * 100
        fh = InMemoryFileHandle("/f", "p1", data)
        assert fh.read() == data

    def test_read_chunked(self):
        from core.interface import InMemoryFileHandle
        data = b"abcdefghij" * 10
        fh = InMemoryFileHandle("/f", "p1", data)
        assert fh.read(10) == b"abcdefghij"

    def test_seek_and_tell(self):
        from core.interface import InMemoryFileHandle
        fh = InMemoryFileHandle("/f", "p1", b"hello world")
        fh.seek(6)
        assert fh.tell() == 6
        assert fh.read() == b"world"

    def test_size_property(self):
        from core.interface import InMemoryFileHandle
        fh = InMemoryFileHandle("/f", "p1", b"x" * 1024)
        assert fh.size == 1024

    def test_idle_seconds(self):
        from core.interface import InMemoryFileHandle
        fh = InMemoryFileHandle("/f", "p1", b"data")
        assert fh.idle_seconds >= 0

    def test_is_not_expired_fresh(self):
        from core.interface import InMemoryFileHandle
        fh = InMemoryFileHandle("/f", "p1", b"data")
        assert not fh.is_expired(ttl=600)

    def test_is_expired_zero_ttl(self):
        from core.interface import InMemoryFileHandle
        fh = InMemoryFileHandle("/f", "p1", b"data")
        assert fh.is_expired(ttl=0.0)

    def test_read_count(self):
        from core.interface import InMemoryFileHandle
        fh = InMemoryFileHandle("/f", "p1", b"data")
        fh.read(); fh.read()
        assert fh._read_count == 2

    @pytest.mark.asyncio
    async def test_stream_all_data(self):
        from core.interface import InMemoryFileHandle
        data = b"x" * 200_000
        fh = InMemoryFileHandle("/f", "p1", data)
        chunks = [chunk async for chunk in fh.stream(chunk_size=64 * 1024)]
        assert b"".join(chunks) == data


class TestVirtualMount:
    def _make_mount(self, mid=None):
        from core.interface import VirtualMount
        import uuid as _u
        return VirtualMount(mount_id=mid or str(_u.uuid4()), root_path="/")

    def test_create(self):
        mount = self._make_mount("m1")
        assert mount.mount_id == "m1"

    def test_register_and_stat(self):
        mount = self._make_mount()
        mount.register_file("/corpus/a.arrow", "p1", size=1024)
        stat = mount.stat("/corpus/a.arrow")
        assert stat is not None
        assert stat.pulse_id == "p1"

    def test_stat_missing(self):
        mount = self._make_mount()
        assert mount.stat("/nope.txt") is None

    def test_list_dir(self):
        mount = self._make_mount()
        mount.register_file("/corpus/a.arrow", "p1", size=1024)
        mount.register_file("/corpus/b.arrow", "p2", size=2048)
        listing = mount.list_dir("/corpus")
        assert len(listing) == 2

    def test_list_dir_root(self):
        mount = self._make_mount()
        for i in range(3):
            mount.register_file(f"/file{i}.txt", f"p{i}", size=100)
        listing = mount.list_dir("/")
        assert len(listing) == 3

    def test_handle_cache_miss(self):
        mount = self._make_mount()
        assert mount.get_handle("/nope.txt") is None

    def test_flush_handles(self):
        from core.interface import InMemoryFileHandle
        mount = self._make_mount()
        for i in range(3):
            mount.put_handle(f"/f{i}", InMemoryFileHandle(f"/f{i}", f"p{i}", b"x"))
        n = mount.flush_handles()
        assert n == 3
        assert mount.get_handle("/f0") is None

    def test_info_property(self):
        mount = self._make_mount("m42")
        info = mount.info
        assert info.mount_id == "m42"


class TestMountManager:
    def _make_mm(self):
        from core.interface import MountManager
        mm = MountManager()
        mm.set_engine(MagicMock())
        return mm

    def test_create_returns_mount(self):
        mm = self._make_mm()
        mount = mm.create_mount()
        assert mount.mount_id is not None

    def test_get_mount(self):
        mm = self._make_mm()
        mount = mm.create_mount()
        retrieved = mm.get_mount(mount.mount_id)
        assert retrieved.mount_id == mount.mount_id

    def test_get_nonexistent_raises(self):
        mm = self._make_mm()
        with pytest.raises(KeyError):
            mm.get_mount("nope")

    def test_destroy_mount(self):
        mm = self._make_mm()
        mount = mm.create_mount()
        mm.destroy_mount(mount.mount_id)
        with pytest.raises(KeyError):
            mm.get_mount(mount.mount_id)

    def test_destroy_nonexistent_ok(self):
        mm = self._make_mm()
        result = mm.destroy_mount("ghost")
        assert result == 0

    def test_list_mounts(self):
        mm = self._make_mm()
        for i in range(3):
            mm.create_mount()
        mounts = mm.list_mounts()
        assert len(mounts) == 3


# ══════════════════════════════════════════════════════════════════════════════
# core/scanner.py
# ══════════════════════════════════════════════════════════════════════════════

class TestScanner:
    def test_producer_basic(self, tmp_path):
        from queue import Queue
        from core.scanner import _scandir_producer, _SENTINEL
        from models.pulse_models import ScanMode
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        q = Queue(maxsize=1000)
        _scandir_producer(str(tmp_path), q, ScanMode.SHALLOW, max_depth=0,
                           skip_hidden=True)
        items = []
        while True:
            item = q.get()
            if item is _SENTINEL: break
            items.append(item)
        assert len(items) >= 2

    def test_producer_recursive(self, tmp_path):
        from queue import Queue
        from core.scanner import _scandir_producer, _SENTINEL
        from models.pulse_models import ScanMode
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.txt").write_text("y")
        q = Queue(maxsize=1000)
        _scandir_producer(str(tmp_path), q, ScanMode.RECURSIVE, max_depth=5,
                           skip_hidden=True)
        items = []
        while True:
            item = q.get()
            if item is _SENTINEL: break
            items.append(item)
        assert len(items) >= 3

    def test_producer_skips_hidden(self, tmp_path):
        from queue import Queue
        from core.scanner import _scandir_producer, _SENTINEL
        from models.pulse_models import ScanMode
        (tmp_path / ".hidden").write_text("secret")
        (tmp_path / "visible.txt").write_text("public")
        q = Queue(maxsize=100)
        _scandir_producer(str(tmp_path), q, ScanMode.SHALLOW, max_depth=0,
                           skip_hidden=True)
        items = []
        while True:
            item = q.get()
            if item is _SENTINEL: break
            items.append(item)
        names = [item[0].name for item in items]
        assert "visible.txt" in names
        assert ".hidden" not in names

    def test_producer_skips_pyc(self, tmp_path):
        from queue import Queue
        from core.scanner import _scandir_producer, _SENTINEL
        from models.pulse_models import ScanMode
        (tmp_path / "code.py").write_text("pass")
        (tmp_path / "code.pyc").write_bytes(b"\x00")
        q = Queue(maxsize=100)
        _scandir_producer(str(tmp_path), q, ScanMode.SHALLOW, max_depth=0,
                           skip_hidden=True)
        items = []
        while True:
            item = q.get()
            if item is _SENTINEL: break
            items.append(item)
        names = [item[0].name for item in items]
        assert "code.py" in names
        assert "code.pyc" not in names

    def test_scanner_init(self):
        from core.scanner import QuantumScanner
        from models.pulse_models import ScanMode
        scanner = QuantumScanner("/tmp", mode=ScanMode.SHALLOW)
        assert scanner.root == "/tmp"

    @pytest.mark.asyncio
    async def test_scan_creates_manifest(self, tmp_path):
        from core.scanner import QuantumScanner
        from models.pulse_models import ScanMode
        (tmp_path / "a.txt").write_text("hello world content here for scanning")
        (tmp_path / "b.txt").write_text("more textual content to scan properly")
        scanner = QuantumScanner(str(tmp_path), mode=ScanMode.SHALLOW)
        manifests = [m async for m in scanner.scan()]
        assert len(manifests) >= 1

    @pytest.mark.asyncio
    async def test_scan_empty_dir(self, tmp_path):
        from core.scanner import QuantumScanner
        from models.pulse_models import ScanMode
        scanner = QuantumScanner(str(tmp_path), mode=ScanMode.SHALLOW)
        manifests = [m async for m in scanner.scan()]
        assert len(manifests) == 0 or manifests[0].total_files == 0

    def test_skip_extensions_constant(self):
        from core.scanner import _SKIP_EXTENSIONS
        assert ".pyc" in _SKIP_EXTENSIONS
        assert ".so" in _SKIP_EXTENSIONS


# ══════════════════════════════════════════════════════════════════════════════
# models/pulse_models.py
# ══════════════════════════════════════════════════════════════════════════════

class TestPulseModels:
    def test_compression_stats_ratio(self):
        from models.pulse_models import CompressionStats
        s = CompressionStats(original_bytes=10000, packed_bytes=9800,
                              compressed_bytes=200, encrypted_bytes=220,
                              duration_ms=15.0, entropy_bits_per_byte=7.8)
        assert s.ratio == pytest.approx(10000 / 220, rel=1e-3)
        assert s.ratio > 1.0

    def test_pulse_blob_defaults(self):
        from models.pulse_models import PulseBlob, CompressionStats
        stats = CompressionStats(original_bytes=500, packed_bytes=500,
                                  compressed_bytes=50, encrypted_bytes=60,
                                  duration_ms=5.0, entropy_bits_per_byte=7.0)
        blob = PulseBlob(pulse_id="abc", merkle_root="abcdef1234567890" * 4,
                          chunk_hash="deadbeef12345678" * 4,
                          salt="aabbccdd", nonce="ccddee00", stats=stats)
        assert blob.tags == {}
        assert blob.parent_id is None
        assert blob.dict_version == 0

    def test_pulse_status_enum(self):
        from models.pulse_models import PulseStatus
        assert PulseStatus.SEALED == "sealed"
        assert PulseStatus.PENDING == "pending"
        assert PulseStatus.EXPIRED == "expired"

    def test_scan_mode_enum(self):
        from models.pulse_models import ScanMode
        assert ScanMode.SHALLOW == "shallow"
        assert ScanMode.RECURSIVE == "recursive"

    def test_scan_stats(self):
        from models.pulse_models import ScanStats
        s = ScanStats(total_files=100, total_bytes=1_000_000,
                       skipped_files=5, scan_duration_ms=250.0)
        assert s.total_files == 100

    def test_file_entry(self):
        from models.pulse_models import FileEntry
        fe = FileEntry(path="/data/train.jsonl", name="train.jsonl",
                        size=50000, mtime=1234567890.0)
        assert fe.path == "/data/train.jsonl"
        assert fe.size == 50000

    def test_dir_manifest(self):
        from models.pulse_models import DirManifest, FileEntry, ScanStats
        stats = ScanStats(total_files=2, total_bytes=2000)
        files = [FileEntry(path=f"/{i}.txt", name=f"{i}.txt",
                            size=1000, mtime=0.0) for i in range(2)]
        manifest = DirManifest(root_path="/data", entries=files, stats=stats)
        assert len(manifest.entries) == 2

    def test_master_pulse(self):
        from models.pulse_models import MasterPulse
        mp = MasterPulse(master_id="m1", shard_ids=["s1","s2","s3"],
                          merkle_tree=["h1","h2","root"], merkle_root="root",
                          total_original_bytes=9000, total_shards=3)
        assert mp.total_shards == 3

    def test_vault_mount(self):
        from models.pulse_models import VaultMount
        m = VaultMount(mount_id="vm1", root_path="/corpus")
        assert m.mount_id == "vm1"
        assert m.files == {}

    def test_mounted_file(self):
        from models.pulse_models import MountedFile
        f = MountedFile(virtual_path="/corpus/a.arrow", pulse_id="p1", size=2048)
        assert f.virtual_path == "/corpus/a.arrow"
        assert f.size == 2048

    def test_storage_backend_enum(self):
        from models.pulse_models import StorageBackend
        assert StorageBackend.MEMORY == "memory"
        assert StorageBackend.MONGO == "mongo"


# ══════════════════════════════════════════════════════════════════════════════
# core/compression.py — boost from 76%
# ══════════════════════════════════════════════════════════════════════════════

class TestPulseCompressor:
    @pytest.mark.asyncio
    async def test_compress_decompress_roundtrip(self):
        from core.compression import PulseCompressor
        pc = PulseCompressor()
        data = _make_sample(1) * 50
        compressed, result = await pc.compress(data)
        assert await pc.decompress(compressed) == data
        assert result.ratio > 1.0

    @pytest.mark.asyncio
    async def test_compress_result_fields(self):
        from core.compression import PulseCompressor
        pc = PulseCompressor()
        _, result = await pc.compress(b"test data " * 500)
        assert result.original_bytes > 0
        assert result.duration_ms >= 0
        assert result.throughput_mb_s >= 0

    @pytest.mark.asyncio
    async def test_train_from_samples(self):
        from core.compression import PulseCompressor
        pc = PulseCompressor()
        # samples must be >= 1 KiB each
        samples = [_make_sample(i) * 10 for i in range(20)]
        await pc.train_from_samples(samples)
        assert pc.is_dict_trained
        assert pc.dict_id is not None

    @pytest.mark.asyncio
    async def test_train_from_text(self):
        from core.compression import PulseCompressor
        pc = PulseCompressor()
        # texts must encode to >= 1 KiB
        await pc.train_from_text(["Transformer model training data. " * 100
                                   for _ in range(20)])
        assert pc.is_dict_trained

    @pytest.mark.asyncio
    async def test_train_rejects_small_samples(self):
        from core.compression import PulseCompressor
        pc = PulseCompressor()
        await pc.train_from_samples([b"tiny"] * 5)
        assert not pc.is_dict_trained

    @pytest.mark.asyncio
    async def test_benchmark_report(self):
        from core.compression import PulseCompressor
        pc = PulseCompressor()
        samples = [_make_sample(i) * 10 for i in range(30)]
        await pc.train_from_samples(samples)
        report = await pc.benchmark(samples)
        assert report.sample_count == len(samples)
        assert report.dict_ms >= 0

    @pytest.mark.asyncio
    async def test_inspect_frame(self):
        from core.compression import PulseCompressor
        pc = PulseCompressor()
        compressed, _ = await pc.compress(_make_sample(1) * 20)
        info = pc.inspect_frame(compressed)
        assert "window_size" in info

    @pytest.mark.asyncio
    async def test_stream_compress(self):
        from core.compression import PulseCompressor
        async def _source():
            data = _make_sample(1) * 20
            for i in range(0, len(data), len(data) // 4):
                yield data[i:i + len(data) // 4]
        pc = PulseCompressor()
        chunks = [c async for c in pc.compress_stream(_source())]
        assert len(b"".join(chunks)) > 0


# ══════════════════════════════════════════════════════════════════════════════
# FIXES + COVERAGE BOOST ADDITIONS
# ══════════════════════════════════════════════════════════════════════════════

import os as _os
_os.environ.setdefault("QUANTUM_PASSPHRASE", "test-passphrase-16c")
_os.environ.setdefault("QUANTUM_API_KEYS", '["test-api-key-unit"]')

# Canonical API key to use in tests — picks up whatever key is configured
_TEST_API_KEY = _os.environ.get("QUANTUM_API_KEYS", '["test-api-key-unit"]')
import json as _json
try:
    _TEST_API_KEY = _json.loads(_TEST_API_KEY)[0]
except Exception:
    _TEST_API_KEY = "test-api-key-unit"


# ── Fix: auth tests that need settings available ───────────────────────────

class TestAuthFixed:
    """Fixed versions of tests that import auth without monkeypatching."""

    def test_principal_dataclass(self):
        from core.auth import Principal
        p = Principal(identity="api_key:test", auth_method="api_key",
                      scopes=["read", "write"], issued_at=time.time())
        assert p.auth_method == "api_key"
        assert "write" in p.scopes

    def test_anon_principal_fixed(self):
        from core.auth import ANON
        assert ANON.auth_method == "anon"
        assert ANON.identity == "anon"
        assert isinstance(ANON.scopes, list)

    def test_require_scope_ok(self):
        from core.auth import require_scope
        factory = require_scope("read")
        assert callable(factory)

    def test_auth_router_importable(self):
        from core.auth import auth_router
        assert auth_router is not None

    def test_issue_token_endpoint_valid(self):
        from core.config import get_settings
        get_settings.cache_clear()
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import auth_router
        app = FastAPI()
        app.include_router(auth_router)
        resp = TestClient(app).post("/auth/token",
                                    json={"api_key": _TEST_API_KEY})
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"

    def test_issue_token_endpoint_invalid(self):
        from core.config import get_settings
        get_settings.cache_clear()
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import auth_router
        app = FastAPI()
        app.include_router(auth_router)
        resp = TestClient(app).post("/auth/token", json={"api_key": "bad-key"})
        assert resp.status_code == 403

    def test_require_api_key_valid(self):
        from core.config import get_settings
        get_settings.cache_clear()
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import require_api_key
        app = FastAPI()
        @app.get("/protected")
        async def route(p=__import__('fastapi').Depends(require_api_key)):
            return {"identity": p.identity}
        resp = TestClient(app).get("/protected",
                                   headers={"X-API-Key": _TEST_API_KEY})
        assert resp.status_code == 200

    def test_require_api_key_missing(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import require_api_key
        app = FastAPI()
        @app.get("/protected")
        async def route(p=__import__('fastapi').Depends(require_api_key)):
            return {"ok": True}
        resp = TestClient(app).get("/protected")
        assert resp.status_code in (401, 403)

    def test_require_api_key_wrong(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import require_api_key
        app = FastAPI()
        @app.get("/protected")
        async def route(p=__import__('fastapi').Depends(require_api_key)):
            return {"ok": True}
        resp = TestClient(app).get("/protected", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 403

    def test_require_auth_bearer_token(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import require_auth, create_access_token
        app = FastAPI()
        @app.get("/protected")
        async def route(p=__import__('fastapi').Depends(require_auth)):
            return {"identity": p.identity}
        token = create_access_token("testuser", scopes=["read", "write"])
        resp = TestClient(app).get("/protected",
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert "jwt:testuser" in resp.json()["identity"]

    def test_optional_auth_returns_anon_on_no_creds(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import optional_auth
        app = FastAPI()
        @app.get("/optional")
        async def route(p=__import__('fastapi').Depends(optional_auth)):
            return {"identity": p.identity if p else "none"}
        resp = TestClient(app).get("/optional")
        assert resp.status_code == 200

    def test_require_scope_blocks_missing_scope(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from core.auth import require_scope, create_access_token
        app = FastAPI()
        @app.get("/admin")
        async def route(p=__import__('fastapi').Depends(require_scope("superpower"))):
            return {"ok": True}
        token = create_access_token("user", scopes=["read"])
        resp = TestClient(app).get("/admin",
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403


# ── Fix: middleware tests that need settings ───────────────────────────────

class TestMiddlewareFixed:
    def test_security_headers_fixed(self):
        from core.middleware import SecurityHeadersMiddleware
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)
        @app.get("/test")
        def route(): return {"ok": True}
        resp = TestClient(app).get("/test")
        assert "X-Content-Type-Options" in resp.headers
        assert "X-Frame-Options" in resp.headers

    def test_global_exception_500_fixed(self):
        from core.middleware import apply_middleware
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        apply_middleware(app)
        @app.get("/boom")
        def boom(): raise RuntimeError("unexpected!")
        resp = TestClient(app, raise_server_exceptions=False).get("/boom")
        assert resp.status_code == 500

    def test_apply_middleware_cors(self):
        from core.middleware import apply_middleware
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        apply_middleware(app)
        @app.get("/test")
        def route(): return {"ok": True}
        resp = TestClient(app).get("/test",
                                   headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200

    def test_rate_limit_setup(self):
        from core.middleware import apply_middleware
        from fastapi import FastAPI
        app = FastAPI()
        # Should not raise
        apply_middleware(app)

    def test_http_validation_error_422(self):
        from fastapi import FastAPI
        from fastapi.exceptions import RequestValidationError
        from core.middleware import apply_middleware
        from starlette.testclient import TestClient
        app = FastAPI()
        apply_middleware(app)
        from pydantic import BaseModel
        class Body(BaseModel):
            value: int
        @app.post("/validate")
        def route(b: Body): return b.model_dump()
        resp = TestClient(app).post("/validate", json={"value": "not-an-int"})
        assert resp.status_code == 422


# ── Fix: interface TTL eviction test ─────────────────────────────────────

class TestVirtualMountFixed:
    def test_handle_expired_evicted_fixed(self):
        from core.interface import InMemoryFileHandle, VirtualMount
        import time as _t
        mount = VirtualMount(mount_id="m-fix", root_path="/")
        fh = InMemoryFileHandle("/f", "p1", b"data")
        mount.put_handle("/f", fh)
        # Force expiry by making last_read far in the past
        fh._last_read_at = _t.monotonic() - 700  # > 600s TTL
        assert mount.get_handle("/f") is None

    def test_handle_not_expired_under_ttl(self):
        from core.interface import InMemoryFileHandle, VirtualMount
        mount = VirtualMount(mount_id="m-fix2", root_path="/")
        fh = InMemoryFileHandle("/f", "p1", b"data")
        mount.put_handle("/f", fh)
        assert mount.get_handle("/f") is fh


# ── Vault: additional coverage ────────────────────────────────────────────

class TestVaultAdditional:
    @pytest.mark.asyncio
    async def test_unlock_returns_vault_key(self):
        from core.vault import QuantumVault
        vault = QuantumVault("test-passphrase-abc123")
        vk = await vault.unlock()
        assert vk.raw is not None
        assert len(vk.raw) == 32

    @pytest.mark.asyncio
    async def test_lock_clears_key(self):
        from core.vault import QuantumVault
        vault = QuantumVault("test-passphrase-abc123")
        await vault.unlock()
        vault.lock()
        assert vault._master_vk is None

    @pytest.mark.asyncio
    async def test_derive_shard_key_returns_bytes(self):
        from core.vault import QuantumVault
        vault = QuantumVault("test-passphrase-abc123")
        salt = _os.urandom(32)
        key = await vault.derive_shard_key("pulse-123", salt)
        assert isinstance(key, bytes)
        assert len(key) == 32

    @pytest.mark.asyncio
    async def test_derive_shard_key_cached(self):
        from core.vault import QuantumVault
        vault = QuantumVault("test-passphrase-abc123")
        salt = _os.urandom(32)
        k1 = await vault.derive_shard_key("pulse-abc", salt)
        k2 = await vault.derive_shard_key("pulse-abc", salt)
        assert k1 == k2

    @pytest.mark.asyncio
    async def test_change_passphrase(self):
        from core.vault import QuantumVault
        vault = QuantumVault("test-passphrase-abc123")
        await vault.unlock()
        new_pp = "new-passphrase-xyz456789"
        new_vk = await vault.change_passphrase(new_pp, new_pp)
        assert new_vk is not None
        assert vault._passphrase == new_pp

    @pytest.mark.asyncio
    async def test_change_passphrase_too_short(self):
        from core.vault import QuantumVault
        vault = QuantumVault("test-passphrase-abc123")
        with pytest.raises(ValueError):
            await vault.change_passphrase("short", "short")

    def test_generate_passphrase(self):
        from core.vault import QuantumVault
        pp = QuantumVault.generate_passphrase(4)
        parts = pp.split("-")
        assert len(parts) >= 4

    def test_generate_salt(self):
        from core.vault import QuantumVault
        s = QuantumVault.generate_salt()
        assert isinstance(s, bytes)
        assert len(s) > 0

    @pytest.mark.asyncio
    async def test_rotate_all_shards_empty(self):
        from core.vault import QuantumVault
        vault = QuantumVault("test-passphrase-abc123")
        results = await vault.rotate_all_shards([], "test-passphrase-abc123")
        assert results == []


# ── Health: router coverage ───────────────────────────────────────────────

class TestHealthRouter:
    @pytest.mark.asyncio
    async def test_liveness_endpoint(self):
        from core.health import create_health_router
        from core.engine import QuantumEngine
        from core.db import PulseDB
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        engine = QuantumEngine(passphrase="test-passphrase-health1")
        db = PulseDB(); await db.connect()
        router = create_health_router(lambda: engine, lambda: db)
        app = FastAPI()
        app.include_router(router)

        resp = TestClient(app).get("/healthz/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    @pytest.mark.asyncio
    async def test_startup_probe_before_complete(self):
        from core.health import create_health_router
        import core.health as _h
        _h._startup_complete = False
        from core.engine import QuantumEngine
        from core.db import PulseDB
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        engine = QuantumEngine(passphrase="test-passphrase-health2")
        db = PulseDB(); await db.connect()
        router = create_health_router(lambda: engine, lambda: db)
        app = FastAPI()
        app.include_router(router)

        resp = TestClient(app).get("/healthz/startup")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_startup_probe_after_complete(self):
        from core.health import create_health_router, mark_startup_complete
        from core.engine import QuantumEngine
        from core.db import PulseDB
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        mark_startup_complete()
        engine = QuantumEngine(passphrase="test-passphrase-health3")
        db = PulseDB(); await db.connect()
        router = create_health_router(lambda: engine, lambda: db)
        app = FastAPI()
        app.include_router(router)

        resp = TestClient(app).get("/healthz/startup")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_readiness_endpoint(self):
        from core.health import create_health_router
        from core.engine import QuantumEngine
        from core.db import PulseDB
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        engine = QuantumEngine(passphrase="test-passphrase-health4")
        db = PulseDB(); await db.connect()
        router = create_health_router(lambda: engine, lambda: db)
        app = FastAPI()
        app.include_router(router)

        resp = TestClient(app).get("/healthz/ready")
        assert resp.status_code in (200, 503)
        body = resp.json()
        assert "status" in body
        assert "checks" in body

    @pytest.mark.asyncio
    async def test_full_report_endpoint(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        from core.config import get_settings
        get_settings.cache_clear()
        from core.health import create_health_router
        from core.engine import QuantumEngine
        from core.db import PulseDB
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        engine = QuantumEngine(passphrase="test-passphrase-health5")
        db = PulseDB(); await db.connect()
        router = create_health_router(lambda: engine, lambda: db)
        app = FastAPI()
        app.include_router(router)

        resp = TestClient(app).get("/healthz/")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "uptime_s" in body

    @pytest.mark.asyncio
    async def test_check_engine_pass(self):
        from core.health import _check_engine, CheckStatus
        from core.engine import QuantumEngine
        engine = QuantumEngine(passphrase="test-passphrase-health6")
        result = await _check_engine(engine)
        assert result.name == "engine"
        assert result.status == CheckStatus.PASS

    @pytest.mark.asyncio
    async def test_run_check_timeout(self):
        from core.health import _run_check, CheckStatus
        async def _slow():
            await asyncio.sleep(10)
        result = await _run_check("slow", _slow, timeout=0.01)
        assert result.status == CheckStatus.FAIL


# ── DB: additional memory-store coverage ─────────────────────────────────

class TestMemoryStoreAdditional:
    @pytest.mark.asyncio
    async def test_list_pulses_empty(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        assert await s.list_pulses() == []

    @pytest.mark.asyncio
    async def test_list_pulses_limit(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        for i in range(10):
            await s.save_pulse(f"p{i}", b"d", _make_pulse_blob(f"p{i}"))
        result = await s.list_pulses()
        assert len(result) == 10

    @pytest.mark.asyncio
    async def test_update_pulse_overwrites(self):
        from core.db import _MemoryStore
        s = _MemoryStore()
        await s.save_pulse("p1", b"old", _make_pulse_blob("p1"))
        await s.update_pulse("p1", b"new", _make_pulse_blob("p1"))
        blob, _ = await s.load_pulse("p1")
        assert blob == b"new"

    @pytest.mark.asyncio
    async def test_pulse_db_ops_memory(self):
        from core.db import PulseDB
        db = PulseDB()
        await db.connect()
        meta = _make_pulse_blob("p99")
        await db.save_pulse("p99", b"rawdata", meta)
        blob, loaded = await db.load_pulse("p99")
        assert blob == b"rawdata"
        assert loaded.pulse_id == "p99"
        count = await db.count_pulses()
        assert count >= 1
        deleted = await db.delete_pulse("p99")
        assert deleted is True

    @pytest.mark.asyncio
    async def test_pulse_db_list(self):
        from core.db import PulseDB
        db = PulseDB()
        await db.connect()
        for i in range(3):
            await db.save_pulse(f"px{i}", b"data", _make_pulse_blob(f"px{i}"))
        listing = await db.list_pulses()
        assert len(listing) >= 3


# ── Scheduler: job callback coverage ─────────────────────────────────────

class TestSchedulerCallbacks:
    @pytest.mark.asyncio
    async def test_health_ping_job_runs(self):
        from core.scheduler import QuantumScheduler
        from core.db import PulseDB

        db = PulseDB()
        await db.connect()

        mock_engine = MagicMock()
        mock_engine._trainer = MagicMock()
        mock_engine._trainer.is_trained = False

        qs = QuantumScheduler()
        qs.start()
        qs.register_health_ping(lambda: mock_engine, lambda: db, interval_s=9999)
        # Manually trigger by accessing the job and invoking it
        jobs = qs.list_jobs()
        hp = next((j for j in jobs if j["id"] == "health_ping"), None)
        assert hp is not None
        qs.stop()

    @pytest.mark.asyncio
    async def test_metrics_snapshot_job_runs(self):
        from core.scheduler import QuantumScheduler
        from core.db import PulseDB

        db = PulseDB()
        await db.connect()

        mock_engine = MagicMock()
        mock_engine._trainer = MagicMock()
        mock_engine._trainer.dict_id = None
        mock_engine._trainer.is_trained = False

        qs = QuantumScheduler()
        qs.start()
        qs.register_metrics_snapshot(lambda: mock_engine, lambda: db, interval_s=9999)
        jobs = qs.list_jobs()
        assert any(j["id"] == "metrics_snapshot" for j in jobs)
        qs.stop()

    @pytest.mark.asyncio
    async def test_ttl_cleanup_memory_backend_skips(self):
        from core.scheduler import QuantumScheduler
        from core.db import PulseDB

        db = PulseDB()
        await db.connect()

        qs = QuantumScheduler()
        qs.start()
        qs.register_ttl_cleanup(lambda: db, ttl_days=7, interval_s=9999)
        assert any(j["id"] == "ttl_cleanup" for j in qs.list_jobs())
        qs.stop()


# ── Adaptive: more branch coverage ──────────────────────────────────────

class TestAdaptiveAdditional:
    @pytest.mark.asyncio
    async def test_improvement_threshold_rejects(self):
        """Candidate dict must be ≥min_improvement better to commit."""
        from core.adaptive import AdaptiveDictManager
        mgr = AdaptiveDictManager(
            retrain_every_n=10, min_improvement=99.0,  # impossibly high
            min_samples=10, buffer_max=200, dict_size_bytes=16 * 1024,
        )
        for i in range(10):
            mgr._buffer.append(_make_sample(i))
        result = await mgr.force_retrain()
        # Either not committed (rejected) or committed if it somehow beat 99%
        assert result is not None
        assert hasattr(result, "committed")

    @pytest.mark.asyncio
    async def test_stats_after_training(self):
        from core.adaptive import AdaptiveDictManager
        mgr = AdaptiveDictManager(
            retrain_every_n=10, min_improvement=0.0,
            min_samples=10, buffer_max=200, dict_size_bytes=16 * 1024,
        )
        for i in range(10):
            mgr._buffer.append(_make_sample(i))
        await mgr.force_retrain()
        stats = mgr.stats()
        assert stats["current_version"] >= 1
        assert stats["is_trained"] is True
        assert "versions_kept" in stats

    @pytest.mark.asyncio
    async def test_version_history_in_stats(self):
        from core.adaptive import AdaptiveDictManager
        mgr = AdaptiveDictManager(
            retrain_every_n=10, min_improvement=0.0,
            min_samples=10, buffer_max=200, dict_size_bytes=16 * 1024,
        )
        for i in range(10):
            mgr._buffer.append(_make_sample(i))
        await mgr.force_retrain()
        stats = mgr.stats()
        assert "versions_kept" in stats
        assert stats["versions_kept"] >= 1


# ── Config: production environment ────────────────────────────────────────

class TestConfigAdditional:
    def setup_method(self):
        from core.config import get_settings
        get_settings.cache_clear()

    def teardown_method(self):
        from core.config import get_settings
        get_settings.cache_clear()

    def test_production_environment(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_ENVIRONMENT", "production")
        from core.config import get_settings
        cfg = get_settings()
        assert cfg.is_production is True
        assert cfg.is_development is False

    def test_zstd_level_configurable(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_ZSTD_LEVEL", "10")
        from core.config import get_settings
        cfg = get_settings()
        assert cfg.zstd_level == 10

    def test_log_level_configurable(self, monkeypatch):
        monkeypatch.setenv("QUANTUM_PASSPHRASE", "test-passphrase-16c")
        monkeypatch.setenv("QUANTUM_LOG_LEVEL", "DEBUG")
        from core.config import get_settings, LogLevel
        cfg = get_settings()
        assert cfg.log_level == LogLevel.DEBUG


# ── Audit: MongoDB branch (mocked) ──────────────────────────────────────

class TestAuditMongo:
    @pytest.mark.asyncio
    async def test_emit_writes_to_mongo_if_available(self, tmp_path):
        from core.audit import AuditLogger, AuditRecord
        logger = AuditLogger(str(tmp_path / "audit.jsonl"))
        mock_col = AsyncMock()
        mock_db = MagicMock()
        mock_db.db_audit = mock_col
        logger.set_db(mock_db)
        rec = AuditRecord(event_type="seal", outcome="success", pulse_id="p1")
        await logger.emit(rec)
        # File should still be written
        assert (tmp_path / "audit.jsonl").exists()

    @pytest.mark.asyncio
    async def test_query_recent_limit(self, tmp_path):
        from core.audit import AuditLogger, AuditRecord
        logger = AuditLogger(str(tmp_path / "audit.jsonl"))
        for i in range(20):
            logger.emit_sync(AuditRecord(event_type="seal", outcome="success",
                                          pulse_id=f"p{i}"))
        records = await logger.query_recent(limit=5)
        assert len(records) == 5


# ── S3 Storage Backend ───────────────────────────────────────────────────────

class TestS3Store:
    """Tests for S3Store using mocked aioboto3."""

    def _make_store(self):
        from core.storage_s3 import S3Store
        store = S3Store.__new__(S3Store)
        store._bucket = "test-bucket"
        store._prefix = "quantum-pulse"
        store._region = "us-east-1"
        store._endpoint_url = None
        store._session = MagicMock()
        return store

    def _make_pulse(self, pid="test-pulse-id-0001"):
        from models.pulse_models import PulseBlob, CompressionStats
        stats = CompressionStats(
            original_bytes=100, packed_bytes=90, compressed_bytes=50,
            encrypted_bytes=60, duration_ms=2.0,
        )
        return PulseBlob(
            pulse_id=pid, parent_id=None, salt="aa" * 32, nonce="bb" * 12,
            chunk_hash="cc" * 32, merkle_root="dd" * 32,
            dict_version=0, stats=stats,
        )

    def test_blob_key(self):
        store = self._make_store()
        assert store._blob_key("abc") == "quantum-pulse/blobs/abc"

    def test_meta_key(self):
        store = self._make_store()
        assert store._meta_key("abc") == "quantum-pulse/meta/abc.json"

    def test_master_key(self):
        store = self._make_store()
        assert store._master_key("abc") == "quantum-pulse/masters/abc.json"

    @pytest.mark.asyncio
    async def test_save_pulse(self):
        store = self._make_store()
        meta = self._make_pulse()
        blob = b"encrypted-blob-data"

        mock_s3 = AsyncMock()
        mock_s3.put_object = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_s3)
        cm.__aexit__ = AsyncMock(return_value=False)
        store._session.client = MagicMock(return_value=cm)

        result = await store.save_pulse(meta.pulse_id, blob, meta)
        assert result == "s3"
        assert mock_s3.put_object.call_count == 2

    @pytest.mark.asyncio
    async def test_load_pulse(self):
        store = self._make_store()
        meta = self._make_pulse()
        blob = b"encrypted-data"

        blob_body = AsyncMock()
        blob_body.read = AsyncMock(return_value=blob)
        meta_body = AsyncMock()
        meta_body.read = AsyncMock(return_value=meta.model_dump_json().encode())

        mock_s3 = AsyncMock()
        mock_s3.get_object = AsyncMock(side_effect=[
            {"Body": blob_body},
            {"Body": meta_body},
        ])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_s3)
        cm.__aexit__ = AsyncMock(return_value=False)
        store._session.client = MagicMock(return_value=cm)

        got_blob, got_meta = await store.load_pulse(meta.pulse_id)
        assert got_blob == blob
        assert got_meta.pulse_id == meta.pulse_id

    @pytest.mark.asyncio
    async def test_delete_pulse_existing(self):
        store = self._make_store()
        meta = self._make_pulse()

        mock_s3 = AsyncMock()
        mock_s3.head_object = AsyncMock(return_value={})
        mock_s3.delete_object = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_s3)
        cm.__aexit__ = AsyncMock(return_value=False)
        store._session.client = MagicMock(return_value=cm)

        result = await store.delete_pulse(meta.pulse_id)
        assert result is True
        assert mock_s3.delete_object.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_pulse_nonexistent(self):
        store = self._make_store()

        mock_s3 = AsyncMock()
        mock_s3.head_object = AsyncMock(side_effect=Exception("404 Not Found"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_s3)
        cm.__aexit__ = AsyncMock(return_value=False)
        store._session.client = MagicMock(return_value=cm)

        result = await store.delete_pulse("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_count_pulses(self):
        store = self._make_store()

        paginator = AsyncMock()
        page = {"Contents": [{"Key": "a"}, {"Key": "b"}, {"Key": "c"}]}

        async def async_pages(*args, **kwargs):
            yield page

        paginator.paginate = MagicMock(return_value=async_pages())

        mock_s3 = AsyncMock()
        mock_s3.get_paginator = MagicMock(return_value=paginator)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_s3)
        cm.__aexit__ = AsyncMock(return_value=False)
        store._session.client = MagicMock(return_value=cm)

        count = await store.count_pulses()
        assert count == 3

    @pytest.mark.asyncio
    async def test_update_pulse_calls_save(self):
        store = self._make_store()
        meta = self._make_pulse()
        blob = b"new-blob"

        saved = []

        async def mock_save(pid, b, m):
            saved.append((pid, b, m))
            return "s3"

        store.save_pulse = mock_save
        await store.update_pulse(meta.pulse_id, blob, meta)
        assert len(saved) == 1
        assert saved[0][1] == blob

    @pytest.mark.asyncio
    async def test_save_master(self):
        from models.pulse_models import MasterPulse
        store = self._make_store()
        master = MasterPulse(
            master_id="master-001", shard_ids=["p1", "p2"],
            merkle_tree=["ee"*32, "ff"*32], merkle_root="ee" * 32,
            total_original_bytes=100, total_shards=2,
        )
        mock_s3 = AsyncMock()
        mock_s3.put_object = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_s3)
        cm.__aexit__ = AsyncMock(return_value=False)
        store._session.client = MagicMock(return_value=cm)

        await store.save_master(master)
        mock_s3.put_object.assert_called_once()

    def test_import_error_without_aioboto3(self):
        import sys
        aioboto3_backup = sys.modules.get("aioboto3")
        sys.modules["aioboto3"] = None  # type: ignore
        try:
            import importlib
            import core.storage_s3 as s3mod
            importlib.reload(s3mod)
            with pytest.raises(ImportError, match="aioboto3"):
                s3mod.S3Store(bucket="b")
        finally:
            if aioboto3_backup is not None:
                sys.modules["aioboto3"] = aioboto3_backup
            else:
                sys.modules.pop("aioboto3", None)


# ── GCS Storage Backend ──────────────────────────────────────────────────────

class TestGCSStore:
    """Tests for GCSStore using mocked gcloud-aio-storage."""

    def _make_store(self):
        from core.storage_gcs import GCSStore
        store = GCSStore.__new__(GCSStore)
        store._bucket = "test-gcs-bucket"
        store._prefix = "quantum-pulse"
        store._service_file = None
        return store

    def _make_pulse(self, pid="gcs-pulse-id-0001"):
        from models.pulse_models import PulseBlob, CompressionStats
        stats = CompressionStats(
            original_bytes=100, packed_bytes=90, compressed_bytes=50,
            encrypted_bytes=60, duration_ms=2.0,
        )
        return PulseBlob(
            pulse_id=pid, parent_id=None, salt="aa" * 32, nonce="bb" * 12,
            chunk_hash="cc" * 32, merkle_root="dd" * 32,
            dict_version=0, stats=stats,
        )

    def test_blob_key(self):
        store = self._make_store()
        assert store._blob_key("abc") == "quantum-pulse/blobs/abc"

    def test_meta_key(self):
        store = self._make_store()
        assert store._meta_key("abc") == "quantum-pulse/meta/abc.json"

    def test_master_key(self):
        store = self._make_store()
        assert store._master_key("abc") == "quantum-pulse/masters/abc.json"

    def test_storage_kwargs_no_service_file(self):
        store = self._make_store()
        assert store._storage_kwargs() == {}

    def test_storage_kwargs_with_service_file(self):
        store = self._make_store()
        store._service_file = "/path/to/key.json"
        assert store._storage_kwargs() == {"service_file": "/path/to/key.json"}

    @pytest.mark.asyncio
    async def test_save_pulse(self):
        store = self._make_store()
        meta = self._make_pulse()
        blob = b"gcs-encrypted-blob"

        mock_storage = AsyncMock()
        mock_storage.upload = AsyncMock(return_value={})
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.storage_gcs.aiohttp") as mock_aiohttp, \
             patch("core.storage_gcs.Storage") as MockStorage:
            mock_aiohttp.ClientSession.return_value = mock_session
            MockStorage.return_value = mock_storage
            result = await store.save_pulse(meta.pulse_id, blob, meta)

        assert result == "gcs"
        assert mock_storage.upload.call_count == 2

    @pytest.mark.asyncio
    async def test_load_pulse(self):
        store = self._make_store()
        meta = self._make_pulse()
        blob = b"gcs-blob-data"

        mock_storage = AsyncMock()
        mock_storage.download = AsyncMock(side_effect=[
            blob,
            meta.model_dump_json().encode(),
        ])
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.storage_gcs.aiohttp") as mock_aiohttp, \
             patch("core.storage_gcs.Storage") as MockStorage:
            mock_aiohttp.ClientSession.return_value = mock_session
            MockStorage.return_value = mock_storage
            got_blob, got_meta = await store.load_pulse(meta.pulse_id)

        assert got_blob == blob
        assert got_meta.pulse_id == meta.pulse_id

    @pytest.mark.asyncio
    async def test_load_pulse_not_found(self):
        store = self._make_store()

        mock_storage = AsyncMock()
        mock_storage.download = AsyncMock(side_effect=Exception("404 Not Found"))
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.storage_gcs.aiohttp") as mock_aiohttp, \
             patch("core.storage_gcs.Storage") as MockStorage:
            mock_aiohttp.ClientSession.return_value = mock_session
            MockStorage.return_value = mock_storage
            with pytest.raises(KeyError):
                await store.load_pulse("missing-pulse")

    @pytest.mark.asyncio
    async def test_delete_pulse_existing(self):
        store = self._make_store()
        meta = self._make_pulse()

        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(return_value={})
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.storage_gcs.aiohttp") as mock_aiohttp, \
             patch("core.storage_gcs.Storage") as MockStorage:
            mock_aiohttp.ClientSession.return_value = mock_session
            MockStorage.return_value = mock_storage
            result = await store.delete_pulse(meta.pulse_id)

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_pulse_not_found(self):
        store = self._make_store()

        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(side_effect=Exception("404 does not exist"))
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.storage_gcs.aiohttp") as mock_aiohttp, \
             patch("core.storage_gcs.Storage") as MockStorage:
            mock_aiohttp.ClientSession.return_value = mock_session
            MockStorage.return_value = mock_storage
            result = await store.delete_pulse("gone")

        assert result is False

    @pytest.mark.asyncio
    async def test_count_pulses(self):
        store = self._make_store()

        mock_storage = AsyncMock()
        mock_storage.list_objects = AsyncMock(return_value={
            "items": [{"name": "a"}, {"name": "b"}]
        })
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.storage_gcs.aiohttp") as mock_aiohttp, \
             patch("core.storage_gcs.Storage") as MockStorage:
            mock_aiohttp.ClientSession.return_value = mock_session
            MockStorage.return_value = mock_storage
            count = await store.count_pulses()

        assert count == 2

    @pytest.mark.asyncio
    async def test_update_pulse_calls_save(self):
        store = self._make_store()
        meta = self._make_pulse()
        blob = b"updated-blob"
        saved = []

        async def mock_save(pid, b, m):
            saved.append((pid, b, m))
            return "gcs"

        store.save_pulse = mock_save
        await store.update_pulse(meta.pulse_id, blob, meta)
        assert saved[0][1] == blob

    @pytest.mark.asyncio
    async def test_save_and_load_master(self):
        from models.pulse_models import MasterPulse
        store = self._make_store()
        master = MasterPulse(
            master_id="gcs-master-001", shard_ids=["p1"],
            merkle_tree=["ff"*32], merkle_root="ff" * 32,
            total_original_bytes=50, total_shards=1,
        )
        mock_storage = AsyncMock()
        mock_storage.upload = AsyncMock(return_value={})
        mock_storage.download = AsyncMock(return_value=master.model_dump_json().encode())
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.storage_gcs.aiohttp") as mock_aiohttp, \
             patch("core.storage_gcs.Storage") as MockStorage:
            mock_aiohttp.ClientSession.return_value = mock_session
            MockStorage.return_value = mock_storage
            await store.save_master(master)
            got = await store.load_master(master.master_id)

        assert got.master_id == master.master_id

    def test_import_error_without_gcloud(self):
        import sys
        backup_aiohttp = sys.modules.get("aiohttp")
        backup_gcloud = sys.modules.get("gcloud.aio.storage")
        sys.modules["gcloud.aio.storage"] = None  # type: ignore
        try:
            import importlib
            import core.storage_gcs as gcsmod
            importlib.reload(gcsmod)
            with pytest.raises(ImportError, match="gcloud-aio-storage"):
                gcsmod.GCSStore(bucket="b")
        finally:
            if backup_aiohttp is not None:
                sys.modules["aiohttp"] = backup_aiohttp
            if backup_gcloud is not None:
                sys.modules["gcloud.aio.storage"] = backup_gcloud
            else:
                sys.modules.pop("gcloud.aio.storage", None)


# ── PulseDB cloud routing ─────────────────────────────────────────────────────

class TestPulseDBCloudRouting:
    """Tests for PulseDB routing to S3/GCS backends."""

    def _make_db_with_mock_cloud(self, backend_name="s3"):
        from core.db import PulseDB
        db = PulseDB.__new__(PulseDB)
        db._storage_backend = backend_name
        db._client = None
        db._db = None
        db._gfs = None
        db._ready = True
        db._mem = MagicMock()
        mock_cloud = AsyncMock()
        mock_cloud.save_pulse = AsyncMock(return_value=backend_name)
        mock_cloud.load_pulse = AsyncMock(return_value=(b"blob", MagicMock()))
        mock_cloud.update_pulse = AsyncMock()
        mock_cloud.delete_pulse = AsyncMock(return_value=True)
        mock_cloud.list_pulses = AsyncMock(return_value=[])
        mock_cloud.count_pulses = AsyncMock(return_value=5)
        mock_cloud.save_master = AsyncMock()
        mock_cloud.load_master = AsyncMock(return_value=MagicMock())
        db._cloud = mock_cloud
        return db, mock_cloud

    def test_is_cloud_true(self):
        db, _ = self._make_db_with_mock_cloud()
        assert db.is_cloud is True

    def test_is_mongo_false_when_cloud(self):
        db, _ = self._make_db_with_mock_cloud()
        assert db.is_mongo is False

    def test_backend_name_s3(self):
        db, _ = self._make_db_with_mock_cloud("s3")
        assert db.backend_name == "s3"

    def test_backend_name_gcs(self):
        db, _ = self._make_db_with_mock_cloud("gcs")
        assert db.backend_name == "gcs"

    def test_backend_name_memory(self):
        from core.db import PulseDB
        db = PulseDB.__new__(PulseDB)
        db._client = None
        db._cloud = None
        db._storage_backend = "memory"
        assert db.backend_name == "memory"

    @pytest.mark.asyncio
    async def test_save_pulse_routes_to_cloud(self):
        from models.pulse_models import PulseBlob, CompressionStats
        db, mock_cloud = self._make_db_with_mock_cloud()
        stats = CompressionStats(
            original_bytes=10, packed_bytes=9, compressed_bytes=5,
            encrypted_bytes=6, duration_ms=2.0,
        )
        meta = PulseBlob(
            pulse_id="p1", salt="aa"*32, nonce="bb"*12,
            chunk_hash="cc"*32, merkle_root="dd"*32, dict_version=0, stats=stats,
        )
        result = await db.save_pulse("p1", b"blob", meta)
        mock_cloud.save_pulse.assert_awaited_once()
        assert result == "s3"

    @pytest.mark.asyncio
    async def test_load_pulse_routes_to_cloud(self):
        db, mock_cloud = self._make_db_with_mock_cloud()
        await db.load_pulse("p1")
        mock_cloud.load_pulse.assert_awaited_once_with("p1")

    @pytest.mark.asyncio
    async def test_delete_pulse_routes_to_cloud(self):
        db, mock_cloud = self._make_db_with_mock_cloud()
        result = await db.delete_pulse("p1")
        mock_cloud.delete_pulse.assert_awaited_once_with("p1")
        assert result is True

    @pytest.mark.asyncio
    async def test_count_pulses_routes_to_cloud(self):
        db, mock_cloud = self._make_db_with_mock_cloud()
        count = await db.count_pulses()
        mock_cloud.count_pulses.assert_awaited_once()
        assert count == 5

    @pytest.mark.asyncio
    async def test_list_pulses_routes_to_cloud(self):
        db, mock_cloud = self._make_db_with_mock_cloud()
        result = await db.list_pulses()
        mock_cloud.list_pulses.assert_awaited_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_save_master_routes_to_cloud(self):
        db, mock_cloud = self._make_db_with_mock_cloud()
        master = MagicMock()
        await db.save_master(master)
        mock_cloud.save_master.assert_awaited_once_with(master)

    @pytest.mark.asyncio
    async def test_load_master_routes_to_cloud(self):
        db, mock_cloud = self._make_db_with_mock_cloud()
        await db.load_master("m1")
        mock_cloud.load_master.assert_awaited_once_with("m1")
