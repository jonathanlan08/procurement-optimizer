"""Regression tests for the middleware fixes from the 2026-08 security audit.

- MEDIUM-2 — oversized DECLARED bodies are now rejected pre-routing by
  `BodySizeLimitMiddleware`, instead of Starlette spooling the whole multipart
  body to disk before the route-level 413 fired.
- MEDIUM-4 — rate-limit key eviction: the old oldest-inserted fallback let an
  address-spray evict (and thereby reset) the sprayer's own auth bucket; now
  keys with fresh windows are never evicted and new clients overflow into a
  shared bucket instead.

No database is touched: the 413 fires before routing, and the eviction test
drives the middleware's dispatch directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from app.api.middleware import RateLimitMiddleware
from app.core.config import Environment, Settings
from app.main import create_app

ORIGIN = "http://localhost:5173"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url="postgresql+psycopg://unused/unused",
        allowed_origins=[ORIGIN],
        **overrides,
    )


class TestBodySizeLimit:
    def test_oversized_declared_body_is_rejected_before_routing(self) -> None:
        app = create_app(_settings(max_upload_bytes=1024))
        with TestClient(app, base_url="http://testserver") as client:
            # cap = 1024 + 1 MiB slack; 2 MiB exceeds it. The login route
            # would need a DB — the 413 must fire before it ever runs.
            resp = client.post(
                "/api/v1/auth/login",
                content=b"x" * (2 * 1024 * 1024),
                headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            )
        assert resp.status_code == 413, resp.text
        assert resp.json()["error"]["code"] == "payload_too_large"

    def test_ordinary_body_passes_the_middleware(self) -> None:
        app = create_app(_settings(max_upload_bytes=1024))
        with TestClient(app, base_url="http://testserver") as client:
            # A small body must clear the middleware; a nonexistent path keeps
            # the request off the database, so 404 (not 413) proves pass-through.
            resp = client.post(
                "/api/v1/nonexistent",
                json={"small": "body"},
                headers={"Origin": ORIGIN},
            )
        assert resp.status_code == 404, resp.text


def _request(path: str, ip: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
        "client": (ip, 12345),
    }
    return Request(scope)


async def _ok(_request: Request) -> Response:
    return Response("ok")


class TestRateLimitEviction:
    def test_active_auth_bucket_survives_address_spray(self) -> None:
        middleware = RateLimitMiddleware(
            app=None,  # type: ignore[arg-type]  # dispatch never touches .app here
            settings=_settings(rate_limit_auth_per_minute=10, rate_limit_per_minute=100),
        )
        middleware._max_keys = 3

        async def scenario() -> None:
            # Victim (attacker's own) auth bucket accumulates failures.
            for _ in range(2):
                await middleware.dispatch(_request("/api/v1/auth/login", "10.0.0.1"), _ok)
            assert "auth:10.0.0.1" in middleware._hits
            before = len(middleware._hits["auth:10.0.0.1"])

            # Spray: many distinct addresses, all with fresh windows.
            for i in range(20):
                await middleware.dispatch(_request("/api/v1/things", f"10.9.9.{i}"), _ok)

            # The old behaviour evicted the oldest-inserted key — exactly
            # auth:10.0.0.1 — resetting its counter. It must survive intact.
            assert "auth:10.0.0.1" in middleware._hits
            assert len(middleware._hits["auth:10.0.0.1"]) == before
            # Overflow requests were collectively tracked, not dropped.
            assert any(k.endswith(":overflow") for k in middleware._hits)

        asyncio.run(scenario())

    def test_stale_keys_are_still_evicted_normally(self) -> None:
        middleware = RateLimitMiddleware(
            app=None,  # type: ignore[arg-type]
            settings=_settings(rate_limit_auth_per_minute=10, rate_limit_per_minute=100),
        )
        middleware._max_keys = 2

        async def scenario() -> None:
            await middleware.dispatch(_request("/api/v1/things", "10.0.0.1"), _ok)
            # Age the first key's window past 60s.
            middleware._hits["general:10.0.0.1"][0] -= 120.0
            await middleware.dispatch(_request("/api/v1/things", "10.0.0.2"), _ok)
            await middleware.dispatch(_request("/api/v1/things", "10.0.0.3"), _ok)
            assert "general:10.0.0.1" not in middleware._hits
            assert not any(k.endswith(":overflow") for k in middleware._hits)

        asyncio.run(scenario())
