# 03 — REST API Contract

Status: **DRAFT FOR PRINCIPAL REVIEW**
Base path: `/api/v1`. Media type: `application/json; charset=utf-8` (uploads: `multipart/form-data`).
The generated OpenAPI document is committed at `docs/openapi.json` and CI fails on undeclared drift.

---

## 1. Conventions

### 1.1 Identity and scope
- Auth is a session cookie: `sid=<opaque>; HttpOnly; Secure; SameSite=Lax; Path=/`.
- Unsafe methods require **both** `X-CSRF-Token` matching the `csrf` cookie **and** an `Origin`/
  `Referer` in the configured allowlist.
- **Organization scope is never taken from the request body or a query parameter.** It comes from the
  session's `active_organization_id`. Switching orgs is an explicit
  `POST /auth/session/organization` call. Any `organization_id` appearing in a payload is ignored and
  logged as a suspicious-input security event.
- Cross-org access returns **`404`**, never `403`.

### 1.2 Naming and shapes
- Resources plural and kebab-free (`/quote-documents` → `/quote_documents`? no): use **hyphen-free
  snake segments are ugly in URLs**, so: lowercase plural nouns with hyphens — `/quote-documents`,
  `/comparison-scenarios`, `/exchange-rates`. JSON fields are `snake_case` (matches Pydantic and the
  DB; the SPA's generated types follow).
- Timestamps: RFC 3339 UTC with `Z`. Dates: `YYYY-MM-DD`.
- **Money and decimals are JSON strings, never numbers.** `"unit_price": "10.500000"`. A JSON number
  is an IEEE double the moment any JS parser touches it; the SPEC forbids floating-point money and
  this is where it would leak in. Amounts always travel as an object where ambiguity is possible:
  `{"amount": "1234.560000", "currency": "USD"}`.
- Nullable vs absent: a field that the supplier did not provide is `null` **and** listed in the
  resource's `missing_fields` array. The API never substitutes `0`.

### 1.3 Pagination
Two modes, chosen per endpoint and documented in the route table:

- **Cursor (default for high-volume lists):** `?limit=50&cursor=<opaque>`
  ```json
  { "items": [ ... ],
    "page": { "limit": 50, "next_cursor": "eyJ...", "prev_cursor": null, "has_more": true } }
  ```
  Cursor encodes the last `(sort_key, id)` — keyset pagination, stable under insert.
- **Offset (small, bounded lists):** `?limit=50&offset=0`, adds `"total": 137` to `page`.

`limit` default 50, max 200. Unknown query params are rejected with `422` (typo-proofing).

### 1.4 Filtering and sorting
- Explicit, typed query parameters only — no generic query language. e.g.
  `GET /rfqs?status=open&status=under_review&due_before=2026-09-01&q=bracket&sort=-created_at`.
- Repeated param = OR within a field; different params = AND.
- `q` is a free-text search over a documented, per-resource field list.
- `sort` accepts a documented allowlist with `-` prefix for descending. Anything else → `422`.
- `include_archived=true` required to see soft-deleted rows.

### 1.5 Long-running operations
`POST` endpoints that trigger extraction, optimization, or report rendering return
**`202 Accepted`** with `Location: /api/v1/jobs/{job_id}` and body:
```json
{ "job": { "id": "...", "kind": "extraction.run", "state": "queued",
           "resource_type": "extraction_run", "resource_id": "..." } }
```
`GET /jobs/{id}` returns `state ∈ queued|running|succeeded|failed|cancelled`, plus `result_ref` and a
structured `error` when failed. The SPA polls with TanStack Query (2 s interval, exponential backoff,
30 s ceiling). In `JOB_RUNNER=inline` (tests) the same endpoints return `201` with the completed
resource — documented, so contract tests assert both shapes.

### 1.6 Idempotency
`POST` endpoints that create expensive or duplicable resources (uploads, extraction runs,
optimization runs, report generation) accept `Idempotency-Key: <uuid>`. Same key + same org + same
body hash within 24 h returns the original response. Stored in a small `idempotency_keys` table.

### 1.7 Concurrency
`GET` of mutable aggregates returns `ETag: "<version>"`. `PATCH`/`PUT` must send `If-Match`; a
mismatch returns `409 conflict_version` with the current representation. Applied to suppliers, parts,
RFQs, quotes, scoring configurations.

### 1.8 Rate limits
Per-session and per-IP token buckets. `429` includes `Retry-After` and
`X-RateLimit-{Limit,Remaining,Reset}`. Tighter buckets on `/auth/login` (5/min/IP, 10/hour/email),
uploads (20/min/org), and optimization (10/min/org).

---

## 2. Roles

| Role | Intent |
|---|---|
| `owner` | Everything, including org settings, membership changes, org archive. |
| `administrator` | Everything except org deletion/ownership transfer. |
| `analyst` | Full operational work: create/edit master data, upload, review, calculate, optimize, generate. Cannot manage members or org settings. |
| `viewer` | Read-only, including downloading reports. Cannot upload, correct, confirm, calculate, or export-generate. |

The matrix below is the single source of truth and is **read by the route-matrix test** in
`08-test-strategy.md`; it is generated from a declarative table in `app/api/permissions.py`, not from
scattered `if` statements.

---

## 3. Error format

Single envelope for every non-2xx, aligned with RFC 9457 field names where sensible:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request body failed validation.",
    "status": 422,
    "details": [
      { "field": "lines[0].unit_price", "issue": "must be a decimal string >= 0", "value_hint": "-3" }
    ],
    "request_id": "01J9…",
    "timestamp": "2026-08-10T12:00:00Z",
    "doc_url": "https://…/docs/errors#validation_error"
  }
}
```

Rules: `message` is safe for display and never contains SQL, stack traces, file paths, or another
org's data. `details[].value_hint` is truncated to 64 chars and omitted for secret-ish fields.
`request_id` also appears as a response header and in the structured log.

| `code` | HTTP | When |
|---|---|---|
| `validation_error` | 422 | Pydantic / business validation failure |
| `unauthenticated` | 401 | no/expired session |
| `csrf_failed` | 403 | missing/mismatched CSRF token or bad Origin |
| `forbidden_role` | 403 | authenticated, in-org, insufficient role |
| `not_found` | 404 | absent **or** other-org resource |
| `conflict_state` | 409 | illegal state transition (e.g. confirm an already-superseded quote) |
| `conflict_version` | 409 | `If-Match` mismatch |
| `conflict_duplicate` | 409 | uniqueness violation (duplicate supplier code, duplicate upload) |
| `unsupported_media_type` | 415 | file type not in allowlist |
| `payload_too_large` | 413 | over `MAX_UPLOAD_BYTES` |
| `rate_limited` | 429 | bucket exhausted |
| `provider_unavailable` | 502 | external provider failed (never silently mocked) |
| `solver_error` | 200 | **not an HTTP error** — reported inside the allocation result (§4.16) |
| `internal_error` | 500 | unexpected; generic message + `request_id` only |

Note the deliberate exception: an infeasible or failed **solve is a successful HTTP response** with an
honest status inside. Turning infeasibility into a 4xx would hide it from the audit trail.

---

## 4. Route table

Legend for roles: `O` owner, `A` administrator, `N` analyst, `V` viewer. `—` = not allowed.

### 4.1 Meta and health
| Method | Path | Roles | Request | Response | Notes |
|---|---|---|---|---|---|
| GET | `/healthz` | public | — | `{status, version}` | liveness; no DB |
| GET | `/readyz` | public | — | `{status, checks:{db,storage}}` | readiness |
| GET | `/meta` | O A N V | — | `{app_version, calculation_version, solver_version, ai_mode:"simulated"\|"live", demo_mode, providers:{...}}` | **drives the UI's "Simulated AI" banner** |

### 4.2 Authentication — `/auth`
| Method | Path | Roles | Request | Response |
|---|---|---|---|---|
| POST | `/auth/login` | public | `{email, password}` | `200 {user, memberships[], active_organization}` + `sid`/`csrf` cookies. Generic failure message; constant-time; rate limited. |
| POST | `/auth/logout` | any | — | `204`, session revoked |
| GET | `/auth/session` | any | — | `{user, active_organization, role, permissions[], csrf_token}` |
| POST | `/auth/session/organization` | any | `{organization_id}` | `200`, rotates session id; 404 if not a member |
| POST | `/auth/password` | any | `{current_password, new_password}` | `204`, revokes all other sessions |
| GET | `/auth/sessions` | any | — | list of active sessions (device/ip/last_seen) |
| DELETE | `/auth/sessions/{id}` | any (own) | — | `204` |

No self-service registration in v0.1.0; demo access is a seeded credential (see `07-security-model.md` §9).

### 4.3 Organizations and memberships
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/organizations/current` | O A N V | current org profile |
| PATCH | `/organizations/current` | O A | name, base_currency; `If-Match` |
| GET | `/memberships` | O A | list members (cursor) |
| POST | `/memberships` | O A | `{email, role}`; creates user if absent (invite flow stubbed in v0.1.0) |
| PATCH | `/memberships/{id}` | O A | `{role}`; owner role assignable by `O` only; cannot demote the last owner (`409 conflict_state`) |
| DELETE | `/memberships/{id}` | O A | revoke (soft); cannot revoke last owner |

