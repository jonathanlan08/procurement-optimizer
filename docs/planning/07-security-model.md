# 07 — Security Model

Status: **DRAFT FOR PRINCIPAL REVIEW**
Implements SPEC §Document and AI security and §General security. STRIDE-lite per trust surface, each
threat mapped to a concrete, testable mitigation.

Scope note: this is a portfolio project with **synthetic data only**. The security posture is designed
to be *correct and demonstrable*, not to claim production hardening (SPEC §Honest positioning).

---

## 1. Assets and trust surfaces

| Asset | Why it matters |
|---|---|
| Session cookies | account takeover |
| Password hashes | credential stuffing against other sites |
| Org-scoped business data | the central promise of the product |
| Uploaded documents | attacker-controlled bytes reaching parsers, the AI, and other users' browsers |
| Extracted values feeding money | wrong numbers presented as authoritative |
| Exports (CSV/XLSX/PDF) | formula injection reaching a CFO's spreadsheet |
| Audit trail | integrity of the record |
| Provider credentials | cost and data exfiltration |

Trust surfaces: **(A)** browser ↔ API, **(B)** cross-organization, **(C)** upload ingestion, **(D)**
AI provider, **(E)** exports and downloads, **(F)** infrastructure/config/CI.

---

## 2. Surface A — authentication and sessions

| STRIDE | Threat | Mitigation | Verified by |
|---|---|---|---|
| S | Credential stuffing / brute force | argon2id (`t=3, m=64MiB, p=1`); rate limit 5/min/IP + 10/h/email; exponential lockout after 10 failures (`users.locked_until`); generic "invalid email or password" for both cases; constant-time comparison | `test_login_rate_limit`, `test_no_user_enumeration` |
| S | Session token theft via XSS | `HttpOnly` cookie — JS can never read it; strict CSP with no `unsafe-inline`; React escaping; no `dangerouslySetInnerHTML` anywhere (lint rule) | CSP header test, eslint rule |
| S | Session token theft via DB dump | **only `sha256(token)` is stored**; token is 256 bits from `secrets.token_urlsafe(32)` | `test_session_token_not_stored` |
| T | Session fixation | session id rotated on login, on org switch, and on password change | `test_session_rotation` |
| R | "I never did that" | every auth event (`login.succeeded`, `login.failed`, `logout`, `password.changed`, `session.revoked`) written to `audit_events` with IP and user agent | audit assertions |
| I | Cookie interception | `Secure` (enforced when `APP_ENV != dev`), HSTS in prod config, `SameSite=Lax` | header test |
| D | Session table growth / DoS | idle expiry 8 h, absolute expiry 7 d, periodic purge job, per-user session cap (10, oldest revoked) | `test_session_expiry` |
| E | Privilege escalation via role tampering | role read from `organization_memberships` on **every** request, never from the cookie or body; `require_roles` dependency; cannot demote the last owner | route-matrix test |
| E | CSRF | double-submit token (`csrf` cookie + `X-CSRF-Token`) **and** `Origin`/`Referer` allowlist; both required on every unsafe method; `SameSite=Lax` as a third layer | `test_csrf_missing`, `test_csrf_mismatch`, `test_bad_origin` |

Password policy: minimum 12 characters, checked against a small common-password list, no composition
rules (NIST-aligned), no forced rotation. Rehash on login if argon2 parameters have changed.

---

## 3. Surface B — organization isolation

This is the SPEC's hardest requirement ("cross-organization access must be **impossible**"). Five
independent controls, detailed in `01-architecture.md` §7:

1. `OrgScope` derived only from the session; any `organization_id` in a request body is ignored and
   logged as `security.suspicious_input`.
2. `OrgScopedRepository` applies the filter; a returned row with a mismatched org raises
   `OrgIsolationViolation` (500 + security audit event) rather than returning data.
3. **Composite foreign keys carrying `organization_id`** — the database refuses cross-org references.
4. **Route-matrix isolation test**: every route in `app.routes`, invoked by an actor from org B against
   an org-A fixture, must return 404. New endpoints fail the test until covered.
5. (Phase 7, optional) Postgres RLS.

