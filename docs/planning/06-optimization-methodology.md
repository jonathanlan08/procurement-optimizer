# 06 — Order-Allocation Optimization Methodology

Status: **DRAFT FOR PRINCIPAL REVIEW**
Implements SPEC §Order-allocation optimization. Solver: OR-Tools CP-SAT (agreed with the principal,
with the determinism changes in `01-architecture.md` §10 D3).

---

## 1. Problem statement

Given an RFQ with lines `L` (each requiring a quantity of a part), and confirmed quotes from suppliers
`S` (each quote line offering a price schedule, MOQ, capacity, lead time, compliance), choose how many
units of each line to buy from each supplier so that **total landed cost is minimized** subject to
demand, capacity, MOQ, tier, concentration, supplier-count, budget, deadline, lock and exclusion
constraints — and report honestly whether the answer is proven optimal.

Problem size in this product: `|S| ≤ ~20`, `|L| ≤ ~200`, tiers per pair `≤ ~6`. That is small. CP-SAT
solves it in milliseconds to low seconds; the engineering risk is **not** performance, it is
determinism, correct piecewise modelling, and honest status reporting.

---

## 2. Sets, parameters, and pre-solve filtering

| Symbol | Meaning |
|---|---|
| `L` | RFQ lines requiring allocation, `d_l` = required quantity (canonical units, integer-scaled) |
| `S` | suppliers with a confirmed, non-excluded quote |
| `P` | eligible pairs `(s,l)` — supplier `s` quoted line `l` **and** passed pre-filtering |
| `T(s,l)` | ordered price-break tiers; tier `t` has `[lo_t, hi_t]` and unit cost `c_t` |
| `cap_{s,l}` | supplier capacity for that line (units) |
| `capS_s` | supplier-level capacity across lines (optional) |
| `moq_{s,l}` | minimum order quantity |
| `setup_{s,l}` | tooling + setup, charged if the pair is used |
| `fixed_s` | supplier-level fixed cost, charged if the supplier is used |
| `var_{s,l}` | per-unit landed cost **excluding** material (freight/unit, duty/unit, risk/unit …) |
| `K` | max supplier count |
| `ρ` | max concentration (fraction of total cost, see §4.6) |
| `B` | budget cap |

**Pre-solve filtering** removes candidates for reasons that are *explanations*, not constraints —
each removal is recorded with a reason string and surfaces in `rejected_alternatives`:

- supplier excluded by the user, or `rfq_suppliers.status = 'excluded'`;
- quote not `confirmed`, superseded, or expired at the scenario `as_of_date`;
- quote line has unconfirmed low-confidence critical fields (SPEC: uncertain values must not silently
  affect recommendations) — configurable to `warn_and_include` with an explicit override;
- part match not confirmed for a non-exact match;
- specification compliance below the RFQ's minimum;
- lead time > days available before `required_by_date` (when `enforce_deadline=true`);
- landed cost incomputable (missing unit conversion, missing FX rate, `MISSING` required cost).

Filtering before the model keeps the MILP small and — more importantly — turns "no supplier can do
this" into a sentence a human can read, rather than a bare `INFEASIBLE`.

---

## 3. Decision variables

All integers; quantities in canonical units scaled by `QTY_MULT` (default 1 for integral parts, `10^3`
for parts measured in kg/m — see §7).

| Variable | Domain | Meaning |
|---|---|---|
| `x[s,l]` | `0 … cap_{s,l}` | units of line `l` from supplier `s` |
| `u[s,l]` | bool | pair used (`x[s,l] > 0`) |
| `y[s]` | bool | supplier used anywhere |
| `z[s,l,t]` | bool | tier `t` selected for the pair |
| `q[s,l,t]` | `0 … hi_t` | units priced at tier `t` |

