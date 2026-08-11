# Order-allocation optimization

A CP-SAT model that decides how much of each RFQ line to buy from which supplier — and,
just as importantly, is honest about how confident it is in the answer.

Contract: `backend/src/app/domain/optimization/contracts.py` (frozen,
`OPTIMIZATION_VERSION = "1.0.0"`). Implementation:
`backend/src/app/domain/optimization/solver.py`. Assembly and persistence:
`backend/src/app/services/scenario_service.py`.

Related: [METHODOLOGY.md](METHODOLOGY.md) (where landed unit costs come from) ·
[DATABASE.md](DATABASE.md) (how scenarios are snapshotted)

---

## 1. Inputs

| Type | Fields |
|---|---|
| `DemandLine` | `rfq_line_id`, `part_label`, `required_quantity` (integer units after normalization) |
| `OfferTier` | `min_quantity`, `max_quantity` (`None` = open-ended), `landed_unit_cost` (exact `Decimal`) |
| `Offer` | one supplier's quote line for one demand line: `tiers` (≥ 1, sorted), `moq`, `capacity`, `fixed_cost` (charged once if any allocation > 0), `incomplete_landed_cost` |
| `AllocationConstraints` | `max_supplier_count`, `max_concentration`, `budget_limit`, `locked_allocations`, `excluded_supplier_ids`, `allow_incomplete_offers` |
| `EligibilityExclusion` | pre-solve exclusions with a human reason (lead time, spec, user, incomplete) |

Each tier's `landed_unit_cost` is a **fully landed** per-unit cost produced by the
landed-cost engine, not a raw quoted price. That is what makes the model's objective the
right one: it minimizes total landed cost, not sticker price.

## 2. The model

Variables, created in canonical order:

- `alloc[offer]` — integer units, `0 … min(demand, capacity)`;
- `used[offer]` — boolean, tied to `alloc > 0` by
  `alloc ≤ upper·used` and `alloc ≥ used`;
- `tier[offer, k]` — boolean per price-break tier, with `Σ_k tier = used` (exactly one tier
  per used offer);
- `offer_cost[offer]` — integer scaled cost;
- `supplier_used[s]`, `supplier_spend[s]`, and a global `total_cost`.

Constraints:

| Group | Encoding |
|---|---|
| **demand** (never relaxable) | `Σ_offers alloc = required_quantity` per line |
| `moq` | `alloc ≥ moq` enforced only if the offer is used |
| `capacity` | `alloc ≤ capacity` (per quote line — cross-line shared capacity is roadmap) |
| `supplier_count` | `Σ supplier_used ≤ k` |
| `concentration` | **cost basis**: `spend_s · 10^6 ≤ cap_num · total_cost`, `cap_num = round(ρ · 10^6)` |
| `budget` | `total_cost ≤ scaled(budget_limit)` |
| `locks` | `alloc = locked_quantity` |

Objective: `minimize total_cost`, where
`total_cost = Σ offer_cost + Σ fixed_cost·used`.

**Tier linearisation.** Rather than auxiliary "quantity priced at tier *t*" variables, the
model ties `offer_cost` directly to `alloc` per tier with a conditional equality:
`offer_cost == scaled_unit_cost_k · alloc` enforced if `tier[offer,k]`. This is exactly
linear (`alloc` is never split across tiers, which is what all-units discounts mean) and
big-M free, because the enforcement is conditional rather than a relaxed bound. Tiers whose
`min_quantity` exceeds the offer's upper bound are pinned to `0` as structurally
unreachable.

**Money scaling.** `MONEY_INT_SCALE = 10_000` (10⁴) converts `Decimal` money into int64 with
headroom. `_check_overflow` computes the worst-case scaled objective *before any variable is
created* and raises `ScalingOverflowError` if any coefficient or the bound exceeds
`int64 / 4`. The concentration cap uses its own 10⁶ denominator for 6-dp fraction
resolution.

## 3. Determinism

The recipe, ratified in `docs/planning/00-decisions.md` §1.1 and implemented in
`AllocationSolver._make_solver` / `_canonical_problem_dict`:

- `num_search_workers = 1` — no parallel search nondeterminism;
- `random_seed = 0`;
- `max_deterministic_time = 30.0` (plus a `max_time_in_seconds = 120.0` wall-clock backstop);
- **canonical input ordering** — lines sorted by `str(rfq_line_id)`, offers by
  `(str(rfq_line_id), str(supplier_id), str(quote_line_id))`, tiers by `min_quantity`, locks
  and exclusions sorted too. (The methodology's `(code, id)` / `(line_number, id)` sort keys
  do not exist on the frozen dataclasses, so the only always-present stable keys are used
  instead; a reordering test asserts the result is unchanged.)
