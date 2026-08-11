"""20-case matrix for `AllocationSolver` (docs/planning/06-optimization-methodology.md
§10, docs/planning/09-task-decomposition.md task 6.12).

Pure-domain solver tests: no database, no fixtures beyond in-memory contract
dataclasses. Each test constructs its own minimal `AllocationProblem` via the
small factories below rather than sharing large fixtures, so a failure
pinpoints exactly one behavior.
"""

from __future__ import annotations

import random
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from ortools.sat.python import cp_model

from app.domain.optimization.contracts import (
    AllocationConstraints,
    AllocationProblem,
    AllocationStatus,
    DemandLine,
    EligibilityExclusion,
    LockedAllocation,
    Offer,
    OfferTier,
)
from app.domain.optimization.solver import AllocationSolver, ConsistencyError, ScalingOverflowError


def _line(demand: int, label: str = "Widget") -> DemandLine:
    return DemandLine(rfq_line_id=uuid4(), part_label=label, required_quantity=demand)


def _flat_tier(unit_cost: str) -> tuple[OfferTier, ...]:
    return (OfferTier(min_quantity=1, max_quantity=None, landed_unit_cost=Decimal(unit_cost)),)


def _offer(
    line: DemandLine,
    supplier_label: str,
    unit_cost: str,
    *,
    supplier_id: UUID | None = None,
    moq: int | None = None,
    capacity: int | None = None,
    fixed_cost: str = "0",
    incomplete: bool = False,
    tiers: tuple[OfferTier, ...] | None = None,
) -> Offer:
    return Offer(
        quote_line_id=uuid4(),
        rfq_line_id=line.rfq_line_id,
        supplier_id=supplier_id or uuid4(),
        supplier_label=supplier_label,
        tiers=tiers if tiers is not None else _flat_tier(unit_cost),
        moq=moq,
        capacity=capacity,
        fixed_cost=Decimal(fixed_cost),
        incomplete_landed_cost=incomplete,
    )


SPEC_TIERS: tuple[OfferTier, ...] = (
    OfferTier(min_quantity=1, max_quantity=99, landed_unit_cost=Decimal("12.00")),
    OfferTier(min_quantity=100, max_quantity=499, landed_unit_cost=Decimal("10.50")),
    OfferTier(min_quantity=500, max_quantity=999, landed_unit_cost=Decimal("9.20")),
    OfferTier(min_quantity=1000, max_quantity=None, landed_unit_cost=Decimal("8.60")),
)


class TestSingleSupplierTrivial:
    def test_all_demand_to_the_only_supplier(self) -> None:
        line = _line(100)
        offer = _offer(line, "Acme", "10.00")
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(offer,), constraints=AllocationConstraints())
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert len(result.allocations) == 1
        assert result.allocations[0].quantity == 100
        assert result.allocations[0].supplier_label == "Acme"
        assert result.expected_total_cost == Decimal("1000.000000")
        assert result.rejected_alternatives == ()  # already single-supplier: nothing to compare


class TestCapacityForcesSplit:
    def test_split_matches_hand_calculation(self) -> None:
        line = _line(150)
        cheap = _offer(line, "Cheap", "10.00", capacity=100)
        pricier = _offer(line, "Pricier", "12.00")
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,), offers=(cheap, pricier), constraints=AllocationConstraints()
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        by_label = {a.supplier_label: a.quantity for a in result.allocations}
        assert by_label == {"Cheap": 100, "Pricier": 50}
        # hand calculation: 100*10.00 + 50*12.00 = 1600.00
        assert result.expected_total_cost == Decimal("1600.000000")
        assert any(b.name == "capacity" for b in result.binding_constraints)


class TestMoqExcludesTinyAllocation:
    def test_second_choice_used_when_cheapest_moq_exceeds_demand(self) -> None:
        line = _line(50)
        cheap_but_moq = _offer(line, "CheapHighMoq", "5.00", moq=200)
        usable = _offer(line, "UsableSupplier", "9.00")
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,), offers=(cheap_but_moq, usable), constraints=AllocationConstraints()
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert len(result.allocations) == 1
        assert result.allocations[0].supplier_label == "UsableSupplier"
        assert result.allocations[0].quantity == 50


