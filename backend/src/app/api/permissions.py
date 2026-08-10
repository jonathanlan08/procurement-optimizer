"""Permission matrix — PRINCIPAL-OWNED. Isolation/authorization control #4.

Every API route MUST have an entry here. A contract test walks the FastAPI
route table and fails when a route has no declaration, so forgetting to think
about authorization is a CI failure, not a production incident.

Values:
- "public": no session required
- "authenticated": any live session, any role
- Role.X: at least that role in the hierarchy viewer < analyst < admin < owner
"""

from __future__ import annotations

from app.models.identity import Role

PermissionLevel = Role | str  # "public" | "authenticated" | Role

PERMISSIONS: dict[tuple[str, str], PermissionLevel] = {
    ("GET", "/api/health"): "public",
    ("POST", "/api/v1/auth/login"): "public",
    ("POST", "/api/v1/auth/logout"): "authenticated",
    ("GET", "/api/v1/auth/me"): "authenticated",
    ("GET", "/api/v1/suppliers"): Role.VIEWER,
    ("POST", "/api/v1/suppliers"): Role.ANALYST,
    ("GET", "/api/v1/suppliers/{supplier_id}"): Role.VIEWER,
    ("PATCH", "/api/v1/suppliers/{supplier_id}"): Role.ANALYST,
    ("POST", "/api/v1/suppliers/{supplier_id}/archive"): Role.ADMINISTRATOR,
    ("POST", "/api/v1/suppliers/{supplier_id}/unarchive"): Role.ADMINISTRATOR,
}

# Routes that are org-scoped resources (subject to the 404 cross-org matrix
# test). Phase 2+ adds every business resource here as it lands.
ORG_SCOPED_RESOURCES: list[str] = ["suppliers"]
