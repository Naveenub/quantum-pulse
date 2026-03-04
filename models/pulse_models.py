"""
QUANTUM-PULSE :: models/pulse_models.py
========================================
Canonical Pydantic V2 models shared across the entire system.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────── enums ────────────────────────────────────── #

class StorageBackend(str, Enum):
    MEMORY  = "memory"
    MONGO   = "mongo"
    GRIDFS  = "gridfs"


class PulseStatus(str, Enum):
    PENDING   = "pending"
    SEALED    = "sealed"
    CORRUPTED = "corrupted"
    EXPIRED   = "expired"


class ScanMode(str, Enum):
    SHALLOW   = "shallow"    # one level only
    RECURSIVE = "recursive"  # full depth with entropy sharding
    SAMPLED   = "sampled"    # first 5 % for dict training


# ─────────────────────────────── stats ────────────────────────────────────── #

class CompressionStats(BaseModel):
    original_bytes:     int
    packed_bytes:       int
    compressed_bytes:   int
    encrypted_bytes:    int
    duration_ms:        float
    ratio:              float = 0.0
    entropy_bits_per_byte: float = 0.0

    @model_validator(mode="after")
    def _compute_ratio(self) -> "CompressionStats":
        if self.original_bytes:
            self.ratio = self.original_bytes / max(self.encrypted_bytes, 1)
        return self


class ScanStats(BaseModel):
    total_files:       int   = 0
    total_dirs:        int   = 0
    total_bytes:       int   = 0
    skipped_files:     int   = 0
    scan_duration_ms:  float = 0.0
    shards_created:    int   = 0


# ─────────────────────────────── file metadata ────────────────────────────── #

class FileEntry(BaseModel):
    path:         str
    name:         str
    size:         int
    mtime:        float
    is_dir:       bool   = False
    content_hash: Optional[str] = None      # SHA3-256 hex, populated lazily
    pulse_id:     Optional[str] = None      # set after sealing


class DirManifest(BaseModel):
    """Snapshot of a directory tree, used as the MsgPack payload for a shard."""
    root_path:   str
    entries:     list[FileEntry]
    depth:       int   = 0
    stats:       ScanStats = Field(default_factory=ScanStats)
    created_at:  float = Field(default_factory=time.time)


# ─────────────────────────────── pulse / master ───────────────────────────── #

class PulseBlob(BaseModel):
    pulse_id:     str
    parent_id:    Optional[str]  = None
    merkle_root:  str
    chunk_hash:   str
    salt:         str
    nonce:        str
    zstd_dict_id: Optional[int] = None
    dict_version: int             = 0    # adaptive dict version used at seal time
    stats:        CompressionStats
    status:       PulseStatus   = PulseStatus.SEALED
    created_at:   float         = Field(default_factory=time.time)
    tags:         dict[str, str] = Field(default_factory=dict)

    @field_validator("merkle_root", "chunk_hash", "salt", "nonce")
    @classmethod
    def _must_be_hex(cls, v: str) -> str:
        try:
            bytes.fromhex(v)
        except ValueError:
            raise ValueError(f"Expected hex string, got: {v!r}")
        return v.lower()


class MasterPulse(BaseModel):
    master_id:            str
    shard_ids:            list[str]
    merkle_tree:          list[str]
    merkle_root:          str
    total_original_bytes: int
    total_shards:         int
    created_at:           float = Field(default_factory=time.time)


# ─────────────────────────────── virtual mount ────────────────────────────── #

class MountedFile(BaseModel):
    """In-memory representation of a file served by the virtual mount layer."""
    virtual_path:  str
    pulse_id:      str
    size:          int
    content_type:  str  = "application/octet-stream"
    decrypted:     bool = False


class VaultMount(BaseModel):
    mount_id:      str
    root_path:     str
    files:         dict[str, MountedFile] = Field(default_factory=dict)
    created_at:    float = Field(default_factory=time.time)
    read_count:    int   = 0
