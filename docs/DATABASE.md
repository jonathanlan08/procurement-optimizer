# Database

PostgreSQL 16, SQLAlchemy 2.x declarative models, Alembic migrations. This document covers
the schema conventions and the migration inventory. For a table-by-table column listing see
[DATA_DICTIONARY.md](DATA_DICTIONARY.md); for the isolation rationale see
[ARCHITECTURE.md](ARCHITECTURE.md) §3 and [SECURITY.md](SECURITY.md).

---

## 1. Conventions

All conventions live in `backend/src/app/models/base.py` and are applied by every business
table.

**Naming.** A `MetaData(naming_convention=…)` fixes index/constraint names
(`ix_%(table_name)s_%(column_0_N_name)s`, `uq_…`, `ck_…`, `fk_…`, `pk_…`), so Alembic
autogenerate produces stable names and migrations never drift on constraint naming.

**UUID primary keys, application-generated.** `PkMixin` declares
`id: Mapped[uuid.UUID] = mapped_column(primary_key=True)` with no server default. Ids come
from the injected `IdGenerator` (`backend/src/app/core/ids.py`), which makes id generation
deterministic under test.

**UTC timestamps.** `TIMESTAMP(timezone=True)` everywhere, named `*_at`, written by the
injected `Clock`.

**Exact decimals - never floats.** Fixed scales, ratified in
`docs/planning/00-decisions.md` §1.3:

| Kind | Column type | Constant |
|---|---|---|
| Monetary totals / component amounts | `NUMERIC(18, 6)` | `MONEY_SCALE` |
| Unit prices, effective unit cost | `NUMERIC(18, 8)` | `UNIT_PRICE_SCALE` |
| FX rates, unit-conversion factors | `NUMERIC(24, 12)` | `RATE_SCALE` |
| Quantities | `NUMERIC(18, 6)` | `QTY_SCALE` |
| Confidences, rates, performance ratios | `NUMERIC(9, 6)` | - |

Constants: `backend/src/app/core/money.py`. Money crosses the API as **strings**, always.

**`org_identity_constraint`.** Every org-owned table declares
`UNIQUE (organization_id, id)`. This is not redundant with the primary key - it is the
*target* that composite foreign keys point at.

**Composite organization foreign keys.** Children reference parents as
`(organization_id, parent_id) → parent(organization_id, id)`, so a cross-organization
reference is refused by PostgreSQL, not merely rejected by application code. Example, from
`backend/src/app/models/suppliers.py`:

```python
ForeignKeyConstraint(
    ["organization_id", "supplier_id"],
    ["suppliers.organization_id", "suppliers.id"],
    ondelete="RESTRICT",
)
```

`ondelete="RESTRICT"` is the default posture; the schema contains exactly one `CASCADE`
pair (documented in `backend/src/app/models/documents.py`).

**No PostgreSQL extensions.** By design, so the schema reproduces on any vanilla
PostgreSQL (migration `0001` states this at the top of `upgrade()`). Consequences:

- case-insensitive uniqueness uses **functional unique indexes on `lower(...)`** instead of
  `citext` - `uq_organizations_slug_lower`, `uq_users_email_lower`,
  `(organization_id, lower(code))` for suppliers, `(organization_id, lower(internal_part_number))`
  for parts, `(organization_id, lower(internal_reference))` for RFQs,
  `(organization_id, lower(name))` for scoring configurations;
