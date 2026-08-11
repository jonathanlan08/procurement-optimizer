"""Landed-cost service — assembles `LandedCostInput` from persisted data and
runs the frozen `LandedCostCalculatorV1` (docs/planning/03-api-contract.md
§4.15, app/domain/landed_cost/{contracts,calculator}.py, app/domain/fx/
normalize.py, app/domain/units/normalize.py).

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. `calculate_and_store`
writes exactly one audit event (`landed_cost.calculated`) in the same
transaction as the row it describes. `preview` writes none — it is a
stateless calculator, not a decision (03-api-contract.md §6 gap #2: "I
believe that is correct... it is the one mutation-free money path with no
audit row").

**Assembly recipe** (this task's brief, followed literally):
`quote_line` + its `quote_price_breaks` (via the frozen
`app.domain.landed_cost.breaks.select_price_break`, evaluated at the matched
RFQ line's `required_quantity`) + `quote_terms` (payment terms days) + the FX
effective rate (`FxService.get_effective_rate`, as-of the quote's
`quote_date`) + a unit-conversion leg (`app.domain.units.normalize.
convert_quantity`) when the quote line's unit differs from the matched RFQ
line's unit — the RFQ line's unit is always the target. A missing input at
any stage never becomes zero silently: it becomes a `Quantified.missing(...)`
that the calculator itself turns into a `MissingInput` entry and degrades
`completeness` (`app.domain.values`, `05-calculation-methodology.md` §2).

**Two deliberate extensions beyond the five scenario-assumption fields this
task's brief names** (`quality_risk_rate`, `delay_risk_per_day`,
`annual_rate`, `baseline_terms_days`, `assume_missing_costs_zero`) — spelled
out here because both are load-bearing for the worked example
(05-calculation-methodology.md §9) that `tests/integration/
test_landed_cost_api.py` must reproduce exactly, and neither has any other
possible source:

1. **`tariff_rate`/`duty_rate`.** `ImportCosts` (contracts.py) requires a
   rate for each of tariff/duty when the corresponding amount is not quoted
   (the "derive amount = rate x basis" path) — the worked example's import
   component is entirely rate-derived (`0.035 x (material + logistics)`,
   `tariff_amount`/`duty_amount` both unstated on the quote line). Neither
   rate has any column anywhere in this schema (`quote_lines`,
   `rfq_lines`, `quote_terms`) — a scenario assumption is the only possible
   source, exactly like `quality_risk_rate`. Added as two more optional
   `LandedCostAssumptions` fields.
2. **`promised_lead_time_days`/`required_lead_time_days`.** `RiskAssumptions`
   requires both for `DELAY_RISK` to be anything other than `is_missing`.
   `QuoteLine.lead_time_days` supplies "promised" when the supplier stated
   it (used here as a `SUPPLIER`-provenance fallback before falling back to
   an assumption override), but **no table anywhere in this schema has a
   "required lead time" column** — `RfqLine` deliberately omits it
   (app/models/rfqs.py module docstring point 3: "RfqLine does not carry
   required_by_date... omitted from the delegating task's field list").
   Since the worked example states "no delay" (an affirmative, non-missing
   zero-day gap) and needs `completeness=ASSUMPTION_DEPENDENT` rather than
   `INCOMPLETE`, `required_lead_time_days` cannot be left permanently
   unsourceable; added as an assumption override, `promised_lead_time_days`
   alongside it for symmetry (and to let a caller override a stale/missing
   `lead_time_days` on the line without editing the quote).

Both extensions are recorded with `USER_ASSUMPTION` provenance, identically
to the five named fields, when supplied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import ConflictStateError, NotFoundError, ValidationAppError
from app.core.ids import IdGenerator
from app.core.money import (
    CALC_CONTEXT,
    InvalidDecimalString,
    parse_decimal,
    quantize_money,
    quantize_qty,
    quantize_unit_price,
    to_wire,
)
from app.domain.fx.normalize import normalize_price
from app.domain.landed_cost.breaks import select_price_break
from app.domain.landed_cost.calculator import LandedCostCalculatorV1
from app.domain.landed_cost.contracts import (
    Assumption,
    ComponentResult,
    FinancingAssumptions,
    FixedCosts,
    ImportCosts,
    LandedCostInput,
    LandedCostResult,
    LogisticsCosts,
    MissingInput,
    PriceBreakTier,
    RiskAssumptions,
)
from app.domain.units.normalize import (
    ConversionAssumptionMissingError,
    Dimension,
    DimensionMismatchError,
    Unit,
    convert_quantity,
)
from app.domain.values import Provenance, Quantified
from app.models.analysis import LandedCostComponent
from app.models.analysis import LandedCostResult as LandedCostResultRow
from app.models.quotes import Quote, QuoteLine
from app.models.rfqs import Rfq, RfqLine
from app.models.units import UnitConversion, UnitDefinition
from app.repositories.base import OrgScopedRepository
from app.repositories.quote_repository import QuoteRepository
from app.repositories.rfq_repository import RfqRepository
from app.services.audit import AuditRecorder
from app.services.fx_service import FxService

_StoredResult = tuple[LandedCostResultRow, list[LandedCostComponent]]


@dataclass(frozen=True, slots=True)
class LandedCostAssumptions:
    """Optional, all-decimal-string scenario assumptions from the request.
    See module docstring for why `tariff_rate`/`duty_rate`/
    `promised_lead_time_days`/`required_lead_time_days` are present in
    addition to the five fields this task's brief names by name."""

    quality_risk_rate: str | None = None
    delay_risk_per_day: str | None = None
    promised_lead_time_days: str | None = None
    required_lead_time_days: str | None = None
    annual_rate: str | None = None
    baseline_terms_days: str | None = None
    tariff_rate: str | None = None
    duty_rate: str | None = None
    assume_missing_costs_zero: bool = False


