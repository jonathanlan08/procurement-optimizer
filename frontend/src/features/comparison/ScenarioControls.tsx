/** Scenario-creation form — strategy + constraints + "Run scenario",
 * replacing the old single-purpose "Score" button (see ./api.ts's header:
 * `POST /rfqs/{id}/comparison-scenarios` now runs scoring AND allocation in
 * one call, and this form is the control surface for the constraints that
 * feed the solver half, not just the scoring half).
 *
 * Design decisions:
 *  - **Scoring configuration is conditionally required.** `scenario_service.
 *    py`'s `_resolve_scoring_config` only reads `scoring_configuration_id`
 *    for `balanced`/`custom` (`_STRATEGIES_NEEDING_CONFIG`) and 422s if it's
 *    missing for those two; the other four strategies use a fixed built-in
 *    weight set and ignore the field even if sent. The select is disabled
 *    with an explanatory hint for the other four rather than always shown
 *    as if it always mattered.
 *  - **Max concentration is collected as a percent, converted to
 *    `FractionString` wire format (RATIO_SCALE = 6dp) with a pure-string
 *    `percentToFraction`** — mirrors `ComparisonPage.tsx`'s existing
 *    `toPercentDisplay` (its exact inverse), never `Number()`/`parseFloat`
 *    per lib/money.ts's file-level rule.
 *  - **Supplier exclusion is a toggle-chip multi-select over this RFQ's
 *    invited, not-already-excluded suppliers** (`useRfqSuppliers`), tracked
 *    as local `useState` outside react-hook-form — same "toggle chips beat
 *    `<select multiple>` for a handful of scannable options" call
 *    `RfqsPage.tsx`'s own file header already makes for its status filter,
 *    applied here to a per-scenario solver constraint
 *    (`excluded_supplier_ids`) rather than an RFQ-wide invitation exclusion
 *    — a supplier excluded here stays invited and can be included in a
 *    later scenario; this is not `useExcludeSupplier`.
 *  - **`max_supplier_count` is a genuine JSON integer** (not a decimal wire
 *    string — see ./api.ts's header), so plain `Number()` on the trimmed
 *    input is fine here without violating the money-string rule, which is
 *    scoped to decimal/money precision, not small integer counts.
 */

import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { FormField } from "../../components/FormField";
import "../../components/workspace.css";
import { compareDecimalStrings } from "../../lib/decimalSort";
import { isDecimalString } from "../../lib/money";
import { zodResolver } from "../../lib/zodResolver";
import { useRfqSuppliers } from "../rfqs/api";
import { useSupplier } from "../suppliers/api";
import {
  COMPARISON_STRATEGIES,
  useCreateScenario,
  useScoringConfigurations,
  type ComparisonStrategyValue,
  type ScenarioResponse,
} from "./api";
import "./comparison.css";

/** Percent input string (e.g. "35", "35.5", "100") -> `FractionString` wire
 * format at RATIO_SCALE (6dp) — pure string/decimal-point shift, the exact
 * inverse of `ComparisonPage.tsx`'s `toPercentDisplay`. Never
 * `Number()`/`parseFloat` (lib/money.ts's file-level rule). */
function percentToFraction(raw: string): string {
  const trimmed = raw.trim();
  const negative = trimmed.startsWith("-");
  const body = negative ? trimmed.slice(1) : trimmed;
  const [intPart = "0", fracPart = ""] = body.split(".");
  const totalFracLen = fracPart.length + 2; // shift decimal point 2 places left (/100)
  const digits = intPart + fracPart;
  const padded = digits.padStart(totalFracLen + 1, "0");
  const cut = padded.length - totalFracLen;
  const newInt = padded.slice(0, cut).replace(/^0+(?=\d)/, "") || "0";
  const newFrac = padded.slice(cut);
  const scaledFrac = newFrac.length >= 6 ? newFrac.slice(0, 6) : newFrac.padEnd(6, "0");
  return `${negative ? "-" : ""}${newInt}.${scaledFrac}`;
}

