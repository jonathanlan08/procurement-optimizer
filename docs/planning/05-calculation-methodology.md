# 05 — Calculation Methodology

Status: **DRAFT FOR PRINCIPAL REVIEW**
Implements SPEC §Landed-cost engine, §Vendor comparison engine, §9 currency, §10 units, §11 price
breaks. Everything here is **pure**: `app/domain/**` has no database, no clock, no network.

---

## 1. Decimal policy

```python
# app/core/money.py  — PRINCIPAL-OWNED
from decimal import Decimal, Context, ROUND_HALF_EVEN, localcontext

CALC_PRECISION   = 34                       # working precision, never a storage scale
MONEY_SCALE      = Decimal("0.000001")      # 6 dp  -> NUMERIC(18,6)
UNIT_PRICE_SCALE = Decimal("0.00000001")    # 8 dp  -> NUMERIC(18,8)
RATE_SCALE       = Decimal("0.000000000001")# 12 dp -> NUMERIC(24,12)
QTY_SCALE        = Decimal("0.000001")      # 6 dp
RATIO_SCALE      = Decimal("0.000001")      # 6 dp
DISPLAY_SCALE    = Decimal("0.01")          # presentation only
ROUNDING         = ROUND_HALF_EVEN
```

Rules:

1. **Working precision 34, banker's rounding.** `ROUND_HALF_EVEN` avoids the systematic upward bias of
   `ROUND_HALF_UP` across thousands of line items, and matches IEEE-754 decimal and most accounting
   systems. Documented in the methodology section of every report.
2. **Quantize at boundaries, not mid-expression.** Inside a single component formula, arithmetic runs
   at full precision; the component result is quantized once.
3. **Quantize each component, then sum.** The invariant we guarantee is:
   `sum(displayed components) == displayed total`, exactly. The alternative (sum at full precision,
   quantize the total) produces reports whose columns do not add up, which destroys CFO trust faster
   than a sixth-decimal-place difference ever could. The residual is bounded by
   `n_components × 5e-7` and is stated in the methodology.
4. **`effective_unit_cost = total_landed_cost / accepted_quantity`** is quantized at
   `UNIT_PRICE_SCALE` (8 dp), and `effective_unit_cost × qty` is explicitly **not** guaranteed to
   re-equal the total. Reports show the total as authoritative and the unit cost as derived.
5. **No floats, anywhere.** `float(` is banned in `app/domain` by lint; SQLAlchemy columns are
   `Numeric(asdecimal=True)`; JSON serialization uses strings (`03-api-contract.md` §1.2); JSON
   *parsing* of decimals goes through `Decimal(str)`, never `float`.
6. **Division guards.** `accepted_quantity == 0` ⇒ `ZeroQuantityError`, never `Infinity`/`NaN`.
   `InvalidOperation`, `DivisionByZero`, `Overflow` are trapped (not silently flagged) by setting the
   decimal context traps explicitly.
7. Input strings are parsed with `Decimal(str_value)`; scale beyond the column is a `422` at the API
   boundary, never a silent round (see `03-api-contract.md` §5).

---

## 2. Value semantics: PRESENT / MISSING / ASSUMED

The single most important modelling decision in this document.

```python
# app/domain/values.py
class Provenance(StrEnum):
    SUPPLIER      = "supplier"        # stated on the quote
    USER_INPUT    = "user_input"      # typed by a human
    USER_ASSUMPTION = "user_assumption"  # a scenario assumption (e.g. quality risk 2%)
    CALCULATED    = "calculated"
    DEFAULT       = "default"         # system default, always disclosed
    MISSING       = "missing"         # not stated; NOT zero

@dataclass(frozen=True, slots=True)
class Quantified:
    value: Decimal | None
    provenance: Provenance
    note: str | None = None
    @property
    def is_missing(self) -> bool: return self.provenance is Provenance.MISSING
```

- A missing cost is **never** silently treated as zero. To include a quote in a comparison the user
  must either supply the value or opt in to an explicit assumption
  (`assume_missing_costs_zero=true`), which is recorded in `assumptions` and downgrades the result.
- Result completeness (stored on `landed_cost_results` and `scenario_results`):

| `completeness` | Meaning | UI / report treatment |
|---|---|---|
| `complete` | every input `SUPPLIER` / `USER_INPUT` / `CALCULATED`; no unconfirmed low-confidence fields | plain |
| `assumption_dependent` | at least one `USER_ASSUMPTION` or `DEFAULT` input, everything else present | badge + assumptions listed inline |
| `incomplete` | at least one required input `MISSING`, or an unconfirmed field that feeds the number | prominent warning; excluded from "recommended" ranking unless the user explicitly overrides |

