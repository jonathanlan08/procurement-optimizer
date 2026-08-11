"""Unit tests for ScorerV1: the SPEC scoring matrix
(docs/planning/05-calculation-methodology.md §7)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.scoring.contracts import (
    Criterion,
    CriterionSpec,
    Direction,
    SupplierCriterionValue,
    SupplierScoringInput,
)
from app.domain.scoring.scorer import ScorerV1, ZeroTotalWeightError

SCORER = ScorerV1()


def _supplier(
    name: str,
    values: dict[Criterion, Decimal | None],
    *,
    supplier_id: UUID | None = None,
    excluded: bool = False,
    exclusion_reason: str | None = None,
) -> SupplierScoringInput:
    return SupplierScoringInput(
        supplier_id=supplier_id or uuid4(),
        supplier_name=name,
        values=tuple(
            SupplierCriterionValue(criterion=c, value=v, source="test") for c, v in values.items()
        ),
        excluded=excluded,
        exclusion_reason=exclusion_reason,
    )


def _spec(
    criterion: Criterion, weight: str, direction: Direction, label: str | None = None
) -> CriterionSpec:
    return CriterionSpec(criterion, Decimal(weight), direction, label or criterion.value)


class TestDirectionHandling:
    def test_higher_is_better_rewards_the_larger_value(self) -> None:
        a = _supplier("A", {Criterion.SPEC_COMPLIANCE: Decimal("0.5")})
        b = _supplier("B", {Criterion.SPEC_COMPLIANCE: Decimal("1.0")})
        weights = (_spec(Criterion.SPEC_COMPLIANCE, "1", Direction.HIGHER_IS_BETTER),)
        result = SCORER.score((a, b), weights)
        by_name = {s.supplier_name: s for s in result.scores}
        assert by_name["B"].total_score == Decimal("1")
        assert by_name["A"].total_score == Decimal("0")

    def test_lower_is_better_rewards_the_smaller_value(self) -> None:
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("100")})
        b = _supplier("B", {Criterion.TOTAL_LANDED_COST: Decimal("200")})
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER),)
        result = SCORER.score((a, b), weights)
        by_name = {s.supplier_name: s for s in result.scores}
        assert by_name["A"].total_score == Decimal("1")
        assert by_name["B"].total_score == Decimal("0")


class TestEqualValuesAllScoreOne:
    def test_all_equal_scores_everyone_one_with_a_reason(self) -> None:
        a = _supplier("A", {Criterion.LEAD_TIME: Decimal("10")})
        b = _supplier("B", {Criterion.LEAD_TIME: Decimal("10")})
        weights = (_spec(Criterion.LEAD_TIME, "1", Direction.LOWER_IS_BETTER),)
        result = SCORER.score((a, b), weights)
        for s in result.scores:
            assert s.total_score == Decimal("1")
            assert "non-discriminating" in s.criterion_scores[0].reason

    def test_single_candidate_cohort_scores_one(self) -> None:
        a = _supplier("A", {Criterion.LEAD_TIME: Decimal("10")})
        weights = (_spec(Criterion.LEAD_TIME, "1", Direction.LOWER_IS_BETTER),)
        result = SCORER.score((a,), weights)
        assert result.scores[0].total_score == Decimal("1")


class TestMissingValueRenormalization:
    def test_missing_criterion_renormalizes_that_suppliers_weight_and_is_listed(self) -> None:
        weights = (
            _spec(Criterion.TOTAL_LANDED_COST, "0.6", Direction.LOWER_IS_BETTER, "Cost"),
            _spec(Criterion.QUALITY_HISTORY, "0.4", Direction.HIGHER_IS_BETTER, "Quality"),
        )
        a = _supplier(
            "A",
            {Criterion.TOTAL_LANDED_COST: Decimal("10"), Criterion.QUALITY_HISTORY: Decimal("0.9")},
        )
        b = _supplier(
            "B", {Criterion.TOTAL_LANDED_COST: Decimal("20"), Criterion.QUALITY_HISTORY: None}
        )
        result = SCORER.score((a, b), weights)
        by_name = {s.supplier_name: s for s in result.scores}

        b_score = by_name["B"]
        assert b_score.missing_criteria == (Criterion.QUALITY_HISTORY,)
        assert b_score.weights_renormalized is True
        cost_cs = next(
            cs for cs in b_score.criterion_scores if cs.criterion is Criterion.TOTAL_LANDED_COST
        )
        # B's only present criterion absorbs the entire weight (0.6 / 0.6 = 1)
        assert cost_cs.effective_weight == Decimal("1")
        missing_cs = next(
            cs for cs in b_score.criterion_scores if cs.criterion is Criterion.QUALITY_HISTORY
        )
        assert missing_cs.raw_value is None
        assert missing_cs.normalized_score is None
        assert missing_cs.effective_weight == Decimal("0")
        assert missing_cs.weighted_contribution == Decimal("0")

        a_score = by_name["A"]
        assert a_score.missing_criteria == ()
        assert a_score.weights_renormalized is False


class TestZeroWeightEvaluatedButContributesNothing:
    def test_zero_weight_criterion_is_scored_and_displayed_but_does_not_affect_total(self) -> None:
        weights = (
            _spec(Criterion.TOTAL_LANDED_COST, "1.0", Direction.LOWER_IS_BETTER, "Cost"),
            _spec(Criterion.LEAD_TIME, "0", Direction.LOWER_IS_BETTER, "Lead time"),
        )
        a = _supplier(
            "A", {Criterion.TOTAL_LANDED_COST: Decimal("10"), Criterion.LEAD_TIME: Decimal("5")}
        )
        b = _supplier(
            "B", {Criterion.TOTAL_LANDED_COST: Decimal("20"), Criterion.LEAD_TIME: Decimal("50")}
        )
        result = SCORER.score((a, b), weights)
        by_name = {s.supplier_name: s for s in result.scores}

        a_score = by_name["A"]
        lead_time_cs = next(
            cs for cs in a_score.criterion_scores if cs.criterion is Criterion.LEAD_TIME
        )
        assert lead_time_cs.normalized_score is not None  # evaluated, not dropped
        assert lead_time_cs.effective_weight == Decimal("0")
        assert lead_time_cs.weighted_contribution == Decimal("0")

        # total is driven entirely by cost; lead_time varies wildly but has no say
        assert a_score.total_score == Decimal("1")
        assert by_name["B"].total_score == Decimal("0")


class TestGlobalWeightRenormalization:
    def test_weights_not_summing_to_one_are_renormalized_with_a_note(self) -> None:
        weights = (
            _spec(Criterion.TOTAL_LANDED_COST, "0.5", Direction.LOWER_IS_BETTER, "Cost"),
            _spec(Criterion.LEAD_TIME, "0.3", Direction.LOWER_IS_BETTER, "Lead time"),
        )
        a = _supplier(
            "A", {Criterion.TOTAL_LANDED_COST: Decimal("10"), Criterion.LEAD_TIME: Decimal("5")}
        )
        result = SCORER.score((a,), weights)

        assert len(result.notes) == 1
        assert "0.8" in result.notes[0]
        assert "renormalized" in result.notes[0]

        renormalized = {w.criterion: w.weight for w in result.weights_used}
        assert renormalized[Criterion.TOTAL_LANDED_COST] == Decimal("0.5") / Decimal("0.8")
        assert renormalized[Criterion.LEAD_TIME] == Decimal("0.3") / Decimal("0.8")

    def test_weights_already_summing_to_one_produce_no_note(self) -> None:
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1.0", Direction.LOWER_IS_BETTER),)
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        result = SCORER.score((a,), weights)
        assert result.notes == ()

    def test_all_zero_weights_raises_rather_than_dividing_by_zero(self) -> None:
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "0", Direction.LOWER_IS_BETTER),)
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        with pytest.raises(ZeroTotalWeightError):
            SCORER.score((a,), weights)

    def test_no_criteria_at_all_also_raises(self) -> None:
        a = _supplier("A", {})
        with pytest.raises(ZeroTotalWeightError):
            SCORER.score((a,), ())


class TestExclusions:
    def test_excluded_supplier_is_unscored_and_listed_last_with_reason(self) -> None:
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER),)
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        b = _supplier("B", {Criterion.TOTAL_LANDED_COST: Decimal("20")})
        c = _supplier(
            "C",
            {Criterion.TOTAL_LANDED_COST: Decimal("30")},
            excluded=True,
            exclusion_reason="disqualified: sanctions match",
        )
        result = SCORER.score((a, b, c), weights)

        assert result.cohort_size == 2
        assert [s.supplier_name for s in result.scores] == ["A", "B", "C"]
        excluded_score = result.scores[-1]
        assert excluded_score.excluded is True
        assert excluded_score.exclusion_reason == "disqualified: sanctions match"
        assert excluded_score.criterion_scores == ()
        assert excluded_score.missing_criteria == ()
        assert excluded_score.weights_renormalized is False
        assert excluded_score.rank == 0

    def test_excluded_outlier_is_removed_before_min_max_not_after(self) -> None:
        """05 §7: excluded suppliers must be removed BEFORE min/max
        computation -- otherwise an excluded outlier silently compresses
        everyone else's scores. If C leaked into the cohort, B (cost 20)
        would score near 1.0 instead of exactly 0."""
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER),)
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        b = _supplier("B", {Criterion.TOTAL_LANDED_COST: Decimal("20")})
        c = _supplier(
            "C",
            {Criterion.TOTAL_LANDED_COST: Decimal("1000000")},
            excluded=True,
            exclusion_reason="disqualified",
        )
        result = SCORER.score((a, b, c), weights)
        by_name = {s.supplier_name: s for s in result.scores}
        assert by_name["A"].total_score == Decimal("1")
        assert by_name["B"].total_score == Decimal("0")


class TestTieRanks:
    def test_ties_share_the_smallest_rank_and_the_next_rank_skips(self) -> None:
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER),)
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        b = _supplier("B", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        c = _supplier("C", {Criterion.TOTAL_LANDED_COST: Decimal("20")})
        result = SCORER.score((a, b, c), weights)
        by_name = {s.supplier_name: s for s in result.scores}
        assert by_name["A"].rank == 1
        assert by_name["B"].rank == 1
        assert by_name["C"].rank == 3

    def test_deterministic_tie_break_order_is_name_then_id(self) -> None:
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER),)
        a = _supplier("Alpha", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        b = _supplier("Beta", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        result = SCORER.score((b, a), weights)  # note: passed in reverse
        assert [s.supplier_name for s in result.scores] == ["Alpha", "Beta"]


class TestReproducibility:
    def test_same_input_twice_is_byte_identical(self) -> None:
        weights = (
            _spec(Criterion.TOTAL_LANDED_COST, "0.6", Direction.LOWER_IS_BETTER, "Cost"),
            _spec(Criterion.LEAD_TIME, "0.4", Direction.LOWER_IS_BETTER, "Lead time"),
        )
        a = _supplier(
            "A", {Criterion.TOTAL_LANDED_COST: Decimal("10"), Criterion.LEAD_TIME: Decimal("5")}
        )
        b = _supplier("B", {Criterion.TOTAL_LANDED_COST: Decimal("20"), Criterion.LEAD_TIME: None})
        suppliers = (a, b)

        first = SCORER.score(suppliers, weights)
        second = SCORER.score(suppliers, weights)
        assert first == second


class TestOutlierNotClipped:
    def test_ten_x_outlier_scores_zero_others_spread_not_winsorized(self) -> None:
        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER),)
        cheap1 = _supplier("Cheap1", {Criterion.TOTAL_LANDED_COST: Decimal("100")})
        cheap2 = _supplier("Cheap2", {Criterion.TOTAL_LANDED_COST: Decimal("110")})
        cheap3 = _supplier("Cheap3", {Criterion.TOTAL_LANDED_COST: Decimal("120")})
        outlier = _supplier("Outlier", {Criterion.TOTAL_LANDED_COST: Decimal("1000")})
        result = SCORER.score((cheap1, cheap2, cheap3, outlier), weights)
        by_name = {s.supplier_name: s for s in result.scores}

        assert by_name["Outlier"].total_score == Decimal("0")
        assert by_name["Cheap1"].total_score == Decimal("1")  # the cohort minimum
        # NOT clipped: the outlier stretches the min-max range, so the cheap
        # suppliers compress toward the top instead of being independently
        # re-scaled as if the outlier didn't exist.
        for name in ("Cheap2", "Cheap3"):
            score = by_name[name].total_score
            assert Decimal("0.9") < score < Decimal("1")


class TestReasonStringsNameRawValueAndCohortMinMax:
    def test_reason_includes_the_raw_value_and_the_cohort_range(self) -> None:
        weights = (
            _spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER, "Total landed cost"),
        )
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("7240.55")})
        b = _supplier("B", {Criterion.TOTAL_LANDED_COST: Decimal("9100.00")})
        result = SCORER.score((a, b), weights)
        a_score = next(s for s in result.scores if s.supplier_name == "A")
        reason = a_score.criterion_scores[0].reason
        assert "7240.55" in reason
        assert "9100.00" in reason


class TestVendorScorerProtocolShape:
    def test_version_is_the_scoring_version(self) -> None:
        from app.domain.scoring.contracts import SCORING_VERSION

        assert SCORER.version == SCORING_VERSION

    def test_scoring_result_carries_the_scoring_version(self) -> None:
        from app.domain.scoring.contracts import SCORING_VERSION

        weights = (_spec(Criterion.TOTAL_LANDED_COST, "1", Direction.LOWER_IS_BETTER),)
        a = _supplier("A", {Criterion.TOTAL_LANDED_COST: Decimal("10")})
        result = SCORER.score((a,), weights)
        assert result.scoring_version == SCORING_VERSION


# --- Hypothesis property: total_score is always in [0, 1] -----------------

_CRITERIA: tuple[Criterion, ...] = (
    Criterion.TOTAL_LANDED_COST,
    Criterion.SPEC_COMPLIANCE,
    Criterion.LEAD_TIME,
)
_DIRECTIONS: dict[Criterion, Direction] = {
    Criterion.TOTAL_LANDED_COST: Direction.LOWER_IS_BETTER,
    Criterion.SPEC_COMPLIANCE: Direction.HIGHER_IS_BETTER,
    Criterion.LEAD_TIME: Direction.LOWER_IS_BETTER,
}


def _bounded_decimal(min_value: str, max_value: str, places: int = 2) -> st.SearchStrategy[Decimal]:
    return st.decimals(
        min_value=Decimal(min_value),
        max_value=Decimal(max_value),
        places=places,
        allow_nan=False,
        allow_infinity=False,
    )


@st.composite
def _scoring_case(
    draw: st.DrawFn,
) -> tuple[tuple[SupplierScoringInput, ...], tuple[CriterionSpec, ...]]:
    weight_values = draw(st.tuples(*(_bounded_decimal("0", "1") for _ in _CRITERIA)))
    assume(sum(weight_values) > 0)
    weights = tuple(
        CriterionSpec(criterion, weight, _DIRECTIONS[criterion], criterion.value)
        for criterion, weight in zip(_CRITERIA, weight_values, strict=True)
    )

    n_suppliers = draw(st.integers(min_value=1, max_value=5))
    suppliers = []
    value_strategy = st.one_of(st.none(), _bounded_decimal("0", "1000"))
    for i in range(n_suppliers):
        values = {criterion: draw(value_strategy) for criterion in _CRITERIA}
        excluded = draw(st.booleans())
        suppliers.append(
            _supplier(
                f"Supplier {i}",
                values,
                excluded=excluded,
                exclusion_reason="excluded for test" if excluded else None,
            )
        )
    return tuple(suppliers), weights


class TestTotalScoreBoundsProperty:
    @settings(max_examples=200, deadline=None)
    @given(_scoring_case())
    def test_total_score_is_always_in_the_unit_interval(
        self, case: tuple[tuple[SupplierScoringInput, ...], tuple[CriterionSpec, ...]]
    ) -> None:
        suppliers, weights = case
        result = SCORER.score(suppliers, weights)
        for score in result.scores:
            if not score.excluded:
                assert Decimal("0") <= score.total_score <= Decimal("1")

    @settings(max_examples=200, deadline=None)
    @given(_scoring_case())
    def test_scoring_is_reproducible_for_any_generated_case(
        self, case: tuple[tuple[SupplierScoringInput, ...], tuple[CriterionSpec, ...]]
    ) -> None:
        suppliers, weights = case
        first = SCORER.score(suppliers, weights)
        second = SCORER.score(suppliers, weights)
        assert first == second