const scenarioFormSchema = z
  .object({
    name: z.string().trim().min(1, "Required").max(200, "Must be 200 characters or fewer"),
    strategy: z.string().min(1, "Select a strategy"),
    scoringConfigurationId: z.string(),
    maxSupplierCount: z.string(),
    maxConcentrationPercent: z.string(),
    budgetLimit: z.string(),
    allowIncompleteOffers: z.boolean(),
  })
  .superRefine((val, ctx) => {
    const descriptor = COMPARISON_STRATEGIES.find((s) => s.value === val.strategy);
    if (descriptor?.needsScoringConfig && val.scoringConfigurationId.trim() === "") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Required for this strategy",
        path: ["scoringConfigurationId"],
      });
    }
    const maxSupplierRaw = val.maxSupplierCount.trim();
    if (maxSupplierRaw !== "" && !/^[1-9]\d*$/.test(maxSupplierRaw)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Must be a whole number of 1 or more",
        path: ["maxSupplierCount"],
      });
    }
    const concRaw = val.maxConcentrationPercent.trim();
    if (concRaw !== "") {
      if (!isDecimalString(concRaw) || concRaw.startsWith("-")) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Must be a non-negative decimal",
          path: ["maxConcentrationPercent"],
        });
      } else if (
        compareDecimalStrings(concRaw, "0") <= 0 ||
        compareDecimalStrings(concRaw, "100") > 0
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Must be greater than 0 and at most 100",
          path: ["maxConcentrationPercent"],
        });
      }
    }
    const budgetRaw = val.budgetLimit.trim();
    if (budgetRaw !== "" && (!isDecimalString(budgetRaw) || budgetRaw.startsWith("-"))) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Must be a non-negative decimal",
        path: ["budgetLimit"],
      });
    }
  });

type ScenarioFormValues = z.infer<typeof scenarioFormSchema>;

const emptyScenarioForm: ScenarioFormValues = {
  name: "",
  strategy: "",
  scoringConfigurationId: "",
  maxSupplierCount: "",
  maxConcentrationPercent: "",
  budgetLimit: "",
  allowIncompleteOffers: false,
};

function ExcludeSupplierChip({
  supplierId,
  pressed,
  onToggle,
}: {
  supplierId: string;
  pressed: boolean;
  onToggle: () => void;
}) {
  const q = useSupplier(supplierId);
  const label = q.data ? `${q.data.code} — ${q.data.name}` : supplierId;
  return (
    <button type="button" className="exclude-supplier-chip" aria-pressed={pressed} onClick={onToggle}>
      {label}
    </button>
  );
}