- **Comparability rule:** two suppliers may only be ranked against each other when their
  `completeness` is comparable. If A is `complete` and B is `incomplete`, the comparison still renders
  but carries a `not_like_for_like` warning naming exactly which components B lacks. Silently ranking
  a supplier first because it forgot to quote freight is the classic failure of this product category.

---

## 3. Landed-cost formulas → code contracts

SPEC formulas, made precise. All amounts in the **RFQ base currency** after normalization (§4).

```
extended_material_cost = normalized_unit_price × accepted_quantity
allocated_fixed_cost   = tooling + setup + documentation + other_fixed
logistics_cost         = shipping + insurance + packaging + handling
import_cost            = tariffs + duties + customs_fees
quality_risk_cost      = quality_risk_rate × extended_material_cost
delay_risk_cost        = delay_risk_per_day × expected_delay_days
financing_cost         = extended_material_cost × annual_rate × (payment_terms_days / 365)
total_landed_cost      = Σ of the seven components
effective_unit_cost    = total_landed_cost / accepted_quantity
```

Decisions the SPEC leaves open, resolved here (each is a documented assumption, each is overridable):

| Question | Decision | Rationale |
|---|---|---|
| Is `quality_risk_cost` a rate or an amount? | Both supported; rate applied to `extended_material_cost`. Default input is a rate. | A per-unit defect cost scales with spend, not with headcount. |
| `delay_risk_cost` basis | `delay_risk_per_day × max(0, promised_lead_time_days − required_lead_time_days)` | Only *late* delivery costs money; early does not earn a credit. |
| `financing_cost` sign | **Positive cost for shorter terms, negative (benefit) for longer terms**, relative to a scenario-level `baseline_payment_terms_days` (default 30). Formula: `extended_material × annual_rate × (baseline_days − terms_days)/365`. | Net-60 is genuinely worth money versus Net-30; modelling it as an always-positive cost would rank a better payment term as worse. **This is the one place a negative component is legal**, and it is labelled "financing benefit". |
| Are tariffs a rate or amount? | Amount if quoted; otherwise `tariff_rate × (extended_material + logistics)` per an explicit assumption with the duty basis stated (CIF vs FOB) | Duty basis materially changes the number; it must be visible. |
| Is tax included? | **Excluded by default** (`tax_is_recoverable=true` assumed) with a scenario flag to include | VAT-style tax is usually recoverable and would distort comparison; but the assumption must be shown. |
| Fixed-cost amortization | Charged **fully to the awarded quantity in this RFQ**, not amortized over forecast volume | Amortizing requires a volume forecast the product does not have; inventing one would be fabrication. |

Code contract (principal-owned interface, implementation delegable):

```python
# app/domain/landed_cost/contracts.py  — PRINCIPAL-OWNED
CALCULATION_VERSION: Final[str] = "1.0.0"

@dataclass(frozen=True, slots=True)
class LandedCostInput:
    quote_line_id: UUID
    accepted_quantity: Decimal            # > 0
    normalized_unit_price: Quantified     # already unit- and currency-normalized
    fixed: FixedCosts                     # tooling/setup/documentation/other
    logistics: LogisticsCosts             # shipping/insurance/packaging/handling
    imports: ImportCosts                  # tariffs/duties/customs
    risk: RiskAssumptions                 # quality_risk_rate, delay_risk_per_day, expected_delay_days
    financing: FinancingAssumptions       # annual_rate, terms_days, baseline_days
    currency: CurrencyCode                # target (RFQ base)
    overrides: Mapping[str, Quantified]   # user manual overrides, each audited

@dataclass(frozen=True, slots=True)
class ComponentResult:
    component: CostComponent
    amount: Decimal                       # quantized MONEY_SCALE
    formula: str                          # human-readable, printed in reports
    inputs: Mapping[str, Decimal | None]
    provenance: Provenance
    is_assumed: bool

@dataclass(frozen=True, slots=True)
class LandedCostResult:
    components: tuple[ComponentResult, ...]
    total_landed_cost: Decimal            # == sum(c.amount for c in components), exactly
    effective_unit_cost: Decimal
    currency: CurrencyCode
    completeness: Completeness
    missing_inputs: tuple[MissingInput, ...]
    assumptions: tuple[Assumption, ...]
    overrides_applied: tuple[AppliedOverride, ...]
    calculation_version: str

class LandedCostCalculator(Protocol):
    version: str
    def calculate(self, inp: LandedCostInput) -> LandedCostResult: ...
```

