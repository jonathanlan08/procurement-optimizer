# Calculation methodology

Landed cost, currency and unit normalization, price breaks, vendor scoring, confidence
bands, and the decimal policy that underpins all of them. Everything here is deterministic
and reproducible without an LLM.

Related: [OPTIMIZATION.md](OPTIMIZATION.md) (allocation) ·
[DOCUMENT_PIPELINE.md](DOCUMENT_PIPELINE.md) (where the numbers come from) ·
[DATABASE.md](DATABASE.md) (how they are stored)

---

## 1. Decimal policy

`backend/src/app/core/money.py` is the single source of truth. Binary floating point is
forbidden for money and quantities; `ruff` even bans a `app.domain.float` import path in
`backend/pyproject.toml`.

- Working precision **34** significant digits, rounding **`ROUND_HALF_EVEN`** (banker's).
- Traps enabled for `InvalidOperation`, `DivisionByZero`, `Overflow` — arithmetic raises
  instead of quietly producing `NaN`/`Infinity`.
- Arithmetic runs at full precision *inside* a formula; each result is quantized **once**
  at its boundary scale. Components are quantized before summing, so displayed components
  sum **exactly** to the displayed total.
- Boundary scales: `MONEY_SCALE` 6 dp, `UNIT_PRICE_SCALE` 8 dp, `RATE_SCALE` 12 dp,
  `QTY_SCALE` 6 dp, `RATIO_SCALE` 6 dp, `DISPLAY_SCALE` 2 dp (presentation only).
- Input finer than a column's scale is a **validation error**, never a silent round
  (`parse_at_scale` → `ScaleExceeded`).
- Money crosses the wire as a **string** (`to_wire` → `format(value, "f")`); it is never a
  JSON number.
- The frontend never parses money with `Number()`/`parseFloat`. `frontend/src/lib/money.ts`
  validates decimal strings with a regex and implements half-even rounding in `BigInt`
  string arithmetic, matching the backend bit for bit.

## 2. Missing is not zero

`backend/src/app/domain/values.py` defines `Quantified` — a `Decimal | None` plus a
`Provenance` — with the invariant `value is None ⟺ provenance is MISSING`, enforced in
`__post_init__`.

`Provenance`: `supplier` (stated on the quote) > `user_input` > `calculated` >
`user_assumption` > `default` > `missing`. A component's reported provenance is the
**weakest** provenance among the inputs actually used
(`backend/src/app/domain/landed_cost/calculator.py::_weakest`).

`Completeness`: `complete` · `assumption_dependent` · `incomplete`.

## 3. Landed cost

Contract: `backend/src/app/domain/landed_cost/contracts.py` (frozen). Implementation:
`backend/src/app/domain/landed_cost/calculator.py` (`LandedCostCalculatorV1`,
`CALCULATION_VERSION = "1.0.0"`). Pure: no database, no clock, no network, no randomness.

```
extended_material_cost = normalized_unit_price × accepted_quantity
allocated_fixed_cost   = tooling + setup + documentation + other_fixed
logistics_cost         = shipping + insurance + packaging + handling
import_cost            = tariff + duty + customs_fees
                         (quoted amounts win; otherwise rate × duty basis,
                          the basis recorded as an explicit labelled assumption)
quality_risk_cost      = quality_risk_rate × extended_material_cost
delay_risk_cost        = delay_risk_per_day × max(0, promised − required lead time)
financing_cost         = extended_material_cost × annual_rate
                         × (baseline_terms_days − payment_terms_days) / 365
total_landed_cost      = Σ of the seven components
effective_unit_cost    = total_landed_cost / accepted_quantity
```

`accepted_quantity == 0` raises `ZeroQuantityError`; `< 0` raises `NegativeQuantityError` —
never a division into infinity.

### The signed component

**Financing is the one component that can be negative.** Payment terms longer than the
organization's baseline (default Net-30, cost of capital default 8 %, both labelled user
assumptions) produce a negative amount, and the formula string is suffixed
`(financing benefit)`. This was blessed explicitly in `docs/planning/00-decisions.md` §2
ruling 2 — a supplier who lets you pay later is genuinely cheaper in cash terms, and hiding
that behind `max(0, …)` would misstate the comparison.

### Duty basis

