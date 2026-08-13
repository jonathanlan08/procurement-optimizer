"""The full synthetic demonstration dataset for Meridian Fabrication Works
(Demo) (docs/SPEC.md §Synthetic demonstration dataset).

ALL DATA IS SYNTHETIC. This module is the single source of truth for the
demo dataset; `backend/scripts/seed_demo.py` is now a thin CLI wrapper around
`seed_demo_dataset()` below (restructured by design: "allowed
to restructure it into `backend/src/app/seed/demo_dataset.py` with the
script as a thin wrapper").

**Idempotency discipline.** Every step here follows the same shape: look up
the entity by its natural key directly against the ORM (a plain read, no
service needed for that), and only call the owning service's mutating method
when nothing was found. Services are used for every actual mutation (per
the spec: "PREFER seeding through services where they exist") -
suppliers/parts/BOMs/RFQs/quotes/FX/scoring/scenarios/documents/extraction
all go through their real service, which gets the seed dataset audit events
and domain validation for free. The two exceptions are documented at their
call sites: `Organization`/`User`/`OrganizationMembership` (identity
bootstrap; no service owns account creation) and nothing else - every other
table in this dataset is reached exclusively through a service method.

Re-running `seed_demo_dataset` against an already-seeded organization is
therefore a no-op end to end: every natural-key lookup finds its row and
skips the create call, so counts, ids, and audit history are stable across
repeated runs (verified in `tests/integration/test_demo_dataset.py`).

**The dataset's engineered numbers** (SPEC's required demonstration cases)
are documented next to the code that creates them:
- suppliers: `_SUPPLIER_SPECS` - 6 suppliers, 5 distinct currencies, Net
  30/45/60 payment terms, lead times spanning the required 7-45 day range,
  differentiated quality/defect/on-time performance records.
- parts: `_PART_SPECS` - 19 parts, 6 categories, one kg-based part
  (`MF-GASK-510`), three approved alternatives including one external-MPN
  alternative (`MF-CAST-300` -> `SKF-BH-2205`).
- BOMs: `_seed_boms` - "RK-200 Server Rack" exercises the copy-on-write
  version chain (v1 draft->active, v2 active supersedes v1); "EN-50
  Enclosure" carries one substitute-part line.
- RFQs: `_seed_rfqs` - (a) "Q3 Rack Hardware" open, BOM-exploded, all six
  suppliers invited, one excluded with reason; (b) "Enclosure Pilot Build"
  under_review with four quotes; (c) "Legacy Gasket Buy" draft, inline
  lines.
- quotes: `_seed_quotes` - four manual quotes on RFQ (b) in CNY/EUR/USD/MXN;
  Cascade Precision's mounting-plate price breaks are SPEC §11's own example
  table verbatim (1-99: 12.00, 100-499: 10.50, 500-999: 9.20, 1000+: 8.60);
  Baltic Casting's quote has no payment terms (the missing-commercial-term
  case, matching the same supplier's fixture document); Pacific Metal's
  quote adds a near-miss-part-number line (the uncertain-part-match case).
  Shenzhen Precision is engineered to have the LOWEST raw unit price on the
  bearing-housing line (105.00 CNY ~= 14.50 USD normalized, below every
  other supplier) but, once its enormous shipping cost and the scenario's
  tariff/quality assumptions are applied, the HIGHEST landed effective unit
  cost of the four - the product's own thesis, verified numerically in
  `tests/integration/test_demo_dataset.py` by calling `LandedCostService`
  directly.
- scenarios: `_seed_scenarios` - three persisted `ComparisonScenario` rows:
  a feasible `lowest_landed_cost` run (whose ranking should put Cascade,
  not Shenzhen, first), a `budget_limit=500` run engineered infeasible, and
  a `lowest_unit_price` run whose allocation is forced into a >=2-supplier
  split because every quote's `production_capacity` for the bearing-housing
  line (250 units) is below the RFQ line's required quantity (500).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.ids import IdGenerator
from app.core.security import hash_password
from app.domain.scoring.contracts import Criterion, Direction
from app.models.boms import BillOfMaterials, BomStatus
from app.models.documents import ExtractionRun, PartMatchCandidate, QuoteCorrection, QuoteDocument
from app.models.fx import ExchangeRate
from app.models.identity import Organization, OrganizationMembership, Role, User
from app.models.parts import Part, PartAlternative
from app.models.quotes import Quote, QuoteLine
from app.models.rfqs import Rfq, RfqStatus, RfqSupplier
from app.models.scenarios import ComparisonScenario, ComparisonStrategy
from app.models.suppliers import Supplier, SupplierContact, SupplierPerformanceRecord
from app.providers.extraction.base import ExtractionProvider
from app.providers.storage.base import StorageProvider
from app.schemas.boms import BomCreate, BomLineCreate, BomVersionCreate
from app.schemas.parts import ApprovalStatus, PartAlternativeCreate, PartCreate
from app.schemas.quotes import (
    QuoteCreate,
    QuoteLineCreate,
    QuotePriceBreakCreate,
    QuoteTermsCreate,
)
from app.schemas.rfqs import (
    RfqCreate,
    RfqLineCreate,
    RfqStatusChangeRequest,
    RfqSupplierInviteRequest,
)
from app.schemas.supplier_contacts import SupplierContactCreate
from app.schemas.supplier_performance import SupplierPerformanceCreate
from app.schemas.suppliers import SupplierCreate
from app.seed.units_catalog import seed_unit_catalog
from app.services.audit import AuditRecorder
from app.services.bom_service import BomService
from app.services.document_service import DocumentService
from app.services.extraction_service import ExtractionService
from app.services.fx_service import FxService
from app.services.landed_cost_service import LandedCostAssumptions
from app.services.matching_service import MatchingService
from app.services.part_service import PartService
from app.services.quote_service import QuoteService
from app.services.rfq_service import RfqService
from app.services.scenario_service import ScenarioConstraintsInput, ScenarioService
from app.services.scoring_config_service import CriterionSpecInput, ScoringConfigService
from app.services.supplier_contact_service import SupplierContactService
from app.services.supplier_performance_service import SupplierPerformanceService
from app.services.supplier_service import SupplierService

DEMO_SLUG: Final[str] = "meridian-fab"
DEMO_ORG_NAME: Final[str] = "Meridian Fabrication Works (Demo)"

# Synthetic demo credentials - intentionally public, documented in the README.
DEMO_USERS: Final[tuple[tuple[str, str, Role, str], ...]] = (
    ("demo-owner@meridianfab.example", "Morgan Reyes", Role.OWNER, "demo-owner-2026"),
    ("demo-analyst@meridianfab.example", "Ada Chen", Role.ANALYST, "demo-analyst-2026"),
    ("demo-viewer@meridianfab.example", "Sam Okafor", Role.VIEWER, "demo-viewer-2026"),
)

# backend/src/app/seed/demo_dataset.py -> backend/tests/fixtures/documents
FIXTURES_DOCUMENTS_DIR: Final[Path] = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "documents"
)


@dataclass(slots=True)
class DemoDatasetSummary:
    """Natural-key -> id maps for everything `seed_demo_dataset` touches, so
    a caller (the CLI wrapper, or the integration test) never has to
    re-derive an id by re-querying the database."""

    organization_id: uuid.UUID
    user_ids: dict[str, uuid.UUID]  # by Role.value: "owner" / "analyst" / "viewer"
    supplier_ids: dict[str, uuid.UUID]  # by Supplier.code
    part_ids: dict[str, uuid.UUID]  # by Part.internal_part_number
    unit_ids: dict[str, uuid.UUID]  # by UnitDefinition.code
    bom_ids: dict[str, uuid.UUID]  # "RK-200:v1" / "RK-200:v2" / "EN-50:v1"
    rfq_ids: dict[str, uuid.UUID]  # by Rfq.internal_reference
    quote_ids: dict[str, uuid.UUID]  # by Supplier.code (RFQ (b) only)
    scenario_ids: dict[str, uuid.UUID]  # by ComparisonScenario.name
    scoring_configuration_ids: dict[str, uuid.UUID]  # by ScoringConfiguration.name
    document_ids: dict[str, uuid.UUID]  # by QuoteDocument.original_filename
    exchange_rate_ids: dict[str, uuid.UUID]  # by "BASE/QUOTE"
    extraction_run_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# identity bootstrap (no owning service; kept from the original seed_demo.py)
# ---------------------------------------------------------------------------


def seed_identity(
    session: SaSession, clock: Clock, ids: IdGenerator
) -> tuple[Organization, dict[str, User]]:
    """Idempotent: the demo org (by slug) and its three demo users (by
    email). Direct model inserts - no service owns organization/user/
    membership creation in this codebase."""
    now = clock.now()
    org = session.execute(
        select(Organization).where(Organization.slug == DEMO_SLUG)
    ).scalar_one_or_none()
    if org is None:
        org = Organization(
            id=ids.new_id(),
            slug=DEMO_SLUG,
            name=DEMO_ORG_NAME,
            base_currency="USD",
            is_demo=True,
            created_at=now,
            updated_at=now,
        )
        session.add(org)
        session.flush()

    users: dict[str, User] = {}
    for email, full_name, role, password in DEMO_USERS:
        user = session.execute(
            select(User).where(func.lower(User.email) == email.lower())
        ).scalar_one_or_none()
        if user is None:
            user = User(
                id=ids.new_id(),
                email=email,
                password_hash=hash_password(password),
                full_name=full_name,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            session.add(
                OrganizationMembership(
                    id=ids.new_id(),
                    organization_id=org.id,
                    user_id=user.id,
                    role=role,
                    accepted_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
        users[role.value] = user

    return org, users


# ---------------------------------------------------------------------------
# suppliers + contacts + performance records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ContactSpec:
    name: str
    email: str
    phone: str
    role_title: str


@dataclass(frozen=True, slots=True)
class _PerformanceSpec:
    on_time_delivery_rate: Decimal
    defect_rate: Decimal
    quality_score: Decimal
    orders_count: int


@dataclass(frozen=True, slots=True)
class _SupplierSpec:
    code: str
    name: str
    country_code: str
    supported_currencies: tuple[str, ...]
    standard_payment_terms: str
    standard_incoterm: str
    typical_lead_time_days: int
    capacity_units_per_month: Decimal
    default_moq: Decimal
    contact: _ContactSpec
    performance: _PerformanceSpec


_PERFORMANCE_PERIOD_START: Final[date] = date(2025, 1, 1)
_PERFORMANCE_PERIOD_END: Final[date] = date(2025, 12, 31)

# Six suppliers: the four fixture suppliers (docs/planning §, scripts/
# generate_fixtures.py) plus two new fictional ones - a US domestic
# short-lead supplier and a German precision supplier, per the
# brief. Currencies span USD/CNY/MXN/EUR/SEK (5 distinct, exceeding the
# "at least 3 distinct" requirement and naming all 5 of the SPEC's own
# examples). Lead times span 7 (Cascade, the domestic short-lead boundary)
# to 45 days (Baltic, the slow-but-high-quality boundary). Payment terms
# cover Net 30/45/60 plus Pacific Metal's deposit-style term. Performance
# records are deliberately differentiated: Baltic Casting is
# high-quality-slow (0.985 quality, 0.004 defect, but a 45-day lead time);
# Pacific Metal is cheap-but-defect-prone (0.81 quality, 0.062 defect).
_SUPPLIER_SPECS: Final[tuple[_SupplierSpec, ...]] = (
    _SupplierSpec(
        code="SHENZHEN-PREC",
        name="Shenzhen Precision Manufacturing Co., Ltd.",
        country_code="CN",
        supported_currencies=("USD", "CNY"),
        standard_payment_terms="Net 60",
        standard_incoterm="FOB Shenzhen",
        typical_lead_time_days=35,
        capacity_units_per_month=Decimal("120000"),
        default_moq=Decimal("500"),
        contact=_ContactSpec(
            "Wei Zhang",
            "wei.zhang@shenzhenprecision.example",
            "+86 755 1234 5678",
            "Export Sales Manager",
        ),
        performance=_PerformanceSpec(
            Decimal("0.910000"), Decimal("0.015000"), Decimal("0.930000"), 40
        ),
    ),
    _SupplierSpec(
        code="PACIFIC-METAL",
        name="Pacific Metal Fabricación S.A. de C.V.",
        country_code="MX",
        supported_currencies=("MXN", "USD"),
        standard_payment_terms="50% deposit, balance Net 30",
        standard_incoterm="FOB Manzanillo",
        typical_lead_time_days=28,
        capacity_units_per_month=Decimal("60000"),
        default_moq=Decimal("200"),
        contact=_ContactSpec(
            "Luis Hernández",
            "luis.hernandez@pacificmetal.example",
            "+52 314 123 4567",
            "Account Manager",
        ),
        performance=_PerformanceSpec(
            Decimal("0.830000"), Decimal("0.062000"), Decimal("0.810000"), 25
        ),
    ),
    _SupplierSpec(
        code="NORDIC-FASTENER",
        name="Nordic Fastener AB",
        country_code="SE",
        supported_currencies=("EUR", "SEK"),
        standard_payment_terms="Net 30",
        standard_incoterm="FCA Gothenburg",
        typical_lead_time_days=18,
        capacity_units_per_month=Decimal("2000000"),
        default_moq=Decimal("10000"),
        contact=_ContactSpec(
            "Elin Karlsson",
            "elin.karlsson@nordicfastener.example",
            "+46 31 123 4567",
            "Sales Director",
        ),
        performance=_PerformanceSpec(
            Decimal("0.950000"), Decimal("0.008000"), Decimal("0.960000"), 60
        ),
    ),
    _SupplierSpec(
        code="BALTIC-CASTING",
        name="Baltic Casting Works SIA",
        country_code="LV",
        supported_currencies=("EUR",),
        standard_payment_terms="Net 45",
        standard_incoterm="FOB Riga",
        typical_lead_time_days=45,
        capacity_units_per_month=Decimal("15000"),
        default_moq=Decimal("100"),
        contact=_ContactSpec(
            "Kristaps Ozols",
            "kristaps.ozols@balticcasting.example",
            "+371 6 123 4567",
            "Export Manager",
        ),
        performance=_PerformanceSpec(
            Decimal("0.880000"), Decimal("0.004000"), Decimal("0.985000"), 18
        ),
    ),
    _SupplierSpec(
        code="CASCADE-PRECISION",
        name="Cascade Precision LLC",
        country_code="US",
        supported_currencies=("USD",),
        standard_payment_terms="Net 30",
        standard_incoterm="FCA Portland, OR",
        typical_lead_time_days=7,
        capacity_units_per_month=Decimal("40000"),
        default_moq=Decimal("50"),
        contact=_ContactSpec(
            "Jordan Blake",
            "jordan.blake@cascadeprecision.example",
            "+1 503 555 0142",
            "VP Sales",
        ),
        performance=_PerformanceSpec(
            Decimal("0.970000"), Decimal("0.010000"), Decimal("0.950000"), 50
        ),
    ),
    _SupplierSpec(
        code="RUHRTAL-PRAEZISION",
        name="Ruhrtal Präzisionstechnik GmbH",
        country_code="DE",
        supported_currencies=("EUR",),
        standard_payment_terms="Net 45",
        standard_incoterm="EXW Bochum",
        typical_lead_time_days=21,
        capacity_units_per_month=Decimal("30000"),
        default_moq=Decimal("100"),
        contact=_ContactSpec(
            "Anna Fischer", "anna.fischer@ruhrtal-praezision.example", "+49 234 123456",
            "Key Account Manager",
        ),
        performance=_PerformanceSpec(
            Decimal("0.930000"), Decimal("0.003000"), Decimal("0.990000"), 22
        ),
    ),
)


def _seed_suppliers(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
) -> dict[str, uuid.UUID]:
    service = SupplierService(session, organization_id, audit, clock, ids)
    contact_service = SupplierContactService(session, organization_id, audit, clock, ids)
    performance_service = SupplierPerformanceService(session, organization_id, audit, clock, ids)

    supplier_ids: dict[str, uuid.UUID] = {}
    for spec in _SUPPLIER_SPECS:
        supplier = session.execute(
            select(Supplier).where(
                Supplier.organization_id == organization_id,
                func.lower(Supplier.code) == spec.code.lower(),
            )
        ).scalar_one_or_none()
        if supplier is None:
            supplier = service.create(
                SupplierCreate(
                    code=spec.code,
                    name=spec.name,
                    country_code=spec.country_code,
                    supported_currencies=list(spec.supported_currencies),
                    standard_payment_terms=spec.standard_payment_terms,
                    standard_incoterm=spec.standard_incoterm,
                    typical_lead_time_days=spec.typical_lead_time_days,
                    capacity_units_per_month=spec.capacity_units_per_month,
                    default_moq=spec.default_moq,
                )
            )
        supplier_ids[spec.code] = supplier.id

        existing_contact = session.execute(
            select(SupplierContact).where(
                SupplierContact.organization_id == organization_id,
                SupplierContact.supplier_id == supplier.id,
                SupplierContact.name == spec.contact.name,
            )
        ).scalar_one_or_none()
        if existing_contact is None:
            contact_service.create(
                supplier.id,
                SupplierContactCreate(
                    name=spec.contact.name,
                    email=spec.contact.email,
                    phone=spec.contact.phone,
                    role_title=spec.contact.role_title,
                    is_primary=True,
                ),
            )

        existing_perf = session.execute(
            select(SupplierPerformanceRecord).where(
                SupplierPerformanceRecord.organization_id == organization_id,
                SupplierPerformanceRecord.supplier_id == supplier.id,
                SupplierPerformanceRecord.period_start == _PERFORMANCE_PERIOD_START,
                SupplierPerformanceRecord.period_end == _PERFORMANCE_PERIOD_END,
            )
        ).scalar_one_or_none()
        if existing_perf is None:
            performance_service.create(
                supplier.id,
                SupplierPerformanceCreate(
                    period_start=_PERFORMANCE_PERIOD_START,
                    period_end=_PERFORMANCE_PERIOD_END,
                    on_time_delivery_rate=spec.performance.on_time_delivery_rate,
                    defect_rate=spec.performance.defect_rate,
                    quality_score=spec.performance.quality_score,
                    orders_count=spec.performance.orders_count,
                    source="synthetic_seed",
                    notes=f"Synthetic demonstration performance history for {spec.name}.",
                ),
            )

    session.flush()
    return supplier_ids


# ---------------------------------------------------------------------------
# parts + alternatives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PartSpec:
    ipn: str
    name: str
    description: str
    category: str
    unit_code: str
    target_price: Decimal
    target_price_currency: str
    manufacturer_part_number: str | None = None


# Nineteen parts across six categories (brackets, panels, fasteners,
# castings, gaskets, hardware). The first twelve reuse the exact part
# numbers/names quoted in the four fixture documents (scripts/
# generate_fixtures.py), so the catalog lines up with the demo's own
# extraction fixtures. MF-GASK-510 is kg-based (unit-conversion demos);
# every other part is "each".
_PART_SPECS: Final[tuple[_PartSpec, ...]] = (
    _PartSpec(
        "MF-BRKT-010", "L-Bracket, 3mm Steel, Zinc Plated",
        "L-shaped mounting bracket, 3mm zinc-plated steel.", "brackets", "each",
        Decimal("0.50000000"), "USD",
    ),
    _PartSpec(
        "MF-BRKT-014", "U-Bracket, 3mm Steel, Zinc Plated",
        "U-shaped mounting bracket, 3mm zinc-plated steel.", "brackets", "each",
        Decimal("0.75000000"), "USD",
    ),
    _PartSpec(
        "MF-SHIM-002", "Precision Shim, 0.5mm Stainless Steel",
        "Precision alignment shim, 0.5mm 304 stainless steel.", "fasteners", "each",
        Decimal("0.12000000"), "USD",
    ),
    _PartSpec(
        "MF-PLT-021", "Mounting Plate, Aluminum 5052, 3mm",
        "General-purpose mounting plate, 5052-H32 aluminum, 3mm.", "panels", "each",
        Decimal("40.00000000"), "USD", "AL5052-3MM-PLT",
    ),
    _PartSpec(
        "MF-PLT-022", "Cover Plate, Aluminum 5052, 2mm",
        "Access cover plate, 5052-H32 aluminum, 2mm.", "panels", "each",
        Decimal("35.00000000"), "USD",
    ),
    _PartSpec(
        "MF-CLIP-005", "Retaining Clip, Spring Steel",
        "Spring-steel retaining clip for panel edges.", "fasteners", "each",
        Decimal("1.60000000"), "USD",
    ),
    _PartSpec(
        "MF-SCR-100", "M4x12 Socket Head Cap Screw A2 Stainless",
        "DIN 912 socket head cap screw, M4x12, A2 stainless.", "fasteners", "each",
        Decimal("0.02000000"), "USD", "DIN912-M4X12-A2",
    ),
    _PartSpec(
        "MF-SCR-101", "M5x16 Socket Head Cap Screw A2 Stainless",
        "DIN 912 socket head cap screw, M5x16, A2 stainless.", "fasteners", "each",
        Decimal("0.02500000"), "USD", "DIN912-M5X16-A2",
    ),
    _PartSpec(
        "MF-WASH-030", "M5 Flat Washer A2 Stainless",
        "DIN 125 flat washer, M5, A2 stainless.", "fasteners", "each",
        Decimal("0.00700000"), "USD", "DIN125-M5-A2",
    ),
    _PartSpec(
        "MF-WASH-031", "M5 Flat Washer, Zinc Plated Steel",
        "DIN 125 flat washer, M5, zinc-plated carbon steel.", "fasteners", "each",
        Decimal("0.00400000"), "USD", "DIN125-M5-ZN",
    ),
    _PartSpec(
        "MF-CAST-300", "Bearing Housing, Ductile Iron Casting",
        "Ductile iron bearing housing casting.", "castings", "each",
        Decimal("20.00000000"), "USD",
    ),
    _PartSpec(
        "MF-CAST-301", "Pump Body, Ductile Iron Casting",
        "Ductile iron pump body casting.", "castings", "each",
        Decimal("28.00000000"), "USD",
    ),
    _PartSpec(
        "MF-CAST-302", "Flange Adapter, Grey Iron Casting",
        "Grey iron flange adapter casting.", "castings", "each",
        Decimal("10.00000000"), "USD",
    ),
    _PartSpec(
        "MF-PNL-401", "Rack Front Panel, 2mm Steel",
        "RK-200 front panel, 2mm cold-rolled steel.", "panels", "each",
        Decimal("65.00000000"), "USD",
    ),
    _PartSpec(
        "MF-PNL-402", "Rack Rear Panel, 2mm Steel",
        "RK-200 rear panel, 2mm cold-rolled steel.", "panels", "each",
        Decimal("60.00000000"), "USD",
    ),
    _PartSpec(
        "MF-GASK-510", "Enclosure Door Gasket, EPDM Rubber",
        "EPDM door gasket stock, sold by weight.", "gaskets", "kg",
        Decimal("9.50000000"), "USD",
    ),
    _PartSpec(
        "MF-GASK-511", "Enclosure Seal Gasket, Silicone",
        "Molded silicone seal gasket.", "gaskets", "each",
        Decimal("3.20000000"), "USD",
    ),
    _PartSpec(
        "MF-STDF-201", "M5 Hex Standoff, Brass, 20mm",
        "M5 brass hex standoff, 20mm.", "fasteners", "each",
        Decimal("0.35000000"), "USD",
    ),
    _PartSpec(
        "MF-HNGE-601", "Enclosure Hinge, Stainless Steel",
        "Stainless steel enclosure hinge.", "hardware", "each",
        Decimal("4.75000000"), "USD",
    ),
)


def _ensure_alternative(
    session: SaSession,
    service: PartService,
    organization_id: uuid.UUID,
    *,
    part_id: uuid.UUID,
    alternative_part_id: uuid.UUID | None,
    alternative_mpn: str | None,
    approval_status: ApprovalStatus,
    rationale: str,
    actor_id: uuid.UUID,
) -> None:
    stmt = select(PartAlternative).where(
        PartAlternative.organization_id == organization_id,
        PartAlternative.part_id == part_id,
    )
    if alternative_part_id is not None:
        stmt = stmt.where(PartAlternative.alternative_part_id == alternative_part_id)
    else:
        stmt = stmt.where(PartAlternative.alternative_mpn == alternative_mpn)
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is not None:
        return

    service.add_alternative(
        part_id,
        PartAlternativeCreate(
            alternative_part_id=alternative_part_id,
            alternative_mpn=alternative_mpn,
            approval_status=approval_status,
            rationale=rationale,
        ),
        actor_id=actor_id,
    )


def _seed_parts(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    unit_ids: dict[str, uuid.UUID],
    actor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    service = PartService(session, organization_id, audit, clock, ids)

    part_ids: dict[str, uuid.UUID] = {}
    for spec in _PART_SPECS:
        part = session.execute(
            select(Part).where(
                Part.organization_id == organization_id,
                func.lower(Part.internal_part_number) == spec.ipn.lower(),
            )
        ).scalar_one_or_none()
        if part is None:
            part = service.create(
                PartCreate(
                    internal_part_number=spec.ipn,
                    manufacturer_part_number=spec.manufacturer_part_number,
                    name=spec.name,
                    description=spec.description,
                    category=spec.category,
                    unit_definition_id=unit_ids[spec.unit_code],
                    target_price=spec.target_price,
                    target_price_currency=spec.target_price_currency,
                )
            )
        part_ids[spec.ipn] = part.id
    session.flush()

    # Three approved alternatives, one external (no catalogued Part -
    # alternative_mpn only): see module docstring.
    _ensure_alternative(
        session, service, organization_id,
        part_id=part_ids["MF-BRKT-010"], alternative_part_id=part_ids["MF-BRKT-014"],
        alternative_mpn=None, approval_status="approved",
        rationale="Equivalent mounting bracket; verified fit for RK-200 rack rail assembly.",
        actor_id=actor_id,
    )
    _ensure_alternative(
        session, service, organization_id,
        part_id=part_ids["MF-WASH-030"], alternative_part_id=part_ids["MF-WASH-031"],
        alternative_mpn=None, approval_status="approved",
        rationale=(
            "Zinc-plated washer accepted for indoor/covered installs; "
            "cost savings vs. stainless."
        ),
        actor_id=actor_id,
    )
    _ensure_alternative(
        session, service, organization_id,
        part_id=part_ids["MF-CAST-300"], alternative_part_id=None,
        alternative_mpn="SKF-BH-2205", approval_status="approved",
        rationale="Equivalent bearing housing from SKF catalog; requires incoming inspection.",
        actor_id=actor_id,
    )

    return part_ids


# ---------------------------------------------------------------------------
# BOMs
# ---------------------------------------------------------------------------


def _get_or_create_bom_v1(
    session: SaSession,
    service: BomService,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    name: str,
    product_name: str,
    notes: str,
    lines: list[BomLineCreate],
) -> BillOfMaterials:
    existing = session.execute(
        select(BillOfMaterials).where(
            BillOfMaterials.organization_id == organization_id,
            BillOfMaterials.name == name,
            BillOfMaterials.version_number == 1,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    bom, _lines = service.create(
        BomCreate(name=name, product_name=product_name, notes=notes, lines=lines),
        actor_id=actor_id,
    )
    return bom


def _get_or_create_bom_v2(
    session: SaSession,
    service: BomService,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    root_bom: BillOfMaterials,
    notes: str,
    lines: list[BomLineCreate],
) -> BillOfMaterials:
    existing = session.execute(
        select(BillOfMaterials).where(
            BillOfMaterials.organization_id == organization_id,
            BillOfMaterials.root_bom_id == root_bom.id,
            BillOfMaterials.version_number == 2,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    new_bom, _lines = service.new_version(
        root_bom.id,
        BomVersionCreate(name=None, product_name=None, notes=notes, lines=lines),
        actor_id=actor_id,
    )
    return new_bom


def _ensure_bom_active(service: BomService, bom: BillOfMaterials) -> BillOfMaterials:
    if bom.status == BomStatus.DRAFT:
        activated, _lines = service.activate(bom.id)
        return activated
    return bom


def _seed_boms(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    part_ids: dict[str, uuid.UUID],
    actor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    service = BomService(session, organization_id, audit, clock, ids)
    bom_ids: dict[str, uuid.UUID] = {}

    # "RK-200 Server Rack": copy-on-write version chain - v1 activated, then
    # v2 (bracket qty revised, standoff line added) forked and activated,
    # marking v1 superseded.
    rk200_v1 = _get_or_create_bom_v1(
        session, service, organization_id, actor_id,
        name="RK-200 Server Rack",
        product_name="RK-200 42U Server Rack Frame",
        notes="Standard 42U rack frame hardware kit.",
        lines=[
            BomLineCreate(part_id=part_ids["MF-BRKT-010"], quantity_per_assembly=Decimal("8")),
            BomLineCreate(part_id=part_ids["MF-BRKT-014"], quantity_per_assembly=Decimal("4")),
            BomLineCreate(part_id=part_ids["MF-PNL-401"], quantity_per_assembly=Decimal("1")),
            BomLineCreate(part_id=part_ids["MF-PNL-402"], quantity_per_assembly=Decimal("1")),
            BomLineCreate(part_id=part_ids["MF-SCR-101"], quantity_per_assembly=Decimal("24")),
            BomLineCreate(part_id=part_ids["MF-WASH-030"], quantity_per_assembly=Decimal("24")),
        ],
    )
    rk200_v1 = _ensure_bom_active(service, rk200_v1)
    bom_ids["RK-200:v1"] = rk200_v1.id

    rk200_v2 = _get_or_create_bom_v2(
        session, service, organization_id, actor_id,
        root_bom=rk200_v1,
        notes="Rev B: added standoffs; bracket qty increased for revised rail spacing.",
        lines=[
            BomLineCreate(part_id=part_ids["MF-BRKT-010"], quantity_per_assembly=Decimal("10")),
            BomLineCreate(part_id=part_ids["MF-BRKT-014"], quantity_per_assembly=Decimal("4")),
            BomLineCreate(part_id=part_ids["MF-PNL-401"], quantity_per_assembly=Decimal("1")),
            BomLineCreate(part_id=part_ids["MF-PNL-402"], quantity_per_assembly=Decimal("1")),
            BomLineCreate(part_id=part_ids["MF-SCR-101"], quantity_per_assembly=Decimal("24")),
            BomLineCreate(part_id=part_ids["MF-WASH-030"], quantity_per_assembly=Decimal("24")),
            BomLineCreate(part_id=part_ids["MF-STDF-201"], quantity_per_assembly=Decimal("8")),
        ],
    )
    rk200_v2 = _ensure_bom_active(service, rk200_v2)
    bom_ids["RK-200:v2"] = rk200_v2.id

    # "EN-50 Enclosure": single version, one line carries an approved
    # substitute part.
    en50_v1 = _get_or_create_bom_v1(
        session, service, organization_id, actor_id,
        name="EN-50 Enclosure",
        product_name="EN-50 Outdoor Enclosure",
        notes="Outdoor field enclosure hardware kit.",
        lines=[
            BomLineCreate(part_id=part_ids["MF-CAST-300"], quantity_per_assembly=Decimal("2")),
            BomLineCreate(part_id=part_ids["MF-CAST-302"], quantity_per_assembly=Decimal("4")),
            BomLineCreate(part_id=part_ids["MF-PLT-021"], quantity_per_assembly=Decimal("2")),
            BomLineCreate(
                part_id=part_ids["MF-GASK-510"], quantity_per_assembly=Decimal("0.75")
            ),
            BomLineCreate(part_id=part_ids["MF-HNGE-601"], quantity_per_assembly=Decimal("2")),
            BomLineCreate(
                part_id=part_ids["MF-WASH-030"],
                quantity_per_assembly=Decimal("12"),
                substitute_part_id=part_ids["MF-WASH-031"],
                notes="Approved zinc-plated substitute for indoor/covered installs.",
            ),
        ],
    )
    en50_v1 = _ensure_bom_active(service, en50_v1)
    bom_ids["EN-50:v1"] = en50_v1.id

    return bom_ids


# ---------------------------------------------------------------------------
# RFQs
# ---------------------------------------------------------------------------

_RFQ_CHAIN: Final[dict[RfqStatus, RfqStatus]] = {
    RfqStatus.DRAFT: RfqStatus.OPEN,
    RfqStatus.OPEN: RfqStatus.UNDER_REVIEW,
}
# The forward progression this module ever drives an RFQ through. Used so a
# second seed run - which calls _ensure_rfq_status(..., OPEN, ...) again
# even though RFQ (b) has already moved on to UNDER_REVIEW - recognizes the
# target as an already-passed milestone (no-op) rather than attempting an
# illegal backward transition.
_RFQ_PROGRESSION: Final[tuple[RfqStatus, ...]] = (
    RfqStatus.DRAFT,
    RfqStatus.OPEN,
    RfqStatus.UNDER_REVIEW,
)


def _ensure_rfq_status(
    service: RfqService, rfq: Rfq, target: RfqStatus, *, reason: str, actor_id: uuid.UUID
) -> Rfq:
    """Walks the single allowed chain draft->open->under_review one step at
    a time until `target` is reached; a no-op if already there OR already
    past it (idempotent on re-run - see `_RFQ_PROGRESSION`)."""
    current = rfq
    target_index = _RFQ_PROGRESSION.index(target)
    while current.status != target:
        if (
            current.status not in _RFQ_PROGRESSION
            or _RFQ_PROGRESSION.index(current.status) > target_index
        ):
            return current
        next_status = _RFQ_CHAIN.get(current.status)
        if next_status is None:  # pragma: no cover - defensive only
            raise ValueError(f"No transition path from {current.status} to {target}.")
        current, _lines = service.change_status(
            current.id,
            RfqStatusChangeRequest(to_status=next_status, reason=reason),
            actor_id=actor_id,
        )
    return current


def _ensure_suppliers_invited(
    session: SaSession,
    service: RfqService,
    organization_id: uuid.UUID,
    rfq_id: uuid.UUID,
    supplier_ids: list[uuid.UUID],
) -> None:
    existing = (
        session.execute(
            select(RfqSupplier.supplier_id).where(
                RfqSupplier.organization_id == organization_id,
                RfqSupplier.rfq_id == rfq_id,
            )
        )
        .scalars()
        .all()
    )
    existing_set = set(existing)
    missing = [sid for sid in supplier_ids if sid not in existing_set]
    if missing:
        service.invite_suppliers(rfq_id, RfqSupplierInviteRequest(supplier_ids=missing))


def _ensure_supplier_excluded(
    session: SaSession,
    service: RfqService,
    organization_id: uuid.UUID,
    rfq_id: uuid.UUID,
    supplier_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    rs = session.execute(
        select(RfqSupplier).where(
            RfqSupplier.organization_id == organization_id,
            RfqSupplier.rfq_id == rfq_id,
            RfqSupplier.supplier_id == supplier_id,
        )
    ).scalar_one_or_none()
    if rs is not None and rs.excluded_at is None:
        service.exclude_supplier(rfq_id, rs.id, reason=reason)


def _get_or_create_rfq(
    session: SaSession,
    service: RfqService,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    internal_reference: str,
    name: str,
    base_currency: str,
    due_date: date,
    requested_delivery_date: date | None,
    requested_payment_terms: str | None,
    requested_incoterm: str | None,
    notes: str,
    source_bom_id: uuid.UUID | None,
    assembly_quantity: Decimal | None,
    lines: list[RfqLineCreate] | None,
) -> Rfq:
    existing = session.execute(
        select(Rfq).where(
            Rfq.organization_id == organization_id,
            func.lower(Rfq.internal_reference) == internal_reference.lower(),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    rfq, _lines = service.create(
        RfqCreate(
            name=name,
            internal_reference=internal_reference,
            base_currency=base_currency,
            due_date=due_date,
            requested_delivery_date=requested_delivery_date,
            requested_payment_terms=requested_payment_terms,
            requested_incoterm=requested_incoterm,
            notes=notes,
            lines=lines if lines is not None else [],
            source_bom_id=source_bom_id,
            assembly_quantity=assembly_quantity,
        ),
        actor_id=actor_id,
    )
    return rfq


_ALL_SUPPLIER_CODES: Final[tuple[str, ...]] = (
    "SHENZHEN-PREC",
    "PACIFIC-METAL",
    "NORDIC-FASTENER",
    "BALTIC-CASTING",
    "CASCADE-PRECISION",
    "RUHRTAL-PRAEZISION",
)
_ENCLOSURE_QUOTING_SUPPLIER_CODES: Final[tuple[str, ...]] = (
    "SHENZHEN-PREC",
    "BALTIC-CASTING",
    "CASCADE-PRECISION",
    "PACIFIC-METAL",
)


def _seed_rfqs(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    part_ids: dict[str, uuid.UUID],
    bom_ids: dict[str, uuid.UUID],
    supplier_ids: dict[str, uuid.UUID],
    actor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    service = RfqService(session, organization_id, audit, clock, ids)
    rfq_ids: dict[str, uuid.UUID] = {}

    # (a) "Q3 Rack Hardware" - open, exploded from RK-200's active v2, all
    # six suppliers invited, one (Pacific Metal) excluded with reason.
    rfq_a = _get_or_create_rfq(
        session, service, organization_id, actor_id,
        internal_reference="RFQ-2026-Q3-RACK",
        name="Q3 Rack Hardware",
        base_currency="USD",
        due_date=date(2026, 9, 5),
        requested_delivery_date=date(2026, 9, 25),
        requested_payment_terms="Net 30",
        requested_incoterm="FOB Origin",
        notes="Q3 replenishment run for RK-200 rack hardware, exploded from the active BOM.",
        source_bom_id=bom_ids["RK-200:v2"],
        assembly_quantity=Decimal("5"),
        lines=None,
    )
    rfq_a = _ensure_rfq_status(
        service, rfq_a, RfqStatus.OPEN,
        reason="Released to invited suppliers for Q3 rack hardware sourcing.",
        actor_id=actor_id,
    )
    rfq_ids["RFQ-2026-Q3-RACK"] = rfq_a.id
    _ensure_suppliers_invited(
        session, service, organization_id, rfq_a.id,
        [supplier_ids[code] for code in _ALL_SUPPLIER_CODES],
    )
    _ensure_supplier_excluded(
        session, service, organization_id, rfq_a.id, supplier_ids["PACIFIC-METAL"],
        reason=(
            "Pacific Metal is already fully engaged on the Enclosure Pilot Build this "
            "quarter; excluding to avoid overcommitting their capacity on a second "
            "concurrent hardware run."
        ),
    )

    # (b) "Enclosure Pilot Build" - under_review with manual lines (quotes
    # attached by _seed_quotes).
    rfq_b = _get_or_create_rfq(
        session, service, organization_id, actor_id,
        internal_reference="RFQ-2026-ENC-PILOT",
        name="Enclosure Pilot Build",
        base_currency="USD",
        due_date=date(2026, 6, 15),
        requested_delivery_date=date(2026, 7, 10),
        requested_payment_terms="Net 30",
        requested_incoterm="FOB Origin",
        notes="Pilot build sourcing round for the EN-50 enclosure's castings and plate.",
        source_bom_id=None,
        assembly_quantity=None,
        lines=[
            RfqLineCreate(
                part_id=part_ids["MF-CAST-300"], required_quantity=Decimal("500"),
                notes="Pilot build bearing housings.",
            ),
            RfqLineCreate(
                part_id=part_ids["MF-PLT-021"], required_quantity=Decimal("650"),
                notes="Pilot build mounting plates.",
            ),
            RfqLineCreate(
                part_id=part_ids["MF-GASK-510"], required_quantity=Decimal("15"),
                notes="Door gasket stock, sold by weight.",
            ),
        ],
    )
    rfq_b = _ensure_rfq_status(
        service, rfq_b, RfqStatus.OPEN,
        reason="Released to invited suppliers for the enclosure pilot build.",
        actor_id=actor_id,
    )
    _ensure_suppliers_invited(
        session, service, organization_id, rfq_b.id,
        [supplier_ids[code] for code in _ENCLOSURE_QUOTING_SUPPLIER_CODES],
    )
    rfq_b = _ensure_rfq_status(
        service, rfq_b, RfqStatus.UNDER_REVIEW,
        reason="Quotes received; moving to technical and commercial review.",
        actor_id=actor_id,
    )
    rfq_ids["RFQ-2026-ENC-PILOT"] = rfq_b.id

    # (c) "Legacy Gasket Buy" - draft, inline lines, no suppliers/quotes.
    rfq_c = _get_or_create_rfq(
        session, service, organization_id, actor_id,
        internal_reference="RFQ-2026-LEGACY-GASKET",
        name="Legacy Gasket Buy",
        base_currency="USD",
        due_date=date(2026, 9, 20),
        requested_delivery_date=None,
        requested_payment_terms="Net 30",
        requested_incoterm=None,
        notes="Low-volume legacy service-parts replenishment; not yet released.",
        source_bom_id=None,
        assembly_quantity=None,
        lines=[
            RfqLineCreate(
                part_id=part_ids["MF-GASK-510"], required_quantity=Decimal("50"),
                notes="Legacy service-parts replenishment, sold by weight.",
            ),
            RfqLineCreate(
                part_id=part_ids["MF-GASK-511"], required_quantity=Decimal("200"),
                notes="Legacy service-parts replenishment.",
            ),
        ],
    )
    rfq_ids["RFQ-2026-LEGACY-GASKET"] = rfq_c.id

    return rfq_ids


# ---------------------------------------------------------------------------
# quotes (RFQ (b) only)
# ---------------------------------------------------------------------------


def _enclosure_pilot_quote_specs(
    unit_ids: dict[str, uuid.UUID], supplier_ids: dict[str, uuid.UUID]
) -> list[tuple[str, QuoteCreate]]:
    """Four manual quotes on the Enclosure Pilot Build. See module docstring
    for the engineered price-inversion, price-break, missing-term, and
    uncertain-match numbers."""
    each = unit_ids["each"]
    kg = unit_ids["kg"]

    return [
        (
            "SHENZHEN-PREC",
            QuoteCreate(
                supplier_id=supplier_ids["SHENZHEN-PREC"],
                quote_number="SPM-Q-77410",
                quote_date=date(2026, 6, 2),
                expiration_date=date(2026, 7, 2),
                currency="CNY",
                notes=(
                    "Quoted against Enclosure Pilot Build; pricing per Shenzhen "
                    "Precision's standard export terms."
                ),
                lines=[
                    QuoteLineCreate(
                        quoted_part_number="MF-CAST-300",
                        description="Bearing Housing, Ductile Iron Casting",
                        quantity=Decimal("500"),
                        unit_definition_id=each,
                        unit_price=Decimal("105.00"),
                        moq=Decimal("100"),
                        lead_time_days=35,
                        country_of_origin="CN",
                        shipping_cost=Decimal("30000.00"),
                        production_capacity=Decimal("250"),
                    ),
                    QuoteLineCreate(
                        quoted_part_number="MF-PLT-021",
                        description="Mounting Plate, Aluminum 5052, 3mm",
                        quantity=Decimal("650"),
                        unit_definition_id=each,
                        unit_price=Decimal("60.00"),
                        moq=Decimal("50"),
                        lead_time_days=30,
                        country_of_origin="CN",
                        shipping_cost=Decimal("8000.00"),
                        production_capacity=Decimal("800"),
                        price_breaks=[
                            QuotePriceBreakCreate(
                                min_quantity=Decimal("1"), max_quantity=Decimal("99"),
                                unit_price=Decimal("70.00"),
                            ),
                            QuotePriceBreakCreate(
                                min_quantity=Decimal("100"), max_quantity=Decimal("999"),
                                unit_price=Decimal("60.00"),
                            ),
                            QuotePriceBreakCreate(
                                min_quantity=Decimal("1000"), max_quantity=None,
                                unit_price=Decimal("50.00"),
                            ),
                        ],
                    ),
                ],
                terms=QuoteTermsCreate(
                    payment_terms="Net 60",
                    payment_terms_days=60,
                    incoterm="FOB Shenzhen",
                    shipping_terms="FOB Shenzhen",
                    warranty_terms="12 months against manufacturing defects",
                    validity_days=30,
                ),
            ),
        ),
        (
            "BALTIC-CASTING",
            QuoteCreate(
                supplier_id=supplier_ids["BALTIC-CASTING"],
                quote_number="BC-Q-9012",
                quote_date=date(2026, 6, 5),
                expiration_date=date(2026, 7, 5),
                currency="EUR",
                notes="Quoted against Enclosure Pilot Build.",
                lines=[
                    QuoteLineCreate(
                        quoted_part_number="MF-CAST-300",
                        description="Bearing Housing, Ductile Iron Casting",
                        quantity=Decimal("500"),
                        unit_definition_id=each,
                        unit_price=Decimal("18.50"),
                        moq=Decimal("50"),
                        lead_time_days=50,
                        country_of_origin="LV",
                        shipping_cost=Decimal("350.00"),
                        production_capacity=Decimal("250"),
                    ),
                    QuoteLineCreate(
                        quoted_part_number="MF-PLT-021",
                        description="Mounting Plate, Aluminum 5052, 3mm",
                        quantity=Decimal("650"),
                        unit_definition_id=each,
                        unit_price=Decimal("48.00"),
                        moq=Decimal("25"),
                        lead_time_days=45,
                        country_of_origin="LV",
                        shipping_cost=Decimal("300.00"),
                        production_capacity=Decimal("900"),
                    ),
                ],
                # Missing commercial term: no payment_terms stated, matching
                # the same supplier's fixture document (baltic_casting_quote.xlsx).
                terms=QuoteTermsCreate(
                    payment_terms=None,
                    payment_terms_days=None,
                    incoterm="FOB Riga",
                    shipping_terms="FOB Riga",
                    warranty_terms="12 months",
                    validity_days=30,
                ),
            ),
        ),
        (
            "CASCADE-PRECISION",
            QuoteCreate(
                supplier_id=supplier_ids["CASCADE-PRECISION"],
                quote_number="CAS-2026-0417",
                quote_date=date(2026, 6, 1),
                expiration_date=date(2026, 8, 1),
                currency="USD",
                notes="Quoted against Enclosure Pilot Build; domestic short-lead option.",
                lines=[
                    QuoteLineCreate(
                        quoted_part_number="MF-CAST-300",
                        description="Bearing Housing, Ductile Iron Casting",
                        quantity=Decimal("500"),
                        unit_definition_id=each,
                        unit_price=Decimal("16.00"),
                        moq=Decimal("50"),
                        lead_time_days=8,
                        country_of_origin="US",
                        shipping_cost=Decimal("150.00"),
                        production_capacity=Decimal("250"),
                    ),
                    QuoteLineCreate(
                        quoted_part_number="MF-PLT-021",
                        description="Mounting Plate, Aluminum 5052, 3mm",
                        quantity=Decimal("650"),
                        unit_definition_id=each,
                        unit_price=Decimal("9.20"),
                        moq=Decimal("20"),
                        lead_time_days=7,
                        country_of_origin="US",
                        shipping_cost=Decimal("80.00"),
                        production_capacity=Decimal("900"),
                        # SPEC §11's own example price-break table, verbatim.
                        price_breaks=[
                            QuotePriceBreakCreate(
                                min_quantity=Decimal("1"), max_quantity=Decimal("99"),
                                unit_price=Decimal("12.00"),
                            ),
                            QuotePriceBreakCreate(
                                min_quantity=Decimal("100"), max_quantity=Decimal("499"),
                                unit_price=Decimal("10.50"),
                            ),
                            QuotePriceBreakCreate(
                                min_quantity=Decimal("500"), max_quantity=Decimal("999"),
                                unit_price=Decimal("9.20"),
                            ),
                            QuotePriceBreakCreate(
                                min_quantity=Decimal("1000"), max_quantity=None,
                                unit_price=Decimal("8.60"),
                            ),
                        ],
                    ),
                    # Exact part-number match (auto-confirms via
                    # MatchingService) so RFQ line 3 (MF-GASK-510) has at
                    # least one real, matched offer - Pacific Metal's own
                    # gasket line below is deliberately a near-miss that
                    # stays unmatched (the uncertain-part-match demo), so
                    # this line is what keeps every scenario over this RFQ
                    # solvable.
                    QuoteLineCreate(
                        quoted_part_number="MF-GASK-510",
                        description="Enclosure Door Gasket, EPDM Rubber",
                        quantity=Decimal("15"),
                        unit_definition_id=kg,
                        unit_price=Decimal("9.80"),
                        lead_time_days=10,
                        country_of_origin="US",
                        shipping_cost=Decimal("25.00"),
                        production_capacity=Decimal("50"),
                    ),
                ],
                terms=QuoteTermsCreate(
                    payment_terms="Net 30",
                    payment_terms_days=30,
                    incoterm="FCA Portland, OR",
                    shipping_terms="FCA Portland, OR",
                    warranty_terms="24 months",
                    validity_days=60,
                ),
            ),
        ),
        (
            "PACIFIC-METAL",
            QuoteCreate(
                supplier_id=supplier_ids["PACIFIC-METAL"],
                quote_number="PMF-2026-1180",
                quote_date=date(2026, 6, 8),
                expiration_date=date(2026, 7, 8),
                currency="MXN",
                notes=(
                    "Quoted against Enclosure Pilot Build; includes a sample gasket line "
                    "for evaluation."
                ),
                lines=[
                    QuoteLineCreate(
                        quoted_part_number="MF-CAST-300",
                        description="Bearing Housing, Ductile Iron Casting",
                        quantity=Decimal("500"),
                        unit_definition_id=each,
                        unit_price=Decimal("300.00"),
                        moq=Decimal("200"),
                        lead_time_days=28,
                        country_of_origin="MX",
                        shipping_cost=Decimal("4500.00"),
                        production_capacity=Decimal("250"),
                    ),
                    QuoteLineCreate(
                        quoted_part_number="MF-PLT-021",
                        description="Mounting Plate, Aluminum 5052, 3mm",
                        quantity=Decimal("650"),
                        unit_definition_id=each,
                        unit_price=Decimal("620.00"),
                        moq=Decimal("40"),
                        lead_time_days=25,
                        country_of_origin="MX",
                        shipping_cost=Decimal("3800.00"),
                        production_capacity=Decimal("900"),
                    ),
                    # Uncertain part match: "MFGASK510" normalizes identically
                    # to MF-GASK-510's normalized_key (strip non-alnum,
                    # lowercase) but is not the exact internal part number, so
                    # MatchingService lands this on strategy `normalized_text`
                    # (confidence 0.85) rather than auto-confirming it.
                    QuoteLineCreate(
                        quoted_part_number="MFGASK510",
                        description="Enclosure door gasket sample (evaluation quantity)",
                        quantity=Decimal("15"),
                        unit_definition_id=kg,
                        unit_price=Decimal("145.00"),
                        lead_time_days=40,
                        country_of_origin="MX",
                        notes=(
                            "Sample line pending part-match review; not yet confirmed "
                            "against the catalog."
                        ),
                    ),
                ],
                terms=QuoteTermsCreate(
                    payment_terms="50% deposit, balance Net 30",
                    payment_terms_days=30,
                    incoterm="FOB Manzanillo",
                    shipping_terms="FOB Manzanillo",
                    warranty_terms="6 months",
                    validity_days=30,
                ),
            ),
        ),
    ]


def _seed_quotes(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    rfq_b_id: uuid.UUID,
    supplier_ids: dict[str, uuid.UUID],
    unit_ids: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    service = QuoteService(session, organization_id, audit, clock, ids)
    quote_ids: dict[str, uuid.UUID] = {}

    for supplier_code, body in _enclosure_pilot_quote_specs(unit_ids, supplier_ids):
        quote = session.execute(
            select(Quote).where(
                Quote.organization_id == organization_id,
                Quote.rfq_id == rfq_b_id,
                Quote.supplier_id == supplier_ids[supplier_code],
            )
        ).scalar_one_or_none()
        if quote is None:
            quote, _lines, _breaks, _terms = service.create(rfq_b_id, body)
        quote_ids[supplier_code] = quote.id

    return quote_ids


# ---------------------------------------------------------------------------
# part matching (the "one uncertain part match" demo)
# ---------------------------------------------------------------------------


def _seed_matching(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    quote_ids: dict[str, uuid.UUID],
    actor_id: uuid.UUID,
) -> None:
    service = MatchingService(session, organization_id, audit, clock, ids)
    for quote_id in quote_ids.values():
        line_ids = (
            session.execute(
                select(QuoteLine.id).where(
                    QuoteLine.organization_id == organization_id,
                    QuoteLine.quote_id == quote_id,
                )
            )
            .scalars()
            .all()
        )
        if not line_ids:
            continue
        already_matched = session.execute(
            select(func.count())
            .select_from(PartMatchCandidate)
            .where(
                PartMatchCandidate.organization_id == organization_id,
                PartMatchCandidate.quote_line_id.in_(line_ids),
            )
        ).scalar_one()
        if already_matched:
            continue
        service.generate_for_quote(quote_id, actor_id=actor_id)


# ---------------------------------------------------------------------------
# FX manual overrides
# ---------------------------------------------------------------------------

# Values consistent with app.providers.fx.synthetic.SyntheticFxProvider's own
# USD-to-X table, seeded as manual overrides so the demo shows the override
# feature (and every scenario's fx_snapshot carries is_manual_override=True).
_FX_OVERRIDES: Final[tuple[tuple[str, str, Decimal], ...]] = (
    ("USD", "CNY", Decimal("7.24")),
    ("USD", "EUR", Decimal("0.92")),
    ("USD", "MXN", Decimal("17.05")),
)
_FX_EFFECTIVE_DATE: Final[date] = date(2026, 1, 1)


def _seed_fx(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    actor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    service = FxService(session, organization_id, audit, clock, ids)
    rate_ids: dict[str, uuid.UUID] = {}
    for base, quote, rate in _FX_OVERRIDES:
        key = f"{base}/{quote}"
        row = session.execute(
            select(ExchangeRate).where(
                ExchangeRate.organization_id == organization_id,
                ExchangeRate.base_currency == base,
                ExchangeRate.quote_currency == quote,
                ExchangeRate.effective_date == _FX_EFFECTIVE_DATE,
            )
        ).scalar_one_or_none()
        if row is None:
            row = service.set_override(
                base=base,
                quote=quote,
                rate=rate,
                effective_date=_FX_EFFECTIVE_DATE,
                reason=(
                    "Demo: pinned synthetic-consistent manual override for reproducible "
                    "landed-cost figures."
                ),
                actor_id=actor_id,
            )
        rate_ids[key] = row.id
    return rate_ids


# ---------------------------------------------------------------------------
# scoring configurations
# ---------------------------------------------------------------------------

QUALITY_FIRST_CONFIG_NAME: Final[str] = "Quality-first"


def _seed_scoring_configs(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    actor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    service = ScoringConfigService(session, organization_id, audit, clock, ids)
    sample = service.ensure_sample_configuration(actor_id)
    config_ids: dict[str, uuid.UUID] = {sample.name: sample.id}

    custom = next(
        (c for c in service.list(actor_id) if c.name == QUALITY_FIRST_CONFIG_NAME), None
    )
    if custom is None:
        custom, _notes = service.create(
            QUALITY_FIRST_CONFIG_NAME,
            [
                CriterionSpecInput(
                    criterion=Criterion.QUALITY_HISTORY.value, weight=Decimal("0.40"),
                    direction=Direction.HIGHER_IS_BETTER.value, label="Quality history",
                ),
                CriterionSpecInput(
                    criterion=Criterion.DEFECT_RATE.value, weight=Decimal("0.30"),
                    direction=Direction.LOWER_IS_BETTER.value, label="Defect rate",
                ),
                CriterionSpecInput(
                    criterion=Criterion.ON_TIME_DELIVERY.value, weight=Decimal("0.20"),
                    direction=Direction.HIGHER_IS_BETTER.value, label="On-time delivery",
                ),
                CriterionSpecInput(
                    criterion=Criterion.TOTAL_LANDED_COST.value, weight=Decimal("0.10"),
                    direction=Direction.LOWER_IS_BETTER.value, label="Total landed cost",
                ),
            ],
            actor_id=actor_id,
        )
    config_ids[QUALITY_FIRST_CONFIG_NAME] = custom.id
    return config_ids


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

# The sample assumptions the spec names by name: tariff 3.5%,
# quality 2%, annual 8%, baseline Net-30. `delay_risk_per_day="0"` +
# `required_lead_time_days` are also supplied so DELAY_RISK is
# ASSUMPTION_DEPENDENT (present, zero-cost) rather than MISSING for every
# offer - required_lead_time_days has no source column anywhere in this
# schema (app/services/landed_cost_service.py module docstring), so without
# an explicit override every offer's completeness degrades to INCOMPLETE and
# app.services.scenario_service's Offer.incomplete_landed_cost gate excludes
# it unless allow_incomplete_offers is set (see ScenarioConstraintsInput
# below). A zero delay-risk rate keeps this override cost-neutral: it never
# perturbs the engineered landed-cost figures, only unblocks eligibility.
SCENARIO_ASSUMPTIONS: Final[LandedCostAssumptions] = LandedCostAssumptions(
    quality_risk_rate="0.02",
    tariff_rate="0.035",
    annual_rate="0.08",
    baseline_terms_days="30",
    delay_risk_per_day="0",
    required_lead_time_days="45",
)

SCENARIO_LOWEST_LANDED_COST_NAME: Final[str] = "Enclosure Pilot - Lowest Landed Cost"
SCENARIO_INFEASIBLE_NAME: Final[str] = "Enclosure Pilot - Budget Ceiling (Infeasible Demo)"
SCENARIO_SPLIT_NAME: Final[str] = "Enclosure Pilot - Capacity-Constrained Split"


def _seed_scenarios(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    rfq_b_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    service = ScenarioService(session, organization_id, audit, clock, ids)
    scenario_ids: dict[str, uuid.UUID] = {}

    specs: list[tuple[str, ComparisonStrategy, ScenarioConstraintsInput, str]] = [
        (
            SCENARIO_LOWEST_LANDED_COST_NAME,
            ComparisonStrategy.LOWEST_LANDED_COST,
            # allow_incomplete_offers=True: Baltic Casting's quote has no
            # payment terms (the missing-commercial-term case), which makes
            # its FINANCING component - and therefore its landed-cost
            # completeness - genuinely INCOMPLETE, not merely
            # assumption-dependent, and there is no assumption override for
            # payment_terms_days (see SCENARIO_ASSUMPTIONS above). Every
            # supplier should still be a real candidate in the comparison.
            ScenarioConstraintsInput(allow_incomplete_offers=True),
            "Feasible baseline: ranks suppliers by total landed cost under the sample "
            "assumptions (tariff 3.5%, quality 2%, annual 8%, baseline Net-30).",
        ),
        (
            SCENARIO_INFEASIBLE_NAME,
            ComparisonStrategy.LOWEST_LANDED_COST,
            ScenarioConstraintsInput(
                budget_limit=Decimal("500"), allow_incomplete_offers=True
            ),
            "Engineered infeasible: a 500 USD budget cannot cover 500+ units of "
            "castings and plate at any quoted price.",
        ),
        (
            SCENARIO_SPLIT_NAME,
            ComparisonStrategy.LOWEST_UNIT_PRICE,
            ScenarioConstraintsInput(allow_incomplete_offers=True),
            "Every supplier's stated production_capacity for the bearing-housing line "
            "(250 units) is below the required 500, forcing a multi-supplier split.",
        ),
    ]
    for name, strategy, constraints, notes in specs:
        scenario = session.execute(
            select(ComparisonScenario).where(
                ComparisonScenario.organization_id == organization_id,
                ComparisonScenario.rfq_id == rfq_b_id,
                ComparisonScenario.name == name,
            )
        ).scalar_one_or_none()
        if scenario is None:
            pkg = service.create_and_run(
                rfq_b_id,
                name=name,
                strategy=strategy,
                scoring_configuration_id=None,
                assumptions=SCENARIO_ASSUMPTIONS,
                constraints=constraints,
                notes=notes,
                actor_id=actor_id,
            )
            scenario = pkg.scenario
        scenario_ids[name] = scenario.id

    return scenario_ids


# ---------------------------------------------------------------------------
# documents + extraction (RFQ (a))
# ---------------------------------------------------------------------------

_DOCUMENT_SUPPLIER_CODES: Final[dict[str, str]] = {
    "shenzhen_precision_quote.pdf": "SHENZHEN-PREC",
    "pacific_metal_quote.png": "PACIFIC-METAL",
    "nordic_fastener_quote.csv": "NORDIC-FASTENER",
    "baltic_casting_quote.xlsx": "BALTIC-CASTING",
}
_DOCUMENT_CONTENT_TYPES: Final[dict[str, str]] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_CSV_DOCUMENT_FILENAME: Final[str] = "nordic_fastener_quote.csv"


def _seed_documents(
    session: SaSession,
    organization_id: uuid.UUID,
    audit: AuditRecorder,
    clock: Clock,
    ids: IdGenerator,
    rfq_a_id: uuid.UUID,
    supplier_ids: dict[str, uuid.UUID],
    storage: StorageProvider,
    extraction_provider: ExtractionProvider,
    max_upload_bytes: int,
    actor_id: uuid.UUID,
) -> tuple[dict[str, uuid.UUID], uuid.UUID]:
    doc_service = DocumentService(
        session, organization_id, audit, clock, ids,
        storage=storage, max_upload_bytes=max_upload_bytes,
    )
    document_ids: dict[str, uuid.UUID] = {}

    for filename, supplier_code in _DOCUMENT_SUPPLIER_CODES.items():
        data = (FIXTURES_DOCUMENTS_DIR / filename).read_bytes()
        sha256 = hashlib.sha256(data).digest()
        document = session.execute(
            select(QuoteDocument).where(
                QuoteDocument.organization_id == organization_id,
                QuoteDocument.content_sha256 == sha256,
            )
        ).scalar_one_or_none()
        if document is None:
            suffix = Path(filename).suffix.lower()
            document = doc_service.upload(
                rfq_id=rfq_a_id,
                supplier_id=supplier_ids[supplier_code],
                filename=filename,
                content_type=_DOCUMENT_CONTENT_TYPES[suffix],
                data=data,
                actor_id=actor_id,
            )
        document_ids[filename] = document.id

    extraction_service = ExtractionService(
        session, organization_id, audit, clock, ids,
        storage=storage, provider=extraction_provider,
    )
    csv_document_id = document_ids[_CSV_DOCUMENT_FILENAME]
    run = session.execute(
        select(ExtractionRun).where(
            ExtractionRun.organization_id == organization_id,
            ExtractionRun.document_id == csv_document_id,
        )
    ).scalar_one_or_none()
    if run is None:
        run = extraction_service.start_run(csv_document_id, actor_id=actor_id)

    correction_count = session.execute(
        select(func.count())
        .select_from(QuoteCorrection)
        .where(
            QuoteCorrection.organization_id == organization_id,
            QuoteCorrection.extraction_run_id == run.id,
        )
    ).scalar_one()
    if not correction_count:
        fields = extraction_service.list_fields(run.id)
        supplier_name_field = next(f for f in fields if f.field_path == "supplier_name")
        extraction_service.correct_field(
            run.id,
            supplier_name_field.id,
            new_value="Nordic Fastener AB",
            reason="Confirmed supplier legal name against Meridian's supplier master record.",
            actor_id=actor_id,
        )

    return document_ids, run.id


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

DEFAULT_MAX_UPLOAD_BYTES: Final[int] = 20 * 1024 * 1024


def seed_demo_dataset(
    session: SaSession,
    *,
    clock: Clock,
    ids: IdGenerator,
    storage: StorageProvider,
    extraction_provider: ExtractionProvider,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> DemoDatasetSummary:
    """Seed (or idempotently upgrade) the full demonstration dataset for the
    Meridian Fabrication Works (Demo) organization. Safe to call repeatedly:
    every step looks its entity up by natural key first and only mutates
    when nothing is found."""
    org, users = seed_identity(session, clock, ids)
    owner = users["owner"]
    audit = AuditRecorder(
        session, clock, ids, organization_id=org.id, actor_user_id=owner.id
    )

    unit_rows = seed_unit_catalog(session, org.id, clock, ids)
    unit_ids = {u.code: u.id for u in unit_rows}

    supplier_ids = _seed_suppliers(session, org.id, audit, clock, ids)
    part_ids = _seed_parts(session, org.id, audit, clock, ids, unit_ids, owner.id)
    bom_ids = _seed_boms(session, org.id, audit, clock, ids, part_ids, owner.id)
    rfq_ids = _seed_rfqs(
        session, org.id, audit, clock, ids, part_ids, bom_ids, supplier_ids, owner.id
    )
    quote_ids = _seed_quotes(
        session, org.id, audit, clock, ids,
        rfq_ids["RFQ-2026-ENC-PILOT"], supplier_ids, unit_ids,
    )
    _seed_matching(session, org.id, audit, clock, ids, quote_ids, owner.id)
    exchange_rate_ids = _seed_fx(session, org.id, audit, clock, ids, owner.id)
    scoring_configuration_ids = _seed_scoring_configs(session, org.id, audit, clock, ids, owner.id)
    scenario_ids = _seed_scenarios(
        session, org.id, audit, clock, ids, rfq_ids["RFQ-2026-ENC-PILOT"], owner.id
    )
    document_ids, extraction_run_id = _seed_documents(
        session, org.id, audit, clock, ids,
        rfq_ids["RFQ-2026-Q3-RACK"], supplier_ids,
        storage, extraction_provider, max_upload_bytes, owner.id,
    )

    return DemoDatasetSummary(
        organization_id=org.id,
        user_ids={role: user.id for role, user in users.items()},
        supplier_ids=supplier_ids,
        part_ids=part_ids,
        unit_ids=unit_ids,
        bom_ids=bom_ids,
        rfq_ids=rfq_ids,
        quote_ids=quote_ids,
        scenario_ids=scenario_ids,
        scoring_configuration_ids=scoring_configuration_ids,
        document_ids=document_ids,
        exchange_rate_ids=exchange_rate_ids,
        extraction_run_id=extraction_run_id,
    )


__all__ = [
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEMO_ORG_NAME",
    "DEMO_SLUG",
    "DEMO_USERS",
    "QUALITY_FIRST_CONFIG_NAME",
    "SCENARIO_ASSUMPTIONS",
    "SCENARIO_INFEASIBLE_NAME",
    "SCENARIO_LOWEST_LANDED_COST_NAME",
    "SCENARIO_SPLIT_NAME",
    "DemoDatasetSummary",
    "seed_demo_dataset",
    "seed_identity",
]
