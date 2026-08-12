# Procurement Optimizer

A full-stack vendor-negotiation and procurement-optimization platform that turns
inconsistent supplier quotes into transparent landed-cost comparisons, configurable vendor
scores, optimized order allocations, and grounded negotiation briefs — with a complete audit
trail behind every number.

**Synthetic demonstration data only.** No real supplier, price, or BOM data appears
anywhere in this repository.

---

## The problem this solves

The cheapest unit price is routinely not the cheapest purchase. An offshore supplier quoting
CNY 105.00 per bearing housing — the lowest normalized unit price of every bidder — can still
cost the most per delivered unit once you add ocean freight, insurance, tariffs, a tooling
charge, quality-risk exposure, and the cash-flow difference between Net 30 and Net 60.
Procurement teams routinely make six-figure decisions in a spreadsheet that models none of
that, and cannot reconstruct six months later why a supplier was chosen.

This platform makes that comparison explicit and reproducible: it computes an **exact
decimal** landed cost per quote line with every component, formula, assumption, and missing
input shown; scores suppliers against weights you control; and hands the result to a CP-SAT
solver that reports honestly whether the allocation it found is *proven optimal*, merely
*feasible*, or *infeasible* — and, when infeasible, which constraints conflict and what
minimal relaxation would fix it. The bundled demo dataset **engineers the price inversion on
purpose**: Shenzhen Precision has the lowest raw unit price on the bearing-housing line and
the highest landed effective unit cost of the four bidders, and a test asserts it.

## Screenshots

![Comparison workspace: supplier scores, landed-cost breakdown, and explainability drawer](docs/images/comparison-workspace.png)

![Sign in: brand panel and one-click demo access](docs/images/login.png)

![Overview: hue-coded workspace summary](docs/images/overview.png)

![Extraction review: confidence badges, the Simulated chip, and the prompt-injection banner](docs/images/extraction-review.png)

![Allocation panel: honest solver status, split table, and infeasibility explainer](docs/images/allocation-panel.png)

![Negotiation brief drawer: per-section provenance badges and the never-sends-emails callout](docs/images/negotiation-brief.png)

![Reports workspace: generate a CSV/XLSX/PDF export and download it](docs/images/reports.png)

![Audit log: filterable, cursor-paged event table with before/after diffs](docs/images/audit-log.png)

## What it does, end to end

**1 · Organization, suppliers, parts, BOMs.** Multi-tenant from the first migration. Six
suppliers with currencies, payment terms, lead times, capacity, MOQs, and performance
history; a parts catalogue with alternates and CSV/XLSX import (validated, previewed,
transactional, audited); bills of materials with a copy-on-write version chain.

**2 · RFQs.** Create an RFQ by exploding a BOM or by adding lines directly. Invite
suppliers, exclude them with a reason, reinstate them. Status transitions are validated and
preserved as history.

**3 · Quotes — manual or extracted from documents.** Upload a PDF, scanned image, CSV, or
XLSX. Every upload is treated as hostile: type decided by **magic bytes**, filenames
sanitized to display-only metadata, storage keys server-generated and org-namespaced, and
size, row/column, and decompression-bomb bounds enforced. Document text is scanned by an
injection canary that *flags without blocking* (zero-width-evasion-normalized), and the
**per-request nonce fence** is the mandatory envelope any future AI provider must receive
document text through — the shipped mock answers from committed fixtures and never sees a
prompt at all. Extracted fields carry per-field confidence bands (≥ 0.95 high, ≥ 0.60 medium,
below 0.60 low), and **materialization is blocked while any low-confidence field is
unconfirmed**. Mock-provider results are labelled "Simulated" wherever they surface.

**4 · Landed cost — exact decimal, fully explained.** Seven components (extended material,
allocated fixed, logistics, import, quality risk, delay risk, and the one **signed**
component, financing). Every value is a `Decimal` at 34-digit precision with banker's
rounding, quantized once per component so the displayed parts sum **exactly** to the
displayed total. A missing input never silently becomes zero: it becomes a recorded
`MissingInput` with a consequence sentence, and the result's completeness degrades. Money
crosses the wire as a string and is never parsed into a JavaScript `number`.

