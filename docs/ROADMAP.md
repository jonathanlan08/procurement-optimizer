# Roadmap

Everything on this page is a real, known gap in v0.1.0 — deliberately deferred, not
overlooked. Each entry says what exists today so the size of the remaining work is honest.

Related: [SECURITY.md](SECURITY.md) §13 · [METHODOLOGY.md](METHODOLOGY.md) §10 ·
[RELEASE_NOTES_v0.1.0.md](RELEASE_NOTES_v0.1.0.md)

---

## AI providers

**Anthropic extraction adapter.** The `ExtractionProvider` Protocol, the versioned payload
schema, the nonce-fenced envelope, the canary detector, and the whole validation ladder all
exist and are exercised by the mock provider
(`backend/src/app/providers/extraction/{base,envelope,mock}.py`). `PO_EXTRACTION_PROVIDER`
already accepts `anthropic`, and selecting it raises `ProviderUnavailableError` rather than
silently substituting the mock. What is missing is the adapter itself — and it must build its
prompt **only** through `envelope.build_document_envelope`.

**Anthropic narrative adapter.** Same shape:
`backend/src/app/providers/narrative/base.py` defines `AiNarrativeProvider` and the exact
section keys; `template.py` ships as the deterministic default with `is_generated = False`
(so `negotiation_briefs.simulated` is `true`). `BriefService._numeric_cross_check` already
polices invented figures, so the guard rails a real model needs are in place.

## Platform

**Job queue.** `Settings.job_runner` and the `jobs` table exist but nothing reads or writes
them; every operation runs inline inside the request
([ARCHITECTURE.md](ARCHITECTURE.md) §7). A real worker would introduce the `202 Accepted` +
job-polling response shape that `docs/planning/03-api-contract.md` §1.5 already specifies,
without changing service method signatures. Until then, a long CP-SAT solve holds an HTTP
request open.

**Distributed rate limiting.** The limiter is an in-memory sliding window per process
(`backend/src/app/api/middleware.py`); multi-node deployments need a shared store.

**Session purge job.** Expired and revoked sessions are ignored at resolve time but never
swept from the table.

**Report purge scheduler.** `generated_reports` carries `expires_at` and `purged_at`, the
schema nulls `storage_key`/`content_sha256` together, and `GET /reports/{id}/content`
already returns `410` for a purged report — but **nothing purges anything**. The retention
window itself (`REPORT_RETENTION_DAYS = 90` in
`backend/src/app/services/report_service.py`) is a documented assumption: neither the SPEC
nor the ERD states a duration.

## Features

**Audit actor-name resolution.** `audit_events.actor_user_id` is stored and displayed as a
raw UUID; `frontend/src/features/audit/AuditPage.tsx` renders `actor_user_id` directly
(falling back to "— system"). Joining to `users.full_name` for display is a small,
deliberate follow-up.

**PDF font coverage.** `backend/src/app/reports/pdf_renderer.py` uses ReportLab's built-in
Helvetica only — no font files, no network fetch. Helvetica is effectively Latin-1, and a
character outside it raises inside ReportLab (surfacing as a `failed` report row rather than
mangled output). This is a **deviation from `docs/planning/00-decisions.md` §4 #24**, which
ruled that DejaVu Sans should be bundled; the shipped code chose the zero-dependency route
instead. Bundling DejaVu Sans, and then CJK coverage, remain open.

**`PATCH /negotiation-briefs/{id}`.** `docs/planning/03-api-contract.md` §4.17 lists an edit
route for human edits to any section, audited. It is not implemented: `sections` has no
per-field edit-history column to hang an audited diff off of, so doing it properly is more
than "add a route". Today a brief can be generated, read, reviewed
(`draft → human_reviewed`), and archived — but not edited.
`BriefState.APPROVED` exists in the enum and is unreachable through v0.1 routes.

**Un-archive coverage.** `POST …/unarchive` exists for suppliers and parts and both have a
"Restore" control in the SPA. BOMs, quotes, documents, scenarios, briefs, and scoring
configurations can be archived but not un-archived — archive is one-way for those resources
in v0.1.

**Landed-cost inputs with no home.** `documentation_cost` and `handling_cost` have no
columns on `quote_lines`, which is why `Completeness.COMPLETE` is structurally unreachable
([METHODOLOGY.md](METHODOLOGY.md) §7). `rfq_lines` has no `required_by_date`, which is why
required lead time arrives as a scenario assumption and why the lead-time pre-solve
eligibility filter of `docs/planning/00-decisions.md` §4 #12 is not implemented.

## Calculation and optimization

**Epsilon tie-break for exactly-tied optima.** Determinism is real today
(`num_search_workers=1`, `random_seed=0`, canonical ordering, stored `model_hash`), so
repeat and permuted-input solves reproduce exactly. What is *not* guaranteed is which of
several **exactly**-tied optima a future code change would surface. The methodology's
epsilon tie-break term (fewest suppliers, then lexicographic) is deferred and flagged in
`backend/src/app/domain/optimization/solver.py`'s module docstring.

**Incremental (marginal-unit) price breaks.** v0.1 implements all-units discounts only
(`docs/planning/00-decisions.md` §2 ruling 1).

**Scrap and yield adjustments.** Deferred to v0.2 by ruling
(`docs/planning/00-decisions.md` §4).

**Shared cross-line supplier capacity.** v0.1 constrains capacity per quote line; a supplier
whose plant capacity is shared across several RFQ lines is not modelled.

**Non-linear freight.** Freight is linear in quantity; break-bulk and container-step curves
are not modelled (accepted limitation).

**Rejected-alternatives sweep.** The solver reports one alternative (the best
single-supplier allocation). The methodology's full every-supplier and next-best-split sweep
is not implemented.

## Contract integrity and CI

**Committed `docs/openapi.json` + generation + drift job.** Ratified in
`docs/planning/00-decisions.md` §1.4 but not delivered: the file is not committed,
`npm run generate:api` therefore has no input, and CI has no drift job. Frontend API types
are hand-written today.

**`pip-audit` and a licence gate.** The licence rulings exist (`pypdfium2` over PyMuPDF to
avoid AGPL; `rapidfuzz` MIT; fonts self-hosted rather than CDN) but nothing enforces them in
CI. Planned as a job that blocks AGPL/GPL dependencies.

**End-to-end (Playwright) suite.** `.gitignore` reserves the Playwright output paths and the
SPEC specifies the full E2E journey, but no Playwright suite is committed. Coverage today is
the backend pytest suite (unit, integration against real PostgreSQL, and contract) plus the
frontend Vitest component suite.

## Accessibility and UX

**Deeper accessibility pass.** The design system specifies focus rings, non-colour-only
status encoding (glyphs paired with colour), and dense-table semantics, and the shell uses
landmark roles and `aria-label`ed navigation — but no systematic audit (axe sweep, keyboard
traversal of every drawer and dialog, screen-reader pass over the comparison tables) has been
performed.

## Lifecycle and administration

Accepted v0.1 limitations, all from `docs/planning/00-decisions.md` §4 #18–#23:

- no data retention or purge policy, and no table partitioning;
- no password reset flow (a CLI/DB operation) and no email invitation flow — an administrator
  adds members directly;
- demo reset is "re-run the idempotent seed script";
- no uploader/confirmer segregation of duties;
- PDF output bundles DejaVu Sans; CJK glyph coverage is deferred.