- **`model_hash`** — SHA-256 over canonical JSON of the *input* (sorted keys, ids as
  strings, decimals via `to_wire`), not over the built CpModel proto. Cheaper, and it means a
  pre-solve `INFEASIBLE` verdict — reached without ever calling the solver — still carries a
  meaningful, reproducible hash.

Repeat solves and permuted-input solves of the same problem produce the same model and the
same solution, asserted by the determinism tests in
`backend/tests/unit/test_allocation_solver.py`.

**One honest gap:** the methodology's epsilon tie-break term is **deferred**. CP-SAT with a
fixed seed, a single worker, and canonical variable-creation order is deterministic for a
fixed model, so today's results are reproducible. What is not guaranteed is *which* of
several **exactly**-tied optima a future code change would surface. This is flagged in the
solver's module docstring rather than silently added or silently skipped, and is on the
[roadmap](ROADMAP.md).

## 4. Honest statuses

`AllocationStatus` has four members and the mapping is 1:1 — **`FEASIBLE` is never presented
as `OPTIMAL`**:

| Status | Meaning |
|---|---|
| `optimal` | CP-SAT returned `OPTIMAL` — proven optimal within the deterministic budget |
| `feasible` | CP-SAT returned `FEASIBLE` — a valid allocation, optimality **not** proven |
| `infeasible` | no allocation satisfies the constraints |
| `error` | solver failure or an unexpected status; the message is preserved, never hidden |

`SolverStats` records the raw CP-SAT status name verbatim (`status_raw`), the deterministic
time, the `model_hash`, and the variable/constraint counts. A pre-solve infeasibility is
labelled `PRESOLVE_INFEASIBLE`. Any exception during model build or solve is caught once and
returned as `status=ERROR` with `error_message=str(exc)` — never a bare 500, never a silent
empty result.

The frontend allocation panel (`frontend/src/features/comparison/AllocationPanel.tsx`)
renders a distinct banner per status; "feasible" says so in words.

## 5. The reported cost is recomputed exactly, never taken from the solver

`_extract_allocations` never reads the solver's scaled objective for the reported number.
For each allocated offer it:

1. re-selects the price-break tier at the **allocated** quantity using the same
   `select_price_break` the landed-cost engine uses;
2. reads which tier CP-SAT actually chose;
3. **raises `ConsistencyError` if the two disagree** — on the tier's `min_quantity` or its
   unit price — or if the allocated quantity cannot be priced at all, or if no tier boolean
   is set;
4. computes `line_cost = quantize_money(tier.landed_unit_cost × quantity)` in exact
   `Decimal`, adds each used offer's `fixed_cost` once, and quantizes the total.

`ConsistencyError` is a `RuntimeError` and is deliberately never swallowed: it means the
tier-linking constraints and the exact tier selection have diverged, which is a modelling
bug, not a user error. `_unscale_money` exists only for human-readable sentences and is
documented as never authoritative.

## 6. Infeasibility that explains itself

### Pre-solve explanations

Before building a model at all, the solver checks — in order — and returns immediately with
a specific explanation:

1. **no supply for a line** — names each uncovered line, its required quantity, and the
   reasons its would-be suppliers dropped out; attributes the conflict to `moq` when *all*
   the reasons were MOQ-related, otherwise `capacity`;
2. **capacity shortfall** — "total capacity for line X is N units; M are required", with a
   relaxation hint;
3. **lock conflicts** — a lock that exceeds an offer's ceiling, falls below its MOQ, prices
   into no tier, points at an ineligible supplier or absent line, or whose per-line sum
   exceeds demand;
4. **structural concentration conflict** — "a 0.4 concentration cap requires at least 3
   suppliers; only 2 are available", naming whether `max_supplier_count` or the eligible
   supplier count is the limiter;
5. **budget below the floor** — the cheapest conceivable allocation is computed exactly and
   compared to the budget: "budget of X is below the cheapest possible allocation of Y",
   with "raising `budget_limit` to at least Y restores feasibility".

### CP-SAT assumption cores

For anything the pre-solve checks do not catch, the main solve runs in **assumption mode**:
all six relaxable constraint groups — `moq`, `capacity`, `supplier_count`, `concentration`,
`budget`, `locks` — are gated by boolean literals registered with `AddAssumptions`, always
present even when a problem does not use that constraint, so
`SufficientAssumptionsForInfeasibility()` has a uniform, canonical set to reason over.
`demand` and the tier/linking structure are never gated: they are not policy a user could
choose to relax.

The returned core is *sufficient*, not minimal (in practice it includes groups with no
constraints wired in that instance), so `_minimize_core` performs **deletion-based
minimization**: for each candidate group, rebuild the model with every other kept group
enforced and everything else relaxed; if it is still infeasible, that candidate was not
needed and is dropped.

`_minimal_relaxation` then re-solves with each surviving group relaxed in turn and reports a
concrete number where one exists: "a budget of at least 9 140.00 restores feasibility",
"raising `max_supplier_count` to at least 3 restores feasibility", otherwise
"relaxing 'capacity' restores feasibility".