- price-break range overlap cannot use a `btree_gist` exclusion constraint, so
  `UNIQUE (organization_id, quote_line_id, min_quantity)` stands in for it at the schema
  level and full overlap validation is done in the application
  (`backend/src/app/services/quote_service.py`; see migration `0009`'s docstring).

Most of those uniqueness indexes are **partial** - `WHERE archived_at IS NULL` - so
archiving frees a code/reference for reuse.

**Soft delete.** `ArchivableMixin` adds `archived_at`, `archived_by_id`, `archive_reason`.
Business data is never hard-deleted; archive routes set `archived_at` and are gated to
administrator+. Three `DELETE` routes exist, and only two of them actually remove a row:

- `DELETE /suppliers/{id}/contacts/{contact_id}` is an **archive**, not a delete
  (`SupplierContactService.archive`: "DELETE = archive (§4.4): soft delete, never a hard
  delete");
- `DELETE /parts/{id}/alternatives/{alternative_id}` genuinely removes the join row
  (`backend/src/app/services/part_service.py`) - an alternatives link carries no history of
  its own;
- `DELETE /rfqs/{id}/lines/{line_id}` genuinely removes the line, gated by
  `_ensure_draft_or_override` (draft RFQs only, unless an administrator supplies an
  override reason, which is recorded in the `rfq.line_removed` audit event).

**Optimistic locking.** `VersionedMixin` adds an integer `version` column on mutable
aggregates; the API accepts `If-Match` (`conflict_version` → 409).

**Append-only audit.** Migration `0001` installs a PL/pgSQL function
`audit_events_append_only()` plus two triggers: `trg_audit_events_append_only`
(`BEFORE UPDATE OR DELETE … FOR EACH ROW`) and `trg_audit_events_no_truncate`
(`BEFORE TRUNCATE … FOR EACH STATEMENT`). Both raise `audit_events is append-only`.

**Immutable result tables.** `landed_cost_results`, `scenario_results`,
`allocation_results`, and `generated_reports` have no `updated_at`/`version`/`archived_at`.
Recalculation inserts a new row (landed cost) or requires a whole new scenario
(`UNIQUE (organization_id, scenario_id)` on both scenario result tables). "Latest wins" is a
query concern, served by `ix_landed_cost_results_line_latest` on
`(organization_id, quote_line_id, calculated_at DESC)`.

**The two documented hard-delete exceptions** (ERD §11) are expired sessions and expired
report artifacts. A report purge keeps the row and drops only the blob: `storage_key` and
`content_sha256` are nulled together and `purged_at` is set. No purge job ships in v0.1
(see [ROADMAP.md](ROADMAP.md)); the schema is simply ready for one.

## 2. Migration inventory

Migrations live in `backend/migrations/versions/`, are linear (`0001 → 0015`), and are
**append-only once merged** (`CONTRIBUTING.md`). CI runs `alembic upgrade head` →
`downgrade base` → `upgrade head` against an empty database on every push, so every
migration has a working `downgrade()`.

| Rev | File | Creates |
|---|---|---|
| 0001 | `0001_identity_tenancy_audit.py` | `organizations`, `users`, `organization_memberships`, `sessions`, `audit_events` (+ append-only and no-truncate triggers), `jobs`; enums `role_enum`, `job_state_enum`; functional unique indexes on `lower(slug)` / `lower(email)`; partial unique membership index `WHERE revoked_at IS NULL`. |
| 0002 | `0002_suppliers.py` | `suppliers`, `supplier_contacts`, `supplier_performance_records`; first use of `UNIQUE (organization_id, id)` + composite org FKs; partial case-insensitive `uq_suppliers_org_code_active`. |
| 0003 | `0003_units.py` | `unit_definitions` (global catalogue rows have `organization_id IS NULL`, plus org-defined units) and `unit_conversions`; enum `dimension_enum` (`count`, `mass`, `length`). |
| 0004 | `0004_parts.py` | `parts`, `part_alternatives`; `normalized_key` maintained as `regexp_replace(lower(internal_part_number), '[^a-z0-9]', '', 'g')` for case/punctuation-insensitive matching; partial unique index on `(organization_id, lower(internal_part_number))`. |
| 0005 | `0005_boms.py` | `bills_of_materials`, `bill_of_material_lines`; enum `bom_status_enum`; copy-on-write version chain (`root_bom_id`, `version_number`, `previous_version_id`) with `UNIQUE (organization_id, root_bom_id, version_number)`. |
| 0006 | `0006_part_imports.py` | `part_import_batches`, `part_import_rows`; enums `part_import_format_enum` (`csv`, `xlsx`) and `import_state_enum`. |
| 0007 | `0007_rfqs.py` | `rfqs`, `rfq_lines`, `rfq_suppliers`, `rfq_status_history`; enum `rfq_status_enum`; partial unique index on `(organization_id, lower(internal_reference))`. |
| 0008 | `0008_exchange_rates.py` | `exchange_rates` - `NUMERIC(24,12)` rate, `source`, `effective_date`, `retrieved_at`, `is_manual_override` + `override_reason`. |
| 0009 | `0009_quotes.py` | `quotes`, `quote_lines`, `quote_price_breaks`, `quote_terms`; enums `quote_status_enum`, `quote_source_enum` (`manual`/`extracted`), `match_status_enum`. Documents the `btree_gist`-free stand-in for price-break overlap prevention. |
| 0010 | `0010_documents.py` | `quote_documents`, `document_pages`, `extraction_runs`, `extraction_fields`, `quote_corrections`, `part_match_candidates`; enums `document_state_enum`, `extraction_state_enum`, `confidence_band_enum`, `match_strategy_enum`; confidence bounds and confirmation-pairing CHECKs. |
| 0011 | `0011_analysis.py` | `landed_cost_results`, `landed_cost_components`, `scoring_configurations`; enums `landed_cost_result_completeness_enum`, `landed_cost_component_enum` (both mirroring the frozen domain enums); `ix_landed_cost_results_line_latest`. |
| 0012 | `0012_corrections_nullable.py` | Loosening only: drops `NOT NULL` on `quote_corrections.quote_id` and adds a nullable `extraction_run_id` composite FK - so a correction made *before* materialization (pipeline stage 10 precedes stage 11) has a row to live in and a durable link to its run. |
| 0013 | `0013_scenarios.py` | `comparison_scenarios` (five granular reproducibility snapshots: `constraints_snapshot`, `assumptions_snapshot`, `fx_snapshot`, `quote_snapshot_refs`, `weights_snapshot`), `scenario_results`, `allocation_results`; enums `comparison_strategy_enum`, `scenario_state_enum`, `allocation_status_enum`. Both result tables are immutable and unique per scenario. |
| 0014 | `0014_negotiation_briefs.py` | `negotiation_briefs` - `sections` JSONB keyed by SPEC section with a per-section provenance label, computed `price_target`/`stretch_target`/`walk_away_threshold`, `narrative_provider`, `simulated`, `state` (`brief_state_enum`). |
| 0015 | `0015_generated_reports.py` | `generated_reports`; enums `report_type_enum`, `report_format_enum`, `report_state_enum`. `storage_key`/`content_sha256` are nullable and CHECK-paired (`(storage_key IS NULL) = (content_sha256 IS NULL)`) so ERD §11's purge - row kept, blob dropped, `purged_at` set - is representable; `error_message` records why a `failed` row failed; index `ix_reports_org_time` on `(organization_id, generated_at DESC)`. |

Migration `0015` is the head of the chain at v0.1.0.

## 3. Enum handling

Enums are created as native PostgreSQL types with explicit
`op.execute("CREATE TYPE … AS ENUM (…)")` and referenced with `create_type=False`, so
Alembic never tries to create them twice. Several DB enums deliberately wrap a frozen
*domain* enum rather than redefining it - `landed_cost_component_enum` wraps
`app.domain.landed_cost.contracts.CostComponent`, `confidence_band_enum` wraps
`app.domain.confidence.ConfidenceBand`, `allocation_status_enum` wraps
`app.domain.optimization.contracts.AllocationStatus` - because one enum that cannot drift is
worth more than two that agree today.

## 4. Local database

Two supported paths, both documented in [DEPLOYMENT.md](DEPLOYMENT.md):

- **No Docker:** `uv run python scripts/dev_db.py` from `backend/` boots a user-space
  PostgreSQL via `pgserver` (data directory `~/.local/share/procurement-optimizer/pgdata`,
  deliberately outside the repo because `pg_ctl` cannot handle a socket path containing
  spaces), migrates to head, and prints the `PO_DATABASE_URL` to export. `--stop` stops it.
- **Docker:** `docker compose up -d` starts `postgres:16-alpine` on 5432 (and MinIO on
  9000/9001).

The test suite defaults to `pgserver` too: when `PO_TEST_DATABASE_URL` is unset, a real
PostgreSQL is spun up automatically. No test ever runs against SQLite.
