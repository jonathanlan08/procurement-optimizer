"""Pure unit-quantity conversion (docs/planning/05-calculation-methodology.md §5,
SPEC §10). Pure domain code: no database, no clock, no network, no randomness.

This module does NOT depend on `app.models.units` — the SQLAlchemy
`UnitDefinition`/`UnitConversion` rows are a persistence concern (they carry
`organization_id`, timestamps, audit ids) that has no business being on a
pure conversion function's call stack. `Unit` below is a lightweight,
domain-only mirror of exactly the three fields the arithmetic needs (`code`,
`dimension`, `to_canonical_factor`); the service layer is responsible for
projecting a `UnitDefinition` row into a `Unit` before calling here.

Design notes (interpretations of the methodology's prose):

- **Canonical-base conversion.** §5: `qty_canonical = qty x unit.to_canonical_factor
  x (part_pack_factor if applicable)`. Read literally that formula only ever
  multiplies going *into* canonical form; converting canonical back *out* to
  a target unit is the inverse operation (divide by the target's factor).
  `convert_quantity` composes both legs in one call:
  `value_canonical = value * from_unit.to_canonical_factor`, then
  `result = value_canonical / to_unit.to_canonical_factor` — i.e.
  `value * from_factor / to_factor`, exactly as the objective brief states.
- **Count-dimension containers with no universal factor** (`pack`, `box`,
  `tray`, `reel` — `to_canonical_factor is None`, see
  `app.seed.units_catalog.STANDARD_UNIT_CATALOG`) require an explicit
  per-part factor supplied by the caller (`part_factor`), because "1 reel =
  5000 each" is a property of the part, not the word "reel" (§5). The
  argument name is deliberately singular: it resolves whichever ONE of
  `from_unit`/`to_unit` is missing a factor. If *both* sides of a requested
  conversion lack a universal factor (e.g. `pack` -> `reel` directly), a
  single scalar cannot disambiguate two independent unknowns, so this raises
  `ConversionAssumptionMissingError` naming both units and recommending a
  two-step conversion through `each` — each step supplies its own
  part-specific factor. This case is outside the objective's required test
  matrix but is handled defensively rather than silently misapplying one
  factor to both units.
- **`conversion_note`** spells out the exact ratio applied, in the same
  "1 x = y z" form as the methodology's own examples ("1 lb = 0.45359237
  kg", "1 ft = 0.3048 m"): `to_wire(from_factor / to_factor)`. When either
  factor came from `part_factor` rather than the unit's own catalogue
  definition, the note is suffixed to flag it as a per-part assumption
  (§5: "Every applied conversion is recorded ... and rendered in the UI").
- **Precision policy** matches `app.core.money`: the division runs at full
  `CALC_CONTEXT` precision (34 significant digits) and the quantity result
  is quantized exactly once, at `QTY_SCALE` (6dp) — never mid-expression.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from app.core.money import CALC_CONTEXT, quantize_qty, to_wire


class Dimension(StrEnum):
    """Mirrors `app.models.units.UnitDimension`'s values by convention (so a
    service layer can round-trip a DB row's `dimension` string into this
    enum without translation), without importing the ORM module itself."""

    COUNT = "count"
    MASS = "mass"
    LENGTH = "length"


@dataclass(frozen=True, slots=True)
class Unit:
    """A domain-only mirror of `UnitDefinition`'s three arithmetic-relevant
    fields. `to_canonical_factor` is `None` exactly for count-dimension
    containers with no universal each-ratio (pack/box/tray/reel) — see
    module docstring."""

    code: str
    dimension: Dimension
    to_canonical_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class ConvertedQuantity:
    """The result of a successful `convert_quantity` call."""

    value: Decimal  # quantized at QTY_SCALE
    conversion_note: str  # e.g. "1 lb = 0.45359237 kg"


class DimensionMismatchError(ValueError):
    """`from_unit` and `to_unit` are different dimensions (e.g. mass vs
    count): comparing/converting across dimensions is a modelling error, not
    a warning (05-calculation-methodology.md §5)."""

    def __init__(self, from_unit: Unit, to_unit: Unit) -> None:
        super().__init__(
            f"cannot convert {from_unit.code} ({from_unit.dimension.value}) to "
            f"{to_unit.code} ({to_unit.dimension.value}): incompatible dimensions"
        )
        self.from_unit = from_unit
        self.to_unit = to_unit


class ConversionAssumptionMissingError(ValueError):
    """A unit with no universal `to_canonical_factor` (a count-dimension
    container) was used without a `part_factor` to resolve it. Never guess a
    pack size — that is "the single easiest way to produce a 5000x error"
    (05-calculation-methodology.md §5)."""

    def __init__(self, unit: Unit, *, detail: str | None = None) -> None:
        message = detail or (
            f"'{unit.code}' has no universal to-canonical factor and no "
            "part_factor was supplied; a count-dimension container's "
            "each-ratio is a property of the specific part, not the unit "
            "name, so it cannot be assumed"
        )
        super().__init__(message)
        self.unit = unit


def _resolve_factor(unit: Unit, part_factor: Decimal | None) -> tuple[Decimal, bool]:
    """Returns (factor, came_from_part_factor)."""
    if unit.to_canonical_factor is not None:
        return unit.to_canonical_factor, False
    if part_factor is None:
        raise ConversionAssumptionMissingError(unit)
    return part_factor, True


def convert_quantity(
    value: Decimal,
    from_unit: Unit,
    to_unit: Unit,
    *,
    part_factor: Decimal | None = None,
) -> ConvertedQuantity:
    """Convert `value` (in `from_unit`) to `to_unit`, via the canonical base.

    `part_factor` supplies the missing `to_canonical_factor` for whichever
    ONE of `from_unit`/`to_unit` is a count-dimension container with no
    universal factor (pack/box/tray/reel) — e.g. converting `pack` -> `each`
    with `part_factor=Decimal("50")` means "1 pack = 50 each" for this part.

    Raises `DimensionMismatchError` if the two units are not the same
    dimension, and `ConversionAssumptionMissingError` if a required factor is
    absent (including the case where both units lack a universal factor,
    which a single `part_factor` cannot resolve).
    """
    if from_unit.dimension != to_unit.dimension:
        raise DimensionMismatchError(from_unit, to_unit)

    if from_unit.to_canonical_factor is None and to_unit.to_canonical_factor is None:
        raise ConversionAssumptionMissingError(
            from_unit,
            detail=(
                f"both '{from_unit.code}' and '{to_unit.code}' have no universal "
                "to-canonical factor; a single part_factor cannot resolve both — "
                "convert via 'each' in two explicit steps, each with its own "
                "part-specific factor"
            ),
        )

    from_factor, from_is_assumption = _resolve_factor(from_unit, part_factor)
    to_factor, to_is_assumption = _resolve_factor(to_unit, part_factor)

    with localcontext(CALC_CONTEXT):
        canonical = value * from_factor
        converted = canonical / to_factor
        ratio = from_factor / to_factor
    result = quantize_qty(converted)

    note = f"1 {from_unit.code} = {to_wire(ratio)} {to_unit.code}"
    if from_is_assumption or to_is_assumption:
        note += " (per-part conversion assumption)"

    return ConvertedQuantity(value=result, conversion_note=note)


__all__ = [
    "ConversionAssumptionMissingError",
    "ConvertedQuantity",
    "Dimension",
    "DimensionMismatchError",
    "Unit",
    "convert_quantity",
]
