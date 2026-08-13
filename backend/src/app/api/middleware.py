"""Request-id, security headers, and rate limiting."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.core.config import Environment, Settings
from app.core.errors import RateLimitedError

REQUEST_ID_HEADER = "X-Request-ID"

# Strict CSP: the API serves JSON only; the SPA is served separately. No inline
# anything. Frontend dev server handles its own CSP in dev.
_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, environment: Environment) -> None:
        super().__init__(app)
        self._hsts = environment is Environment.PROD

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if self._hsts:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        return response


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Origin/Referer allowlist for EVERY mutating API request - including login,
    which has no session yet and would otherwise be exposed to login-CSRF.
    The per-session CSRF token check in deps.py remains as the second factor."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._allowed = settings.allowed_origins

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith(
            "/api/"
        ):
            from app.core.security import origin_allowed

            origin = request.headers.get("origin") or request.headers.get("referer")
            if not origin_allowed(origin, self._allowed):
                from datetime import UTC, datetime

                from starlette.responses import JSONResponse

                from app.core.errors import CsrfError

                err = CsrfError("Request origin not allowed.")
                return JSONResponse(
                    status_code=err.status,
                    content=err.envelope(
                        getattr(request.state, "request_id", ""),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window in-memory limiter, per client IP. Single-node v0.1 scope;
    a multi-node deployment needs a shared store (documented limitation)."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._general = settings.rate_limit_per_minute
        self._auth = settings.rate_limit_auth_per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._max_keys = 10_000  # bound memory under address-spraying

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        ip = request.client.host if request.client else "unknown"
        is_auth = request.url.path.startswith("/api/v1/auth/login")
        limit = self._auth if is_auth else self._general
        key = f"{'auth' if is_auth else 'general'}:{ip}"

        now = time.monotonic()
        if key not in self._hits and len(self._hits) >= self._max_keys:
            # Evict only entries whose windows have fully expired. NEVER evict a
            # key with fresh hits: oldest-inserted eviction let an address-spray
            # reset the sprayer's own auth counter (2026-08 security audit,
            # MEDIUM-4). If every key is active, new clients share one bounded
            # overflow bucket instead - existing counters survive, and the spray
            # itself gets collectively rate-limited.
            stale = [k for k, w in self._hits.items() if not w or now - w[-1] > 60.0]
            if stale:
                for k in stale[:1000]:
                    self._hits.pop(k, None)
            else:
                key = f"{'auth' if is_auth else 'general'}:overflow"
        window = self._hits[key]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= limit:
            # exceptions raised in middleware bypass FastAPI's handlers, so the
            # envelope is rendered here directly
            from datetime import UTC, datetime

            from starlette.responses import JSONResponse

            err = RateLimitedError()
            return JSONResponse(
                status_code=err.status,
                content=err.envelope(
                    getattr(request.state, "request_id", ""),
                    datetime.now(UTC).isoformat(),
                ),
                headers={"Retry-After": "60"},
            )
        window.append(now)
        return await call_next(request)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies from the Content-Length header BEFORE
    routing. FastAPI resolves `UploadFile` by awaiting `request.form()`, and
    Starlette's multipart parser spools file parts to disk with no size limit -
    so the route-level 413 checks only fire after the whole body is already on
    disk (2026-08 security audit, MEDIUM-2). This closes the declared-length
    path; a chunked body without Content-Length still spools, which is why
    DEPLOYMENT.md §8 requires a reverse-proxy body cap (client_max_body_size)
    in front of any real deployment.

    The cap is max_upload_bytes plus slack for multipart framing and ordinary
    JSON bodies - this is a guard against gigabyte-scale abuse, not the exact
    per-file limit (the route-level streamed check stays authoritative)."""

    _SLACK_BYTES = 1024 * 1024

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._max_bytes = settings.max_upload_bytes + self._SLACK_BYTES

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > self._max_bytes:
                from datetime import UTC, datetime

                from starlette.responses import JSONResponse

                from app.core.errors import PayloadTooLargeError

                err = PayloadTooLargeError(
                    f"Request body exceeds the maximum allowed size of "
                    f"{self._max_bytes} bytes."
                )
                return JSONResponse(
                    status_code=err.status,
                    content=err.envelope(
                        getattr(request.state, "request_id", ""),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        return await call_next(request)