export function ScenarioControls({
  rfqId,
  canWrite,
  onCreated,
}: {
  rfqId: string;
  canWrite: boolean;
  onCreated: (scenario: ScenarioResponse) => void;
}) {
  const configsQuery = useScoringConfigurations();
  const rfqSuppliersQuery = useRfqSuppliers(rfqId);
  const createMutation = useCreateScenario();
  const [excludedSupplierIds, setExcludedSupplierIds] = useState<Set<string>>(new Set());

  const {
    register,
    handleSubmit,
    watch,
    formState: { errors },
  } = useForm<ScenarioFormValues>({
    resolver: zodResolver(scenarioFormSchema),
    defaultValues: emptyScenarioForm,
  });

  const strategy = watch("strategy") as ComparisonStrategyValue | "";
  const descriptor = COMPARISON_STRATEGIES.find((s) => s.value === strategy);
  const configs = configsQuery.data?.items ?? [];

  const excludableSuppliers = (rfqSuppliersQuery.data?.items ?? []).filter(
    (rs) => rs.excluded_at === null,
  );

  function toggleExcluded(supplierId: string) {
    setExcludedSupplierIds((prev) => {
      const next = new Set(prev);
      if (next.has(supplierId)) next.delete(supplierId);
      else next.add(supplierId);
      return next;
    });
  }

  async function onValid(values: ScenarioFormValues) {
    const maxSupplierRaw = values.maxSupplierCount.trim();
    const concRaw = values.maxConcentrationPercent.trim();
    const budgetRaw = values.budgetLimit.trim();
    try {
      const scenario = await createMutation.mutateAsync({
        rfqId,
        name: values.name.trim(),
        strategy: values.strategy as ComparisonStrategyValue,
        scoringConfigurationId:
          values.scoringConfigurationId.trim() === "" ? null : values.scoringConfigurationId,
        constraints: {
          max_supplier_count: maxSupplierRaw === "" ? null : Number(maxSupplierRaw),
          max_concentration: concRaw === "" ? null : percentToFraction(concRaw),
          budget_limit: budgetRaw === "" ? null : budgetRaw,
          excluded_supplier_ids: Array.from(excludedSupplierIds),
          allow_incomplete_offers: values.allowIncompleteOffers,
        },
      });
      onCreated(scenario);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <section className="detail-section">
      <h3 className="detail-section-title">Scenario</h3>
      <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
        <div className="form-grid">
          <FormField label="Scenario name" error={errors.name?.message} full>
            <input {...register("name")} type="text" placeholder="e.g. Q3 widget award" />
          </FormField>

          <FormField label="Strategy" error={errors.strategy?.message}>
            <select {...register("strategy")}>
              <option value="">Select strategy…</option>
              {COMPARISON_STRATEGIES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </FormField>

          <FormField
            label="Scoring configuration"
            {...(descriptor && !descriptor.needsScoringConfig
              ? { hint: "not used by this strategy" }
              : {})}
            error={errors.scoringConfigurationId?.message}
          >
            <select
              {...register("scoringConfigurationId")}
              disabled={!descriptor?.needsScoringConfig}
            >
              <option value="">Select configuration…</option>
              {configs.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.is_sample ? " — Sample weights (demonstration)" : ""}
                </option>
              ))}
            </select>
          </FormField>
        </div>

        {descriptor && <p className="scenario-strategy-description">{descriptor.description}</p>}

        <div className="form-grid">
          <FormField label="Max suppliers" hint="optional" error={errors.maxSupplierCount?.message}>
            <input
              {...register("maxSupplierCount")}
              inputMode="numeric"
              placeholder="no limit"
            />
          </FormField>
          <FormField
            label="Max concentration"
            hint="%, optional"
            error={errors.maxConcentrationPercent?.message}
          >
            <input
              {...register("maxConcentrationPercent")}
              inputMode="decimal"
              placeholder="no limit"
            />
          </FormField>
          <FormField label="Budget limit" hint="optional" error={errors.budgetLimit?.message}>
            <input {...register("budgetLimit")} inputMode="decimal" placeholder="no limit" />
          </FormField>
          <FormField label="Allow incomplete offers" checkbox>
            <input type="checkbox" {...register("allowIncompleteOffers")} />
          </FormField>
        </div>
        <p className="scenario-incomplete-warning">
          By default, quote lines missing a required cost input (with no assumption covering it)
          are excluded from this scenario&apos;s allocation entirely. Enabling this includes them
          anyway — their landed cost may be understated.
        </p>

        {excludableSuppliers.length > 0 && (
          <FormField label="Exclude suppliers from this scenario" hint="optional" full>
            <div className="exclude-supplier-row" role="group" aria-label="Exclude suppliers">
              {excludableSuppliers.map((rs) => (
                <ExcludeSupplierChip
                  key={rs.id}
                  supplierId={rs.supplier_id}
                  pressed={excludedSupplierIds.has(rs.supplier_id)}
                  onToggle={() => toggleExcluded(rs.supplier_id)}
                />
              ))}
            </div>
          </FormField>
        )}

        <ApiErrorBanner error={createMutation.error} />

        {canWrite && (
          <div className="form-actions">
            <button type="submit" className="btn-primary-sm" disabled={createMutation.isPending}>
              {createMutation.isPending ? "Running…" : "Run scenario"}
            </button>
          </div>
        )}
      </form>
    </section>
  );
}
