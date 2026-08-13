# Vendor Negotiation and Procurement Optimizer — Project Specification

Repository: `procurement-optimizer` (working title). Portfolio-quality, database-backed
procurement intelligence platform. Public repo uses **synthetic demonstration data only**.
Not a commercial launch. Never include real company data, real supplier documents,
real negotiated prices, or real BOMs.

## Mission

Help a business: (1) create an RFQ, (2) upload inconsistent supplier quotes, (3) extract
and normalize quote information, (4) match quoted items to requested parts, (5) calculate
true landed cost, (6) compare suppliers with transparent criteria, (7) optimize
single-supplier or split-order allocation, (8) generate a grounded negotiation brief,
(9) export a CFO-facing recommendation, (10) preserve a complete audit trail.

## Intended users

Procurement analysts, finance managers, operations managers, SMB manufacturers, hardware
companies comparing supplier quotations.

## Required end-to-end workflow

Create organization → create suppliers → import parts/BOM → create RFQ → upload supplier
quotes → extract quote information → human review and correction → match quote lines to
RFQ lines → normalize currency and units → calculate landed cost → configure vendor
weights → compare suppliers → optimize allocation → generate negotiation brief → generate
report → preserve audit history.

---

## Functional requirements

### 1. Organizations and users
- Users, organizations, organization memberships.
- Roles: owner, administrator, analyst, viewer. Controlled public demo access; synthetic
  demonstration organization.
- All business data belongs to an organization. Cross-organization access to suppliers,
  parts, BOMs, RFQs, quotes, documents, analyses, reports, or audit events must be
  impossible. Enforce ownership **server-side**; never rely on frontend filtering.

### 2. Supplier management
Fields: name, code, contact info, country, supported currencies, standard payment terms,
standard shipping terms, typical lead time, capacity, MOQ expectations, quality history,
defect history, on-time delivery history, notes, active status, archived status.
All performance values are synthetic or user-entered. No fabricated live intelligence.

### 3. Parts
Fields: internal part number, manufacturer part number, name, description, category, unit
of measure, required specifications, approved alternatives, target price, historical
prices, active/archived status.
CSV and XLSX import with: header validation, row-level validation, duplicate detection,
import preview, transactional import, rollback after failure, import audit event.

### 4. Bills of materials
BOM name, version, associated product, status, component lines, required quantities,
optional substitutes, notes, creation timestamp, version history.

### 5. RFQs
Fields: name, internal reference, status, due date, requested delivery date, base
currency, parts, quantities, required specifications, requested payment terms, invited
suppliers, notes. Statuses: draft, open, under review, awarded, closed, archived.
Preserve meaningful status history.

### 6. Supplier quote uploads
Formats: text PDF, scanned PDF, PNG, JPEG, CSV, XLSX, manual entry.
Per document: validate file type and size; store securely; extract text/tables; convert to
structured fields; assign field-level extraction confidence; display extracted values for
human review; require confirmation for uncertain values; preserve original document;
preserve extraction versions; record user corrections; create audit events.
Never use an unconfirmed low-confidence value in a final recommendation without a
prominent warning.

### 7. Quote extraction fields
Supplier, quote number, quote date, expiration date, currency, part number, description,
quantity, unit of measure, unit price, price breaks, MOQ, tooling cost, setup cost,
packaging cost, shipping cost, insurance, shipping terms, country of origin, lead time,
production capacity, payment terms, warranty, taxes, tariffs, duties, customs fees,
additional fees, exceptions, exclusions, notes.
Missing information stays explicitly missing. Never silently invent a value.

### 8. Part matching
Strategies: exact internal part number, manufacturer part number, normalized text,
approved alternatives, configurable fuzzy matching.
Every non-exact match records: confidence, explanation, alternative candidates, human
confirmation status. Uncertain matches must not silently affect recommendations.

### 9. Currency normalization
Each exchange rate preserves: base currency, quote currency, rate, source, effective
date, timestamp, manual override, override reason. Public demo ships deterministic
synthetic exchange rates; automated tests must not depend on external services.

### 10. Unit normalization
Units: each, pack, box, tray, reel, kilogram, pound, meter, foot, user-defined.
Show conversion assumptions explicitly. Never compare prices until quantities and units
have compatible normalized meanings.

### 11. Price breaks
Tiered pricing (e.g. 1–99: $12.00, 100–499: $10.50, 500–999: $9.20, 1000+: $8.60).
Select applicable break from allocated quantity; recompute when split allocation changes
quantities. Test boundaries carefully.

---

## Landed-cost engine

Explainable, deterministic. **Exact decimal arithmetic for all monetary values — no
binary floating point.**

```
extended_material_cost = normalized_unit_price × accepted_quantity
allocated_fixed_cost   = tooling + setup + documentation + other_fixed_charges
logistics_cost         = shipping + insurance + packaging + handling
import_cost            = tariffs + duties + customs_fees
quality_risk_cost      = user-configured expected quality cost
delay_risk_cost        = user-configured expected delay cost
financing_cost         = optional cost tied to payment terms
total_landed_cost      = sum of all above
effective_unit_cost    = total_landed_cost / accepted_quantity
```