The calculator takes ids only as opaque labels and never loads anything. The application service is
responsible for assembling `LandedCostInput` from repositories — which is precisely what makes
"financial calculations testable without a database" true.

---

## 4. Currency normalization

`ExchangeRate(base_currency, quote_currency, rate)` means:
**`1 base_currency = rate × quote_currency`** (standard direct quotation, e.g. base USD, quote EUR,
rate 0.92 ⇒ 1 USD = 0.92 EUR).

```
to_base(amount_in_quote, rate)  = quantize(amount_in_quote / rate, target_scale)
to_quote(amount_in_base, rate)  = quantize(amount_in_base  * rate, target_scale)
```

Rules:
- **Convert once, as late as possible, on the *unit price* and each *component*, not on the total.**
  Converting components individually keeps the "components sum to total" invariant in the target
  currency.
- **Rate selection is as-of the scenario, not as-of now.** Pick the row with the greatest
  `effective_date <= scenario.as_of_date` for the exact pair; manual overrides win over provider rows
  on the same date. If no rate exists: `MissingExchangeRateError` — never fall back to 1.0, never
  cross-triangulate silently.
- **Triangulation** (EUR→JPY via USD) is allowed only when explicitly enabled, is recorded as an
  assumption naming the pivot currency, and both legs' rate ids are stored in `fx_snapshot`.
- **Inverse rates:** if only the inverse pair exists, use `1/rate` computed at precision 34, record
  `derived_inverse=true`. Do not store a rounded inverse in the rates table.
- The scenario stores the rate **id and value**; later edits to rates never change historical results.
- Fixture provider ships a fixed synthetic table (USD base; EUR, GBP, JPY, CNY, MXN, INR) with several
  `effective_date`s so date-selection logic is actually exercised. Clearly labelled synthetic.

Rounding note printed in reports: *"Amounts converted at the pinned rate shown; conversion is applied
per component at 6 decimal places, so a converted total may differ from converting the source total by
up to 0.000001 × number of components."*

---

## 5. Unit normalization

Canonical dimensions: `count` (canonical unit `each`), `mass` (`kilogram`), `length` (`meter`).

```
qty_canonical = qty × unit.to_canonical_factor × (part_pack_factor if applicable)
```

Rules:
- **Comparison across dimensions is an error, not a warning.** A price per kg and a price per each are
  incomparable; the calculator raises `IncompatibleUnitsError` and the UI asks for a conversion.
- **Part-specific pack factors** (`unit_conversions.part_id` set) take precedence over global ones:
  "1 reel = 5000 each" is a property of the part, not of the word "reel".
- If a quote is in `reel` and no reel→each factor exists for that part, the line is `MISSING` for
  quantity normalization and cannot be costed. It is reported, not guessed. Guessing pack sizes is the
  single easiest way to produce a 5000× error.
- Every applied conversion is recorded in `assumptions` with its factor, source row id, and
  `assumption_note`, and rendered in the UI as "1 reel = 5,000 each (org default, set 2026-07-14)".
- `normalized_unit_price = unit_price / conversion_factor` when converting a per-pack price to a
  per-each price; quantized at `UNIT_PRICE_SCALE`. The reciprocal direction is applied to quantities.
- Mass/length conversions use exact defined factors (`1 lb = 0.45359237 kg`, `1 ft = 0.3048 m`), stored
  at 12 dp, marked `is_exact=true`.

---

## 6. Price-break selection

Interval semantics: **closed on both ends**, `[min_quantity, max_quantity]`, `max_quantity = NULL`
meaning unbounded. The SPEC's example (`1–99, 100–499, 500–999, 1000+`) is exactly this.

```python
def select_break(breaks: Sequence[PriceBreak], qty: Decimal) -> PriceBreak | None:
    # breaks pre-validated: sorted by min_quantity, non-overlapping
    for b in breaks:
        if qty >= b.min_quantity and (b.max_quantity is None or qty <= b.max_quantity):
            return b
    return None   # gap or below the lowest tier -> explicit failure, never nearest-match
```

Rules and the boundary tests they imply:
- **All-units discount semantics**: the selected tier's price applies to the *entire* quantity.
  (Assumption flagged in `01-architecture.md` §14.3 — needs principal confirmation. Incremental
  discounts would require a different formula and a different MILP.)