class TestMoqPlusDemandInfeasible:
    def test_single_offer_moq_above_demand_is_presolve_infeasible(self) -> None:
        line = _line(50)
        offer = _offer(line, "OnlyOne", "9.00", moq=500)
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(offer,), constraints=AllocationConstraints())
        )
        assert result.status == AllocationStatus.INFEASIBLE
        assert result.infeasibility is not None
        assert "moq" in result.infeasibility.conflicting_groups
        assert "minimum order quantity" in result.infeasibility.detail
        # pre-solve infeasibility: no CP-SAT solve happened
        assert result.stats is not None
        assert result.stats.status_raw == "PRESOLVE_INFEASIBLE"


class TestPriceBreakTierSwitch:
    def test_tier_recomputed_at_boundary_crossing(self) -> None:
        line99 = _line(99)
        line100 = _line(100)
        offer99 = _offer(line99, "X", "0", tiers=SPEC_TIERS)
        offer100 = _offer(line100, "X", "0", tiers=SPEC_TIERS)
        solver = AllocationSolver()

        below = solver.solve(
            AllocationProblem(
                lines=(line99,), offers=(offer99,), constraints=AllocationConstraints()
            )
        )
        at_break = solver.solve(
            AllocationProblem(
                lines=(line100,), offers=(offer100,), constraints=AllocationConstraints()
            )
        )

        assert below.allocations[0].tier_applied.landed_unit_cost == Decimal("12.00")
        assert below.allocations[0].line_cost == Decimal("1188.000000")  # 99 * 12.00

        assert at_break.allocations[0].tier_applied.landed_unit_cost == Decimal("10.50")
        assert at_break.allocations[0].tier_applied.min_quantity == 100
        # cost uses the ALLOCATED quantity's tier, not the pre-break tier
        assert at_break.allocations[0].line_cost == Decimal("1050.000000")  # 100 * 10.50


class TestMaxSupplierCountBinding:
    def test_binding_at_the_configured_maximum(self) -> None:
        line = _line(150)
        s1 = _offer(line, "S1", "8.00", capacity=100)
        s2 = _offer(line, "S2", "9.00", capacity=100)
        s3 = _offer(line, "S3", "20.00")  # unlimited but expensive: last resort
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(s1, s2, s3),
                constraints=AllocationConstraints(max_supplier_count=2),
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert result.supplier_count == 2
        assert {a.supplier_label for a in result.allocations} == {"S1", "S2"}
        assert any(b.name == "max_supplier_count" for b in result.binding_constraints)


class TestConcentrationCapForcesSplit:
    def test_no_supplier_exceeds_the_cost_basis_cap(self) -> None:
        line = _line(100)
        s1 = _offer(line, "S1", "10.00")
        s2 = _offer(line, "S2", "10.00")
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(s1, s2),
                constraints=AllocationConstraints(max_concentration=Decimal("0.5")),
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        by_label = {a.supplier_label: a.line_cost for a in result.allocations}
        assert len(by_label) == 2
        total = sum(by_label.values(), Decimal("0"))
        assert result.expected_total_cost == total
        # cost basis, numerically: neither supplier's spend exceeds 50% of total
        for cost in by_label.values():
            assert cost <= total * Decimal("0.5")


class TestBudgetInfeasibleWithRelaxation:
    def test_shortfall_reported_with_a_numeric_relaxation(self) -> None:
        line = _line(100)
        offer = _offer(line, "Only", "10.00")
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(offer,),
                constraints=AllocationConstraints(budget_limit=Decimal("500")),
            )
        )
        assert result.status == AllocationStatus.INFEASIBLE
        assert result.infeasibility is not None
        assert result.infeasibility.conflicting_groups == ("budget",)
        assert "1000" in result.infeasibility.detail  # exact cheapest-possible shortfall
        assert result.infeasibility.minimal_relaxation is not None
        assert "1000" in result.infeasibility.minimal_relaxation


class TestBudgetFeasibleBoundary:
    def test_budget_exactly_at_optimum_is_feasible(self) -> None:
        line = _line(100)
        offer = _offer(line, "Only", "10.00")
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(offer,),
                constraints=AllocationConstraints(budget_limit=Decimal("1000.00")),
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert result.expected_total_cost == Decimal("1000.000000")


class TestLockedAllocationHonored:
    def test_locked_quantity_respected_and_remainder_optimized(self) -> None:
        line = _line(100)
        s_cheap = uuid4()
        s_locked = uuid4()
        cheap = _offer(line, "Cheap", "8.00", supplier_id=s_cheap)
        locked_supplier = _offer(line, "Locked", "15.00", supplier_id=s_locked)
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(cheap, locked_supplier),
                constraints=AllocationConstraints(
                    locked_allocations=(
                        LockedAllocation(
                            rfq_line_id=line.rfq_line_id, supplier_id=s_locked, quantity=30
                        ),
                    )
                ),
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        by_label = {a.supplier_label: a.quantity for a in result.allocations}
        assert by_label == {"Locked": 30, "Cheap": 70}