Each result shows: input values, formula, assumptions, data source, missing information,
manual overrides, calculation timestamp, calculation version. Missing required cost ⇒
result flagged incomplete/assumption-dependent, never hidden.

## Vendor comparison engine

Configurable criteria: total landed cost, effective unit cost, specification compliance,
lead time, capacity, MOQ flexibility, payment terms, quality history, defect rate,
on-time delivery, commercial exceptions, supply concentration, user-defined criteria.
Demonstration weights labeled as sample assumptions (e.g. landed cost 35%, spec
compliance 25%, lead time 15%, reliability 10%, payment terms 5%, MOQ flexibility 5%,
quality history 5%); users can change weights.
Correctly handle: higher-is-better, lower-is-better, equal values, missing data, zero
weights, extreme outliers, user overrides, excluded suppliers.
Scores reproducible **without an LLM**; show the reason for each score.

## Order-allocation optimization

Support: single-supplier and split-order recommendations, required quantity, supplier
capacity, MOQ, price breaks, max supplier concentration, max supplier count, spec
compliance, delivery deadline, budget limit, user-locked allocations, supplier
exclusions.
Deterministic solver (OR-Tools or equivalent). Document decision variables, objective,
constraints, feasibility, solver status, recommended allocation, expected total cost, why
alternatives were rejected.
Statuses reported honestly: optimal / feasible-not-proven-optimal / infeasible / solver
error. Never label everything "optimal". On infeasibility, explain conflicting
constraints.

## Scenario comparison

Strategies: lowest quoted unit price, lowest total landed cost, fastest delivery, lowest
supply risk, balanced, user-configured. Each saved scenario preserves: input quotes,
assumptions, exchange rates, scoring weights, constraints, results, solver status,
calculation version, timestamp, creator. Historical results reproducible after
assumptions change.

## Negotiation brief

Based only on confirmed supplier data, user-entered assumptions, deterministic results.
Contents: procurement objective, supplier position, quoted unit price, effective unit
cost, landed-cost comparison, price target, stretch target, walk-away threshold, volume
leverage, payment-term opportunities, lead-time concerns, quality concerns, spec
concerns, alternative suppliers, BATNA, recommended concessions, questions requiring
clarification, draft supplier email.
Clearly label: supplier-provided vs user-assumption vs calculated vs AI narrative vs
missing. AI must not invent market benchmarks, competitor quotes, historical prices,
supplier behavior, savings, concessions, or delivery promises. Human review required
before use; never auto-send emails.

## Reports and exports

Supplier-comparison report, CFO-facing recommendation, negotiation brief, scenario
summary, audit-history report; CSV, XLSX, professional PDF.
Each report: RFQ summary, supplier quotes, normalized assumptions, landed-cost
calculation, scoring, allocation recommendation, risks, missing data, methodology,
disclaimer, generation date, calculation version.

## Audit trail

Record: account actions; supplier create/edit; part imports; BOM changes; RFQ changes;
quote uploads; extraction runs and corrections; part matching and confirmations;
exchange-rate changes; assumption changes; weight changes; supplier inclusion/exclusion;
optimization runs; recommendation generation; report generation; manual overrides.
Each event: organization, user, event type, entity type, entity ID, before/after state
where appropriate, explanation, timestamp.

---

## Technical architecture (baseline; final selection documented by principal)

Frontend: TypeScript framework (principal selects) · Backend: FastAPI + Python ·
DB: PostgreSQL · ORM: SQLAlchemy 2.x · Migrations: Alembic · Validation: Pydantic ·
Object storage: S3-compatible abstraction (MinIO locally or equivalent) · Optimization:
OR-Tools or equivalent · Spreadsheets: openpyxl or equivalent · PDF: reliable Python
tooling · OCR / AI extraction / AI narrative: provider abstractions · Backend tests:
pytest · Frontend tests: appropriate TS framework · E2E: Playwright · Local services:
Docker Compose · CI: GitHub Actions.

Request flow: `API route → application service → domain service → repository →
PostgreSQL`. Route handlers thin; calculations separate from routes; DB logic separate
from business logic; AI and storage behind interfaces; financial calculations testable
without a database; demo mode testable without an external AI provider.

## Database requirements

UUID PKs, UTC timestamps, exact decimal money, FKs, check constraints, appropriate
cascades, indexes, transactions, organization ownership, historical versioning, soft
delete/archive where appropriate.

Minimum entities: users, organizations, organization_memberships, suppliers,
supplier_contacts, supplier_performance_records, parts, part_alternatives,
bills_of_materials, bill_of_material_lines, rfqs, rfq_lines, rfq_suppliers,
quote_documents, quotes, quote_lines, quote_price_breaks, quote_terms, extraction_runs,
extraction_fields, part_match_candidates, exchange_rates, comparison_scenarios,
scoring_configurations, scenario_results, allocation_results, negotiation_briefs,
generated_reports, audit_events.

Forbidden: localStorage as primary persistence, static JSON as production DB, in-memory
dicts as app storage, floating-point money.

