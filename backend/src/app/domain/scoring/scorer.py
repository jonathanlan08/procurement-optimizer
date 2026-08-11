"""ScorerV1 — implementation of the principal-owned `VendorScorer` contract.

Implements `VendorScorer` (`contracts.py`, FROZEN). Pure domain code: no
database, no clock, no network, no randomness. See
`docs/planning/05-calculation-methodology.md` §7 for the ratified algorithm
this module reproduces.

Design notes (interpretations of the contract's prose, spelled out because
the contract's docstring is normative but not exhaustive pseudocode — the
same convention `landed_cost/calculator.py` uses):

- **Missing-value policy.** 05 §7 describes three `missing_policy` options
  (`renormalize` / `worst` / `exclude_supplier`), but the FROZEN
  `CriterionSpec` in `contracts.py` has no `missing_policy` field at all — a
  per-supplier's-view of a criterion is either present or missing, full
  stop. This implementation therefore always applies the default
  (`renormalize`): a missing criterion is dropped for that supplier, that
  supplier's remaining weights are renormalized over its present criteria,
  and the criterion is listed in `missing_criteria`. Callers wanting
  `worst`/`exclude_supplier` semantics apply them upstream, before building
  `SupplierScoringInput` (e.g. `exclude_supplier` maps directly onto the
  `excluded`/`exclusion_reason` fields already on that dataclass).
- **Global defensive renormalization.** `weights` are "fractions summing to
  ~1" (contracts.py docstring); if the raw sum isn't exactly 1 (Decimal
  equality — the caller is expected to pass fractions that already sum to
  1, so any deviation is treated as needing correction, not tolerated as
  noise), every weight is divided by the raw sum and a note is appended to
  `ScoringResult.notes`. This happens once, globally, before any
  per-supplier renormalization, and is unrelated to the per-supplier
  mechanism above. If the raw sum is exactly zero (including the
  zero-criteria case, `weights == ()`), there is no weight signal to
  renormalize against and `ZeroTotalWeightError` is raised — mirroring
  `landed_cost.calculator.ZeroQuantityError`'s "fail loud, never divide
  into nonsense" policy for a violated arithmetic precondition (05 §7:
  "Weight sum must be > 0").
- **`total_score` is the ONLY quantized value in this module.** Per-criterion
  `normalized_score` / `effective_weight` / `weighted_contribution` on
  `CriterionScore` are kept at full `CALC_CONTEXT` precision (the objective
  brief only mandates "total_score quantized RATIO_SCALE" — it does not
  require the per-criterion display fields to be independently rounded).
  This is a deliberate choice, not an oversight: per-supplier normalized
  scores are mathematically bounded to exactly `[0, 1]` and per-supplier
  effective weights sum to exactly `1` (both by construction, at full
  precision), so their weighted sum is bounded to `[0, 1]` before any
  rounding. Independently quantizing each criterion's contribution first
  and *then* summing the rounded pieces can, across enough criteria,
  accumulate enough rounding to push a near-1.0 total fractionally over 1
  after the final quantize — which would violate the contract's `[0, 1]`
  guarantee. Quantizing once, at the end, from the full-precision sum
  avoids that failure mode entirely while keeping
  `sum(c.weighted_contribution for c in criterion_scores) ==` the
  *unquantized* total exactly (the quantize is idempotent rounding of an
  already-correct sum, not a second independent rounding).
- **Excluded suppliers are unscored**, per contracts.py: no
  `CriterionScore`s are computed for them (`criterion_scores=()`,
  `missing_criteria=()`, `weights_renormalized=False`), `total_score` is
  `Decimal("0")` (a value that means nothing here, since the field is not
  optional on the frozen `SupplierScore`), and `rank` is the sentinel `0`
  — distinguishing "not ranked" from the 1-based ranks used by the scored
  cohort, since 05 §7 removes excluded suppliers before scoring entirely
  (they are listed, not ranked).
- **Ranking** is standard competition ranking ("1224"): a group of suppliers
  tied on `total_score` all get the smallest rank in that group, and the
  next distinct score's rank skips over the tied group's size (e.g. two
  suppliers tied for 1st are both rank 1; the next supplier is rank 3, not
  2). Deterministic ordering — required so ties resolve identically on
  every run — is `(-total_score, supplier_name, supplier_id)`, per the
  objective brief; `uuid.UUID` is natively orderable so `supplier_id` sorts
  by its underlying integer value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, localcontext
from uuid import UUID

from app.core.money import CALC_CONTEXT, RATIO_SCALE, quantize, to_wire
from app.domain.scoring.contracts import (
    SCORING_VERSION,
    Criterion,
    CriterionScore,
    CriterionSpec,
    Direction,
    ScoringResult,
    SupplierScore,
    SupplierScoringInput,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")


class ZeroTotalWeightError(ValueError):
    """The supplied criterion weights sum to zero (including the
    zero-criteria case): there is no weight signal to renormalize against
    or to score with. Mirrors `landed_cost.calculator.ZeroQuantityError` —
    fail loud rather than silently dividing into nonsense."""

    def __init__(self) -> None:
        super().__init__(
            "criterion weights sum to zero; cannot renormalize or compute a score "
            "(05-calculation-methodology.md §7: weight sum must be > 0)"
        )


def _global_renormalize(
    weights: tuple[CriterionSpec, ...],
) -> tuple[tuple[CriterionSpec, ...], tuple[str, ...]]:
    """Defensive global renormalization: weights are expected to sum to 1;
    if they don't, divide each by the raw sum and record a note."""
    with localcontext(CALC_CONTEXT):
        raw_sum = _ZERO
        for w in weights:
            raw_sum += w.weight
    if raw_sum == _ZERO:
        raise ZeroTotalWeightError()
    if raw_sum == _ONE:
        return weights, ()

    with localcontext(CALC_CONTEXT):
        adjusted = tuple(replace(w, weight=w.weight / raw_sum) for w in weights)
    note = (
        f"criterion weights summed to {to_wire(raw_sum)} rather than 1; "
        f"renormalized globally by dividing each weight by {to_wire(raw_sum)}"
    )
    return adjusted, (note,)