### 4.4 Suppliers — `/suppliers`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/suppliers` | O A N V | filters: `q, country, is_active, include_archived`; sort allowlist `name,code,created_at`; cursor |
| POST | `/suppliers` | O A N | body: name, code, country_code, supported_currencies[], payment/shipping terms, lead time, capacity, moq, notes |
| GET | `/suppliers/{id}` | O A N V | includes contacts + latest performance summary |
| PATCH | `/suppliers/{id}` | O A N | `If-Match` |
| POST | `/suppliers/{id}/archive` | O A | `{reason}` → 409 with `blockers[]` if referenced by an open RFQ |
| POST | `/suppliers/{id}/unarchive` | O A | |
| GET/POST | `/suppliers/{id}/contacts` | O A N (V read) | |
| PATCH/DELETE | `/suppliers/{id}/contacts/{cid}` | O A N | delete = archive |
| GET | `/suppliers/{id}/performance` | O A N V | records list |
| POST | `/suppliers/{id}/performance` | O A N | `{period_start, period_end, on_time_delivery_rate, defect_rate, quality_score, orders_count, source, notes}`; rates are decimal strings 0–1 |

### 4.5 Parts — `/parts`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/parts` | O A N V | filters `q, category, is_active, include_archived` |
| POST | `/parts` | O A N | |
| GET/PATCH | `/parts/{id}` | O A N V / O A N | |
| POST | `/parts/{id}/archive` | O A | |
| GET/POST | `/parts/{id}/alternatives` | O A N V / O A N | `{alternative_part_id?|alternative_mpn?, approval_status, rationale}` |
| DELETE | `/parts/{id}/alternatives/{aid}` | O A N | |
| POST | `/part-imports` | O A N | `multipart` CSV/XLSX → **preview batch**, `201 {batch_id, rows_total, rows_valid, rows_invalid, rows_duplicate, sample_rows[], errors[]}`. Nothing is written to `parts` yet. |
| GET | `/part-imports/{id}` | O A N V | full row-level preview, cursor-paginated |
| POST | `/part-imports/{id}/commit` | O A N | single transaction; `200 {created, updated, skipped}` or `409` with the failing row; **all-or-nothing** |
| POST | `/part-imports/{id}/cancel` | O A N | marks rolled_back |

