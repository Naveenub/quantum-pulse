"""
QUANTUM-PULSE :: core/auth.py
================================
Authentication and authorisation layer.

Two auth mechanisms (both optional, independently toggleable):

  1. API Key  — static keys in config; passed via X-API-Key header
                Suitable for service-to-service calls (LLM training nodes)

  2. JWT      — HS256 tokens issued by /auth/token
                Suitable for human operator access (CLI, dashboard)

FastAPI dependency functions:
  require_api_key     — hard block if key missing/invalid
  require_auth        — accept EITHER valid API key OR valid JWT
  optional_auth       — returns principal or None; never blocks

Principal (resolved identity) is injected into request state so audit
log and metrics can tag every operation with the caller.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext
from pydantic import BaseModel

from core.config import get_settings

# ─────────────────────────────── types ────────────────────────────────────── #


@dataclass
class Principal:
    """Resolved identity attached to every authenticated request."""

    identity: str  # "api_key:last4" or "jwt:subject"
    auth_method: str  # "api_key" | "jwt" | "anon"
    scopes: list[str]  # ["read", "write", "admin"]
    issued_at: float  # unix timestamp


ANON = Principal(identity="anon", auth_method="anon", scopes=["read"], issued_at=0.0)

# ─────────────────────────────── security schemes ─────────────────────────── #

_api_key_header = APIKeyHeader(name=get_settings().api_key_header, auto_error=False)
_bearer = HTTPBearer(auto_error=False)
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ─────────────────────────────── JWT helpers ──────────────────────────────── #


def create_access_token(subject: str, scopes: list[str] | None = None) -> str:
    cfg = get_settings()
    now = time.time()
    payload = {
        "sub": subject,
        "scopes": scopes or ["read", "write"],
        "iat": now,
        "exp": now + cfg.jwt_expire_minutes * 60,
        "iss": "quantum-pulse",
    }
    return jwt.encode(
        payload,
        cfg.jwt_secret.get_secret_value(),
        algorithm=cfg.jwt_algorithm,
    )


def decode_token(token: str) -> dict:
    cfg = get_settings()
    return jwt.decode(
        token,
        cfg.jwt_secret.get_secret_value(),
        algorithms=[cfg.jwt_algorithm],
    )


# ─────────────────────────────── API key validation ──────────────────────────#


def _validate_api_key(key: str) -> Principal | None:
    """Constant-time comparison against all configured API keys."""
    cfg = get_settings()
    for valid_key in cfg.api_keys:
        # constant-time to prevent timing attacks
        if hmac.compare_digest(
            hashlib.sha256(key.encode()).digest(),
            hashlib.sha256(valid_key.encode()).digest(),
        ):
            last4 = key[-4:] if len(key) >= 4 else "****"
            return Principal(
                identity=f"api_key:***{last4}",
                auth_method="api_key",
                scopes=["read", "write", "admin"],
                issued_at=time.time(),
            )
    return None


# ─────────────────────────────── FastAPI dependencies ─────────────────────── #


async def require_api_key(
    request: Request,
    api_key: str | None = Security(_api_key_header),
) -> Principal:
    """Dependency: block unless a valid API key is provided."""
    cfg = get_settings()

    if not cfg.api_key_enabled:
        principal = ANON
        request.state.principal = principal
        return principal

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": 'APIKey realm="quantum-pulse"'},
        )

    principal = _validate_api_key(api_key)
    if principal is None:
        logger.warning(
            "Invalid API key attempt  ip={}", request.client.host if request.client else "?"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    request.state.principal = principal
    return principal


async def require_auth(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Dependency: accept valid API key OR valid JWT bearer token."""
    cfg = get_settings()

    if not cfg.api_key_enabled:
        request.state.principal = ANON
        return ANON

    # Try API key first (cheaper — no crypto)
    if api_key:
        principal = _validate_api_key(api_key)
        if principal:
            request.state.principal = principal
            return principal

    # Try JWT
    if bearer:
        try:
            claims = decode_token(bearer.credentials)
            principal = Principal(
                identity=f"jwt:{claims['sub']}",
                auth_method="jwt",
                scopes=claims.get("scopes", ["read"]),
                issued_at=claims.get("iat", 0.0),
            )
            request.state.principal = principal
            return principal
        except JWTError as exc:
            logger.warning("JWT validation failed: {}", exc)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Valid API key or Bearer token required",
        headers={"WWW-Authenticate": 'Bearer realm="quantum-pulse"'},
    )


async def optional_auth(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal | None:
    """Dependency: resolve identity if credentials present; never raises."""
    try:
        return await require_auth(request, api_key=api_key, bearer=bearer)
    except HTTPException:
        request.state.principal = ANON
        return ANON


def require_scope(scope: str):
    """Dependency factory: ensure authenticated principal has a specific scope."""

    async def _check(principal: Principal = Depends(require_auth)) -> Principal:
        if scope not in principal.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Scope '{scope}' required",
            )
        return principal

    return _check


# ─────────────────────────────── /auth/token endpoint ─────────────────────── #

auth_router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    api_key: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@auth_router.post("/token", response_model=TokenResponse)
async def issue_token(req: TokenRequest) -> TokenResponse:
    """
    Exchange a valid API key for a short-lived JWT.
    Useful for browser/dashboard access without embedding the raw API key.
    """
    principal = _validate_api_key(req.api_key)
    if not principal:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid API key")

    cfg = get_settings()
    token = create_access_token(principal.identity, principal.scopes)
    return TokenResponse(
        access_token=token,
        expires_in=cfg.jwt_expire_minutes * 60,
    )