class TestContradictoryLockPresolveInfeasible:
    def test_lock_exceeding_line_demand_is_a_targeted_presolve_error(self) -> None:
        # capacity is unlimited (None) so the general capacity-shortfall
        # pre-check does not fire; the lock itself (150 > 100 required) is
        # the sole, isolated contradiction under test.
        line = _line(100)
        supplier_id = uuid4()
        offer = _offer(line, "Uncapped", "10.00", supplier_id=supplier_id, capacity=None)
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(offer,),
                constraints=AllocationConstraints(
                    locked_allocations=(
                        LockedAllocation(
                            rfq_line_id=line.rfq_line_id, supplier_id=supplier_id, quantity=150
                        ),
                    )
                ),
            )
        )
        assert result.status == AllocationStatus.INFEASIBLE
        assert result.infeasibility is not None
        assert result.infeasibility.conflicting_groups == ("locks",)
        assert result.stats is not None
        assert result.stats.status_raw == "PRESOLVE_INFEASIBLE"  # no solver call

    def test_lock_below_moq_is_a_targeted_presolve_error(self) -> None:
        line = _line(100)
        supplier_id = uuid4()
        offer = _offer(line, "MoqSupplier", "10.00", supplier_id=supplier_id, moq=40)
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(offer,),
                constraints=AllocationConstraints(
                    locked_allocations=(
                        LockedAllocation(
                            rfq_line_id=line.rfq_line_id, supplier_id=supplier_id, quantity=10
                        ),
                    )
                ),
            )
        )
        assert result.status == AllocationStatus.INFEASIBLE
        assert result.infeasibility is not None
        assert result.infeasibility.conflicting_groups == ("locks",)
        assert "minimum order quantity" in result.infeasibility.detail


class TestExclusionHonored:
    def test_excluded_supplier_never_allocated_even_when_cheapest(self) -> None:
        line = _line(100)
        cheap_id = uuid4()
        cheap = _offer(line, "Cheap", "5.00", supplier_id=cheap_id)
        other = _offer(line, "Other", "9.00")
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(cheap, other),
                constraints=AllocationConstraints(excluded_supplier_ids=(cheap_id,)),
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert len(result.allocations) == 1
        assert result.allocations[0].supplier_label == "Other"


class TestIncompleteOfferHandling:
    def test_dropped_with_reason_by_default(self) -> None:
        line = _line(100)
        offer = _offer(line, "Incomplete", "1.00", incomplete=True)
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(offer,), constraints=AllocationConstraints())
        )
        assert result.status == AllocationStatus.INFEASIBLE
        assert result.infeasibility is not None
        assert "incomplete" in result.infeasibility.detail

    def test_allowed_when_flag_set(self) -> None:
        line = _line(100)
        offer = _offer(line, "Incomplete", "1.00", incomplete=True)
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(offer,),
                constraints=AllocationConstraints(allow_incomplete_offers=True),
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert result.allocations[0].quantity == 100

    def test_pre_excluded_pass_through_is_not_re_explained(self) -> None:
        line = _line(100)
        dropped = _offer(line, "Dropped", "1.00")
        usable = _offer(line, "Usable", "2.00")
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(dropped, usable),
                constraints=AllocationConstraints(),
                pre_excluded=(
                    EligibilityExclusion(
                        quote_line_id=dropped.quote_line_id,
                        supplier_label="Dropped",
                        reason="lead time exceeds required-by date",
                    ),
                ),
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert len(result.allocations) == 1
        assert result.allocations[0].supplier_label == "Usable"


class TestOpenEndedTopTier:
    def test_far_above_open_ended_minimum_still_prices_correctly(self) -> None:
        line = _line(5_000)
        offer = _offer(line, "Bulk", "0", tiers=SPEC_TIERS)
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(offer,), constraints=AllocationConstraints())
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert result.allocations[0].tier_applied.max_quantity is None
        assert result.allocations[0].tier_applied.landed_unit_cost == Decimal("8.60")
        assert result.expected_total_cost == Decimal("43000.000000")  # 5000 * 8.60