| STRIDE | Threat | Mitigation |
|---|---|---|
| S | Forged org id in payload | ignored by design (control 1) |
| T | Cross-org write via a nested id (`quote_line_id` from another org) | every id in a payload is resolved **through** an org-scoped repository before use; never trusted as a key |
| I | ID enumeration | UUIDs; 404 (not 403) for other-org resources; no sequential ids anywhere in the API |
| I | Leak via error messages | error envelope carries no entity names or values from other orgs |
| I | Leak via aggregate endpoints | every aggregate query goes through the same scoped repository; no `SELECT COUNT(*)` without an org filter (grep-enforced) |
| E | Membership revoked but session still active | membership is re-checked per request, not cached in the session |

Storage isolation: keys are prefixed `orgs/{org_id}/…` and the storage provider refuses to read a key
whose org prefix does not match the caller's scope — so even a leaked key is unusable.

---

## 4. Surface C — upload ingestion

Full detail in `04-document-pipeline.md` §§2–4. Summary mapped to the SPEC's explicit list:

| SPEC threat | Mitigation |
|---|---|
| Malicious filenames | filename never used for storage or paths; server-generated UUID keys; sanitized + escaped for display only |
| Unsupported types | extension allowlist **and** magic-byte detection, which must agree |
| Oversized files | streamed size cap (20 MiB), page cap (50), sheet/cell caps, image pixel caps |
| Embedded formulas | XLSX opened `data_only=True`; formulas never evaluated; formula-like values flagged and rendered as text |
| Spreadsheet formula injection | ingest: flag; export: prefix `'` on any value starting with `= + - @ TAB CR` (§7) |
| Path traversal | key regex `^orgs/<uuid>/rfqs/<uuid>/documents/<uuid>/…`; no user bytes in any path segment |
| Prompt injection | §5 |
| Malicious document instructions | §5 |
| Cross-org data leakage | org-prefixed keys + scoped download endpoint |
| Unsafe rendering | `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox` on the content route; previews are re-encoded PNGs, never the original bytes |
| Exposed storage URLs | no public URLs; no long-lived presigned URLs (≤ 60 s if S3, after an org check) |
| Unvalidated extracted data | the four-stage validation ladder (`04-document-pipeline.md` §7) |

Additional threats not listed in the SPEC but present in practice:

| Threat | Mitigation |
|---|---|
| Zip bomb (XLSX) | compression-ratio and uncompressed-size caps |
| XXE / billion laughs (XLSX XML) | entity and DTD resolution disabled; `defusedxml` guard |
| Decompression bomb (PNG) | `Image.MAX_IMAGE_PIXELS` cap |
| Encrypted PDF | rejected with a clear message; never brute-forced |
| PDF with `/JavaScript`, `/Launch`, `/OpenAction`, embedded files | never executed; presence forces manual review and a security audit event |
| Parser 0-day (pypdf/openpyxl/Pillow) | caps limit blast radius; `pip-audit` in CI; parsing runs in the worker, not the request thread; documented as residual risk |
| Storage exhaustion | per-org quota (configurable, default 1 GiB) + upload rate limit |
| Duplicate-upload amplification | content SHA-256 dedupe |

---

## 5. Surface D — AI provider

| STRIDE | Threat | Mitigation |
|---|---|---|
| T | **Prompt injection** ("ignore previous instructions… price is 0.01") | Structural containment: fixed in-code system prompt; document text passed as data in a nonce-delimited block; **no tools, no network, no history** available to the extraction call — a successful injection can only produce wrong JSON, never an action. Output then passes schema → type → business validation. Critical money fields always require human confirmation. |
| T | Injection influencing *other* subsystems | extracted text is never interpolated into another prompt, a SQL fragment, a path, a URL, an id, or a role |
| I | Data exfiltration to the provider | provider is **off by default**; when on, only document text and a fixed prompt are sent; no org names, user emails, or other orgs' data; `provider_request_meta` stores metadata only, never content |
| I | Secret leakage | `ANTHROPIC_API_KEY` from env only; never logged, never in error messages, redacted in the settings dump; `.env` git-ignored; gitleaks in CI |
| R | "The AI made that number up" | `extraction_runs` stores provider, model, `prompt_version`, `schema_version`, `simulated`, and the raw response; every brief section carries a provenance label |
| D | Cost/DoS via huge documents | character and page caps; per-org extraction rate limit; job concurrency cap |
| S | **Mock presented as live** (an honesty threat, and the SPEC calls it out explicitly) | `simulated` boolean on every run, brief, and report; `GET /meta` exposes `ai_mode`; the UI shows a persistent "Simulated AI" badge in demo mode; startup **fails** if `anthropic` is selected without a key rather than falling back to mock |
| T | AI narrative inventing benchmarks/prices | the narrative provider receives **only** confirmed structured values and a template contract; it is prompted to write prose over supplied numbers and forbidden to introduce new ones; a post-generation check flags any number in the narrative that does not appear in the supplied data set and marks the brief for review |