Completion checks: Postgres starts locally; migrations succeed on empty DB; seed loads;
transactions roll back; data survives backend restart; historical scenarios preserved;
organization isolation enforced; clean clone reproduces the DB.

## Document and AI security

Every uploaded document is untrusted. Protect against: malicious filenames, unsupported
types, oversized files, embedded formulas, spreadsheet formula injection, path traversal,
prompt injection, malicious document instructions, cross-org data leakage, unsafe
rendering, exposed storage URLs, unvalidated extracted data.
Document content isolated as data in AI prompts; "ignore previous instructions" inside a
quote must not change behavior. AI extraction output passes: structured schema validation
→ type validation → business validation → confidence evaluation → human confirmation
where required.

## General security

Server-side authorization; org-isolation checks; secure password/session handling;
least-privilege storage; secure cookies; CSRF where applicable; CORS restrictions;
security headers; rate limiting; safe errors; secret management; upload limits; audit
logging; dependency review; production-safe configuration.
Never commit: API keys, passwords, `.env`, real supplier documents, real prices, real
BOMs, confidential company data.

## Synthetic demonstration dataset

Fictional manufacturer with: 1 demo org, ≥6 suppliers, ≥15 parts, ≥2 BOMs, ≥3 RFQs,
multiple quote formats, different currencies, conflicting MOQs, different price breaks,
lead times, shipping, tariff assumptions, payment terms, quality histories, delivery
histories.
Must include: a supplier with lowest unit price but not lowest landed cost; one
infeasible allocation scenario; one split-order scenario; one uncertain part match; one
extraction correction; one missing commercial term; one quote containing a
prompt-injection test string.
Synthetic fixture documents: PDF quote, scanned-image quote, CSV quote, XLSX quote.

## External-service strategy

Public demo works without any paid AI key: mock AI provider, deterministic fixture
extraction, synthetic exchange rates, prebuilt demo workflow, clear labeling of simulated
behavior. Optional real providers via environment variables. Never present a mock
response as live.

## API surface

Documented APIs for: authentication, organizations, memberships, suppliers, supplier
performance, parts, BOMs, RFQs, quote uploads, extraction review, quote corrections,
part matching, exchange rates, scenarios, landed-cost calculation, vendor scoring,
allocation optimization, negotiation briefs, reports, audit events. Clear request
validation; safe structured errors.

## Testing requirements

- **Calculation**: decimal arithmetic; each cost component; effective unit cost;
  currency conversion; unit conversion; price-break boundaries; MOQ boundaries; missing
  data; zero quantity; invalid negatives; extreme values.
- **Scoring**: direction handling, equal values, missing values, zero weights, weight
  normalization, outliers, exclusions, overrides, reproducibility.
- **Optimization**: 1 and N suppliers; split orders; capacity; MOQ; price-break changes;
  max concentration; max supplier count; budget limits; locked allocations; infeasible
  problems; solver failures; solver-status reporting.
- **Extraction**: text PDF, scanned image, CSV, XLSX; missing columns; invalid types;
  duplicate lines; low-confidence values; corrections; prompt-injection content;
  part-match confidence.
- **Integration**: migrations, org isolation, auth, upload persistence/rollback,
  extraction persistence, correction history, scenario versioning, storage integration,
  audit-event creation, report generation.
- **E2E** (no paid provider): enter demo org → create RFQ → add parts → upload 3 quotes
  → review extraction → correct an uncertain value → confirm matches → calculate landed
  costs → adjust weights → compare suppliers → run split-order optimization → generate
  negotiation brief → export report → reload app → restart services → data persists.

## GitHub deliverables

Professional README (screenshots/demo GIF, product description, business problem, key
features, architecture diagram, ERD, methodology, stack, local setup, env vars,
migration/seed/testing/demo instructions, security explanation, synthetic-data
explanation, limitations, roadmap), contributing guide, MIT license, `.env.example`,
`.gitignore`, issue templates, PR template, passing CI, v0.1.0 release notes.
Docs: `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/METHODOLOGY.md`,
`docs/DOCUMENT_PIPELINE.md`, `docs/OPTIMIZATION.md`, `docs/SECURITY.md`,
`docs/DATA_DICTIONARY.md`, `docs/DEPLOYMENT.md`, `docs/ROADMAP.md`.
No push/deploy/purchases/external resources without explicit user approval.

## Honest positioning

"A full-stack procurement intelligence platform that converts inconsistent supplier
quotes into transparent landed-cost comparisons, configurable vendor scores, optimized
order allocations, and grounded negotiation briefs."
Never claim: real customers, production adoption, real savings, validated prediction
accuracy, autonomous procurement, proprietary supplier data, guaranteed optimality
without solver evidence.

## Definition of done

Postgres persistence works; migrations succeed on empty DB; seed loads; org isolation
proven; all sample document formats work; uncertain extractions require review;
financial calculations pass hand-verified tests; scoring transparent and reproducible;
optimization handles feasible and infeasible cases; audit history works; reports
generate; data survives restarts; full workflow passes E2E; CI passes; no secrets; no
confidential data; new developer can reproduce from README; independent reviews
complete; principal performs final acceptance.