class TestDeterminism:
    @staticmethod
    def _problem() -> AllocationProblem:
        line = _line(150)
        s1 = _offer(line, "A", "10.00", capacity=100)
        s2 = _offer(line, "B", "11.00", capacity=100)
        s3 = _offer(line, "C", "12.00")
        return AllocationProblem(
            lines=(line,), offers=(s1, s2, s3), constraints=AllocationConstraints()
        )

    def test_repeated_solves_are_bit_identical(self) -> None:
        problem = self._problem()
        solver = AllocationSolver()
        results = [solver.solve(problem) for _ in range(3)]
        hashes = {r.stats.model_hash for r in results if r.stats is not None}
        assert len(hashes) == 1
        allocation_sets = {tuple(r.allocations) for r in results}
        assert len(allocation_sets) == 1
        costs = {r.expected_total_cost for r in results}
        assert len(costs) == 1

    def test_permuted_offer_order_yields_the_same_hash_and_result(self) -> None:
        line = _line(150)
        s1 = _offer(line, "A", "10.00", capacity=100)
        s2 = _offer(line, "B", "11.00", capacity=100)
        s3 = _offer(line, "C", "12.00")
        solver = AllocationSolver()

        original = AllocationProblem(
            lines=(line,), offers=(s1, s2, s3), constraints=AllocationConstraints()
        )
        permuted = AllocationProblem(
            lines=(line,), offers=(s3, s1, s2), constraints=AllocationConstraints()
        )

        result_a = solver.solve(original)
        result_b = solver.solve(permuted)

        assert result_a.stats is not None and result_b.stats is not None
        assert result_a.stats.model_hash == result_b.stats.model_hash
        assert result_a.allocations == result_b.allocations
        assert result_a.expected_total_cost == result_b.expected_total_cost


class TestStatusHonesty:
    @staticmethod
    def _hard_problem() -> AllocationProblem:
        rng = random.Random(7)
        lines: list[DemandLine] = []
        offers: list[Offer] = []
        for i in range(20):
            line = _line(rng.randint(200, 800), label=f"P{i}")
            lines.append(line)
            for j in range(10):
                tiers = (
                    OfferTier(
                        min_quantity=1,
                        max_quantity=49,
                        landed_unit_cost=Decimal(str(round(rng.uniform(5, 20), 2))),
                    ),
                    OfferTier(
                        min_quantity=50,
                        max_quantity=199,
                        landed_unit_cost=Decimal(str(round(rng.uniform(4, 18), 2))),
                    ),
                    OfferTier(
                        min_quantity=200,
                        max_quantity=None,
                        landed_unit_cost=Decimal(str(round(rng.uniform(2, 15), 2))),
                    ),
                )
                offers.append(
                    _offer(
                        line,
                        f"S{i}_{j}",
                        "0",
                        tiers=tiers,
                        moq=rng.choice([None, None, 10, 20]),
                        capacity=rng.randint(50, 300),
                        fixed_cost=str(rng.choice([0, 0, 5, 10])),
                    )
                )
        return AllocationProblem(
            lines=tuple(lines), offers=tuple(offers), constraints=AllocationConstraints()
        )

    def test_reported_status_never_upgrades_the_solver_raw_claim(self) -> None:
        problem = self._hard_problem()
        solver = AllocationSolver(max_deterministic_time=0.01)
        result = solver.solve(problem)
        assert result.stats is not None
        raw = result.stats.status_raw
        # the invariant under test: the reported status is a 1:1 mirror of
        # the solver's own claim, in either direction. It never reports
        # OPTIMAL when the raw status was only FEASIBLE, or vice-versa.
        assert (result.status == AllocationStatus.OPTIMAL) == (raw == "OPTIMAL")
        assert (result.status == AllocationStatus.FEASIBLE) == (raw == "FEASIBLE")
        assert (result.status == AllocationStatus.INFEASIBLE) == (raw == "INFEASIBLE")
        if raw not in ("OPTIMAL", "FEASIBLE", "INFEASIBLE"):
            assert result.status == AllocationStatus.ERROR


class TestScaledOverflowRaises:
    def test_absurd_unit_cost_raises_before_solving(self) -> None:
        line = _line(10)
        offer = _offer(line, "Huge", "999999999999999999.00")
        with pytest.raises(ScalingOverflowError):
            AllocationSolver().solve(
                AllocationProblem(
                    lines=(line,), offers=(offer,), constraints=AllocationConstraints()
                )
            )