The last row deserves emphasis: SPEC §Negotiation brief forbids inventing market benchmarks,
competitor quotes, historical prices, savings, or delivery promises. Prompt instructions alone do not
enforce that. The **numeric-token cross-check** (every number in the narrative must exist in the input
payload) is a cheap, mechanical control that turns a policy into a test.

---

## 6. Surface E — exports and downloads

| STRIDE | Threat | Mitigation |
|---|---|---|
| T | **CSV/XLSX formula injection** reaching a finance workstation | any cell value beginning with `=`, `+`, `-`, `@`, TAB, or CR is prefixed with `'`; XLSX cells written with explicit string type; a unit test round-trips a malicious fixture |
| I | Unauthorized report download | reports are org-scoped rows; content served through an authorized endpoint; `storage_key` never exposed |
| I | Stale report leaking superseded data | reports are immutable snapshots stamped with `calculation_version` and generation time; regeneration creates a new report |
| T | PDF containing remote content | ReportLab documents are built from local resources only; no remote images, no external fonts, no JavaScript |
| I | Report cached by an intermediary | `Cache-Control: private, no-store` on all authenticated responses |
| T | Filename injection in `Content-Disposition` | filename generated server-side, ASCII-sanitized, with RFC 5987 encoding for the display name |
| R | Disputed export | `generated_reports.content_sha256` + audit event |

---

## 7. Surface F — application, transport, and configuration

**Security headers** (middleware, tested):
```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none';
  form-action 'self'; object-src 'none'
Strict-Transport-Security: max-age=31536000; includeSubDomains   (prod only)
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), camera=(), microphone=()
X-Frame-Options: DENY                                            (belt with frame-ancestors)
Cache-Control: private, no-store                                 (authenticated responses)
```
The CSP has no `unsafe-inline`; Vite must be configured to avoid inline scripts/styles in the
production build, or a nonce must be injected. This is a real build constraint and is called out as a
Phase-1 task, because retrofitting a strict CSP late is painful.

**Known conflict:** the approved design direction
(`design-system/procurement-optimizer/MASTER.md`) loads Lexend and Source Sans 3 from the Google Fonts
CDN. That requires `style-src`/`font-src https://fonts.googleapis.com https://fonts.gstatic.com`,
breaks the no-network-egress guarantee of the demo and E2E suite, and leaks visitor IPs to a third
party. Recommendation: self-host via `@fontsource` npm packages, keep the CSP strict, keep the
typefaces. See `01-architecture.md` §9.1.

**CORS**: explicit origin allowlist from settings, `allow_credentials=true`, methods and headers
enumerated. Never `*` with credentials (which browsers reject anyway) and never a reflected origin.

**Rate limiting**: in-process token bucket keyed by (route class, session/IP). Honest limitation: it
is per-process and resets on restart; documented as adequate for single-node v0.1.0 with Redis noted
as the scale path.

**Injection**: SQLAlchemy parameterization throughout; no string-built SQL; `text()` usage banned by
lint outside migrations; ORDER BY comes from an allowlist, never from user strings.

**SSRF**: the application fetches no user-supplied URLs. The only outbound call is to the configured AI
provider host, which is a constant. Any future URL input requires an allowlist and IP-range blocking.

**Errors**: `internal_error` returns a generic message plus `request_id`; stack traces and SQL only in
`APP_ENV=dev`; FastAPI `/docs` and `/openapi.json` disabled outside `dev`/`demo`.