class _LandedCostRepository(OrgScopedRepository[LandedCostResultRow]):
    model = LandedCostResultRow

    def add_component(self, component: LandedCostComponent) -> LandedCostComponent:
        if component.organization_id != self.organization_id:
            raise ValueError("component organization_id does not match repository scope")
        self.session.add(component)
        return component

    def list_components(self, result_id: uuid.UUID) -> list[LandedCostComponent]:
        stmt = (
            select(LandedCostComponent)
            .where(
                LandedCostComponent.organization_id == self.organization_id,
                LandedCostComponent.landed_cost_result_id == result_id,
            )
            .order_by(LandedCostComponent.component)
        )
        return list(self.session.execute(stmt).scalars().all())

    def latest_per_line_for_rfq(self, rfq_id: uuid.UUID) -> list[LandedCostResultRow]:
        stmt = (
            select(LandedCostResultRow)
            .join(QuoteLine, QuoteLine.id == LandedCostResultRow.quote_line_id)
            .join(Quote, Quote.id == QuoteLine.quote_id)
            .where(
                LandedCostResultRow.organization_id == self.organization_id,
                Quote.rfq_id == rfq_id,
            )
            .order_by(
                LandedCostResultRow.quote_line_id,
                LandedCostResultRow.calculated_at.desc(),
            )
        )
        rows = list(self.session.execute(stmt).scalars().all())
        latest: dict[uuid.UUID, LandedCostResultRow] = {}
        for row in rows:
            latest.setdefault(row.quote_line_id, row)
        return list(latest.values())


def _dec(raw: str | None, *, field: str) -> Decimal:
    if raw is None:
        raise ValueError(f"{field} is None")  # pragma: no cover - guarded by caller
    try:
        return parse_decimal(raw)
    except InvalidDecimalString as exc:
        raise ValidationAppError(
            f"{field} must be a valid decimal string.",
        ) from exc


