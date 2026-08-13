"""Report generation and reads (docs/SPEC.md §Reports and exports,
docs/planning/03-api-contract.md §4.18, docs/planning/02-erd.md
GENERATED_REPORTS box + §11).

Services never commit: the request-scoped `get_db` dependency
(app/api/deps.py) commits on success and rolls back on any raised
exception. `generate` writes exactly one audit event (`report.generated`)
in the same transaction as the row it describes (SPEC §Audit trail: "report
generation" is one of the named audited actions).

**ALL figures come from stored rows, never recomputed differently.** Every
report type's builder below either (a) reuses an existing, already-tested
response builder verbatim (`app.schemas.scenarios.ScoringResultResponse`/
`AllocationResultResponse`/`ScenarioResponse`, `app.schemas.briefs.
NegotiationBriefResponse`) - the exact same shape `GET /comparison-
scenarios/{id}` or `GET /negotiation-briefs/{id}` would return - or (b)
sums already-persisted `LandedCostResult` rows the same way
`app/services/brief_service.py`'s own `_aggregate_offers` does (per-supplier
landed-cost totals across a scenario's `quote_snapshot_refs`), never a
fresh optimizer/scorer/landed-cost run. Nothing here calls the solver, the
scorer, or the landed-cost calculator.

## Deviation 1 - `POST /reports` runs synchronously, `201` not `202`

03-api-contract.md §4.18's literal table says `-> 202`. Same
`JOB_RUNNER=inline` precedent `services/extraction_service.py` establishes
and `services/scenario_service.py`/`services/brief_service.py` already
apply to their own routes: this codebase has no job queue anywhere.
`generate` runs to completion (build content, render, hash, store) in one
call, returning the finished row - `state` is `ready` or `failed`, always
visible on the `201` body, never a `job` envelope to poll.

## Deviation 2 - a renderer exception is persisted as a `failed` row, not a `500`

The delegating task's own instruction: "On renderer exception: persist the
row state=failed with error_message, still 201". `generate` therefore
resolves the scenario/RFQ/(for `negotiation_brief`) the target brief FIRST -
those lookups can raise a genuine `404`/`409`/`422` and are NOT covered by
this - then wraps only the content-build + render + storage-write step in a
`try/except Exception`. A failure there (a `KeyError` from unexpected data
shape, a reportlab layout error, a storage I/O error) becomes a `failed`
row with `error_message` set and no stored bytes, still returned as `201`:
the report is now an auditable, listable fact ("generation was attempted
and failed"), not a swallowed `500` the caller has no record of. This is a
deliberately broad `except Exception` - every other service in this
codebase lets exceptions propagate - scoped narrowly to this one step for
this one documented reason.

## Deviation 3 - `410` on a purged report has no `ErrorCode` member to reuse literally

`app/core/errors.py` is  and off the edit list;
its `AppError.status` is a fixed `ErrorCode -> int` table with no `410`
entry, and adding one is exactly the "add none" the delegating task's own
instruction forbids. `get_content` therefore raises `ReportPurgedError`
(below), a plain `Exception`, NOT an `AppError` subclass - `api/v1/
reports.py`'s `download_report_content` catches it directly and hand-builds
a `410` envelope reusing `ErrorCode.NOT_FOUND`'s wire `code` string
(`"not_found"`) as the closest existing vocabulary ("the thing you asked
for is not there anymore" is a variant of "not found", not of any conflict/
validation code), rather than inventing a new code string this change was
told not to add.

A report whose `state` is `pending`/`failed` (never produced bytes, and was
never purged) is a plain `NotFoundError` (`404`) instead - genuinely
distinct from "was ready, now gone".

## Deviation 4 - `expires_at` retention duration is an undocumented assumption

Neither `docs/SPEC.md` nor `docs/planning/02-erd.md` states how long a
report should live before it becomes eligible for the §11 purge job (out of
the scope - no purge route is implemented; §11's "row kept, blob
purged, storage_key nulled with purged_at set" shape is exercised only by
tests writing `purged_at` directly, mirroring how DB-level test setup is
already done elsewhere in this codebase, e.g. `test_briefs_api.py`'s direct
`audit_events` inserts). `REPORT_RETENTION_DAYS = 90` below is a flat,
documented guess, not a literal requirement.

## `negotiation_brief` report: brief resolution

`parameters.brief_id` (optional): a specific `NegotiationBrief` to render;
`404 not_found` if it does not exist, is another organization's, or does
not belong to the given `scenario_id`. Omitted: the latest non-archived
brief for the scenario; `404 not_found` if none exists - resolved BEFORE
the try/except in Deviation 2 above, since "no brief exists to render" is a
precondition failure, not a rendering failure, and must not be silently
turned into a stored `failed` report row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Final

from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import AppError, ConflictStateError, NotFoundError, ValidationAppError
from app.core.ids import IdGenerator
from app.core.money import CALC_CONTEXT, quantize_money, quantize_unit_price, to_wire
from app.domain.landed_cost.breaks import select_price_break
from app.domain.landed_cost.contracts import PriceBreakTier
from app.models.audit import AuditEvent
from app.models.briefs import NegotiationBrief
from app.models.reports import GeneratedReport, ReportFormat, ReportState, ReportType
from app.models.scenarios import AllocationResultRecord, ComparisonScenario, ScenarioResult
from app.providers.storage.base import StorageProvider
from app.reports import (
    KeyValueBlock,
    ReportDocument,
    TableBlock,
    TextBlock,
    content_type_for,
    render,
)
from app.repositories.base import OrgIsolationViolation, OrgScopedRepository
from app.repositories.matching_repository import QuoteLineRepository
from app.repositories.quote_repository import QuoteRepository
from app.repositories.rfq_repository import RfqRepository
from app.schemas.briefs import NegotiationBriefResponse
from app.schemas.scenarios import (
    AllocationResultResponse,
    ScenarioResponse,
    ScoringResultResponse,
)
from app.services.audit import AuditRecorder
from app.services.audit_read_service import AuditReadService
from app.services.landed_cost_service import LandedCostService

logger = logging.getLogger(__name__)

REPORT_RETENTION_DAYS: Final[int] = 90

_METHODOLOGY: Final[str] = (
    "Figures in this report are read directly from previously stored "
    "calculation results (landed-cost calculations, scoring runs, "
    "optimizer allocations, or negotiation-brief sections) - nothing is "
    "recomputed for this export. See 'Calculation version' above for the "
    "landed-cost/scoring calculation version that produced these figures."
)
_DISCLAIMER: Final[str] = (
    "This report is generated from confirmed supplier data, user-entered "
    "assumptions, and deterministic calculations only. It does not contain "
    "invented market benchmarks, competitor quotes, historical prices, or "
    "supplier behavior predictions. Any AI-narrative content is clearly "
    "labelled and requires human review before use."
)


class ReportPurgedError(Exception):
    """Raised by `get_content` when `report.purged_at` is set - see module
    docstring "Deviation 3" for why this is not an `app.core.errors.AppError`
    subclass and how the API layer turns it into a `410`."""

    def __init__(self, report_id: uuid.UUID) -> None:
        super().__init__(f"Report {report_id} has been purged.")
        self.report_id = report_id


@dataclass(frozen=True, slots=True)
class _SupplierTotals:
    """Per-supplier landed-cost aggregation over one scenario's offers - the
    same sum-then-divide `LandedCostResult` aggregation
    `app/services/brief_service.py`'s `_aggregate_offers` performs, trimmed
    to only what `supplier_comparison` needs (module docstring)."""

    total_landed_cost: Decimal | None
    effective_unit_cost: Decimal | None
    quoted_unit_price: Decimal | None
    currency: str | None


class ReportService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
        *,
        storage: StorageProvider,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._storage = storage

        self._scenario_repo = _ScenarioLookupRepository(session, organization_id)
        self._result_repo = _ScenarioResultLookupRepository(session, organization_id)
        self._allocation_repo = _AllocationResultLookupRepository(session, organization_id)
        self._rfq_repo = RfqRepository(session, organization_id)
        self._quote_repo = QuoteRepository(session, organization_id)
        self._quote_line_repo = QuoteLineRepository(session, organization_id)
        self._brief_repo = _BriefLookupRepository(session, organization_id)
        self._landed_cost = LandedCostService(session, organization_id, audit, clock, ids)
        self._audit_read = AuditReadService(session, organization_id)
        self._repo = _ReportRepository(session, organization_id)

    # -- generate -------------------------------------------------------

    def generate(
        self,
        *,
        scenario_id: uuid.UUID,
        report_type: ReportType,
        report_format: ReportFormat,
        parameters: dict[str, Any] | None,
        actor_id: uuid.UUID,
    ) -> GeneratedReport:
        scenario = self._scenario_repo.get_or_raise(scenario_id)
        result = self._result_repo.get_by_scenario(scenario.id)
        allocation = self._allocation_repo.get_by_scenario(scenario.id)
        if result is None or allocation is None:
            raise ConflictStateError(
                "The scenario must be complete (scored and allocated) before a report "
                "can be generated."
            )
        rfq = self._rfq_repo.get_or_raise(scenario.rfq_id)
        parameters = dict(parameters or {})

        brief: NegotiationBrief | None = None
        if report_type is ReportType.NEGOTIATION_BRIEF:
            brief = self._resolve_brief(scenario.id, parameters)

        now = self._clock.now()
        report_id = self._ids.new_id()
        expires_at = now + timedelta(days=REPORT_RETENTION_DAYS)

        try:
            document = self._build_document(
                report_type,
                scenario=scenario,
                rfq_name=rfq.name,
                rfq_base_currency=rfq.base_currency,
                result=result,
                allocation=allocation,
                brief=brief,
                generated_at=now,
            )
            data = render(document, report_format)
            content_sha256 = hashlib.sha256(data).digest()
            # 32 lowercase hex chars + extension, the server-generated shape
            # app.providers.storage.base.KEY_RE requires.
            storage_key = f"{uuid.uuid4().hex}.{report_format.value}"
            self._storage.put(
                organization_id=self._organization_id,
                key=storage_key,
                data=data,
                content_type=content_type_for(report_format),
            )
            report = GeneratedReport(
                id=report_id,
                organization_id=self._organization_id,
                scenario_id=scenario.id,
                report_type=report_type,
                format=report_format,
                storage_key=storage_key,
                content_sha256=content_sha256,
                size_bytes=len(data),
                parameters=parameters,
                calculation_version=scenario.calculation_version,
                state=ReportState.READY,
                generated_by_id=actor_id,
                generated_at=now,
                expires_at=expires_at,
                purged_at=None,
                error_message=None,
            )
        except (AppError, OrgIsolationViolation):
            # Never swallow domain/security failures into a `failed` row:
            # AppError is a deliberate 4xx, and OrgIsolationViolation is
            # designed to fail loudly (2026-08 security audit, MEDIUM-6).
            raise
        except Exception as exc:
            # The wire-visible message must not carry SQL fragments, paths,
            # or stack detail (SECURITY.md §10) - the full exception goes to
            # the server log only, keyed by the report id.
            logger.exception("report generation failed (report_id=%s)", report_id)
            report = GeneratedReport(
                id=report_id,
                organization_id=self._organization_id,
                scenario_id=scenario.id,
                report_type=report_type,
                format=report_format,
                storage_key=None,
                content_sha256=None,
                size_bytes=0,
                parameters=parameters,
                calculation_version=scenario.calculation_version,
                state=ReportState.FAILED,
                generated_by_id=actor_id,
                generated_at=now,
                expires_at=expires_at,
                purged_at=None,
                error_message=(
                    f"Report generation failed ({type(exc).__name__}). "
                    f"Details are in the server log under report id {report_id}."
                ),
            )

        self._repo.add(report)
        self._db.flush()

        self._audit.record(
            "report.generated",
            entity_type="generated_report",
            entity_id=report.id,
            after_state=_report_audit_state(report),
            explanation=(
                f"Report generated: {report_type.value} ({report_format.value}) for "
                f"scenario {scenario.id} (state={report.state.value})"
            ),
        )
        return report

    def _resolve_brief(
        self, scenario_id: uuid.UUID, parameters: dict[str, Any]
    ) -> NegotiationBrief:
        raw_brief_id = parameters.get("brief_id")
        if raw_brief_id is not None:
            try:
                brief_id = uuid.UUID(str(raw_brief_id))
            except ValueError as exc:
                raise ValidationAppError("parameters.brief_id must be a valid UUID.") from exc
            specific_brief = self._brief_repo.get_or_raise(brief_id)
            if specific_brief.scenario_id != scenario_id:
                raise ValidationAppError(
                    "parameters.brief_id does not belong to the given scenario_id."
                )
            return specific_brief
        latest_brief = self._brief_repo.get_latest_for_scenario(scenario_id)
        if latest_brief is None:
            raise NotFoundError("NegotiationBrief")
        return latest_brief

    # -- reads -------------------------------------------------------------

    def get(self, report_id: uuid.UUID) -> GeneratedReport:
        return self._repo.get_or_raise(report_id)

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[list[GeneratedReport], int]:
        items = self._repo.list_page(
            order_by=GeneratedReport.generated_at.desc(), limit=limit, offset=offset
        )
        total = self._repo.count()
        return items, total

    def get_content(self, report_id: uuid.UUID) -> tuple[bytes, str, str]:
        """Returns `(data, content_type, filename)`. Raises `ReportPurgedError`
        (module docstring "Deviation 3") when purged, `NotFoundError` when
        the report never produced bytes at all (`pending`/`failed`)."""
        report = self._repo.get_or_raise(report_id)
        if report.purged_at is not None:
            raise ReportPurgedError(report_id)
        if report.state is not ReportState.READY or report.storage_key is None:
            raise NotFoundError("Report content")
        data = self._storage.get(organization_id=self._organization_id, key=report.storage_key)
        return data, content_type_for(report.format), _safe_filename(report)

    # -- document builders ---------------------------------------------

    def _build_document(
        self,
        report_type: ReportType,
        *,
        scenario: ComparisonScenario,
        rfq_name: str,
        rfq_base_currency: str,
        result: ScenarioResult,
        allocation: AllocationResultRecord,
        brief: NegotiationBrief | None,
        generated_at: datetime,
    ) -> ReportDocument:
        if report_type is ReportType.SUPPLIER_COMPARISON:
            return self._build_supplier_comparison(
                scenario, rfq_name, rfq_base_currency, result, generated_at
            )
        if report_type is ReportType.CFO_RECOMMENDATION:
            return self._build_cfo_recommendation(
                scenario, rfq_name, rfq_base_currency, allocation, generated_at
            )
        if report_type is ReportType.NEGOTIATION_BRIEF:
            if brief is None:  # pragma: no cover - resolved before this is ever called
                raise NotFoundError("NegotiationBrief")
            return self._build_negotiation_brief(scenario, brief, generated_at)
        if report_type is ReportType.SCENARIO_SUMMARY:
            return self._build_scenario_summary(
                scenario, rfq_name, rfq_base_currency, result, allocation, generated_at
            )
        if report_type is ReportType.AUDIT_HISTORY:
            return self._build_audit_history(scenario, generated_at)
        raise ValidationAppError(  # pragma: no cover - exhaustive enum
            f"Unsupported report_type: {report_type!r}"
        )

    def _supplier_totals(
        self, scenario: ComparisonScenario, supplier_id: uuid.UUID
    ) -> _SupplierTotals:
        refs = [
            r for r in scenario.quote_snapshot_refs if r["supplier_id"] == str(supplier_id)
        ]
        if not refs:
            return _SupplierTotals(None, None, None, None)

        total_landed = Decimal("0")
        total_qty = Decimal("0")
        currency: str | None = None
        quoted_unit_price: Decimal | None = None

        for i, ref in enumerate(refs):
            row, _components = self._landed_cost.get(uuid.UUID(ref["landed_cost_result_id"]))
            with localcontext(CALC_CONTEXT):
                total_landed = total_landed + row.total_landed_cost
                total_qty = total_qty + row.accepted_quantity
            currency = row.currency
            if i == 0:
                line = self._quote_line_repo.get_or_raise(uuid.UUID(ref["quote_line_id"]))
                if line.unit_price is not None:
                    quoted_unit_price = line.unit_price
                else:
                    price_breaks = self._quote_repo.list_price_breaks(line.id)
                    if price_breaks:
                        tiers = tuple(
                            PriceBreakTier(
                                min_quantity=pb.min_quantity,
                                max_quantity=pb.max_quantity,
                                unit_price=pb.unit_price,
                            )
                            for pb in price_breaks
                        )
                        selection = select_price_break(tiers, row.accepted_quantity)
                        if selection.tier is not None:
                            quoted_unit_price = selection.tier.unit_price

        if not total_qty:
            return _SupplierTotals(None, None, quoted_unit_price, currency)
        with localcontext(CALC_CONTEXT):
            effective_unit_cost = quantize_unit_price(total_landed / total_qty)
        return _SupplierTotals(
            total_landed_cost=quantize_money(total_landed),
            effective_unit_cost=effective_unit_cost,
            quoted_unit_price=quoted_unit_price,
            currency=currency,
        )

    def _build_supplier_comparison(
        self,
        scenario: ComparisonScenario,
        rfq_name: str,
        rfq_base_currency: str,
        result: ScenarioResult,
        generated_at: datetime,
    ) -> ReportDocument:
        scoring = ScoringResultResponse.from_model(result)
        rows: list[tuple[str, ...]] = []
        missing: list[str] = []
        for score in sorted(scoring.scores, key=lambda s: s.rank):
            totals = self._supplier_totals(scenario, uuid.UUID(score.supplier_id))
            rows.append(
                (
                    str(score.rank),
                    score.supplier_name,
                    to_wire(totals.quoted_unit_price)
                    if totals.quoted_unit_price is not None
                    else "not stated",
                    to_wire(totals.effective_unit_cost)
                    if totals.effective_unit_cost is not None
                    else "not available",
                    to_wire(totals.total_landed_cost)
                    if totals.total_landed_cost is not None
                    else "not available",
                    totals.currency or rfq_base_currency,
                    score.total_score,
                    "excluded" if score.excluded else "included",
                )
            )
            if score.missing_criteria:
                missing.append(
                    f"{score.supplier_name}: missing {', '.join(score.missing_criteria)}"
                )
            if totals.quoted_unit_price is None:
                missing.append(f"{score.supplier_name}: quoted unit price not stated")

        table = TableBlock(
            heading="Supplier comparison",
            columns=(
                "Rank",
                "Supplier",
                "Quoted unit price",
                "Effective unit cost",
                "Total landed cost",
                "Currency",
                "Score",
                "Status",
            ),
            rows=tuple(rows),
        )
        return ReportDocument(
            title=f"Supplier Comparison - {rfq_name} / {scenario.name}",
            generated_at=generated_at.isoformat(),
            calculation_version=scenario.calculation_version,
            methodology=_METHODOLOGY,
            disclaimer=_DISCLAIMER,
            missing_data=tuple(missing),
            blocks=(_assumptions_block(scenario), table),
        )

    def _build_cfo_recommendation(
        self,
        scenario: ComparisonScenario,
        rfq_name: str,
        rfq_base_currency: str,
        allocation: AllocationResultRecord,
        generated_at: datetime,
    ) -> ReportDocument:
        resp = AllocationResultResponse.from_model(allocation, base_currency=rfq_base_currency)

        summary_pairs: list[tuple[str, str]] = [
            ("Solver status", resp.solver_status),
            ("Status explanation", resp.status_explanation),
            (
                "Expected total cost",
                f"{resp.objective_total_cost.amount} {resp.objective_total_cost.currency}"
                if resp.objective_total_cost is not None
                else "not available",
            ),
            ("Supplier count", str(allocation.supplier_count)),
            ("Optimization version", resp.optimization_version),
        ]
        if (
            resp.solver_status == "optimal"
            and resp.stats is not None
            and resp.stats.max_scaling_error is not None
        ):
            # 06-optimization-methodology.md §7.4 / §10.8: the caveat must
            # appear in the exported report, not only in the docs.
            summary_pairs.append(
                (
                    "Optimality caveat",
                    "Optimality is proven with respect to costs rounded to 0.0001 "
                    "currency units; any unconsidered alternative is at most "
                    f"{resp.stats.max_scaling_error} {rfq_base_currency} cheaper.",
                )
            )
        if resp.error_message:
            summary_pairs.append(("Error", resp.error_message))

        blocks: list[Any] = [
            KeyValueBlock(heading="Allocation summary", pairs=tuple(summary_pairs))
        ]

        allocation_rows = tuple(
            (a.supplier_label, str(a.quantity), a.price_break.unit_price, a.line_landed_cost)
            for a in resp.allocations
        )
        blocks.append(
            TableBlock(
                heading="Allocation",
                columns=("Supplier", "Quantity", "Unit landed cost", "Line landed cost"),
                rows=allocation_rows,
            )
        )
        if resp.binding_constraints:
            blocks.append(
                TextBlock(
                    heading="Binding constraints",
                    paragraphs=tuple(f"{b.name}: {b.detail}" for b in resp.binding_constraints),
                )
            )
        if resp.infeasibility_explanation is not None:
            ie = resp.infeasibility_explanation
            paragraphs = [ie.narrative]
            if ie.conflicting_constraint_groups:
                paragraphs.append(
                    "Conflicting constraint groups: "
                    + ", ".join(ie.conflicting_constraint_groups)
                )
            if ie.minimal_relaxation:
                paragraphs.append(f"Minimal relaxation: {ie.minimal_relaxation}")
            blocks.append(
                TextBlock(heading="Infeasibility explanation", paragraphs=tuple(paragraphs))
            )
        if resp.rejected_alternatives:
            blocks.append(
                TextBlock(
                    heading="Rejected alternatives",
                    paragraphs=tuple(resp.rejected_alternatives),
                )
            )

        missing = (
            ()
            if resp.objective_total_cost is not None
            else (f"Expected total cost not available (solver status: {resp.solver_status}).",)
        )

        return ReportDocument(
            title=f"CFO Recommendation - {rfq_name} / {scenario.name}",
            generated_at=generated_at.isoformat(),
            calculation_version=scenario.calculation_version,
            methodology=_METHODOLOGY,
            disclaimer=_DISCLAIMER,
            missing_data=missing,
            blocks=tuple(blocks),
        )

    def _build_negotiation_brief(
        self, scenario: ComparisonScenario, brief: NegotiationBrief, generated_at: datetime
    ) -> ReportDocument:
        resp = NegotiationBriefResponse.from_model(brief)

        targets_pairs = (
            (
                "Price target",
                to_wire(resp.price_target) if resp.price_target is not None else "not available",
            ),
            (
                "Stretch target",
                to_wire(resp.stretch_target)
                if resp.stretch_target is not None
                else "not available",
            ),
            (
                "Walk-away threshold",
                to_wire(resp.walk_away_threshold)
                if resp.walk_away_threshold is not None
                else "not available",
            ),
            ("Narrative provider", resp.narrative_provider),
            ("Simulated", str(resp.simulated)),
            ("Review state", resp.state),
        )
        blocks: list[Any] = [KeyValueBlock(heading="Targets", pairs=targets_pairs)]

        for key, section in resp.sections.items():
            heading = f"{key.replace('_', ' ').title()} [{section.provenance}]"
            paragraphs: tuple[str, ...]
            if key == "draft_supplier_email":
                paragraphs = (
                    "DRAFT - not sent.",
                    f"Subject: {resp.draft_email_subject}",
                    resp.draft_email_body,
                )
            else:
                paragraphs = (section.text,)
            blocks.append(TextBlock(heading=heading, paragraphs=paragraphs))

        missing = tuple(
            f"{key}: {section.text}"
            for key, section in resp.sections.items()
            if section.provenance == "missing"
        )

        return ReportDocument(
            title=f"Negotiation Brief - {scenario.name}",
            generated_at=generated_at.isoformat(),
            calculation_version=scenario.calculation_version,
            methodology=_METHODOLOGY,
            disclaimer=_DISCLAIMER,
            missing_data=missing,
            blocks=tuple(blocks),
        )

    def _build_scenario_summary(
        self,
        scenario: ComparisonScenario,
        rfq_name: str,
        rfq_base_currency: str,
        result: ScenarioResult,
        allocation: AllocationResultRecord,
        generated_at: datetime,
    ) -> ReportDocument:
        resp = ScenarioResponse.from_model(
            scenario, result, allocation, base_currency=rfq_base_currency
        )

        meta_pairs = (
            ("RFQ", rfq_name),
            ("Scenario", resp.name),
            ("Strategy", resp.strategy),
            ("State", resp.state),
            ("Notes", resp.notes or "none"),
            ("Calculation version", resp.calculation_version),
            ("Solver version", resp.solver_version),
            ("Created by", resp.created_by_id),
            (
                "Completed at",
                resp.completed_at.isoformat() if resp.completed_at else "not completed",
            ),
        )
        snapshots_block = TextBlock(
            heading="Assumptions and constraints snapshot",
            paragraphs=(
                "Constraints: "
                + json.dumps(resp.constraints_snapshot, sort_keys=True, default=str),
                "Assumptions: "
                + json.dumps(resp.assumptions_snapshot, sort_keys=True, default=str),
            ),
        )
        result_summary_pairs = (
            ("Cohort size", str(resp.scoring_result.cohort_size)),
            ("Scoring version", resp.scoring_result.scoring_version),
            ("Solver status", resp.allocation_result.solver_status),
            (
                "Expected total cost",
                f"{resp.allocation_result.objective_total_cost.amount} "
                f"{resp.allocation_result.objective_total_cost.currency}"
                if resp.allocation_result.objective_total_cost is not None
                else "not available",
            ),
        )
        scores_rows = tuple(
            (str(s.rank), s.supplier_name, s.total_score, "excluded" if s.excluded else "included")
            for s in sorted(resp.scoring_result.scores, key=lambda s: s.rank)
        )
        scores_table = TableBlock(
            heading="Scores",
            columns=("Rank", "Supplier", "Score", "Status"),
            rows=scores_rows,
        )
        missing = tuple(
            f"{s.supplier_name}: missing {', '.join(s.missing_criteria)}"
            for s in resp.scoring_result.scores
            if s.missing_criteria
        )

        return ReportDocument(
            title=f"Scenario Summary - {rfq_name} / {resp.name}",
            generated_at=generated_at.isoformat(),
            calculation_version=resp.calculation_version,
            methodology=_METHODOLOGY,
            disclaimer=_DISCLAIMER,
            missing_data=missing,
            blocks=(
                KeyValueBlock(heading="Scenario", pairs=meta_pairs),
                snapshots_block,
                KeyValueBlock(heading="Result summary", pairs=result_summary_pairs),
                scores_table,
            ),
        )

    def _build_audit_history(
        self, scenario: ComparisonScenario, generated_at: datetime
    ) -> ReportDocument:
        events: list[AuditEvent] = []
        cursor: str | None = None
        while True:
            page, cursor = self._audit_read.list_for_entity(
                "comparison_scenario", scenario.id, limit=200, cursor=cursor
            )
            events.extend(page)
            if cursor is None:
                break

        rows = tuple(
            (
                e.occurred_at.isoformat(),
                e.event_type,
                str(e.actor_user_id) if e.actor_user_id is not None else "system",
                e.explanation or "",
            )
            for e in events
        )
        table = TableBlock(
            heading="Audit history",
            columns=("Occurred at", "Event type", "Actor", "Explanation"),
            rows=rows,
        )
        missing = () if events else ("No audit events recorded for this scenario.",)
        return ReportDocument(
            title=f"Audit History - {scenario.name}",
            generated_at=generated_at.isoformat(),
            calculation_version=scenario.calculation_version,
            methodology=_METHODOLOGY,
            disclaimer=_DISCLAIMER,
            missing_data=missing,
            blocks=(table,),
        )


# -- tiny repositories (no dedicated repositories.py module exists for
# GeneratedReport/ScenarioResult/AllocationResultRecord/NegotiationBrief
# lookups here; the same "add the minimum here" pattern
# repositories/matching_repository.py's own docstring documents, already
# followed identically by brief_service.py's own tiny repositories) --------


class _ScenarioLookupRepository(OrgScopedRepository[ComparisonScenario]):
    model = ComparisonScenario


class _ScenarioResultLookupRepository(OrgScopedRepository[ScenarioResult]):
    model = ScenarioResult

    def get_by_scenario(self, scenario_id: uuid.UUID) -> ScenarioResult | None:
        stmt = self._base_query().where(ScenarioResult.scenario_id == scenario_id)
        return self.session.execute(stmt).scalar_one_or_none()


class _AllocationResultLookupRepository(OrgScopedRepository[AllocationResultRecord]):
    model = AllocationResultRecord

    def get_by_scenario(self, scenario_id: uuid.UUID) -> AllocationResultRecord | None:
        stmt = self._base_query().where(AllocationResultRecord.scenario_id == scenario_id)
        return self.session.execute(stmt).scalar_one_or_none()


class _BriefLookupRepository(OrgScopedRepository[NegotiationBrief]):
    model = NegotiationBrief

    def get_latest_for_scenario(self, scenario_id: uuid.UUID) -> NegotiationBrief | None:
        stmt = (
            self._base_query()
            .where(
                NegotiationBrief.scenario_id == scenario_id,
                NegotiationBrief.archived_at.is_(None),
            )
            .order_by(NegotiationBrief.created_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()


class _ReportRepository(OrgScopedRepository[GeneratedReport]):
    model = GeneratedReport


# -- pure helpers ------------------------------------------------------


def _assumptions_block(scenario: ComparisonScenario) -> KeyValueBlock:
    pairs = tuple(
        (key, "null" if value is None else str(value))
        for key, value in sorted(scenario.assumptions_snapshot.items())
    )
    return KeyValueBlock(heading="Assumptions snapshot", pairs=pairs)


def _safe_filename(report: GeneratedReport) -> str:
    """`{report_type}-{id-prefix}.{format}` - never the raw storage key."""
    id_prefix = str(report.id).split("-")[0]
    return f"{report.report_type.value}-{id_prefix}.{report.format.value}"


def _report_audit_state(report: GeneratedReport) -> dict[str, Any]:
    return {
        "scenario_id": str(report.scenario_id),
        "report_type": report.report_type.value,
        "format": report.format.value,
        "state": report.state.value,
        "size_bytes": report.size_bytes,
        "error_message": report.error_message,
    }


__all__ = [
    "REPORT_RETENTION_DAYS",
    "ReportPurgedError",
    "ReportService",
]
