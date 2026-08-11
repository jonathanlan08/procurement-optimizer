# Release notes — v0.1.0

First public release of the Procurement Optimizer: a vendor-negotiation and
procurement-optimization platform that converts inconsistent supplier quotes into
transparent landed-cost comparisons, configurable vendor scores, optimized order
allocations, and grounded negotiation briefs, with a complete audit trail.

**All data in this repository is synthetic.** No real supplier, price, document, or BOM
data is included anywhere.

---

## Highlights

**Exact-decimal landed cost.** Seven components — extended material, allocated fixed,
logistics, import, quality risk, delay risk, and the one signed component, financing —
computed at 34-digit precision with banker's rounding and quantized once per component, so
displayed components sum exactly to the displayed total. Every component ships its formula
with values substituted, its inputs, its provenance, and whether it was assumed or missing.
The methodology's worked example (`total 7240.548318`, `effective unit 14.48109664`,
`financing −37.522335`) is asserted both as a pure unit test and end to end through the HTTP
stack and the database.

**Missing is never zero.** Every quantity carries a `Provenance`, with the invariant
`value is None ⟺ MISSING` enforced at construction. A missing input produces a recorded
`MissingInput` with a human consequence sentence and degrades the result's completeness — it
never silently becomes `0`.

**Deterministic, honest CP-SAT allocation.** Single search worker, fixed seed, deterministic
time budget, canonically sorted inputs, and a stored `model_hash`. `FEASIBLE` is never
presented as `OPTIMAL`. The reported cost is an exact `Decimal` recomputation of the chosen
allocation — never the solver's scaled objective — guarded by a `ConsistencyError` if the
solver's tier and the exact price-break re-selection ever disagree. Infeasible scenarios
return minimized assumption cores and, where one exists, a concrete minimal relaxation
("a budget of at least X restores feasibility").

**Document pipeline with a real trust boundary.** Magic-byte type sniffing, sanitized
filenames, server-generated org-namespaced storage keys, acquisition caps, per-request
nonce-fenced prompts, and an injection canary that flags and audits without dropping the
supplier's data. Four byte-deterministic fixtures (PDF, scanned PNG, CSV, XLSX) with
sha256-keyed golden extractions, including one that proves an embedded injection string is
inert: the real unit price is extracted, not the attacker's.

**Human-in-the-loop extraction.** Confidence bands at 0.95 / 0.60, a five-step validation
ladder, per-field confirm and correct with audited before/after, and materialization blocked
while any low-confidence field is unconfirmed. Mock-provider output is labelled
`simulated` everywhere it surfaces.

**Transparent scoring.** Min–max normalization, explicit direction per criterion, ties
scoring 1.0, per-supplier weight renormalization on missing data (never imputation),
zero-weight criteria still shown, outliers deliberately unclipped, a reason string on every
criterion score, and full reproducibility without an LLM.

**Grounded negotiation briefs.** Five-label section provenance, computed target / stretch /
walk-away with disclosed formulas, and a numeric cross-check that refuses any figure in the
generated prose that is not already a stored fact. There is no send route, and a test
asserts no path containing "send" or "email" exists.

**Reports and audit.** Five report types across CSV, XLSX, and PDF, rendered from one
renderer-agnostic document model and sourced entirely from stored rows; shared
formula-injection escaping on every CSV/XLSX cell. A browsable, filterable audit log with
keyset pagination over an append-only table protected by database triggers.

**Organization isolation in four independent layers**, with cross-org access returning 404
rather than 403, and a contract test that fails CI if any route lacks a permission
declaration.

## Scope

- Database: **PostgreSQL 16**, migrations **`0001` → `0015`**, **40 tables**.
- API: `/api/v1` — auth, suppliers (+ contacts, performance), parts (+ alternatives,
  imports), BOMs, RFQs, quotes, quote documents, extraction runs, part matching, exchange
  rates, landed costs, scoring configurations, comparison scenarios, negotiation briefs,
  reports, audit events.
