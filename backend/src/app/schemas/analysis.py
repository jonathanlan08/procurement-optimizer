"""Landed-cost + scoring-configuration request/response schemas
(docs/planning/03-api-contract.md §4.14-4.15).

Wire conventions (§1.2): every decimal crosses the wire as a JSON string,
never a number. `DecimalString` below is deliberately **unscaled** (parses
via `app.core.money.parse_decimal`, not `parse_at_scale`): scenario
assumptions (`quality_risk_rate`, `annual_rate`, lead-time day counts, ...)
have no single DB column/scale of their own — they are recorded inside
`landed_cost_results.inputs_snapshot`/`.assumptions` JSONB, not a typed
`NUMERIC(p,s)` column — so there is no boundary scale to enforce at the API
edge the way `UnitPriceString`/`QuantityString` enforce theirs. Money/
quantity fields that DO map onto a fixed-scale column (`amount`,
`accepted_quantity`, `effective_unit_cost`, a weight fraction) reuse the
existing scaled string types from `app.schemas.parts`/`app.schemas.boms`.

See `app/services/landed_cost_service.py` module docstring for why
`LandedCostAssumptionsRequest` carries `tariff_rate`/`duty_rate`/
`promised_lead_time_days`/`required_lead_time_days` in addition to the five
fields this task's brief names by name.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
)

from app.core.money import RATIO_SCALE, MoneyError, parse_at_scale, parse_decimal, to_wire
from app.domain.landed_cost.contracts import ComponentResult, LandedCostResult
from app.models.analysis import LandedCostComponent, ScoringConfiguration
from app.models.analysis import LandedCostResult as LandedCostResultRow
from app.services.scoring_config_service import CriterionSpecInput


def _decimal_from_wire_unscaled(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        raise ValueError('must be a decimal string, e.g. "0.035"')
    try:
        return parse_decimal(value)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc


DecimalString = Annotated[
    Decimal,
    BeforeValidator(_decimal_from_wire_unscaled),
    PlainSerializer(to_wire, return_type=str),
    WithJsonSchema({"type": "string", "example": "0.035"}),
]


def _fraction_from_wire(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        raise ValueError('must be a decimal string, e.g. "0.350000"')
    try:
        return parse_at_scale(value, RATIO_SCALE)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc


FractionString = Annotated[
    Decimal,
    BeforeValidator(_fraction_from_wire),
    PlainSerializer(to_wire, return_type=str),
    WithJsonSchema({"type": "string", "example": "0.350000"}),
]


# -- landed cost -------------------------------------------------------


class LandedCostAssumptionsRequest(BaseModel):
    """All fields optional decimal strings; omitted means "not supplied" and
    the corresponding domain input stays `Quantified.missing(...)`, never a
    silent zero (app/domain/values.py)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    quality_risk_rate: DecimalString | None = Field(default=None, ge=0)
    delay_risk_per_day: DecimalString | None = Field(default=None, ge=0)
    promised_lead_time_days: DecimalString | None = Field(default=None, ge=0)
    required_lead_time_days: DecimalString | None = Field(default=None, ge=0)
    annual_rate: DecimalString | None = Field(default=None, ge=0)
    baseline_terms_days: DecimalString | None = Field(default=None, ge=0)
    tariff_rate: DecimalString | None = Field(default=None, ge=0)
    duty_rate: DecimalString | None = Field(default=None, ge=0)
    assume_missing_costs_zero: bool = False


class LandedCostPreviewRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    quote_line_id: uuid.UUID
    assumptions: LandedCostAssumptionsRequest = Field(
        default_factory=LandedCostAssumptionsRequest
    )


