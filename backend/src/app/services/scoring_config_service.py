"""Scoring-configuration service - CRUD + validation for
`scoring_configurations` (docs/planning/03-api-contract.md §4.14,
app/domain/scoring/contracts.py, app/models/analysis.py).

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. Every mutation
writes exactly one audit event in the same transaction as the data change it
describes (`scoring_configuration.created`/`.updated`/`.archived`).

**Weight validation** (the spec): every `weights[]` entry must name
a known `Criterion` (`app.domain.scoring.contracts.Criterion`), its `weight`
must be a fraction in `[0, 1]`, and its `direction` must be a valid
`Direction`. For every criterion except `USER_DEFINED`, `direction` must
match that criterion's canonical direction (`DEFAULT_DIRECTIONS`) - a
`total_landed_cost` weight tagged `higher_is_better` is not a "custom
opinion", it is a data-entry error the domain layer has no way to catch
later (`VendorScorer` trusts `CriterionSpec.direction` as given).
`USER_DEFINED` entries require a non-blank `label` (the domain's own
`CriterionSpec.label` docstring: "required for USER_DEFINED"). A weight sum
that is not exactly `1` is **not** rejected (02-erd.md §8: "weights are
stored raw... normalized at calculation time" - the same policy the
brief describes as "warn-note"); instead a human-readable note is returned
alongside the created/updated row so the UI can surface it without the
write being blocked.

**Sample-configuration seeding.** `ensure_sample_configuration` inserts
`SAMPLE_WEIGHTS` (app/domain/scoring/contracts.py) under the fixed name
"Sample weights (demonstration)", `is_sample=True`, if no non-archived row
with that name already exists for the org - idempotent by construction (a
second call is a no-op, verified by the org-scoped uniqueness index rather
than a pre-check race). `list()` calls this before returning rows, using the
requesting principal as `created_by_id`, so the contract's "`GET
/scoring-configurations` ... includes the seeded sample config" (§4.14) is
true on every org's very first read - there is no separate seed script in
this phase to have run it already.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import (
    ConflictDuplicateError,
    ConflictVersionError,
    ErrorDetail,
    ValidationAppError,
)
from app.core.ids import IdGenerator
from app.domain.scoring.contracts import (
    DEFAULT_DIRECTIONS,
    SAMPLE_WEIGHTS,
    Criterion,
    CriterionSpec,
    Direction,
)
from app.models.analysis import ScoringConfiguration
from app.repositories.base import OrgScopedRepository
from app.services.audit import AuditRecorder

SAMPLE_CONFIGURATION_NAME = "Sample weights (demonstration)"


@dataclass(frozen=True, slots=True)
class CriterionSpecInput:
    criterion: str
    weight: Decimal
    direction: str
    label: str | None = None


# Module-level aliases, not inline `list[...]` annotations: ScoringConfigService
# defines a method literally named `list`, which shadows the builtin inside
# every other annotation in the same class body once `from __future__ import
# annotations` defers evaluation - the same precaution rfq_service.py/
# bom_service.py already take (see their own module-level `_RfqLineList`-
# style aliases) for exactly this reason.
_CriterionSpecInputList = list[CriterionSpecInput]
_CriterionSpecList = list[CriterionSpec]
_NoteList = list[str]
_ScoringConfigurationList = list[ScoringConfiguration]


class _ScoringConfigRepository(OrgScopedRepository[ScoringConfiguration]):
    model = ScoringConfiguration

    def get_active_by_name(self, name: str) -> ScoringConfiguration | None:
        stmt = self._base_query().where(
            func.lower(ScoringConfiguration.name) == name.strip().lower(),
            ScoringConfiguration.archived_at.is_(None),
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_all(self, *, include_archived: bool) -> list[ScoringConfiguration]:
        stmt = self._base_query()
        if not include_archived:
            stmt = stmt.where(ScoringConfiguration.archived_at.is_(None))
        stmt = stmt.order_by(ScoringConfiguration.created_at.desc())
        return list(self.session.execute(stmt).scalars().all())


def _spec_to_json(spec: CriterionSpec) -> dict[str, Any]:
    return {
        "criterion": spec.criterion.value,
        "weight": str(spec.weight),
        "direction": spec.direction.value,
        "label": spec.label,
        "is_sample_weight": spec.is_sample_weight,
    }


def _weight_sum(weights: list[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for w in weights:
        total += Decimal(str(w["weight"]))
    return total


def _config_state(row: ScoringConfiguration) -> dict[str, Any]:
    return {
        "name": row.name,
        "weights": row.weights,
        "is_sample": row.is_sample,
        "version": row.version,
        "is_archived": row.is_archived,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        "archive_reason": row.archive_reason,
    }


class ScoringConfigService:
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
        self._repo = _ScoringConfigRepository(session, organization_id)

    # -- validation ------------------------------------------------------

    def _validate_weights(
        self, weights: _CriterionSpecInputList
    ) -> tuple[_CriterionSpecList, _NoteList]:
        if not weights:
            raise ValidationAppError(
                "weights must contain at least one criterion.",
                details=[ErrorDetail(field="weights", issue="must not be empty")],
            )

        specs: _CriterionSpecList = []
        for i, w in enumerate(weights):
            try:
                criterion = Criterion(w.criterion)
            except ValueError as exc:
                raise ValidationAppError(
                    "Unknown scoring criterion.",
                    details=[
                        ErrorDetail(
                            field=f"weights[{i}].criterion",
                            issue=f"unknown criterion '{w.criterion}'",
                        )
                    ],
                ) from exc

            if not (Decimal("0") <= w.weight <= Decimal("1")):
                raise ValidationAppError(
                    "weight must be a fraction between 0 and 1.",
                    details=[
                        ErrorDetail(
                            field=f"weights[{i}].weight",
                            issue="must be between 0 and 1 inclusive",
                            value_hint=str(w.weight),
                        )
                    ],
                )

            try:
                direction = Direction(w.direction)
            except ValueError as exc:
                raise ValidationAppError(
                    "Invalid scoring direction.",
                    details=[
                        ErrorDetail(
                            field=f"weights[{i}].direction",
                            issue=f"unknown direction '{w.direction}'",
                        )
                    ],
                ) from exc

            if criterion is Criterion.USER_DEFINED:
                if not w.label or not w.label.strip():
                    raise ValidationAppError(
                        "label is required for a user_defined criterion.",
                        details=[
                            ErrorDetail(
                                field=f"weights[{i}].label",
                                issue="required for user_defined criterion",
                            )
                        ],
                    )
            else:
                expected = DEFAULT_DIRECTIONS[criterion]
                if direction is not expected:
                    raise ValidationAppError(
                        f"direction for '{criterion.value}' must be '{expected.value}'.",
                        details=[
                            ErrorDetail(
                                field=f"weights[{i}].direction",
                                issue=f"must be '{expected.value}' for this criterion",
                            )
                        ],
                    )

            specs.append(
                CriterionSpec(
                    criterion=criterion,
                    weight=w.weight,
                    direction=direction,
                    label=w.label or criterion.value,
                    is_sample_weight=False,
                )
            )

        notes: _NoteList = []
        total = sum((s.weight for s in specs), Decimal("0"))
        if total != Decimal("1"):
            notes.append(
                f"weights sum to {total}, not 1 - stored as entered; renormalization (if "
                "any) happens at scoring time, per calculation policy."
            )
        return specs, notes

    # -- reads -----------------------------------------------------------

    def list(
        self, actor_id: uuid.UUID, *, include_archived: bool = False
    ) -> _ScoringConfigurationList:
        self.ensure_sample_configuration(actor_id)
        return self._repo.list_all(include_archived=include_archived)

    def get(self, config_id: uuid.UUID) -> ScoringConfiguration:
        return self._repo.get_or_raise(config_id)

    # -- writes ----------------------------------------------------------

    def create(
        self, name: str, weights: _CriterionSpecInputList, *, actor_id: uuid.UUID
    ) -> tuple[ScoringConfiguration, _NoteList]:
        specs, notes = self._validate_weights(weights)

        now = self._clock.now()
        row = ScoringConfiguration(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            name=name.strip(),
            weights=[_spec_to_json(s) for s in specs],
            is_sample=False,
            created_by_id=actor_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(row)
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise ConflictDuplicateError(
                f"A scoring configuration named '{name}' already exists in this organization."
            ) from exc

        self._audit.record(
            "scoring_configuration.created",
            entity_type="scoring_configuration",
            entity_id=row.id,
            after_state=_config_state(row),
            explanation=f"Scoring configuration '{row.name}' created",
        )
        return row, notes

    def update(
        self,
        config_id: uuid.UUID,
        *,
        name: str | None,
        weights: _CriterionSpecInputList | None,
        if_match_version: int,
    ) -> tuple[ScoringConfiguration, _NoteList]:
        row = self._repo.get_or_raise(config_id)
        if row.version != if_match_version:
            raise ConflictVersionError()

        before = _config_state(row)
        notes: _NoteList = []

        if name is not None:
            row.name = name.strip()
        if weights is not None:
            specs, notes = self._validate_weights(weights)
            row.weights = [_spec_to_json(s) for s in specs]

        row.version += 1
        row.updated_at = self._clock.now()
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise ConflictDuplicateError(
                f"A scoring configuration named '{row.name}' already exists in this "
                "organization."
            ) from exc

        self._audit.record(
            "scoring_configuration.updated",
            entity_type="scoring_configuration",
            entity_id=row.id,
            before_state=before,
            after_state=_config_state(row),
            explanation=f"Scoring configuration '{row.name}' updated",
        )
        # A name-only update (weights untouched) still gets the same warn-note
        # about the *stored* weights, so the UI does not have to re-derive it
        # from the response's raw `weights[]` itself.
        if weights is None:
            existing_sum = total_or_none(row.weights)
            if existing_sum is not None and existing_sum != Decimal("1"):
                notes.append(
                    f"weights sum to {existing_sum}, not 1 - stored as entered; "
                    "renormalization (if any) happens at scoring time, per calculation policy."
                )
        return row, notes

    def archive(
        self, config_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID
    ) -> ScoringConfiguration:
        row = self._repo.get_or_raise(config_id)
        if row.is_archived:
            raise ValidationAppError("Scoring configuration is already archived.")

        before = _config_state(row)
        now = self._clock.now()
        row.archived_at = now
        row.archived_by_id = actor_id
        row.archive_reason = reason
        row.version += 1
        row.updated_at = now
        self._db.flush()

        self._audit.record(
            "scoring_configuration.archived",
            entity_type="scoring_configuration",
            entity_id=row.id,
            before_state=before,
            after_state=_config_state(row),
            explanation=f"Scoring configuration '{row.name}' archived: {reason}",
        )
        return row

    # -- seeding ---------------------------------------------------------

    def ensure_sample_configuration(self, actor_id: uuid.UUID) -> ScoringConfiguration:
        """Idempotent: a second call finds the existing active row and
        returns it unchanged rather than inserting a duplicate."""
        existing = self._repo.get_active_by_name(SAMPLE_CONFIGURATION_NAME)
        if existing is not None:
            return existing

        now = self._clock.now()
        row = ScoringConfiguration(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            name=SAMPLE_CONFIGURATION_NAME,
            weights=[_spec_to_json(s) for s in SAMPLE_WEIGHTS],
            is_sample=True,
            created_by_id=actor_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(row)
        try:
            self._db.flush()
        except IntegrityError as exc:
            # A concurrent first-ever request for this org lost the race.
            # Services never roll back the request-scoped session themselves
            # (app/api/deps.py's get_db owns that); surfacing this as the same
            # conflict-duplicate error `create()` would raise is consistent
            # with every other unique-name write in this codebase (see
            # ScoringConfigService.create, FxService.set_override) rather than
            # attempting an in-service recovery that would leave the
            # transaction in an undefined state for whatever else runs on it
            # this request.
            raise ConflictDuplicateError(
                "The sample scoring configuration was created by a concurrent request."
            ) from exc

        self._audit.record(
            "scoring_configuration.created",
            entity_type="scoring_configuration",
            entity_id=row.id,
            after_state=_config_state(row),
            explanation="Sample scoring configuration seeded",
        )
        return row


def total_or_none(weights: list[dict[str, Any]] | None) -> Decimal | None:
    if not weights:
        return None
    return _weight_sum(weights)


__all__ = ["SAMPLE_CONFIGURATION_NAME", "CriterionSpecInput", "ScoringConfigService"]