def _assumption(raw: str | None, *, field: str, missing_note: str) -> Quantified:
    if raw is None:
        return Quantified.missing(note=missing_note)
    return Quantified.assumption(_dec(raw, field=field), note=f"{field} scenario assumption")


def _missing_input_json(m: MissingInput) -> dict[str, str]:
    return {
        "component": m.component.value,
        "input_name": m.input_name,
        "consequence": m.consequence,
    }


def _assumption_json(a: Assumption) -> dict[str, str]:
    return {
        "key": a.key,
        "value": a.value,
        "description": a.description,
        "provenance": a.provenance.value,
    }


def _component_json(c: ComponentResult) -> dict[str, Any]:
    return {
        "component": c.component.value,
        "amount": to_wire(c.amount),
        "formula": c.formula,
        "inputs": c.inputs,
        "provenance": c.provenance.value,
        "is_assumed": c.is_assumed,
        "is_missing": c.is_missing,
    }


def _result_state(result: LandedCostResult) -> dict[str, Any]:
    """JSON-safe snapshot of the domain result for audit after_state."""
    return {
        "quote_line_id": str(result.quote_line_id),
        "total_landed_cost": to_wire(result.total_landed_cost),
        "effective_unit_cost": to_wire(result.effective_unit_cost),
        "currency": result.currency,
        "completeness": result.completeness.value,
        "calculation_version": result.calculation_version,
        "components": [_component_json(c) for c in result.components],
        "missing_inputs": [_missing_input_json(m) for m in result.missing_inputs],
        "assumptions": [_assumption_json(a) for a in result.assumptions],
    }


