"""Serving the built single-page app from this process.

Why this exists: the frontend calls the API with RELATIVE paths
(`/api/v1/...`, see frontend/src/api/client.ts) and the session cookie is
`SameSite=Lax`. Both assume one origin. Splitting the SPA onto a second
domain would mean relative calls hit the static host instead of the API, and
the cookie would not ride along on cross-site requests, so login would fail
in a way that looks like "wrong password". Serving both from here keeps the
deployment honest to what the code already assumes, and costs one process
rather than two.

Mounted only when `PO_STATIC_ROOT` points at a real build; the dev server and
the entire test suite run without it and are unaffected.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles


def mount_spa(app: FastAPI, static_root: str) -> bool:
    """Serve `static_root` (a Vite `dist/`) at the app root.

    Returns False when the directory has no `index.html`, so a
    missing/half-built frontend degrades to an API-only server with a warning
    instead of crashing the process on boot.
    """
    root = Path(static_root).resolve()
    index = root / "index.html"
    if not index.is_file():
        return False

    assets = root / "assets"
    if assets.is_dir():
        # Hashed filenames (Vite) - safe to cache hard.
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(request: Request, full_path: str) -> Response:
        """Any non-API path returns the SPA shell so client-side routing
        (React Router) can resolve /suppliers, /rfqs, deep links, refreshes.

        `/api/**` never reaches here: those routers are registered first and
        FastAPI matches in registration order. An unmatched `/api/...` is
        answered with a JSON 404 rather than an HTML page, so a mistyped
        endpoint cannot hand an API client a page of markup.
        """
        if full_path.startswith("api/"):
            return Response(status_code=404, content='{"detail":"Not Found"}',
                            media_type="application/json")
        # Real files (favicon, manifest, images) are served as themselves;
        # everything else falls through to index.html.
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)

    return True


__all__ = ["mount_spa"]