`DutyBasis.MATERIAL_PLUS_LOGISTICS` (CIF-like) is the default and is computed from the
already-quantized `EXTENDED_MATERIAL` and `LOGISTICS` amounts;
`DutyBasis.MATERIAL_ONLY` (FOB-like) is the alternative. Whenever an import charge is
rate-derived rather than quoted, an `Assumption` row records the basis
(`import.<tariff|duty>.duty_basis`). Recoverable tax is excluded from landed cost by default
(`docs/planning/00-decisions.md` §2 ruling 3).

### Missingness cascades

Additive components (`allocated_fixed`, `logistics`, `import`) drop a missing field from the
sum and record a `MissingInput` — unless the caller sets `assume_missing_costs_zero`, which
substitutes zero **and records an `Assumption`** instead.

Multiplicative/derived components (`extended_material`, `quality_risk`, `delay_risk`,
`financing`) cannot do that: a missing *factor* is not "contributes nothing". Any missing
required input makes the whole component `is_missing=True` (amount reported as `0`,
excluded from provenance aggregation). And because `quality_risk`, `financing`, and the
CIF-like duty basis all consume `extended_material_cost`, a missing unit price cascades into
all of them, each recording its own `MissingInput` — rather than silently multiplying by the
placeholder zero.

### Completeness

```
if missing_inputs:   INCOMPLETE
elif assumptions:    ASSUMPTION_DEPENDENT
else:                COMPLETE
```

### Result payload

Each `LandedCostResult` carries all seven `ComponentResult`s in stable order, and each
component carries: the quantized `amount`, a human-readable `formula` **with the actual
values substituted** (this is what reports print), the raw `inputs` map (`"missing"` where
not stated), `provenance`, `is_assumed`, `is_missing`. The result also carries
`missing_inputs` (each with a `consequence` sentence), `assumptions`, `currency`,
`completeness`, and `calculation_version`.

## 4. Worked example (hand-verified, asserted by tests)

500 pieces at EUR 10.50; tooling EUR 800; shipping EUR 300; insurance EUR 45; tariff 3.5 %
on (material + logistics); quality risk 2 %; no delay; Net-60 against a Net-30 baseline at
8 % annual. FX `1 USD = 0.92 EUR`. RFQ base currency USD.

| Step | Computation | Value |
|---|---|---|
| unit price → USD | `10.50 / 0.92` | `11.41304348` |
| extended material | `11.41304348 × 500` | `5706.521740` |
| allocated fixed | `800 / 0.92` | `869.565217` |
| logistics | `(300 + 45) / 0.92` | `375.000000` |
| import | `0.035 × (5706.521740 + 375.000000)` | `212.853261` |
| quality risk | `0.02 × 5706.521740` | `114.130435` |
| delay risk | `0 × 0` | `0.000000` |
| **financing** | `5706.521740 × 0.08 × (30 − 60)/365` | **`−37.522335`** (benefit) |
| **total** | sum of the seven | **`7240.548318`** |
| **effective unit cost** | `7240.548318 / 500` | **`14.48109664`** |

The planning draft's financing figure (`−37.520000`, total `7240.550653`) was arithmetically
wrong and was corrected by the principal (`docs/planning/00-decisions.md` §3):
`0.08 × (30 − 60)/365 = −0.006575342465…`, `5706.521740 × 0.006575342465… = 37.52233473…` →
`−37.522335` at 6 dp.

These exact strings are asserted twice: in the pure unit test
(`backend/tests/unit/test_landed_cost.py`) and end-to-end through the live HTTP stack and
the database (`backend/tests/integration/test_landed_cost_api.py`). Any change to these
numbers must be a deliberate `calculation_version` bump.

## 5. Price breaks — all-units

`backend/src/app/domain/landed_cost/breaks.py`.

**All-units discount semantics**: the selected tier's `unit_price` applies to the *entire*
accepted quantity, not just to the units above the tier minimum
(`docs/planning/00-decisions.md` §2 ruling 1). Incremental/marginal breaks are
[roadmap](ROADMAP.md).

Intervals are **closed on both ends** — `[min_quantity, max_quantity]` — with
`max_quantity = None` meaning an open-ended top tier. The SPEC's own example table
(1–99 · 100–499 · 500–999 · 1000+) is exactly this shape and is reproduced verbatim in the
demo dataset for Cascade Precision's mounting plate.

Two situations return `tier=None` with a reason string rather than a guess:

- the quantity is below every tier's minimum;
- the quantity falls in a genuine gap between two tiers (upstream data defect) — the reason
  names the bounding tiers.

