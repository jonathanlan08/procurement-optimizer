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
    ("GET", "/api/v1/units"): Role.VIEWER,
    ("GET", "/api/v1/suppliers"): Role.VIEWER,
    ("POST", "/api/v1/suppliers"): Role.ANALYST,
    ("GET", "/api/v1/suppliers/{supplier_id}"): Role.VIEWER,
    ("PATCH", "/api/v1/suppliers/{supplier_id}"): Role.ANALYST,
    ("POST", "/api/v1/suppliers/{supplier_id}/archive"): Role.ADMINISTRATOR,
    ("POST", "/api/v1/suppliers/{supplier_id}/unarchive"): Role.ADMINISTRATOR,
    # supplier contacts (03-api-contract.md §4.4): "O A N (V read)" — list is
    # viewer+, create/update/archive are analyst+; delete = archive (soft).
    ("GET", "/api/v1/suppliers/{supplier_id}/contacts"): Role.VIEWER,
    ("POST", "/api/v1/suppliers/{supplier_id}/contacts"): Role.ANALYST,
    ("PATCH", "/api/v1/suppliers/{supplier_id}/contacts/{contact_id}"): Role.ANALYST,
    ("DELETE", "/api/v1/suppliers/{supplier_id}/contacts/{contact_id}"): Role.ANALYST,
    # supplier performance (03-api-contract.md §4.4): "O A N V" for list,
    # "O A N" for create; no update/delete route in the contract.
    ("GET", "/api/v1/suppliers/{supplier_id}/performance"): Role.VIEWER,
    ("POST", "/api/v1/suppliers/{supplier_id}/performance"): Role.ANALYST,
    # parts (03-api-contract.md §4.5): "O A N V" for list/get, "O A N" for
    # create/update, "O A" for archive. `unarchive` has no contract-table
    # entry of its own (see api/v1/parts.py module docstring for why this
    # router still implements it); it is declared here with the same O A
    # gate as archive, mirroring suppliers.
    ("GET", "/api/v1/parts"): Role.VIEWER,
    ("POST", "/api/v1/parts"): Role.ANALYST,
    ("GET", "/api/v1/parts/{part_id}"): Role.VIEWER,
    ("PATCH", "/api/v1/parts/{part_id}"): Role.ANALYST,
    ("POST", "/api/v1/parts/{part_id}/archive"): Role.ADMINISTRATOR,
    ("POST", "/api/v1/parts/{part_id}/unarchive"): Role.ADMINISTRATOR,
    # part alternatives (03-api-contract.md §4.5): "O A N V" for list, "O A N"
    # for create/delete.
    ("GET", "/api/v1/parts/{part_id}/alternatives"): Role.VIEWER,
    ("POST", "/api/v1/parts/{part_id}/alternatives"): Role.ANALYST,
    ("DELETE", "/api/v1/parts/{part_id}/alternatives/{alternative_id}"): Role.ANALYST,
    # part imports (03-api-contract.md §4.5): "O A N" for preview/commit/
    # cancel, "O A N V" for the read-only row-level preview.
    ("POST", "/api/v1/part-imports"): Role.ANALYST,
    ("GET", "/api/v1/part-imports/{batch_id}"): Role.VIEWER,
    ("POST", "/api/v1/part-imports/{batch_id}/commit"): Role.ANALYST,
    ("POST", "/api/v1/part-imports/{batch_id}/cancel"): Role.ANALYST,
    # BOMs (03-api-contract.md §4.7 + this task's stated default "viewer
    # reads, analyst mutates, admin archives"). `activate` has no contract-
    # table entry of its own (see api/v1/boms.py module docstring for why
    # this router still implements it); gated analyst+ like create/versions,
    # since it is a routine publishing action, not a destructive one.
    ("GET", "/api/v1/boms"): Role.VIEWER,
    ("POST", "/api/v1/boms"): Role.ANALYST,
    ("GET", "/api/v1/boms/{bom_id}"): Role.VIEWER,
    ("GET", "/api/v1/boms/{bom_id}/versions"): Role.VIEWER,
    ("POST", "/api/v1/boms/{bom_id}/versions"): Role.ANALYST,
    ("POST", "/api/v1/boms/{bom_id}/activate"): Role.ANALYST,
    ("POST", "/api/v1/boms/{bom_id}/archive"): Role.ADMINISTRATOR,
    # exchange rates (03-api-contract.md §4.13): "O A N V" for the list/
    # effective-lookup GET, "O A N" for a manual override, "O A" (not
    # analyst) for refresh.
    ("GET", "/api/v1/exchange-rates"): Role.VIEWER,
    ("POST", "/api/v1/exchange-rates"): Role.ANALYST,
    ("POST", "/api/v1/exchange-rates/refresh"): Role.ADMINISTRATOR,
    # RFQs (03-api-contract.md §4.8): "O A N V" for every read route
    # (list/get/status-history/lines-list/suppliers-list), "O A N" for every
    # mutation. Unlike BOMs/parts/suppliers, the contract carves out no
    # separate admin-only archive route for RFQs — reaching `archived` status
    # happens through the same analyst-level `POST .../status` route as any
    # other transition (see app/services/rfq_service.py module docstring).
    # `.../suppliers/{rfq_supplier_id}/reinstate` has no contract-table entry
    # of its own (see api/v1/rfqs.py module docstring for why this router
    # still implements it); gated the same analyst+ level as the contract's
    # own exclude route (`DELETE .../suppliers/{sid}`), not lifted to
    # administrator+.
    ("GET", "/api/v1/rfqs"): Role.VIEWER,
    ("POST", "/api/v1/rfqs"): Role.ANALYST,
    ("GET", "/api/v1/rfqs/{rfq_id}"): Role.VIEWER,
    ("PATCH", "/api/v1/rfqs/{rfq_id}"): Role.ANALYST,
    ("POST", "/api/v1/rfqs/{rfq_id}/status"): Role.ANALYST,
    ("GET", "/api/v1/rfqs/{rfq_id}/status-history"): Role.VIEWER,
    ("GET", "/api/v1/rfqs/{rfq_id}/lines"): Role.VIEWER,
    ("POST", "/api/v1/rfqs/{rfq_id}/lines"): Role.ANALYST,
    ("PATCH", "/api/v1/rfqs/{rfq_id}/lines/{line_id}"): Role.ANALYST,
    ("DELETE", "/api/v1/rfqs/{rfq_id}/lines/{line_id}"): Role.ANALYST,
    ("GET", "/api/v1/rfqs/{rfq_id}/suppliers"): Role.VIEWER,
    ("POST", "/api/v1/rfqs/{rfq_id}/suppliers"): Role.ANALYST,
    ("DELETE", "/api/v1/rfqs/{rfq_id}/suppliers/{rfq_supplier_id}"): Role.ANALYST,
    ("POST", "/api/v1/rfqs/{rfq_id}/suppliers/{rfq_supplier_id}/reinstate"): Role.ANALYST,
    # Quotes (03-api-contract.md §4.11), manual entry only. List/get are
    # viewer+; create/update/supersede are analyst+ (mirrors the RFQ mutation
    # gate). `archive` has no contract-table entry of its own (see
    # api/v1/quotes.py module docstring for why this router still implements
    # it); gated administrator+ like every other archivable resource's
    # archive route (suppliers/parts/BOMs), not the analyst+ level of create.
    ("GET", "/api/v1/rfqs/{rfq_id}/quotes"): Role.VIEWER,
    ("POST", "/api/v1/rfqs/{rfq_id}/quotes"): Role.ANALYST,
    ("GET", "/api/v1/quotes/{quote_id}"): Role.VIEWER,
    ("PATCH", "/api/v1/quotes/{quote_id}"): Role.ANALYST,
    ("POST", "/api/v1/quotes/{quote_id}/supersede"): Role.ANALYST,
    ("POST", "/api/v1/quotes/{quote_id}/archive"): Role.ADMINISTRATOR,
}

# Routes that are org-scoped resources (subject to the 404 cross-org matrix
# test). Phase 2+ adds every business resource here as it lands.
ORG_SCOPED_RESOURCES: list[str] = [
    "suppliers",
    "supplier_contacts",
    "supplier_performance",
    "parts",
    "part_alternatives",
    "part_imports",
    "boms",
    "exchange_rates",
    "rfqs",
    "quotes",
]
