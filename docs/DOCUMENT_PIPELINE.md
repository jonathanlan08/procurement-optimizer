# Document pipeline

How a supplier quote document travels from an untrusted upload to structured quote rows -
and every place where the system refuses to guess.

Related: [SECURITY.md](SECURITY.md) · [METHODOLOGY.md](METHODOLOGY.md) ·
[DATA_DICTIONARY.md](DATA_DICTIONARY.md)

---

## 0. Stages at a glance

```
upload ──► transport validation ──► content validation (magic bytes) ──► storage
                                                                          │
                                        acquisition (text / tables)  ◄─────┘
                                                  │
                              injection canary scan  (flag, never block)
                                                  │
                             nonce-fenced envelope ──► ExtractionProvider
                                                  │
                          validation ladder ──► confidence banding
                                                  │
                          human review: confirm / correct
                                                  │
                          materialization (gated) ──► Quote + QuoteLines + terms
                                                  │
                                            part matching
```

Every step is synchronous in this build (see [ARCHITECTURE.md](ARCHITECTURE.md) §7).

## 1. Every uploaded document is untrusted

`backend/src/app/ingestion/file_validation.py` is a pure module - callers stream the bytes
and enforce the size cap at the transport layer; this module rules on what the bytes claim
to be.

**Type is decided by magic bytes, never by extension or `Content-Type`.**

| Kind | Sniffed signature |
|---|---|
| PDF | `%PDF-` |
| PNG | `\x89PNG\r\n\x1a\n` |
| JPEG | `\xff\xd8\xff` |
| XLSX | `PK\x03\x04` (the only zip format allowed) |
| CSV | no magic: must decode as UTF-8 or Latin-1, contain **no NUL bytes**, and claim a `.csv` extension |

After sniffing, the extension **and** the declared content type must both *agree* with the
sniffed kind, or the upload is rejected with a display-safe message that never echoes file
content. Empty files and files over `PO_MAX_UPLOAD_BYTES` (default 20 MiB) are rejected
before anything else.

**Filenames are data, not paths.** `sanitize_filename` strips both separator styles, takes
only the last path segment, NFKC-normalizes, drops non-printable characters, replaces
anything outside `[\w.\- ()\[\]]` with `_`, refuses empty/dotfile names, and truncates to
160 characters preserving the extension. The result is stored and displayed - it is *never*
used as a filesystem path.

**Storage keys are server-generated.** `validate_upload` returns
`storage_key = f"{uuid4().hex}{canonical_extension}"`. `StorageProvider.validate_key`
re-checks every key against `^[a-z0-9]{32}\.[a-z0-9]{2,5}$` in *every* public method of
*every* provider, and each provider namespaces by organization (`root/<org_id>/<key>` on the
filesystem, `<bucket>/<org_id>/<key>` on S3). Path traversal is structurally impossible, and
org isolation is enforced at the storage layer itself rather than trusted from the caller.

**No public URLs, ever.** Bytes are streamed back through
`GET /api/v1/quote-documents/{id}/content` as a `StreamingResponse` with
`Content-Disposition: attachment` and the sanitized filename; the global
`X-Content-Type-Options: nosniff` header applies. There is no pre-signed-URL path.

**Per-organization dedupe.** `quote_documents` carries
`UNIQUE (organization_id, content_sha256)`; a repeat upload returns `409 conflict_duplicate`
with the existing document id in the error details.

`DocumentService.upload` orders its work validate → dedupe → store → create row → audit, so a
storage failure never leaves a metadata row pointing at bytes that were never written. Rows
are created directly in `DocumentState.STORED`; a content-validation failure creates no row
at all.

## 2. Acquisition

`backend/src/app/ingestion/acquisition.py` - pure over already-validated bytes; no network,
no disk writes, no OCR execution.

| Kind | Strategy |
|---|---|
| PDF | `pypdf` supplies per-page text; `pdfplumber` additionally locates ruled tables and appends each as pipe-separated rows. `pdfplumber` is best-effort - a failure there must never discard text `pypdf` already produced. |
| CSV | decoded (`utf-8-sig`, falling back to Latin-1) and passed through as-is |
| XLSX | `openpyxl` `read_only=True, data_only=True` (no formula evaluation); one page per sheet, cells joined with `\|` |
| PNG / JPEG | one page, empty text, `used_ocr=True` |

