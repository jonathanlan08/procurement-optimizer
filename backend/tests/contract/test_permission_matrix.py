"""Every registered API route must be declared in the permission matrix.

This is the guard rail that turns "forgot to think about authorization" into a
failing test instead of a security incident.
"""

from __future__ import annotations

from app.api.permissions import PERMISSIONS
from app.core.config import Environment, Settings
from app.main import create_app


def _registered_routes() -> set[tuple[str, str]]:
    """All (method, path) pairs, resolved via the OpenAPI schema.

    The schema is used instead of walking app.routes because FastAPI may
    register included routers lazily (as internal wrapper objects); OpenAPI is
    the stable, public view of the final route table.
    """
    settings = Settings(environment=Environment.TEST, database_url="postgresql+psycopg://x/x")
    app = create_app(settings)
    schema = app.openapi()
    routes: set[tuple[str, str]] = set()
    for path, operations in schema["paths"].items():
        for method in operations:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                routes.add((method.upper(), path))
    return routes


class TestPermissionMatrix:
    def test_every_route_is_declared(self) -> None:
        undeclared = _registered_routes() - set(PERMISSIONS.keys())
        assert not undeclared, (
            f"routes with NO permission declaration (add them to "
            f"app/api/permissions.py): {sorted(undeclared)}"
        )

    def test_no_stale_declarations(self) -> None:
        stale = set(PERMISSIONS.keys()) - _registered_routes()
        assert not stale, f"declared permissions for routes that no longer exist: {sorted(stale)}"
