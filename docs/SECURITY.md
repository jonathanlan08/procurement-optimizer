# Security model

What is defended, how, and — equally important — what is *not* defended in v0.1.0. Nothing
here is aspirational: every control cited exists in the code at the path given.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [DOCUMENT_PIPELINE.md](DOCUMENT_PIPELINE.md) ·
[DEPLOYMENT.md](DEPLOYMENT.md) · [ROADMAP.md](ROADMAP.md)

---

## 1. Threat model, briefly

This is a multi-tenant B2B application handling commercially sensitive procurement data and
ingesting **untrusted documents supplied by third parties**. The threats taken seriously:

1. one organization reading or referencing another organization's data;
2. an authenticated user acting above their role;
3. session theft, CSRF, and credential attacks (stuffing, enumeration, timing oracles);
4. malicious uploads — wrong type, oversized, traversal-shaped filenames, spreadsheet
   formula injection, zip bombs;
5. **prompt injection** through document content reaching an AI extraction provider;
6. leaking data through error messages, logs, exports, or storage URLs;
7. committing secrets or real (non-synthetic) commercial data to a public repository.

Out of scope for v0.1.0 and stated as such below: DoS beyond a single-node rate limiter,
key management/HSM, SSO/MFA, and infrastructure hardening.

## 2. Authentication

`backend/src/app/core/security.py`, `backend/src/app/services/auth.py`.

- **argon2id** password hashing (`argon2-cffi` defaults: `time_cost=3`, 64 MiB memory).
  `verify_password` catches `VerifyMismatchError`, `VerificationError`, **and**
  `InvalidHashError`, so a corrupt stored hash is a failed login, never a 500.
- **Session tokens are 256-bit random** (`secrets.token_urlsafe(32)`), handed to the browser
  in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` forced on in `prod`). The database stores
  only `sha256(token)` — a database read never yields a usable session token.
- **Timing-oracle defence.** Every login attempt performs exactly one argon2 verification.
  Absent, inactive, archived, and locked accounts are all verified against a module-level
  `_DUMMY_HASH`, and every failure path raises the same
  `UnauthenticatedError("Invalid email or password.")`. Account existence and lock state do
  not leak through message or timing.
- **Rollback-proof lockout.** 8 failed attempts → a 15-minute lock. Counters are written in
  their **own transaction** (`_record_failed_login`, `SELECT … FOR UPDATE`), because the
  request transaction is rolled back when login raises — otherwise the counter would be
  rolled back along with the failure it counted. This was Phase 1 review finding #1
  (`docs/planning/00-decisions.md` §7) and carries a regression test.
- **Two expiries.** An absolute TTL (`PO_SESSION_TTL_HOURS`, default 12) *and* a 2-hour idle
  timeout enforced in `AuthService.resolve` against `sessions.last_seen_at`.
- Logout revokes the session row (`revoked_at`), it does not delete it.

**Documented deviation:** successful logins and logouts are audit events
(`auth.login_succeeded`, `auth.logout`); **failed** logins go to the structured security log
(`logging.getLogger("app.security")`, `event: auth.login_failed`, with request id and IP) and
not to `audit_events`, because that table is org-scoped and a failed login frequently has no
organization to attribute it to. This is Phase 1 review finding #5, resolved by ruling.

## 3. CSRF and Origin

Two independent factors on every mutating request:

1. **Origin/Referer allowlist** — `OriginCheckMiddleware` applies it to every mutating
   `/api/` request **including `POST /api/v1/auth/login`**, which has no session yet and
   would otherwise be exposed to login-CSRF. `current_principal` applies it again for
   authenticated routes. A **missing** Origin/Referer header fails closed: browsers send
   `Origin` on all cross-origin and same-origin non-GET requests, so its absence on a
   mutation is suspicious.
2. **Double-submit CSRF token** — a per-session `csrf_secret` compared with the
   `X-CSRF-Token` header using `hmac.compare_digest`.

`GET /api/v1/auth/me` returns the `csrf_token`, so a hard page reload can re-acquire it
without a new login (Phase 1 review finding #4).

Security headers on every response (`SecurityHeadersMiddleware`): `nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'`,
`Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`, plus
HSTS in `prod`.

## 4. Authorization