**5 · Configurable scoring.** Min–max normalization across the cohort, explicit
higher/lower-is-better direction per criterion, ties scoring 1.0 for everyone, per-supplier
weight **renormalization** when a criterion is missing (never imputation), zero-weight
criteria still displayed, outliers deliberately **not** clipped, and a human-readable reason
string on every criterion score. Reproducible without an LLM, bit for bit. The demonstration
weights are labelled as sample assumptions in both the code and the database.

**6 · CP-SAT order allocation, honestly reported.** Demand, capacity, MOQ, price-break
tiers, maximum supplier count, cost-basis concentration cap, budget limit, locked
allocations, and exclusions. Determinism is engineered: single search worker, fixed seed,
deterministic time budget, canonically sorted inputs, and a stored `model_hash`. The
reported cost is an **exact `Decimal` recomputation** of the chosen allocation — never the
solver's scaled objective — guarded by a `ConsistencyError` if the solver's tier choice and
the exact price-break re-selection ever disagree.

**7 · Negotiation briefs, grounded in stored facts.** Every figure comes from a persisted
landed-cost result, scoring output, quote line, or performance record. Price target, stretch
target, and walk-away threshold are computed with their formulas disclosed. Each section
carries a provenance badge — supplier-provided, user assumption, calculated, AI narrative, or
missing — and a numeric cross-check re-parses every number in the generated prose and refuses
any figure that is not already a stored fact. **The system never sends email**, and a test
asserts no route containing "send" or "email" exists.

**8 · Reports.** Supplier comparison, CFO recommendation, negotiation brief, scenario
summary, and audit history, rendered to CSV, XLSX, or PDF from a single renderer-agnostic
document model. Every figure is read from stored rows — nothing is recomputed differently
from what the API already returned. CSV and XLSX cells pass through a shared
formula-injection escape.

**9 · Complete audit trail.** 59 event types on an append-only table protected by database
triggers that reject `UPDATE`, `DELETE`, and `TRUNCATE`. Browsable with keyset (cursor)
pagination on `(occurred_at, id)`, filterable by event type, entity, actor, and time range,
with before/after state diffs.

## Quickstart

Two supported paths. Both end with a seeded demo organization you can sign into.

### Path A — Docker Compose (standard)

`docker-compose.yml` provides PostgreSQL 16 and MinIO; the application processes run on the
host.

```bash
git clone <repo> && cd <repo>
cp .env.example .env                 # never commit .env

docker compose up -d                 # postgres :5432, minio :9000/:9001

cd backend
uv sync
export PO_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/procurement"
uv run alembic upgrade head
uv run python scripts/seed_demo.py
uv run uvicorn app.main:create_app --factory --reload --port 8000
```

```bash
# second terminal
cd frontend
npm ci
npm run dev                          # http://localhost:5173
```

MinIO is only needed if you set `PO_STORAGE_PROVIDER=s3`; the default filesystem provider
needs nothing.

### Path B — no Docker (the path this project was built on)

The development machine had neither Docker nor Homebrew, so this path is fully supported:
`pgserver` boots a real user-space PostgreSQL, and Node lives under `~/.local/node22`.

```bash
export PATH="$HOME/Library/Python/3.9/bin:$PATH"     # uv on PATH

cd backend
uv sync
uv run python scripts/dev_db.py        # boots PostgreSQL + migrates to head,
                                       # prints the PO_DATABASE_URL to export
                                       # (`--stop` stops it; re-running is idempotent)
export PO_DATABASE_URL="<the URL it printed>"

uv run python scripts/seed_demo.py     # idempotent synthetic dataset
uv run uvicorn app.main:create_app --factory --reload --port 8001
```

```bash
# second terminal
export PATH="$HOME/.local/node22/bin:$PATH"
cd frontend
npm ci
BACKEND_PORT=8001 npm run dev          # Vite proxies /api → localhost:8001
```

`backend/scripts/dev_db.py` keeps its data directory at
`~/.local/share/procurement-optimizer/pgdata`, deliberately outside the repository —
`pgserver` passes the socket path unquoted to `pg_ctl`, so a repository path containing
spaces would break it.

### Demo credentials