Linking:
```
x[s,l] = Σ_t q[s,l,t]
Σ_t z[s,l,t] = u[s,l]                       # exactly one tier iff the pair is used
lo_t · z[s,l,t] ≤ q[s,l,t] ≤ hi_t · z[s,l,t]
x[s,l] ≤ cap_{s,l} · u[s,l]                 # u = 1 whenever x > 0
u[s,l] ≤ y[s]
```

**Why this models all-units discounts exactly.** Because exactly one `z[s,l,t]` is 1 for a used pair,
all other `q[s,l,t]` are forced to 0, so `x = q_t*` and the material cost `Σ_t c_t · q[s,l,t]` equals
`c_{t*} · x`. The formulation is fully linear despite price and quantity both being decisions — this
is the standard trick and it is exact, not an approximation. (Incremental discounts would need a
different, also-linear formulation; they are out of scope, see §9.)

`q[s,l,t]` upper bound uses `min(hi_t, cap_{s,l}, d_l)` to keep the domains tight.

---

## 4. Constraints

### 4.1 Demand
```
Σ_{s : (s,l) ∈ P} x[s,l] = d_l                for all l
```
Equality by default. `allow_oversupply=true` relaxes to `≥ d_l` (needed when `moq_policy=round_up`
forces a supplier above the requirement); the surplus is costed and reported as `oversupply_units`.
If oversupply is disallowed and MOQs make equality impossible, that is a genuine infeasibility and is
explained as such.

### 4.2 Capacity
```
x[s,l] ≤ cap_{s,l}                            (also encoded in the variable domain)
Σ_l x[s,l] ≤ capS_s                           when a supplier-level cap exists
```

### 4.3 MOQ
```
x[s,l] ≥ moq_{s,l} · u[s,l]
```
Big-M free: the upper linkage is the capacity bound, so there is no numerically fragile `M`.

### 4.4 Price-break tiers
Encoded in §3. Additionally, tiers are validated non-overlapping and ascending before model build; a
gap makes the intermediate quantities infeasible **for that pair only**, which is correct and is
explained (`"supplier X has no price for quantities 100–199"`).

### 4.5 Max supplier count
```
Σ_s y[s] ≤ K
```

### 4.6 Max concentration
Two bases; **cost basis is the default** because "no more than 60 % of spend with one supplier" is what
procurement policy actually means:
```
cost basis:  cost_s ≤ round(ρ · total_cost_var)     with total_cost_var an integer variable
             total_cost_var = Σ_s cost_s
qty basis:   Σ_l x[s,l] ≤ floor(ρ · Σ_l d_l)
```
The cost-basis form is linear because both sides are linear integer expressions. It requires
`total_cost_var` to exist as a variable rather than only as an objective expression — cheap, and it
also gives the budget constraint (§4.8) something to bind to.

### 4.7 Locked allocations
```
x[s,l] = locked_{s,l}
```
Locks are validated against capacity/MOQ/tier availability **before** solving; a lock that is itself
impossible produces a targeted error rather than a bare infeasibility.

### 4.8 Budget
```
total_cost_var ≤ ⌊B · SCALE⌋
```

### 4.9 Deadline and compliance
Handled by pre-solve filtering (§2) rather than as constraints, so that the reason a supplier is
absent is a sentence, not a hidden row. When `enforce_deadline=false`, late suppliers are admitted and
their lateness feeds `delay_risk_cost` in the objective instead.

### 4.10 Single-supplier mode
`K = 1`. Reported separately as a comparison point even when split mode is requested (§8).

---

## 5. Objective

```
minimize   Σ_{(s,l)∈P} Σ_t c_t · q[s,l,t]                 # material at the selected tier
         + Σ_{(s,l)∈P} var_{s,l} · x[s,l]                 # per-unit landed adders
         + Σ_{(s,l)∈P} setup_{s,l} · u[s,l]               # per-pair fixed
         + Σ_s fixed_s · y[s]                             # per-supplier fixed
         (+ ε · tiebreak_term)                            # deterministic tie-break, §6
```

All coefficients are integers in **scaled cost units** (§7). `total_cost_var` is constrained equal to
the first four terms so that budget and concentration can reference it.