Nearest-matching to an adjacent tier is deliberately never done: guessing a price break is
exactly the fabrication this codebase forbids everywhere else.

Because tiers depend on quantity, splitting an order changes the applicable tier. The solver
therefore re-selects the tier at the **allocated** quantity and cross-checks that against
CP-SAT's chosen tier — see [OPTIMIZATION.md](OPTIMIZATION.md) §5.

## 6. Currency and unit normalization

### FX

Rate convention for stored `exchange_rates` and `FxRateProvider.get_rate(base, quote)`:
**`1 base = rate × quote`**. Converting an amount stated in the *quote* currency into the
*base* currency therefore **divides** by the rate — the worked example's
`EUR 10.50 / 0.92 = USD 11.41304348`.

The pure function `backend/src/app/domain/fx/normalize.py::normalize_price` deliberately uses
a different, unambiguous convention — **`rate` is "target per source":
`target = source × rate`** — and states so loudly in its docstring. Inverting a stored
base/quote rate into that form is the service layer's job
(`backend/src/app/services/fx_service.py`), because only the service knows which side of the
stored pair is the target for this call.

The shipped provider is `backend/src/app/providers/fx/synthetic.py`
(`SyntheticFxProvider`, source label `synthetic_fixture`): a fixed USD-base table
(EUR 0.92, GBP 0.79, JPY 149.50, CNY 7.24, MXN 17.05, INR 83.12), non-USD pairs triangulated
through USD, same-currency exactly `1`, and an **unknown currency returns `None`, not a
guess**. It is deliberately date-invariant; as-of date selection, manual-override precedence
and `MissingExchangeRateError` are `FxService`'s job over persisted `exchange_rates` rows.

Every rate that participates in a calculation is captured in the result's `inputs_snapshot`
and in the scenario's `fx_snapshot`, with its source and whether it was a manual override.

### Units

`backend/src/app/domain/units/normalize.py`. Canonical unit per dimension: `each` (count),
`kg` (mass), `m` (length). Conversion is
`value × from_unit.to_canonical_factor / to_unit.to_canonical_factor`, run at full
precision and quantized once at `QTY_SCALE`.

Container count units (`pack`, `box`, `tray`, `reel`) have `to_canonical_factor = NULL`
because "1 reel = 5000 each" is a property of the *part*, not of the word "reel". The caller
must supply a `part_factor`; if **both** sides of a conversion lack a universal factor, the
module raises `ConversionAssumptionMissingError` naming both units and recommending a
two-step conversion through `each` rather than misapplying one factor to both sides.

Every applied conversion produces a `conversion_note` spelling out the exact ratio
(`1 lb = 0.45359237 kg`), suffixed to flag it when the factor came from a per-part
assumption rather than the catalogue. Prices are never compared until quantities and units
have compatible normalized meanings.

## 7. Why `COMPLETE` completeness is structurally unreachable in v0.1

This is an honest limitation, not a defect to be talked around.

`FixedCosts` requires a `documentation` cost and `LogisticsCosts` requires a `handling`
cost. **Neither has a source column anywhere in the schema** — `quote_lines` has
`tooling_cost`, `setup_cost`, `packaging_cost`, `shipping_cost`, `insurance_cost`,
`other_fixed_cost`, and the import charges, but no documentation and no handling column
(see [DATA_DICTIONARY.md](DATA_DICTIONARY.md)). `LandedCostService` therefore always passes:

```python
documentation=Quantified.missing(note="documentation cost has no source column on quote_lines"),
handling=Quantified.missing(note="handling cost has no source column on quote_lines"),
```

Consequence: every persisted landed-cost result is either `INCOMPLETE` (default) or
`ASSUMPTION_DEPENDENT` (when the caller sets `assume_missing_costs_zero`, which turns those
two into recorded, human-visible assumptions). `COMPLETE` cannot be produced through the
service path at all — only by calling the pure calculator directly with those fields
supplied, which the unit tests do.

`ScenarioService` accounts for this rather than pretending otherwise: it maps
`incomplete_landed_cost = (completeness is Completeness.INCOMPLETE)`, so
`ASSUMPTION_DEPENDENT` offers are usable by default and only a genuinely unsourced input
requires the `allow_incomplete_offers` override
(`backend/src/app/services/scenario_service.py` module docstring). The demo dataset's
scenarios still set that override, for a *different* honest reason: Baltic Casting's quote
states no payment terms at all, so its financing component — and hence its completeness — is
genuinely `INCOMPLETE`, and the supplier would otherwise silently vanish from the
comparison.

