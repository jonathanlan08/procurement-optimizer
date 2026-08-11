/** Comparison workspace — routed at `/scenarios` (replaces the
 * `PlaceholderPage`, per this task's ALLOWED App.tsx edit), the
 * pre-optimizer surface for comparing live quotes against one RFQ line's
 * landed cost, plus a vendor-scoring section below it.
 *
 * Design decisions:
 *  - **The comparison grain is one RFQ line, not the whole RFQ.** The task
 *    brief's own "not like-for-like" warning only makes sense comparing the
 *    *same* requested item across suppliers — a `<select>` next to the RFQ
 *    picker lets the reviewer choose which RFQ line to compare (defaulting
 *    to the first once the RFQ loads); "Calculate" persists landed cost for
 *    every compared supplier's quote line matched to *that* RFQ line only
 *    (one `POST /rfqs/{id}/landed-costs` call per matched line, per
 *    ./api.ts's file header — there is no batch route).
 *  - **Comparable quotes** are this RFQ's non-superseded (`GET
 *    .../quotes?include_superseded=false`), non-rejected, non-archived
 *    quotes with a line whose `matched_rfq_line_id` equals the selected RFQ
 *    line — i.e. quotes a human (or matching, see ../extraction/
 *    ReviewPane.tsx) has already tied to that specific request. Quote
 *    *details* (lines/terms) are fetched with `useQueries` (one query per
 *    comparable quote, keyed with the same `quoteKeys.detail` builder
 *    ../quotes/api.ts already exports) rather than a per-row child
 *    component, so the winning-cell/not-like-for-like computations in this
 *    file can see every column's data at once.
 *  - **Per-cell click opens the explainability drawer** for that column's
 *    landed-cost result (all six metric rows share one drawer per supplier
 *    column — every row is a view onto the *same* computed
 *    `LandedCostResultResponse`, not six independent explanations).
 *    Built on components/Drawer.tsx (allowed, unmodified use — an
 *    explainability drawer is exactly what that component already is,
 *    per MASTER.md's own "signature interaction" framing), unlike
 *    ../extraction/ReviewPane.tsx's full-screen surface.
 *  - **Scoring section**: see ./api.ts's file header for the documented
 *    backend gap (no implemented ranked-scoring route yet) — this section
 *    is fully built and testable against a mocked response, with an
 *    explicit on-page note about the gap rather than pretending the
 *    feature is production-ready.
 */

import { useQueries } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { get } from "../../api/client";
import { useAuth } from "../../auth/session";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import "../../components/badges.css";
import { Drawer } from "../../components/Drawer";
import { FormField } from "../../components/FormField";
import "../../components/workspace.css";
import { compareDecimalStrings } from "../../lib/decimalSort";
import { formatMoney, isDecimalString } from "../../lib/money";
import { isAnalystOrAbove } from "../../lib/roles";
import { zodResolver } from "../../lib/zodResolver";
import { usePart } from "../parts/api";
import { quoteKeys, type QuoteLineResponse, type QuoteResponse } from "../quotes/api";
import { useRfqQuotes } from "../quotes/api";
import { type RfqLineResponse, type RfqStatus, useRfq, useRfqs } from "../rfqs/api";
import { useSupplier } from "../suppliers/api";
import {
  type Completeness,
  type CostComponentKind,
  EMPTY_ASSUMPTIONS,
  type LandedCostAssumptionsInput,
  type LandedCostResultResponse,
  type ScoringRunResponse,
  useCalculateLandedCost,
  useComputeScoring,
  useRfqLandedCosts,
  useScoringConfigurations,
} from "./api";
import "./comparison.css";

const OPEN_RFQ_STATUSES: RfqStatus[] = ["open", "under_review", "awarded"];