def _supplier_criterion_map(supplier: SupplierScoringInput) -> dict[Criterion, Decimal | None]:
    return {scv.criterion: scv.value for scv in supplier.values}


def _cohort_stats(
    criterion: Criterion,
    included: Sequence[SupplierScoringInput],
    value_maps: Mapping[UUID, dict[Criterion, Decimal | None]],
) -> tuple[Decimal, Decimal, int] | None:
    """(min, max, present_count) across the INCLUDED cohort's present values
    for `criterion`. `None` means no supplier in the cohort has a present
    value at all — the criterion is globally missing, not just for one
    supplier. The count exists so the degenerate-cohort reason can say
    "only candidate with a value" instead of the untrue "all candidates
    equal" (2026-08 calculation audit F7)."""
    present: list[Decimal] = []
    for s in included:
        v = value_maps[s.supplier_id].get(criterion)
        if v is not None:
            present.append(v)
    if not present:
        return None
    return min(present), max(present), len(present)


def _criterion_score_present(
    spec: CriterionSpec,
    raw: Decimal,
    stats: tuple[Decimal, Decimal, int],
) -> tuple[Decimal, str]:
    """Returns (normalized_score, reason) for a present, non-degenerate or
    degenerate cohort. Full CALC_CONTEXT precision; not quantized here."""
    min_v, max_v, present_count = stats
    if min_v == max_v:
        if present_count == 1:
            reason = (
                f"only candidate with a value for this criterion "
                f"({spec.label}={to_wire(raw)}); the criterion could not discriminate"
            )
        else:
            reason = (
                f"all {present_count} candidates equal on this criterion "
                f"({spec.label}={to_wire(raw)}); criterion is non-discriminating"
            )
        return _ONE, reason

    with localcontext(CALC_CONTEXT):
        if spec.direction is Direction.HIGHER_IS_BETTER:
            normalized = (raw - min_v) / (max_v - min_v)
        else:
            normalized = (max_v - raw) / (max_v - min_v)

    reason = (
        f"{spec.label}: raw {to_wire(raw)}, cohort range {to_wire(min_v)}-{to_wire(max_v)} "
        f"({spec.direction.value}); normalized {to_wire(normalized)}"
    )
    return normalized, reason


def _score_included_supplier(
    supplier: SupplierScoringInput,
    weights: tuple[CriterionSpec, ...],
    value_map: dict[Criterion, Decimal | None],
    cohort_stats: Mapping[Criterion, tuple[Decimal, Decimal, int] | None],
) -> SupplierScore:
    missing: list[Criterion] = []
    present_specs: list[CriterionSpec] = []
    present_raw: dict[Criterion, Decimal] = {}

    for spec in weights:
        raw = value_map.get(spec.criterion)
        if raw is None:
            missing.append(spec.criterion)
        else:
            present_specs.append(spec)
            present_raw[spec.criterion] = raw

    weights_renormalized = bool(missing)

    with localcontext(CALC_CONTEXT):
        present_weight_sum = _ZERO
        for s in present_specs:
            present_weight_sum += s.weight

    criterion_scores: list[CriterionScore] = []
    total_raw = _ZERO

    for spec in weights:
        if spec.criterion in present_raw:
            stats = cohort_stats[spec.criterion]
            assert stats is not None  # a present raw value implies non-empty cohort stats
            normalized, reason = _criterion_score_present(spec, present_raw[spec.criterion], stats)
            if present_weight_sum == _ZERO:
                effective_weight = _ZERO
            else:
                with localcontext(CALC_CONTEXT):
                    effective_weight = spec.weight / present_weight_sum
            with localcontext(CALC_CONTEXT):
                weighted_contribution = effective_weight * normalized
                total_raw += weighted_contribution
            criterion_scores.append(
                CriterionScore(
                    criterion=spec.criterion,
                    raw_value=present_raw[spec.criterion],
                    normalized_score=normalized,
                    effective_weight=effective_weight,
                    weighted_contribution=weighted_contribution,
                    reason=reason,
                )
            )
        else:
            reason = (
                f"{spec.label} is missing for this supplier; its weight was redistributed "
                "across this supplier's remaining present criteria"
            )
            criterion_scores.append(
                CriterionScore(
                    criterion=spec.criterion,
                    raw_value=None,
                    normalized_score=None,
                    effective_weight=_ZERO,
                    weighted_contribution=_ZERO,
                    reason=reason,
                )
            )

    total_score = quantize(total_raw, RATIO_SCALE)

    return SupplierScore(
        supplier_id=supplier.supplier_id,
        supplier_name=supplier.supplier_name,
        total_score=total_score,
        rank=0,  # placeholder; assigned by the caller after sorting the cohort
        criterion_scores=tuple(criterion_scores),
        missing_criteria=tuple(missing),
        weights_renormalized=weights_renormalized,
        excluded=False,
        exclusion_reason=None,
    )