### 4.6 Units — `/units`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/units` | O A N V | global + org-defined |
| POST | `/units` | O A N | user-defined unit `{code, display_name, dimension, to_canonical_factor}` |
| GET | `/unit-conversions` | O A N V | filter by `part_id` |
| POST | `/unit-conversions` | O A N | `{from_unit_id, to_unit_id, part_id?, factor, assumption_note}` |

### 4.7 BOMs — `/boms`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/boms` | O A N V | latest version per `root_bom_id` by default; `?all_versions=true` |
| POST | `/boms` | O A N | creates v1 |
| GET | `/boms/{id}` | O A N V | with lines |
| POST | `/boms/{id}/versions` | O A N | copy-on-write new version from a full line payload; returns new id |
| GET | `/boms/{root_id}/versions` | O A N V | version history |
| PATCH | `/boms/{id}` | O A N | metadata only while `draft`; line edits on an `active` BOM require a new version (`409 conflict_state`) |
| POST | `/boms/{id}/archive` | O A | |

### 4.8 RFQs — `/rfqs`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/rfqs` | O A N V | filters `status[], q, due_before, due_after` |
| POST | `/rfqs` | O A N | optionally `{source_bom_id, assembly_quantity}` to explode a BOM into lines |
| GET | `/rfqs/{id}` | O A N V | summary + counts (lines, invited, quotes, unresolved reviews) |
| PATCH | `/rfqs/{id}` | O A N | `If-Match`; line edits blocked once `status != draft` unless `A`/`O` with `{override_reason}` (audited) |
| POST | `/rfqs/{id}/status` | O A N | `{to_status, reason}`; validated against the transition table; writes `rfq_status_history` |
| GET | `/rfqs/{id}/status-history` | O A N V | |
| GET/POST | `/rfqs/{id}/lines` | O A N V / O A N | bulk POST accepted |
| PATCH/DELETE | `/rfqs/{id}/lines/{lid}` | O A N | |
| GET/POST | `/rfqs/{id}/suppliers` | O A N V / O A N | invite `{supplier_id[]}` |
| DELETE | `/rfqs/{id}/suppliers/{sid}` | O A N | `{exclusion_reason}` → status `excluded`, retained for audit |

