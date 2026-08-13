"""Single-origin SPA serving (app/api/spa.py).

The demo deployment runs one process that serves both the API and the built
frontend, because the frontend calls the API with relative paths and the
session cookie is SameSite=Lax - see app/api/spa.py's module docstring. These
tests pin the parts that would silently break that arrangement:

  * API routes must still win over the catch-all,
  * an unknown /api path must stay JSON (never an HTML page),
  * deep links must return the SPA shell so client-side routing works,
  * the strict API CSP must not be relaxed for API responses, while served
    HTML must be allowed to load its own bundle,
  * path traversal out of the static root must not serve arbitrary files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

ORIGIN = "http://localhost:5173"


@pytest.fixture
def spa_client(tmp_path: Path, migrated_engine: object) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>SPA</title><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log('bundle')")
    (dist / "favicon.svg").write_text("<svg/>")
    # a file OUTSIDE the static root that traversal must never reach
    (tmp_path / "secret.txt").write_text("do not serve me")

    settings = Settings(static_root=str(dist))  # type: ignore[call-arg]
    return TestClient(create_app(settings))


class TestSpaServing:
    def test_root_serves_the_spa_shell(self, spa_client: TestClient) -> None:
        resp = spa_client.get("/")
        assert resp.status_code == 200
        assert "<div id=root>" in resp.text

    def test_deep_link_serves_the_shell_for_client_side_routing(
        self, spa_client: TestClient
    ) -> None:
        # React Router owns /suppliers; the server must not 404 a refresh there
        resp = spa_client.get("/suppliers")
        assert resp.status_code == 200
        assert "<div id=root>" in resp.text

    def test_real_static_files_are_served_as_themselves(self, spa_client: TestClient) -> None:
        assert spa_client.get("/assets/app.js").status_code == 200
        assert "bundle" in spa_client.get("/assets/app.js").text
        assert spa_client.get("/favicon.svg").status_code == 200

    def test_api_routes_still_win_over_the_catch_all(self, spa_client: TestClient) -> None:
        resp = spa_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_unknown_api_path_stays_json_never_html(self, spa_client: TestClient) -> None:
        """An API client hitting a typo must not receive the SPA page."""
        resp = spa_client.get("/api/v1/does-not-exist", headers={"Origin": ORIGIN})
        assert resp.status_code == 404
        assert "text/html" not in resp.headers.get("content-type", "")
        assert "<div id=root>" not in resp.text

    def test_api_keeps_the_strict_csp_and_html_gets_the_spa_csp(
        self, spa_client: TestClient
    ) -> None:
        api_csp = spa_client.get("/api/health").headers["Content-Security-Policy"]
        assert api_csp == "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

        page_csp = spa_client.get("/").headers["Content-Security-Policy"]
        assert "script-src 'self'" in page_csp  # the bundle may load
        assert "default-src 'none'" not in page_csp
        # still locked down where it matters
        assert "frame-ancestors 'none'" in page_csp
        assert "object-src 'none'" in page_csp
        assert "base-uri 'none'" in page_csp

    def test_traversal_outside_the_static_root_falls_back_to_the_shell(
        self, spa_client: TestClient
    ) -> None:
        resp = spa_client.get("/../secret.txt")
        assert "do not serve me" not in resp.text


class TestWithoutStaticRoot:
    def test_api_only_when_unset(self, migrated_engine: object) -> None:
        """Default (dev, tests, API-only deploys): no SPA, no catch-all."""
        client = TestClient(create_app(Settings()))  # type: ignore[call-arg]
        assert client.get("/api/health").status_code == 200
        assert client.get("/suppliers").status_code == 404

    def test_missing_build_degrades_to_api_only(
        self, tmp_path: Path, migrated_engine: object
    ) -> None:
        """A pointed-at directory with no index.html must not crash boot."""
        empty = tmp_path / "not-built"
        empty.mkdir()
        client = TestClient(create_app(Settings(static_root=str(empty))))  # type: ignore[call-arg]
        assert client.get("/api/health").status_code == 200
        assert client.get("/suppliers").status_code == 404
