"""Scenario/results schema tests: migration coverage, composite-FK org
isolation (comparison_scenarios -> rfqs/scoring_configurations,
scenario_results -> comparison_scenarios, allocation_results ->
comparison_scenarios), comparison_strategy_enum/scenario_state_enum/
allocation_status_enum rejecting invalid values, immutability-by-convention
on scenario_results/allocation_results (no updated_at/version/archived_at
columns at all, one-result-per-scenario uniqueness), CHECK-constraint
coverage (expected_total_cost/supplier_count non-negativity, model_hash/
stats pairing), and snapshot/output JSONB round-tripping (docs/planning/
02-erd.md §7; §10; docs/SPEC.md §Scenario comparison).

Fixture pattern mirrors test_documents_schema.py: a committed org + user +
RFQ + scoring configuration, built directly against the migrated database
with raw SQL (schema tests intentionally avoid the ORM so they exercise the
same DDL a real client would hit)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError


def _j(value: Any) -> str:
    return json.dumps(value)


def _mk_org(db, org_id, slug) -> None:
    db.execute(
        text(
            "INSERT INTO organizations (id, slug, name, base_currency, is_demo,"
            " version, created_at, updated_at)"
            " VALUES (:id, :slug, 'Test Org', 'USD', false, 1, :now, :now)"
        ),
        {"id": org_id, "slug": slug, "now": datetime.now(UTC)},
    )


def _mk_user(db, user_id, email) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO users (id, email, password_hash, full_name, is_active,"
            " created_at, updated_at)"
            " VALUES (:id, :email, 'x', 'Test User', true, :now, :now)"
        ),
        {"id": user_id, "email": email, "now": now},
    )


def _mk_rfq(db, rfq_id, org_id, created_by_id, internal_reference) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO rfqs (id, organization_id, name, internal_reference,"
            " status, base_currency, due_date, created_by_id,"
            " version, created_at, updated_at)"
            " VALUES (:id, :org, :ref, :ref, 'draft'::rfq_status_enum,"
            " 'USD', :due, :creator, 1, :now, :now)"
        ),
        {
            "id": rfq_id,
            "org": org_id,
            "ref": internal_reference,
            "due": date(2026, 9, 1),
            "creator": created_by_id,
            "now": now,
        },
    )


def _mk_scoring_configuration(db, config_id, org_id, created_by_id, name) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO scoring_configurations (id, organization_id, name, weights,"
            " is_sample, created_by_id, created_at, updated_at, version)"
            " VALUES (:id, :org, :name, CAST(:weights AS jsonb), false, :creator,"
            " :now, :now, 1)"
        ),
        {
            "id": config_id,
            "org": org_id,
            "name": name,
            "weights": _j([]),
            "creator": created_by_id,
            "now": now,
        },
    )


def _mk_comparison_scenario(
    db,
    scenario_id,
    org_id,
    rfq_id,
    created_by_id,
    name="Baseline",
    strategy="lowest_unit_price",
    state="draft",
    scoring_configuration_id=None,
    constraints_snapshot=None,
    assumptions_snapshot=None,
    fx_snapshot=None,
    quote_snapshot_refs=None,
    weights_snapshot=None,
    calculation_version="1.0.0",
    solver_version="1.0.0",
    completed_at=None,
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO comparison_scenarios (id, organization_id, rfq_id, name,"
            " strategy, scoring_configuration_id, constraints_snapshot,"
            " assumptions_snapshot, fx_snapshot, quote_snapshot_refs, weights_snapshot,"
            " calculation_version, solver_version, state, created_by_id, completed_at,"
            " version, created_at, updated_at)"
            " VALUES (:id, :org, :rfq, :name, CAST(:strategy AS comparison_strategy_enum),"
            " :scoring_config, CAST(:constraints AS jsonb), CAST(:assumptions AS jsonb),"
            " CAST(:fx AS jsonb), CAST(:quotes AS jsonb), CAST(:weights AS jsonb),"
            " :calc_version, :solver_version, CAST(:state AS scenario_state_enum),"
            " :creator, :completed_at, 1, :now, :now)"
        ),
        {
            "id": scenario_id,
            "org": org_id,
            "rfq": rfq_id,
            "name": name,
            "strategy": strategy,
            "scoring_config": scoring_configuration_id,
            "constraints": _j(constraints_snapshot if constraints_snapshot is not None else {}),
            "assumptions": _j(assumptions_snapshot if assumptions_snapshot is not None else {}),
            "fx": _j(fx_snapshot if fx_snapshot is not None else []),
            "quotes": _j(quote_snapshot_refs if quote_snapshot_refs is not None else []),
            "weights": _j(weights_snapshot if weights_snapshot is not None else []),
            "calc_version": calculation_version,
            "solver_version": solver_version,
            "state": state,
            "creator": created_by_id,
            "completed_at": completed_at,
            "now": now,
        },
    )


def _mk_scenario_result(
    db,
    result_id,
    org_id,
    scenario_id,
    scoring_output=None,
    calculation_version="1.0.0",
    scoring_version="1.0.0",
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO scenario_results (id, organization_id, scenario_id,"
            " scoring_output, calculation_version, scoring_version, computed_at)"
            " VALUES (:id, :org, :scenario, CAST(:scoring AS jsonb), :calc_version,"
            " :scoring_version, :now)"
        ),
        {
            "id": result_id,
            "org": org_id,
            "scenario": scenario_id,
            "scoring": _j(scoring_output if scoring_output is not None else {"scores": []}),
            "calc_version": calculation_version,
            "scoring_version": scoring_version,
            "now": now,
        },
    )


_DEFAULT_STATS = {
    "status_raw": "OPTIMAL",
    "deterministic_time": 0.01,
    "model_hash": "deadbeef",
    "num_variables": 1,
    "num_constraints": 1,
}


def _mk_allocation_result(
    db,
    result_id,
    org_id,
    scenario_id,
    status="optimal",
    allocations=None,
    expected_total_cost="1000.00",
    supplier_count=1,
    binding_constraints=None,
    infeasibility=None,
    rejected_alternatives=None,
    stats=_DEFAULT_STATS,
    model_hash="deadbeef",
    optimization_version="1.0.0",
    error_message=None,
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO allocation_results (id, organization_id, scenario_id, status,"
            " allocations, expected_total_cost, supplier_count, binding_constraints,"
            " infeasibility, rejected_alternatives, stats, model_hash,"
            " optimization_version, error_message, solved_at)"
            " VALUES (:id, :org, :scenario, CAST(:status AS allocation_status_enum),"
            " CAST(:allocations AS jsonb), :cost, :supplier_count,"
            " CAST(:binding AS jsonb), CAST(:infeasibility AS jsonb),"
            " CAST(:rejected AS jsonb), CAST(:stats AS jsonb), :model_hash,"
            " :opt_version, :error_message, :now)"
        ),
        {
            "id": result_id,
            "org": org_id,
            "scenario": scenario_id,
            "status": status,
            "allocations": _j(allocations if allocations is not None else []),
            "cost": expected_total_cost,
            "supplier_count": supplier_count,
            "binding": _j(binding_constraints if binding_constraints is not None else []),
            "infeasibility": None if infeasibility is None else _j(infeasibility),
            "rejected": _j(rejected_alternatives if rejected_alternatives is not None else []),
            "stats": None if stats is None else _j(stats),
            "model_hash": model_hash,
            "opt_version": optimization_version,
            "error_message": error_message,
            "now": now,
        },
    )


class _Fixture:
    """Bundles one org's worth of parent rows comparison_scenarios needs."""

    def __init__(self, db, make_uuid, label="org") -> None:
        self.db = db
        self.make_uuid = make_uuid
        org_id = make_uuid.new_id()
        self.org_id = org_id
        _mk_org(db, org_id, f"{label}-{org_id}")
        self.user_id = make_uuid.new_id()
        _mk_user(db, self.user_id, f"user-{self.user_id}@example.test")
        self.rfq_id = make_uuid.new_id()
        _mk_rfq(db, self.rfq_id, org_id, self.user_id, f"RFQ-{org_id}")
        self.scoring_configuration_id = make_uuid.new_id()
        _mk_scoring_configuration(
            db, self.scoring_configuration_id, org_id, self.user_id, f"Config-{org_id}"
        )

    def mk_scenario(self, **kwargs):
        scenario_id = self.make_uuid.new_id()
        _mk_comparison_scenario(
            self.db, scenario_id, self.org_id, self.rfq_id, self.user_id, **kwargs
        )
        return scenario_id

    def mk_scenario_result(self, scenario_id=None, **kwargs):
        scenario_id = scenario_id or self.mk_scenario()
        result_id = self.make_uuid.new_id()
        _mk_scenario_result(self.db, result_id, self.org_id, scenario_id, **kwargs)
        return result_id

    def mk_allocation_result(self, scenario_id=None, **kwargs):
        scenario_id = scenario_id or self.mk_scenario()
        result_id = self.make_uuid.new_id()
        _mk_allocation_result(self.db, result_id, self.org_id, scenario_id, **kwargs)
        return result_id


