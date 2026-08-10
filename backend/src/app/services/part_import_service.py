"""Part import service — org-scoped orchestration of CSV/XLSX part imports
(SPEC §3; docs/planning/03-api-contract.md §4.5).

Three phases, matching the contract's three mutating routes:

- `preview()` (`POST /part-imports`): parses + validates the file (`app.
  importing.part_import_parser`, pure/no-DB), then — still in this one
  request's transaction — persists a `PartImportBatch` and one
  `PartImportRow` per data row with a `disposition` already assigned
  (`create`/`skip_duplicate`/`error`). **Nothing is written to `parts`
  here** (§4.5). Services never commit (`app.services.audit` docstring);
  `app.api.deps.get_db` commits on success, rolls back on any raised
  exception.
- `commit()` (`POST /part-imports/{id}/commit`): all-or-nothing. Refuses
  outright if the batch is not `previewing` (idempotent-guard, `409
  conflict_state` — this task's brief) or if it contains any `error` row
  (the whole batch is refused, not just that row — see
  `PartImportService.commit` docstring for why). Otherwise creates one
  `Part` per `create` row via `PartService.create` — the exact same
  validation/duplicate/audit path a hand-entered part goes through — and
  lets any failure (a duplicate that appeared since preview, a unit that
  stopped resolving) propagate unhandled: `get_db`'s rollback-on-exception
  is *itself* the "rollback after failure" SPEC §3 asks for, so nothing
  extra is needed here to make partially-created parts disappear.
- `cancel()` (`POST /part-imports/{id}/cancel`): marks the batch
  `rolled_back`. Also idempotent-guarded.

Unit resolution (`unit_code` -> `unit_definition_id`) and existing-active-
part duplicate detection both need the database, so both happen here, not in
the pure parser — see `app.importing.part_import_parser` module docstring.
`commit()` re-runs both (this task's brief: "re-check duplicates") rather
than trusting what `preview()` recorded, because time passes between preview
and commit and another request may have changed what is true.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import (
    ConflictStateError,
    ErrorDetail,
    ValidationAppError,
)
from app.core.ids import IdGenerator
from app.importing.part_import_parser import (
    PartImportParseError,
    RowError,
    ValidatedRow,
    parse_csv,
    parse_xlsx,
    validate_rows,
)
from app.models.part_imports import (
    PartImportBatch,
    PartImportFormat,
    PartImportRow,
    PartImportRowDisposition,
    PartImportState,
)
from app.models.units import UnitDefinition
from app.repositories.base import OrgScopedRepository
from app.repositories.part_repository import PartRepository
from app.schemas.parts import PartCreate
from app.services.audit import AuditRecorder
from app.services.part_service import PartService

_DEFAULT_UNIT_CODE = "each"


class PartImportBatchRepository(OrgScopedRepository[PartImportBatch]):
    model = PartImportBatch


class PartImportRowRepository(OrgScopedRepository[PartImportRow]):
    model = PartImportRow

    def all_for_batch_ordered(self, batch_id: uuid.UUID) -> list[PartImportRow]:
        """Unbounded — `commit()` needs every row of a batch (up to
        `app.importing.part_import_parser.MAX_ROWS`), not a paginated page.
        `OrgScopedRepository.list_page` caps at 200, which is correct for
        the paginated `GET` endpoint (`page_after` below) but wrong here."""
        stmt = (
            self._base_query()
            .where(PartImportRow.batch_id == batch_id)
            .order_by(PartImportRow.row_number)
        )
        return list(self.session.execute(stmt).scalars().all())

    def page_after(
        self, batch_id: uuid.UUID, *, after_row_number: int, limit: int
    ) -> list[PartImportRow]:
        """Keyset page: rows with `row_number > after_row_number`, one extra
        row fetched so the caller can detect `has_more` without a second
        count query."""
        stmt = (
            self._base_query()
            .where(
                PartImportRow.batch_id == batch_id,
                PartImportRow.row_number > after_row_number,
            )
            .order_by(PartImportRow.row_number)
            .limit(limit + 1)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_with_disposition(self, batch_id: uuid.UUID, disposition: str) -> int:
        return self.count(
            PartImportRow.batch_id == batch_id, PartImportRow.disposition == disposition
        )

    def errored_rows(self, batch_id: uuid.UUID, *, limit: int) -> list[PartImportRow]:
        stmt = (
            self._base_query()
            .where(
                PartImportRow.batch_id == batch_id,
                PartImportRow.disposition == PartImportRowDisposition.ERROR.value,
            )
            .order_by(PartImportRow.row_number)
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())


@dataclass(frozen=True, slots=True)
class PartImportCommitResult:
    created: int
    updated: int
    skipped: int


@dataclass(frozen=True, slots=True)
class RowErrorEntry:
    row_number: int
    field: str | None
    issue: str


def _encode_cursor(row_number: int) -> str:
    return base64.urlsafe_b64encode(str(row_number).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:
        raise ValidationAppError("Invalid or corrupt pagination cursor.") from exc


class PartImportService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._batch_repo = PartImportBatchRepository(session, organization_id)
        self._row_repo = PartImportRowRepository(session, organization_id)
        self._part_repo = PartRepository(session, organization_id)
        self._part_service = PartService(session, organization_id, audit, clock, ids)

    # -- unit_code resolution ------------------------------------------------

    def _resolve_unit_code(self, unit_code: str | None) -> tuple[uuid.UUID | None, str | None]:
        """`unit_code` -> `unit_definition_id`, org-own catalogue preferred
        over the global one when both define the same code. A blank/absent
        `unit_code` defaults to `"each"` (`app.seed.units_catalog.
        STANDARD_UNIT_CATALOG`'s canonical count-dimension unit, factor 1 by
        definition) — see `app.importing.part_import_parser` module
        docstring for why `unit_code` is an optional import column despite
        `Part.unit_definition_id` being `NOT NULL`.

        Returns `(unit_definition_id, None)` on success, `(None, issue)` on
        failure — never both/neither.
        """
        code = (unit_code or "").strip() or _DEFAULT_UNIT_CODE
        stmt = (
            select(UnitDefinition)
            .where(
                func.lower(UnitDefinition.code) == code.lower(),
                or_(
                    UnitDefinition.organization_id == self._organization_id,
                    UnitDefinition.organization_id.is_(None),
                ),
            )
            # False (org-owned row) sorts before True (global row IS NULL):
            # an org's own unit shadows a same-named global catalogue entry.
            .order_by(UnitDefinition.organization_id.is_(None))
        )
        unit = self._db.execute(stmt).scalars().first()
        if unit is None:
            return None, f"unknown unit_code {code!r}"
        return unit.id, None

    # -- preview --------------------------------------------------------------

    def preview(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        fmt: PartImportFormat,
        actor_id: uuid.UUID,
    ) -> PartImportBatch:
        try:
            raw_file = (
                parse_csv(file_bytes) if fmt is PartImportFormat.CSV else parse_xlsx(file_bytes)
            )
        except PartImportParseError as exc:
            raise ValidationAppError(str(exc)) from exc

        validated_rows = validate_rows(raw_file.rows)

        now = self._clock.now()
        batch = PartImportBatch(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            source_filename=filename,
            file_sha256=hashlib.sha256(file_bytes).digest(),
            format=fmt,
            state=PartImportState.PREVIEWING,
            rows_total=len(validated_rows),
            rows_valid=0,
            rows_invalid=0,
            rows_duplicate=0,
            created_by_id=actor_id,
            created_at=now,
        )
        self._batch_repo.add(batch)
        self._db.flush()  # batch.id must exist in the DB before rows FK to it

        rows_valid = rows_invalid = rows_duplicate = 0
        for vrow in validated_rows:
            disposition, errors = self._classify_row(vrow)
            if disposition is PartImportRowDisposition.CREATE:
                rows_valid += 1
            elif disposition is PartImportRowDisposition.SKIP_DUPLICATE:
                rows_duplicate += 1
            else:
                rows_invalid += 1
            row = PartImportRow(
                id=self._ids.new_id(),
                organization_id=self._organization_id,
                batch_id=batch.id,
                row_number=vrow.row_number,
                raw_values=vrow.raw,
                normalized_values=(vrow.parsed.to_json() if vrow.parsed is not None else None),
                disposition=disposition.value,
                errors=[e.to_dict() for e in errors],
                resulting_part_id=None,
            )
            self._row_repo.add(row)

        batch.rows_valid = rows_valid
        batch.rows_invalid = rows_invalid
        batch.rows_duplicate = rows_duplicate
        self._db.flush()

        self._audit.record(
            "part_import.previewed",
            entity_type="part_import_batch",
            entity_id=batch.id,
            after_state={
                "source_filename": batch.source_filename,
                "format": batch.format.value,
                "rows_total": batch.rows_total,
                "rows_valid": batch.rows_valid,
                "rows_invalid": batch.rows_invalid,
                "rows_duplicate": batch.rows_duplicate,
            },
            explanation=(
                f"Import preview '{batch.source_filename}': {batch.rows_total} row(s), "
                f"{batch.rows_valid} valid, {batch.rows_invalid} invalid, "
                f"{batch.rows_duplicate} duplicate"
            ),
        )
        return batch

    def _classify_row(
        self, vrow: ValidatedRow
    ) -> tuple[PartImportRowDisposition, list[RowError]]:
        """DB-dependent second pass over one already field-validated row:
        in-file duplicates are already known (`app.importing.
        part_import_parser.validate_rows`); this adds the existing-active-
        part duplicate check and `unit_code` resolution to decide the final
        `disposition`."""
        if vrow.errors:
            return PartImportRowDisposition.ERROR, list(vrow.errors)
        assert vrow.parsed is not None  # parser invariant: no errors => parsed is set

        if vrow.is_duplicate_in_file:
            return PartImportRowDisposition.SKIP_DUPLICATE, []

        existing = self._part_repo.get_by_internal_part_number(vrow.parsed.internal_part_number)
        if existing is not None:
            return PartImportRowDisposition.SKIP_DUPLICATE, []

        _unit_id, unit_error = self._resolve_unit_code(vrow.parsed.unit_code)
        if unit_error is not None:
            return PartImportRowDisposition.ERROR, [RowError(field="unit_code", issue=unit_error)]

        return PartImportRowDisposition.CREATE, []

    # -- read ------------------------------------------------------------------

    def get(self, batch_id: uuid.UUID) -> PartImportBatch:
        return self._batch_repo.get_or_raise(batch_id)

    def list_rows(
        self, batch_id: uuid.UUID, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[PartImportRow], str | None, bool]:
        """Keyset pagination by `row_number` (§1.3 "cursor... keyset
        pagination, stable under insert"). Returns `(rows, next_cursor,
        has_more)`."""
        self._batch_repo.get_or_raise(batch_id)  # 404 for absent/cross-org, before touching rows
        after_row_number = _decode_cursor(cursor) if cursor else 0
        fetched = self._row_repo.page_after(
            batch_id, after_row_number=after_row_number, limit=limit
        )
        has_more = len(fetched) > limit
        page = fetched[:limit]
        next_cursor = _encode_cursor(page[-1].row_number) if has_more and page else None
        return page, next_cursor, has_more

    def list_errors(self, batch_id: uuid.UUID, *, limit: int = 100) -> list[RowErrorEntry]:
        self._batch_repo.get_or_raise(batch_id)
        out: list[RowErrorEntry] = []
        for row in self._row_repo.errored_rows(batch_id, limit=limit):
            for err in row.errors:
                if len(out) >= limit:
                    return out
                out.append(
                    RowErrorEntry(
                        row_number=row.row_number,
                        field=err.get("field"),
                        issue=err.get("issue", "invalid"),
                    )
                )
        return out

    # -- commit ------------------------------------------------------------------

    def commit(self, batch_id: uuid.UUID) -> PartImportCommitResult:
        """All-or-nothing (§4.5). Two distinct failure shapes, both `409`:

        1. The batch is not `previewing` (already committed/cancelled) —
           `commit` is idempotent-guarded, this task's brief.
        2. The batch contains any `error` row. The *whole* batch is refused
           here, not just that row: `create` rows in the same batch are not
           created either. This reads `SPEC §3`'s "transactional import...
           rollback after failure" as applying to the decision to commit at
           all, not only to a failure encountered mid-commit — there is no
           edit-a-row endpoint in the contract, so once a batch contains an
           invalid row the only paths forward are fixing the source file and
           re-uploading, or `cancel`.

        Beyond that gate, any exception raised while creating parts (a
        duplicate that appeared since preview, a `unit_code` that stopped
        resolving) is left to propagate: `app.api.deps.get_db` rolls back
        the whole request's transaction on any raised exception, which is
        exactly "nothing persists" for every row processed so far in this
        same call, not just the one that failed.
        """
        batch = self._batch_repo.get_or_raise(batch_id)
        if batch.state != PartImportState.PREVIEWING:
            raise ConflictStateError(
                "This import batch has already been committed or cancelled."
            )

        rows = self._row_repo.all_for_batch_ordered(batch_id)

        invalid_rows = [r for r in rows if r.disposition == PartImportRowDisposition.ERROR.value]
        if invalid_rows:
            first = invalid_rows[0]
            first_issue = first.errors[0].get("issue", "invalid") if first.errors else "invalid"
            raise ConflictStateError(
                f"Batch has {len(invalid_rows)} invalid row(s) and cannot be committed; fix "
                f"the source file and re-upload, or cancel this batch. First failing row: "
                f"{first.row_number} ({first_issue}).",
                details=[
                    ErrorDetail(
                        field=f"row_{first.row_number}", issue=e.get("issue", "invalid")
                    )
                    for e in (first.errors or [])[:10]
                ],
            )

        created_count = 0
        skipped_count = 0

        for row in rows:
            if row.disposition == PartImportRowDisposition.SKIP_DUPLICATE.value:
                skipped_count += 1
                continue

            parsed = row.normalized_values or {}
            unit_id, unit_error = self._resolve_unit_code(parsed.get("unit_code"))
            if unit_id is None:
                raise ConflictStateError(
                    f"Row {row.row_number}: {unit_error} (it resolved during preview but no "
                    "longer does)."
                )

            part = self._part_service.create(
                PartCreate(
                    internal_part_number=parsed["internal_part_number"],
                    manufacturer_part_number=parsed.get("manufacturer_part_number"),
                    name=parsed["name"],
                    description=parsed.get("description"),
                    category=parsed.get("category"),
                    unit_definition_id=unit_id,
                    target_price=parsed.get("target_price"),
                    target_price_currency=parsed.get("target_price_currency"),
                    is_active=True,
                )
            )
            row.resulting_part_id = part.id
            created_count += 1

        batch.state = PartImportState.COMMITTED
        batch.committed_at = self._clock.now()
        self._db.flush()

        self._audit.record(
            "part_import.committed",
            entity_type="part_import_batch",
            entity_id=batch.id,
            after_state={"created": created_count, "updated": 0, "skipped": skipped_count},
            explanation=(
                f"Import batch '{batch.source_filename}' committed: {created_count} part(s) "
                f"created, {skipped_count} skipped"
            ),
        )
        return PartImportCommitResult(created=created_count, updated=0, skipped=skipped_count)

    # -- cancel ------------------------------------------------------------------

    def cancel(self, batch_id: uuid.UUID) -> PartImportBatch:
        batch = self._batch_repo.get_or_raise(batch_id)
        if batch.state != PartImportState.PREVIEWING:
            raise ConflictStateError("This import batch has already been committed or cancelled.")

        batch.state = PartImportState.ROLLED_BACK
        self._db.flush()

        self._audit.record(
            "part_import.cancelled",
            entity_type="part_import_batch",
            entity_id=batch.id,
            explanation=f"Import batch '{batch.source_filename}' cancelled before commit",
        )
        return batch


__all__ = [
    "PartImportBatchRepository",
    "PartImportCommitResult",
    "PartImportRowRepository",
    "PartImportService",
    "RowErrorEntry",
]