function statusLabel(raw: string): string {
  return raw
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** Pure string decimal-point shift (never Number()/parseFloat, lib/money.ts's
 * rule): "0.987000" -> "98.7%". Duplicated from ../extraction/ReviewPane.tsx —
 * same small-pure-helper-per-feature convention as formatDate/formatQuantity
 * across features/rfqs/RfqsPage.tsx and features/quotes/QuotesSection.tsx. */
function toPercentDisplay(raw: string): string {
  const negative = raw.startsWith("-");
  const body = negative ? raw.slice(1) : raw;
  const [intPart, fracPart = ""] = body.split(".");
  const padded = fracPart.padEnd(2, "0");
  const moved = padded.slice(0, 2);
  const remaining = padded.slice(2).replace(/0+$/, "");
  let newInt = (intPart + moved).replace(/^0+(?=\d)/, "");
  if (newInt === "") newInt = "0";
  const result = remaining ? `${newInt}.${remaining}` : newInt;
  return `${negative ? "-" : ""}${result}%`;
}

const COMPONENT_LABELS: Record<CostComponentKind, string> = {
  extended_material: "Extended material cost",
  allocated_fixed: "Allocated fixed cost",
  logistics: "Logistics",
  import: "Import (tariffs / duties / customs)",
  quality_risk: "Quality risk",
  delay_risk: "Delay risk",
  financing: "Financing",
};

function provenanceLabel(p: string): string {
  switch (p) {
    case "supplier":
      return "Supplier-provided";
    case "user_input":
      return "User input";
    case "user_assumption":
      return "User assumption";
    case "calculated":
      return "Calculated";
    case "default":
      return "Default";
    case "missing":
      return "Missing";
    default:
      return p;
  }
}

function ProvenanceBadge({ provenance }: { provenance: string }) {
  return <span className={`badge badge--provenance-${provenance}`}>{provenanceLabel(provenance)}</span>;
}

function CompletenessBadge({ completeness }: { completeness: Completeness }) {
  const labels: Record<Completeness, string> = {
    complete: "Complete",
    assumption_dependent: "Assumption-dependent",
    incomplete: "Incomplete",
  };
  return <span className={`badge badge--completeness-${completeness}`}>{labels[completeness]}</span>;
}

// --- RFQ / RFQ-line pickers -------------------------------------------------

function RfqLineOption({ line }: { line: RfqLineResponse }) {
  const partQuery = usePart(line.part_id);
  const label = partQuery.data
    ? `${partQuery.data.internal_part_number} — ${partQuery.data.name}`
    : line.part_id;
  return (
    <option value={line.id}>
      {label} — qty {line.required_quantity}
    </option>
  );
}

function SupplierHeaderLabel({ supplierId }: { supplierId: string }) {
  const q = useSupplier(supplierId);
  return <>{q.data ? `${q.data.code} — ${q.data.name}` : supplierId}</>;
}

// --- assumptions form --------------------------------------------------

const DECIMAL_FIELDS = [
  "quality_risk_rate",
  "delay_risk_per_day",
  "annual_rate",
  "baseline_terms_days",
  "tariff_rate",
  "duty_rate",
  "promised_lead_time_days",
  "required_lead_time_days",
] as const;

const assumptionsFormSchema = z
  .object({
    quality_risk_rate: z.string(),
    delay_risk_per_day: z.string(),
    annual_rate: z.string(),
    baseline_terms_days: z.string(),
    tariff_rate: z.string(),
    duty_rate: z.string(),
    promised_lead_time_days: z.string(),
    required_lead_time_days: z.string(),
    assume_missing_costs_zero: z.boolean(),
  })
  .superRefine((val, ctx) => {
    for (const key of DECIMAL_FIELDS) {
      const raw = val[key].trim();
      if (raw === "") continue;
      if (!isDecimalString(raw) || raw.startsWith("-")) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Must be a non-negative decimal number",
          path: [key],
        });
      }
    }
  });

type AssumptionsFormValues = z.infer<typeof assumptionsFormSchema>;

const emptyAssumptionsForm: AssumptionsFormValues = {
  quality_risk_rate: "",
  delay_risk_per_day: "",
  annual_rate: "",
  baseline_terms_days: "",
  tariff_rate: "",
  duty_rate: "",
  promised_lead_time_days: "",
  required_lead_time_days: "",
  assume_missing_costs_zero: false,
};