class LandedCostCalculateRequest(BaseModel):
    """`POST /rfqs/{rfq_id}/landed-costs` body: this task's `calculate_and_store`
    is per-quote-line, so the request names the line explicitly (the
    contract's own prose describes a batch-all-matched-lines shape; see
    api/v1/analysis.py module docstring for why the narrower, task-specified
    service surface is what this route wraps)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    quote_line_id: uuid.UUID
    assumptions: LandedCostAssumptionsRequest = Field(
        default_factory=LandedCostAssumptionsRequest
    )


class ComponentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    component: str
    amount: str
    formula: str
    inputs: dict[str, str]
    provenance: str
    is_assumed: bool
    is_missing: bool

    @classmethod
    def from_domain(cls, c: ComponentResult) -> ComponentResponse:
        return cls(
            component=c.component.value,
            amount=to_wire(c.amount),
            formula=c.formula,
            inputs=c.inputs,
            provenance=c.provenance.value,
            is_assumed=c.is_assumed,
            is_missing=c.is_missing,
        )

    @classmethod
    def from_model(cls, row: LandedCostComponent) -> ComponentResponse:
        return cls(
            component=row.component.value,
            amount=to_wire(row.amount),
            formula=row.formula,
            inputs=row.inputs,
            provenance=row.provenance,
            is_assumed=row.is_assumed,
            is_missing=row.is_missing,
        )


class MissingInputResponse(BaseModel):
    component: str
    input_name: str
    consequence: str


class AssumptionResponse(BaseModel):
    key: str
    value: str
    description: str
    provenance: str


class LandedCostPreviewResponse(BaseModel):
    """`POST /landed-costs:preview` response — computed, never persisted."""

    quote_line_id: str
    accepted_quantity: str
    currency: str
    total_landed_cost: str
    effective_unit_cost: str
    completeness: str
    calculation_version: str
    components: list[ComponentResponse]
    missing_inputs: list[MissingInputResponse]
    assumptions: list[AssumptionResponse]

    @classmethod
    def from_domain(
        cls, result: LandedCostResult, *, accepted_quantity: Decimal
    ) -> LandedCostPreviewResponse:
        return cls(
            quote_line_id=str(result.quote_line_id),
            accepted_quantity=to_wire(accepted_quantity),
            currency=result.currency,
            total_landed_cost=to_wire(result.total_landed_cost),
            effective_unit_cost=to_wire(result.effective_unit_cost),
            completeness=result.completeness.value,
            calculation_version=result.calculation_version,
            components=[ComponentResponse.from_domain(c) for c in result.components],
            missing_inputs=[
                MissingInputResponse(
                    component=m.component.value,
                    input_name=m.input_name,
                    consequence=m.consequence,
                )
                for m in result.missing_inputs
            ],
            assumptions=[
                AssumptionResponse(
                    key=a.key,
                    value=a.value,
                    description=a.description,
                    provenance=a.provenance.value,
                )
                for a in result.assumptions
            ],
        )


class LandedCostResultResponse(BaseModel):
    """A persisted `landed_cost_results` row plus its components."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    quote_line_id: str
    accepted_quantity: str
    currency: str
    total_landed_cost: str
    effective_unit_cost: str
    completeness: str
    calculation_version: str
    calculated_at: datetime
    calculated_by_id: str
    missing_inputs: list[MissingInputResponse]
    assumptions: list[AssumptionResponse]
    components: list[ComponentResponse]

    @classmethod
    def from_model(
        cls, row: LandedCostResultRow, components: list[LandedCostComponent]
    ) -> LandedCostResultResponse:
        return cls(
            id=str(row.id),
            quote_line_id=str(row.quote_line_id),
            accepted_quantity=to_wire(row.accepted_quantity),
            currency=row.currency,
            total_landed_cost=to_wire(row.total_landed_cost),
            effective_unit_cost=to_wire(row.effective_unit_cost),
            completeness=row.completeness.value,
            calculation_version=row.calculation_version,
            calculated_at=row.calculated_at,
            calculated_by_id=str(row.calculated_by_id),
            missing_inputs=[MissingInputResponse(**m) for m in row.missing_inputs],
            assumptions=[AssumptionResponse(**a) for a in row.assumptions],
            components=[ComponentResponse.from_model(c) for c in components],
        )


class LandedCostResultListResponse(BaseModel):
    items: list[LandedCostResultResponse]


# -- scoring configurations --------------------------------------------


class CriterionSpecCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    criterion: str
    weight: FractionString = Field(ge=0, le=1)
    direction: str
    label: str | None = Field(default=None, max_length=200)

    def to_domain_input(self) -> CriterionSpecInput:
        return CriterionSpecInput(
            criterion=self.criterion, weight=self.weight, direction=self.direction, label=self.label
        )


class CriterionSpecResponse(BaseModel):
    criterion: str
    weight: str
    direction: str
    label: str | None
    is_sample_weight: bool

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> CriterionSpecResponse:
        return cls(
            criterion=raw["criterion"],
            weight=raw["weight"],
            direction=raw["direction"],
            label=raw.get("label"),
            is_sample_weight=bool(raw.get("is_sample_weight", False)),
        )


class ScoringConfigurationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    weights: list[CriterionSpecCreate] = Field(min_length=1)


class ScoringConfigurationUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    weights: list[CriterionSpecCreate] | None = None


class ScoringConfigurationArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class ScoringConfigurationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    weights: list[CriterionSpecResponse]
    weight_sum: str
    is_sample: bool
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    notes: list[str]

    @classmethod
    def from_model(
        cls, row: ScoringConfiguration, *, notes: list[str] | None = None
    ) -> ScoringConfigurationResponse:
        total = sum((Decimal(str(w["weight"])) for w in row.weights), Decimal("0"))
        return cls(
            id=str(row.id),
            name=row.name,
            weights=[CriterionSpecResponse.from_json(w) for w in row.weights],
            weight_sum=to_wire(total),
            is_sample=row.is_sample,
            is_archived=row.is_archived,
            archived_at=row.archived_at,
            archive_reason=row.archive_reason,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            notes=notes or [],
        )


class ScoringConfigurationListResponse(BaseModel):
    items: list[ScoringConfigurationResponse]


__all__ = [
    "AssumptionResponse",
    "ComponentResponse",
    "CriterionSpecCreate",
    "CriterionSpecResponse",
    "DecimalString",
    "FractionString",
    "LandedCostAssumptionsRequest",
    "LandedCostCalculateRequest",
    "LandedCostPreviewRequest",
    "LandedCostPreviewResponse",
    "LandedCostResultListResponse",
    "LandedCostResultResponse",
    "MissingInputResponse",
    "ScoringConfigurationArchiveRequest",
    "ScoringConfigurationCreate",
    "ScoringConfigurationListResponse",
    "ScoringConfigurationResponse",
    "ScoringConfigurationUpdate",
]