- `qty` below the lowest `min_quantity` ⇒ `BelowMinimumTierError`; the UI offers "raise quantity to
  the tier minimum" as an explicit user action, never automatic.
- A gap between tiers ⇒ validation warning at quote confirmation and `NoApplicableTierError` at
  calculation. Overlap ⇒ hard validation error at write time (DB exclusion constraint, §8 of `02-erd`).
- If a quote has **no** price breaks, `unit_price` is used at any quantity ≥ MOQ.
- **MOQ interaction**, three-way policy on the scenario (`moq_policy`):
  - `enforce` (default): allocations below MOQ are infeasible for that supplier/line.
  - `round_up`: allocation is raised to MOQ, the surplus is costed and shown as `moq_surplus_units`,
    and the result is flagged `assumption_dependent`.
  - `ignore`: allowed only with an explicit override reason, audited.
- **Recomputation on re-allocation**: whenever the optimizer changes a quantity, tier selection is
  recomputed from scratch. There is no cached "chosen tier".
- Required boundary tests (mapped in `08-test-strategy.md`): `qty = min-1, min, min+1, max-1, max,
  max+1` for every tier, `qty = MOQ-1, MOQ, MOQ+1`, `qty = 0` (error), fractional qty against integral
  tiers, and single-tier and unbounded-tier quotes.

---

## 7. Vendor scoring

Deterministic, LLM-free, reproducible (SPEC §Vendor comparison engine).

**Step 1 — collect raw criterion values** per supplier. Built-in criteria and directions:

| Key | Direction | Source |
|---|---|---|
| `total_landed_cost` | lower better | calculated |
| `effective_unit_cost` | lower better | calculated |
| `specification_compliance` | higher better | user-scored 0–1 per RFQ line, averaged |
| `lead_time_days` | lower better | quote |
| `capacity_units` | higher better | quote / supplier |
| `moq_flexibility` | higher better | derived: `1 − min(1, moq / required_qty)` |
| `payment_terms_days` | higher better | quote terms |
| `quality_score` | higher better | performance records |
| `defect_rate` | lower better | performance records |
| `on_time_delivery_rate` | higher better | performance records |
| `commercial_exceptions` | lower better | count of stated exceptions/exclusions |
| `supply_concentration` | lower better | scenario-level, allocation-dependent |
| user-defined | declared per criterion | user input |

**Step 2 — normalize to [0,1]** with explicit handling of the SPEC's listed edge cases:

```
higher_better: n = (v - min) / (max - min)
lower_better:  n = (max - v) / (max - min)
```
- `max == min` (all equal, including a single candidate) ⇒ **all candidates get `n = 1.0`** with
  reason `"all candidates equal on this criterion; criterion is non-discriminating"`. Using 0.5 would
  arbitrarily penalize; using 0 would be worse. The reason string makes it inspectable.
- **Missing value** ⇒ policy per criterion (`missing_policy`):
  - `renormalize` (default): the criterion is dropped **for that supplier**, remaining weights are
    renormalized for that supplier, and a `missing_criterion` warning is attached. The supplier is
    marked `not_like_for_like`.
  - `worst`: `n = 0` (pessimistic). Available for users who want missing data punished.
  - `exclude_supplier`: the supplier is excluded from ranking with a stated reason.
- **Zero weight** ⇒ criterion computed and displayed but contributes 0; not dropped, so the user can
  see what it would have said.