def _assign_ranks(scores: Sequence[SupplierScore]) -> list[SupplierScore]:
    """`scores` must already be sorted by (-total_score, name, id). Standard
    competition ranking: ties share the smallest rank in their group."""
    ranked: list[SupplierScore] = []
    previous_score: Decimal | None = None
    current_rank = 0
    for position, score in enumerate(scores, start=1):
        if previous_score is None or score.total_score != previous_score:
            current_rank = position
            previous_score = score.total_score
        ranked.append(replace(score, rank=current_rank))
    return ranked


class ScorerV1:
    """Implements `VendorScorer` (contracts.py). Pure, deterministic."""

    version: str = SCORING_VERSION

    def score(
        self,
        suppliers: tuple[SupplierScoringInput, ...],
        weights: tuple[CriterionSpec, ...],
    ) -> ScoringResult:
        renormalized_weights, notes = _global_renormalize(weights)

        included = tuple(s for s in suppliers if not s.excluded)
        excluded = tuple(s for s in suppliers if s.excluded)

        value_maps = {s.supplier_id: _supplier_criterion_map(s) for s in included}
        cohort_stats = {
            spec.criterion: _cohort_stats(spec.criterion, included, value_maps)
            for spec in renormalized_weights
        }

        # A supplier with NO present value on ANY positive-weight criterion is
        # NOT scoreable. Scoring it 0.000000 would be exactly the
        # worst-in-cohort imputation the methodology forbids (00-decisions.md
        # §2 ruling 5; 2026-08 calculation audit F3) — it is reported like an
        # exclusion instead: rank=0 sentinel, reason attached, never ranked.
        # It still contributes any present zero-weight values to cohort stats
        # above, so other suppliers' displayed normalizations are unaffected.
        def _is_scoreable(s: SupplierScoringInput) -> bool:
            return any(
                spec.weight > _ZERO
                and value_maps[s.supplier_id].get(spec.criterion) is not None
                for spec in renormalized_weights
            )

        scoreable = tuple(s for s in included if _is_scoreable(s))
        unscoreable = tuple(s for s in included if not _is_scoreable(s))
        if unscoreable:
            names = ", ".join(
                s.supplier_name
                for s in sorted(unscoreable, key=lambda s: (s.supplier_name, s.supplier_id))
            )
            notes = (
                *notes,
                f"not scoreable (no weighted criterion has a value): {names}",
            )

        scored = [
            _score_included_supplier(
                s, renormalized_weights, value_maps[s.supplier_id], cohort_stats
            )
            for s in scoreable
        ]
        scored.sort(key=lambda s: (-s.total_score, s.supplier_name, s.supplier_id))
        ranked = _assign_ranks(scored)

        unscoreable_scores = [
            SupplierScore(
                supplier_id=s.supplier_id,
                supplier_name=s.supplier_name,
                total_score=_ZERO,
                rank=0,
                criterion_scores=(),
                missing_criteria=tuple(
                    spec.criterion
                    for spec in renormalized_weights
                    if value_maps[s.supplier_id].get(spec.criterion) is None
                ),
                weights_renormalized=False,
                excluded=True,
                exclusion_reason=(
                    "not scoreable: no weighted criterion has a value for this supplier"
                ),
            )
            for s in sorted(unscoreable, key=lambda s: (s.supplier_name, s.supplier_id))
        ]

        excluded_scores = [
            SupplierScore(
                supplier_id=s.supplier_id,
                supplier_name=s.supplier_name,
                total_score=_ZERO,
                rank=0,
                criterion_scores=(),
                missing_criteria=(),
                weights_renormalized=False,
                excluded=True,
                exclusion_reason=s.exclusion_reason,
            )
            for s in sorted(excluded, key=lambda s: (s.supplier_name, s.supplier_id))
        ]

        return ScoringResult(
            scores=tuple(ranked) + tuple(unscoreable_scores) + tuple(excluded_scores),
            weights_used=renormalized_weights,
            cohort_size=len(scoreable),
            notes=notes,
            scoring_version=SCORING_VERSION,
        )


__all__ = ["ScorerV1", "ZeroTotalWeightError"]
