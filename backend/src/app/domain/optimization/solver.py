"""Order-allocation CP-SAT solver — implementation of the frozen
`AllocationSolver` shape described in `contracts.py`.

Methodology: `docs/planning/06-optimization-methodology.md`. This module
implements the model of §3-§5 against the *frozen* `contracts.py` data
shapes, which are simpler than the methodology's full parameter set (no
supplier-level fixed cost, no separate `var_{s,l}` per-unit adder, no
lexicographic strategies, no oversupply) — every `Offer` already carries a
single fully landed per-unit cost per tier plus one fixed cost, so the
material term and the `var` term of §5 collapse into one.

Design notes (interpretations of the task spec, spelled out because the
contract's docstring is normative but not exhaustive pseudocode):

- **Canonical ordering.** `06-optimization-methodology.md` §6 sorts suppliers
  by `(code, id)` and lines by `(line_number, id)`; neither field exists on
  the frozen `DemandLine`/`Offer` dataclasses. This module sorts lines by
  `str(rfq_line_id)` and offers by `(str(rfq_line_id), str(supplier_id),
  str(quote_line_id))` — the only stable, always-present keys — which still
  gives a pure function of the data and is asserted by the reordering test.
- **`model_hash`** is a SHA-256 over a canonical JSON *of the input*
  (sorted, ids as strings, Decimals as `to_wire` strings), not over the
  built `CpModel` proto — cheaper, and it lets a pre-solve INFEASIBLE result
  (no solver call at all) still carry a meaningful, reproducible hash.
- **Tier cost linearisation.** Rather than the methodology's `q[s,l,t]`
  "quantity priced at tier `t`" auxiliary variables, this module ties an
  `offer_cost` integer variable directly to `alloc` per tier via
  `OnlyEnforceIf`: `offer_cost == scaled_unit_cost_t * alloc` when tier `t`
  is the active tier. This is exactly linear (`alloc` is not split across
  tiers) and is big-M-free because the enforcement is conditional, not a
  bound relaxation.
- **Deterministic tie-break (methodology §6) is deferred.** CP-SAT with
  `num_search_workers=1`, `random_seed=0`, and canonical variable-creation
  order is deterministic for a fixed model — repeat solves and permuted-input
  solves of the *same* problem provably return the same proto and therefore
  the same solution (asserted by the determinism tests). What is not
  guaranteed is which of several *exactly*-tied optima a future code change
  might surface. The methodology's epsilon tie-break term is real scope but
  is not part of this task's deliverable; flagged here for the principal
  rather than silently added or silently skipped.
- **Assumption-literal groups.** Six relaxable constraint groups —
  `moq`, `capacity`, `supplier_count`, `concentration`, `budget`, `locks` —
  are always represented by boolean literals in the main solve (even when a
  problem does not use that constraint) so that
  `SufficientAssumptionsForInfeasibility` has a uniform, canonical set to
  reason over. `demand` and the tier/linking structure are never gated: they
  are not "policy" constraints a user could choose to relax.
- **Rejected alternatives, scoped to the task.** Only the single-supplier
  comparison is computed (one extra deterministic solve with
  `max_supplier_count=1`), per the task's explicit scope — not the
  methodology's full "every supplier" + "next-best split" sweep.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Final
from uuid import UUID

from ortools.sat.python import cp_model

from app.core.money import CALC_CONTEXT, quantize, quantize_money
from app.domain.landed_cost.breaks import select_price_break
from app.domain.landed_cost.contracts import PriceBreakTier
from app.domain.optimization.contracts import (
    MONEY_INT_SCALE,
    AllocationConstraints,
    AllocationEntry,
    AllocationProblem,
    AllocationResult,
    AllocationStatus,
    BindingConstraint,
    DemandLine,
    InfeasibilityExplanation,
    Offer,
    OfferTier,
    SolverStats,
)

INT64_MAX: Final[int] = 2**63 - 1
SCALE_OVERFLOW_THRESHOLD: Final[int] = INT64_MAX // 4
CONCENTRATION_DENOM: Final[int] = 1_000_000  # 6dp fraction resolution for the concentration cap

_GROUP_ORDER: Final[tuple[str, ...]] = (
    "moq",
    "capacity",
    "supplier_count",
    "concentration",
    "budget",
    "locks",
)


class ScalingOverflowError(ValueError):
    """A scaled money coefficient, or the worst-case scaled objective bound,
    exceeds `SCALE_OVERFLOW_THRESHOLD` (int64 / 4). Raised before any solver
    call is made."""


class ConsistencyError(RuntimeError):
    """The exact Decimal re-selection of a price-break tier at the allocated
    quantity disagrees with the tier CP-SAT chose. This indicates a modelling
    bug (the tier-linking constraints and `breaks.select_price_break` have
    diverged) — it must never be silently swallowed."""


# --------------------------------------------------------------------------
# Money scaling (docs/planning/06-optimization-methodology.md §7)
# --------------------------------------------------------------------------


def _scale_money(value: Decimal) -> int:
    """Decimal money -> int64 scaled units, ROUND_HALF_EVEN, via the
    core.money calculation context."""
    return int(quantize(value * MONEY_INT_SCALE, Decimal(1)))


def _unscale_money(raw: int) -> Decimal:
    """Inverse of `_scale_money`, for human-readable sentences ONLY — never
    used for the authoritative `expected_total_cost`."""
    with localcontext(CALC_CONTEXT):
        value = Decimal(raw) / Decimal(MONEY_INT_SCALE)
    return quantize_money(value)


# --------------------------------------------------------------------------
# Canonical serialization + model_hash
# --------------------------------------------------------------------------


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _canonical_problem_dict(problem: AllocationProblem) -> dict[str, object]:
    lines = sorted(problem.lines, key=lambda line: str(line.rfq_line_id))
    offers = sorted(
        problem.offers,
        key=lambda o: (str(o.rfq_line_id), str(o.supplier_id), str(o.quote_line_id)),
    )
    pre_excluded = sorted(problem.pre_excluded, key=lambda e: str(e.quote_line_id))
    locks = sorted(
        problem.constraints.locked_allocations,
        key=lambda lk: (str(lk.rfq_line_id), str(lk.supplier_id)),
    )
    excluded_suppliers = sorted(str(s) for s in problem.constraints.excluded_supplier_ids)

    def tier_dict(t: OfferTier) -> dict[str, object]:
        return {
            "min_quantity": t.min_quantity,
            "max_quantity": t.max_quantity,
            "landed_unit_cost": _decimal_str(t.landed_unit_cost),
        }

    def offer_dict(o: Offer) -> dict[str, object]:
        tiers = sorted(o.tiers, key=lambda t: t.min_quantity)
        return {
            "quote_line_id": str(o.quote_line_id),
            "rfq_line_id": str(o.rfq_line_id),
            "supplier_id": str(o.supplier_id),
            "supplier_label": o.supplier_label,
            "moq": o.moq,
            "capacity": o.capacity,
            "fixed_cost": _decimal_str(o.fixed_cost),
            "incomplete_landed_cost": o.incomplete_landed_cost,
            "tiers": [tier_dict(t) for t in tiers],
        }

    return {
        "lines": [
            {
                "rfq_line_id": str(line.rfq_line_id),
                "part_label": line.part_label,
                "required_quantity": line.required_quantity,
            }
            for line in lines
        ],
        "offers": [offer_dict(o) for o in offers],
        "constraints": {
            "max_supplier_count": problem.constraints.max_supplier_count,
            "max_concentration": _decimal_str(problem.constraints.max_concentration),
            "budget_limit": _decimal_str(problem.constraints.budget_limit),
            "locked_allocations": [
                {
                    "rfq_line_id": str(lk.rfq_line_id),
                    "supplier_id": str(lk.supplier_id),
                    "quantity": lk.quantity,
                }
                for lk in locks
            ],
            "excluded_supplier_ids": excluded_suppliers,
            "allow_incomplete_offers": problem.constraints.allow_incomplete_offers,
        },
        "pre_excluded": [
            {
                "quote_line_id": str(e.quote_line_id),
                "supplier_label": e.supplier_label,
                "reason": e.reason,
            }
            for e in pre_excluded
        ],
    }


def _model_hash(problem: AllocationProblem) -> str:
    canonical = _canonical_problem_dict(problem)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Pre-solve filtering
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Exclusion:
    quote_line_id: UUID
    rfq_line_id: UUID
    supplier_label: str
    reason: str


def _sorted_offers(offers: tuple[Offer, ...]) -> tuple[Offer, ...]:
    return tuple(
        sorted(
            offers,
            key=lambda o: (str(o.rfq_line_id), str(o.supplier_id), str(o.quote_line_id)),
        )
    )


def _filter_offers(problem: AllocationProblem) -> tuple[tuple[Offer, ...], tuple[_Exclusion, ...]]:
    """Drops offers pre-excluded upstream, user-excluded suppliers, and
    incomplete-landed-cost offers (unless overridden). Orphaned offers
    (referencing a line not in `problem.lines`) are dropped defensively."""
    pre_excluded_ids = {e.quote_line_id for e in problem.pre_excluded}
    user_excluded_suppliers = set(problem.constraints.excluded_supplier_ids)
    line_ids = {line.rfq_line_id for line in problem.lines}

    usable: list[Offer] = []
    excluded: list[_Exclusion] = []
    for o in _sorted_offers(problem.offers):
        if o.quote_line_id in pre_excluded_ids:
            continue  # already explained upstream; pass-through drop, no new reason needed
        if o.rfq_line_id not in line_ids:
            excluded.append(
                _Exclusion(
                    o.quote_line_id,
                    o.rfq_line_id,
                    o.supplier_label,
                    "offer references a demand line not present in this problem",
                )
            )
            continue
        if o.supplier_id in user_excluded_suppliers:
            excluded.append(
                _Exclusion(
                    o.quote_line_id,
                    o.rfq_line_id,
                    o.supplier_label,
                    f"{o.supplier_label} is excluded from this scenario",
                )
            )
            continue
        if o.incomplete_landed_cost and not problem.constraints.allow_incomplete_offers:
            excluded.append(
                _Exclusion(
                    o.quote_line_id,
                    o.rfq_line_id,
                    o.supplier_label,
                    f"{o.supplier_label}'s landed cost is incomplete and allow_incomplete_offers "
                    "is not set",
                )
            )
            continue
        usable.append(o)
    return tuple(usable), tuple(excluded)


def _upper_bound(offer: Offer, demand: int) -> int:
    if offer.capacity is None:
        return demand
    return min(demand, offer.capacity)


def _dead_reason(offer: Offer, upper: int) -> str | None:
    if upper <= 0:
        return f"{offer.supplier_label} has no usable capacity against this line's demand"
    if offer.moq is not None and offer.moq > upper:
        return (
            f"{offer.supplier_label}'s minimum order quantity ({offer.moq}) exceeds the "
            f"{upper} units available to it"
        )
    return None


def _partition_live_and_dead(
    usable: tuple[Offer, ...], line_by_id: dict[UUID, DemandLine]
) -> tuple[tuple[Offer, ...], tuple[_Exclusion, ...]]:
    live: list[Offer] = []
    dead: list[_Exclusion] = []
    for o in usable:
        line = line_by_id[o.rfq_line_id]
        upper = _upper_bound(o, line.required_quantity)
        reason = _dead_reason(o, upper)
        if reason is not None:
            dead.append(_Exclusion(o.quote_line_id, o.rfq_line_id, o.supplier_label, reason))
        else:
            live.append(o)
    return tuple(live), tuple(dead)


def _lines_with_no_supply(
    lines: tuple[DemandLine, ...], live_offers: tuple[Offer, ...]
) -> tuple[DemandLine, ...]:
    covered = {o.rfq_line_id for o in live_offers}
    return tuple(
        line for line in lines if line.required_quantity > 0 and line.rfq_line_id not in covered
    )


def _no_supply_explanation(
    lines_without_supply: tuple[DemandLine, ...], dead_and_excluded: tuple[_Exclusion, ...]
) -> InfeasibilityExplanation:
    by_line: dict[UUID, list[_Exclusion]] = defaultdict(list)
    for ex in dead_and_excluded:
        by_line[ex.rfq_line_id].append(ex)

    sentences: list[str] = []
    groups: set[str] = set()
    for line in sorted(lines_without_supply, key=lambda line: str(line.rfq_line_id)):
        reasons = by_line.get(line.rfq_line_id, [])
        if reasons and all("minimum order quantity" in r.reason for r in reasons):
            groups.add("moq")
        else:
            groups.add("capacity")
        reason_text = "; ".join(r.reason for r in reasons) or "no supplier quoted this line"
        sentences.append(
            f"line {line.part_label} requires {line.required_quantity} units but has no "
            f"eligible supplier ({reason_text})"
        )
    return InfeasibilityExplanation(
        conflicting_groups=tuple(sorted(groups)),
        detail=" ".join(sentences),
        minimal_relaxation=None,
    )


def _capacity_shortfall_explanation(
    lines: tuple[DemandLine, ...], live_offers: tuple[Offer, ...]
) -> InfeasibilityExplanation | None:
    offers_by_line: dict[UUID, list[Offer]] = defaultdict(list)
    for o in live_offers:
        offers_by_line[o.rfq_line_id].append(o)

    sentences: list[str] = []
    for line in lines:
        offers = offers_by_line.get(line.rfq_line_id, [])
        if not offers:
            continue  # covered by _no_supply_explanation
        if any(o.capacity is None for o in offers):
            continue  # unlimited capacity present -> cannot be capacity-short
        total_capacity = sum(o.capacity for o in offers if o.capacity is not None)
        if total_capacity < line.required_quantity:
            sentences.append(
                f"total capacity for line {line.part_label} is {total_capacity} units; "
                f"{line.required_quantity} are required"
            )
    if not sentences:
        return None
    return InfeasibilityExplanation(
        conflicting_groups=("capacity",),
        detail=" ".join(sentences),
        minimal_relaxation=(
            "raising the capacity of at least one eligible supplier per short line restores "
            "feasibility"
        ),
    )


def _budget_lower_bound_explanation(
    problem: AllocationProblem, lines: tuple[DemandLine, ...], live_offers: tuple[Offer, ...]
) -> InfeasibilityExplanation | None:
    budget = problem.constraints.budget_limit
    if budget is None:
        return None
    offers_by_line: dict[UUID, list[Offer]] = defaultdict(list)
    for o in live_offers:
        offers_by_line[o.rfq_line_id].append(o)

    with localcontext(CALC_CONTEXT):
        total_lower_bound = Decimal("0")
        for line in lines:
            offers = offers_by_line.get(line.rfq_line_id, [])
            if not offers or line.required_quantity <= 0:
                continue
            cheapest_unit = min(t.landed_unit_cost for o in offers for t in o.tiers)
            total_lower_bound += cheapest_unit * line.required_quantity
        total_lower_bound = quantize_money(total_lower_bound)

    if budget >= total_lower_bound:
        return None
    return InfeasibilityExplanation(
        conflicting_groups=("budget",),
        detail=(
            f"budget of {format(budget, 'f')} is below the cheapest possible allocation of "
            f"{format(total_lower_bound, 'f')}"
        ),
        minimal_relaxation=(
            f"raising budget_limit to at least {format(total_lower_bound, 'f')} restores "
            "feasibility"
        ),
    )


def _concentration_structural_explanation(
    problem: AllocationProblem, live_offers: tuple[Offer, ...]
) -> InfeasibilityExplanation | None:
    rho = problem.constraints.max_concentration
    if rho is None or rho <= 0:
        return None
    eligible_count = len({o.supplier_id for o in live_offers})
    max_k = problem.constraints.max_supplier_count
    effective = eligible_count if max_k is None else min(eligible_count, max_k)
    if effective <= 0:
        return None  # covered by _no_supply_explanation
    with localcontext(CALC_CONTEXT):
        if Decimal(effective) * rho >= 1:
            return None
        required = int((Decimal(1) / rho).to_integral_value(rounding=ROUND_CEILING))
    limiter = (
        "max_supplier_count"
        if (max_k is not None and max_k <= eligible_count)
        else "eligible suppliers"
    )
    detail = (
        f"a {format(rho, 'f')} concentration cap requires at least {required} suppliers; "
        f"only {effective} are available ({limiter})"
    )
    return InfeasibilityExplanation(
        conflicting_groups=("concentration",),
        detail=detail,
        minimal_relaxation=(
            f"raising max_concentration above {format(rho, 'f')}, or the eligible supplier "
            f"count to {required}, restores feasibility"
        ),
    )


def _validate_locks(
    problem: AllocationProblem, lines: tuple[DemandLine, ...], live_offers: tuple[Offer, ...]
) -> tuple[dict[UUID, int], InfeasibilityExplanation | None]:
    line_by_id = {line.rfq_line_id: line for line in lines}
    offer_by_line_supplier = {(o.rfq_line_id, o.supplier_id): o for o in live_offers}
    locked: dict[UUID, int] = {}
    problems: list[str] = []
    demand_used: dict[UUID, int] = defaultdict(int)

    for lock in problem.constraints.locked_allocations:
        line = line_by_id.get(lock.rfq_line_id)
        if line is None:
            problems.append(
                f"locked allocation refers to a demand line not present in this problem "
                f"({lock.rfq_line_id})"
            )
            continue
        offer = offer_by_line_supplier.get((lock.rfq_line_id, lock.supplier_id))
        if offer is None:
            problems.append(
                f"locked allocation for line {line.part_label} refers to a supplier that is "
                "not eligible for that line (excluded, incomplete, dead, or no quote)"
            )
            continue
        upper = _upper_bound(offer, line.required_quantity)
        if lock.quantity < 0 or lock.quantity > upper:
            problems.append(
                f"locked quantity {lock.quantity} for {offer.supplier_label} on line "
                f"{line.part_label} exceeds the {upper}-unit limit available to that supplier"
            )
            continue
        if offer.moq is not None and 0 < lock.quantity < offer.moq:
            problems.append(
                f"locked quantity {lock.quantity} for {offer.supplier_label} on line "
                f"{line.part_label} is below that supplier's minimum order quantity ({offer.moq})"
            )
            continue
        if lock.quantity > 0:
            price_break_tiers = tuple(
                PriceBreakTier(
                    min_quantity=Decimal(t.min_quantity),
                    max_quantity=None if t.max_quantity is None else Decimal(t.max_quantity),
                    unit_price=t.landed_unit_cost,
                )
                for t in sorted(offer.tiers, key=lambda t: t.min_quantity)
            )
            selection = select_price_break(price_break_tiers, Decimal(lock.quantity))
            if selection.tier is None:
                problems.append(
                    f"locked quantity {lock.quantity} for {offer.supplier_label} on line "
                    f"{line.part_label} does not fall within any price-break tier "
                    f"({selection.reason})"
                )
                continue
        locked[offer.quote_line_id] = lock.quantity
        demand_used[lock.rfq_line_id] += lock.quantity

    for line in lines:
        used = demand_used.get(line.rfq_line_id, 0)
        if used > line.required_quantity:
            problems.append(
                f"locked allocations for line {line.part_label} sum to {used} units, more than "
                f"the {line.required_quantity} required"
            )

    if problems:
        return {}, InfeasibilityExplanation(
            conflicting_groups=("locks",),
            detail=" ".join(problems),
            minimal_relaxation=(
                "removing or adjusting the conflicting locked allocation(s) restores feasibility"
            ),
        )
    return locked, None


def _max_scaling_error(
    lines: tuple[DemandLine, ...], live_offers: tuple[Offer, ...]
) -> Decimal:
    """06-optimization-methodology.md §7.4 (2026-08 calculation audit F1):
    each scaled per-unit cost is off by at most 5e-5 (half-even at 4 dp), so
    a full allocation's scaled total is off by at most 5e-5 x allocated
    units + 5e-5 per nonzero fixed cost — and comparing the chosen solution
    against a passed-over alternative doubles the band. This is the amount
    by which an unconsidered alternative could be cheaper in exact space
    while 'optimal' is still truthful in scaled space."""
    total_units = sum(line.required_quantity for line in lines)
    fixed_terms = sum(1 for o in live_offers if o.fixed_cost != 0)
    return Decimal(2) * Decimal("0.00005") * Decimal(total_units + fixed_terms)


def _check_overflow(
    lines: tuple[DemandLine, ...],
    live_offers: tuple[Offer, ...],
    *,
    has_concentration_cap: bool = False,
) -> int:
    """Raises `ScalingOverflowError` before any variable is created. Returns
    the worst-case scaled objective bound (reused as variable domains).

    With a concentration cap, the constraint multiplies scaled spend by
    `CONCENTRATION_DENOM` (10^6), so the effective ceiling is int64 / 10^6 —
    otherwise CP-SAT returns an opaque MODEL_INVALID above ~$922M total
    (2026-08 calculation audit F8)."""
    line_by_id = {line.rfq_line_id: line for line in lines}
    worst_case = 0
    for o in live_offers:
        line = line_by_id[o.rfq_line_id]
        upper = _upper_bound(o, line.required_quantity)
        max_unit_scaled = 0
        for t in o.tiers:
            scaled = _scale_money(t.landed_unit_cost)
            if scaled > SCALE_OVERFLOW_THRESHOLD:
                raise ScalingOverflowError(
                    f"scaled unit cost for {o.supplier_label} ({scaled}) exceeds the overflow "
                    f"threshold {SCALE_OVERFLOW_THRESHOLD}"
                )
            max_unit_scaled = max(max_unit_scaled, scaled)
        fixed_scaled = _scale_money(o.fixed_cost)
        if fixed_scaled > SCALE_OVERFLOW_THRESHOLD:
            raise ScalingOverflowError(
                f"scaled fixed cost for {o.supplier_label} ({fixed_scaled}) exceeds the "
                f"overflow threshold {SCALE_OVERFLOW_THRESHOLD}"
            )
        worst_case += max_unit_scaled * upper + fixed_scaled
        if worst_case > SCALE_OVERFLOW_THRESHOLD:
            raise ScalingOverflowError(
                f"worst-case scaled objective bound ({worst_case}) exceeds the overflow "
                f"threshold {SCALE_OVERFLOW_THRESHOLD}"
            )
    if has_concentration_cap and worst_case > INT64_MAX // CONCENTRATION_DENOM:
        raise ScalingOverflowError(
            f"worst-case scaled spend ({worst_case}) times the concentration "
            f"denominator ({CONCENTRATION_DENOM}) would overflow int64; the "
            "max_concentration constraint cannot be modelled at this problem "
            "size — remove the concentration cap or reduce the total cost scale"
        )
    return worst_case


# --------------------------------------------------------------------------
# CP-SAT model construction
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BuildContext:
    lines: tuple[DemandLine, ...]
    live_offers: tuple[Offer, ...]
    line_by_id: dict[UUID, DemandLine]
    upper_by_offer: dict[UUID, int]
    locked_by_offer: dict[UUID, int]
    constraints: AllocationConstraints
    total_cost_upper_bound: int


@dataclass(frozen=True, slots=True)
class _BuiltModel:
    model: cp_model.CpModel
    alloc: dict[UUID, cp_model.IntVar]
    offer_used: dict[UUID, cp_model.IntVar]
    tier_bool: dict[UUID, list[cp_model.IntVar]]
    offer_cost: dict[UUID, cp_model.IntVar]
    supplier_used: dict[UUID, cp_model.IntVar]
    supplier_spend: dict[UUID, cp_model.IntVar]
    total_cost: cp_model.IntVar
    group_literals: dict[str, cp_model.IntVar] | None  # only set in assumption mode


def _build_model(ctx: _BuildContext, relax: frozenset[str] | None) -> _BuiltModel:
    """Builds a fresh CP-SAT model. `relax is None` -> assumption mode: all
    six relaxable groups are gated by boolean literals and registered via
    `AddAssumptions`, used for the main solve so an INFEASIBLE result yields
    a genuine minimal core. `relax is not None` -> direct mode: constraints
    for groups named in `relax` are omitted entirely (used for the
    single-supplier alternative and the minimal-relaxation search)."""
    assumption_mode = relax is None
    active_relax = relax or frozenset()
    model = cp_model.CpModel()

    offers_by_line: dict[UUID, list[Offer]] = defaultdict(list)
    offers_by_supplier: dict[UUID, list[Offer]] = defaultdict(list)
    for o in ctx.live_offers:
        offers_by_line[o.rfq_line_id].append(o)
        offers_by_supplier[o.supplier_id].append(o)

    group_literals: dict[str, cp_model.IntVar] = {}
    if assumption_mode:
        for name in _GROUP_ORDER:
            group_literals[name] = model.new_bool_var(f"assume_{name}")

    alloc: dict[UUID, cp_model.IntVar] = {}
    offer_used: dict[UUID, cp_model.IntVar] = {}
    tier_bool: dict[UUID, list[cp_model.IntVar]] = {}
    offer_cost: dict[UUID, cp_model.IntVar] = {}

    for o in ctx.live_offers:  # canonically sorted by the caller
        qid = o.quote_line_id
        upper = ctx.upper_by_offer[qid]
        alloc_var = model.new_int_var(0, upper, f"alloc_{qid}")
        used_var = model.new_bool_var(f"used_{qid}")
        alloc[qid] = alloc_var
        offer_used[qid] = used_var

        # used[o] <=> alloc[o] > 0
        model.add(alloc_var <= upper * used_var)
        model.add(alloc_var >= used_var)

        if o.moq is not None:
            if assumption_mode:
                model.add(alloc_var >= o.moq).only_enforce_if([used_var, group_literals["moq"]])
            elif "moq" not in active_relax:
                model.add(alloc_var >= o.moq).only_enforce_if(used_var)

        if o.capacity is not None:
            if assumption_mode:
                model.add(alloc_var <= o.capacity).only_enforce_if(group_literals["capacity"])
            elif "capacity" not in active_relax:
                model.add(alloc_var <= o.capacity)

        locked_qty = ctx.locked_by_offer.get(qid)
        if locked_qty is not None:
            if assumption_mode:
                model.add(alloc_var == locked_qty).only_enforce_if(group_literals["locks"])
            elif "locks" not in active_relax:
                model.add(alloc_var == locked_qty)

        tiers_sorted = tuple(sorted(o.tiers, key=lambda t: t.min_quantity))
        z_list: list[cp_model.IntVar] = []
        tier_costs_scaled: list[int] = []
        max_unit_scaled = 0
        for k, t in enumerate(tiers_sorted):
            z = model.new_bool_var(f"tier_{qid}_{k}")
            z_list.append(z)
            unit_scaled = _scale_money(t.landed_unit_cost)
            tier_costs_scaled.append(unit_scaled)
            max_unit_scaled = max(max_unit_scaled, unit_scaled)
            min_k = t.min_quantity
            if min_k <= upper:
                max_k_eff = upper if t.max_quantity is None else min(t.max_quantity, upper)
                model.add(alloc_var >= min_k).only_enforce_if(z)
                model.add(alloc_var <= max_k_eff).only_enforce_if(z)
            else:
                model.add(z == 0)  # tier structurally unreachable at this upper bound
        model.add(sum(z_list) == used_var)
        tier_bool[qid] = z_list

        cost_var = model.new_int_var(0, max(max_unit_scaled * upper, 0), f"cost_{qid}")
        offer_cost[qid] = cost_var
        for k, z in enumerate(z_list):
            model.add(cost_var == tier_costs_scaled[k] * alloc_var).only_enforce_if(z)
        model.add(cost_var == 0).only_enforce_if(used_var.negated())

    # demand: always hard, never relaxable
    for line in ctx.lines:
        offers = offers_by_line.get(line.rfq_line_id, [])
        model.add(sum(alloc[o.quote_line_id] for o in offers) == line.required_quantity)

    fixed_scaled_by_offer = {o.quote_line_id: _scale_money(o.fixed_cost) for o in ctx.live_offers}

    supplier_used: dict[UUID, cp_model.IntVar] = {}
    supplier_spend: dict[UUID, cp_model.IntVar] = {}
    for supplier_id, offers in offers_by_supplier.items():
        used_vars = [offer_used[o.quote_line_id] for o in offers]
        s_used = model.new_bool_var(f"supplier_used_{supplier_id}")
        model.add_max_equality(s_used, used_vars)
        supplier_used[supplier_id] = s_used

        spend_var = model.new_int_var(
            0, ctx.total_cost_upper_bound, f"supplier_spend_{supplier_id}"
        )
        model.add(
            spend_var
            == sum(offer_cost[o.quote_line_id] for o in offers)
            + sum(
                fixed_scaled_by_offer[o.quote_line_id] * offer_used[o.quote_line_id] for o in offers
            )
        )
        supplier_spend[supplier_id] = spend_var

    total_cost = model.new_int_var(0, ctx.total_cost_upper_bound, "total_cost")
    model.add(
        total_cost
        == sum(offer_cost[o.quote_line_id] for o in ctx.live_offers)
        + sum(
            fixed_scaled_by_offer[o.quote_line_id] * offer_used[o.quote_line_id]
            for o in ctx.live_offers
        )
    )

    if ctx.constraints.max_supplier_count is not None:
        k = ctx.constraints.max_supplier_count
        if assumption_mode:
            model.add(sum(supplier_used.values()) <= k).only_enforce_if(
                group_literals["supplier_count"]
            )
        elif "supplier_count" not in active_relax:
            model.add(sum(supplier_used.values()) <= k)

    if ctx.constraints.max_concentration is not None:
        rho = ctx.constraints.max_concentration
        cap_num = int(quantize(rho * CONCENTRATION_DENOM, Decimal(1)))
        for spend_var in supplier_spend.values():
            if assumption_mode:
                model.add(
                    spend_var * CONCENTRATION_DENOM <= cap_num * total_cost
                ).only_enforce_if(group_literals["concentration"])
            elif "concentration" not in active_relax:
                model.add(spend_var * CONCENTRATION_DENOM <= cap_num * total_cost)

    if ctx.constraints.budget_limit is not None:
        budget_scaled = _scale_money(ctx.constraints.budget_limit)
        if assumption_mode:
            model.add(total_cost <= budget_scaled).only_enforce_if(group_literals["budget"])
        elif "budget" not in active_relax:
            model.add(total_cost <= budget_scaled)

    model.minimize(total_cost)

    if assumption_mode:
        model.add_assumptions(list(group_literals.values()))

    return _BuiltModel(
        model=model,
        alloc=alloc,
        offer_used=offer_used,
        tier_bool=tier_bool,
        offer_cost=offer_cost,
        supplier_used=supplier_used,
        supplier_spend=supplier_spend,
        total_cost=total_cost,
        group_literals=group_literals if assumption_mode else None,
    )


# --------------------------------------------------------------------------
# Solver
# --------------------------------------------------------------------------


class AllocationSolver:
    """Pure-domain CP-SAT order-allocation solver. No database, no clock, no
    randomness beyond CP-SAT's fixed, seeded internal search."""

    def __init__(self, max_deterministic_time: float = 30.0) -> None:
        self._max_deterministic_time = max_deterministic_time

    def solve(self, problem: AllocationProblem) -> AllocationResult:
        return self._solve(problem, compute_alternatives=True)

    # -- orchestration -----------------------------------------------------

    def _solve(self, problem: AllocationProblem, *, compute_alternatives: bool) -> AllocationResult:
        model_hash = _model_hash(problem)

        usable, exclusions_a = _filter_offers(problem)
        line_by_id = {line.rfq_line_id: line for line in problem.lines}
        live_offers, exclusions_b = _partition_live_and_dead(usable, line_by_id)
        all_exclusions = exclusions_a + exclusions_b

        no_supply_lines = _lines_with_no_supply(problem.lines, live_offers)
        if no_supply_lines:
            return self._presolve_infeasible(
                model_hash, _no_supply_explanation(no_supply_lines, all_exclusions)
            )

        cap_explanation = _capacity_shortfall_explanation(problem.lines, live_offers)
        if cap_explanation is not None:
            return self._presolve_infeasible(model_hash, cap_explanation)

        locked_by_offer, lock_explanation = _validate_locks(problem, problem.lines, live_offers)
        if lock_explanation is not None:
            return self._presolve_infeasible(model_hash, lock_explanation)

        conc_explanation = _concentration_structural_explanation(problem, live_offers)
        if conc_explanation is not None:
            return self._presolve_infeasible(model_hash, conc_explanation)

        budget_explanation = _budget_lower_bound_explanation(problem, problem.lines, live_offers)
        if budget_explanation is not None:
            return self._presolve_infeasible(model_hash, budget_explanation)

        total_cost_upper_bound = _check_overflow(
            problem.lines,
            live_offers,
            has_concentration_cap=problem.constraints.max_concentration is not None,
        )

        upper_by_offer = {
            o.quote_line_id: _upper_bound(o, line_by_id[o.rfq_line_id].required_quantity)
            for o in live_offers
        }
        sorted_lines = tuple(sorted(problem.lines, key=lambda line: str(line.rfq_line_id)))
        ctx = _BuildContext(
            lines=sorted_lines,
            live_offers=live_offers,
            line_by_id=line_by_id,
            upper_by_offer=upper_by_offer,
            locked_by_offer=locked_by_offer,
            constraints=problem.constraints,
            total_cost_upper_bound=total_cost_upper_bound,
        )

        try:
            built = _build_model(ctx, relax=None)
            solver = self._make_solver()
            raw_status = solver.solve(built.model)
        except Exception as exc:  # solver_error: audited, never a bare crash
            return AllocationResult(
                status=AllocationStatus.ERROR,
                allocations=(),
                expected_total_cost=None,
                supplier_count=0,
                binding_constraints=(),
                infeasibility=None,
                rejected_alternatives=(),
                stats=None,
                error_message=str(exc),
            )

        stats = SolverStats(
            status_raw=solver.status_name(raw_status),
            deterministic_time=solver.deterministic_time,
            model_hash=model_hash,
            num_variables=len(built.model.proto.variables),
            num_constraints=len(built.model.proto.constraints),
            max_scaling_error=_max_scaling_error(problem.lines, live_offers),
        )

        if raw_status == cp_model.INFEASIBLE:
            explanation = self._explain_cp_sat_infeasibility(ctx, built, solver)
            return AllocationResult(
                status=AllocationStatus.INFEASIBLE,
                allocations=(),
                expected_total_cost=None,
                supplier_count=0,
                binding_constraints=(),
                infeasibility=explanation,
                rejected_alternatives=(),
                stats=stats,
                error_message=None,
            )

        if raw_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return AllocationResult(
                status=AllocationStatus.ERROR,
                allocations=(),
                expected_total_cost=None,
                supplier_count=0,
                binding_constraints=(),
                infeasibility=None,
                rejected_alternatives=(),
                stats=stats,
                error_message=f"solver returned {solver.status_name(raw_status)}",
            )

        is_optimal = raw_status == cp_model.OPTIMAL
        status = AllocationStatus.OPTIMAL if is_optimal else AllocationStatus.FEASIBLE
        allocations, expected_total_cost = self._extract_allocations(ctx, built, solver)
        supplier_count = len({a.supplier_id for a in allocations})
        binding = self._binding_constraints(ctx, built, solver, supplier_count)

        rejected: tuple[str, ...] = ()
        if compute_alternatives and supplier_count > 1:
            rejected = (self._single_supplier_alternative(problem, expected_total_cost),)

        return AllocationResult(
            status=status,
            allocations=allocations,
            expected_total_cost=expected_total_cost,
            supplier_count=supplier_count,
            binding_constraints=binding,
            infeasibility=None,
            rejected_alternatives=rejected,
            stats=stats,
            error_message=None,
        )

    def _make_solver(self) -> cp_model.CpSolver:
        solver = cp_model.CpSolver()
        p = solver.parameters
        p.num_search_workers = 1
        p.random_seed = 0
        p.max_deterministic_time = self._max_deterministic_time
        p.max_time_in_seconds = 120.0
        p.log_search_progress = False
        p.cp_model_presolve = True
        return solver

    def _presolve_infeasible(
        self, model_hash: str, explanation: InfeasibilityExplanation
    ) -> AllocationResult:
        return AllocationResult(
            status=AllocationStatus.INFEASIBLE,
            allocations=(),
            expected_total_cost=None,
            supplier_count=0,
            binding_constraints=(),
            infeasibility=explanation,
            rejected_alternatives=(),
            stats=SolverStats(
                status_raw="PRESOLVE_INFEASIBLE",
                deterministic_time=0.0,
                model_hash=model_hash,
                num_variables=0,
                num_constraints=0,
            ),
            error_message=None,
        )

    # -- infeasibility (CP-SAT layer) --------------------------------------

    def _explain_cp_sat_infeasibility(
        self, ctx: _BuildContext, built: _BuiltModel, solver: cp_model.CpSolver
    ) -> InfeasibilityExplanation:
        assert built.group_literals is not None
        # SufficientAssumptionsForInfeasibility() returns the literal indices
        # directly (already ints), not literal objects. It is a *sufficient*
        # core, not guaranteed minimal (observed in practice to include
        # groups with zero constraints wired in this instance) — refine it
        # with deletion-based minimization below.
        core_indices = set(solver.sufficient_assumptions_for_infeasibility())
        raw_core = tuple(
            name for name in _GROUP_ORDER if built.group_literals[name].index in core_indices
        )
        conflicting = self._minimize_core(ctx, raw_core)
        if not conflicting:
            return InfeasibilityExplanation(
                conflicting_groups=(),
                detail=(
                    "the model is infeasible for reasons not attributable to a single "
                    "relaxable constraint group; demand cannot be met given the eligible "
                    "offers' price-break structure"
                ),
                minimal_relaxation=None,
            )
        detail = (
            "no allocation satisfies demand together with: "
            + ", ".join(conflicting)
            + " (relaxing one of these restores feasibility)"
        )
        return InfeasibilityExplanation(
            conflicting_groups=conflicting,
            detail=detail,
            minimal_relaxation=self._minimal_relaxation(ctx, conflicting),
        )

    def _minimize_core(self, ctx: _BuildContext, raw_core: tuple[str, ...]) -> tuple[str, ...]:
        """Deletion-based unsat-core minimization. `raw_core` is *sufficient*
        for infeasibility but may include groups with no bearing on it. For
        each candidate, check whether the model is still infeasible with
        every OTHER kept group enforced and every non-kept group fully
        relaxed; if so the candidate was not needed and is dropped."""
        necessary = list(raw_core)
        for group in raw_core:
            if group not in necessary:
                continue
            trial = [g for g in necessary if g != group]
            relax = frozenset(_GROUP_ORDER) - frozenset(trial)
            built = _build_model(ctx, relax=relax)
            solver = self._make_solver()
            status = solver.solve(built.model)
            if status == cp_model.INFEASIBLE:
                necessary = trial  # `group` was not needed for infeasibility
        return tuple(name for name in _GROUP_ORDER if name in necessary)

    def _minimal_relaxation(self, ctx: _BuildContext, conflicting: tuple[str, ...]) -> str | None:
        for group in conflicting:
            built = _build_model(ctx, relax=frozenset({group}))
            solver = self._make_solver()
            status = solver.solve(built.model)
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                continue
            if group == "budget":
                cost = _unscale_money(solver.value(built.total_cost))
                return f"a budget of at least {format(cost, 'f')} restores feasibility"
            if group == "supplier_count":
                needed = sum(solver.value(v) for v in built.supplier_used.values())
                return f"raising max_supplier_count to at least {needed} restores feasibility"
            return f"relaxing '{group}' restores feasibility"
        return None

    # -- result extraction ---------------------------------------------------

    def _extract_allocations(
        self, ctx: _BuildContext, built: _BuiltModel, solver: cp_model.CpSolver
    ) -> tuple[tuple[AllocationEntry, ...], Decimal]:
        entries: list[AllocationEntry] = []
        used_offer_ids: set[UUID] = set()
        with localcontext(CALC_CONTEXT):
            total = Decimal("0")
            for o in ctx.live_offers:
                qty = solver.value(built.alloc[o.quote_line_id])
                if qty <= 0:
                    continue
                used_offer_ids.add(o.quote_line_id)

                tiers_sorted = tuple(sorted(o.tiers, key=lambda t: t.min_quantity))
                price_break_tiers = tuple(
                    PriceBreakTier(
                        min_quantity=Decimal(t.min_quantity),
                        max_quantity=None if t.max_quantity is None else Decimal(t.max_quantity),
                        unit_price=t.landed_unit_cost,
                    )
                    for t in tiers_sorted
                )
                selection = select_price_break(price_break_tiers, Decimal(qty))
                if selection.tier is None:
                    raise ConsistencyError(
                        f"solver allocated {qty} units to {o.supplier_label} on line "
                        f"{o.rfq_line_id} but select_price_break cannot price that quantity: "
                        f"{selection.reason}"
                    )

                solver_tier_index = next(
                    (
                        k
                        for k, z in enumerate(built.tier_bool[o.quote_line_id])
                        if solver.value(z) == 1
                    ),
                    None,
                )
                if solver_tier_index is None:
                    raise ConsistencyError(
                        f"solver allocated {qty} units to {o.supplier_label} but no tier "
                        "boolean is set — tier-linking constraints are inconsistent"
                    )
                solver_tier = tiers_sorted[solver_tier_index]
                if (
                    Decimal(solver_tier.min_quantity) != selection.tier.min_quantity
                    or solver_tier.landed_unit_cost != selection.tier.unit_price
                ):
                    raise ConsistencyError(
                        f"tier mismatch for {o.supplier_label}: solver chose the tier starting "
                        f"at {solver_tier.min_quantity}, but the exact recomputation at the "
                        f"allocated quantity {qty} selects the tier starting at "
                        f"{selection.tier.min_quantity}"
                    )

                line_cost = quantize_money(solver_tier.landed_unit_cost * Decimal(qty))
                total += line_cost
                entries.append(
                    AllocationEntry(
                        rfq_line_id=o.rfq_line_id,
                        quote_line_id=o.quote_line_id,
                        supplier_id=o.supplier_id,
                        supplier_label=o.supplier_label,
                        quantity=qty,
                        tier_applied=solver_tier,
                        line_cost=line_cost,
                    )
                )
            for o in ctx.live_offers:
                if o.quote_line_id in used_offer_ids and o.fixed_cost != 0:
                    total += o.fixed_cost
            total = quantize_money(total)

        entries_sorted = tuple(
            sorted(entries, key=lambda e: (str(e.rfq_line_id), str(e.supplier_id)))
        )
        return entries_sorted, total

    def _binding_constraints(
        self,
        ctx: _BuildContext,
        built: _BuiltModel,
        solver: cp_model.CpSolver,
        supplier_count: int,
    ) -> tuple[BindingConstraint, ...]:
        binding: list[BindingConstraint] = []

        for o in ctx.live_offers:
            if o.capacity is not None and solver.value(built.alloc[o.quote_line_id]) == o.capacity:
                binding.append(
                    BindingConstraint(
                        name="capacity",
                        detail=(
                            f"{o.supplier_label} is allocated at its full capacity of "
                            f"{o.capacity} units"
                        ),
                    )
                )

        if (
            ctx.constraints.max_supplier_count is not None
            and supplier_count == ctx.constraints.max_supplier_count
        ):
            binding.append(
                BindingConstraint(
                    name="max_supplier_count",
                    detail=f"the allocation uses the maximum permitted {supplier_count} suppliers",
                )
            )

        if ctx.constraints.budget_limit is not None:
            budget_scaled = _scale_money(ctx.constraints.budget_limit)
            total_scaled = solver.value(built.total_cost)
            if 0 <= budget_scaled - total_scaled <= 1:
                binding.append(
                    BindingConstraint(
                        name="budget",
                        detail=(
                            "the allocation is within one scaled unit of the budget limit "
                            f"({format(_unscale_money(budget_scaled), 'f')})"
                        ),
                    )
                )

        if ctx.constraints.max_concentration is not None:
            rho = ctx.constraints.max_concentration
            cap_num = int(quantize(rho * CONCENTRATION_DENOM, Decimal(1)))
            total_scaled = solver.value(built.total_cost)
            for supplier_id, spend_var in built.supplier_spend.items():
                spend_scaled = solver.value(spend_var)
                slack = cap_num * total_scaled - spend_scaled * CONCENTRATION_DENOM
                if 0 <= slack <= CONCENTRATION_DENOM:
                    binding.append(
                        BindingConstraint(
                            name="max_concentration",
                            detail=(
                                f"supplier {supplier_id} spend is at or within one scaled unit "
                                f"of the {format(rho, 'f')} concentration cap"
                            ),
                        )
                    )

        return tuple(binding)

    def _single_supplier_alternative(self, problem: AllocationProblem, main_cost: Decimal) -> str:
        alt_problem = replace(
            problem, constraints=replace(problem.constraints, max_supplier_count=1)
        )
        alt_result = self._solve(alt_problem, compute_alternatives=False)
        if alt_result.status in (AllocationStatus.OPTIMAL, AllocationStatus.FEASIBLE):
            assert alt_result.expected_total_cost is not None
            delta = alt_result.expected_total_cost - main_cost
            sign = "+" if delta >= 0 else ""
            alt_cost_str = format(alt_result.expected_total_cost, "f")
            if alt_result.status is AllocationStatus.OPTIMAL:
                verb = "would cost"
            else:
                # FEASIBLE is an upper bound, not a proven figure — the one
                # place this discipline lapsed (2026-08 calculation audit F9).
                verb = "would cost at most"
            return (
                f"a single-supplier allocation {verb} {alt_cost_str} "
                f"({sign}{format(delta, 'f')} vs. the recommended split"
                + (
                    ")"
                    if alt_result.status is AllocationStatus.OPTIMAL
                    else "; optimality of the alternative not proven within the search budget)"
                )
            )
        return "no feasible single-supplier alternative"
