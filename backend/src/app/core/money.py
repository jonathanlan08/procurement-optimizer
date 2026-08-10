"""Decimal money policy — PRINCIPAL-OWNED.

Every monetary value in this codebase is a ``decimal.Decimal``. Binary floats are
forbidden for money and quantities; JSON carries decimals as strings.

Policy (docs/planning/05-calculation-methodology.md §1, ratified in 00-decisions.md):
- working precision 34, banker's rounding (ROUND_HALF_EVEN)
- arithmetic runs at full precision inside a formula; each result is quantized once
  at its boundary scale, and displayed components always sum exactly to the
  displayed total
- traps are enabled: InvalidOperation, DivisionByZero, Overflow raise instead of
  silently producing NaN/Infinity
"""

from __future__ import annotations

from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Final

CALC_PRECISION: Final[int] = 34

MONEY_SCALE: Final[Decimal] = Decimal("0.000001")  # 6 dp -> NUMERIC(18,6)
UNIT_PRICE_SCALE: Final[Decimal] = Decimal("0.00000001")  # 8 dp -> NUMERIC(18,8)
RATE_SCALE: Final[Decimal] = Decimal("0.000000000001")  # 12 dp -> NUMERIC(24,12)
QTY_SCALE: Final[Decimal] = Decimal("0.000001")  # 6 dp
RATIO_SCALE: Final[Decimal] = Decimal("0.000001")  # 6 dp
DISPLAY_SCALE: Final[Decimal] = Decimal("0.01")  # presentation only

ROUNDING: Final[str] = ROUND_HALF_EVEN

CALC_CONTEXT: Final[Context] = Context(
    prec=CALC_PRECISION,
    rounding=ROUNDING,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


class MoneyError(ValueError):
    """Base class for monetary parsing/validation failures."""


class InvalidDecimalString(MoneyError):
    def __init__(self, raw: str) -> None:
        super().__init__(f"not a valid decimal string: {raw!r}")
        self.raw = raw


class ScaleExceeded(MoneyError):
    """The value has more fractional digits than its column/scale allows."""

    def __init__(self, raw: str, scale: Decimal) -> None:
        super().__init__(f"value {raw!r} exceeds allowed scale {scale}")
        self.raw = raw
        self.scale = scale


def calc_context() -> Context:
    """A copy of the calculation context, safe to mutate locally."""
    return CALC_CONTEXT.copy()


def parse_decimal(raw: str | int | Decimal) -> Decimal:
    """Parse user/API input into a Decimal. Floats are rejected by type."""
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):  # pragma: no cover - defensive; typing forbids it
        raise InvalidDecimalString(repr(raw))
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise InvalidDecimalString(raw) from exc
    if not value.is_finite():
        raise InvalidDecimalString(raw)
    return value


def parse_at_scale(raw: str | int | Decimal, scale: Decimal) -> Decimal:
    """Parse and reject (not round) input finer than ``scale``.

    API rule: scale beyond the column is a validation error, never a silent round.
    """
    value = parse_decimal(raw)
    scale_exp = scale.as_tuple().exponent
    value_exp = value.as_tuple().exponent
    if not isinstance(scale_exp, int) or not isinstance(value_exp, int):
        # 'n'/'N'/'F' exponents mean NaN/Infinity; parse_decimal already rejects
        raise InvalidDecimalString(str(raw))
    if -value_exp > -scale_exp:
        raise ScaleExceeded(str(raw), scale)
    return value


def quantize(value: Decimal, scale: Decimal) -> Decimal:
    """Quantize at a boundary scale using the calculation context."""
    with localcontext(CALC_CONTEXT):
        return value.quantize(scale)


def quantize_money(value: Decimal) -> Decimal:
    return quantize(value, MONEY_SCALE)


def quantize_unit_price(value: Decimal) -> Decimal:
    return quantize(value, UNIT_PRICE_SCALE)


def quantize_rate(value: Decimal) -> Decimal:
    return quantize(value, RATE_SCALE)


def quantize_qty(value: Decimal) -> Decimal:
    return quantize(value, QTY_SCALE)


def to_wire(value: Decimal) -> str:
    """Serialize for JSON. Money never crosses the wire as a JSON number."""
    return format(value, "f")
