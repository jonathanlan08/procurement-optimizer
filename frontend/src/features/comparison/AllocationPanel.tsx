/** Allocation-result panel — renders `ScenarioResponse.allocation_result`
 * (backend/src/app/schemas/scenarios.py's `AllocationResultResponse`) for
 * either a freshly-run scenario or a scenario loaded read-only from history
 * (ScenarioHistory.tsx passes `savedResult` in the latter case).
 *
 * Design decisions:
 *  - **Status banner first, four distinct tones** — `solver_status` drives
 *    both the banner's tone and which body renders below it: `optimal`/
 *    `feasible` get the allocation table + summary strip (both are "solved",
 *    per `AllocationStatus`'s own docstring: "feasible... a valid
 *    allocation, optimality NOT proven" — a legitimate result, not a
 *    warning-toned near-failure); `infeasible` gets the explainer panel
 *    instead of a table (there is no `allocations[]` to show); `error` gets
 *    the raw `error_message` in the shared `.banner-error` destructive
 *    style. `status_explanation` (backend-composed, already the exact prose
 *    this task's brief paraphrases — "proven optimal" / "optimality not
 *    proven") is rendered verbatim rather than re-derived client-side.
 *  - **"% of spend" is computed client-side with pure BigInt division**
 *    (`percentOfTotal` below) — `line_landed_cost` and the objective total
 *    are both decimal wire strings; running either through `Number()`/
 *    `parseFloat` would violate lib/money.ts's file-level rule. The backend
 *    has no such field itself (an accepted, documented derivation, same
 *    class of decision as `ComparisonPage.tsx`'s existing `toPercentDisplay`).
 *  - **Binding-constraint chips carry their `detail` sentence as a `title`
 *    tooltip** — the same "hover the row for the reason" convention
 *    `ComparisonPage.tsx`'s `ScoringSection` already established for
 *    `score-criterion-row`, not a new interaction pattern.
 *  - **Provenance footer is always rendered** (model hash + optimization
 *    version) whenever `stats`/the result exist, independent of saved-vs-
 *    live; `savedResult` only adds the extra "Saved result" badge + the
 *    three other version stamps (calculation/solver/scoring) that only make
 *    sense once a scenario has been fully persisted and re-fetched.
 */

import { isDecimalString } from "../../lib/money";
import { formatMoney } from "../../lib/money";
import type { AllocationResultResponse, AllocationStatus } from "./api";
import "./comparison.css";

export interface SavedResultVersions {
  calculationVersion: string;
  solverVersion: string;
  scoringVersion: string;
}