@pytest.fixture()
def fx(db, make_uuid):
    return _Fixture(db, make_uuid, label="org-a")


class TestScenariosSchema:
    def test_migration_creates_scenario_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('comparison_scenarios', 'scenario_results', 'allocation_results')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {
            "comparison_scenarios",
            "scenario_results",
            "allocation_results",
        }

    # -- comparison_scenarios -------------------------------------------

    def test_composite_fk_rejects_cross_org_scenario_to_rfq(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            _mk_comparison_scenario(
                db, make_uuid.new_id(), org_a.org_id, org_b.rfq_id, org_a.user_id
            )

    def test_composite_fk_rejects_cross_org_scenario_to_scoring_configuration(
        self, db, make_uuid
    ) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            _mk_comparison_scenario(
                db,
                make_uuid.new_id(),
                org_a.org_id,
                org_a.rfq_id,
                org_a.user_id,
                scoring_configuration_id=org_b.scoring_configuration_id,
            )

    def test_composite_fk_accepts_same_org_scenario(self, fx) -> None:
        scenario_id = fx.mk_scenario(scoring_configuration_id=fx.scoring_configuration_id)
        count = fx.db.execute(
            text("SELECT count(*) FROM comparison_scenarios WHERE id = :id"),
            {"id": scenario_id},
        ).scalar_one()
        assert count == 1

    def test_scoring_configuration_nullable(self, fx) -> None:
        """module docstring point 2: single-criterion strategies need no
        weighted scoring configuration at all."""
        scenario_id = fx.mk_scenario(
            strategy="lowest_unit_price", scoring_configuration_id=None
        )
        value = fx.db.execute(
            text(
                "SELECT scoring_configuration_id FROM comparison_scenarios WHERE id = :id"
            ),
            {"id": scenario_id},
        ).scalar_one()
        assert value is None

    def test_comparison_strategy_enum_rejects_invalid_value(self, fx) -> None:
        with pytest.raises(DataError):
            fx.mk_scenario(strategy="cheapest_vibes")

    def test_comparison_strategy_enum_accepts_every_documented_member(self, fx) -> None:
        for strategy in (
            "lowest_unit_price",
            "lowest_landed_cost",
            "fastest_delivery",
            "lowest_risk",
            "balanced",
            "custom",
        ):
            scenario_id = fx.mk_scenario(strategy=strategy)
            value = fx.db.execute(
                text("SELECT strategy FROM comparison_scenarios WHERE id = :id"),
                {"id": scenario_id},
            ).scalar_one()
            assert value == strategy

    def test_scenario_state_enum_rejects_invalid_value(self, fx) -> None:
        with pytest.raises(DataError):
            fx.mk_scenario(state="in_limbo")

    def test_scenario_state_defaults_to_draft(self, fx) -> None:
        scenario_id = fx.make_uuid.new_id()
        fx.db.execute(
            text(
                "INSERT INTO comparison_scenarios (id, organization_id, rfq_id, name,"
                " strategy, constraints_snapshot, assumptions_snapshot, fx_snapshot,"
                " quote_snapshot_refs, weights_snapshot, calculation_version,"
                " solver_version, created_by_id, version, created_at, updated_at)"
                " VALUES (:id, :org, :rfq, 'Default state', 'balanced'::comparison_strategy_enum,"
                " '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,"
                " '1.0.0', '1.0.0', :creator, 1, :now, :now)"
            ),
            {
                "id": scenario_id,
                "org": fx.org_id,
                "rfq": fx.rfq_id,
                "creator": fx.user_id,
                "now": datetime.now(UTC),
            },
        )
        state = fx.db.execute(
            text("SELECT state FROM comparison_scenarios WHERE id = :id"),
            {"id": scenario_id},
        ).scalar_one()
        assert state == "draft"

    def test_snapshot_jsonb_round_trips(self, fx) -> None:
        """02-erd.md §10: the FULL reproducibility snapshot - every input
        that could change later - must survive a write/read cycle exactly."""
        constraints = {"max_supplier_count": 3, "budget_limit": "9140.00"}
        assumptions = {"tax_is_recoverable": False, "note": "assume DDP"}
        fx_rates = [
            {"rate_id": str(fx.make_uuid.new_id()), "base": "USD", "quote": "EUR", "rate": "0.92"}
        ]
        quote_refs = [
            {
                "quote_id": str(fx.make_uuid.new_id()),
                "revision": 1,
                "landed_cost_result_id": str(fx.make_uuid.new_id()),
            }
        ]
        weights = [
            {"criterion": "total_landed_cost", "weight": "0.35", "direction": "lower_is_better"}
        ]
        scenario_id = fx.mk_scenario(
            constraints_snapshot=constraints,
            assumptions_snapshot=assumptions,
            fx_snapshot=fx_rates,
            quote_snapshot_refs=quote_refs,
            weights_snapshot=weights,
        )
        row = fx.db.execute(
            text(
                "SELECT constraints_snapshot, assumptions_snapshot, fx_snapshot,"
                " quote_snapshot_refs, weights_snapshot FROM comparison_scenarios"
                " WHERE id = :id"
            ),
            {"id": scenario_id},
        ).one()
        assert row.constraints_snapshot == constraints
        assert row.assumptions_snapshot == assumptions
        assert row.fx_snapshot == fx_rates
        assert row.quote_snapshot_refs == quote_refs
        assert row.weights_snapshot == weights

    def test_snapshot_jsonb_defaults_when_omitted(self, fx) -> None:
        scenario_id = fx.mk_scenario()
        row = fx.db.execute(
            text(
                "SELECT constraints_snapshot, assumptions_snapshot, fx_snapshot,"
                " quote_snapshot_refs, weights_snapshot FROM comparison_scenarios"
                " WHERE id = :id"
            ),
            {"id": scenario_id},
        ).one()
        assert row.constraints_snapshot == {}
        assert row.assumptions_snapshot == {}
        assert row.fx_snapshot == []
        assert row.quote_snapshot_refs == []
        assert row.weights_snapshot == []

    # -- scenario_results -------------------------------------------------

    def test_composite_fk_rejects_cross_org_scenario_result_to_scenario(
        self, db, make_uuid
    ) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        scenario_b = org_b.mk_scenario()

        with pytest.raises(IntegrityError):
            _mk_scenario_result(db, make_uuid.new_id(), org_a.org_id, scenario_b)

    def test_composite_fk_accepts_same_org_scenario_result(self, fx) -> None:
        result_id = fx.mk_scenario_result()
        count = fx.db.execute(
            text("SELECT count(*) FROM scenario_results WHERE id = :id"), {"id": result_id}
        ).scalar_one()
        assert count == 1

    def test_scenario_result_unique_per_scenario(self, fx) -> None:
        scenario_id = fx.mk_scenario()
        fx.mk_scenario_result(scenario_id=scenario_id)
        with pytest.raises(IntegrityError):
            fx.mk_scenario_result(scenario_id=scenario_id)

    def test_scenario_results_no_mutability_columns(self, db) -> None:
        """module docstring 'Immutability': result rows are never updated,
        so the mutability columns must be entirely absent, not merely
        unused."""
        columns = (
            db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'scenario_results'"
                )
            )
            .scalars()
            .all()
        )
        assert set(columns).isdisjoint(
            {"updated_at", "version", "archived_at", "archived_by_id", "archive_reason"}
        )

    def test_scenario_result_scoring_output_round_trips(self, fx) -> None:
        """mirrors app.domain.scoring.contracts.ScoringResult: ranked scores
        incl. per-criterion reasons, weights used, cohort size, notes."""
        scoring_output = {
            "scores": [
                {
                    "supplier_id": str(fx.make_uuid.new_id()),
                    "supplier_name": "Acme Co",
                    "total_score": "0.812345",
                    "rank": 1,
                    "criterion_scores": [
                        {
                            "criterion": "total_landed_cost",
                            "raw_value": "7240.55",
                            "normalized_score": "1.0",
                            "effective_weight": "0.35",
                            "weighted_contribution": "0.35",
                            "reason": "lowest landed cost 7240.55 of cohort 7240.55-9100.00",
                        }
                    ],
                    "missing_criteria": [],
                    "weights_renormalized": False,
                    "excluded": False,
                    "exclusion_reason": None,
                }
            ],
            "weights_used": [
                {
                    "criterion": "total_landed_cost",
                    "weight": "0.35",
                    "direction": "lower_is_better",
                }
            ],
            "cohort_size": 1,
            "notes": [],
        }
        result_id = fx.mk_scenario_result(scoring_output=scoring_output)
        stored = fx.db.execute(
            text("SELECT scoring_output FROM scenario_results WHERE id = :id"),
            {"id": result_id},
        ).scalar_one()
        assert stored == scoring_output

    # -- allocation_results -------------------------------------------------

    def test_composite_fk_rejects_cross_org_allocation_result_to_scenario(
        self, db, make_uuid
    ) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        scenario_b = org_b.mk_scenario()

        with pytest.raises(IntegrityError):
            _mk_allocation_result(db, make_uuid.new_id(), org_a.org_id, scenario_b)

    def test_composite_fk_accepts_same_org_allocation_result(self, fx) -> None:
        result_id = fx.mk_allocation_result()
        count = fx.db.execute(
            text("SELECT count(*) FROM allocation_results WHERE id = :id"), {"id": result_id}
        ).scalar_one()
        assert count == 1

    def test_allocation_result_unique_per_scenario(self, fx) -> None:
        scenario_id = fx.mk_scenario()
        fx.mk_allocation_result(scenario_id=scenario_id)
        with pytest.raises(IntegrityError):
            fx.mk_allocation_result(scenario_id=scenario_id)

    def test_allocation_results_no_mutability_columns(self, db) -> None:
        columns = (
            db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'allocation_results'"
                )
            )
            .scalars()
            .all()
        )
        assert set(columns).isdisjoint(
            {"updated_at", "version", "archived_at", "archived_by_id", "archive_reason"}
        )

    def test_allocation_status_enum_rejects_invalid_value(self, fx) -> None:
        with pytest.raises(DataError):
            fx.mk_allocation_result(status="probably_fine")

    def test_allocation_status_enum_accepts_every_frozen_member(self, fx) -> None:
        """wraps app.domain.optimization.contracts.AllocationStatus directly
        (4 members) - module docstring point 8."""
        for status in ("optimal", "feasible", "infeasible", "error"):
            result_id = fx.mk_allocation_result(status=status)
            value = fx.db.execute(
                text("SELECT status FROM allocation_results WHERE id = :id"),
                {"id": result_id},
            ).scalar_one()
            assert value == status

    def test_allocation_result_allocations_jsonb_round_trips(self, fx) -> None:
        """mirrors app.domain.optimization.contracts.AllocationEntry."""
        allocations = [
            {
                "rfq_line_id": str(fx.make_uuid.new_id()),
                "quote_line_id": str(fx.make_uuid.new_id()),
                "supplier_id": str(fx.make_uuid.new_id()),
                "supplier_label": "Acme Co",
                "quantity": 500,
                "tier_applied": {
                    "min_quantity": 100,
                    "max_quantity": None,
                    "landed_unit_cost": "14.480000",
                },
                "line_cost": "7240.000000",
            }
        ]
        result_id = fx.mk_allocation_result(allocations=allocations, supplier_count=1)
        stored = fx.db.execute(
            text("SELECT allocations FROM allocation_results WHERE id = :id"),
            {"id": result_id},
        ).scalar_one()
        assert stored == allocations

    def test_expected_total_cost_nullable_when_infeasible(self, fx) -> None:
        """contracts.py: expected_total_cost is None unless solved."""
        result_id = fx.mk_allocation_result(
            status="infeasible",
            expected_total_cost=None,
            stats=None,
            model_hash=None,
            infeasibility={
                "conflicting_groups": ["capacity", "max_concentration"],
                "detail": "no allocation satisfies capacity and concentration together",
                "minimal_relaxation": "raising budget_limit to 9,140.00 restores feasibility",
            },
        )
        row = fx.db.execute(
            text(
                "SELECT expected_total_cost, infeasibility FROM allocation_results"
                " WHERE id = :id"
            ),
            {"id": result_id},
        ).one()
        assert row.expected_total_cost is None
        assert row.infeasibility["conflicting_groups"] == ["capacity", "max_concentration"]

    def test_expected_total_cost_check_rejects_negative(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_allocation_result(expected_total_cost="-1.00")

    def test_supplier_count_check_rejects_negative(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_allocation_result(supplier_count=-1)

    def test_model_hash_stats_pairing_rejects_hash_without_stats(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_allocation_result(stats=None, model_hash="deadbeef")

    def test_model_hash_stats_pairing_rejects_stats_without_hash(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_allocation_result(stats=_DEFAULT_STATS, model_hash=None)

    def test_model_hash_stats_pairing_accepts_both_null(self, fx) -> None:
        """an ERROR result that failed before a model was ever built has
        neither."""
        result_id = fx.mk_allocation_result(
            status="error",
            stats=None,
            model_hash=None,
            expected_total_cost=None,
            error_message="solver process crashed before model construction",
        )
        row = fx.db.execute(
            text("SELECT stats, model_hash, error_message FROM allocation_results WHERE id = :id"),
            {"id": result_id},
        ).one()
        assert row.stats is None
        assert row.model_hash is None
        assert row.error_message == "solver process crashed before model construction"
