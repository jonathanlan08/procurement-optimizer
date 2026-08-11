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
    # Quote documents (03-api-contract.md §4.9): "O A N V" for every read
    # route (list/get/content), "O A N" for upload, "O A" (administrator+)
    # for archive — mirroring every other archivable resource's archive
    # gate (suppliers/parts/BOMs/quotes). See app/services/document_service.py
    # and api/v1/documents.py module docstrings for the create/list nesting
    # under the RFQ vs. the contract's own terse route-table shorthand.
    ("POST", "/api/v1/rfqs/{rfq_id}/quote-documents"): Role.ANALYST,
    ("GET", "/api/v1/rfqs/{rfq_id}/quote-documents"): Role.VIEWER,
    ("GET", "/api/v1/quote-documents/{document_id}"): Role.VIEWER,
    ("GET", "/api/v1/quote-documents/{document_id}/content"): Role.VIEWER,
    ("POST", "/api/v1/quote-documents/{document_id}/archive"): Role.ADMINISTRATOR,
    # Extraction (03-api-contract.md §4.10): "O A N" for starting a run and
    # for every field-review/materialize mutation, "O A N V" for every read
    # route (run history, run detail, field list). See
    # api/v1/extractions.py's own module docstring for why these paths
    # follow the contract's literal route table rather than this task's own
    # paraphrase (POST .../extractions, POST .../confirm+.../correct,
    # POST .../materialize).
    ("POST", "/api/v1/quote-documents/{document_id}/extraction-runs"): Role.ANALYST,
    ("GET", "/api/v1/quote-documents/{document_id}/extraction-runs"): Role.VIEWER,
    ("GET", "/api/v1/extraction-runs/{run_id}"): Role.VIEWER,
    ("GET", "/api/v1/extraction-runs/{run_id}/fields"): Role.VIEWER,
    ("PATCH", "/api/v1/extraction-runs/{run_id}/fields/{field_id}"): Role.ANALYST,
    ("POST", "/api/v1/extraction-runs/{run_id}/confirm"): Role.ANALYST,
    # Part matching (03-api-contract.md §4.12): "O A N" for generate/
    # confirm/unmatch, "O A N V" for the ranked-candidates read route. See
    # api/v1/matching.py's own module docstring for why these paths follow
    # the contract's literal route table rather than this task's own
    # paraphrase (POST/GET .../match-candidates, POST
    # /match-candidates/{id}/confirm|reject).
    ("POST", "/api/v1/quotes/{quote_id}/match"): Role.ANALYST,
    ("GET", "/api/v1/quotes/{quote_id}/matches"): Role.VIEWER,
    ("POST", "/api/v1/quote-lines/{quote_line_id}/match"): Role.ANALYST,
    ("DELETE", "/api/v1/quote-lines/{quote_line_id}/match"): Role.ANALYST,
    # Landed cost (03-api-contract.md §4.15). Preview is the contract's own
    # literal "O A N V" — a stateless calculator, no audit row, viewer-
    # readable (§6 gap #2), NOT the analyst+ this task's own paraphrase
    # states; see api/v1/analysis.py module docstring point 1 for why the
    # contract's literal role wins. Persisting and the per-RFQ latest-per-
    # line listing follow this task's own explicit "analyst+"/"viewer+"
    # split.
    ("POST", "/api/v1/landed-costs:preview"): Role.VIEWER,
    ("POST", "/api/v1/rfqs/{rfq_id}/landed-costs"): Role.ANALYST,
    ("GET", "/api/v1/rfqs/{rfq_id}/landed-costs"): Role.VIEWER,
    ("GET", "/api/v1/landed-cost-results/{result_id}"): Role.VIEWER,
    # Scoring configurations (03-api-contract.md §4.14): "O A N V" for list,
    # "O A N" for create/update, "O A" (administrator+) for archive.
    ("GET", "/api/v1/scoring-configurations"): Role.VIEWER,
    ("POST", "/api/v1/scoring-configurations"): Role.ANALYST,
    ("PATCH", "/api/v1/scoring-configurations/{config_id}"): Role.ANALYST,
    ("POST", "/api/v1/scoring-configurations/{config_id}/archive"): Role.ADMINISTRATOR,
    # Comparison scenarios (03-api-contract.md §4.16): "O A N V" for every
    # read route (list/get/results/allocation), "O A N" for create/optimize/
    # clone, "O A" (administrator+) for archive — mirroring every other
    # archivable resource's archive gate. See api/v1/scenarios.py's own
    # module docstring for why create/optimize/clone run synchronously and
    # why optimize is idempotent rather than a fresh solve.
    ("GET", "/api/v1/rfqs/{rfq_id}/comparison-scenarios"): Role.VIEWER,
    ("POST", "/api/v1/rfqs/{rfq_id}/comparison-scenarios"): Role.ANALYST,
    ("GET", "/api/v1/comparison-scenarios/{scenario_id}"): Role.VIEWER,
    ("GET", "/api/v1/comparison-scenarios/{scenario_id}/results"): Role.VIEWER,
    ("GET", "/api/v1/comparison-scenarios/{scenario_id}/allocation"): Role.VIEWER,
    ("POST", "/api/v1/comparison-scenarios/{scenario_id}/optimize"): Role.ANALYST,
    ("POST", "/api/v1/comparison-scenarios/{scenario_id}/clone"): Role.ANALYST,
    ("POST", "/api/v1/comparison-scenarios/{scenario_id}/archive"): Role.ADMINISTRATOR,
    # Negotiation briefs (03-api-contract.md §4.17). Generate/review are
    # "O A N" per the contract's literal table; list routes (both nestings)
    # follow every other resource's "O A N V" read gate; archive is this
    # task's own explicit addition, gated administrator+ like every other
    # archivable resource's archive route — see api/v1/briefs.py's own
    # module docstring for why there is no PATCH edit route and no
    # `/email-draft` route in this build (the latter deliberately: SPEC
    # "never auto-send", and this task's own test requirement is "assert no
    # such route exists").
    ("POST", "/api/v1/comparison-scenarios/{scenario_id}/negotiation-briefs"): Role.ANALYST,
    ("GET", "/api/v1/comparison-scenarios/{scenario_id}/negotiation-briefs"): Role.VIEWER,
    ("GET", "/api/v1/rfqs/{rfq_id}/negotiation-briefs"): Role.VIEWER,
    ("GET", "/api/v1/negotiation-briefs/{brief_id}"): Role.VIEWER,
    ("POST", "/api/v1/negotiation-briefs/{brief_id}/review"): Role.ANALYST,
    ("POST", "/api/v1/negotiation-briefs/{brief_id}/archive"): Role.ADMINISTRATOR,
    # Reports (03-api-contract.md §4.18): "O A N" for generate, "O A N V"
    # for every read route (list/get/content) — the contract's own literal
    # answer to its §6 gap #1 ("should viewer download reports?"): yes. See
    # api/v1/reports.py's own module docstring for why generate returns
    # 201 synchronously and why content returns a hand-built 410 on purge.
    ("POST", "/api/v1/reports"): Role.ANALYST,
    ("GET", "/api/v1/reports"): Role.VIEWER,
    ("GET", "/api/v1/reports/{report_id}"): Role.VIEWER,
    ("GET", "/api/v1/reports/{report_id}/content"): Role.VIEWER,
    # Audit events (03-api-contract.md §4.19): "O A N V" on every route —
    # the contract's own parenthetical spells out viewer read-own-org
    # access explicitly. No write/update/delete route exists anywhere for
    # audit events (see api/v1/audit.py's own module docstring); there is
    # therefore nothing to declare here but GET.
    ("GET", "/api/v1/audit-events"): Role.VIEWER,
    ("GET", "/api/v1/audit-events/{event_id}"): Role.VIEWER,
    ("GET", "/api/v1/entities/{entity_type}/{entity_id}/audit-events"): Role.VIEWER,
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
    "quote_documents",
    "extraction_runs",
    "extraction_fields",
    "part_match_candidates",
    "landed_cost_results",
    "scoring_configurations",
    "comparison_scenarios",
    "negotiation_briefs",
    "generated_reports",
    "audit_events",
]