### 4.9 Quote documents (upload) — `/rfqs/{rfq_id}/quote-documents`
| Method | Path | Roles | Notes |
|---|---|---|---|
| POST | `/rfqs/{id}/quote-documents` | O A N | `multipart`: `file`, `supplier_id?`. Validates magic bytes, size, page/sheet count. `201 {document, duplicate_of?}`. `415`/`413` per §3. |
| GET | `/rfqs/{id}/quote-documents` | O A N V | list with `state` |
| GET | `/quote-documents/{id}` | O A N V | metadata + page summaries; **never** a storage URL |
| GET | `/quote-documents/{id}/content` | O A N V | authorized stream; `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox` |
| GET | `/quote-documents/{id}/pages/{n}/preview` | O A N V | rendered PNG for the review UI |
| POST | `/quote-documents/{id}/archive` | O A | |

### 4.10 Extraction — `/quote-documents/{id}/extraction-runs`
| Method | Path | Roles | Notes |
|---|---|---|---|
| POST | `/quote-documents/{id}/extraction-runs` | O A N | `202` job; supersedes prior run on success |
| GET | `/quote-documents/{id}/extraction-runs` | O A N V | run history |
| GET | `/extraction-runs/{id}` | O A N V | `{state, provider, simulated, schema_version, overall_confidence, fields_requiring_review, error}` |
| GET | `/extraction-runs/{id}/fields` | O A N V | filters `requires_confirmation`, `band`, `is_confirmed`; includes `raw_value`, `normalized_value`, `confidence`, `source_page`, `source_bbox`, `injection_flagged` |
| PATCH | `/extraction-runs/{id}/fields/{fid}` | O A N | `{normalized_value?, is_confirmed, reason?}` → writes `quote_corrections` + audit |
| POST | `/extraction-runs/{id}/confirm` | O A N | materializes `quotes`/`quote_lines`/`quote_terms`; `409` if any `requires_confirmation && !is_confirmed` field remains, with the offending list |