class TestSingleSupplierAlternative:
    def test_sentence_present_on_a_genuine_split(self) -> None:
        line = _line(300)
        s1 = _offer(line, "A", "10.00", capacity=200)
        s2 = _offer(line, "B", "11.00", capacity=200)
        s3 = _offer(line, "C", "12.00", capacity=200)
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,), offers=(s1, s2, s3), constraints=AllocationConstraints()
            )
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert result.supplier_count > 1
        assert len(result.rejected_alternatives) == 1
        assert "single-supplier" in result.rejected_alternatives[0]

    def test_no_feasible_single_supplier_alternative_reported_honestly(self) -> None:
        # each supplier capped below demand: forcing K=1 is genuinely infeasible
        line = _line(300)
        s1 = _offer(line, "A", "10.00", capacity=200)
        s2 = _offer(line, "B", "11.00", capacity=200)
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(s1, s2), constraints=AllocationConstraints())
        )
        assert result.status == AllocationStatus.OPTIMAL
        assert result.rejected_alternatives == ("no feasible single-supplier alternative",)

    def test_skipped_when_main_solve_is_already_single_supplier(self) -> None:
        line = _line(100)
        offer = _offer(line, "Only", "10.00")
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(offer,), constraints=AllocationConstraints())
        )
        assert result.supplier_count == 1
        assert result.rejected_alternatives == ()


class TestEmptyOffersInfeasible:
    def test_no_offers_at_all_is_explained(self) -> None:
        line = _line(100)
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(), constraints=AllocationConstraints())
        )
        assert result.status == AllocationStatus.INFEASIBLE
        assert result.infeasibility is not None
        assert result.infeasibility.conflicting_groups != ()
        assert "Widget" in result.infeasibility.detail
        assert result.stats is not None
        assert result.stats.status_raw == "PRESOLVE_INFEASIBLE"


# --------------------------------------------------------------------------
# Extra coverage beyond the required 20, exercising behaviors the contract
# specifically calls out (solver_error honesty, consistency guard, minimal
# core reporting on a genuine multi-constraint conflict).
# --------------------------------------------------------------------------


class TestSolverErrorNeverCrashes:
    def test_injected_exception_is_reported_as_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(self: cp_model.CpSolver, model: cp_model.CpModel) -> int:
            raise RuntimeError("injected solver fault")

        monkeypatch.setattr(cp_model.CpSolver, "solve", _boom)

        line = _line(10)
        offer = _offer(line, "Only", "1.00")
        result = AllocationSolver().solve(
            AllocationProblem(lines=(line,), offers=(offer,), constraints=AllocationConstraints())
        )
        assert result.status == AllocationStatus.ERROR
        assert result.error_message == "injected solver fault"
        assert result.stats is None


class TestConsistencyGuard:
    def test_disagreement_with_the_exact_recomputation_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The result's tier must equal an independent Decimal recomputation
        via `breaks.select_price_break`. If that recomputation is forced to
        disagree with what CP-SAT chose, the mismatch must surface as a
        raised `ConsistencyError`, never as a silently wrong allocation."""
        import app.domain.optimization.solver as solver_mod
        from app.domain.landed_cost.contracts import PriceBreakSelection

        def _always_unpriceable(
            tiers: object, quantity: object
        ) -> PriceBreakSelection:
            return PriceBreakSelection(
                tier=None, reason="forced mismatch for the consistency-guard test"
            )

        monkeypatch.setattr(solver_mod, "select_price_break", _always_unpriceable)

        line = _line(10)
        offer = _offer(line, "Only", "1.00")
        with pytest.raises(ConsistencyError):
            AllocationSolver().solve(
                AllocationProblem(
                    lines=(line,), offers=(offer,), constraints=AllocationConstraints()
                )
            )


class TestConflictingGroupsOnGenuineCpSatInfeasibility:
    def test_minimal_core_excludes_irrelevant_groups(self) -> None:
        line = _line(300)
        s1 = _offer(line, "A", "10.00", capacity=200)
        s2 = _offer(line, "B", "11.00", capacity=200)
        s3 = _offer(line, "C", "12.00", capacity=200)
        result = AllocationSolver().solve(
            AllocationProblem(
                lines=(line,),
                offers=(s1, s2, s3),
                constraints=AllocationConstraints(max_supplier_count=1),
            )
        )
        assert result.status == AllocationStatus.INFEASIBLE
        assert result.infeasibility is not None
        # only supplier_count is genuinely conflicting; capacity alone
        # (3 x 200 = 600 >= 300) is not, and must not appear in the core
        assert result.infeasibility.conflicting_groups == ("supplier_count",)
        assert result.infeasibility.minimal_relaxation is not None
        assert "max_supplier_count" in result.infeasibility.minimal_relaxation
