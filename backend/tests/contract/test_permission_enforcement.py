"""Declared-vs-ENFORCED permission contract (adopted from the 2026-08
independent security audit, finding LOW-10).

`tests/contract/test_permission_matrix.py` proves the key-SETS match (every
route declared, no stale declarations). It never proved that the Role a route
DECLARES equals the Role its dependency tree actually ENFORCES — which made
`PERMISSIONS` documentation, not enforcement. This test walks the real
FastAPI dependant graph of every route and fails on any divergence, making
the matrix load-bearing.

FastAPI registers included routers lazily as `_IncludedRouter` wrappers, so
`app.routes` alone does not expose the API routes (the same quirk
`test_permission_matrix.py` works around via `app.openapi()`); here we need
the actual route objects for their dependants, so we descend into
`original_router.routes` and re-apply the mount prefix.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI

from app.api.deps import ROLE_ORDER, current_principal
from app.api.permissions import PERMISSIONS
from app.core.config import Environment, Settings
from app.main import API_PREFIX, create_app
from app.models.identity import Role

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
ALL_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _build_app() -> FastAPI:
    settings = Settings(
        environment=Environment.TEST, database_url="postgresql+psycopg://x/x"
    )
    return create_app(settings)


def _api_routes(app: FastAPI) -> Iterator[tuple[str, str, Any]]:
    """Yield (method, effective_path, route) for every real API route."""
    for r in app.routes:
        if type(r).__name__ == "_IncludedRouter":
            for sub in r.original_router.routes:  # type: ignore[attr-defined]
                if not hasattr(sub, "dependant"):
                    continue
                for m in sub.methods or ():
                    if m in ALL_METHODS:
                        yield m, API_PREFIX + sub.path, sub
        elif hasattr(r, "dependant"):
            for m in r.methods or ():  # type: ignore[attr-defined]
                if m in ALL_METHODS:
                    yield m, r.path, r


def _walk(dep: Any, seen: set[int] | None = None) -> Iterator[Any]:
    if seen is None:
        seen = set()
    if id(dep) in seen:
        return
    seen.add(id(dep))
    yield dep
    for sub in dep.dependencies:
        yield from _walk(sub, seen)


def _enforced(route: Any) -> tuple[Role | str, bool]:
    """The level a route's dependency tree actually enforces: the strictest
    `require_role(minimum)` closure found (its `minimum` recovered via
    closure inspection), else 'authenticated' if `current_principal` appears,
    else 'public'."""
    has_principal = False
    roles: list[Role] = []
    for d in _walk(route.dependant):
        call = d.call
        if call is current_principal:
            has_principal = True
        if call is not None and getattr(call, "__name__", "") == "checker":
            minimum = inspect.getclosurevars(call).nonlocals.get("minimum")
            if isinstance(minimum, Role):
                roles.append(minimum)
                has_principal = True
    if roles:
        return max(roles, key=lambda r: ROLE_ORDER[r]), has_principal
    return ("authenticated" if has_principal else "public"), has_principal


class TestPermissionEnforcement:
    def test_route_discovery_is_not_vacuous(self) -> None:
        app = _build_app()
        found = {(m, p) for m, p, _ in _api_routes(app)}
        assert len(found) > 50, "route discovery broke — refusing a vacuous pass"
        assert found == set(PERMISSIONS.keys()), {
            "undeclared": sorted(found - set(PERMISSIONS)),
            "stale": sorted(set(PERMISSIONS) - found),
        }

    def test_declared_role_equals_enforced_role(self) -> None:
        app = _build_app()
        problems: list[tuple[str, str, str]] = []
        for method, path, route in _api_routes(app):
            declared = PERMISSIONS.get((method, path))
            enforced, has_auth = _enforced(route)
            if declared is None:
                problems.append((method, path, "UNDECLARED"))
            elif declared == "public":
                if has_auth:
                    problems.append((method, path, "declared public but enforces auth"))
            elif declared == "authenticated":
                if not has_auth:
                    problems.append(
                        (method, path, "declared authenticated but no auth dependency")
                    )
            elif isinstance(declared, Role):
                if not isinstance(enforced, Role):
                    problems.append(
                        (method, path, f"declared {declared.value}, enforced {enforced}")
                    )
                elif ROLE_ORDER[enforced] != ROLE_ORDER[declared]:
                    problems.append(
                        (
                            method,
                            path,
                            f"declared {declared.value} but enforces {enforced.value}",
                        )
                    )
        assert not problems, problems

    def test_every_mutating_route_requires_a_principal(self) -> None:
        app = _build_app()
        bad = []
        for method, path, route in _api_routes(app):
            if method not in MUTATING:
                continue
            _, has_auth = _enforced(route)
            if not has_auth and (method, path) != ("POST", "/api/v1/auth/login"):
                bad.append((method, path))
        assert not bad, bad