Adding `documentation_cost` and `handling_cost` columns (and the UI to enter them) is on the
[roadmap](ROADMAP.md).

## 8. Vendor scoring

Contract: `backend/src/app/domain/scoring/contracts.py` (frozen,
`SCORING_VERSION = "1.0.0"`). Implementation: `backend/src/app/domain/scoring/scorer.py`
(`ScorerV1`). Pure and reproducible **without an LLM** — same inputs, same scores, bit for
bit, forever within a `scoring_version`.

### Criteria and direction

`Criterion`: `total_landed_cost`, `effective_unit_cost`, `spec_compliance`, `lead_time`,
`capacity`, `moq_flexibility`, `payment_terms`, `quality_history`, `defect_rate`,
`on_time_delivery`, `user_defined`. Direction is explicit per criterion
(`higher_is_better` / `lower_is_better`), with sensible defaults in `DEFAULT_DIRECTIONS`
(cost and lead time lower-is-better; compliance, capacity, payment terms, quality history
and on-time delivery higher-is-better; defect rate lower-is-better).

### Normalization

Min–max across the compared cohort. **Equal values across the cohort score 1.0 for
everyone** — nobody is penalized for a tie. Ties share the smallest rank.

### Missing values → renormalize, never impute

A supplier missing a criterion has that criterion's weight **renormalized away for that
supplier only**; the criterion is listed in `missing_criteria` and `weights_renormalized`
is set. A missing value is never replaced with 0, with the cohort mean, or with the
worst-in-cohort (`docs/planning/00-decisions.md` §2 ruling 5). The frozen `CriterionSpec`
has no `missing_policy` field, so `renormalize` is the only policy; callers wanting
"exclude this supplier" semantics use the `excluded`/`exclusion_reason` fields instead.

There is also a **global defensive renormalization**: if the supplied weights do not sum to
exactly 1, every weight is divided by the raw sum and a note is appended to
`ScoringResult.notes`. A raw sum of exactly zero raises `ZeroTotalWeightError` rather than
dividing into nonsense.

### Zero weights, exclusions, outliers

Zero-weight criteria are still evaluated and displayed; they contribute nothing. Excluded
suppliers appear in the result as excluded-with-reason, unscored (`criterion_scores=()`,
sentinel `rank=0`).

**Outliers are not clipped.** Clipping would hide real price outliers, which is precisely
what this product must not do. Documented as a deliberate v0.1 choice in the contract
docstring.

### Every score carries a reason

`CriterionScore.reason` is a human-auditable sentence
(e.g. "lowest landed cost 7240.55 of cohort 7240.55–9100.00"), surfaced in the comparison
UI's explainability drawer.

### Sample weights are labelled as such

`SAMPLE_WEIGHTS` in the contract — landed cost 0.35, spec compliance 0.25, lead time 0.15,
supplier reliability (on-time delivery) 0.10, payment terms 0.05, MOQ flexibility 0.05,
quality history 0.05 — is the SPEC's own demonstration set. Every entry carries
`is_sample_weight=True`, and `scoring_configurations.is_sample` mirrors that in the
database, so the UI can say "these are sample assumptions, change them" rather than
implying a house view.

## 9. Confidence bands

`backend/src/app/domain/confidence.py` is the single source of truth; the design system
mirrors it.

| Band | Range | Meaning |
|---|---|---|
| `high` | ≥ **0.95** | Accepted automatically, still correctable |
| `medium` | [0.60, 0.95) | Requires review |
| `low` | < **0.60** | **Must be confirmed before use** |

A confidence outside `[0, 1]` raises. Materialization of an extraction run is blocked while
any `low`-band field is unconfirmed — see [DOCUMENT_PIPELINE.md](DOCUMENT_PIPELINE.md).

## 10. What is deliberately not modelled in v0.1

- Incremental (marginal-unit) price breaks — all-units only.
- Scrap/yield adjustments, minimum-order charges as a distinct concept (they fold into
  `other_fixed_cost` with a note).
- Freight is linear in quantity; no break-bulk / container-step freight curves.
- Recoverable tax handling beyond "excluded by default".
- Shared cross-line supplier capacity (v0.1 constrains per quote line).
- A `required_by_date` on RFQ lines: `RfqLine` has no such column, so required lead time
  arrives as a scenario assumption (`LandedCostAssumptions`), documented at
  `backend/src/app/services/landed_cost_service.py`.