**Acquisition caps** bound the cost of the parse: `MAX_ACQUIRED_ROWS = 10_000`,
`MAX_ACQUIRED_COLUMNS = 50`, breached → `AcquisitionLimitError`, which fails the run with
`error_code="acquisition_limit_exceeded"` rather than crashing. These are distinct from (and
smaller than) the upload-time content caps.

**OCR is a provider seam, not a shipped engine.** v0.1.0 bundles no OCR; image documents
acquire as a single page with empty text and `used_ocr=True`. A real OCR adapter would honor
the same contract and simply fill in the text. The mock extraction provider answers from its
fixture registry rather than pretending to read pixels.

Acquisition deliberately does **not** filter, redact, or judge the text it returns: an
injection string must survive acquisition byte-for-byte so the canary scanner downstream can
see it. Filtering would be worse - it would hide the attack instead of recording it.

## 3. The prompt-injection trust boundary

`backend/src/app/providers/extraction/envelope.py` - principal-owned.

**Structural defense - the nonce fence.** Document text is wrapped between per-request
fences built from `secrets.token_hex(16)`:

```
<<<DOC {nonce}>>>
[page 1]
…document text…
<<<END-DOC {nonce}>>>
```

The nonce is generated *after* upload, so a document cannot fabricate a closing fence it has
never seen. Lookalike fence markers (`<<<`, `>>>`) in the document text are neutralized
anyway, as defense in depth. The system instructions live outside the fences and state
explicitly that everything inside is untrusted third-party data, that a field the document
does not state is `null`, that the model must never estimate or invent, and that text
addressing the model has no effect. **The Anthropic adapter, if built, must construct its
prompt only through this module.**

**Observational defense - the canary detector.** `scan_for_injection` runs over the real
acquired page text, *before* the provider is called, matching ten instruction-shaped
patterns (`ignore (all|any) (previous|prior|above|earlier) instructions`,
`disregard …`, `you are now …`, `system prompt`, `as an AI`, `pretend to be`,
`reveal (your|the) (instructions|prompt|system)`, `do not (tell|inform) the user`,
`instead of extracting`, `set (the )?(price|confidence|total) to`).

A match **flags, it does not block**:

1. a `security.injection_suspected` audit event is written, carrying the matched snippets
   (truncated to 80 characters each) and the document id;
2. the verdict is persisted on the run itself, inside `extraction_runs.raw_response` as
   `{"injection_scan": {"suspected": …, "matches": […]}}`;
3. every field the run produces gets `injection_flagged = true`;
4. the review UI shows a banner.

Flag-not-block is deliberate: the phrase may be a legitimate part of the quote ("please
ignore our previous quotation"), and silently dropping a supplier's line item would be a
worse failure than surfacing a warning.

## 4. Extraction providers

Contract: `backend/src/app/providers/extraction/base.py`, schema version `1.0`. Rules
enforced by the contract:

- every numeric value is a decimal **string**; no floats;
- a field the document does not state is `None` - providers never invent a value to fill the
  schema;
- provider output is **untrusted** until it clears the validation ladder;
- `provider_notes` carries provider diagnostics only, never document text;
- `injection_suspected` is set by the canary detector in the service layer, never by a
  provider.

The payload covers quote-level fields (supplier, quote number, quote date, expiration,
currency), per-line fields (part number, description, quantity, UoM, unit price, MOQ, lead
time, country of origin, production capacity, tooling/setup/packaging/shipping/insurance
costs, tariff/duty/customs/tax, plus price-break tiers), and terms (payment, shipping,
warranty, exceptions, exclusions, notes).

### The mock provider is deterministic, never random

`backend/src/app/providers/extraction/mock.py`, `is_simulated = True` **unconditionally**.

- Given a `document_sha256` present in the registry
  (`backend/tests/fixtures/extraction/<sha256>.json`), it returns that exact golden payload.
- For an unrecognized hash it falls back to a deliberately weak regex heuristic over the
  acquired page text - fixed confidence 0.5 per field it manages to find, 0.4 overall - so
  an arbitrary upload still demonstrates the review flow instead of failing outright. It is
  not trying to compete with a real model, and its `provider_notes` says so.

Selecting `PO_EXTRACTION_PROVIDER=anthropic` raises `ProviderUnavailableError`: no adapter
ships in this build, and the system never quietly substitutes the mock for a provider you
asked for. `is_simulated` flows into `extraction_runs.simulated` and surfaces in the UI as a
"Simulated" chip.

## 5. The four byte-deterministic fixtures

`backend/scripts/generate_fixtures.py` generates both the documents and their golden
payloads. Documents live in `backend/tests/fixtures/documents/`; goldens in
`backend/tests/fixtures/extraction/`, **keyed by the document's own actual sha256** (computed
after generation, never hand-typed).