Strategy variants (SPEC §Scenario comparison) change the objective or add constraints, never the
model's structure:

| Strategy | Implementation |
|---|---|
| `lowest_total_landed_cost` | the objective above |
| `lowest_quoted_unit_price` | material term only; other costs reported but not optimized |
| `fastest_delivery` | lexicographic: minimize `max lead time` first, then cost (two solves, second with the first's optimum fixed as a constraint) |
| `lowest_supply_risk` | minimize cost subject to a tightened `ρ` and a minimum supplier count `Σ y ≥ 2` |
| `balanced` | cost objective + penalty terms derived from the scoring weights, with the weight-to-cost conversion **explicitly documented and shown** (this is the one place scores and money mix, and it must be transparent) |
| `user_configured` | user-set constraints, cost objective |

---

## 6. Determinism

CP-SAT is **not deterministic by default**. Required parameters:

```python
p = solver.parameters
p.num_search_workers      = 1        # multi-worker returns different equally-optimal solutions
p.random_seed             = 0
p.max_deterministic_time  = 30.0     # deterministic budget, NOT wall clock
p.max_time_in_seconds     = 120.0    # safety net only; hitting it reports `solver_timeout`
p.log_search_progress     = False
p.cp_model_presolve       = True     # deterministic given fixed seed and single worker
```

Plus, at model-build time:
- **Sorted input ordering.** Suppliers by `(code, id)`, lines by `(line_number, id)`, tiers by
  `min_quantity`. Variable creation order is therefore a pure function of the data.
- **Stable variable names** (`x_{supplier_code}_{line_number}`) so exported models diff cleanly.
- **`model_hash`** = SHA-256 of the serialized `CpModel` proto, stored on `allocation_results`. Two
  runs that produce the same hash must produce the same allocation; this is asserted in tests and is
  the fastest way to detect an accidental nondeterminism regression.
- **Tie-break term.** Among equal-cost optima, prefer (a) fewer suppliers, (b) lexicographically
  smallest supplier code sequence. Implemented as `ε · (Σ_s 2^rank(s) · y[s])`-style penalty with `ε`
  chosen strictly below the smallest possible cost difference (1 scaled unit) divided by the maximum
  penalty magnitude — with a pre-solve assertion that the bound holds. If the bound cannot be
  guaranteed for a given instance, the tie-break is **disabled** and the result is flagged
  `tiebreak_disabled` rather than silently risking a wrong optimum. Correctness beats tidiness.

**Consequence for CI:** a slow runner changes wall-clock but not deterministic time, so the same input
yields the same status and allocation on a MacBook and on a GitHub runner. Using
`max_time_in_seconds` as the primary limit would have made the test suite flaky in a way that looks
like a solver bug.

---

## 7. Integer scaling of Decimal costs

CP-SAT accepts only integer coefficients. Policy:

```
COST_SCALE = 10_000            # 1 scaled unit = 0.0001 currency units
scaled(x)  = int(x.quantize(Decimal("0.0001"), ROUND_HALF_EVEN) * COST_SCALE)
QTY_MULT   = 1 for integral units; 1_000 for kg/m (3 dp of quantity)
```

Why `10^4` and not `10^6` (the principal's provisional value): headroom. The largest objective term is
`Σ c_t · q`, bounded by `max_unit_cost × total_qty × COST_SCALE`. With `10^6`, a 10 M-unit order of a
$1 000 part reaches `10^16` — still inside int64 but within two orders of magnitude of the limit, and
CP-SAT's internal products can exceed it. With `10^4` there are four more decimal orders of headroom
for the same worst case. A tenth of a *cent* of resolution is far below any decision-relevant
difference in this domain.

Guards (all mandatory, all tested):
1. **Pre-solve bound check.** Compute the maximum possible objective value; if it exceeds `2^62`,
   raise `ModelTooLargeError` with the offending term rather than letting CP-SAT overflow.
2. **Rounding direction.** Costs are rounded half-even at 4 dp. Maximum absolute error per term is
   `5×10^-5`; total error over `n` terms is bounded by `n × 5×10^-5` and is reported in the result as
   `max_scaling_error`.
3. **The reported cost is never the solver's objective.** After a solution is returned, the allocation
   is re-costed through the *exact Decimal* landed-cost calculator of `05-calculation-methodology.md`,
   and that number is what is stored, displayed, and exported. `objective_source` is
   `"exact_decimal_recomputation"` in the API response.
4. **Optimality caveat.** Because scaling is a rounding, a solution proven optimal in scaled space
   could in principle be beaten by `< max_scaling_error` in exact space. This is disclosed in
   `docs/OPTIMIZATION.md` in one sentence: *"Optimality is proven with respect to costs rounded to
   0.0001 currency units; any unconsidered alternative is at most `max_scaling_error` cheaper."* This
   is the honest statement and it costs nothing to make.

---

## 8. Solver-status mapping and rejected alternatives

| CP-SAT status | Reported `solver_status` | Message |
|---|---|---|
| `OPTIMAL` | `optimal` | "Proven lowest total landed cost under the stated constraints and rounding." |
| `FEASIBLE` | `feasible` | "A valid allocation was found but optimality was not proven within the deterministic search budget." |
| `INFEASIBLE` | `infeasible` | plus the explanation of §8.1 |
| `MODEL_INVALID` | `solver_error` | internal; audited; never shown as a business outcome |
| `UNKNOWN` + deterministic limit hit | `solver_timeout` | "Search budget exhausted before any solution was found." |
| exception | `solver_error` | error code + `request_id`, no stack trace |

**"Never label everything optimal"** is enforced by construction: `optimal` is written only when
`status == OPTIMAL`, and there is a unit test that feeds a model with a forced small budget and asserts
`feasible`, not `optimal`.

### 8.1 Infeasibility explanation

Three layers, cheapest first:

1. **Pre-solve arithmetic checks**, each producing a specific sentence:
   - `Σ_s cap_{s,l} < d_l` → "Total capacity for line 4 (BRK-100) is 900 units; 1,200 are required."
   - `min Σ moq > d_l` under `K` → "The 2 permitted suppliers have minimum order quantities of 800 and 600; only 1,200 units are required."
   - `B < ` lower bound on cost → "Budget of $150,000 is below the cheapest possible allocation of $184,320."
   - locked allocations exceeding capacity / violating MOQ / summing past demand.
   - concentration cap × supplier count < 1 → "A 40 % concentration cap requires at least 3 suppliers; only 2 are eligible."
2. **Minimal conflicting-constraint core via CP-SAT assumption literals.** Each *relaxable constraint
   group* (`budget`, `max_supplier_count`, `max_concentration`, `moq`, `capacity`, `deadline`,
   `locked_allocations`, `exclusions`) is gated by a boolean assumption literal. On `INFEASIBLE`,
   `solver.SufficientAssumptionsForInfeasibility()` returns a subset of those literals — a genuine,
   solver-derived minimal-ish core, not a guess. Reported as `conflicting_constraint_groups`.
3. **Minimal relaxation search.** For each group in the core, re-solve with that group alone relaxed
   (deterministically ordered) and report which single relaxation restores feasibility, including the
   numeric threshold where possible ("raising max supplier count from 2 to 3 restores feasibility";
   "a budget of $184,320 or more is required"). Bounded to `|core| + 1` extra solves.

Every infeasible result still writes an `allocation_results` row and an audit event — an infeasible
answer is a real analytical result, not an error to be swallowed.

### 8.2 Rejected alternatives

SPEC requires explaining "why alternatives were rejected". Deterministic, cheap approach:

1. **Every single-supplier option**: for each eligible supplier, solve with `y[s'] = 0 ∀ s' ≠ s`.
   Report cost or infeasibility reason. This directly answers the question a CFO asks first.
2. **The next-best split**: re-solve with a no-good cut excluding the recommended allocation vector;
   report the delta. One extra solve.
3. **Constraint shadow information**: which constraints are binding (`binding_constraints`) — derived
   by checking slack on each constraint at the solution, not from duals (CP-SAT has none).
4. Alternatives are sorted by cost ascending, with infeasible ones last and their reason attached.

This is `|S| + 1` additional solves of a small model — milliseconds — and it converts the optimizer
from a black box into an explanation.

---

## 9. Assumptions, limits, and gaps

1. **All-units discounts assumed** (see `05-calculation-methodology.md` §6). Incremental/marginal
   discounts are a different formulation; if the principal wants them, it is a `q[s,l,t]` chain with
   ordering constraints — roughly a day of work and a new set of boundary tests.
2. **Deterministic demand.** No safety stock, no forecast uncertainty, no multi-period planning.
3. **Costs are linear in quantity within a tier.** Volume-dependent freight (a second container at
   1 200 units) is not modelled; it would need step-fixed freight variables. Flagged as a realistic
   gap the demo should mention rather than hide.
4. **One-shot award.** No lot-splitting over time, no delivery scheduling.
5. **Concentration on cost basis by default** — confirm with the principal; qty basis is a one-line
   switch but changes results.
6. **Supplier-level capacity** is optional and, when absent, only per-line capacity binds. Real
   suppliers have shared capacity across parts; the field exists, the seed data should exercise it.
7. **Tie-break disabling** (§6) means that in rare instances two runs on *different data* could pick
   different equal-cost optima; on *identical* data determinism is guaranteed by seed + single worker.
8. **Scaling and optimality caveat** (§7.4) must appear in the exported report, not only in the docs.
9. **The `balanced` strategy converts scores into cost penalties.** That conversion is inherently
   arbitrary; it must be displayed as "1 score point = $X" and be user-editable, or it becomes exactly
   the kind of opaque number this product exists to eliminate. I would rather ship `balanced` as
   "cost objective + hard constraints derived from scores" than as a weighted blend — flagged for a
   principal decision.

---

## 10. Required test matrix (mirrored in `08-test-strategy.md`)

| # | Case | Expected |
|---|---|---|
| 1 | single supplier, single line, no constraints | `optimal`, all quantity to that supplier |
| 2 | N suppliers, cheapest wins | `optimal`, exact expected allocation |
| 3 | capacity forces a split | `optimal`, split matches hand calculation |
| 4 | MOQ makes the cheapest supplier unusable at small volume | second-cheapest chosen, reason recorded |
| 5 | crossing a price break changes the winner | tier recomputed, winner flips at the documented boundary |
| 6 | `max_concentration = 0.5` | no supplier exceeds 50 % of cost |
| 7 | `max_supplier_count = 1` vs `= 3` | different allocations, both `optimal` |
| 8 | budget just below optimum | `infeasible` + budget explanation with the exact shortfall |
| 9 | budget just above optimum | `optimal` |
| 10 | locked allocation consistent | respected |
| 11 | locked allocation impossible | targeted pre-solve error, not bare `infeasible` |
| 12 | all suppliers excluded | `infeasible` + "no eligible suppliers" with per-supplier reasons |
| 13 | capacity shortfall | `infeasible` + capacity explanation |
| 14 | forced tiny deterministic budget | `feasible` or `solver_timeout`, **never** `optimal` |
| 15 | solver exception injected | `solver_error`, audited, row persisted |
| 16 | same input twice | identical `model_hash`, identical allocation |
| 17 | input reordered (suppliers shuffled) | identical `model_hash`, identical allocation |
| 18 | objective overflow attempt | `ModelTooLargeError` before solving |
| 19 | oversupply from `moq_policy=round_up` | surplus reported, result `assumption_dependent` |
| 20 | rejected-alternatives list | contains every single-supplier option with cost or reason |
