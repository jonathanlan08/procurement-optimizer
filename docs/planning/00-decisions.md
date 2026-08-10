# 00 — Principal's Decision Record (Plan Approval)

Date: 2026-08-10 · Author: principal (Fable 5) · Status: **PLAN APPROVED with the rulings below**

The planning package (`01`–`09`) produced by the architecture-manager subagent is approved as the
basis for implementation, subject to the corrections and rulings in this document. Where this
document conflicts with `01`–`09`, **this document wins**.

## 1. Architecture-manager recommendations — accepted

1. **CP-SAT determinism** — ACCEPTED in full: `num_search_workers=1`, `random_seed=0`,
   `max_deterministic_time`, sorted inputs, stored `model_hash`; report the **exact Decimal
   recomputation** of the chosen allocation, never the scaled solver objective; integer scale
   `10^4` for int64 headroom. Determinism controls land with the first solve (task 6.6 is not
   deferrable).
2. **Composite org FKs** — ACCEPTED: `UNIQUE (organization_id, id)` on parents; child FKs carry
   `organization_id`. Cross-org references become database-refusable. Route-matrix test asserts
   404 (not 403) for every endpoint accessed cross-org.
3. **Numeric scales** — ACCEPTED: FX `NUMERIC(24,12)`; unit prices `NUMERIC(18,8)`; monetary
   totals `NUMERIC(18,6)`. Money crosses the wire as **strings**, always.
4. **Contract integrity** — ACCEPTED: committed `docs/openapi.json` + `openapi-typescript`
   generation + CI drift job; `react-hook-form` + `zod`; **all DB-touching routes are sync `def`**
   (threadpool), never `async def` with a sync session.
5. **Auth details** — ACCEPTED: CSRF double-submit **plus** Origin/Referer allowlist;
   `SameSite=Lax`; DB stores `sha256(session_token)` only; providers extended with `Clock`,
   `IdGenerator`, `ReportRenderer`, `AiNarrativeProvider`.

**Library rulings** (licence + no-Homebrew constraints): `pypdfium2` (not PyMuPDF — AGPL) for PDF
rastering; `pypdf` + `pdfplumber` for text/tables; ReportLab for PDF generation; `rapidfuzz` (MIT)
for fuzzy matching; fonts self-hosted via `@fontsource` (no Google Fonts CDN — CSP + offline demo);
CI licence gate blocks AGPL/GPL.

## 2. Rulings on the open questions

| # | Question | Ruling |
|---|---|---|
| 1 | Price-break semantics | **All-units** discounts. Incremental breaks are roadmap. |
| 2 | Financing sign | **Signed component**, relative to an org-configurable baseline payment term (default Net-30) and annual cost-of-capital rate (default 8%, labelled user assumption). Longer terms may produce a negative (benefit) component. Blessed explicitly. |
| 3 | Duty basis / tax | Duty basis defaults to **CIF-like (material + logistics)**, stored as an explicit labelled assumption. **Recoverable tax excluded** from landed cost by default. |
| 4 | Concentration cap basis | **Cost basis** (share of total awarded spend). Documented in scenario UI. |
| 5 | Missing criterion / viewer rights | **Renormalize weights** over present criteria (missing shown explicitly, never imputed). `viewer` may read and download documents/reports; may never confirm, correct, calculate, generate, or mutate. |
| 6 | Phase construction | **Adopted** (P1 foundation → P2 master data → P3 RFQ + manual quotes → P4 documents → P5 calculation → P6 optimization → P7 release), including manual-quotes-before-extraction. |

## 3. Worked-example correction (hand-verified)

`05-calculation-methodology.md` §9 financing line was arithmetically wrong (−37.520000).
Correct: `0.08 × (30−60)/365 × 5706.521740 = −37.522335`; total `7240.548318`; effective unit
`14.48109664`. Corrected in place with a note. The first hand-verified test asserts the corrected
values.

## 4. Rulings on flagged edge cases (09 §11)