function statusLabel(raw: string): string {
  return raw
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

const STATUS_META: Record<AllocationStatus, { label: string; tone: string }> = {
  optimal: { label: "Optimal", tone: "positive" },
  feasible: { label: "Feasible", tone: "info" },
  infeasible: { label: "Infeasible", tone: "warning" },
  error: { label: "Error", tone: "destructive" },
};

// -- pure-string/BigInt percentage (never Number()/parseFloat on decimal
// wire strings — lib/money.ts's file-level rule) -----------------------

function toScaledBigInt(raw: string, scale: number): bigint {
  const negative = raw.startsWith("-");
  const body = negative ? raw.slice(1) : raw;
  const [intPart = "0", fracPart = ""] = body.split(".");
  const digits = `${intPart}${fracPart.padEnd(scale, "0").slice(0, scale)}`.replace(
    /^0+(?=\d)/,
    "",
  );
  const value = BigInt(digits === "" ? "0" : digits);
  return negative ? -value : value;
}

/** `(line / total) * 100`, truncated to `WORK_SCALE` internal digits then
 * rendered to `places` decimal digits — pure BigInt division. Returns null
 * when `total` is zero/invalid (no spend to be a percentage of). */
function percentOfTotal(line: string, total: string, places = 1): string | null {
  if (!isDecimalString(line) || !isDecimalString(total)) return null;
  const WORK_SCALE = 8;
  const lineInt = toScaledBigInt(line, WORK_SCALE);
  const totalInt = toScaledBigInt(total, WORK_SCALE);
  if (totalInt === 0n) return null;
  const numerator = lineInt * 100n * 10n ** BigInt(WORK_SCALE);
  const quotient = numerator / totalInt;
  const negative = quotient < 0n;
  const abs = negative ? -quotient : quotient;
  const digits = abs.toString().padStart(WORK_SCALE + 1, "0");
  const intPart = digits.slice(0, digits.length - WORK_SCALE) || "0";
  const fracPart = digits.slice(digits.length - WORK_SCALE).slice(0, places).padEnd(places, "0");
  const body = places > 0 ? `${intPart}.${fracPart}` : intPart;
  return `${negative ? "-" : ""}${body}`;
}

// -- status banner --------------------------------------------------------

function AllocationStatusBanner({ allocation }: { allocation: AllocationResultResponse }) {
  const meta = STATUS_META[allocation.solver_status];
  return (
    <div
      className={`allocation-status-banner allocation-status-banner--${meta.tone}`}
      role={allocation.solver_status === "error" ? "alert" : "status"}
    >
      <span className={`allocation-status-pill allocation-status-pill--${meta.tone}`}>
        {meta.label}
      </span>
      <span className="allocation-status-text">{allocation.status_explanation}</span>
    </div>
  );
}

// -- infeasibility explainer ------------------------------------------------

function InfeasibilityExplainer({
  narrative,
  conflictingGroups,
  minimalRelaxation,
}: {
  narrative: string;
  conflictingGroups: string[];
  minimalRelaxation: string | null;
}) {
  return (
    <div className="infeasibility-explainer">
      {conflictingGroups.length > 0 && (
        <div className="chip-row">
          {conflictingGroups.map((g) => (
            <span key={g} className="chip chip--conflict">
              {statusLabel(g)}
            </span>
          ))}
        </div>
      )}
      <p className="infeasibility-narrative">{narrative}</p>
      {minimalRelaxation && (
        <div className="infeasibility-relaxation-callout">
          <span className="infeasibility-relaxation-label">Suggested relaxation</span>
          <p>{minimalRelaxation}</p>
        </div>
      )}
    </div>
  );
}

// -- allocation table -------------------------------------------------------

function AllocationTable({
  allocation,
  currency,
}: {
  allocation: AllocationResultResponse;
  currency: string;
}) {
  const totalCost = allocation.objective_total_cost?.amount ?? null;
  const supplierCount = new Set(allocation.allocations.map((a) => a.supplier_id)).size;

  return (
    <>
      <div className="allocation-summary-strip">
        {allocation.objective_total_cost && (
          <div className="allocation-summary-kpi">
            <span className="detail-label">Expected total cost</span>
            <span className="allocation-summary-value num">
              {formatMoney(allocation.objective_total_cost.amount, {
                currency: allocation.objective_total_cost.currency,
              })}
            </span>
          </div>
        )}
        <div className="allocation-summary-kpi">
          <span className="detail-label">Supplier count</span>
          <span className="allocation-summary-value num">{supplierCount}</span>
        </div>
      </div>

      <div className="allocation-table-wrap">
        <table className="allocation-table">
          <thead>
            <tr>
              <th>Supplier</th>
              <th className="num">Quantity</th>
              <th>Tier applied</th>
              <th className="num">Line cost</th>
              <th className="num">% of spend</th>
            </tr>
          </thead>
          <tbody>
            {allocation.allocations.length === 0 ? (
              <tr>
                <td colSpan={5} className="allocation-table-empty">
                  No line allocations were returned for this result.
                </td>
              </tr>
            ) : (
              allocation.allocations.map((a) => {
                const pct = totalCost ? percentOfTotal(a.line_landed_cost, totalCost) : null;
                return (
                  <tr key={`${a.rfq_line_id}-${a.quote_line_id}`}>
                    <td>{a.supplier_label}</td>
                    <td className="num">{a.quantity}</td>
                    <td>
                      <span className="allocation-tier-range">
                        {a.price_break.min_quantity}
                        {"–"}
                        {a.price_break.max_quantity ?? "∞"}
                      </span>
                      <span className="allocation-tier-price">
                        {" "}
                        @ {formatMoney(a.price_break.unit_price, { currency })}/unit
                      </span>
                    </td>
                    <td className="num">{formatMoney(a.line_landed_cost, { currency })}</td>
                    <td className="num">{pct !== null ? `${pct}%` : "—"}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {allocation.binding_constraints.length > 0 && (
        <section className="detail-section">
          <h4 className="detail-section-title">Binding constraints</h4>
          <div className="chip-row">
            {allocation.binding_constraints.map((bc, i) => (
              // Several constraints can share a name (e.g. one "capacity" per
              // bound supplier), so the name alone is not a unique key.
              <span key={`${bc.name}-${i}`} className="chip chip--binding" title={bc.detail}>
                {statusLabel(bc.name)}
              </span>
            ))}
          </div>
        </section>
      )}

      {allocation.rejected_alternatives.length > 0 && (
        <section className="detail-section">
          <h4 className="detail-section-title">Rejected alternatives</h4>
          <ul className="explain-list">
            {allocation.rejected_alternatives.map((r, i) => (
              <li className="explain-list-item" key={i}>
                {r}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

// -- provenance footer --------------------------------------------------

function AllocationProvenanceFooter({
  allocation,
  savedResult,
}: {
  allocation: AllocationResultResponse;
  savedResult?: SavedResultVersions | null;
}) {
  return (
    <footer className="allocation-provenance">
      {allocation.stats && (
        <span className="detail-label mono">
          Model hash {allocation.stats.model_hash.slice(0, 12)}
        </span>
      )}
      <span className="detail-label">Optimization v{allocation.optimization_version}</span>
      {savedResult && (
        <>
          <span className="badge badge--saved-result">Saved result</span>
          <span className="detail-label">Calculation v{savedResult.calculationVersion}</span>
          <span className="detail-label">Solver v{savedResult.solverVersion}</span>
          <span className="detail-label">Scoring v{savedResult.scoringVersion}</span>
        </>
      )}
    </footer>
  );
}

// -- panel ------------------------------------------------------------------

export function AllocationPanel({
  allocation,
  currency,
  savedResult = null,
}: {
  allocation: AllocationResultResponse;
  currency: string;
  savedResult?: SavedResultVersions | null;
}) {
  return (
    <section className="allocation-panel">
      <AllocationStatusBanner allocation={allocation} />

      {allocation.solver_status === "optimal" && allocation.stats?.max_scaling_error && (
        <p className="detail-label allocation-scaling-caveat">
          Optimality is proven with respect to costs rounded to 0.0001 currency units; any
          unconsidered alternative is at most {allocation.stats.max_scaling_error} {currency}{" "}
          cheaper.
        </p>
      )}

      {(allocation.solver_status === "optimal" || allocation.solver_status === "feasible") && (
        <AllocationTable allocation={allocation} currency={currency} />
      )}

      {allocation.solver_status === "infeasible" && allocation.infeasibility_explanation && (
        <InfeasibilityExplainer
          narrative={allocation.infeasibility_explanation.narrative}
          conflictingGroups={allocation.infeasibility_explanation.conflicting_constraint_groups}
          minimalRelaxation={allocation.infeasibility_explanation.minimal_relaxation}
        />
      )}

      {allocation.solver_status === "error" && (
        <div className="banner-error" role="alert">
          {allocation.error_message ?? "The solver failed without a detailed message."}
        </div>
      )}

      <AllocationProvenanceFooter allocation={allocation} savedResult={savedResult} />
    </section>
  );
}