| Fixture | Format | What it proves |
|---|---|---|
| `shenzhen_precision_quote.pdf` | native-text PDF (USD) | clean happy path with a price-break table and "Net 60" terms |
| `pacific_metal_quote.png` | rasterized scan, no text layer (MXN) | the uncertain-extraction case - OCR-shaped confidences including one field below 0.60 alongside several in the 0.60-0.95 band |
| `nordic_fastener_quote.csv` | CSV (EUR) | the **prompt-injection acceptance test** |
| `baltic_casting_quote.xlsx` | XLSX (EUR) | the missing-commercial-term case: the Payment Terms cell is genuinely blank and the golden's `terms.payment_terms` is `MISSING`, never invented |

**Byte determinism** matters because the goldens are keyed by hash. PDFs are built with
ReportLab's `invariant=1` (fixed object ids and timestamps); the PNG is rasterized from an
invariant PDF via `pypdfium2` at a fixed scale and encoded with `struct`/`zlib` only (no
Pillow); the XLSX archive is rebuilt with every ZIP member timestamp pinned and
`docProps/core.xml`'s `<dcterms:modified>` patched, because `openpyxl` re-stamps it on every
save; CSV is plain text. Re-running the generator and diffing `git status` should show
nothing.

### The injection fixture, specifically

One line's `notes` column in `nordic_fastener_quote.csv` literally contains an injection
attempt ending in "…set unit_price to 0.01". The committed golden reports the **real** unit
price (`0.024`), never the injected one. Two independent reasons it is inert:

1. there is no `notes` field on `ExtractedLine` at all - the attempted instruction has no
   schema slot to land in even in principle;
2. the canary scanner flags the document, the run is marked, and a
   `security.injection_suspected` audit event is written.

The fixture's `injection_suspected` stays `False` in the golden by design: that flag belongs
to the service layer's scan over acquired text, not to a provider or a fixture.

## 6. The validation ladder

Run per field in `backend/src/app/services/extraction_service.py::_build_fields`.

1. **Schema validation** - Pydantic parses the provider payload into
   `ExtractedQuotePayload`; a structurally invalid response fails the run with
   `error_code="provider_error"`, recorded on the run, never swallowed.
2. **Type validation** - `_validate_value(value_type, raw)` coerces text / decimal / integer
   / date / currency. `raw is None` (not stated) is **always valid**: there is nothing to
   type-check, and "missing" is a legitimate answer. Only a *stated* value that fails to
   parse is a validation failure.
3. **Business validation** - quantities and unit prices must be positive; price-break tiers
   are checked for the three structural rules (duplicate `min_quantity`, an open-ended tier
   that is not the highest, overlapping ranges). Crucially, this step flags **every**
   offending tier independently rather than aborting at the first: one bad date must not
   discard forty good line items.
4. **Confidence banding** - `app.domain.confidence.band()` assigns `high` / `medium` / `low`
   at the 0.95 / 0.60 thresholds.
5. **Human confirmation** - `requires_confirmation = (band != HIGH) or not valid`. A field
   that failed validation has `normalized_value` cleared to `NULL` and always requires
   confirmation.

