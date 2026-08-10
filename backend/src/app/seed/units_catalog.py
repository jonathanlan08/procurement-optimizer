"""Standard/global unit catalogue (docs/planning/02-erd.md §4, SPEC §10).

Seeded once, shared by every organization: `UnitDefinition.organization_id` is
NULL for every row this module creates ("null = global catalog", 02-erd.md
§4; docs/planning/09-task-decomposition.md task 2.5 "global unit catalogue
seed"). An organization's own units are separate `UnitDefinition` rows with
that organization's id — not this module's concern.

Exact factors (docs/planning/05-calculation-methodology.md §5): ``1 lb =
0.45359237 kg``, ``1 ft = 0.3048 m``, ``1 in = 0.0254 m`` are legally defined
exact values, not measurements, so they are entered as exact `Decimal`
literals rather than derived arithmetically. ``1 oz = 0.028349523125 kg``
follows from the same legal definition (1 lb / 16, exact).

`pack`/`box`/`tray`/`reel` have no universal `to_canonical_factor`: the
each-per-container ratio is a property of the part, not the word "reel", and
is recorded per part (or as an org default) in `UnitConversion` instead
(05-calculation-methodology.md §5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.ids import IdGenerator
from app.models.units import UnitDefinition, UnitDimension


@dataclass(frozen=True, slots=True)
class CatalogUnit:
    code: str
    display_name: str
    dimension: UnitDimension
    to_canonical_factor: Decimal | None


# Canonical unit per dimension (05-calculation-methodology.md §5): each
# (count), kilogram (mass), meter (length) — factor 1 by definition.
STANDARD_UNIT_CATALOG: tuple[CatalogUnit, ...] = (
    CatalogUnit("each", "Each", UnitDimension.COUNT, Decimal("1")),
    CatalogUnit("pack", "Pack", UnitDimension.COUNT, None),
    CatalogUnit("box", "Box", UnitDimension.COUNT, None),
    CatalogUnit("tray", "Tray", UnitDimension.COUNT, None),
    CatalogUnit("reel", "Reel", UnitDimension.COUNT, None),
    CatalogUnit("kg", "Kilogram", UnitDimension.MASS, Decimal("1")),
    CatalogUnit("g", "Gram", UnitDimension.MASS, Decimal("0.001")),
    CatalogUnit("lb", "Pound", UnitDimension.MASS, Decimal("0.45359237")),
    CatalogUnit("oz", "Ounce", UnitDimension.MASS, Decimal("0.028349523125")),
    CatalogUnit("m", "Meter", UnitDimension.LENGTH, Decimal("1")),
    CatalogUnit("cm", "Centimeter", UnitDimension.LENGTH, Decimal("0.01")),
    CatalogUnit("mm", "Millimeter", UnitDimension.LENGTH, Decimal("0.001")),
    CatalogUnit("ft", "Foot", UnitDimension.LENGTH, Decimal("0.3048")),
    CatalogUnit("in", "Inch", UnitDimension.LENGTH, Decimal("0.0254")),
)


def seed_unit_catalog(
    session: SaSession,
    organization_id: uuid.UUID,
    clock: Clock,
    ids: IdGenerator,
) -> list[UnitDefinition]:
    """Insert the standard catalogue idempotently; skip codes that already exist.

    Global rows (``organization_id IS NULL``) are shared by every
    organization, so this is safe to call from more than one organization's
    bootstrap flow: whichever call arrives first creates the missing rows,
    later calls are no-ops for those codes.

    ``organization_id`` is accepted — and required, matching the shape of
    other per-organization seed routines such as ``scripts/seed_demo.py`` —
    so a caller always states which organization's bootstrap triggered this,
    but it is never written onto the global rows this function creates:
    02-erd.md §4 marks ``unit_definitions.organization_id`` nullable
    specifically so the standard catalogue can be shared, not duplicated per
    org. ``clock`` is accepted for the same interface-consistency reason and
    reserved for a future audit-event emission; 02-erd.md §4 gives
    ``unit_definitions`` no timestamp column, so nothing here calls it today.

    Returns every catalogue ``UnitDefinition`` row that exists after the
    call (pre-existing and newly created), in ``STANDARD_UNIT_CATALOG`` order.
    """
    existing_codes = {
        code.lower()
        for code in session.execute(
            select(UnitDefinition.code).where(UnitDefinition.organization_id.is_(None))
        )
        .scalars()
        .all()
    }

    for entry in STANDARD_UNIT_CATALOG:
        if entry.code.lower() in existing_codes:
            continue
        session.add(
            UnitDefinition(
                id=ids.new_id(),
                organization_id=None,
                code=entry.code,
                display_name=entry.display_name,
                dimension=entry.dimension,
                to_canonical_factor=entry.to_canonical_factor,
                is_user_defined=False,
            )
        )
    session.flush()

    rows = (
        session.execute(select(UnitDefinition).where(UnitDefinition.organization_id.is_(None)))
        .scalars()
        .all()
    )
    by_code = {row.code.lower(): row for row in rows}
    return [
        by_code[entry.code.lower()]
        for entry in STANDARD_UNIT_CATALOG
        if entry.code.lower() in by_code
    ]


__all__ = ["STANDARD_UNIT_CATALOG", "CatalogUnit", "seed_unit_catalog"]