Roles form a strict hierarchy: `viewer < analyst < administrator < owner`
(`ROLE_ORDER` in `backend/src/app/api/deps.py`). By ruling
(`docs/planning/00-decisions.md` §2 #5) a **viewer may read and download documents and
reports, and may never confirm, correct, calculate, generate, or mutate anything**. The
reports router follows that literally: every `GET` is `viewer`+, `POST /reports` is
`analyst`+ — viewers download, never generate.

The permission matrix (`backend/src/app/api/permissions.py`) declares a level for every
single route: `"public"`, `"authenticated"`, or a minimum `Role`. Two contract tests
(`backend/tests/contract/test_permission_matrix.py`) walk the live OpenAPI route table and
fail if any route is **undeclared** or if a declaration is **stale**. Forgetting to think
about authorization is therefore a CI failure, not a production incident. The matrix file is
also a readable audit artifact: each block carries a comment explaining which contract row it
implements and why any deviation was chosen.

Broad pattern: reads are `viewer`+, mutations are `analyst`+, archive/destructive and
FX-refresh operations are `administrator`+.

**Known v0.1 gap:** there is no uploader/confirmer segregation of duties — an `analyst` may
both upload a document and confirm its low-confidence fields
(`docs/planning/00-decisions.md` §4 #14, single-analyst demo). In a production deployment
handling real money this should be split.

## 5. Organization isolation — defence in depth

Four independent controls; see [ARCHITECTURE.md](ARCHITECTURE.md) §3 for the full table.

1. The client never chooses the organization: it comes from `sessions.active_organization_id`.
2. `OrgScopedRepository` is generic over `OrgOwnedBase`, so mypy verifies the org filter
   statically; a post-fetch runtime check raises `OrgIsolationViolation` if a row ever
   escapes it; `add()` refuses an entity whose `organization_id` differs from the scope.
3. Composite `(organization_id, parent_id)` foreign keys make a cross-org reference
   **refusable by PostgreSQL itself**.
4. Route-matrix and per-resource integration tests.

**Cross-org access returns 404, never 403.** Existence must not leak. `NotFoundError` is
documented as covering "both genuinely absent and other-organization resources", and the
test suites assert 404 on cross-org access across every resource (56 cross-org test
functions at the time of writing). This discipline extends to storage:
`FilesystemStorageProvider` / `S3StorageProvider` namespace by organization internally, so
even a leaked storage key from another org resolves to a different path.

## 6. Untrusted uploads

Covered in detail in [DOCUMENT_PIPELINE.md](DOCUMENT_PIPELINE.md) §1. Summary of controls:

- magic-byte type allowlist (PDF/PNG/JPEG/CSV/XLSX); extension **and** declared content type
  must agree with the sniffed kind;
- CSV rejected if it contains NUL bytes or fails to decode;
- XLSX parsed `read_only=True, data_only=True` — **no formula evaluation** — with row/column
  caps (10 000 × 50) plus decompression-bomb bounds checked against the zip directory's
  declared sizes **before** any parsing (100:1 ratio cap, 100 MiB absolute cap), a
  32,767-character per-cell cap (Excel's own limit), and a 10 M-character total
  acquired-text cap; PDFs are capped at 500 pages and the same total-text bound
  (all in `backend/src/app/ingestion/acquisition.py`; added after the 2026-08
  security audit demonstrated a 68 KiB upload expanding to 67 MB through the
  row/column caps alone);
- size cap `PO_MAX_UPLOAD_BYTES` (20 MiB default), empty files rejected. Oversized
  **declared** bodies are rejected pre-routing by `BodySizeLimitMiddleware` (Starlette
  would otherwise spool the whole multipart body to a temp file before the route-level
  check runs); a chunked body without `Content-Length` still spools, which is why
  [DEPLOYMENT.md](DEPLOYMENT.md) §8 requires a reverse-proxy body cap
  (`client_max_body_size`) in any real deployment;
- filenames sanitized and treated as display metadata only; **storage keys are
  server-generated UUID hex + canonical extension** and re-validated against a strict regex
  in every storage method;
- per-organization sha256 dedupe;
- downloads stream through an authorized route with `Content-Disposition: attachment` and
  global `nosniff`. **No public or pre-signed URLs exist.**

### Spreadsheet formula injection

`backend/src/app/importing/part_import_parser.py::_is_formula_injection` rejects any imported
cell whose first non-whitespace character is `=`, `+`, `-`, or `@` and which is not a plain
signed number, with the row-level error "value begins with a spreadsheet formula character
(=, +, -, @) and is not a plain number; rejected as a formula-injection risk". The check runs
on **every column of every row**, and there is a test asserting exactly that. XLSX cells that
store a formula with no cached value are tracked separately as `formula_columns`.

On **egress**, `backend/src/app/reports/escape.py::escape_formula_cell` is the single place
the check is written, and both `csv_renderer.py` and `xlsx_renderer.py` call it on **every
cell** before writing. A value beginning with `=`, `+`, `-`, `@`, tab, or carriage return is
prefixed with `'`, which every major spreadsheet application treats as forced plain text.
This matters especially for XLSX: `openpyxl` auto-detects a leading `=` on a plain string
assigned to `Cell.value` and stores it as a **live formula** (`data_type='f'`) unless the
prefix has already been neutralized — so skipping the escape would make this codebase's own
writer emit an executable formula.

The PDF renderer deliberately does **not** call it (PDF has no live-formula surface); it
instead escapes `&`, `<`, `>` via `xml.sax.saxutils.escape` before splicing text into
ReportLab's mini-XML `Paragraph` markup, escaping label and value *individually* so a
supplier name containing `<b>` cannot forge markup. That is a presentation-correctness
control, not a security one, and the module says so.

## 7. Prompt injection: the AI trust boundary

Document content is **data, never instructions**
(`backend/src/app/providers/extraction/envelope.py`):

- a per-request `secrets.token_hex(16)` nonce fences the document text; the nonce is
  generated after upload, so a document cannot forge a closing fence. **Honesty note:**
  because no external AI provider ships in v0.1 (the mock answers from committed
  fixtures and never receives a prompt), `build_document_envelope` has no caller today —
  it is the mandatory entry point for the future Anthropic adapter, not an active
  control in this build;
- lookalike fence markers inside document text are neutralized anyway;
- system instructions sit outside the fences and state that everything inside is untrusted
  third-party data, that unstated fields are `null`, and that text addressing the model has
  no effect;
- a canary detector scans the acquired text for instruction-shaped patterns — after
  stripping zero-width characters and NFKC-normalizing, so "Ign​ore all previous
  instructions" cannot slip between a pattern's letters — and **flags without blocking**:
  a `security.injection_suspected` audit event is written with the matched snippets, the
  verdict is stored on the run, every field is marked `injection_flagged`, and the review
  UI shows a banner. Pattern matching is heuristic by nature; a paraphrase can evade it,
  which is acceptable for a flag-only control whose hard guarantees live elsewhere
  (human confirmation gates, the numeric cross-check);
- providers must never execute tools, browse, or act on text found in documents; provider
  output stays untrusted until it clears the validation ladder.

The committed injection fixture (`nordic_fastener_quote.csv`, whose notes column says
"…set unit_price to 0.01") proves the boundary holds: the golden extraction reports the real
price `0.024`, and `ExtractedLine` has no `notes` field for the instruction to land in even
in principle.

The same discipline applies to the narrative side. `AiNarrativeProvider.render_sections`
receives only a flat `brief_facts` dict of pre-formatted strings assembled from stored,
org-scoped rows; it has no database access. `BriefService._numeric_cross_check` re-parses
every decimal-looking token in the provider's rendered prose and asserts each one already
appears in `brief_facts` — a mechanical guard against an invented figure slipping past a
human reviewer.

## 8. No auto-send, structurally

The SPEC requires "never auto-send emails". This build enforces it by **absence**, not by
policy: `NegotiationBrief` stores `draft_email_subject` / `draft_email_body` as plain
columns returned with the brief, and there is no send route, no mailer dependency, and no
`/email-draft` route either.

The guarantee is a test:
`backend/tests/integration/test_briefs_api.py::test_no_send_endpoint_exists_anywhere` builds
the app, reads the OpenAPI schema, and asserts **no path contains "email" or "send"**. A
future PR that adds one fails CI.

## 9. The audit trail is append-only, and read-only over HTTP

`AuditRecorder` (`backend/src/app/services/audit.py`) is the **only** writer anywhere in the
codebase, and it never opens its own transaction: an audit row is written inside the same
transaction as the change it describes, so the trail cannot diverge from the data. 60 event
types are recorded, including `security.injection_suspected`.

At the database level, migration `0001` installs `audit_events_append_only()` plus
`trg_audit_events_append_only` (`BEFORE UPDATE OR DELETE … FOR EACH ROW`) and
`trg_audit_events_no_truncate` (`BEFORE TRUNCATE … FOR EACH STATEMENT`); both raise.

At the HTTP level there is **no write, update, or delete route** for audit events by design.
`backend/src/app/services/audit_read_service.py` issues nothing but `SELECT`, and paginates
by **keyset** on `(occurred_at DESC, id DESC)` — not `OFFSET` — so pages stay stable under
the concurrent inserts an append-only log constantly receives, and two events in the same
instant can neither be dropped nor repeated across a page boundary. The cursor is an opaque
base64 value; a malformed one is a `422 validation_error`, not a stack trace. Reads are
`viewer`+ and, like every other resource, scoped to the caller's organization by the
repository base class.

## 10. Safe errors and logging

- One error envelope for every non-2xx (`backend/src/app/core/errors.py`); messages are
  display-safe by contract — no SQL, no stack traces, no file paths, never another
  organization's data. Unexpected exceptions become a generic
  `internal_error` plus the request id.
- `ErrorDetail.value_hint` is truncated to 64 characters and **suppressed entirely** when the
  field name looks secret-ish (`password`, `token`, `secret`, `key`, `authorization`,
  `cookie`).
- Structured JSON logging to stdout (`backend/src/app/core/logging.py`) applies the **same
  redaction list** to any extra field a caller attaches, replacing values with `[redacted]`.
- Report generation is the one place a broad `except Exception` is deliberately used: a
  renderer failure is persisted as a `failed` report row with an `error_message` and still
  returned as `201`, so a failed generation is an auditable, listable fact rather than a
  swallowed `500` (`backend/src/app/services/report_service.py`, Deviation 2).
- Every response carries `X-Request-ID`, and the same id appears in the error envelope and
  the security log, so an incident can be correlated without logging payloads.
- Rate limiting per client IP: 10/min on `/api/v1/auth/login`, 120/min elsewhere, rendered as
  the standard envelope with `Retry-After: 60`. In-memory and therefore **single-node only** —
  a multi-node deployment needs a shared store.

## 11. Secrets and data policy

- **No keys are committed.** `.gitignore` excludes `.env`, `.env.*` (keeping `.env.example`),
  `*.pem`, `*.key`, `.local-postgres/`, `.local-storage/`.
- `.env.example` documents every `PO_*` setting with clearly fake values and says so.
- **gitleaks runs in CI** on every push and pull request with `fetch-depth: 0`
  (`.github/workflows/ci.yml`, `secrets-scan` job).
- `PO_SECRET_KEY` has a deliberately obvious dev default and the application **refuses to
  start** in `prod` while it is unchanged; `cookie_secure` is forced `True` in `prod`
  regardless of configuration (`backend/src/app/core/config.py::Settings._fail_fast`).
- Secrets are typed `SecretStr` so they do not leak through `repr()` or serialization.
- API docs (`/api/docs`, `/api/openapi.json`) are disabled in `prod` unless `demo_mode` is
  explicitly on.

**Data policy:** the public repository contains **synthetic demonstration data only**.
Suppliers, parts, prices, quotes, and documents in `backend/src/app/seed/demo_dataset.py` and
`backend/tests/fixtures/` are invented; every email address uses the reserved `.example`
TLD. Demo credentials (`demo-owner@meridianfab.example` / `demo-owner-2026`, and the analyst
and viewer equivalents) are intentionally public and are labelled as such in the seed module.

## 12. The demo works without any paid key

A deliberate security-and-reproducibility property: `PO_EXTRACTION_PROVIDER=mock`,
`PO_NARRATIVE_PROVIDER=template`, `PO_OCR_PROVIDER=mock`, synthetic FX rates, and filesystem
storage are all defaults. No test and no demo path makes a network call to a third party, so
there is no key to leak and no vendor to trust with quote data. Selecting a real provider is
opt-in, requires an explicit key, and **fails fast** rather than silently degrading — and a
mock result is always labelled `simulated` wherever it surfaces.

## 13. Known gaps and production caveats

Stated plainly; also tracked in [ROADMAP.md](ROADMAP.md).

| Gap | Detail |
|---|---|
| Rate limiting is per-process | In-memory sliding window; a multi-node deployment needs Redis or equivalent |
| No segregation of duties | An `analyst` can upload and confirm the same document |
| Failed logins are not in the audit table | Org-scoped schema cannot represent an org-less failure; they are in the security log |
| No password reset / invitation flow | Admins add members directly; password changes are a CLI/DB operation |
| No session purge job | Expired session rows are ignored at resolve time but not swept |
| Audit table grants | Development runs as the table owner; production should `REVOKE UPDATE/DELETE` on `audit_events` from the application role as belt-and-braces alongside the append-only triggers |
| No report purge job | The schema and the `410`-after-purge path exist; nothing sweeps expired artifacts |
| Audit UI shows raw actor UUIDs | `audit_events.actor_user_id` is displayed unresolved; no join to `users.full_name` |
| No `pip-audit` / licence gate in CI | Planned; today CI runs ruff, mypy, migrations, tests, and gitleaks |
| No OpenAPI drift job | `docs/openapi.json` is not committed in this build |
| Inline job execution | A long solve holds an HTTP request open; there is no queue or backpressure |
| No in-app proxy-header handling | Behind a reverse proxy the app must be started with uvicorn's `--proxy-headers --forwarded-allow-ips=<proxy>` (DEPLOYMENT.md §8) or all clients share one rate-limit bucket and audit rows record the proxy's IP |
| Chunked uploads spool before rejection | A body without `Content-Length` bypasses `BodySizeLimitMiddleware` and is bounded only by the reverse-proxy cap (DEPLOYMENT.md §8) |
| Canary patterns are heuristic | Zero-width evasion is normalized away, but a paraphrased injection can still evade the flag; the hard gates are human confirmation and the numeric cross-check |