function toAssumptionsInput(v: AssumptionsFormValues): LandedCostAssumptionsInput {
  const n = (s: string): string | null => (s.trim() === "" ? null : s.trim());
  return {
    quality_risk_rate: n(v.quality_risk_rate),
    delay_risk_per_day: n(v.delay_risk_per_day),
    promised_lead_time_days: n(v.promised_lead_time_days),
    required_lead_time_days: n(v.required_lead_time_days),
    annual_rate: n(v.annual_rate),
    baseline_terms_days: n(v.baseline_terms_days),
    tariff_rate: n(v.tariff_rate),
    duty_rate: n(v.duty_rate),
    assume_missing_costs_zero: v.assume_missing_costs_zero,
  };
}

function AssumptionsForm({
  canWrite,
  busy,
  disabled,
  onCalculate,
}: {
  canWrite: boolean;
  busy: boolean;
  disabled: boolean;
  onCalculate: (a: LandedCostAssumptionsInput) => void;
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<AssumptionsFormValues>({
    resolver: zodResolver(assumptionsFormSchema),
    defaultValues: emptyAssumptionsForm,
  });

  function onValid(values: AssumptionsFormValues) {
    onCalculate(toAssumptionsInput(values));
  }

  return (
    <section className="detail-section">
      <div className="detail-header-row">
        <h3 className="detail-section-title">
          Assumptions <span className="badge badge--provenance-user_assumption">User assumption</span>
        </h3>
      </div>
      <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
        <div className="form-grid">
          <FormField label="Quality risk" hint="%, optional" error={errors.quality_risk_rate?.message}>
            <input {...register("quality_risk_rate")} inputMode="decimal" placeholder="not stated" />
          </FormField>
          <FormField
            label="Delay cost / day"
            hint="money per day, optional"
            error={errors.delay_risk_per_day?.message}
          >
            <input {...register("delay_risk_per_day")} inputMode="decimal" placeholder="not stated" />
          </FormField>
          <FormField label="Annual rate" hint="%, optional" error={errors.annual_rate?.message}>
            <input {...register("annual_rate")} inputMode="decimal" placeholder="not stated" />
          </FormField>
          <FormField
            label="Baseline terms"
            hint="days, optional"
            error={errors.baseline_terms_days?.message}
          >
            <input {...register("baseline_terms_days")} inputMode="decimal" placeholder="not stated" />
          </FormField>
          <FormField label="Tariff rate" hint="%, optional" error={errors.tariff_rate?.message}>
            <input {...register("tariff_rate")} inputMode="decimal" placeholder="not stated" />
          </FormField>
          <FormField label="Assume missing costs = 0" checkbox>
            <input type="checkbox" {...register("assume_missing_costs_zero")} />
          </FormField>
        </div>
        <button
          type="button"
          className="btn-ghost-sm assumptions-advanced-toggle"
          aria-expanded={advancedOpen}
          onClick={() => setAdvancedOpen((v) => !v)}
        >
          {advancedOpen ? "Hide advanced assumptions" : "Advanced assumptions"}
        </button>
        {advancedOpen && (
          <div className="form-grid assumptions-advanced-grid">
            <FormField label="Duty rate" hint="%, optional" error={errors.duty_rate?.message}>
              <input {...register("duty_rate")} inputMode="decimal" placeholder="not stated" />
            </FormField>
            <FormField
              label="Promised lead time"
              hint="days, optional — falls back to the quote line's own lead time"
              error={errors.promised_lead_time_days?.message}
            >
              <input
                {...register("promised_lead_time_days")}
                inputMode="decimal"
                placeholder="not stated"
              />
            </FormField>
            <FormField
              label="Required lead time"
              hint="days, optional"
              error={errors.required_lead_time_days?.message}
            >
              <input
                {...register("required_lead_time_days")}
                inputMode="decimal"
                placeholder="not stated"
              />
            </FormField>
          </div>
        )}
        {canWrite && (
          <div className="form-actions">
            <button type="submit" className="btn-primary-sm" disabled={busy || disabled}>
              {busy ? "Calculating…" : "Calculate"}
            </button>
          </div>
        )}
      </form>
    </section>
  );
}

// --- comparison table --------------------------------------------------

interface ComparisonColumn {
  quote: QuoteResponse;
  line: QuoteLineResponse;
}

function ComparisonTable({
  columns,
  resultFor,
  onOpenExplain,
}: {
  columns: ComparisonColumn[];
  resultFor: (quoteLineId: string) => LandedCostResultResponse | undefined;
  onOpenExplain: (quoteLineId: string) => void;
}) {
  const winningLineId = useMemo(() => {
    let bestId: string | null = null;
    let bestTotal: string | null = null;
    for (const col of columns) {
      const r = resultFor(col.line.id);
      if (!r) continue;
      if (bestTotal === null || compareDecimalStrings(r.total_landed_cost, bestTotal) < 0) {
        bestTotal = r.total_landed_cost;
        bestId = col.line.id;
      }
    }
    return bestId;
  }, [columns, resultFor]);

  const completenessSet = new Set(
    columns
      .map((c) => resultFor(c.line.id)?.completeness)
      .filter((v): v is Completeness => v !== undefined),
  );
  const notLikeForLike = completenessSet.size > 1;

  return (
    <div className="comparison-table-wrap">
      <table className="comparison-table">
        <thead>
          <tr>
            <th className="comparison-row-label">Metric</th>
            {columns.map((c) => (
              <th key={c.quote.id}>
                <SupplierHeaderLabel supplierId={c.quote.supplier_id} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {notLikeForLike && (
            <tr>
              <td colSpan={columns.length + 1} className="comparison-not-like-for-like">
                Compared suppliers have different completeness levels for this line — this
                comparison is not fully like-for-like; treat totals cautiously.
              </td>
            </tr>
          )}
          <tr>
            <th className="comparison-row-label">Quoted unit price</th>
            {columns.map((c) => (
              <td key={c.quote.id} className="num">
                {c.line.unit_price
                  ? formatMoney(c.line.unit_price, { currency: c.quote.currency })
                  : "— not stated"}
              </td>
            ))}
          </tr>
          <tr>
            <th className="comparison-row-label">Effective unit cost</th>
            {columns.map((c) => {
              const r = resultFor(c.line.id);
              return (
                <td
                  key={c.quote.id}
                  className={`num${winningLineId === c.line.id ? " is-winning" : ""}`}
                >
                  {r ? (
                    <button
                      type="button"
                      className="comparison-cell-btn"
                      onClick={() => onOpenExplain(c.line.id)}
                    >
                      {formatMoney(r.effective_unit_cost, { currency: r.currency })}
                    </button>
                  ) : (
                    "— not calculated"
                  )}
                </td>
              );
            })}
          </tr>
          <tr>
            <th className="comparison-row-label">Total landed cost</th>
            {columns.map((c) => {
              const r = resultFor(c.line.id);
              return (
                <td
                  key={c.quote.id}
                  className={`num${winningLineId === c.line.id ? " is-winning" : ""}`}
                >
                  {r ? (
                    <button
                      type="button"
                      className="comparison-cell-btn"
                      onClick={() => onOpenExplain(c.line.id)}
                    >
                      {formatMoney(r.total_landed_cost, { currency: r.currency })}
                    </button>
                  ) : (
                    "— not calculated"
                  )}
                </td>
              );
            })}
          </tr>
          <tr>
            <th className="comparison-row-label">Completeness</th>
            {columns.map((c) => {
              const r = resultFor(c.line.id);
              return (
                <td key={c.quote.id}>
                  {r ? (
                    <button
                      type="button"
                      className="comparison-cell-btn"
                      onClick={() => onOpenExplain(c.line.id)}
                    >
                      <CompletenessBadge completeness={r.completeness} />
                    </button>
                  ) : (
                    "—"
                  )}
                </td>
              );
            })}
          </tr>
          <tr>
            <th className="comparison-row-label">Lead time</th>
            {columns.map((c) => (
              <td key={c.quote.id} className="num">
                {c.line.lead_time_days !== null ? `${c.line.lead_time_days}d` : "— not stated"}
              </td>
            ))}
          </tr>
          <tr>
            <th className="comparison-row-label">Payment terms</th>
            {columns.map((c) => (
              <td key={c.quote.id}>{c.quote.terms?.payment_terms ?? "— not stated"}</td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}

// --- explainability drawer ----------------------------------------------

function ExplainDrawer({
  result,
  supplierLabel,
  open,
  onClose,
}: {
  result: LandedCostResultResponse | null;
  supplierLabel: string;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Drawer open={open} onClose={onClose} title={`Landed cost — ${supplierLabel}`}>
      {result && (
        <div>
          <div className="explain-meta">
            <span className="detail-label">Calculation v{result.calculation_version}</span>
            <span className="detail-label">{new Date(result.calculated_at).toLocaleString()}</span>
            <CompletenessBadge completeness={result.completeness} />
          </div>

          <div className="explain-component-list">
            {result.components.map((c) => (
              <div className="explain-component-row" key={c.component}>
                <div className="explain-component-header">
                  <span className="explain-component-title">{COMPONENT_LABELS[c.component]}</span>
                  <span className="explain-component-amount num">
                    {formatMoney(c.amount, { currency: result.currency })}
                  </span>
                </div>
                <div className="explain-formula">{c.formula}</div>
                <ProvenanceBadge provenance={c.provenance} />
                <div className="explain-chips">
                  {c.is_assumed && <span className="chip chip--assumed">Assumed</span>}
                  {c.is_missing && <span className="chip chip--missing">Missing</span>}
                </div>
              </div>
            ))}
          </div>

          {result.missing_inputs.length > 0 && (
            <section className="detail-section">
              <h3 className="detail-section-title">Missing inputs</h3>
              <ul className="explain-list">
                {result.missing_inputs.map((m, i) => (
                  <li className="explain-list-item" key={i}>
                    <strong>{COMPONENT_LABELS[m.component as CostComponentKind] ?? m.component}:</strong>{" "}
                    {m.input_name} — {m.consequence}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {result.assumptions.length > 0 && (
            <section className="detail-section">
              <h3 className="detail-section-title">Assumptions used</h3>
              <ul className="explain-list">
                {result.assumptions.map((a, i) => (
                  <li className="explain-list-item" key={i}>
                    <ProvenanceBadge provenance={a.provenance} /> <strong>{a.key}</strong> ={" "}
                    <span className="mono">{a.value}</span> — {a.description}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </Drawer>
  );
}

// --- scoring section -------------------------------------------------

function criterionLabel(criterion: string): string {
  return statusLabel(criterion);
}

function ScoringSection({
  rfqId,
  canWrite,
  quoteLineIds,
}: {
  rfqId: string;
  canWrite: boolean;
  quoteLineIds: string[];
}) {
  const configsQuery = useScoringConfigurations();
  const [configId, setConfigId] = useState("");
  const computeMutation = useComputeScoring();
  const [result, setResult] = useState<ScoringRunResponse | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const configs = configsQuery.data?.items ?? [];
  const selectedConfig = configs.find((c) => c.id === configId);

  async function handleScore() {
    if (!configId || quoteLineIds.length === 0) return;
    try {
      const r = await computeMutation.mutateAsync({
        rfqId,
        scoringConfigurationId: configId,
        quoteLineIds,
      });
      setResult(r);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  function toggleExpanded(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <section className="detail-section">
      <h3 className="detail-section-title">Scoring</h3>
      <p className="scoring-gap-note">
        Ranks compared suppliers by a scoring configuration&apos;s weighted criteria
        (total landed cost, spec compliance, lead time, and more). This calls a
        scoring endpoint not yet implemented in this backend build — see
        features/comparison/api.ts for the documented gap; the UI below is ready for
        when it lands.
      </p>
      <div className="scoring-config-select-row">
        <FormField label="Scoring configuration">
          <select value={configId} onChange={(e) => setConfigId(e.target.value)}>
            <option value="">Select configuration…</option>
            {configs.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.is_sample ? " — Sample weights (demonstration)" : ""}
              </option>
            ))}
          </select>
        </FormField>
        {canWrite && (
          <button
            type="button"
            className="btn-primary-sm"
            disabled={!configId || quoteLineIds.length === 0 || computeMutation.isPending}
            onClick={() => void handleScore()}
          >
            {computeMutation.isPending ? "Scoring…" : "Score"}
          </button>
        )}
      </div>
      {selectedConfig?.is_sample && (
        <span className="badge badge--sample-weights">Sample weights (demonstration)</span>
      )}
      <ApiErrorBanner error={computeMutation.error} />
      {result && (
        <>
          {result.notes.length > 0 && (
            <ul className="explain-list">
              {result.notes.map((n, i) => (
                <li className="explain-list-item" key={i}>
                  {n}
                </li>
              ))}
            </ul>
          )}
          <div className="score-list">
            {result.scores.map((s) => (
              <div className={`score-row${s.excluded ? " is-excluded" : ""}`} key={s.supplier_id}>
                <div className="score-row-header">
                  <span>
                    <span className="score-rank">{s.excluded ? "—" : `#${s.rank}`}</span>{" "}
                    {s.supplier_name}
                  </span>
                  <span className="score-total">
                    {s.excluded ? "Excluded" : toPercentDisplay(s.total_score)}
                  </span>
                </div>
                {s.excluded ? (
                  <p className="detail-label">{s.exclusion_reason ?? "Excluded from scoring."}</p>
                ) : (
                  <>
                    {s.criterion_scores.map((cs) => {
                      const key = `${s.supplier_id}:${cs.criterion}`;
                      const isOpen = expanded.has(key);
                      const widthPct =
                        cs.normalized_score !== null ? toPercentDisplay(cs.normalized_score) : "0%";
                      return (
                        <div key={cs.criterion}>
                          <div
                            className="score-criterion-row"
                            onClick={() => toggleExpanded(key)}
                            title={cs.reason}
                          >
                            <span className="score-criterion-label">{criterionLabel(cs.criterion)}</span>
                            <div className="score-bar-track">
                              <div className="score-bar-fill" style={{ width: widthPct }} />
                            </div>
                            <span className="score-criterion-value">
                              {cs.normalized_score !== null ? widthPct : "—"}
                            </span>
                          </div>
                          {isOpen && <div className="score-criterion-reason">{cs.reason}</div>}
                        </div>
                      );
                    })}
                    {s.missing_criteria.length > 0 && (
                      <p className="score-missing-note">
                        Missing: {s.missing_criteria.map(criterionLabel).join(", ")}
                      </p>
                    )}
                    {s.weights_renormalized && (
                      <p className="score-renormalized-note">
                        Weights renormalized for this supplier due to missing criteria.
                      </p>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

// --- page -------------------------------------------------------------

export function ComparisonPage() {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);

  const rfqsQuery = useRfqs({ status: OPEN_RFQ_STATUSES, limit: 100, offset: 0 });
  const [rfqId, setRfqId] = useState("");
  const rfqQuery = useRfq(rfqId || null);
  const [rfqLineId, setRfqLineId] = useState("");

  useEffect(() => {
    const firstLine = rfqQuery.data?.lines[0];
    if (firstLine && !rfqLineId) {
      setRfqLineId(firstLine.id);
    }
  }, [rfqQuery.data, rfqLineId]);

  const quotesQuery = useRfqQuotes(rfqId || null, false);
  const comparableSummaries = useMemo(
    () => (quotesQuery.data?.items ?? []).filter((q) => q.status !== "rejected" && !q.is_archived),
    [quotesQuery.data],
  );

  const quoteDetailQueries = useQueries({
    queries: comparableSummaries.map((s) => ({
      queryKey: quoteKeys.detail(s.id),
      queryFn: () => get<QuoteResponse>(`/api/v1/quotes/${s.id}`),
    })),
  });

  const columns = useMemo<ComparisonColumn[]>(() => {
    if (!rfqLineId) return [];
    const cols: ComparisonColumn[] = [];
    for (const q of quoteDetailQueries) {
      if (!q.data) continue;
      const line = q.data.lines.find((l) => l.matched_rfq_line_id === rfqLineId);
      if (line) cols.push({ quote: q.data, line });
    }
    return cols;
  }, [quoteDetailQueries, rfqLineId]);

  const landedCostsQuery = useRfqLandedCosts(rfqId || null);
  const [freshResults, setFreshResults] = useState<Record<string, LandedCostResultResponse>>({});

  const resultFor = useCallback(
    (quoteLineId: string): LandedCostResultResponse | undefined =>
      freshResults[quoteLineId] ??
      (landedCostsQuery.data?.items ?? []).find((r) => r.quote_line_id === quoteLineId),
    [freshResults, landedCostsQuery.data],
  );

  const calculateMutation = useCalculateLandedCost();
  const [calcError, setCalcError] = useState<string | null>(null);

  async function handleCalculate(assumptions: LandedCostAssumptionsInput) {
    if (!rfqId || columns.length === 0) return;
    setCalcError(null);
    const settled = await Promise.allSettled(
      columns.map((c) =>
        calculateMutation.mutateAsync({ rfqId, quoteLineId: c.line.id, assumptions }),
      ),
    );
    const next: Record<string, LandedCostResultResponse> = {};
    let failures = 0;
    for (const s of settled) {
      if (s.status === "fulfilled") next[s.value.quote_line_id] = s.value;
      else failures += 1;
    }
    setFreshResults((prev) => ({ ...prev, ...next }));
    if (failures > 0) {
      setCalcError(
        `${failures} of ${columns.length} line(s) failed to calculate. Check each supplier's quote line for a matched RFQ line and valid inputs.`,
      );
    }
  }

  const [explainLineId, setExplainLineId] = useState<string | null>(null);
  const explainResult = explainLineId ? (resultFor(explainLineId) ?? null) : null;
  const explainColumn = columns.find((c) => c.line.id === explainLineId);
  const explainSupplierQuery = useSupplier(explainColumn?.quote.supplier_id ?? null);
  const explainSupplierLabel = explainSupplierQuery.data
    ? `${explainSupplierQuery.data.code} — ${explainSupplierQuery.data.name}`
    : (explainColumn?.quote.supplier_id ?? "");

  return (
    <section className="comparison-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <h1>Compare quotes</h1>
          <p>Landed-cost comparison and vendor scoring across an RFQ&apos;s live quotes.</p>
        </div>
      </header>

      <div className="comparison-picker-row">
        <FormField label="RFQ">
          <select
            value={rfqId}
            onChange={(e) => {
              setRfqId(e.target.value);
              setRfqLineId("");
              setFreshResults({});
              setExplainLineId(null);
            }}
          >
            <option value="">Select RFQ…</option>
            {(rfqsQuery.data?.items ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.internal_reference} — {r.name} ({statusLabel(r.status)})
              </option>
            ))}
          </select>
        </FormField>
        {rfqQuery.data && rfqQuery.data.lines.length > 0 && (
          <FormField label="RFQ line">
            <select value={rfqLineId} onChange={(e) => setRfqLineId(e.target.value)}>
              {rfqQuery.data.lines.map((l) => (
                <RfqLineOption key={l.id} line={l} />
              ))}
            </select>
          </FormField>
        )}
      </div>

      {!rfqId ? (
        <p className="detail-label">Select an RFQ to compare its quotes.</p>
      ) : rfqQuery.isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : !rfqQuery.data ? (
        <ApiErrorBanner error={rfqQuery.error} />
      ) : (
        <>
          <AssumptionsForm
            canWrite={canWrite}
            busy={calculateMutation.isPending}
            disabled={columns.length === 0}
            onCalculate={(a) => void handleCalculate(a)}
          />
          {calcError && (
            <div className="banner-error" role="alert">
              {calcError}
            </div>
          )}

          {columns.length === 0 ? (
            <p className="detail-label">No comparable quotes are matched to this RFQ line yet.</p>
          ) : (
            <ComparisonTable columns={columns} resultFor={resultFor} onOpenExplain={setExplainLineId} />
          )}

          <ScoringSection
            rfqId={rfqId}
            canWrite={canWrite}
            quoteLineIds={columns.map((c) => c.line.id)}
          />
        </>
      )}

      <ExplainDrawer
        result={explainResult}
        supplierLabel={explainSupplierLabel}
        open={explainLineId !== null}
        onClose={() => setExplainLineId(null)}
      />
    </section>
  );
}