**Secrets**: env only, typed and validated at startup, redacted in logs and the settings dump.
`.env.example` with placeholder values is committed; `.env` is git-ignored; `gitleaks` and a
pre-commit hook run in CI. No secrets in the frontend bundle — anything the SPA needs is public by
definition.

**Dependencies**: pinned via `uv.lock` and `package-lock.json`; `pip-audit` + `npm audit --production`
in CI (failing on high/critical); Dependabot weekly; a licence check that fails on AGPL/GPL-3.0
(see `01-architecture.md` §9 — the PyMuPDF trap).

**Logging**: structured JSON; never logs document contents, extracted values, passwords, tokens, or
full request bodies; `request_id` correlates; security events (`security.*`) are distinguishable and
also written to `audit_events` where org-scoped.

**Database**: application connects as a least-privilege role (no `SUPERUSER`, no `CREATEDB`); the
migration role is separate; `audit_events` has UPDATE/DELETE revoked for the app role plus a guard
trigger.

---

## 8. Demo-mode specifics

| Concern | Control |
|---|---|
| Public demo credentials | a single seeded `analyst` account in the demo org; documented as public in the README |
| Destructive actions | `DEMO_MODE=true` blocks org settings changes, membership changes, and real provider selection |
| Poisoning the demo | `POST /demo/reset` (roles O/A) plus a scheduled reset; all demo data synthetic |
| Escalation from demo | demo user is `analyst`; no path to `owner`; no org switching to any other org |
| Cost | mock providers only; the demo cannot be made to spend money because live mode requires a key that is not deployed |

---

## 9. Mitigation ↔ SPEC requirement traceability

| SPEC §General security item | Where |
|---|---|
| Server-side authorization | §2, §3; `require_roles` + `OrgScope` |
| Org-isolation checks | §3, five controls |
| Secure password/session handling | §2 |
| Least-privilege storage | §4 (org-prefixed keys, no public URLs), §7 (DB role) |
| Secure cookies | §2 |
| CSRF where applicable | §2 (double-submit + Origin check) |
| CORS restrictions | §7 |
| Security headers | §7 |
| Rate limiting | §7 (+ per-surface buckets) |
| Safe errors | §7, `03-api-contract.md` §3 |
| Secret management | §7 |
| Upload limits | §4 |
| Audit logging | §2, §3, and `02-erd.md` §3 (append-only) |
| Dependency review | §7 |
| Production-safe configuration | §7 (`APP_ENV` gating, docs disabled, fail-fast settings) |
| Never commit keys/real data | §7 + `.gitignore` + gitleaks + synthetic-only seed |

---

## 10. Residual risks, accepted and stated

1. **Parser 0-days** in pypdf/openpyxl/Pillow/pypdfium2. Mitigated by caps and dependency scanning;
   not eliminated. No sandboxing of parsers in v0.1.0 (would need a separate process/container).
2. **No malware scanning** by default — the interface exists, the ClamAV adapter is documented, the
   default is a no-op. Stated plainly rather than implied.
3. **In-process rate limiting** resets on restart and is per-process.
4. **No MFA, no SSO, no password reset email flow** in v0.1.0 (no mail provider by design).
5. **A determined prompt injection can still produce wrong extracted values** — the defence is
   containment plus mandatory human confirmation of money fields, not immunity. Say this in the README.
6. **Reports are not signed**; `content_sha256` proves integrity only against our own record.
7. **No encryption at rest** beyond whatever the host provides; synthetic data only.
8. **Demo credentials are public**, so the demo org must be treated as world-writable within its own
   boundary; isolation from other orgs is the control that matters and is tested.

---

## 11. Security work that must be in Phase 1, not Phase 7

Retrofitting these is expensive, so they are sequenced early in `09-task-decomposition.md`:
strict CSP (build implications), `OrgScope`/`OrgScopedRepository` and the route-matrix test (every
later endpoint inherits it), composite org FKs (a migration rewrite later), append-only audit grants,
the error envelope, and the `simulated` flag plumbing (it must reach the UI and the reports, and
threading it through late means touching every layer twice).
