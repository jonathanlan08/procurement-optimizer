# Architecture

How the Procurement Optimizer is actually put together, as built in v0.1.0. Every path
cited here exists in the repository; where the implementation deviates from the planning
documents, the deviation is stated rather than smoothed over.

Related: [DATABASE.md](DATABASE.md) · [SECURITY.md](SECURITY.md) ·
[METHODOLOGY.md](METHODOLOGY.md) · [OPTIMIZATION.md](OPTIMIZATION.md) ·
[DOCUMENT_PIPELINE.md](DOCUMENT_PIPELINE.md) · [DEPLOYMENT.md](DEPLOYMENT.md)

---

## 1. Shape of the system

```
Browser (React SPA, Vite dev server :5173)
   │  same-origin /api proxy  →  http://localhost:${BACKEND_PORT:-8000}
   ▼
FastAPI application  (backend/src/app/main.py :: create_app)
   │
   ├── middleware chain: CORS → RequestId → SecurityHeaders → OriginCheck → RateLimit
   │
   ├── routers          backend/src/app/api/v1/*.py     (sync `def`, thin)
   │      │  Depends: current_principal → Principal(user, organization_id, role)
   │      ▼
   ├── services         backend/src/app/services/*.py   (business logic, never commit)
   │      │
   │      ├── domain    backend/src/app/domain/**       (pure: no I/O, no clock, no RNG)
   │      ├── providers backend/src/app/providers/**    (Protocols + deterministic defaults)
   │      ▼
   ├── repositories     backend/src/app/repositories/*.py (org-scoped queries only)
   │      ▼
   └── SQLAlchemy 2 models  backend/src/app/models/*.py  → PostgreSQL
```

The request-scoped unit of work lives in `backend/src/app/api/deps.py::get_db`: it opens
one session per request, commits on success, rolls back on any exception, and closes in a
`finally`. **Services never call `commit()`** — that rule is repeated in every service
module docstring (`backend/src/app/services/scenario_service.py`,
`backend/src/app/services/brief_service.py`, `backend/src/app/services/landed_cost_service.py`,
…). One HTTP request is at most one database transaction.

One deliberate exception to "one request, one transaction" exists, documented at its call
site:

- `AuthService._record_failed_login` (`backend/src/app/services/auth.py`) writes the
  failed-login counter in its **own** transaction, because the request transaction is
  rolled back when login raises. Without this the lockout counter would be rolled back
  along with the failure it was counting (Phase 1 review finding #1, see
  `docs/planning/00-decisions.md` §7).

## 2. Routers are sync `def`, on purpose

Every DB-touching route handler is a synchronous `def`, not `async def`. FastAPI runs sync
handlers in its threadpool, which is correct for the synchronous SQLAlchemy `Session` this
codebase uses; an `async def` handler holding a sync session would block the event loop.
This was ratified in `docs/planning/00-decisions.md` §1.4 and is visible throughout
`backend/src/app/api/v1/`.

Handlers are thin: they resolve a `Principal`, construct the service with request-scoped
collaborators (`Session`, `Clock`, `IdGenerator`, `AuditRecorder`), call one service
method, and map the result into a Pydantic response schema. No arithmetic happens in a
route.

## 3. The four-layer organization-isolation stack

Cross-organization access is designed to be impossible at four independent layers, so that
a bug in any one of them is not sufficient to leak data. The layers are numbered
consistently in the source.

| # | Control | Where |
|---|---|---|
| 1 | The client never chooses the org. `Principal.organization_id` comes from the session row (`sessions.active_organization_id`); any `organization_id` in a body or query string is ignored. | `backend/src/app/api/deps.py` |
| 2 | `OrgScopedRepository` is generic over `OrgOwnedBase`, so `organization_id` is statically typed and every query starts from `_base_query()`, which is already filtered. A post-fetch assertion raises `OrgIsolationViolation` if a row ever escapes the filter. | `backend/src/app/repositories/base.py` |
| 3 | Composite foreign keys: every business table carries `UNIQUE (organization_id, id)` (`org_identity_constraint` in `backend/src/app/models/base.py`) and children reference parents as `(organization_id, parent_id) → parent(organization_id, id)`. A cross-org reference is refused by PostgreSQL itself. | `backend/src/app/models/*.py`, migrations `0002`–`0014` |
| 4 | A contract test walks the live FastAPI route table (via the generated OpenAPI schema) and fails when any route lacks a declaration in the permission matrix; integration suites assert **404, not 403**, for cross-org access on every resource. | `backend/tests/contract/test_permission_matrix.py`, `backend/src/app/api/permissions.py` |

Cross-org lookups return `None` from the repository and surface as `404 not_found` — never
`403` — because existence itself must not leak (`backend/src/app/core/errors.py`,
`NotFoundError` docstring).

## 4. Middleware chain

Declared in `backend/src/app/main.py`; Starlette runs the *last added* middleware first, so
the file adds them in reverse of the effective order. Effective order, outermost first:

1. **CORS** — `allow_origins=settings.allowed_origins`, credentials on, methods limited to
   `GET/POST/PUT/PATCH/DELETE`, headers limited to `Content-Type`, `X-CSRF-Token`,
   `If-Match`; exposes `X-Request-ID`.
2. **`RequestIdMiddleware`** — assigns `request.state.request_id` (uuid4 hex) and echoes it
   in the `X-Request-ID` response header. Every error envelope carries the same id.
3. **`SecurityHeadersMiddleware`** — sets `X-Content-Type-Options: nosniff`,
   `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
   `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'`,
   `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`,
   plus HSTS when `PO_ENVIRONMENT=prod`. The CSP is that strict because the API serves JSON
   only; the SPA is served separately.
4. **`OriginCheckMiddleware`** — Origin/Referer allowlist on **every** mutating `/api/`
   request, including `POST /api/v1/auth/login`, which has no session yet and would
   otherwise be exposed to login-CSRF.
5. **`RateLimitMiddleware`** — in-memory sliding window per client IP;
   `PO_RATE_LIMIT_AUTH_PER_MINUTE` (default 10) for `/api/v1/auth/login`,
   `PO_RATE_LIMIT_PER_MINUTE` (default 120) elsewhere. Because exceptions raised inside
   middleware bypass FastAPI's exception handlers, this middleware renders the standard
   error envelope **directly** as a `JSONResponse` with `Retry-After: 60`. Key eviction is
   bounded at 10 000 keys to survive address-spraying. Single-node scope: a multi-node
   deployment needs a shared store (documented limitation, see [ROADMAP.md](ROADMAP.md)).

`OriginCheckMiddleware` deliberately duplicates the Origin check that
`current_principal` also performs. The middleware covers login (no session, so no
dependency chain); the dependency covers everything else together with the per-session CSRF
token.

## 5. Error envelope

One shape for every non-2xx response (`backend/src/app/core/errors.py`):

```json
{"error": {"code", "message", "status", "details", "request_id", "timestamp"}}
```

`ErrorCode` is a closed enum mapped to fixed HTTP statuses (`validation_error` 422,
`unauthenticated` 401, `csrf_failed` / `forbidden_role` 403, `not_found` 404,
`conflict_state` / `conflict_version` / `conflict_duplicate` 409,
`unsupported_media_type` 415, `payload_too_large` 413, `rate_limited` 429,
`provider_unavailable` 502, `internal_error` 500). Unexpected exceptions render
`SafeInternalError`: a generic message plus the request id, never a stack trace, SQL
fragment, or file path. `ErrorDetail.value_hint` is truncated to 64 characters and
suppressed entirely for secret-looking field names.

## 6. Providers are Protocols with deterministic defaults

Every external dependency sits behind a `typing.Protocol` so the demo runs offline and the
tests never touch a network.

| Protocol | Default implementation | Alternatives |
|---|---|---|
| `StorageProvider` (`backend/src/app/providers/storage/base.py`) | `filesystem.py` (org-namespaced `root/<org_id>/<key>`) | `s3.py` (MinIO/S3), `memory.py` (tests) |
| `ExtractionProvider` (`backend/src/app/providers/extraction/base.py`) | `mock.py` — sha256-keyed golden fixtures, `is_simulated = True` | `anthropic` selectable in config; **adapter not implemented in this build** |
| `AiNarrativeProvider` (`backend/src/app/providers/narrative/base.py`) | `template.py` — deterministic, `is_generated = False` | `anthropic` selectable in config; **adapter not implemented in this build** |
| FX rates (`backend/src/app/providers/fx/base.py`) | `synthetic.py` — a fixed USD-base table, source label `synthetic_fixture` | none |
| `Clock` / `IdGenerator` | `SystemClock` / `RandomIdGenerator` (`backend/src/app/core/clock.py`, `ids.py`) | injected fakes in tests |

Selecting `anthropic` for extraction or narrative raises `ProviderUnavailableError` rather
than silently substituting the mock — the codebase never presents a simulated response as
live. `Settings` also fails fast at startup if `PO_EXTRACTION_PROVIDER=anthropic` or
`PO_NARRATIVE_PROVIDER=anthropic` is set without `PO_ANTHROPIC_API_KEY`
(`backend/src/app/core/config.py::Settings._fail_fast`).

## 7. Jobs: inline, and honestly so

`Settings.job_runner` exists (`inline | thread`, default `thread`) and `jobs` is a real
table created by migration `0001`, but **nothing in `backend/src/app/` reads that setting or
writes a `jobs` row**. Every long-running operation — extraction runs, part matching,
scenario solve, brief generation — executes synchronously inside the request and returns
`201` with the completed resource.

This is the `JOB_RUNNER=inline` shape that `docs/planning/03-api-contract.md` §1.5
documents as the test-mode alternative to a `202 Accepted` + job-polling envelope; in this
build it is the **only** shape implemented, in every environment. The decision and its
consequences are recorded in the module docstrings of
`backend/src/app/services/extraction_service.py`,
`backend/src/app/api/v1/extractions.py`, `backend/src/app/api/v1/matching.py`,
`backend/src/app/api/v1/scenarios.py`, `backend/src/app/services/scenario_service.py`, and
`backend/src/app/services/brief_service.py`. A real queue is on the
[roadmap](ROADMAP.md); adding it would introduce the `202` path without changing the
service method signatures.

