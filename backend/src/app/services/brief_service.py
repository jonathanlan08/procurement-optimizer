"""Negotiation-brief service — assembles a grounded `NegotiationBrief` from
an already-complete `ComparisonScenario` (docs/SPEC.md §Negotiation brief,
docs/planning/02-erd.md §7, docs/planning/03-api-contract.md §4.17,
app/models/briefs.py, app/providers/narrative/{base,template}.py).

Services never commit: the request-scoped `get_db` dependency
(app/api/deps.py) commits on success and rolls back on any raised
exception. `generate` writes exactly one audit event (`brief.generated`) in
the same transaction as the row it describes.

## Deviation 1 — `POST .../negotiation-briefs` runs synchronously, `201` not `202`

03-api-contract.md §4.17's literal table says `-> 202`. This codebase has no
job queue anywhere — `services/extraction_service.py`'s own module docstring
establishes `JOB_RUNNER=inline` as the only shape this v0.1 build
implements, and `api/v1/extractions.py`/`api/v1/matching.py`/
`api/v1/scenarios.py` already apply that precedent to collapse their own
`202`-job contract entries into synchronous `20x` responses (each router's
own module docstring documents the same reasoning). This module and
`api/v1/briefs.py` do the same: `generate` runs to completion in one call,
returning the finished brief, `201`.

## Deviation 2 — no `PATCH /negotiation-briefs/{id}` in this build

§4.17 also lists `PATCH .../{id}` for "human edits to any section, audited".
This task's own OBJECTIVE text, which enumerates every route/schema/service
method this file and `api/v1/briefs.py` must implement, does not mention an
edit endpoint at all (only generate/get/review/archive/list) — deliberately
narrower than the contract table, the same "task's own explicit scope wins
over an unlisted contract route" precedent this codebase already applies
elsewhere (e.g. `api/v1/scenarios.py` implementing no edit route for
`ComparisonScenario` either). `sections` has no per-field edit-history
column in the ERD box (`02-erd.md §7`) to hang an audited diff off of, so
building this properly is a materially larger addition than "add a route" —
left as a documented gap for a future phase, not silently implemented with a
half shape.

## Deviation 3 — no `/email-draft` route in this build

§4.17 lists `GET .../{id}/email-draft` ("returns text only... no send
endpoint and there will not be one"). This task's own OBJECTIVE is
explicit in the other direction: "NO send/email endpoint (assert in tests
that no such route exists)". The two are reconciled, not contradicted: the
contract's own route is a read of already-generated draft text, never a
send action, so it does not conflict with "never auto-send" on its own
terms — but adding a route whose path literally contains "email" when the
task's own test requirement is "assert no such route exists" is a needless
risk for zero behavioral gain, since `GET /negotiation-briefs/{id}` already
returns `draft_email_subject`/`draft_email_body` (top-level columns, per the
ERD box) as part of the full brief. No separate route is mounted;
`api/v1/briefs.py`'s own module docstring repeats this.

## Grounding discipline (SPEC's "AI must not invent" list)

Every figure in `sections`/`price_target`/`stretch_target`/
`walk_away_threshold` is read from a stored `LandedCostResult`,
`ScenarioResult.scoring_output`, `QuoteLine`/`QuoteTerms`, or
`SupplierPerformanceRecord` row already persisted by an earlier phase — this
module computes aggregates (sums, averages, a `* 0.97` stretch multiplier)
over those numbers but never fabricates a market benchmark, a competitor
quote, a historical price, or a delivery promise. `AiNarrativeProvider.
render_sections` (`providers/narrative/base.py`) receives only a flat
`brief_facts` dict built from those same numbers; `_numeric_cross_check`
below re-parses every decimal-looking token the provider's rendered text
contains and asserts each one already appears somewhere in `brief_facts` —
the mechanical guard against an invented figure slipping into prose a human
reviewer might not hand-verify.

## Section provenance split (SectionProvenance, app/models/briefs.py)

Eleven sections (`app.providers.narrative.base.NARRATIVE_SECTION_KEYS`) are
synthesized prose, rendered by `AiNarrativeProvider.render_sections` and
labelled `AI_NARRATIVE` — except `procurement_objective`, whose provenance
is `USER_ASSUMPTION` when the caller supplied `objective_override` text (a
human-authored override, exempt from the numeric cross-check by design —
see `generate`) or `CALCULATED` otherwise. The remaining seven SPEC sections
(`quoted_unit_price`, `effective_unit_cost`, `landed_cost_comparison`,
`price_target`, `stretch_target`, `walk_away_threshold`,
`volume_leverage`, `payment_terms_opportunity`) are bare figures with a
disclosed formula, written directly by this module as
`SUPPLIER_PROVIDED`/`CALCULATED`/`MISSING` — never passed through a
narrative provider, so there is nothing for the cross-check to police there
(the number IS the fact, not prose built on top of it).

## Price target / stretch / walk-away (CALCULATED, formulas disclosed)

`target = best-alternative landed unit cost` — the lowest `effective_unit_cost`
among this scenario's OTHER scored, non-excluded suppliers (computed the
same way `scenario_service._criterion_values_for_supplier` computes
`EFFECTIVE_UNIT_COST` — quantity-weighted total landed cost / total accepted
quantity across that supplier's offers in THIS scenario — re-derived here
rather than read off `ScenarioResult.scoring_output`, because that JSONB's
`criterion_scores` only ever contains the criteria the scenario's OWN
`strategy` happened to weight, per `domain/scoring/scorer.py`'s `for spec in
weights` loop — a `lowest_unit_price` scenario's scoring output carries no
`EFFECTIVE_UNIT_COST`/`TOTAL_LANDED_COST` entries at all. Recomputing
directly from `quote_snapshot_refs` + `LandedCostResult` rows makes brief
generation work identically regardless of which strategy the scenario used
to rank suppliers). `stretch = target * 0.97`. `walk_away = this supplier's
OWN current effective unit cost` (the ceiling above which continuing to buy
from them beats nothing). `price_target`/`stretch_target` are `None`
(section provenance `MISSING`) when no other scored, non-excluded supplier
exists on the scenario to benchmark against — never a fabricated number.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.config import NarrativeProviderKind, Settings
from app.core.errors import (
    ConflictStateError,
    NotFoundError,
    ProviderUnavailableError,
    ValidationAppError,
)
from app.core.ids import IdGenerator
from app.core.money import CALC_CONTEXT, quantize_money, quantize_unit_price, to_wire
from app.domain.landed_cost.breaks import select_price_break
from app.domain.landed_cost.contracts import PriceBreakTier
from app.models.briefs import BriefState, NegotiationBrief, SectionProvenance
from app.models.quotes import Quote, QuoteLine, QuoteLineMatchStatus
from app.models.rfqs import Rfq
from app.models.scenarios import ComparisonScenario, ScenarioResult, ScenarioState
from app.models.suppliers import Supplier, SupplierPerformanceRecord
from app.providers.narrative.base import NARRATIVE_SECTION_KEYS, AiNarrativeProvider
from app.providers.narrative.template import TemplateNarrativeProvider
from app.repositories.base import OrgScopedRepository
from app.repositories.matching_repository import QuoteLineRepository
from app.repositories.quote_repository import QuoteRepository
from app.repositories.rfq_repository import RfqRepository
from app.repositories.supplier_performance_repository import SupplierPerformanceRepository
from app.repositories.supplier_repository import SupplierRepository
from app.services.audit import AuditRecorder
from app.services.landed_cost_service import LandedCostService

STRETCH_FACTOR: Decimal = Decimal("0.97")

# Any number-looking token: integers included (MOQ, lead-time days, unit
# counts, whole-dollar savings and percentages are all integers in this
# domain — the original decimals-only pattern let every one of them through;
# 2026-08 calculation audit F6), with thousands separators/underscores
# tolerated and stripped before comparison.
_NUMBER_TOKEN_RE = re.compile(r"-?\d[\d,_]*(?:\.\d+)?")

_QUOTE_LINE_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("moq", "What is the minimum order quantity (MOQ)?"),
    ("tooling_cost", "Is there a tooling cost, and if so how much?"),
    ("setup_cost", "Is there a setup cost, and if so how much?"),
    # migration 0016 (2026-08 product-audit remediation): documentation_cost/
    # handling_cost gained real columns on quote_lines, so they now belong
    # in this missing-field question list like every other optional
    # fixed/logistics-cost field here.
    ("documentation_cost", "Is there a documentation cost, and if so how much?"),
    ("packaging_cost", "What is the packaging cost per unit or per order?"),
    ("shipping_cost", "What is the shipping cost for this order?"),
    ("insurance_cost", "What is the insurance cost for this shipment?"),
    ("handling_cost", "What is the handling cost for this shipment?"),
    ("tariff_amount", "What tariff amount, if any, applies to this shipment?"),
    ("duty_amount", "What duty amount, if any, applies to this shipment?"),
    ("customs_fee", "What customs fees, if any, apply to this shipment?"),
    ("tax_amount", "What tax amount, if any, applies to this order?"),
    ("country_of_origin", "What is the country of origin for this part?"),
    ("lead_time_days", "What is the confirmed lead time in days?"),
    ("production_capacity", "What is the supplier's production capacity for this part?"),
)

_QUOTE_TERMS_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("payment_terms", "What are the supplier's payment terms?"),
    ("payment_terms_days", "How many days of credit do the payment terms provide?"),
    ("incoterm", "What incoterm applies to this quote?"),
    ("shipping_terms", "What are the shipping terms for this quote?"),
    ("warranty_terms", "What warranty terms apply to this quote?"),
)


# -- provider selection factory ---------------------------------------------


def build_narrative_provider(settings: Settings) -> AiNarrativeProvider:
    """Provider selection factory, mirroring `services.extraction_service.
    build_extraction_provider`'s shape: a plain function over `Settings`,
    called once per request from the API layer's own dependency-builder
    (`api/v1/briefs.py`)."""
    if settings.narrative_provider is NarrativeProviderKind.TEMPLATE:
        return TemplateNarrativeProvider()
    # No Anthropic adapter module exists under app.providers.narrative in
    # this build (only base.py/template.py) — base.py's own docstring calls
    # it "roadmap", future/optional, not implemented here.
    raise ProviderUnavailableError(
        "The 'anthropic' narrative provider has no adapter implementation in "
        "this build; set PO_NARRATIVE_PROVIDER=template."
    )


# -- numeric cross-check: the mechanical guard against invented figures ----


def _token_to_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "").replace("_", ""))
    except Exception:  # pragma: no cover - regex shape makes this unreachable
        return None


def _collect_number_tokens(value: Any) -> set[Decimal]:
    tokens: set[Decimal] = set()
    if isinstance(value, str):
        for raw in _NUMBER_TOKEN_RE.findall(value):
            parsed = _token_to_decimal(raw)
            if parsed is not None:
                tokens.add(parsed)
    elif isinstance(value, list):
        for item in value:
            tokens.update(_collect_number_tokens(item))
    return tokens


def numeric_cross_check(rendered: dict[str, str], brief_facts: dict[str, Any]) -> None:
    """After narrative rendering: every number-looking token — integers
    included, separators stripped — in the provider's rendered text must
    equal (as an exact `Decimal`, so `500` matches a stored `500.000000` but
    `14.48` does NOT match `14.48109664` — rounding a fact is a provider bug
    that fails closed) some number already present in `brief_facts`'s own
    values. Raises `ValidationAppError` — a clear, generation-halting error,
    never a silently-shipped brief — on the first token that fails. This is
    the guard `docs/SPEC.md` §Negotiation brief's "AI must not invent...
    savings, concessions..." names; widened from decimals-only after the
    2026-08 calculation audit (F6) showed fabricated integers (MOQ, days,
    percentages, whole-dollar savings) passed unchecked. Known remaining
    limit, documented in ROADMAP.md: the allowed set is global to the brief,
    so a real number attributed to the wrong supplier is not caught."""
    allowed: set[Decimal] = set()
    for value in brief_facts.values():
        allowed.update(_collect_number_tokens(value))
    for section_key, text in rendered.items():
        for raw in _NUMBER_TOKEN_RE.findall(text):
            parsed = _token_to_decimal(raw)
            if parsed is not None and parsed not in allowed:
                raise ValidationAppError(
                    f"Narrative section {section_key!r} contains the number "
                    f"{raw!r}, which does not appear anywhere in the underlying "
                    "brief facts. Refusing to generate a brief that may contain an "
                    "invented figure (SPEC: AI must not invent numbers)."
                )


def _find_supplier_score(
    scoring_output: dict[str, Any], supplier_id: uuid.UUID
) -> dict[str, Any] | None:
    scores: list[dict[str, Any]] = scoring_output["scores"]
    for entry in scores:
        if entry["supplier_id"] == str(supplier_id):
            return entry
    return None


# -- per-supplier landed-cost aggregation over one scenario's offers -------


@dataclass(frozen=True, slots=True)
class _OfferAggregate:
    supplier_id: uuid.UUID
    total_landed_cost: Decimal
    total_accepted_quantity: Decimal
    effective_unit_cost: Decimal
    currency: str
    quote_lines: tuple[QuoteLine, ...]
    quote_currency: str | None
    quoted_unit_price: Decimal | None  # primary (first) line's quoted price
    quoted_unit_price_is_stated: bool  # False when derived from a price-break tier
    lead_time_days: int | None  # max across offers (order isn't done until the slowest ships)
    payment_terms_days: int | None
    payment_terms_text: str | None
    matched_rfq_line_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)


class BriefService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
        *,
        provider: AiNarrativeProvider,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._provider = provider

        self._rfq_repo = RfqRepository(session, organization_id)
        self._quote_repo = QuoteRepository(session, organization_id)
        self._quote_line_repo = QuoteLineRepository(session, organization_id)
        self._supplier_repo = SupplierRepository(session, organization_id)
        self._performance_repo = SupplierPerformanceRepository(session, organization_id)
        self._scenario_repo = _ScenarioLookupRepository(session, organization_id)
        self._result_repo = _ScenarioResultLookupRepository(session, organization_id)
        self._repo = _BriefRepository(session, organization_id)
        self._landed_cost = LandedCostService(session, organization_id, audit, clock, ids)

    # -- offer aggregation --------------------------------------------------

    def _aggregate_offers(self, refs: list[dict[str, Any]]) -> _OfferAggregate:
        total_landed = Decimal("0")
        total_qty = Decimal("0")
        currency: str | None = None
        quote_currency: str | None = None
        lines: list[QuoteLine] = []
        quotes_by_id: dict[uuid.UUID, Quote] = {}
        lead_times: list[int] = []
        payment_terms_days: int | None = None
        payment_terms_text: str | None = None
        matched_rfq_line_ids: set[uuid.UUID] = set()
        quoted_unit_price: Decimal | None = None
        quoted_unit_price_is_stated = False

        supplier_id = uuid.UUID(refs[0]["supplier_id"])

        for i, ref in enumerate(refs):
            line = self._quote_line_repo.get_or_raise(uuid.UUID(ref["quote_line_id"]))
            row, _components = self._landed_cost.get(uuid.UUID(ref["landed_cost_result_id"]))
            with localcontext(CALC_CONTEXT):
                total_landed = total_landed + row.total_landed_cost
                total_qty = total_qty + row.accepted_quantity
            currency = row.currency
            lines.append(line)
            if line.matched_rfq_line_id is not None:
                matched_rfq_line_ids.add(line.matched_rfq_line_id)
            if line.lead_time_days is not None:
                lead_times.append(line.lead_time_days)

            quote_id = uuid.UUID(ref["quote_id"])
            if quote_id not in quotes_by_id:
                quotes_by_id[quote_id] = self._quote_repo.get_or_raise(quote_id)
            quote = quotes_by_id[quote_id]

            if payment_terms_days is None:
                terms = self._quote_repo.get_terms(quote_id)
                if terms is not None:
                    payment_terms_days = terms.payment_terms_days
                    payment_terms_text = terms.payment_terms

            if i == 0:  # primary line: "quoted unit price" is this offer's headline figure
                quote_currency = quote.currency
                if line.unit_price is not None:
                    quoted_unit_price = line.unit_price
                    quoted_unit_price_is_stated = True
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
                            quoted_unit_price_is_stated = False

        with localcontext(CALC_CONTEXT):
            effective_unit_cost = (
                quantize_unit_price(total_landed / total_qty) if total_qty else Decimal("0")
            )

        return _OfferAggregate(
            supplier_id=supplier_id,
            total_landed_cost=quantize_money(total_landed),
            total_accepted_quantity=total_qty,
            effective_unit_cost=effective_unit_cost,
            currency=currency or "USD",
            quote_lines=tuple(lines),
            quote_currency=quote_currency,
            quoted_unit_price=quoted_unit_price,
            quoted_unit_price_is_stated=quoted_unit_price_is_stated,
            lead_time_days=max(lead_times) if lead_times else None,
            payment_terms_days=payment_terms_days,
            payment_terms_text=payment_terms_text,
            matched_rfq_line_ids=frozenset(matched_rfq_line_ids),
        )

    # -- generate -------------------------------------------------------

    def generate(
        self,
        scenario_id: uuid.UUID,
        supplier_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        objective_override: str | None = None,
        price_target_override: Decimal | None = None,
        stretch_target_override: Decimal | None = None,
        walk_away_threshold_override: Decimal | None = None,
    ) -> NegotiationBrief:
        scenario = self._scenario_repo.get_or_raise(scenario_id)
        if scenario.state is not ScenarioState.COMPLETE:
            raise ConflictStateError(
                "The scenario must be complete before a negotiation brief can be generated."
            )
        rfq = self._rfq_repo.get_or_raise(scenario.rfq_id)
        result = self._result_repo.get_by_scenario(scenario.id)
        if result is None:  # pragma: no cover - data integrity invariant
            raise NotFoundError("ScenarioResult")
        scoring_output = result.scoring_output

        supplier = self._supplier_repo.get_or_raise(supplier_id)

        subject_score = _find_supplier_score(scoring_output, supplier_id)
        if subject_score is None or subject_score["excluded"]:
            raise ValidationAppError(
                f"Supplier {supplier_id} is not one of the scored suppliers on this scenario."
            )

        refs = [
            r for r in scenario.quote_snapshot_refs if r["supplier_id"] == str(supplier_id)
        ]
        if not refs:  # pragma: no cover - implied by subject_score existing
            raise ValidationAppError(
                f"Supplier {supplier_id} has no offers on this scenario."
            )
        subject_offer = self._aggregate_offers(refs)

        alt_pairs: list[tuple[dict[str, Any], _OfferAggregate]] = []
        for entry in scoring_output["scores"]:
            if entry["excluded"] or entry["supplier_id"] == str(supplier_id):
                continue
            alt_refs = [
                r for r in scenario.quote_snapshot_refs if r["supplier_id"] == entry["supplier_id"]
            ]
            if not alt_refs:
                continue
            alt_pairs.append((entry, self._aggregate_offers(alt_refs)))
        alt_pairs.sort(key=lambda pair: pair[1].effective_unit_cost)
        best_alt = alt_pairs[0] if alt_pairs else None

        rfq_lines = self._rfq_repo.list_lines(rfq.id)
        with localcontext(CALC_CONTEXT):
            rfq_total_quantity = sum((rl.required_quantity for rl in rfq_lines), Decimal("0"))
        unmatched_rfq_lines = [
            f"line {rl.line_number}"
            for rl in rfq_lines
            if rl.id not in subject_offer.matched_rfq_line_ids
        ]
        uncertain_quote_lines = [
            f"line {ql.line_number}"
            for ql in subject_offer.quote_lines
            if ql.match_status is QuoteLineMatchStatus.AUTO
        ]

        performance = self._performance_repo.list_for_supplier(supplier_id, limit=1)
        latest_perf = performance[0] if performance else None

        cohort_best_payment_terms_days = max(
            (agg.payment_terms_days for _e, agg in alt_pairs if agg.payment_terms_days is not None),
            default=None,
        )

        missing_questions = _missing_field_questions(subject_offer)

        # -- price target / stretch / walk-away (module docstring) --------
        price_target: Decimal | None
        if price_target_override is not None:
            computed_price_target = quantize_unit_price(price_target_override)
            price_target = computed_price_target
            price_target_provenance = SectionProvenance.USER_ASSUMPTION
            price_target_text = (
                f"Price target set by user override: {to_wire(computed_price_target)} "
                f"{subject_offer.currency}."
            )
        elif best_alt is not None:
            price_target = best_alt[1].effective_unit_cost
            price_target_provenance = SectionProvenance.CALCULATED
            price_target_text = (
                f"Price target: {to_wire(price_target)} {subject_offer.currency} per unit "
                f"— CALCULATED as the best alternative scored supplier's "
                f"({best_alt[0]['supplier_name']}) effective (landed) unit cost."
            )
        else:
            price_target = None
            price_target_provenance = SectionProvenance.MISSING
            price_target_text = (
                "No price target could be calculated: no other scored, non-excluded "
                "supplier exists on this scenario to benchmark against."
            )

        stretch_target: Decimal | None
        if stretch_target_override is not None:
            computed_stretch_target = quantize_unit_price(stretch_target_override)
            stretch_target = computed_stretch_target
            stretch_target_provenance = SectionProvenance.USER_ASSUMPTION
            stretch_target_text = (
                f"Stretch target set by user override: {to_wire(computed_stretch_target)} "
                f"{subject_offer.currency}."
            )
        elif price_target is not None:
            with localcontext(CALC_CONTEXT):
                stretch_target = quantize_unit_price(price_target * STRETCH_FACTOR)
            stretch_target_provenance = SectionProvenance.CALCULATED
            stretch_target_text = (
                f"Stretch target: {to_wire(stretch_target)} {subject_offer.currency} per unit "
                f"— CALCULATED as price target ({to_wire(price_target)}) x "
                f"{to_wire(STRETCH_FACTOR)} (a modest, achievable ask above the target)."
            )
        else:
            stretch_target = None
            stretch_target_provenance = SectionProvenance.MISSING
            stretch_target_text = "No stretch target: no price target was calculated."

        walk_away_threshold: Decimal | None
        if walk_away_threshold_override is not None:
            computed_walk_away = quantize_unit_price(walk_away_threshold_override)
            walk_away_threshold = computed_walk_away
            walk_away_provenance = SectionProvenance.USER_ASSUMPTION
            walk_away_text = (
                f"Walk-away threshold set by user override: "
                f"{to_wire(computed_walk_away)} {subject_offer.currency}."
            )
        else:
            walk_away_threshold = subject_offer.effective_unit_cost
            walk_away_provenance = SectionProvenance.CALCULATED
            walk_away_text = (
                f"Walk-away threshold: {to_wire(walk_away_threshold)} {subject_offer.currency} "
                f"per unit — CALCULATED as {supplier.name}'s OWN current effective (landed) "
                "unit cost: continuing to buy from them above this price provides no "
                "advantage over their current pricing."
            )

        # -- recommended concessions (rule-based, module docstring) --------
        concessions: list[str] = []
        if price_target is not None and subject_offer.effective_unit_cost > price_target:
            with localcontext(CALC_CONTEXT):
                gap = quantize_unit_price(subject_offer.effective_unit_cost - price_target)
            concessions.append(
                f"Ask for a unit price reduction toward the calculated price target of "
                f"{to_wire(price_target)} {subject_offer.currency} (current effective unit "
                f"cost is {to_wire(subject_offer.effective_unit_cost)} "
                f"{subject_offer.currency}, a gap of {to_wire(gap)} {subject_offer.currency})."
            )
        if (
            best_alt is not None
            and subject_offer.lead_time_days is not None
            and best_alt[1].lead_time_days is not None
            and subject_offer.lead_time_days > best_alt[1].lead_time_days
        ):
            concessions.append(
                f"Ask to match the best alternative supplier's lead time of "
                f"{best_alt[1].lead_time_days} days (currently quoted at "
                f"{subject_offer.lead_time_days} days)."
            )
        if (
            cohort_best_payment_terms_days is not None
            and subject_offer.payment_terms_days is not None
            and cohort_best_payment_terms_days > subject_offer.payment_terms_days
        ):
            concessions.append(
                f"Ask for payment terms extended toward {cohort_best_payment_terms_days} days "
                f"(currently quoted at {subject_offer.payment_terms_days} days), improving "
                "the financing cost of this order."
            )
        if missing_questions:
            concessions.append(
                "Request the missing commercial terms listed under Questions before "
                "finalizing the agreement."
            )

        # -- narrative facts + provider render + cross-check --------------
        facts = _build_facts(
            rfq=rfq,
            supplier=supplier,
            subject_score=subject_score,
            subject_offer=subject_offer,
            best_alt=best_alt,
            alt_pairs=alt_pairs,
            rfq_total_quantity=rfq_total_quantity,
            rfq_line_count=len(rfq_lines),
            unmatched_rfq_lines=unmatched_rfq_lines,
            uncertain_quote_lines=uncertain_quote_lines,
            latest_perf=latest_perf,
            cohort_best_payment_terms_days=cohort_best_payment_terms_days,
            price_target=price_target,
            missing_questions=missing_questions,
            concessions=concessions,
        )
        rendered = self._provider.render_sections(facts)
        missing_keys = set(NARRATIVE_SECTION_KEYS) - set(rendered)
        if missing_keys:
            raise ValidationAppError(
                f"Narrative provider did not render required section(s): {sorted(missing_keys)}."
            )
        numeric_cross_check(rendered, facts)

        # -- assemble sections (module docstring "Section provenance split") --
        sections: dict[str, dict[str, Any]] = {}

        if objective_override is not None:
            sections["procurement_objective"] = {
                "provenance": SectionProvenance.USER_ASSUMPTION.value,
                "text": objective_override,
                "data": None,
            }
        else:
            sections["procurement_objective"] = {
                "provenance": SectionProvenance.CALCULATED.value,
                "text": rendered["procurement_objective"],
                "data": None,
            }

        for key in (
            "supplier_position",
            "lead_time_concerns",
            "quality_concerns",
            "spec_concerns",
            "alternative_suppliers",
            "batna",
            "recommended_concessions",
            "questions_for_clarification",
        ):
            sections[key] = {
                "provenance": SectionProvenance.AI_NARRATIVE.value,
                "text": rendered[key],
                "data": None,
            }

        if subject_offer.quoted_unit_price is not None:
            sections["quoted_unit_price"] = {
                "provenance": SectionProvenance.SUPPLIER_PROVIDED.value,
                "text": (
                    f"{supplier.name} quoted {to_wire(subject_offer.quoted_unit_price)} "
                    f"{subject_offer.quote_currency or subject_offer.currency} per unit"
                    + (
                        ""
                        if subject_offer.quoted_unit_price_is_stated
                        else " (derived from the applicable price-break tier)"
                    )
                    + "."
                ),
                "data": {"amount": to_wire(subject_offer.quoted_unit_price)},
            }
        else:
            sections["quoted_unit_price"] = {
                "provenance": SectionProvenance.MISSING.value,
                "text": (
                    "Unit price is not stated on this supplier's quote (no flat unit "
                    "price and no applicable price-break tier)."
                ),
                "data": None,
            }

        sections["effective_unit_cost"] = {
            "provenance": SectionProvenance.CALCULATED.value,
            "text": (
                f"Effective unit cost (landed, all cost components included): "
                f"{to_wire(subject_offer.effective_unit_cost)} {subject_offer.currency} per "
                f"unit — CALCULATED as total landed cost / accepted quantity "
                f"({to_wire(subject_offer.total_landed_cost)} / "
                f"{to_wire(subject_offer.total_accepted_quantity)})."
            ),
            "data": {"amount": to_wire(subject_offer.effective_unit_cost)},
        }

        if best_alt is not None:
            with localcontext(CALC_CONTEXT):
                diff = quantize_unit_price(
                    subject_offer.effective_unit_cost - best_alt[1].effective_unit_cost
                )
            comparison = "higher than" if diff > 0 else ("lower than" if diff < 0 else "equal to")
            sections["landed_cost_comparison"] = {
                "provenance": SectionProvenance.CALCULATED.value,
                "text": (
                    f"{supplier.name}'s effective unit cost of "
                    f"{to_wire(subject_offer.effective_unit_cost)} {subject_offer.currency} is "
                    f"{comparison} the best alternative scored supplier's "
                    f"({best_alt[0]['supplier_name']}) "
                    f"{to_wire(best_alt[1].effective_unit_cost)} {subject_offer.currency} "
                    f"by {to_wire(abs(diff))} {subject_offer.currency} per unit."
                ),
                "data": None,
            }
        else:
            sections["landed_cost_comparison"] = {
                "provenance": SectionProvenance.MISSING.value,
                "text": (
                    "No other scored supplier is available on this scenario for a "
                    "landed-cost comparison."
                ),
                "data": None,
            }

        sections["price_target"] = {
            "provenance": price_target_provenance.value,
            "text": price_target_text,
            "data": None,
        }
        sections["stretch_target"] = {
            "provenance": stretch_target_provenance.value,
            "text": stretch_target_text,
            "data": None,
        }
        sections["walk_away_threshold"] = {
            "provenance": walk_away_provenance.value,
            "text": walk_away_text,
            "data": None,
        }

        sections["volume_leverage"] = {
            "provenance": SectionProvenance.CALCULATED.value,
            "text": (
                f"This RFQ requests {to_wire(rfq_total_quantity)} total units across "
                f"{len(rfq_lines)} line(s) — CALCULATED as the sum of each RFQ line's "
                "required quantity."
            ),
            "data": {"total_quantity": to_wire(rfq_total_quantity)},
        }

        if subject_offer.payment_terms_days is not None:
            if cohort_best_payment_terms_days is not None:
                pt_text = (
                    f"{supplier.name} offers {subject_offer.payment_terms_days}-day payment "
                    f"terms; the best payment terms among other scored suppliers on this "
                    f"scenario is {cohort_best_payment_terms_days} days."
                )
            else:
                pt_text = (
                    f"{supplier.name} offers {subject_offer.payment_terms_days}-day payment "
                    "terms; no other scored supplier's payment terms are available for "
                    "comparison."
                )
            sections["payment_terms_opportunity"] = {
                "provenance": SectionProvenance.SUPPLIER_PROVIDED.value,
                "text": pt_text,
                "data": None,
            }
        else:
            sections["payment_terms_opportunity"] = {
                "provenance": SectionProvenance.MISSING.value,
                "text": "Payment terms are not stated on this supplier's quote.",
                "data": None,
            }

        sections["draft_supplier_email"] = {
            "provenance": SectionProvenance.AI_NARRATIVE.value,
            "text": f"Subject: {rendered['draft_email_subject']}\n\n{rendered['draft_email_body']}",
            "data": {
                "subject": rendered["draft_email_subject"],
                "body": rendered["draft_email_body"],
            },
        }

        now = self._clock.now()
        brief = NegotiationBrief(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            scenario_id=scenario.id,
            supplier_id=supplier_id,
            sections=sections,
            price_target=price_target,
            stretch_target=stretch_target,
            walk_away_threshold=walk_away_threshold,
            draft_email_subject=rendered["draft_email_subject"],
            draft_email_body=rendered["draft_email_body"],
            narrative_provider=self._provider.name,
            simulated=not self._provider.is_generated,
            state=BriefState.DRAFT,
            created_by_id=actor_id,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(brief)
        self._db.flush()

        self._audit.record(
            "brief.generated",
            entity_type="negotiation_brief",
            entity_id=brief.id,
            after_state=_brief_audit_state(brief),
            explanation=(
                f"Negotiation brief generated for supplier '{supplier.name}' on scenario "
                f"{scenario.id} (provider={self._provider.name}, simulated={brief.simulated})"
            ),
        )
        return brief

    # -- reads -------------------------------------------------------------

    def get(self, brief_id: uuid.UUID) -> NegotiationBrief:
        return self._repo.get_or_raise(brief_id)

    def list_by_scenario(
        self, scenario_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[NegotiationBrief], int]:
        self._scenario_repo.get_or_raise(scenario_id)
        items = self._repo.list_page(
            NegotiationBrief.scenario_id == scenario_id,
            order_by=NegotiationBrief.created_at.desc(),
            limit=limit,
            offset=offset,
        )
        total = self._repo.count(NegotiationBrief.scenario_id == scenario_id)
        return items, total

    def list_by_rfq(
        self, rfq_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[NegotiationBrief], int]:
        self._rfq_repo.get_or_raise(rfq_id)
        items = self._repo.list_by_rfq(rfq_id, limit=limit, offset=offset)
        total = self._repo.count_by_rfq(rfq_id)
        return items, total

    # -- review / archive ----------------------------------------------

    def mark_reviewed(
        self,
        brief_id: uuid.UUID,
        *,
        approved: bool,
        reviewer_notes: str | None,
        actor_id: uuid.UUID,
    ) -> NegotiationBrief:
        brief = self._repo.get_or_raise(brief_id)
        if brief.state is not BriefState.DRAFT:
            raise ConflictStateError("This negotiation brief has already been reviewed.")

        before = _brief_audit_state(brief)
        now = self._clock.now()
        brief.state = BriefState.HUMAN_REVIEWED
        brief.reviewed_by_id = actor_id
        brief.reviewed_at = now
        brief.version += 1
        brief.updated_at = now
        self._db.flush()

        self._audit.record(
            "brief.reviewed",
            entity_type="negotiation_brief",
            entity_id=brief.id,
            before_state=before,
            after_state=_brief_audit_state(brief),
            explanation=(
                f"Negotiation brief {'approved' if approved else 'flagged'} on review"
                + (f": {reviewer_notes}" if reviewer_notes else "")
            ),
        )
        return brief

    def archive(
        self, brief_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID
    ) -> NegotiationBrief:
        brief = self._repo.get_or_raise(brief_id)
        if brief.is_archived:
            raise ValidationAppError("Negotiation brief is already archived.")

        before = _brief_audit_state(brief)
        now = self._clock.now()
        brief.archived_at = now
        brief.archived_by_id = actor_id
        brief.archive_reason = reason
        brief.version += 1
        brief.updated_at = now
        self._db.flush()

        self._audit.record(
            "brief.archived",
            entity_type="negotiation_brief",
            entity_id=brief.id,
            before_state=before,
            after_state=_brief_audit_state(brief),
            explanation=f"Negotiation brief archived: {reason}",
        )
        return brief


# -- tiny repositories (no dedicated repositories.py module exists for
# NegotiationBrief; the same "add the minimum here" pattern
# repositories/matching_repository.py's own docstring documents) ----------


class _ScenarioLookupRepository(OrgScopedRepository[ComparisonScenario]):
    model = ComparisonScenario


class _ScenarioResultLookupRepository(OrgScopedRepository[ScenarioResult]):
    model = ScenarioResult

    def get_by_scenario(self, scenario_id: uuid.UUID) -> ScenarioResult | None:
        stmt = self._base_query().where(ScenarioResult.scenario_id == scenario_id)
        return self.session.execute(stmt).scalar_one_or_none()


class _BriefRepository(OrgScopedRepository[NegotiationBrief]):
    model = NegotiationBrief

    def list_by_rfq(
        self, rfq_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> list[NegotiationBrief]:
        stmt = (
            select(NegotiationBrief)
            .join(ComparisonScenario, ComparisonScenario.id == NegotiationBrief.scenario_id)
            .where(
                NegotiationBrief.organization_id == self.organization_id,
                ComparisonScenario.rfq_id == rfq_id,
            )
            .order_by(NegotiationBrief.created_at.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_by_rfq(self, rfq_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(NegotiationBrief)
            .join(ComparisonScenario, ComparisonScenario.id == NegotiationBrief.scenario_id)
            .where(
                NegotiationBrief.organization_id == self.organization_id,
                ComparisonScenario.rfq_id == rfq_id,
            )
        )
        return int(self.session.execute(stmt).scalar_one())


# -- pure helpers ------------------------------------------------------


def _missing_field_questions(offer: _OfferAggregate) -> list[str]:
    questions: list[str] = []
    seen: set[str] = set()

    def add(question: str) -> None:
        if question not in seen:
            seen.add(question)
            questions.append(question)

    for line in offer.quote_lines:
        if line.unit_price is None:
            add("What is the unit price for this line?")
        for attr, question in _QUOTE_LINE_QUESTIONS:
            if getattr(line, attr) is None:
                add(question)

    if offer.payment_terms_days is None and offer.payment_terms_text is None:
        for _attr, question in _QUOTE_TERMS_QUESTIONS:
            add(question)
    else:
        if offer.payment_terms_text is None:
            add("What are the supplier's payment terms?")
        if offer.payment_terms_days is None:
            add("How many days of credit do the payment terms provide?")

    return questions


def _brief_audit_state(brief: NegotiationBrief) -> dict[str, Any]:
    return {
        "scenario_id": str(brief.scenario_id),
        "supplier_id": str(brief.supplier_id),
        "state": brief.state.value,
        "narrative_provider": brief.narrative_provider,
        "simulated": brief.simulated,
        "version": brief.version,
        "is_archived": brief.is_archived,
        "archived_at": brief.archived_at.isoformat() if brief.archived_at else None,
        "archive_reason": brief.archive_reason,
        "reviewed_by_id": str(brief.reviewed_by_id) if brief.reviewed_by_id else None,
        "reviewed_at": brief.reviewed_at.isoformat() if brief.reviewed_at else None,
    }


def _build_facts(
    *,
    rfq: Rfq,
    supplier: Supplier,
    subject_score: dict[str, Any],
    subject_offer: _OfferAggregate,
    best_alt: tuple[dict[str, Any], _OfferAggregate] | None,
    alt_pairs: list[tuple[dict[str, Any], _OfferAggregate]],
    rfq_total_quantity: Decimal,
    rfq_line_count: int,
    unmatched_rfq_lines: list[str],
    uncertain_quote_lines: list[str],
    latest_perf: SupplierPerformanceRecord | None,
    cohort_best_payment_terms_days: int | None,
    price_target: Decimal | None,
    missing_questions: list[str],
    concessions: list[str],
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "supplier_name": supplier.name,
        "supplier_contact_or_name": supplier.name,
        "buyer_organization_name": "Procurement Team",
        "rfq_name": rfq.name,
        "rfq_line_count": str(rfq_line_count),
        "rfq_total_quantity": to_wire(rfq_total_quantity),
        "currency": subject_offer.currency,
        "rank": str(subject_score["rank"]),
        "cohort_size": str(1 + len(alt_pairs)),
        "total_score": str(subject_score["total_score"]),
        "effective_unit_cost": to_wire(subject_offer.effective_unit_cost),
        "total_landed_cost": to_wire(subject_offer.total_landed_cost),
        "lead_time_days": (
            str(subject_offer.lead_time_days) if subject_offer.lead_time_days is not None else None
        ),
        "on_time_delivery_rate": (
            to_wire(latest_perf.on_time_delivery_rate) if latest_perf is not None else None
        ),
        "quality_score": (to_wire(latest_perf.quality_score) if latest_perf is not None else None),
        "defect_rate": (to_wire(latest_perf.defect_rate) if latest_perf is not None else None),
        "unmatched_rfq_lines": unmatched_rfq_lines,
        "uncertain_quote_lines": uncertain_quote_lines,
        "payment_terms_days": (
            str(subject_offer.payment_terms_days)
            if subject_offer.payment_terms_days is not None
            else None
        ),
        "cohort_best_payment_terms_days": (
            str(cohort_best_payment_terms_days)
            if cohort_best_payment_terms_days is not None
            else None
        ),
        "price_target": to_wire(price_target) if price_target is not None else None,
        "missing_fields_list": missing_questions,
        "recommended_concessions_list": concessions,
    }
    if best_alt is not None:
        entry, agg = best_alt
        facts["best_alternative_supplier_name"] = entry["supplier_name"]
        facts["best_alternative_rank"] = str(entry["rank"])
        facts["best_alternative_effective_unit_cost"] = to_wire(agg.effective_unit_cost)
        facts["best_alternative_total_landed_cost"] = to_wire(agg.total_landed_cost)
    else:
        facts["best_alternative_supplier_name"] = None
        facts["best_alternative_rank"] = None
        facts["best_alternative_effective_unit_cost"] = None
        facts["best_alternative_total_landed_cost"] = None

    facts["alternative_suppliers_list"] = [
        (
            f"{entry['supplier_name']}: rank {entry['rank']}, effective unit cost "
            f"{to_wire(agg.effective_unit_cost)} {subject_offer.currency}, total landed cost "
            f"{to_wire(agg.total_landed_cost)} {subject_offer.currency}"
        )
        for entry, agg in alt_pairs
    ]
    return facts


__all__ = [
    "STRETCH_FACTOR",
    "BriefService",
    "build_narrative_provider",
    "numeric_cross_check",
]