- Frontend: React 18 + TypeScript SPA with workspaces for suppliers, parts, BOMs, RFQs
  (quotes, documents, extraction review), comparison (scoring, allocation, scenario history,
  briefs), FX rates, reports, and the audit log.
- Providers shipped: filesystem and S3 storage, mock extraction, template narrative,
  synthetic FX. The `anthropic` extraction and narrative options are interfaces only.

## Tests

| Suite | Count | Notes |
|---|---|---|
| Backend unit | 415 | Pure domain — money, landed cost, price breaks, scoring, CP-SAT solver, matching, FX/unit normalization, file validation, storage |
| Backend integration | 479 | Against a **real PostgreSQL** (no SQLite anywhere) |
| Backend contract | 2 | Every route must carry a permission declaration; no stale declarations |
| **Backend total** | **896 passing** | `cd backend && uv run pytest` |
| Frontend | **63 passing** | Vitest + Testing Library, 13 files |

CI additionally runs `ruff`, `mypy --strict`, an `upgrade → downgrade base → upgrade`
migration cycle on an empty database, the frontend production build, and a gitleaks secrets
scan.

## Known limitations

Carried over from [ROADMAP.md](ROADMAP.md) and the README, stated plainly:

- **No job queue.** Extraction, matching, scenario solving, and brief/report generation all
  run inline inside the HTTP request; `Settings.job_runner` and the `jobs` table exist but
  are read by nothing. A long solve holds a request open.
- **Template narrative provider only.** `AiNarrativeProvider` and `ExtractionProvider` are
  real seams and `anthropic` is a valid setting, but no adapter ships — selecting it raises
  `ProviderUnavailableError` rather than silently substituting the deterministic default.
- **No `PATCH` on negotiation briefs.** Generate, read, review, and archive only;
  `sections` has no per-field edit history to hang an audited diff off.
- **Epsilon tie-break deferred.** Determinism holds for a fixed model; which of several
  exactly-tied optima surfaces is not pinned.
- **`COMPLETE` completeness is structurally unreachable** — `documentation` and `handling`
  costs have no source columns, so results are `INCOMPLETE` or `ASSUMPTION_DEPENDENT`. See
  [METHODOLOGY.md](METHODOLOGY.md) §7.
- **Audit actor UUIDs are unresolved in the UI.**
- **PDF reports use ReportLab's built-in Helvetica**, not the bundled DejaVu Sans of
  `docs/planning/00-decisions.md` §4 #24 — a documented deviation; non-Latin-1 characters
  raise and surface as a `failed` report row.
- **No report purge job**, no committed `docs/openapi.json` (and therefore no OpenAPI-drift
  CI job), no `pip-audit` or licence gate, no Playwright E2E suite.
- **Single-node rate limiting** (in-memory), **no segregation of duties** between uploader
  and confirmer, no password-reset or invitation flow, and no session purge job.
- **No lead-time pre-solve eligibility filter** — `rfq_lines` has no `required_by_date`
  column; lead time influences outcomes through the scoring criterion instead.

## Synthetic data notice

Every supplier, part, price, quote, document, and performance record in this repository is
invented. Contact addresses use the reserved `.example` TLD. The demo credentials
(`demo-owner@meridianfab.example` / `demo-owner-2026`, plus analyst and viewer equivalents)
are intentionally public and grant access only to synthetic data. Exchange rates come from a
fixed offline table labelled `synthetic_fixture` and are not sourced from any market feed.

The demo dataset deliberately contains the cases the specification requires a serious tool to
handle: a supplier with the lowest unit price but not the lowest landed cost, an infeasible
allocation scenario, a split-order scenario, an uncertain part match, an extraction
correction, a missing commercial term, and a quote containing a prompt-injection test string.

## Getting started

See the [README](../README.md) for both quickstart paths (Docker Compose, and the no-Docker
`uv` + `pgserver` path), and [DEPLOYMENT.md](DEPLOYMENT.md) for the full environment-variable
reference.

## License

MIT.
