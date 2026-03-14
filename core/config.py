"""
QUANTUM-PULSE :: core/config.py
=================================
Single source of truth for all configuration.

• Pydantic Settings V2 — reads from environment / .env file
• Validation with rich error messages
• Secrets support: direct value OR file path (Docker secrets / K8s)
• Environment profiles: development / staging / production
• Immutable after startup (frozen model)
"""

from __future__ import annotations

import os
import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    """
    All application configuration.  Values are resolved in order:
        1. Environment variables
        2. .env file (if present)
        3. Defaults defined here

    Secrets (passphrase, API keys) can be provided as:
        QUANTUM_PASSPHRASE=my-secret          direct value
        QUANTUM_PASSPHRASE_FILE=/run/secrets/passphrase   Docker secret file
    """

    model_config = SettingsConfigDict(
        env_prefix="QUANTUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        frozen=True,  # immutable after startup
        extra="ignore",
    )

    # ── Environment ───────────────────────────────────────────────────────── #
    environment: Environment = Environment.DEVELOPMENT

    # ── Security ──────────────────────────────────────────────────────────── #
    passphrase: SecretStr = Field(
        default=...,
        description="Master encryption passphrase (min 16 chars)",
    )
    api_key_enabled: bool = True
    api_keys: list[str] = Field(default_factory=list)
    api_key_header: str = "X-API-Key"
    jwt_secret: SecretStr = Field(default_factory=lambda: SecretStr(secrets.token_urlsafe(32)))
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # ── MongoDB ───────────────────────────────────────────────────────────── #
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "quantum_pulse"
    mongo_max_pool_size: int = 100
    mongo_min_pool_size: int = 10
    mongo_timeout_ms: int = 5_000
    mongo_retry_writes: bool = True

    # ── API Server ────────────────────────────────────────────────────────── #
    host: str = "0.0.0.0"
    port: int = Field(default=8747, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1, le=32)
    reload: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_request_size_mb: int = Field(default=256, ge=1, le=2048)
    request_timeout_s: int = Field(default=120, ge=5, le=600)

    # ── Rate Limiting ─────────────────────────────────────────────────────── #
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_burst: int = Field(default=20, ge=1)

    # ── Logging ───────────────────────────────────────────────────────────── #
    log_level: LogLevel = LogLevel.INFO
    log_dir: str = "logs"
    log_rotation: str = "50 MB"
    log_retention: str = "30 days"
    log_format: str = "json"  # "json" | "text"
    log_request_bodies: bool = False  # never in production

    # ── Compression ───────────────────────────────────────────────────────── #
    zstd_level: int = Field(default=22, ge=1, le=22)
    zstd_dict_size_kb: int = Field(default=112, ge=16, le=1024)
    zstd_sample_ratio: float = Field(default=0.05, gt=0, le=0.5)

    # ── Key Management ────────────────────────────────────────────────────── #
    kdf_iterations: int = Field(default=600_000, ge=100_000)
    key_rotation_interval_h: int = Field(default=168, ge=1)  # 7 days default
    key_cache_ttl_s: int = Field(default=300, ge=60)

    # ── Observability ─────────────────────────────────────────────────────── #
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    tracing_enabled: bool = False
    otlp_endpoint: str | None = None

    # ── Scheduler ─────────────────────────────────────────────────────────── #
    scheduler_enabled: bool = True
    health_check_interval_s: int = 30
    dict_retrain_interval_s: int = Field(
        default=86400,
        ge=10,
        description="Dict retrain interval in seconds. Set to 30 for verification testing.",
    )

    # ── Storage backend ───────────────────────────────────────────────────── #
    storage_backend: str = Field(
        default="mongo",
        description="Storage backend: mongo | s3 | gcs",
    )
    # S3
    s3_bucket: str | None = None
    s3_prefix: str = "quantum-pulse"
    s3_region: str | None = None
    s3_endpoint_url: str | None = None  # MinIO / LocalStack / custom endpoint
    # GCS
    gcs_bucket: str | None = None
    gcs_prefix: str = "quantum-pulse"
    gcs_service_file: str | None = None  # path to service account JSON
    # MongoDB / GridFS
    gridfs_threshold_mb: int = Field(default=16, ge=1, le=256)
    pulse_ttl_days: int | None = None  # None = never expire

    # ── Audit ─────────────────────────────────────────────────────────────── #
    audit_enabled: bool = True
    audit_log_file: str = "logs/audit.jsonl"

    # ─────────────────────────────── validators ───────────────────────────── #

    @field_validator("passphrase", mode="before")
    @classmethod
    def _resolve_secret_file(cls, v):
        """Support QUANTUM_PASSPHRASE_FILE=/run/secrets/passphrase."""
        file_var = os.getenv("QUANTUM_PASSPHRASE_FILE")
        if file_var and Path(file_var).exists():
            return Path(file_var).read_text().strip()
        return v

    @field_validator("passphrase")
    @classmethod
    def _passphrase_strength(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if len(raw) < 16:
            raise ValueError(f"QUANTUM_PASSPHRASE must be at least 16 characters. Got {len(raw)}.")
        return v

    @field_validator("api_keys", mode="before")
    @classmethod
    def _parse_api_keys(cls, v):
        """Accept comma-separated string or list."""
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _production_hardening(self) -> Settings:
        if self.environment == Environment.PRODUCTION:
            if not self.api_key_enabled:
                raise ValueError("API key auth must be enabled in production")
            if not self.api_keys:
                raise ValueError("At least one API key required in production")
            if self.reload:
                raise ValueError("Hot reload must be disabled in production")
            if self.host == "0.0.0.0" and not self.rate_limit_enabled:
                raise ValueError("Rate limiting must be enabled when binding 0.0.0.0")
        return self

    # ── derived properties ────────────────────────────────────────────────── #

    @property
    def gridfs_threshold_bytes(self) -> int:
        return self.gridfs_threshold_mb * 1024 * 1024

    @property
    def max_request_size_bytes(self) -> int:
        return self.max_request_size_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.environment == Environment.DEVELOPMENT

    def display(self) -> dict:
        """Return safe (no secrets) config dict for logging/debug."""
        d = self.model_dump()
        d.pop("passphrase", None)
        d.pop("jwt_secret", None)
        d["api_keys"] = [f"***{k[-4:]}" if k else "" for k in (d.get("api_keys") or [])]
        d["mongo_uri"] = self.mongo_uri.split("@")[-1] if "@" in self.mongo_uri else self.mongo_uri
        return d


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance.
    Cached — call get_settings.cache_clear() in tests to reload.
    """
    return Settings()