Confirmed as proposed: scrap/yield → v0.2 roadmap (#2); min-order charges → `other_fixed_cost` +
note (#3); linear freight accepted limitation (#4); expired quotes usable with prominent warning
but blocked from "recommended" status (#8); one currency per quote (#9, #10 out of scope);
re-extraction suggests, never reapplies, corrections (#15); manual supersede for revisions (#16);
optimistic locking via `If-Match` (#17); tie-break fewest-suppliers-then-lexicographic (#13).

Additional rulings:
- **#11 capacity**: per-quote-line capacity constrains allocation in v0.1; shared cross-line
  supplier capacity is roadmap.
- **#12 lead time**: quotes whose lead time misses the required-by date are excluded by the
  pre-solve eligibility filter with a reason string; user may override per supplier (logged).
- **#14 confirmation roles**: `analyst`+ confirm; no uploader/confirmer segregation in v0.1
  (single-analyst demo) — noted in SECURITY.md as a production gap.
- **#18–#23 lifecycle**: no retention/purge, no partitioning, no password reset (CLI script only),
  no email invitation flow (admin adds members directly), demo reset via seed script re-run —
  all v0.1 accepted limitations, documented in ROADMAP.md.
- **#24 PDF fonts**: bundle DejaVu Sans in ReportLab output; CJK coverage is roadmap.
- **#25 dates**: `due_date`/`required_by_date` are calendar dates with documented
  "organization-local business date" semantics.
- **#26 fonts**: self-hosted `@fontsource` (design-system MASTER.md updated).
- **#27 confidence bands**: single source of truth `app/domain/confidence.py`, thresholds
  **0.95 / 0.60** (MASTER.md updated to match).

## 5. Scope-control ruling (risk #1)

The full spec ships in v0.1.0 as planned, but if schedule pressure materializes the pre-authorized
descope order is: (a) OCR ships mock-only with the scanned-image fixture exercising the mock path
(spec-compliant: provider abstraction + fixtures), (b) PDF reports reduce to two templates
(comparison + CFO recommendation), CSV/XLSX carry the rest, (c) frontend surfaces for audit
browsing reduce to a filtered table. Nothing else is descopeable. Organization isolation,
calculation correctness, solver honesty, and document security are never descoped.

## 6. File ownership

The map in `09-task-decomposition.md` §10 is **ratified**. In this build, "principal-owned" means
written by me directly; "delegable" tasks go to sonnet-tier subagents with the task template from
the operating model; **R**-marked paths require my diff review before commit.

## 7. Phase 1 independent-review outcome (2026-08-10)

Verdict: APPROVE-WITH-FIXES. All findings triaged; resolution status:

- **#1 lockout rollback (HIGH)** — FIXED: failure counters write in their own
  transaction (`AuthService._record_failed_login`, `SELECT ... FOR UPDATE`);
  regression-tested against rollback.
- **#2 timing oracle (HIGH)** — FIXED: dummy-hash verification on absent/locked
  accounts; locked accounts return the generic failure message.
- **#3 isolation unwired (HIGH)** — FIXED: `OrgOwnedBase` abstract base (typed
  pk + organization_id) adopted by AuditEvent/Job; repository base gains
  post-fetch check, `get_or_raise`, `list_page`; permission matrix + coverage
  test land (tasks 1.12/1.13); composite `UNIQUE (organization_id, id)` will be
  created by each Phase 2+ business-table migration (0001's tables have no
  business children, so retrofitting was declined).
- **#4 CSRF after refresh (MEDIUM)** — FIXED: `/me` returns `csrf_token`;
  browser-verified (login → hard reload → mutation succeeds).
- **#5 auth audit (MEDIUM)** — PARTIAL by ruling: `auth.login_succeeded` and
  `auth.logout` are audit events; failed logins go to the structured security
  log because the org-scoped audit table cannot represent org-less failures
  (documented deviation for SECURITY.md).
- **#6 dead logging (MEDIUM)** — FIXED: `configure_logging` wired in
  `create_app`; security log used for failed logins.
- **#7 idle expiry (MEDIUM)** — FIXED: 2h idle timeout in `resolve`. Session
  purge job remains roadmap.
- **#8 CI gaps (MEDIUM)** — PARTIAL: mypy --strict now clean (26 files) and in
  CI. pip-audit / licence gate / OpenAPI drift jobs are scheduled for their
  phases (drift job lands with the first generated client in Phase 2).
- **#9 money (LOW)** — no defect found by review; `places=0` covered by tests.
- **#10 misc (LOW)** — FIXED: InvalidHashError caught; docs gated off in prod
  (unless demo_mode); TRUNCATE trigger added to 0001 (edited pre-release by
  principal ruling — append-only applies from first release); rate-limiter key
  eviction bound. REVOKE-based audit grants deferred: dev runs as table owner;
  documented for production in SECURITY.md.

Migration 0001 was modified pre-release (TRUNCATE trigger). The dev database
was rebuilt from scratch to verify; append-only discipline for migrations
begins at v0.1.0.