Seeded by `backend/src/app/seed/demo_dataset.py` into
**Meridian Fabrication Works (Demo)**. Intentionally public, and synthetic:

| Role | Email | Password |
|---|---|---|
| Owner | `demo-owner@meridianfab.example` | `demo-owner-2026` |
| Analyst | `demo-analyst@meridianfab.example` | `demo-analyst-2026` |
| Viewer | `demo-viewer@meridianfab.example` | `demo-viewer-2026` |

The sign-in page lists all three with one-click access; use the analyst for the full
workflow, the viewer to see the read-only role enforced server-side.

### The demo dataset

6 suppliers across 5 currencies (USD, EUR, CNY, MXN, SEK) with lead times spanning 7–45
days · 19 parts in 6 categories · 2 BOMs including a two-version chain · 3 RFQs · 4 manual
quotes · 4 synthetic quote documents (PDF, scanned PNG, CSV, XLSX) with committed golden
extractions · 3 scenarios: a feasible baseline, a deliberately infeasible budget ceiling, and
a capacity-forced split. It also contains, by design, the price inversion, an uncertain part
match, an extraction correction, a missing commercial term, and a quote containing a
prompt-injection string.

## Tests

```bash
# backend
export PATH="$HOME/Library/Python/3.9/bin:$PATH"
cd backend && uv run pytest                 # 967 passed

# frontend
export PATH="$HOME/.local/node22/bin:$PATH"
cd frontend && npm test                     # 86 passed in 17 files
```

**967 backend tests, all passing** — 472 unit (pure domain: money, landed cost, price
breaks, scoring, solver, matching, normalization, file validation, storage), 490 integration
against a **real PostgreSQL** (no SQLite anywhere), and 5 contract tests that fail if any
route's declared permissions drift from what its dependencies actually enforce. **86 frontend
tests** (Vitest + Testing Library) and a **25-test Playwright E2E suite** (full journey on
Chromium, smoke on Firefox and WebKit, a mobile-viewport navigation spec, and an axe-core
accessibility sweep of every page at WCAG 2a/2aa with zero violations), all passing.

CI (`.github/workflows/ci.yml`) additionally runs `ruff`, `mypy --strict` over the whole
package, an `upgrade → downgrade base → upgrade` migration cycle on an empty database, the
frontend typecheck and production build, and a gitleaks secrets scan.

## Architecture at a glance

```
React 18 + TypeScript SPA (Vite)
        │  same-origin /api proxy
FastAPI  ─ middleware: CORS → RequestId → SecurityHeaders → OriginCheck → RateLimit
        ├─ routers (sync def, thin)          backend/src/app/api/v1/
        ├─ services (never commit)           backend/src/app/services/
        ├─ domain (pure: no I/O, no clock)   backend/src/app/domain/
        ├─ providers (Protocols)             storage · extraction · narrative · FX
        └─ org-scoped repositories → SQLAlchemy 2 → PostgreSQL 16
```

FastAPI · SQLAlchemy 2 · Alembic · Pydantic v2 · PostgreSQL 16 · OR-Tools CP-SAT ·
ReportLab · openpyxl · pypdf/pdfplumber/pypdfium2 · rapidfuzz · argon2 ·
React 18 · TanStack Query & Table · react-hook-form + zod · Vitest · pytest + hypothesis.

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Security highlights

Organization isolation is defended at **four independent layers**: the client never chooses
the organization (it comes from the session row); every repository is generically bound to
one organization so mypy verifies the filter statically; composite `(organization_id, id)`
foreign keys make a cross-org reference refusable by PostgreSQL itself; and a contract test
fails CI if any route lacks a permission declaration. Cross-organization access returns
**404, never 403** — existence must not leak. Authentication uses argon2id, stores only
`sha256(session_token)`, verifies a dummy hash on absent or locked accounts to close the
timing oracle, and writes lockout counters in their own transaction so a rolled-back request
cannot erase them. Every mutation — including login — is checked against an Origin allowlist
*and* a per-session CSRF token. Uploaded documents are sniffed by magic bytes, stored under
server-generated keys, and never exposed by URL. Document text reaches an AI provider only
inside a nonce fence, with an injection canary that flags and audits without dropping the
supplier's data. No secrets are committed, and gitleaks enforces it.