Practical consequence: a scenario solve holds an HTTP request open for the duration of the
CP-SAT search (bounded by `max_deterministic_time=30.0` and `max_time_in_seconds=120.0` in
`backend/src/app/domain/optimization/solver.py::AllocationSolver._make_solver`).

## 8. Domain layer

`backend/src/app/domain/` contains pure code — no database, no clock, no network, no
randomness — so financial correctness is testable without a database (a SPEC requirement).

- `domain/values.py` — `Quantified` (a `Decimal` plus `Provenance`), `Completeness`. The
  invariant `value is None ⟺ provenance is MISSING` is enforced in `__post_init__`.
- `domain/confidence.py` — the single source of truth for confidence bands (0.95 / 0.60).
- `domain/landed_cost/` — `contracts.py` (frozen dataclasses + formulas), `calculator.py`
  (`LandedCostCalculatorV1`), `breaks.py` (all-units price-break selection).
- `domain/scoring/` — `contracts.py` (criteria, directions, `SAMPLE_WEIGHTS`),
  `scorer.py` (`ScorerV1`).
- `domain/optimization/` — `contracts.py` (CP-SAT model shape, honest statuses),
  `solver.py` (`AllocationSolver`).
- `domain/fx/normalize.py`, `domain/units/normalize.py`, `domain/matching/matcher.py`.

Modules named `contracts.py` are principal-owned and frozen; implementations state, in
their own module docstrings, every place they interpret the contract's prose.

## 9. Frontend

A React 18 + TypeScript SPA built with Vite (`frontend/`). Routing is `react-router-dom`;
server state is TanStack Query; forms are `react-hook-form` + `zod` (through a small local
resolver, `frontend/src/lib/zodResolver.ts`); tables are TanStack Table. Fonts are
self-hosted via `@fontsource` — no CDN, so the demo works offline and the CSP stays tight.

```
frontend/src/
  api/client.ts        typed fetch wrapper: same-origin cookie, X-CSRF-Token on mutations,
                       parses the single error envelope into ApiError
  auth/session.tsx     session context + RequireAuth route guard
  layout/AppShell.tsx  sidebar + header shell, demo badge, role display
  components/          DataTable, Drawer, StatusBadge, FormField, PaginationBar, …
  lib/money.ts         decimal-string formatting via BigInt — never Number()/parseFloat
  features/            suppliers · parts · boms · rfqs · quotes · documents ·
                       extraction · comparison · fx · briefs · reports · audit
  pages/               LoginPage, PlaceholderPage
```

Routes are declared in `frontend/src/App.tsx`; navigation in
`frontend/src/layout/AppShell.tsx`. Only the `/` overview route still renders a
`PlaceholderPage`.

One layer sits outside the router: `backend/src/app/reports/` is a small,
renderer-agnostic export subsystem. `document.py` defines a `ReportDocument` (title,
generated-at, calculation version, methodology, disclaimer, missing-data disclosure, plus
`TableBlock`/`KeyValueBlock`/`TextBlock` sections) that `ReportService` builds once from
stored rows; `csv_renderer.py`, `xlsx_renderer.py`, and `pdf_renderer.py` each render that
same document to bytes. Content is decided once, upstream of any format concern, and the
SPEC-mandated header material is a field on the shared model rather than something each
renderer must remember to print.

Money never becomes a JavaScript `number`. `frontend/src/lib/money.ts` validates decimal
strings with a regex and does half-even rounding with `BigInt` string arithmetic, matching
the backend's banker's rounding exactly.

The Vite dev server proxies `/api` to `http://localhost:${BACKEND_PORT:-8000}` with
`changeOrigin: false`, so the browser sees one origin: the session cookie is same-origin and
the Origin allowlist check passes without extra configuration.

## 10. Contract integrity

`frontend/package.json` defines `npm run generate:api`, which runs `openapi-typescript` over
`../docs/openapi.json`. **`docs/openapi.json` is not committed in this build**, and
`.github/workflows/ci.yml` has no OpenAPI-drift job — both were ratified in
`docs/planning/00-decisions.md` §1.4 but not delivered. The frontend's API modules are
hand-written types today (`frontend/src/features/*/api.ts`). Closing this gap is on the
[roadmap](ROADMAP.md).

What *is* enforced in CI: `ruff check`, `mypy` (strict, whole `app` package), an
up→down→up Alembic migration cycle on an empty database, the full pytest suite with
coverage, frontend typecheck + tests + build, and a gitleaks secrets scan.

## 11. Deployment topology

Application processes run on the host; Docker Compose provides only the backing services
(`postgres`, `minio`). See [DEPLOYMENT.md](DEPLOYMENT.md).