### 4.11 Quotes — `/quotes`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/rfqs/{id}/quotes` | O A N V | includes `has_unconfirmed_low_confidence`, `missing_fields` |
| POST | `/rfqs/{id}/quotes` | O A N | **manual entry** path (SPEC §6 format list includes manual) |
| GET | `/quotes/{id}` | O A N V | full graph: lines, price breaks, terms, provenance |
| PATCH | `/quotes/{id}` | O A N | `If-Match`; edits after `confirmed` create `revision + 1` and supersede |
| GET/POST/PATCH/DELETE | `/quotes/{id}/lines[...]` | O A N (V read) | |
| PUT | `/quotes/{id}/lines/{lid}/price-breaks` | O A N | full replace; validates non-overlap and contiguity (gap = warning) |
| PUT | `/quotes/{id}/terms` | O A N | |
| GET | `/quotes/{id}/corrections` | O A N V | correction history |
| POST | `/quotes/{id}/confirm` | O A N | `409` if unresolved low-confidence fields or unconfirmed non-exact matches |

### 4.12 Part matching
| Method | Path | Roles | Notes |
|---|---|---|---|
| POST | `/quotes/{id}/match` | O A N | run matching for all lines; `202` job (or sync if fast); populates `part_match_candidates` |
| GET | `/quotes/{id}/matches` | O A N V | per quote line: ranked candidates with `strategy`, `confidence`, `explanation` |
| POST | `/quote-lines/{id}/match` | O A N | `{rfq_line_id, confirmed:true, reason?}` → sets `matched_rfq_line_id`, `match_status=confirmed` |
| DELETE | `/quote-lines/{id}/match` | O A N | unmatch, audited |

### 4.13 Exchange rates — `/exchange-rates`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/exchange-rates` | O A N V | filters `base`, `quote`, `as_of` (returns the effective row plus its provenance) |
| POST | `/exchange-rates` | O A N | manual override: `{base_currency, quote_currency, rate, effective_date, override_reason}`; `is_manual_override=true`, `source="manual"` |
| POST | `/exchange-rates/refresh` | O A | pulls from the configured provider; in demo/fixture mode returns `{source:"fixture", simulated:true}` and never touches the network |

### 4.14 Scoring configurations — `/scoring-configurations`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/scoring-configurations` | O A N V | includes the seeded sample config, flagged `is_sample:true` with a display label "sample assumptions" |
| POST/PATCH | `/scoring-configurations[/{id}]` | O A N | `criteria: [{key, weight, direction, missing_policy, is_custom, label}]`; validates `sum(weight) > 0`; zero weights allowed and mean "ignore" |
| POST | `/scoring-configurations/{id}/archive` | O A | |

### 4.15 Landed cost — `/landed-costs`
| Method | Path | Roles | Notes |
|---|---|---|---|
| POST | `/landed-costs:preview` | O A N V | **stateless**: body carries quote line ids + quantities + assumptions; returns full breakdown without persisting. Used by the interactive calculator. |
| POST | `/rfqs/{id}/landed-costs` | O A N | persists `landed_cost_results` for all matched lines under given assumptions |
| GET | `/landed-cost-results/{id}` | O A N V | components, formula strings, inputs, `data_source` per component, assumptions, `missing_inputs`, `manual_overrides`, `completeness`, `calculation_version`, `calculated_at` |