## 7. Binding constraints and rejected alternatives

On a solved result the solver reports which constraints are **binding**: an offer allocated
at exactly its capacity; an allocation using exactly `max_supplier_count` suppliers; a total
within one scaled unit of the budget; a supplier's spend at or within one scaled unit of the
concentration cap.

For split allocations it also computes **one** rejected alternative — a second deterministic
solve with `max_supplier_count = 1` — reported as
"a single-supplier allocation would cost X (+Δ vs. the recommended split)", or "no feasible
single-supplier alternative". This is deliberately scoped to the single-supplier comparison
rather than the methodology's full every-supplier sweep.

## 8. Pre-solve eligibility filtering

`_filter_offers` drops, each with a human-readable reason recorded:

- offers pre-excluded upstream. In this build `ScenarioService._gather_offer_contexts`
  produces exactly one kind of `EligibilityExclusion` — suppliers excluded from the RFQ
  (`rfq_suppliers.excluded_at IS NOT NULL`), reason "<supplier> is excluded from this RFQ" —
  plus a plain skip for quotes in `rejected` status. The lead-time eligibility filter of
  `docs/planning/00-decisions.md` §4 #12 (drop suppliers whose lead time misses the
  required-by date) is **not implemented**: `rfq_lines` has no `required_by_date` column to
  filter against. Lead time still influences the outcome through the `lead_time` scoring
  criterion. See [ROADMAP.md](ROADMAP.md);
- offers from suppliers excluded by the scenario's own `excluded_supplier_ids`;
- offers whose landed cost is `INCOMPLETE`, unless `allow_incomplete_offers` is set (see
  [METHODOLOGY.md](METHODOLOGY.md) §7 for why the gate is `INCOMPLETE` and not
  "not `COMPLETE`");
- defensively, offers referencing a demand line not in the problem.

`_partition_live_and_dead` then removes offers with no usable capacity, or whose MOQ exceeds
the units actually available to them, again with a sentence each. Those sentences are what
the infeasibility explanation quotes back to the user.

## 9. Scenarios: snapshot, solve, persist, rerun

`ScenarioService.create_and_run` does the whole thing in **one request and one
transaction**: resolve landed costs → snapshot → score → solve → persist
`ComparisonScenario` + `ScenarioResult` + `AllocationResultRecord`.

That is a documented deviation from `docs/planning/03-api-contract.md` §4.16, which
describes a two-step async design (`POST …/comparison-scenarios` scores, `POST …/optimize`
allocates). Two reasons, both recorded in the service's module docstring: there is no job
queue anywhere in this build, and the frozen persistence shape has **no state for "scored but
not yet allocated"** — `ComparisonScenario.state` is one machine
(`draft → running → complete|failed`) and both result tables carry
`UNIQUE (organization_id, scenario_id)`.

`POST …/{id}/optimize` is still mounted and is **idempotent**: it returns the allocation half
of the already-solved scenario. There is no "solve again in place" operation, because
scenario results are immutable.

**Reproducibility.** Five granular snapshots are stored on every scenario:
`constraints_snapshot`, `assumptions_snapshot`, `fx_snapshot`, `quote_snapshot_refs`,
`weights_snapshot`. `POST …/{id}/clone` (the contract's route name for what the service calls
`rerun`) creates a **new** scenario from the original's snapshots — reusing
`quote_snapshot_refs` byte-for-byte rather than re-resolving "latest" landed costs — and
performs a fresh solve. Because the scorer and solver are pure functions of their inputs, an
unchanged rerun reproduces identical scores, an identical allocation, and the identical
`model_hash`. History is never mutated; every rerun gets a new id, and
`scenario.rerun` is audited.

**Quote eligibility** for a scenario is "not superseded, not rejected" — deliberately *not*
"confirmed". `QuoteStatus.CONFIRMED` is unreachable through any route in this build's
manual-entry quote pipeline, so gating on it would make every scenario empty. The deviation
is documented in `backend/src/app/services/scenario_service.py`.

## 10. What the demo dataset proves

The seeded scenarios (`backend/src/app/seed/demo_dataset.py`) are engineered to exercise the
honest paths, and asserted by `backend/tests/integration/test_demo_dataset.py`:

- **"Enclosure Pilot — Lowest Landed Cost"** — a feasible run whose ranking puts Cascade
  Precision first even though Shenzhen Precision has the lowest raw unit price;
- **"Enclosure Pilot — Budget Ceiling (Infeasible Demo)"** — `budget_limit = 500`,
  deliberately infeasible, persisted with its explanation rather than hidden;
- **"Enclosure Pilot — Capacity-Constrained Split"** — every quote's per-line production
  capacity (250) is below the RFQ line's required quantity (500), so the optimum is forced
  into a ≥ 2-supplier split.
