"""Synthetic demonstration dataset integration test (docs/SPEC.md
§Synthetic demonstration dataset).

Runs `app.seed.demo_dataset.seed_demo_dataset` against a fresh, migrated
database (the `db` fixture: a session wrapped in an outer transaction that
is always rolled back — see tests/conftest.py) and asserts every item on the
SPEC's checklist, including that running the seed a second time on top of
itself is a true no-op (idempotency).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session as SaSession

from app.core.clock import FrozenClock
from app.core.ids import SequentialIdGenerator
from app.domain.landed_cost.contracts import CostComponent
from app.domain.optimization.contracts import AllocationStatus
from app.models.analysis import LandedCostComponent, LandedCostResult
from app.models.audit import AuditEvent
from app.models.boms import BillOfMaterialLine, BillOfMaterials, BomStatus
from app.models.documents import (
    ExtractionField,
    ExtractionRun,
    ExtractionRunState,
    PartMatchCandidate,
    QuoteCorrection,
)
from app.models.parts import Part, PartAlternative
from app.models.quotes import Quote, QuoteLine, QuotePriceBreak, QuoteTerms
from app.models.rfqs import Rfq, RfqStatus, RfqSupplier
from app.models.scenarios import AllocationResultRecord, ScenarioResult
from app.models.suppliers import Supplier
from app.providers.extraction.mock import MockExtractionProvider
from app.providers.storage.memory import MemoryStorageProvider
from app.seed.demo_dataset import (
    QUALITY_FIRST_CONFIG_NAME,
    SCENARIO_INFEASIBLE_NAME,
    SCENARIO_LOWEST_LANDED_COST_NAME,
    SCENARIO_SPLIT_NAME,
    DemoDatasetSummary,
    seed_demo_dataset,
)

_CLOCK = FrozenClock(datetime(2026, 8, 10, tzinfo=UTC))
_SHENZHEN = "SHENZHEN-PREC"
_BALTIC = "BALTIC-CASTING"
_CASCADE = "CASCADE-PRECISION"
_PACIFIC = "PACIFIC-METAL"
_ENCLOSURE_SUPPLIERS = (_SHENZHEN, _BALTIC, _CASCADE, _PACIFIC)


def _run_seed(session: SaSession) -> DemoDatasetSummary:
    return seed_demo_dataset(
        session,
        clock=_CLOCK,
        ids=SequentialIdGenerator(),
        storage=MemoryStorageProvider(),
        extraction_provider=MockExtractionProvider(),
    )


def _quote_line(session: SaSession, quote_id: uuid.UUID, quoted_part_number: str) -> QuoteLine:
    line = session.execute(
        select(QuoteLine).where(
            QuoteLine.quote_id == quote_id,
            QuoteLine.quoted_part_number == quoted_part_number,
        )
    ).scalar_one()
    return line


def _landed_cost_figures(
    session: SaSession, quote_line_id: uuid.UUID
) -> tuple[Decimal, Decimal]:
    """(normalized_unit_price, effective_unit_cost) for the one persisted
    `LandedCostResult` computed against `quote_line_id` during scenario
    creation."""
    result = session.execute(
        select(LandedCostResult).where(LandedCostResult.quote_line_id == quote_line_id)
    ).scalar_one()
    material = session.execute(
        select(LandedCostComponent).where(
            LandedCostComponent.landed_cost_result_id == result.id,
            LandedCostComponent.component == CostComponent.EXTENDED_MATERIAL,
        )
    ).scalar_one()
    normalized_unit_price = Decimal(material.inputs["normalized_unit_price"])
    return normalized_unit_price, result.effective_unit_cost


def test_seed_demo_dataset_is_idempotent_and_meets_spec_checklist(db: SaSession) -> None:
    session = db

    summary_1 = _run_seed(session)
    summary_2 = _run_seed(session)

    # -- idempotency: identical ids/counts on re-run --------------------
    assert summary_1 == summary_2
    org_id = summary_1.organization_id

    # -- 1 demo org, >=6 suppliers, >=15 parts ---------------------------
    org_supplier_count = session.execute(
        select(func.count()).select_from(Supplier).where(Supplier.organization_id == org_id)
    ).scalar_one()
    assert org_supplier_count >= 6
    assert len(summary_1.supplier_ids) == org_supplier_count

    org_part_count = session.execute(
        select(func.count()).select_from(Part).where(Part.organization_id == org_id)
    ).scalar_one()
    assert org_part_count >= 15
    assert len(summary_1.part_ids) == org_part_count

    # currencies: at least 3 distinct supported_currencies across suppliers
    currencies: set[str] = set()
    for supplier_id in summary_1.supplier_ids.values():
        supplier = session.get(Supplier, supplier_id)
        assert supplier is not None
        currencies.update(supplier.supported_currencies)
    assert len(currencies) >= 3
    assert {"USD", "EUR", "CNY", "MXN", "SEK"} <= currencies

    # lead times span 7-45 days
    lead_times = [
        session.get(Supplier, sid).typical_lead_time_days for sid in summary_1.supplier_ids.values()
    ]
    assert min(t for t in lead_times if t is not None) == 7
    assert max(t for t in lead_times if t is not None) == 45

    # a kg-based part exists (unit-conversion demo)
    gasket = session.execute(
        select(Part).where(
            Part.organization_id == org_id, Part.internal_part_number == "MF-GASK-510"
        )
    ).scalar_one()
    assert gasket.unit_definition_id == summary_1.unit_ids["kg"]

    # approved alternatives, including one external-MPN alternative
    alternatives = session.execute(
        select(PartAlternative).where(PartAlternative.organization_id == org_id)
    ).scalars().all()
    assert len(alternatives) >= 3
    external = [a for a in alternatives if a.alternative_part_id is None]
    assert any(a.alternative_mpn == "SKF-BH-2205" for a in external)

    # -- 2 BOMs, one with an active v2 superseding v1, one with a
    # substitute part on a line --------------------------------------
    rk200_v1 = session.get(BillOfMaterials, summary_1.bom_ids["RK-200:v1"])
    rk200_v2 = session.get(BillOfMaterials, summary_1.bom_ids["RK-200:v2"])
    en50_v1 = session.get(BillOfMaterials, summary_1.bom_ids["EN-50:v1"])
    assert rk200_v1 is not None and rk200_v2 is not None and en50_v1 is not None
    assert rk200_v1.status == BomStatus.SUPERSEDED
    assert rk200_v2.status == BomStatus.ACTIVE
    assert rk200_v2.previous_version_id == rk200_v1.id
    assert en50_v1.status == BomStatus.ACTIVE

    en50_lines = session.execute(
        select(BillOfMaterialLine).where(BillOfMaterialLine.bom_id == en50_v1.id)
    ).scalars().all()
    assert 5 <= len(en50_lines) <= 8
    assert any(line.substitute_part_id is not None for line in en50_lines)
    rk200_v2_lines = session.execute(
        select(BillOfMaterialLine).where(BillOfMaterialLine.bom_id == rk200_v2.id)
    ).scalars().all()
    assert 5 <= len(rk200_v2_lines) <= 8

    # -- 3 RFQs with the required statuses -------------------------------
    rfq_a = session.get(Rfq, summary_1.rfq_ids["RFQ-2026-Q3-RACK"])
    rfq_b = session.get(Rfq, summary_1.rfq_ids["RFQ-2026-ENC-PILOT"])
    rfq_c = session.get(Rfq, summary_1.rfq_ids["RFQ-2026-LEGACY-GASKET"])
    assert rfq_a is not None and rfq_b is not None and rfq_c is not None
    assert rfq_a.status == RfqStatus.OPEN
    assert rfq_b.status == RfqStatus.UNDER_REVIEW
    assert rfq_c.status == RfqStatus.DRAFT

    # RFQ (a): all six suppliers invited, one excluded with a reason
    rfq_a_suppliers = session.execute(
        select(RfqSupplier).where(RfqSupplier.rfq_id == rfq_a.id)
    ).scalars().all()
    assert len(rfq_a_suppliers) == 6
    excluded = [rs for rs in rfq_a_suppliers if rs.excluded_at is not None]
    assert len(excluded) == 1
    assert excluded[0].supplier_id == summary_1.supplier_ids[_PACIFIC]
    assert excluded[0].exclusion_reason

    # -- 4 manual quotes on RFQ (b) --------------------------------------
    assert len(summary_1.quote_ids) == 4
    for code in _ENCLOSURE_SUPPLIERS:
        quote = session.get(Quote, summary_1.quote_ids[code])
        assert quote is not None
        assert quote.rfq_id == rfq_b.id

    # missing commercial term: Baltic's quote has no payment terms
    baltic_terms = session.execute(
        select(QuoteTerms).where(QuoteTerms.quote_id == summary_1.quote_ids[_BALTIC])
    ).scalar_one()
    assert baltic_terms.payment_terms is None

    # different currencies across the four quotes, at least USD/EUR/CNY
    quote_currencies = {
        session.get(Quote, summary_1.quote_ids[code]).currency for code in _ENCLOSURE_SUPPLIERS
    }
    assert {"USD", "EUR", "CNY"} <= quote_currencies
    assert len(quote_currencies) == 4

    # SPEC §11's own example price-break table, verbatim, on Cascade's
    # mounting-plate line
    cascade_plate_line = _quote_line(session, summary_1.quote_ids[_CASCADE], "MF-PLT-021")
    cascade_breaks = session.execute(
        select(QuotePriceBreak)
        .where(QuotePriceBreak.quote_line_id == cascade_plate_line.id)
        .order_by(QuotePriceBreak.min_quantity)
    ).scalars().all()
    assert [(b.min_quantity, b.max_quantity, b.unit_price) for b in cascade_breaks] == [
        (Decimal("1.000000"), Decimal("99.000000"), Decimal("12.00000000")),
        (Decimal("100.000000"), Decimal("499.000000"), Decimal("10.50000000")),
        (Decimal("500.000000"), Decimal("999.000000"), Decimal("9.20000000")),
        (Decimal("1000.000000"), None, Decimal("8.60000000")),
    ]

    # conflicting MOQs across suppliers on the same line
    moqs = {
        code: _quote_line(session, summary_1.quote_ids[code], "MF-CAST-300").moq
        for code in _ENCLOSURE_SUPPLIERS
    }
    assert len(set(moqs.values())) > 1

    # lead-time spread on the same line
    lead_times_on_line1 = {
        code: _quote_line(session, summary_1.quote_ids[code], "MF-CAST-300").lead_time_days
        for code in _ENCLOSURE_SUPPLIERS
    }
    assert max(lead_times_on_line1.values()) - min(lead_times_on_line1.values()) >= 20

    # -- one uncertain part match -----------------------------------------
    pacific_gasket_line = _quote_line(session, summary_1.quote_ids[_PACIFIC], "MFGASK510")
    candidates = session.execute(
        select(PartMatchCandidate).where(
            PartMatchCandidate.quote_line_id == pacific_gasket_line.id
        )
    ).scalars().all()
    assert candidates
    assert all(not c.human_confirmed for c in candidates)
    assert pacific_gasket_line.matched_rfq_line_id is None

    # -- price-inversion: lowest unit price supplier is NOT lowest landed
    # cost (verified numerically via the landed-cost results the scenario
    # service persisted) ------------------------------------------------
    figures: dict[str, tuple[Decimal, Decimal]] = {}
    for code in _ENCLOSURE_SUPPLIERS:
        line = _quote_line(session, summary_1.quote_ids[code], "MF-CAST-300")
        figures[code] = _landed_cost_figures(session, line.id)

    lowest_unit_price_supplier = min(figures, key=lambda code: figures[code][0])
    lowest_landed_cost_supplier = min(figures, key=lambda code: figures[code][1])
    assert lowest_unit_price_supplier == _SHENZHEN
    assert lowest_landed_cost_supplier != _SHENZHEN
    assert figures[_SHENZHEN][1] > figures[lowest_landed_cost_supplier][1]

    # -- scenarios: 3 persisted, feasible/infeasible/split ----------------
    assert len(summary_1.scenario_ids) == 3

    lowest_landed_alloc = session.execute(
        select(AllocationResultRecord).where(
            AllocationResultRecord.scenario_id
            == summary_1.scenario_ids[SCENARIO_LOWEST_LANDED_COST_NAME]
        )
    ).scalar_one()
    assert lowest_landed_alloc.status in (AllocationStatus.OPTIMAL, AllocationStatus.FEASIBLE)

    lowest_landed_result = session.execute(
        select(ScenarioResult).where(
            ScenarioResult.scenario_id
            == summary_1.scenario_ids[SCENARIO_LOWEST_LANDED_COST_NAME]
        )
    ).scalar_one()
    scores = lowest_landed_result.scoring_output["scores"]
    top = next(s for s in scores if s["rank"] == 1 and not s["excluded"])
    shenzhen_name = session.get(Supplier, summary_1.supplier_ids[_SHENZHEN]).name
    assert top["supplier_name"] != shenzhen_name

    infeasible_alloc = session.execute(
        select(AllocationResultRecord).where(
            AllocationResultRecord.scenario_id == summary_1.scenario_ids[SCENARIO_INFEASIBLE_NAME]
        )
    ).scalar_one()
    assert infeasible_alloc.status == AllocationStatus.INFEASIBLE
    assert infeasible_alloc.infeasibility is not None

    split_alloc = session.execute(
        select(AllocationResultRecord).where(
            AllocationResultRecord.scenario_id == summary_1.scenario_ids[SCENARIO_SPLIT_NAME]
        )
    ).scalar_one()
    assert split_alloc.status in (AllocationStatus.OPTIMAL, AllocationStatus.FEASIBLE)
    assert split_alloc.supplier_count >= 2

    # -- scoring configurations: sample + custom "Quality-first" ----------
    assert "Sample weights (demonstration)" in summary_1.scoring_configuration_ids
    assert QUALITY_FIRST_CONFIG_NAME in summary_1.scoring_configuration_ids

    # -- FX manual overrides for every currency pair used ------------------
    assert {"USD/CNY", "USD/EUR", "USD/MXN"} <= summary_1.exchange_rate_ids.keys()

    # -- documents + extraction (RFQ (a)) ----------------------------------
    assert len(summary_1.document_ids) == 4
    assert summary_1.extraction_run_id is not None
    run = session.get(ExtractionRun, summary_1.extraction_run_id)
    assert run is not None
    assert run.raw_response is not None
    assert run.raw_response["injection_scan"]["suspected"] is True

    fields = session.execute(
        select(ExtractionField).where(ExtractionField.extraction_run_id == run.id)
    ).scalars().all()
    assert any(f.injection_flagged for f in fields)
    assert any(f.confidence < Decimal("0.95") for f in fields)

    injection_audit = session.execute(
        select(AuditEvent).where(
            AuditEvent.organization_id == org_id,
            AuditEvent.event_type == "security.injection_suspected",
            AuditEvent.entity_id == run.id,
        )
    ).scalar_one_or_none()
    assert injection_audit is not None

    # -- one extraction correction; run left in needs_review, not
    # materialized ---------------------------------------------------------
    corrections = session.execute(
        select(QuoteCorrection).where(QuoteCorrection.extraction_run_id == run.id)
    ).scalars().all()
    assert len(corrections) == 1
    assert corrections[0].quote_id is None  # not materialized
    assert run.state == ExtractionRunState.NEEDS_REVIEW

    # -- audit events exist for seeded mutations --------------------------
    audit_count = session.execute(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.organization_id == org_id)
    ).scalar_one()
    assert audit_count > 50
