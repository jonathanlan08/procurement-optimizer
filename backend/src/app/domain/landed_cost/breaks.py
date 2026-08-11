"""Price-break tier selection (`docs/planning/05-calculation-methodology.md` §6,
`docs/planning/00-decisions.md` §2 ruling 1).

**All-units discount semantics**: the selected tier's `unit_price` applies to
the *entire* accepted quantity, not just the units above the tier's minimum.
Incremental (marginal-unit) discounts are not modelled in v0.1 — they are
roadmap.

Interval semantics: **closed on both ends**, `[min_quantity, max_quantity]`,
with `max_quantity = None` meaning unbounded (an open-ended top tier). The
SPEC's example table (1-99 / 100-499 / 500-999 / 1000+) is exactly this
shape.

Tiers are expected to already be non-overlapping and gap-free (that is a
write-time validation concern — a DB exclusion constraint per
`02-erd.md` §8); this function is defensive about ordering (it sorts by
`min_quantity` before selecting) but does not itself validate the absence of
overlaps or gaps.

Two situations return `tier=None` rather than "nearest match":

- the quantity is below every tier's minimum (`BelowMinimumTierError` territory
  at the caller); and
- the quantity falls in a genuine gap between two tiers (a data defect
  upstream, since well-formed tier sets are contiguous) — the `reason` names
  the gap's bounding tiers so the caller can report it precisely.

Nearest-matching a quantity to an adjacent tier is deliberately never done:
guessing a price break is exactly the kind of silent fabrication this
codebase's value semantics (`app/domain/values.py`) forbid.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.core.money import to_wire
from app.domain.landed_cost.contracts import PriceBreakSelection, PriceBreakTier


def _describe(tier: PriceBreakTier) -> str:
    upper = "open-ended" if tier.max_quantity is None else to_wire(tier.max_quantity)
    return f"{to_wire(tier.min_quantity)}-{upper}"


def select_price_break(tiers: Sequence[PriceBreakTier], quantity: Decimal) -> PriceBreakSelection:
    sorted_tiers = tuple(sorted(tiers, key=lambda t: t.min_quantity))

    if not sorted_tiers:
        return PriceBreakSelection(
            tier=None,
            reason="no price-break tiers defined",
            tiers_considered=sorted_tiers,
        )

    if quantity < sorted_tiers[0].min_quantity:
        return PriceBreakSelection(
            tier=None,
            reason=(
                f"quantity {to_wire(quantity)} is below the minimum quantity "
                f"{to_wire(sorted_tiers[0].min_quantity)} of the lowest tier"
            ),
            tiers_considered=sorted_tiers,
        )

    # Ascending order, non-overlapping tiers: the highest tier whose
    # min_quantity <= quantity is the only tier that can possibly match.
    candidate: PriceBreakTier | None = None
    candidate_index = -1
    for index, tier in enumerate(sorted_tiers):
        if quantity >= tier.min_quantity:
            candidate = tier
            candidate_index = index
        else:
            break

    if candidate is None:  # pragma: no cover - excluded by the below-minimum check above
        return PriceBreakSelection(
            tier=None,
            reason=f"quantity {to_wire(quantity)} matches no tier",
            tiers_considered=sorted_tiers,
        )

    max_q = candidate.max_quantity
    if max_q is None or quantity <= max_q:
        return PriceBreakSelection(
            tier=candidate,
            reason=f"quantity {to_wire(quantity)} falls in tier {_describe(candidate)}",
            tiers_considered=sorted_tiers,
        )

    # Genuine gap: candidate's max is exceeded, and the next tier (if any)
    # has a minimum still above the quantity.
    next_tier = (
        sorted_tiers[candidate_index + 1] if candidate_index + 1 < len(sorted_tiers) else None
    )
    if next_tier is not None:
        reason = (
            f"quantity {to_wire(quantity)} falls in the gap between tier "
            f"{_describe(candidate)} and tier {_describe(next_tier)}"
        )
    else:
        reason = (
            f"quantity {to_wire(quantity)} is above the highest tier's maximum "
            f"({to_wire(max_q)}) and no higher tier exists"
        )
    return PriceBreakSelection(tier=None, reason=reason, tiers_considered=sorted_tiers)