Response sketch:
```json
{ "landed_cost_result": {
  "id": "...", "quote_line_id": "...", "accepted_quantity": "500.000000",
  "currency": "USD", "source_currency": "EUR", "exchange_rate": {"id":"...","rate":"1.084000000000","source":"fixture","effective_date":"2026-08-01"},
  "components": [
    {"component":"extended_material","amount":"5250.000000","formula":"normalized_unit_price * accepted_quantity",
     "inputs":{"normalized_unit_price":"10.50000000","accepted_quantity":"500.000000"},
     "data_source":"supplier","is_assumed":false},
    {"component":"quality_risk","amount":"105.000000","formula":"extended_material * quality_risk_rate",
     "inputs":{"quality_risk_rate":"0.020000"},"data_source":"user_assumption","is_assumed":true}
  ],
  "total_landed_cost":"6180.000000","effective_unit_cost":"12.36000000",
  "completeness":"assumption_dependent",
  "missing_inputs":[{"field":"tariff_amount","reason":"not stated on quote"}],
  "warnings":[{"code":"unconfirmed_low_confidence","message":"unit_price was extracted with confidence 0.62 and has not been confirmed","severity":"high"}],
  "calculation_version":"1.0.0","calculated_at":"2026-08-10T12:00:00Z" } }
```

### 4.16 Scenarios, scoring, optimization — `/comparison-scenarios`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/rfqs/{id}/comparison-scenarios` | O A N V | history, newest first |
| POST | `/rfqs/{id}/comparison-scenarios` | O A N | `{name, strategy, scoring_configuration_id, assumptions, constraints, excluded_supplier_ids[], locked_allocations[]}` → `202` job; snapshots FX + quotes at creation |
| GET | `/comparison-scenarios/{id}` | O A N V | full snapshot + state |
| GET | `/comparison-scenarios/{id}/results` | O A N V | per-supplier `scenario_results` incl. `criterion_scores` with `raw`, `normalized`, `weight`, `weighted`, `direction`, `reason`, `missing` |
| POST | `/comparison-scenarios/{id}/optimize` | O A N | `202` job → `allocation_results` |
| GET | `/comparison-scenarios/{id}/allocation` | O A N V | see below |
| POST | `/comparison-scenarios/{id}/clone` | O A N | new scenario pre-filled from an old one — the supported way to "change assumptions" without mutating history |
| POST | `/comparison-scenarios/{id}/archive` | O A | |

Allocation response sketch (honest status reporting, SPEC §Order-allocation optimization):
```json
{ "allocation_result": {
  "solver_status": "feasible",
  "status_explanation": "A feasible allocation was found but optimality was not proven within the deterministic search budget.",
  "objective_total_cost": {"amount":"184320.500000","currency":"USD"},
  "objective_source": "exact_decimal_recomputation",
  "allocations": [
    {"supplier_id":"...","rfq_line_id":"...","quantity":"600.000000",
     "price_break":{"min_quantity":"500.000000","max_quantity":"999.000000","unit_price":"9.20000000"},
     "line_landed_cost":"6210.000000"}
  ],
  "binding_constraints": ["supplier_capacity:SUP-3","max_supplier_count"],
  "infeasibility_explanation": null,
  "rejected_alternatives": [
    {"description":"single supplier SUP-1","total_cost":"191400.000000","delta":"+7079.500000","reason":"higher landed cost"},
    {"description":"single supplier SUP-4","total_cost":null,"reason":"infeasible: capacity 400 < required 1200"}
  ],
  "determinism": {"solver":"ortools-cpsat 9.x","seed":0,"workers":1,
                  "deterministic_time":"8.000000","model_hash":"sha256:…"},
  "calculation_version":"1.0.0" } }
```
Infeasible case replaces `infeasibility_explanation` with:
```json
{ "conflicting_constraint_groups": ["supplier_capacity","max_supplier_count"],
  "narrative": "Total available capacity across the 2 permitted suppliers is 900 units; 1,200 are required. Raising max_supplier_count to 3 or excluding fewer suppliers would restore feasibility.",
  "minimal_relaxations": [ {"group":"max_supplier_count","from":2,"to":3,"restores_feasibility":true} ] }
```