class LandedCostService:
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
        self._quote_repo = QuoteRepository(session, organization_id)
        self._rfq_repo = RfqRepository(session, organization_id)
        self._repo = _LandedCostRepository(session, organization_id)
        self._fx = FxService(session, organization_id, audit, clock, ids)

    # -- context loading ---------------------------------------------------

    def _load_context(
        self, quote_line_id: uuid.UUID, *, expected_rfq_id: uuid.UUID | None = None
    ) -> tuple[QuoteLine, Quote, Rfq, RfqLine]:
        stmt = select(QuoteLine).where(
            QuoteLine.id == quote_line_id,
            QuoteLine.organization_id == self._organization_id,
        )
        line = self._db.execute(stmt).scalar_one_or_none()
        if line is None:
            raise NotFoundError("Quote line")

        quote = self._quote_repo.get_or_raise(line.quote_id)
        rfq = self._rfq_repo.get_or_raise(quote.rfq_id)

        if expected_rfq_id is not None and rfq.id != expected_rfq_id:
            raise ValidationAppError(
                "quote_line_id does not belong to a quote against this RFQ.",
            )

        if line.matched_rfq_line_id is None:
            raise ConflictStateError(
                "Quote line is not matched to an RFQ line; landed cost requires a "
                "matched RFQ line to determine the accepted quantity."
            )
        rfq_line = self._rfq_repo.get_line(rfq.id, line.matched_rfq_line_id)
        if rfq_line is None:  # pragma: no cover - FK integrity makes this unreachable
            raise NotFoundError("RFQ line")
        return line, quote, rfq, rfq_line

    # -- unit conversion -----------------------------------------------

    def _unit_row(self, unit_id: uuid.UUID) -> UnitDefinition:
        row = self._db.execute(
            select(UnitDefinition).where(UnitDefinition.id == unit_id)
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover - FK integrity makes this unreachable
            raise NotFoundError("UnitDefinition")
        return row

    @staticmethod
    def _to_domain_unit(row: UnitDefinition) -> Unit:
        return Unit(
            code=row.code,
            dimension=Dimension(row.dimension.value),
            to_canonical_factor=row.to_canonical_factor,
        )

    def _lookup_part_factor(
        self, from_unit_id: uuid.UUID, to_unit_id: uuid.UUID, part_id: uuid.UUID
    ) -> Decimal | None:
        """An org-recorded `UnitConversion` row for this exact pair (either
        direction) and part, if one exists — the source of the "per-part
        pack size" factor `convert_quantity` needs when a count-dimension
        container (pack/box/tray/reel) has no universal ratio."""
        stmt = select(UnitConversion).where(
            UnitConversion.organization_id == self._organization_id,
            UnitConversion.part_id == part_id,
        )
        rows = self._db.execute(stmt).scalars().all()
        for row in rows:
            if row.from_unit_id == from_unit_id and row.to_unit_id == to_unit_id:
                return row.factor
            if row.from_unit_id == to_unit_id and row.to_unit_id == from_unit_id:
                with localcontext(CALC_CONTEXT):
                    return Decimal(1) / row.factor
        return None

    def _apply_unit_conversion(
        self,
        price: Quantified,
        quote_unit_id: uuid.UUID,
        rfq_unit_id: uuid.UUID,
        part_id: uuid.UUID,
    ) -> tuple[Quantified, dict[str, Any]]:
        """Convert `price` (per quote-line unit) into a price per RFQ-line
        unit — the RFQ line's unit is always the target (this task's own
        instruction). A missing conversion factor never guesses: the price
        becomes MISSING with an explanatory note, which the calculator turns
        into an INCOMPLETE result (never a silent 1:1 passthrough)."""
        if quote_unit_id == rfq_unit_id:
            return price, {"applied": False, "reason": "quote and RFQ line units match"}
        if price.value is None:
            return price, {"applied": False, "reason": "price already missing"}

        quote_unit = self._to_domain_unit(self._unit_row(quote_unit_id))
        rfq_unit = self._to_domain_unit(self._unit_row(rfq_unit_id))

        part_factor: Decimal | None = None
        if quote_unit.to_canonical_factor is None or rfq_unit.to_canonical_factor is None:
            part_factor = self._lookup_part_factor(quote_unit_id, rfq_unit_id, part_id)

        try:
            ratio = convert_quantity(Decimal(1), quote_unit, rfq_unit, part_factor=part_factor)
        except (DimensionMismatchError, ConversionAssumptionMissingError) as exc:
            note = f"unit conversion assumption missing: {exc}"
            return Quantified.missing(note=note), {"applied": False, "reason": note}

        with localcontext(CALC_CONTEXT):
            adjusted_raw = price.value / ratio.value
        adjusted = quantize_unit_price(adjusted_raw)
        is_assumption = part_factor is not None
        provenance = Provenance.USER_ASSUMPTION if is_assumption else price.provenance
        converted = Quantified(value=adjusted, provenance=provenance, note=ratio.conversion_note)
        return converted, {
            "applied": True,
            "ratio": to_wire(ratio.value),
            "conversion_note": ratio.conversion_note,
            "is_assumption": is_assumption,
        }

    # -- price selection -----------------------------------------------

    def _select_quoted_price(
        self, line: QuoteLine, accepted_quantity: Decimal
    ) -> tuple[Quantified, dict[str, Any]]:
        price_breaks = self._quote_repo.list_price_breaks(line.id)
        if not price_breaks:
            if line.unit_price is None:
                return (
                    Quantified.missing(note="unit_price is not stated on the quote line"),
                    {"source": "unit_price", "reason": "not stated"},
                )
            return (
                Quantified.supplier(line.unit_price, note="quote line unit_price"),
                {"source": "unit_price", "value": to_wire(line.unit_price)},
            )

        tiers = tuple(
            PriceBreakTier(
                min_quantity=pb.min_quantity, max_quantity=pb.max_quantity, unit_price=pb.unit_price
            )
            for pb in price_breaks
        )
        selection = select_price_break(tiers, accepted_quantity)
        if selection.tier is None:
            return (
                Quantified.missing(note=selection.reason),
                {"source": "price_break", "reason": selection.reason},
            )
        return (
            Quantified.supplier(selection.tier.unit_price, note=selection.reason),
            {
                "source": "price_break",
                "reason": selection.reason,
                "tier_min_quantity": to_wire(selection.tier.min_quantity),
                "tier_max_quantity": (
                    to_wire(selection.tier.max_quantity)
                    if selection.tier.max_quantity is not None
                    else None
                ),
            },
        )

    # -- money-amount currency conversion (distinct scale from unit price) -

    def _money(
        self,
        value: Decimal | None,
        *,
        source_currency: str,
        target_currency: str,
        rate: Decimal | None,
        field: str,
    ) -> tuple[Quantified, dict[str, Any]]:
        """Currency-convert one fixed/logistics/import money amount. Mirrors
        `app.domain.fx.normalize.normalize_price`'s missing/passthrough
        semantics, but quantizes at `MONEY_SCALE` (these are totals, not
        unit prices) — that module's own function always quantizes at
        `UNIT_PRICE_SCALE`, so it cannot be reused verbatim for these
        columns without silently mis-scaling the worked example (§9)."""
        if value is None:
            return Quantified.missing(note=f"{field} is not stated on the quote line"), {
                "field": field,
                "status": "missing",
            }
        if source_currency == target_currency:
            return (
                Quantified.supplier(quantize_money(value), note=field),
                {"field": field, "status": "same_currency", "value": to_wire(value)},
            )
        if rate is None:
            note = f"no FX rate available to convert {source_currency} to {target_currency}"
            return Quantified.missing(note=note), {"field": field, "status": "no_rate"}

        with localcontext(CALC_CONTEXT):
            converted_raw = value * rate
        converted = quantize_money(converted_raw)
        note = f"{field} converted {source_currency}->{target_currency}"
        return (
            Quantified.supplier(converted, note=note),
            {
                "field": field,
                "status": "converted",
                "source_value": to_wire(value),
                "rate": to_wire(rate),
                "converted_value": to_wire(converted),
            },
        )

    # -- assembly ------------------------------------------------------

    def _assemble_input(
        self,
        line: QuoteLine,
        quote: Quote,
        rfq: Rfq,
        rfq_line: RfqLine,
        assumptions: LandedCostAssumptions,
    ) -> tuple[LandedCostInput, dict[str, Any]]:
        source_currency = quote.currency
        target_currency = rfq.base_currency

        fx_rate_for_normalize: Decimal | None = None
        fx_snapshot: dict[str, Any] = {
            "source_currency": source_currency,
            "target_currency": target_currency,
        }
        if source_currency != target_currency:
            effective = self._fx.get_effective_rate(
                target_currency, source_currency, quote.quote_date
            )
            with localcontext(CALC_CONTEXT):
                fx_rate_for_normalize = Decimal(1) / effective.rate
            fx_snapshot.update(
                {
                    "base_currency": target_currency,
                    "quote_currency": source_currency,
                    "as_of": quote.quote_date.isoformat(),
                    "provider_rate": to_wire(effective.rate),
                    "rate_target_per_source": to_wire(fx_rate_for_normalize),
                    "source": effective.source,
                    "is_manual_override": effective.is_manual_override,
                    "exchange_rate_id": (
                        str(effective.exchange_rate_id)
                        if effective.exchange_rate_id is not None
                        else None
                    ),
                }
            )

        accepted_quantity = quantize_qty(rfq_line.required_quantity)

        quoted_price, price_snapshot = self._select_quoted_price(line, accepted_quantity)
        unit_adjusted_price, unit_snapshot = self._apply_unit_conversion(
            quoted_price, line.unit_definition_id, rfq_line.unit_definition_id, rfq_line.part_id
        )
        normalized = normalize_price(
            unit_adjusted_price,
            source_currency,
            target_currency,
            fx_rate_for_normalize,
            fx_snapshot.get("source"),
        )
        normalized_unit_price = normalized.unit_price

        money_snapshot: dict[str, Any] = {}

        def money(value: Decimal | None, field: str) -> Quantified:
            q, snap = self._money(
                value,
                source_currency=source_currency,
                target_currency=target_currency,
                rate=fx_rate_for_normalize,
                field=field,
            )
            money_snapshot[field] = snap
            return q

        fixed = FixedCosts(
            tooling=money(line.tooling_cost, "tooling"),
            setup=money(line.setup_cost, "setup"),
            # no source column anywhere in this schema — see module docstring.
            documentation=Quantified.missing(
                note="documentation cost has no source column on quote_lines"
            ),
            other_fixed=money(line.other_fixed_cost, "other_fixed"),
        )
        logistics = LogisticsCosts(
            shipping=money(line.shipping_cost, "shipping"),
            insurance=money(line.insurance_cost, "insurance"),
            packaging=money(line.packaging_cost, "packaging"),
            # no source column anywhere in this schema — see module docstring.
            handling=Quantified.missing(
                note="handling cost has no source column on quote_lines"
            ),
        )

        tariff_rate = _assumption(
            assumptions.tariff_rate, field="tariff_rate", missing_note="tariff_rate not supplied"
        )
        duty_rate = _assumption(
            assumptions.duty_rate, field="duty_rate", missing_note="duty_rate not supplied"
        )
        imports = ImportCosts(
            tariff_amount=money(line.tariff_amount, "tariff_amount"),
            duty_amount=money(line.duty_amount, "duty_amount"),
            customs_fees=money(line.customs_fee, "customs_fees"),
            tariff_rate=tariff_rate,
            duty_rate=duty_rate,
        )

        quality_risk_rate = _assumption(
            assumptions.quality_risk_rate,
            field="quality_risk_rate",
            missing_note="quality_risk_rate not supplied",
        )
        delay_risk_per_day = _assumption(
            assumptions.delay_risk_per_day,
            field="delay_risk_per_day",
            missing_note="delay_risk_per_day not supplied",
        )
        if assumptions.promised_lead_time_days is not None:
            promised = _assumption(
                assumptions.promised_lead_time_days,
                field="promised_lead_time_days",
                missing_note="promised_lead_time_days not supplied",
            )
        elif line.lead_time_days is not None:
            promised = Quantified.supplier(
                Decimal(line.lead_time_days), note="quote line lead_time_days"
            )
        else:
            promised = Quantified.missing(
                note="promised_lead_time_days not stated on the quote line and not supplied"
            )
        required = _assumption(
            assumptions.required_lead_time_days,
            field="required_lead_time_days",
            missing_note="required_lead_time_days not supplied (no source column on rfq_lines)",
        )
        risk = RiskAssumptions(
            quality_risk_rate=quality_risk_rate,
            delay_risk_per_day=delay_risk_per_day,
            promised_lead_time_days=promised,
            required_lead_time_days=required,
        )

        annual_rate = _assumption(
            assumptions.annual_rate, field="annual_rate", missing_note="annual_rate not supplied"
        )
        baseline_terms_days = _assumption(
            assumptions.baseline_terms_days,
            field="baseline_terms_days",
            missing_note="baseline_terms_days not supplied",
        )
        terms = self._quote_repo.get_terms(quote.id)
        if terms is not None and terms.payment_terms_days is not None:
            payment_terms_days = Quantified.supplier(
                Decimal(terms.payment_terms_days), note="quote_terms.payment_terms_days"
            )
        else:
            payment_terms_days = Quantified.missing(
                note="payment_terms_days not stated on quote_terms"
            )
        financing = FinancingAssumptions(
            annual_rate=annual_rate,
            payment_terms_days=payment_terms_days,
            baseline_terms_days=baseline_terms_days,
        )

        inp = LandedCostInput(
            quote_line_id=line.id,
            accepted_quantity=accepted_quantity,
            normalized_unit_price=normalized_unit_price,
            currency=target_currency,
            fixed=fixed,
            logistics=logistics,
            imports=imports,
            risk=risk,
            financing=financing,
            assume_missing_costs_zero=assumptions.assume_missing_costs_zero,
        )

        snapshot: dict[str, Any] = {
            "quote_line_id": str(line.id),
            "quote_id": str(quote.id),
            "rfq_id": str(rfq.id),
            "rfq_line_id": str(rfq_line.id),
            "accepted_quantity": to_wire(accepted_quantity),
            "source_currency": source_currency,
            "target_currency": target_currency,
            "fx": fx_snapshot,
            "price_selection": price_snapshot,
            "unit_conversion": unit_snapshot,
            "money_amounts": money_snapshot,
            "assume_missing_costs_zero": assumptions.assume_missing_costs_zero,
        }
        return inp, snapshot

    # -- reads -----------------------------------------------------------

    def preview(
        self, quote_line_id: uuid.UUID, assumptions: LandedCostAssumptions
    ) -> tuple[LandedCostResult, dict[str, Any]]:
        line, quote, rfq, rfq_line = self._load_context(quote_line_id)
        inp, snapshot = self._assemble_input(line, quote, rfq, rfq_line, assumptions)
        result = LandedCostCalculatorV1().calculate(inp)
        return result, snapshot

    def get(self, result_id: uuid.UUID) -> _StoredResult:
        row = self._repo.get_or_raise(result_id)
        return row, self._repo.list_components(row.id)

    def latest_for_rfq(self, rfq_id: uuid.UUID) -> list[_StoredResult]:
        self._rfq_repo.get_or_raise(rfq_id)
        rows = self._repo.latest_per_line_for_rfq(rfq_id)
        return [(row, self._repo.list_components(row.id)) for row in rows]

    # -- write -----------------------------------------------------------

    def calculate_and_store(
        self,
        quote_line_id: uuid.UUID,
        assumptions: LandedCostAssumptions,
        *,
        actor_id: uuid.UUID,
        rfq_id: uuid.UUID | None = None,
    ) -> _StoredResult:
        line, quote, rfq, rfq_line = self._load_context(
            quote_line_id, expected_rfq_id=rfq_id
        )
        inp, snapshot = self._assemble_input(line, quote, rfq, rfq_line, assumptions)
        result = LandedCostCalculatorV1().calculate(inp)

        now = self._clock.now()
        row = LandedCostResultRow(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            quote_line_id=line.id,
            accepted_quantity=inp.accepted_quantity,
            currency=result.currency,
            total_landed_cost=result.total_landed_cost,
            effective_unit_cost=result.effective_unit_cost,
            completeness=result.completeness,
            calculation_version=result.calculation_version,
            inputs_snapshot=snapshot,
            missing_inputs=[_missing_input_json(m) for m in result.missing_inputs],
            assumptions=[_assumption_json(a) for a in result.assumptions],
            calculated_at=now,
            calculated_by_id=actor_id,
        )
        self._repo.add(row)
        self._db.flush()

        components: list[LandedCostComponent] = []
        for c in result.components:
            comp_row = LandedCostComponent(
                id=self._ids.new_id(),
                organization_id=self._organization_id,
                landed_cost_result_id=row.id,
                component=c.component,
                amount=c.amount,
                formula=c.formula,
                inputs=c.inputs,
                provenance=c.provenance.value,
                is_assumed=c.is_assumed,
                is_missing=c.is_missing,
            )
            self._repo.add_component(comp_row)
            components.append(comp_row)
        self._db.flush()

        self._audit.record(
            "landed_cost.calculated",
            entity_type="landed_cost_result",
            entity_id=row.id,
            after_state=_result_state(result),
            explanation=(
                f"Landed cost calculated for quote line {line.id}: "
                f"total {to_wire(result.total_landed_cost)} {result.currency} "
                f"({result.completeness.value})"
            ),
        )
        return row, components


__all__ = ["LandedCostAssumptions", "LandedCostService"]