- **Outliers**: no winsorization by default (silently reshaping a CFO's numbers is unacceptable).
  Instead, min-max is computed over included suppliers and any value beyond 3× the interquartile range
  raises an `outlier_detected` warning naming the supplier and criterion. Optional
  `outlier_policy=winsorize_p95` exists, is off by default, and is printed in the methodology when on.
- **Excluded suppliers** are removed *before* min/max computation — otherwise an excluded outlier
  silently compresses everyone else's scores. This is a subtle bug worth a dedicated test.

**Step 3 — weight and aggregate:**
```
w_norm_i = w_i / Σ w   (over criteria applicable to this supplier)
score    = Σ (w_norm_i × n_i)              # quantized RATIO_SCALE
```
Weight sum must be > 0. Weights are stored raw and normalized at calculation time so the user's intent
survives edits.

**Step 4 — explain.** Every `criterion_scores` entry carries
`{key, raw, normalized, weight, weighted, direction, reason, missing, outlier}`. The report prints the
full table; the SPEC's "show the reason for each score" is satisfied by construction, not by prose.

**Reproducibility:** scoring is a pure function of (values, weights, policies). Given the scenario
snapshot, re-running yields byte-identical results. A golden-file test asserts this across a version
bump.

Sample demo weights (labelled `is_sample:true`, displayed as "sample assumptions"): landed cost 35 %,
spec compliance 25 %, lead time 15 %, reliability 10 %, payment terms 5 %, MOQ flexibility 5 %,
quality history 5 %.

---

## 8. Calculation versioning

```python
# app/domain/registry.py
CALCULATORS: Mapping[str, type[LandedCostCalculator]] = {"1.0.0": LandedCostCalculatorV1}
SCORERS:     Mapping[str, type[Scorer]]               = {"1.0.0": ScorerV1}
```

- `calculation_version` is written on every persisted result and every report.
- Semver policy: **patch** = bug fix that changes no correct output; **minor** = new optional
  component/criterion, defaults preserve prior output; **major** = any change to an existing formula,
  rounding, or normalization. Any minor/major bump requires a new registry entry, and the old
  implementation stays in the tree.
- **Historical reproducibility test** (required, in CI): for each stored version, a golden fixture of
  inputs → expected outputs must still pass. This is what makes SPEC §Scenario comparison's
  "historical results reproducible after assumptions change" a checked claim rather than a hope.
- Re-running an old scenario with the current version is allowed but creates a **new** scenario and
  shows a version-diff banner; it never overwrites.
- `docs/METHODOLOGY.md` carries a changelog per version with the reason for the change.

---

## 9. Worked example (becomes the first hand-verified test)

Quote: 500 pcs at EUR 10.50, tooling EUR 800, shipping EUR 300, insurance EUR 45, tariff 3.5 % on
(material + logistics), quality risk 2 %, no delay, Net-60 vs baseline Net-30 at 8 % annual.
FX: 1 USD = 0.92 EUR. RFQ base = USD.

| Step | Computation | Value |
|---|---|---|
| unit price → USD | `10.50 / 0.92` | `11.41304348` |
| extended material | `11.41304348 × 500` | `5706.521740` |
| allocated fixed | `800 / 0.92` | `869.565217` → `869.565217` |
| logistics | `(300 + 45) / 0.92` | `375.000000` |
| import | `0.035 × (5706.521740 + 375.000000)` | `212.853261` |
| quality risk | `0.02 × 5706.521740` | `114.130435` |
| delay risk | `0 × …` | `0.000000` |
| financing | `5706.521740 × 0.08 × (30 − 60)/365` | `−37.520000` (benefit) |
| **total** | sum of the seven | **`7240.550653`** |
| effective unit | `7240.550653 / 500` | `14.48110131` |

Every intermediate above is quantized at its stated scale before summing; the test asserts the exact
strings, not approximate equality. Any change to these numbers must be a deliberate
`calculation_version` bump.

---

## 10. Edge cases and gaps flagged to the principal

1. **All-units vs incremental price breaks** — assumed all-units (§6). Confirm.
2. **Financing as a benefit** (negative component) — I recommend it; it is the only signed component
   and it needs an explicit blessing because "a cost component can be negative" surprises reviewers.
3. **Duty basis (FOB vs CIF)** materially changes import cost; modelled as an explicit assumption with
   a default of "material + logistics" (CIF-like). Confirm the default.
4. **Recoverable tax** default `excluded`. Confirm.
5. **Currency of fixed costs** — a quote may state tooling in USD while pricing parts in EUR. Current
   model assumes one currency per quote (see `02-erd.md` §12.1).
6. **Scrap/yield loss** (buy 1050 to get 1000 good) is a real landed-cost driver and is entirely absent
   from the SPEC. Proposed as a v0.2 criterion, not v0.1.0 — flagged so it is a decision, not an
   oversight.
7. **Minimum line/order charges** ("orders under $500 incur a $50 handling fee") are common on real
   quotes and are not in the SPEC's field list. Proposed: capture in `other_fixed_cost` with a note;
   full modelling is roadmap.
8. **Multi-currency FX risk cost** (hedging) — out of scope, explicitly.
9. **Zero-quantity and negative-quantity inputs** must raise, not return 0 — required tests.
10. **Extreme values**: quantity `10^12` and unit price `10^-8` must not overflow `NUMERIC(18,6)`;
    the calculator raises `AmountOutOfRangeError` before the DB does, with a clear message.