### 4.17 Negotiation briefs — `/negotiation-briefs`
| Method | Path | Roles | Notes |
|---|---|---|---|
| POST | `/comparison-scenarios/{id}/negotiation-briefs` | O A N | `{supplier_id, objective, targets?}` → `202` |
| GET | `/negotiation-briefs/{id}` | O A N V | every section carries `provenance ∈ supplier_provided \| user_assumption \| calculated \| ai_narrative \| missing`, plus `simulated` |
| PATCH | `/negotiation-briefs/{id}` | O A N | human edits to any section, audited |
| POST | `/negotiation-briefs/{id}/review` | O A N | `{approved:true, reviewer_notes}` → `state=human_reviewed` |
| GET | `/negotiation-briefs/{id}/email-draft` | O A N V | returns text only. **There is no send endpoint and there will not be one** (SPEC: never auto-send). |

### 4.18 Reports — `/reports`
| Method | Path | Roles | Notes |
|---|---|---|---|
| POST | `/reports` | O A N | `{scenario_id, report_type, format}` → `202`; types: `supplier_comparison`, `cfo_recommendation`, `negotiation_brief`, `scenario_summary`, `audit_history` |
| GET | `/reports` | O A N V | list with `state`, `expires_at` |
| GET | `/reports/{id}` | O A N V | metadata incl. `content_sha256` |
| GET | `/reports/{id}/content` | O A N V | authorized stream, `attachment`, `nosniff`; `410` after purge |

### 4.19 Audit — `/audit-events`
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/audit-events` | O A N (V read own-org read-only: yes) | cursor pagination on `(occurred_at, id)`; filters `event_type[]`, `entity_type`, `entity_id`, `actor_user_id`, `from`, `to` |
| GET | `/audit-events/{id}` | O A N V | includes `before_state`/`after_state` |
| GET | `/entities/{entity_type}/{entity_id}/audit-events` | O A N V | entity timeline for the UI's "history" tab |

There is no write, update, or delete route for audit events, by design.

### 4.20 Jobs and demo
| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/jobs/{id}` | O A N V | polling |
| POST | `/jobs/{id}/cancel` | O A N | best-effort |
| POST | `/demo/reset` | O A | only when `DEMO_MODE=true`; re-seeds the demo org; heavily rate limited and audited |

---

## 5. Validation rules that belong in the contract, not the code review

- Decimal strings must match `^-?\d{1,12}(\.\d{1,8})?$`; scale beyond the column's is a `422`, not a
  silent round. Rounding is a *calculation-time* concern with a stated policy; input truncation is a
  data-loss bug.
- Currency codes validated against a static ISO-4217 allowlist shipped in the repo.
- Dates: `expiration_date >= quote_date`; `due_date <= requested_delivery_date` warns, does not block.
- Quantities `> 0`; `max_quantity >= min_quantity`; price-break tiers non-overlapping.
- Every free-text field has a max length (names 200, notes 4000, reasons 1000) — unbounded text is a
  DoS surface and a UI hazard.
- File uploads: extension **and** magic bytes must both be in the allowlist and must agree.
- All string inputs are rejected, not sanitized, when they contain NUL bytes or invalid UTF-8.

---

## 6. Contract gaps I want the principal to rule on

1. **Should `viewer` be able to download reports and documents?** I have said yes (read-only includes
   reading artifacts). If the answer is no, the matrix changes in six places.
2. **`/landed-costs:preview` is a stateless POST that returns computed money without an audit event.**
   I believe that is correct (it is a calculator, not a decision) but it is the one mutation-free
   money path with no audit row.
3. **Sub-resource vs top-level ids.** I have used top-level ids for deep resources
   (`/quotes/{id}`, `/extraction-runs/{id}`) instead of full nesting, to keep URLs sane. This relies
   entirely on org scoping for authorization — which is exactly the control §1.1 and
   `01-architecture.md` §7 harden. Worth an explicit blessing.
4. **API versioning policy** — `/api/v1` frozen at v0.1.0; additive changes only, breaking changes get
   `/v2`. Needs stating in the README so the committed `openapi.json` diff review has a rule.
5. **Idempotency key storage** adds a table not in the SPEC minimum; confirm it is acceptable.