Full threat model, including the gaps, in [docs/SECURITY.md](docs/SECURITY.md).

## Limitations

Stated plainly, because a portfolio project that hides its edges is not worth reading.

- **No job queue.** `Settings.job_runner` and a `jobs` table exist, but nothing reads or
  writes them: extraction, matching, scenario solving, brief and report generation all run
  **inline inside the HTTP request**. A long CP-SAT solve holds a request open.
- **The narrative provider is a deterministic template.** `AiNarrativeProvider` is a real
  seam and `PO_NARRATIVE_PROVIDER=anthropic` is a valid setting, but **no Anthropic adapter
  ships** — selecting it raises `ProviderUnavailableError` rather than silently substituting
  the template. The same is true of the extraction provider: the mock returns committed
  golden fixtures and is always labelled `simulated`.
- **No `PATCH` on negotiation briefs.** A brief can be generated, read, reviewed, and
  archived, but not edited: `sections` has no per-field edit-history column to hang an
  audited diff off, so it was left as a documented gap rather than half-built.
- **Epsilon tie-break deferred.** Determinism is real (single worker, fixed seed, canonical
  ordering, stored `model_hash`), so repeat and permuted-input solves reproduce exactly. What
  is *not* guaranteed is which of several **exactly**-tied optima a future code change would
  surface.
- **`COMPLETE` completeness is reachable, but rarely reached in practice.** `documentation_cost`
  and `handling_cost` (migration 0016, 2026-08 product-audit remediation) are now real,
  optional columns on `quote_lines`, so a landed-cost result **can** be `COMPLETE` — but only
  when every commercial field on the line is populated and every risk/financing assumption is
  supplied, which most real quotes and scenarios won't have. Most persisted results are still
  `INCOMPLETE` or `ASSUMPTION_DEPENDENT` in practice. Full history in
  [docs/METHODOLOGY.md](docs/METHODOLOGY.md) §7 — including why the solver's eligibility gate
  is `INCOMPLETE` rather than "not `COMPLETE`".
- **Audit actor UUIDs are unresolved in the UI.** `actor_user_id` is shown as a truncated
  id (full value on hover); nothing joins it to `users.full_name` yet.
- **No report purge job**, no `docs/openapi.json` (so no OpenAPI-drift CI job), in-memory
  single-node rate limiting, and no segregation of duties between the person who uploads a
  document and the person who confirms its low-confidence fields.

Everything above, with what exists today and what remains, is tracked in
[docs/ROADMAP.md](docs/ROADMAP.md).

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, isolation stack, middleware, providers, the inline-jobs decision, SPA structure |
| [DATABASE.md](docs/DATABASE.md) | Schema conventions, composite org FKs, numeric scales, migration inventory `0001`–`0016` |
| [DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | All 40 tables, column by column, plus every enum |
| [METHODOLOGY.md](docs/METHODOLOGY.md) | Decimal policy, landed-cost formulas, the hand-verified worked example, FX/units, scoring |
| [DOCUMENT_PIPELINE.md](docs/DOCUMENT_PIPELINE.md) | Upload security, acquisition, the injection trust boundary, validation ladder, matching |
| [OPTIMIZATION.md](docs/OPTIMIZATION.md) | CP-SAT model, determinism recipe, honest statuses, infeasibility cores, scenario reruns |
| [SECURITY.md](docs/SECURITY.md) | Threat model, auth, CSRF, isolation, prompt injection, secrets policy, known gaps |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Both run paths, every `PO_*` setting, common operations, CI |
| [ROADMAP.md](docs/ROADMAP.md) | Deferred work, honestly scoped |
| [RELEASE_NOTES_v0.1.0.md](docs/RELEASE_NOTES_v0.1.0.md) | What shipped in v0.1.0 |
| [SPEC.md](docs/SPEC.md) | The original project specification |

Contribution workflow, file-ownership rules, and the append-only migration policy are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Honest positioning

This is a portfolio demonstration built against a synthetic dataset. It has no real
customers, no production deployment, and no proprietary supplier data. It does not claim
validated savings or predictive accuracy, and it never labels an allocation "optimal"
without the solver's own proof.

## License

MIT — see [LICENSE](LICENSE).
