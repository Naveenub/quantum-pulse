"""
QUANTUM-PULSE :: core/middleware.py
======================================
Production middleware stack (applied in order by main.py):

  1. GZipMiddleware         — compress responses > 1 KiB
  2. RequestSizeMiddleware  — reject bodies above configured limit
  3. RequestIDMiddleware    — inject X-Request-ID into every request/response
  4. TimingMiddleware       — add X-Process-Time-Ms to every response
  5. SecurityHeadersMiddleware — HSTS, CSP, X-Frame-Options, etc.
  6. StructuredLoggingMiddleware — log every request as structured JSON
  7. GlobalExceptionHandler — convert all unhandled exceptions to RFC-7807

Rate limiting is handled via slowapi (see main.py).
"""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from core.config import get_settings

# ─────────────────────────────── Request ID ──────────────────────────────── #

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Injects a unique X-Request-ID into every request and response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response


# ─────────────────────────────── Timing ──────────────────────────────────── #

class TimingMiddleware(BaseHTTPMiddleware):
    """Adds X-Process-Time-Ms header to every response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        t0       = time.perf_counter()
        response = await call_next(request)
        ms       = (time.perf_counter() - t0) * 1_000
        response.headers["X-Process-Time-Ms"] = f"{ms:.2f}"
        return response


# ─────────────────────────────── Security Headers ────────────────────────── #

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security-relevant HTTP headers.
    Ref: OWASP Secure Headers Project.
    """

    _HEADERS = {
        "X-Content-Type-Options":    "nosniff",
        "X-Frame-Options":           "DENY",
        "X-XSS-Protection":          "1; mode=block",
        "Referrer-Policy":           "strict-origin-when-cross-origin",
        "Permissions-Policy":        "geolocation=(), microphone=(), camera=()",
        "Content-Security-Policy":   "default-src 'self'",
        "Cache-Control":             "no-store",
    }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        cfg      = get_settings()
        for header, value in self._HEADERS.items():
            response.headers[header] = value
        if cfg.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response


# ─────────────────────────────── Request Size ────────────────────────────── #

class RequestSizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds configured limit."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        cfg  = get_settings()
        clen = request.headers.get("content-length")
        if clen and int(clen) > cfg.max_request_size_bytes:
            return JSONResponse(
                status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content     = {
                    "error": "request_too_large",
                    "detail": f"Body exceeds {cfg.max_request_size_mb} MiB limit",
                },
            )
        return await call_next(request)


# ─────────────────────────────── Structured Logging ──────────────────────── #

class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """
    Log every HTTP request as a structured record.
    Skips /health and /metrics to reduce noise.
    """

    _SKIP_PATHS = frozenset({"/health", "/metrics", "/favicon.ico"})

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)

        t0       = time.perf_counter()
        response = await call_next(request)
        ms       = (time.perf_counter() - t0) * 1_000

        principal = getattr(request.state, "principal", None)
        req_id    = getattr(request.state, "request_id", "-")

        logger.info(
            "HTTP {method} {path} → {status}  {ms:.1f}ms  "
            "req={req_id}  ip={ip}  identity={identity}",
            method   = request.method,
            path     = request.url.path,
            status   = response.status_code,
            ms       = ms,
            req_id   = req_id[:8] if req_id else "-",
            ip       = request.client.host if request.client else "?",
            identity = principal.identity if principal else "anon",
        )
        return response


# ─────────────────────────────── Global Exception Handler ────────────────── #

def _rfc7807(
    status_code: int,
    error_type:  str,
    detail:      str,
    request_id:  str = "",
    extra:       dict | None = None,
) -> JSONResponse:
    """RFC 7807 Problem Details JSON response."""
    body: dict[str, Any] = {
        "type":       f"https://quantum-pulse.io/errors/{error_type}",
        "title":      error_type.replace("_", " ").title(),
        "status":     status_code,
        "detail":     detail,
        "request_id": request_id,
    }
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def install_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI application."""

    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", "")
        errors = [
            {"field": ".".join(str(loc) for loc in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        logger.warning("Validation error  req={}  errors={}", req_id[:8], errors)
        return _rfc7807(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "Request body failed validation",
            req_id,
            {"errors": errors},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", "")
        if exc.status_code >= 500:
            logger.error("HTTP {} req={} detail={}", exc.status_code, req_id[:8], exc.detail)
        return _rfc7807(exc.status_code, "http_error", str(exc.detail), req_id)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", "")
        cfg    = get_settings()
        logger.exception("Unhandled exception  req={}", req_id[:8])
        detail = str(exc) if cfg.is_development else "An internal error occurred"
        tb     = traceback.format_exc() if cfg.is_development else None
        return _rfc7807(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            detail,
            req_id,
            {"traceback": tb} if tb else None,
        )


# ─────────────────────────────── factory ─────────────────────────────────── #

def apply_middleware(app: FastAPI) -> None:
    """
    Apply the full middleware stack to *app*.
    Order matters — starlette applies in reverse registration order.
    """
    cfg = get_settings()

    # GZip (outermost — compress final response)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = cfg.cors_origins,
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
        expose_headers    = ["X-Request-ID", "X-Process-Time-Ms"],
    )

    # Custom middleware stack (innermost first)
    app.add_middleware(StructuredLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(RequestSizeMiddleware)

    install_exception_handlers(app)