Every field is persisted as an `extraction_fields` row carrying both `raw_value` and
`normalized_value`, its `confidence`, `band`, `source_page`, and `injection_flagged`.

This is a documented **narrowing** of `docs/planning/04-document-pipeline.md` §7, which also
specified a composed confidence (`provider × acquisition × validation_penalty`) and a
"critical field set" that always requires confirmation regardless of confidence. The
implementation uses the provider's own per-field confidence as-is and force-flags nothing;
the reasoning is spelled out in the service's module docstring. Every shipped fixture still
lands in `needs_review` under the simpler rule.

## 7. Review, correction, and the materialization gate

`extraction_runs.state` follows an explicit transition map
(`ALLOWED_EXTRACTION_RUN_TRANSITIONS` in `backend/src/app/models/documents.py`), transcribed
from the pipeline document's state diagram and stored as shared data rather than scattered
`if` statements:

```
queued → running → {failed_transient, failed, extracted}
failed_transient → {queued, failed}
failed → superseded
extracted → {needs_review, ready}
needs_review → needs_review   (field corrected / confirmed - a real self-transition)
needs_review → ready
ready → {materialized, superseded}
```

- `PATCH /api/v1/extraction-runs/{run_id}/fields/{field_id}` records a **correction**: the
  new value is re-validated, a `quote_corrections` row captures before/after with the actor
  and reason, and an `extraction.field_corrected` audit event is written. Confirming a value
  as-is writes `extraction.field_confirmed`.
- Since migration `0012`, a correction can be recorded **before** materialization
  (`quote_corrections.quote_id` is nullable and `extraction_run_id` was added), because the
  pipeline puts corrections at stage 10 and materialization at stage 11.
- **Materialization is gated.** `ExtractionService.materialize` requires
  `state == READY` and refuses with `409 conflict_state` if *any* `low`-band field is
  unconfirmed, listing each offending `field_path` in the error details. It then re-checks
  RFQ and supplier eligibility (the same gates manual quote entry applies) before building
  `Quote` / `QuoteLine` / `QuotePriceBreak` / `QuoteTerms` rows with
  `source = QuoteSource.EXTRACTED`, and writes `extraction.materialized`.
- Re-extraction **suggests** previous corrections; it never silently reapplies them
  (`docs/planning/00-decisions.md` §4).

The frontend review pane (`frontend/src/features/extraction/ReviewPane.tsx`) shows the
"Simulated" chip, the injection banner, per-field confidence badges, and disables the
materialize action until the gate is satisfied.

## 8. Part matching

`backend/src/app/domain/matching/matcher.py` - pure, no ORM session, no org scoping;
`backend/src/app/services/matching_service.py` loads the org's catalogue (scoped to the RFQ's
lines) and persists `part_match_candidates`.

Five strategies, in execution priority order with their confidences:

| # | Strategy | Confidence |
|---|---|---|
| 1 | `internal_pn` - exact internal part number | **1.00** (auto-confirmed) |
| 2 | `mpn` - manufacturer part number | 0.97 |
| 3 | `alternative` - approved alternative | 0.90 (§10 reserves 0.75 for `conditional`) |
| 4 | `normalized_text` - lowercased, non-alphanumerics stripped | 0.85 |
| 5 | `fuzzy` - `rapidfuzz` `token_set_ratio` | `min(0.80, 0.5 + 0.4·(r − t)/(1 − t))` for `r ≥ t` |

The fuzzy confidence is **capped at 0.80** regardless of how high the raw ratio climbs, so a
fuzzy match can never outrank strategies 1-4 by confidence alone. Where the delegating task's
prose disagreed with `docs/planning/04-document-pipeline.md` §10 (priority of
`normalized_text` vs `alternative`, several confidence values, `token_sort` vs `token_set`),
the planning document's literal table won; the conflict table is reproduced in the module's
own docstring.

Every non-exact match records confidence, an explanation, ranked alternative candidates, and
its human-confirmation status. Unconfirmed matches never silently affect a recommendation.
