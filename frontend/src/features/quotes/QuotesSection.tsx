/** Quote entry, mounted inside the RFQ detail drawer (RfqsPage.tsx's
 * `RfqDetail`, per this task's ALLOWED "minimal insertion" edit there).
 *
 * Design decisions, mirroring the freshest established patterns (features/
 * rfqs/RfqsPage.tsx / features/boms/BomsPage.tsx — see those files' headers
 * for the drawer-vs-route + dense-list rationale this file follows too):
 *
 *  - **Self-contained role gating.** Rather than threading `canWrite`/
 *    `canArchive` down from RfqsPage.tsx (which would turn the "minimal
 *    insertion" mount line into prop-plumbing surgery), this file calls
 *    `useAuth()` directly, exactly the way every other feature page already
 *    does. The RfqsPage.tsx edit stays a single `<QuotesSection rfq={rfq} />`
 *    line plus one import.
 *  - **"Enter quote" mirrors `QuoteService._check_rfq_eligible` exactly**:
 *    visible only for analyst+ while the RFQ itself is `open`/
 *    `under_review` (services/quote_service.py) — the same rule the server
 *    re-enforces on `POST /rfqs/{id}/quotes`. "Supersede" on the detail
 *    view mirrors the same eligibility check (`QuoteService.supersede`
 *    calls `_check_rfq_eligible` too) plus the service's own guard against
 *    superseding an already-superseded quote.
 *  - **Supplier select is scoped to invited, non-excluded suppliers of
 *    this RFQ** (`useRfqSuppliers` from ../rfqs/api, filtered on
 *    `excluded_at === null`) — `QuoteService._check_supplier_eligible`
 *    rejects any other supplier_id with a 409, so offering only the
 *    eligible set avoids a round-trip just to hit that error.
 *  - **Cost fields never default to `"0"`.** Every per-line commercial
 *    field (unit price, MOQ, and all ten tooling/setup/packaging/shipping/
 *    insurance/other/tariff/duty/customs/tax costs) starts life as an empty
 *    string and round-trips to `null`, never `"0"` — see ./api.ts's file
 *    header on why an absent amount must stay absent
 *    (`QuoteLineCreate`: "Missing stays missing").
 *  - **Price-break structural hints are a client-side mirror of
 *    `QuoteService._validate_price_breaks`** (duplicate `min_quantity`,
 *    overlap, non-final open-ended `max_quantity`) — computed live via
 *    `useWatch` so they update on every keystroke, and *also* enforced by
 *    `quoteFormSchema`'s `superRefine` so submission is blocked before the
 *    round trip. The server re-validates authoritatively regardless; this
 *    is advisory, not a substitute.
 *  - **The "RFQ line or free description" line identity is two
 *    independent, non-exclusive fields**, not a mode toggle:
 *    `QuoteLineCreate.matched_rfq_line_id`/`.description` are both
 *    optional and can both be set at once (there's no server-side
 *    either/or constraint) — this form only requires that *at least one*
 *    of the two is present, and prefills quantity/unit/part from the
 *    selected RFQ line as a convenience the user can still override.
 *  - **Quote entry/detail is a second `<Drawer>` stacked on top of the RFQ
 *    drawer** (reusing components/Drawer.tsx, the same component RfqsPage
 *    itself uses) rather than an inline swap of the RFQ drawer's own body —
 *    consistent with MASTER.md's "explainability drawers" progressive-
 *    disclosure idiom. One inherited quirk from stacking two independent
 *    `Drawer` instances: each registers its own capture-phase `Escape`
 *    listener on `document`, and Drawer.tsx is outside this task's edit
 *    scope, so pressing Escape while the quote drawer is open closes both
 *    drawers (the RFQ drawer's listener, registered first, still fires).
 *    Clicking the quote drawer's own close button is unaffected and is the
 *    documented way to close just the quote drawer.
 */

import { useMemo, useState } from "react";
import {
  useFieldArray,
  useForm,
  useWatch,
  type Control,
  type FieldErrors,
  type UseFormRegister,
  type UseFormSetValue,
} from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../../auth/session";
import "../../components/badges.css";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { DetailRow } from "../../components/DetailRow";
import { Drawer } from "../../components/Drawer";
import { FormField } from "../../components/FormField";
import { PlusIcon, XIcon } from "../../components/icons";
import "../../components/workspace.css";
import { isDecimalString } from "../../lib/money";
import { isAdministratorOrAbove, isAnalystOrAbove } from "../../lib/roles";
import { zodResolver } from "../../lib/zodResolver";
import { type UnitDefinitionResponse, useUnits } from "../boms/api";
import { usePart } from "../parts/api";
import type { RfqLineResponse, RfqResponse, RfqStatus } from "../rfqs/api";
import { useRfqSuppliers } from "../rfqs/api";
import { useSupplier } from "../suppliers/api";
import {
  type QuoteCreateInput,
  type QuoteLineInput,
  type QuotePriceBreakInput,
  type QuoteResponse,
  type QuoteStatus,
  type QuoteSummaryResponse,
  type QuoteSupersedeInput,
  type QuoteTermsInput,
  useArchiveQuote,
  useCreateQuote,
  useQuote,
  useRfqQuotes,
  useSupersedeQuote,
} from "./api";
import "./quotes.css";

const CURRENCY_OPTIONS = ["USD", "EUR", "GBP", "JPY", "CNY", "MXN"] as const;

type CostFieldKey =
  | "tooling_cost"
  | "setup_cost"
  | "documentation_cost"
  | "packaging_cost"
  | "shipping_cost"
  | "handling_cost"
  | "insurance_cost"
  | "other_fixed_cost"
  | "tariff_amount"
  | "duty_amount"
  | "customs_fee"
  | "tax_amount";

// `documentation_cost`/`handling_cost` (migration 0016, 2026-08 audit
// remediation P1 — see ./api.ts's `QuoteLineInput` and backend/src/app/
// schemas/quotes.py's module docstring) are placed beside packaging/shipping
// here since that's their real-world grouping (freight-adjacent fixed
// costs), same "not stated" placeholder/validation convention as every
// other optional cost in this array — COST_FIELDS drives both the entry
// form and the read-only detail view generically (see QuoteLineFormRow /
// QuoteLineViewRow below), so adding a key here is the only wiring needed
// for both surfaces.
const COST_FIELDS: { key: CostFieldKey; label: string }[] = [
  { key: "tooling_cost", label: "Tooling" },
  { key: "setup_cost", label: "Setup" },
  { key: "documentation_cost", label: "Documentation" },
  { key: "packaging_cost", label: "Packaging" },
  { key: "shipping_cost", label: "Shipping" },
  { key: "handling_cost", label: "Handling" },
  { key: "insurance_cost", label: "Insurance" },
  { key: "other_fixed_cost", label: "Other" },
  { key: "tariff_amount", label: "Tariff" },
  { key: "duty_amount", label: "Duty" },
  { key: "customs_fee", label: "Customs" },
  { key: "tax_amount", label: "Tax" },
];

function statusLabel(status: string): string {
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** Parses "YYYY-MM-DD" as a local-timezone date, not UTC — same rule (and
 * same off-by-one-day rationale) as features/rfqs/RfqsPage.tsx's own
 * `formatDate`. */
function formatDate(raw: string | null): string {
  if (!raw) return "—";
  const [y, m, d] = raw.split("-").map(Number);
  if (!y || !m || !d) return raw;
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Trims trailing fractional zeros for display without ever parsing the
 * decimal wire string through Number()/parseFloat (lib/money.ts's rule). */
function formatQuantity(raw: string): string {
  if (!raw.includes(".")) return raw;
  const trimmed = raw.replace(/0+$/, "").replace(/\.$/, "");
  return trimmed === "" ? "0" : trimmed;
}

function emptyToNull(v: string): string | null {
  const t = v.trim();
  return t === "" ? null : t;
}

/** Compares two validated decimal strings without float precision loss
 * (BigInt on a shared fractional scale) — used only for price-break
 * ordering/overlap hints, never for a value sent to the server. */
function compareDecimalStrings(a: string, b: string): number {
  const aNeg = a.startsWith("-");
  const bNeg = b.startsWith("-");
  const aAbs = aNeg ? a.slice(1) : a;
  const bAbs = bNeg ? b.slice(1) : b;
  if (aNeg !== bNeg) return aNeg ? -1 : 1;
  const [aInt = "0", aFrac = ""] = aAbs.split(".");
  const [bInt = "0", bFrac = ""] = bAbs.split(".");
  const places = Math.max(aFrac.length, bFrac.length);
  const aScaled = BigInt(aInt + aFrac.padEnd(places, "0"));
  const bScaled = BigInt(bInt + bFrac.padEnd(places, "0"));
  const cmp = aScaled === bScaled ? 0 : aScaled > bScaled ? 1 : -1;
  return aNeg ? -cmp : cmp;
}

interface PriceBreakRow {
  min_quantity: string;
  max_quantity: string;
}

/** Client-side mirror of `QuoteService._validate_price_breaks`'s
 * structural rules — see this file's header. Rows that aren't yet valid
 * decimals are skipped rather than flagged, so partial typing doesn't
 * flash spurious errors. Returns at most one message per row index. */
function priceBreakIssues(rows: PriceBreakRow[]): Record<number, string> {
  const issues: Record<number, string> = {};
  const valid = rows
    .map((r, index) => ({ index, min: r.min_quantity.trim(), max: r.max_quantity.trim() }))
    .filter((r) => isDecimalString(r.min) && !r.min.startsWith("-"));

  const seenMin = new Map<string, number>();
  for (const r of valid) {
    if (seenMin.has(r.min)) {
      issues[r.index] = "Duplicate minimum quantity.";
    } else {
      seenMin.set(r.min, r.index);
    }
  }

  const sorted = [...valid].sort((a, b) => compareDecimalStrings(a.min, b.min));
  sorted.forEach((row, pos) => {
    if (issues[row.index]) return;
    const isLast = pos === sorted.length - 1;
    if (row.max === "") {
      if (!isLast) {
        issues[row.index] = "Open-ended (blank) max quantity is only valid on the highest tier.";
      }
      return;
    }
    if (!isDecimalString(row.max) || row.max.startsWith("-")) return;
    const next = sorted[pos + 1];
    if (!isLast && next && compareDecimalStrings(row.max, next.min) >= 0) {
      issues[row.index] = "Overlaps the next tier — max quantity must be less than its min quantity.";
    }
  });

  return issues;
}

function QuoteStatusBadge({ status }: { status: QuoteStatus }) {
  return <span className={`badge quote-badge--${status}`}>{statusLabel(status)}</span>;
}

function SupplierOption({ supplierId }: { supplierId: string }) {
  const q = useSupplier(supplierId);
  return <option value={supplierId}>{q.data ? `${q.data.code} — ${q.data.name}` : supplierId}</option>;
}

function RfqLineOption({ line }: { line: RfqLineResponse }) {
  const partQuery = usePart(line.part_id);
  const label = partQuery.data
    ? `${partQuery.data.internal_part_number} — ${partQuery.data.name}`
    : line.part_id;
  return (
    <option value={line.id}>
      {label} — qty {formatQuantity(line.required_quantity)}
    </option>
  );
}

// --- form schema ------------------------------------------------------

const priceBreakFormSchema = z.object({
  min_quantity: z.string(),
  max_quantity: z.string(), // blank = open-ended
  unit_price: z.string(),
  setup_fee: z.string(),
});

const quoteLineFormSchema = z.object({
  matched_rfq_line_id: z.string(),
  part_id: z.string(),
  description: z.string(),
  quantity: z.string(),
  unit_definition_id: z.string(),
  unit_price: z.string(),
  moq: z.string(),
  lead_time_days: z.string(),
  country_of_origin: z.string(),
  tooling_cost: z.string(),
  setup_cost: z.string(),
  documentation_cost: z.string(),
  packaging_cost: z.string(),
  shipping_cost: z.string(),
  handling_cost: z.string(),
  insurance_cost: z.string(),
  other_fixed_cost: z.string(),
  tariff_amount: z.string(),
  duty_amount: z.string(),
  customs_fee: z.string(),
  tax_amount: z.string(),
  price_breaks: z.array(priceBreakFormSchema),
});

type QuoteLineFormValues = z.infer<typeof quoteLineFormSchema>;
type QuotePriceBreakFormValues = z.infer<typeof priceBreakFormSchema>;

function checkOptionalNonNegative(raw: string, path: (string | number)[], ctx: z.RefinementCtx) {
  const v = raw.trim();
  if (v === "") return;
  if (!isDecimalString(v) || v.startsWith("-")) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Must be a non-negative decimal number", path });
  }
}

function checkOptionalPositive(raw: string, path: (string | number)[], ctx: z.RefinementCtx) {
  const v = raw.trim();
  if (v === "") return;
  if (!isDecimalString(v) || v.startsWith("-") || /^0+(\.0+)?$/.test(v)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Must be a positive decimal number", path });
  }
}

// No per-field zod refinements on `quoteLineFormSchema`/`priceBreakFormSchema`
// themselves — same reasoning as ../rfqs/RfqsPage.tsx's `rfqFormSchema`
// header: a plain `z.array(...)` would validate every element
// unconditionally, so line/price-break validation lives entirely in this
// top-level `superRefine`.
const quoteFormSchema = z
  .object({
    supplier_id: z.string(),
    quote_number: z.string(),
    quote_date: z.string(),
    expiration_date: z.string(),
    currency: z.string(),
    notes: z.string(),
    payment_terms: z.string(),
    incoterm: z.string(),
    warranty_terms: z.string(),
    exceptions: z.string(),
    exclusions: z.string(),
    lines: z.array(quoteLineFormSchema),
  })
  .superRefine((val, ctx) => {
    if (!val.supplier_id.trim()) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Select a supplier", path: ["supplier_id"] });
    }
    if (!val.quote_date.trim()) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Quote date is required", path: ["quote_date"] });
    }
    if (!(CURRENCY_OPTIONS as readonly string[]).includes(val.currency)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Select a currency", path: ["currency"] });
    }
    if (val.lines.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "At least one line is required",
        path: ["lines"],
      });
    }
    val.lines.forEach((line, i) => {
      const hasRfqLine = line.matched_rfq_line_id.trim() !== "";
      const hasDescription = line.description.trim() !== "";
      if (!hasRfqLine && !hasDescription) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Select an RFQ line or enter a description",
          path: ["lines", i, "description"],
        });
      }
      const qty = line.quantity.trim();
      if (qty === "") {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Quantity is required",
          path: ["lines", i, "quantity"],
        });
      } else if (!isDecimalString(qty) || qty.startsWith("-") || /^0+(\.0+)?$/.test(qty)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Must be a positive decimal number",
          path: ["lines", i, "quantity"],
        });
      }
      if (!line.unit_definition_id.trim()) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Select a unit",
          path: ["lines", i, "unit_definition_id"],
        });
      }
      checkOptionalNonNegative(line.unit_price, ["lines", i, "unit_price"], ctx);
      checkOptionalPositive(line.moq, ["lines", i, "moq"], ctx);
      if (line.lead_time_days.trim() !== "" && !/^\d+$/.test(line.lead_time_days.trim())) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Must be a whole number of days",
          path: ["lines", i, "lead_time_days"],
        });
      }
      if (
        line.country_of_origin.trim() !== "" &&
        !/^[A-Za-z]{2}$/.test(line.country_of_origin.trim())
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Must be a 2-letter country code, e.g. US",
          path: ["lines", i, "country_of_origin"],
        });
      }
      for (const { key } of COST_FIELDS) {
        checkOptionalNonNegative(line[key], ["lines", i, key], ctx);
      }
      line.price_breaks.forEach((pb, j) => {
        const min = pb.min_quantity.trim();
        if (min === "") {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Minimum quantity is required",
            path: ["lines", i, "price_breaks", j, "min_quantity"],
          });
        } else if (!isDecimalString(min) || min.startsWith("-") || /^0+(\.0+)?$/.test(min)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Must be a positive decimal number",
            path: ["lines", i, "price_breaks", j, "min_quantity"],
          });
        }
        if (pb.max_quantity.trim() !== "") {
          checkOptionalPositive(pb.max_quantity, ["lines", i, "price_breaks", j, "max_quantity"], ctx);
        }
        const price = pb.unit_price.trim();
        if (price === "") {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Unit price is required",
            path: ["lines", i, "price_breaks", j, "unit_price"],
          });
        } else if (!isDecimalString(price) || price.startsWith("-")) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Must be a non-negative decimal number",
            path: ["lines", i, "price_breaks", j, "unit_price"],
          });
        }
        checkOptionalNonNegative(pb.setup_fee, ["lines", i, "price_breaks", j, "setup_fee"], ctx);
      });
      const hints = priceBreakIssues(line.price_breaks);
      for (const [idxStr, message] of Object.entries(hints)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message,
          path: ["lines", i, "price_breaks", Number(idxStr), "min_quantity"],
        });
      }
    });
  });

type QuoteFormValues = z.infer<typeof quoteFormSchema>;

const emptyLine: QuoteLineFormValues = {
  matched_rfq_line_id: "",
  part_id: "",
  description: "",
  quantity: "",
  unit_definition_id: "",
  unit_price: "",
  moq: "",
  lead_time_days: "",
  country_of_origin: "",
  tooling_cost: "",
  setup_cost: "",
  documentation_cost: "",
  packaging_cost: "",
  shipping_cost: "",
  handling_cost: "",
  insurance_cost: "",
  other_fixed_cost: "",
  tariff_amount: "",
  duty_amount: "",
  customs_fee: "",
  tax_amount: "",
  price_breaks: [],
};

const emptyFormValues: QuoteFormValues = {
  supplier_id: "",
  quote_number: "",
  quote_date: "",
  expiration_date: "",
  currency: "USD",
  notes: "",
  payment_terms: "",
  incoterm: "",
  warranty_terms: "",
  exceptions: "",
  exclusions: "",
  lines: [emptyLine],
};

/** Prefills the supersede form from the quote being replaced. `supplier_id`
 * is intentionally left blank/unused — see this file's header + ./api.ts's:
 * a supersede inherits the old quote's supplier server-side and never sends
 * one. */
function toFormValues(q: QuoteResponse): QuoteFormValues {
  return {
    supplier_id: "",
    quote_number: q.quote_number ?? "",
    quote_date: q.quote_date,
    expiration_date: q.expiration_date ?? "",
    currency: q.currency,
    notes: q.notes ?? "",
    payment_terms: q.terms?.payment_terms ?? "",
    incoterm: q.terms?.incoterm ?? "",
    warranty_terms: q.terms?.warranty_terms ?? "",
    exceptions: q.terms?.exceptions ?? "",
    exclusions: q.terms?.exclusions ?? "",
    lines:
      q.lines.length > 0
        ? q.lines.map((l) => ({
            matched_rfq_line_id: l.matched_rfq_line_id ?? "",
            part_id: l.part_id ?? "",
            description: l.description ?? "",
            quantity: l.quantity,
            unit_definition_id: l.unit_definition_id,
            unit_price: l.unit_price ?? "",
            moq: l.moq ?? "",
            lead_time_days: l.lead_time_days !== null ? String(l.lead_time_days) : "",
            country_of_origin: l.country_of_origin ?? "",
            tooling_cost: l.tooling_cost ?? "",
            setup_cost: l.setup_cost ?? "",
            documentation_cost: l.documentation_cost ?? "",
            packaging_cost: l.packaging_cost ?? "",
            shipping_cost: l.shipping_cost ?? "",
            handling_cost: l.handling_cost ?? "",
            insurance_cost: l.insurance_cost ?? "",
            other_fixed_cost: l.other_fixed_cost ?? "",
            tariff_amount: l.tariff_amount ?? "",
            duty_amount: l.duty_amount ?? "",
            customs_fee: l.customs_fee ?? "",
            tax_amount: l.tax_amount ?? "",
            price_breaks: l.price_breaks.map((pb) => ({
              min_quantity: pb.min_quantity,
              max_quantity: pb.max_quantity ?? "",
              unit_price: pb.unit_price,
              setup_fee: pb.setup_fee ?? "",
            })),
          }))
        : [emptyLine],
  };
}

function toPriceBreakInput(pb: QuotePriceBreakFormValues): QuotePriceBreakInput {
  return {
    min_quantity: pb.min_quantity.trim(),
    max_quantity: emptyToNull(pb.max_quantity),
    unit_price: pb.unit_price.trim(),
    setup_fee: emptyToNull(pb.setup_fee),
    tier_index: null,
  };
}

function toLineInput(l: QuoteLineFormValues): QuoteLineInput {
  return {
    matched_rfq_line_id: emptyToNull(l.matched_rfq_line_id),
    part_id: emptyToNull(l.part_id),
    quoted_part_number: null,
    quoted_mpn: null,
    description: emptyToNull(l.description),
    quantity: l.quantity.trim(),
    unit_definition_id: l.unit_definition_id.trim(),
    unit_price: emptyToNull(l.unit_price),
    moq: emptyToNull(l.moq),
    tooling_cost: emptyToNull(l.tooling_cost),
    setup_cost: emptyToNull(l.setup_cost),
    documentation_cost: emptyToNull(l.documentation_cost),
    packaging_cost: emptyToNull(l.packaging_cost),
    shipping_cost: emptyToNull(l.shipping_cost),
    handling_cost: emptyToNull(l.handling_cost),
    insurance_cost: emptyToNull(l.insurance_cost),
    other_fixed_cost: emptyToNull(l.other_fixed_cost),
    tariff_amount: emptyToNull(l.tariff_amount),
    duty_amount: emptyToNull(l.duty_amount),
    customs_fee: emptyToNull(l.customs_fee),
    tax_amount: emptyToNull(l.tax_amount),
    country_of_origin: l.country_of_origin.trim() ? l.country_of_origin.trim().toUpperCase() : null,
    lead_time_days: l.lead_time_days.trim() ? Number(l.lead_time_days.trim()) : null,
    production_capacity: null,
    notes: null,
    price_breaks: l.price_breaks.map(toPriceBreakInput),
  };
}

function toTermsInput(v: QuoteFormValues): QuoteTermsInput | null {
  const hasTerms = [v.payment_terms, v.incoterm, v.warranty_terms, v.exceptions, v.exclusions].some(
    (s) => s.trim() !== "",
  );
  if (!hasTerms) return null;
  return {
    payment_terms: emptyToNull(v.payment_terms),
    payment_terms_days: null,
    incoterm: emptyToNull(v.incoterm),
    shipping_terms: null,
    warranty_terms: emptyToNull(v.warranty_terms),
    validity_days: null,
    exceptions: emptyToNull(v.exceptions),
    exclusions: emptyToNull(v.exclusions),
    notes: null,
  };
}

function toCreatePayload(v: QuoteFormValues): QuoteCreateInput {
  return {
    supplier_id: v.supplier_id.trim(),
    quote_number: emptyToNull(v.quote_number),
    quote_date: v.quote_date,
    expiration_date: emptyToNull(v.expiration_date),
    currency: v.currency,
    notes: emptyToNull(v.notes),
    lines: v.lines.map(toLineInput),
    terms: toTermsInput(v),
  };
}

function toSupersedeInput(v: QuoteFormValues): QuoteSupersedeInput {
  return {
    quote_number: emptyToNull(v.quote_number),
    quote_date: v.quote_date,
    expiration_date: emptyToNull(v.expiration_date),
    currency: v.currency,
    notes: emptyToNull(v.notes),
    lines: v.lines.map(toLineInput),
    terms: toTermsInput(v),
  };
}

// --- price-break editor (nested field array) -------------------------

function QuotePriceBreakEditor({
  lineIndex,
  control,
  register,
  errors,
}: {
  lineIndex: number;
  control: Control<QuoteFormValues>;
  register: UseFormRegister<QuoteFormValues>;
  errors: FieldErrors<QuoteFormValues>;
}) {
  const { fields, append, remove } = useFieldArray({
    control,
    name: `lines.${lineIndex}.price_breaks`,
  });
  const rows = useWatch({ control, name: `lines.${lineIndex}.price_breaks` }) ?? [];
  const hints = useMemo(
    () => priceBreakIssues(rows.map((r) => ({ min_quantity: r.min_quantity, max_quantity: r.max_quantity }))),
    [rows],
  );

  const flatErrors = errors as unknown as Record<string, { message?: string } | undefined>;
  const err = (j: number, field: string) => flatErrors[`lines.${lineIndex}.price_breaks.${j}.${field}`]?.message;

  return (
    <div className="quote-price-break-editor">
      <div className="detail-header-row">
        <span className="field-label">Price breaks</span>
        <button
          type="button"
          className="btn-ghost-sm"
          onClick={() => append({ min_quantity: "", max_quantity: "", unit_price: "", setup_fee: "" })}
        >
          <PlusIcon /> Add tier
        </button>
      </div>
      {fields.length === 0 ? (
        <p className="detail-label">No price breaks — single flat unit price above.</p>
      ) : (
        <div className="quote-price-break-list">
          {fields.map((field, j) => (
            <div className="quote-price-break-row" key={field.id}>
              <FormField label="Min qty" error={err(j, "min_quantity")}>
                <input
                  {...register(`lines.${lineIndex}.price_breaks.${j}.min_quantity`)}
                  inputMode="decimal"
                />
              </FormField>
              <FormField label="Max qty" hint="blank = open-ended" error={err(j, "max_quantity")}>
                <input
                  {...register(`lines.${lineIndex}.price_breaks.${j}.max_quantity`)}
                  inputMode="decimal"
                  placeholder="open-ended"
                />
              </FormField>
              <FormField label="Unit price" error={err(j, "unit_price")}>
                <input {...register(`lines.${lineIndex}.price_breaks.${j}.unit_price`)} inputMode="decimal" />
              </FormField>
              <FormField label="Setup fee" hint="optional" error={err(j, "setup_fee")}>
                <input
                  {...register(`lines.${lineIndex}.price_breaks.${j}.setup_fee`)}
                  inputMode="decimal"
                  placeholder="not stated"
                />
              </FormField>
              <button
                type="button"
                className="alt-row-remove"
                aria-label={`Remove price break ${j + 1}`}
                onClick={() => remove(j)}
              >
                <XIcon size={12} />
              </button>
              {hints[j] && (
                <div className="field-error quote-price-break-hint" role="alert">
                  {hints[j]}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- line editor row ----------------------------------------------------

function QuoteLineFormRow({
  index,
  control,
  register,
  setValue,
  errors,
  units,
  rfqLines,
  onRemove,
  canRemove,
}: {
  index: number;
  control: Control<QuoteFormValues>;
  register: UseFormRegister<QuoteFormValues>;
  setValue: UseFormSetValue<QuoteFormValues>;
  errors: FieldErrors<QuoteFormValues>;
  units: UnitDefinitionResponse[];
  rfqLines: RfqLineResponse[];
  onRemove: () => void;
  canRemove: boolean;
}) {
  const matchedRfqLineId = useWatch({ control, name: `lines.${index}.matched_rfq_line_id` });
  const [costsOpen, setCostsOpen] = useState(false);

  const flatErrors = errors as unknown as Record<string, { message?: string } | undefined>;
  const err = (field: string) => flatErrors[`lines.${index}.${field}`]?.message;

  return (
    <div className="quote-line-row">
      <div className="quote-line-row-header">
        <span className="detail-label">Line {index + 1}</span>
        {canRemove && (
          <button
            type="button"
            className="alt-row-remove"
            aria-label={`Remove line ${index + 1}`}
            onClick={onRemove}
          >
            <XIcon size={12} />
          </button>
        )}
      </div>
      <div className="form-grid">
        <FormField label="RFQ line" hint="or enter a description below" full>
          <select
            value={matchedRfqLineId}
            onChange={(e) => {
              const id = e.target.value;
              setValue(`lines.${index}.matched_rfq_line_id`, id, { shouldValidate: true });
              const rfqLine = rfqLines.find((l) => l.id === id);
              if (rfqLine) {
                setValue(`lines.${index}.part_id`, rfqLine.part_id);
                setValue(`lines.${index}.quantity`, rfqLine.required_quantity, { shouldValidate: true });
                setValue(`lines.${index}.unit_definition_id`, rfqLine.unit_definition_id, {
                  shouldValidate: true,
                });
              } else {
                setValue(`lines.${index}.part_id`, "");
              }
            }}
          >
            <option value="">— none (free text) —</option>
            {rfqLines.map((l) => (
              <RfqLineOption key={l.id} line={l} />
            ))}
          </select>
        </FormField>
        <FormField label="Description" hint="required if no RFQ line selected" full error={err("description")}>
          <input {...register(`lines.${index}.description`)} placeholder="Supplier's as-quoted line description" />
        </FormField>
        <FormField label="Quantity" error={err("quantity")}>
          <input
            {...register(`lines.${index}.quantity`)}
            aria-invalid={!!err("quantity")}
            inputMode="decimal"
          />
        </FormField>
        <FormField label="Unit" error={err("unit_definition_id")}>
          <select {...register(`lines.${index}.unit_definition_id`)} aria-invalid={!!err("unit_definition_id")}>
            <option value="">Select unit…</option>
            {units.map((u) => (
              <option key={u.id} value={u.id}>
                {u.code} — {u.name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Unit price" hint="8dp, optional" error={err("unit_price")}>
          <input {...register(`lines.${index}.unit_price`)} inputMode="decimal" placeholder="not stated" />
        </FormField>
        <FormField label="MOQ" hint="optional" error={err("moq")}>
          <input {...register(`lines.${index}.moq`)} inputMode="decimal" placeholder="not stated" />
        </FormField>
        <FormField label="Lead time (days)" hint="optional" error={err("lead_time_days")}>
          <input {...register(`lines.${index}.lead_time_days`)} inputMode="numeric" placeholder="not stated" />
        </FormField>
        <FormField label="Country of origin" hint="optional, ISO-2" error={err("country_of_origin")}>
          <input {...register(`lines.${index}.country_of_origin`)} placeholder="not stated" maxLength={2} />
        </FormField>
      </div>

      <button
        type="button"
        className="btn-ghost-sm quote-costs-toggle"
        onClick={() => setCostsOpen((v) => !v)}
        aria-expanded={costsOpen}
      >
        {costsOpen ? "Hide costs" : "Add costs"}
      </button>
      {costsOpen && (
        <div className="form-grid quote-costs-grid">
          {COST_FIELDS.map(({ key, label }) => (
            <FormField key={key} label={label} hint="optional" error={err(key)}>
              <input {...register(`lines.${index}.${key}`)} inputMode="decimal" placeholder="not stated" />
            </FormField>
          ))}
        </div>
      )}

      <QuotePriceBreakEditor lineIndex={index} control={control} register={register} errors={errors} />
    </div>
  );
}

// --- entry / supersede form -----------------------------------------

function QuoteForm({
  rfqId,
  invitedSupplierIds,
  rfqLines,
  base,
  onSaved,
  onCancel,
}: {
  rfqId: string;
  invitedSupplierIds: string[];
  rfqLines: RfqLineResponse[];
  base?: QuoteResponse;
  onSaved: (q: QuoteResponse) => void;
  onCancel: () => void;
}) {
  const createMutation = useCreateQuote();
  const supersedeMutation = useSupersedeQuote();
  const unitsQuery = useUnits();
  const busy = createMutation.isPending || supersedeMutation.isPending;
  const mutationError = createMutation.error ?? supersedeMutation.error;

  const {
    register,
    handleSubmit,
    control,
    setValue,
    formState: { errors },
  } = useForm<QuoteFormValues>({
    resolver: zodResolver(quoteFormSchema),
    defaultValues: base ? toFormValues(base) : emptyFormValues,
  });
  const { fields, append, remove } = useFieldArray({ control, name: "lines" });

  async function onValid(values: QuoteFormValues) {
    try {
      if (base) {
        const created = await supersedeMutation.mutateAsync({ id: base.id, data: toSupersedeInput(values) });
        onSaved(created);
      } else {
        const created = await createMutation.mutateAsync({ rfqId, data: toCreatePayload(values) });
        onSaved(created);
      }
    } catch {
      // surfaced via ApiErrorBanner reading createMutation.error / supersedeMutation.error
    }
  }

  const units = unitsQuery.data?.items ?? [];
  const flatErrors = errors as unknown as Record<string, { message?: string } | undefined>;
  const linesError = flatErrors["lines"]?.message;

  return (
    <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <ApiErrorBanner error={mutationError} />
      <div className="form-grid">
        {base ? (
          <FormField label="Supplier" full>
            <span className="detail-label">Same supplier as the quote being superseded — inherited automatically.</span>
          </FormField>
        ) : (
          <FormField label="Supplier" error={errors.supplier_id?.message} full>
            <select {...register("supplier_id")} aria-invalid={!!errors.supplier_id}>
              <option value="">Select supplier…</option>
              {invitedSupplierIds.map((id) => (
                <SupplierOption key={id} supplierId={id} />
              ))}
            </select>
          </FormField>
        )}
        <FormField label="Quote number" hint="optional">
          <input {...register("quote_number")} autoComplete="off" />
        </FormField>
        <FormField label="Quote date" error={errors.quote_date?.message}>
          <input type="date" {...register("quote_date")} aria-invalid={!!errors.quote_date} />
        </FormField>
        <FormField label="Expiration date" hint="optional">
          <input type="date" {...register("expiration_date")} />
        </FormField>
        <FormField label="Currency" error={errors.currency?.message}>
          <select {...register("currency")}>
            {CURRENCY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Notes" hint="optional" full>
          <textarea {...register("notes")} rows={3} />
        </FormField>
      </div>

      <section className="detail-section">
        <div className="detail-header-row">
          <h3 className="detail-section-title">Lines</h3>
          <button type="button" className="btn-secondary-sm" onClick={() => append(emptyLine)}>
            <PlusIcon /> Add line
          </button>
        </div>
        {linesError && (
          <div className="field-error" role="alert">
            {linesError}
          </div>
        )}
        <div className="quote-line-list">
          {fields.map((field, index) => (
            <QuoteLineFormRow
              key={field.id}
              index={index}
              control={control}
              register={register}
              setValue={setValue}
              errors={errors}
              units={units}
              rfqLines={rfqLines}
              onRemove={() => remove(index)}
              canRemove={fields.length > 1}
            />
          ))}
        </div>
      </section>

      <section className="detail-section">
        <h3 className="detail-section-title">Terms</h3>
        <div className="form-grid">
          <FormField label="Payment terms" hint="optional">
            <input {...register("payment_terms")} />
          </FormField>
          <FormField label="Incoterm" hint="optional">
            <input {...register("incoterm")} />
          </FormField>
          <FormField label="Warranty" hint="optional" full>
            <input {...register("warranty_terms")} />
          </FormField>
          <FormField label="Exceptions" hint="optional" full>
            <textarea {...register("exceptions")} rows={2} />
          </FormField>
          <FormField label="Exclusions" hint="optional" full>
            <textarea {...register("exclusions")} rows={2} />
          </FormField>
        </div>
      </section>

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn-primary-sm" disabled={busy}>
          {busy ? "Saving…" : base ? "Create revision" : "Enter quote"}
        </button>
      </div>
    </form>
  );
}

// --- read-only line row (quote detail) ---------------------------------

function QuoteLineViewRow({
  line,
  unitsById,
}: {
  line: QuoteResponse["lines"][number];
  unitsById: Map<string, UnitDefinitionResponse>;
}) {
  const partQuery = usePart(line.part_id);
  const unit = unitsById.get(line.unit_definition_id);
  const identity =
    line.description ??
    (line.part_id
      ? partQuery.data
        ? `${partQuery.data.internal_part_number} — ${partQuery.data.name}`
        : line.part_id
      : "—");

  return (
    <div className="quote-line-view-row">
      <div className="detail-header-row">
        <span className="detail-label">Line {line.line_number}</span>
        {line.match_status !== "unmatched" && (
          <span className="detail-label">Match: {statusLabel(line.match_status)}</span>
        )}
      </div>
      <div className="detail-grid">
        <DetailRow label="Identity" value={identity} full />
        <DetailRow label="Quantity" value={formatQuantity(line.quantity)} num />
        <DetailRow label="Unit" value={unit ? unit.code : line.unit_definition_id} mono={!unit} />
        <DetailRow
          label="Unit price"
          value={line.unit_price ? formatQuantity(line.unit_price) : "— not stated"}
          num={!!line.unit_price}
        />
        <DetailRow label="MOQ" value={line.moq ? formatQuantity(line.moq) : "— not stated"} num={!!line.moq} />
        <DetailRow
          label="Lead time"
          value={line.lead_time_days !== null ? `${line.lead_time_days} days` : "— not stated"}
        />
        <DetailRow
          label="Country of origin"
          value={line.country_of_origin ?? "— not stated"}
          mono={!!line.country_of_origin}
        />
        {COST_FIELDS.map(({ key, label }) => (
          <DetailRow
            key={key}
            label={label}
            value={line[key] ? formatQuantity(line[key] as string) : "— not stated"}
            num={!!line[key]}
          />
        ))}
        {line.notes && <DetailRow label="Notes" value={line.notes} full />}
      </div>
      {line.price_breaks.length > 0 && (
        <div className="quote-price-break-view">
          <span className="field-label">Price breaks</span>
          <table className="quote-price-break-table">
            <thead>
              <tr>
                <th>Min qty</th>
                <th>Max qty</th>
                <th data-align="right">Unit price</th>
                <th data-align="right">Setup fee</th>
              </tr>
            </thead>
            <tbody>
              {line.price_breaks.map((pb) => (
                <tr key={pb.id}>
                  <td className="mono">{formatQuantity(pb.min_quantity)}</td>
                  <td className="mono">{pb.max_quantity ? formatQuantity(pb.max_quantity) : "open-ended"}</td>
                  <td data-align="right" className="mono">
                    {formatQuantity(pb.unit_price)}
                  </td>
                  <td data-align="right" className="mono">
                    {pb.setup_fee ? formatQuantity(pb.setup_fee) : "— not stated"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// --- detail drawer -------------------------------------------------------

function QuoteDetail({
  id,
  rfqStatus,
  canWrite,
  canArchive,
  onClosed,
  onSupersede,
}: {
  id: string;
  rfqStatus: RfqStatus;
  canWrite: boolean;
  canArchive: boolean;
  onClosed: () => void;
  onSupersede: (q: QuoteResponse) => void;
}) {
  const { data: quote, isLoading, isError, error, refetch } = useQuote(id);
  const supplierQuery = useSupplier(quote?.supplier_id ?? null);
  const unitsQuery = useUnits();
  const [archiving, setArchiving] = useState(false);
  const [archiveReason, setArchiveReason] = useState("");
  const archiveMutation = useArchiveQuote();

  const unitsById = useMemo(
    () => new Map((unitsQuery.data?.items ?? []).map((u) => [u.id, u] as const)),
    [unitsQuery.data],
  );

  if (isLoading) {
    return <p className="detail-label">Loading…</p>;
  }
  if (isError || !quote) {
    return (
      <div>
        {error ? (
          <ApiErrorBanner error={error} />
        ) : (
          <div className="banner-error" role="alert">
            Quote not found.
          </div>
        )}
        <button type="button" className="btn-ghost-sm" onClick={() => void refetch()}>
          Retry
        </button>
      </div>
    );
  }

  // Mirrors QuoteService.supersede's own guards: RFQ still open/under_review,
  // and the quote itself not already superseded — see this file's header.
  const canSupersede =
    canWrite &&
    !quote.is_archived &&
    quote.status !== "superseded" &&
    (rfqStatus === "open" || rfqStatus === "under_review");

  async function handleArchiveConfirm() {
    if (!quote || archiveReason.trim() === "") return;
    try {
      await archiveMutation.mutateAsync({ id: quote.id, reason: archiveReason.trim() });
      setArchiving(false);
      setArchiveReason("");
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <div>
      <div className="detail-header-row">
        <QuoteStatusBadge status={quote.status} />
        <div className="detail-actions">
          {canSupersede && (
            <button type="button" className="btn-secondary-sm" onClick={() => onSupersede(quote)}>
              Supersede (new revision)
            </button>
          )}
          {canArchive && !quote.is_archived && (
            <button type="button" className="btn-danger-sm" onClick={() => setArchiving(true)}>
              Archive
            </button>
          )}
        </div>
      </div>

      <ApiErrorBanner error={archiveMutation.error} />

      <section className="detail-section">
        <h3 className="detail-section-title">Overview</h3>
        <div className="detail-grid">
          <DetailRow
            label="Supplier"
            value={supplierQuery.data ? `${supplierQuery.data.code} — ${supplierQuery.data.name}` : quote.supplier_id}
            mono={!supplierQuery.data}
          />
          <DetailRow label="Quote number" value={quote.quote_number ?? "—"} mono />
          <DetailRow label="Quote date" value={formatDate(quote.quote_date)} />
          <DetailRow label="Expiration date" value={formatDate(quote.expiration_date)} />
          <DetailRow label="Currency" value={quote.currency} />
          <DetailRow label="Source" value={quote.source === "manual" ? "Manual entry" : "Extracted"} />
          <DetailRow label="Version" value={String(quote.version)} mono />
          <DetailRow label="Updated" value={new Date(quote.updated_at).toLocaleString()} />
          {quote.superseded_by_id && <DetailRow label="Superseded by" value={quote.superseded_by_id} mono />}
          <DetailRow label="Notes" value={quote.notes ?? "—"} full />
        </div>
      </section>

      {quote.is_archived && (
        <section className="detail-section">
          <h3 className="detail-section-title">Archive record</h3>
          <div className="detail-grid">
            <DetailRow
              label="Archived at"
              value={quote.archived_at ? new Date(quote.archived_at).toLocaleString() : "—"}
            />
            <DetailRow label="Reason" value={quote.archive_reason ?? "—"} full />
          </div>
        </section>
      )}

      {archiving && (
        <div className="archive-panel">
          <label className="field">
            <span className="field-label">Archive reason</span>
            <textarea
              value={archiveReason}
              onChange={(e) => setArchiveReason(e.target.value)}
              placeholder="Why is this quote being archived?"
              autoFocus
            />
          </label>
          <div className="form-actions">
            <button type="button" className="btn-ghost-sm" onClick={() => setArchiving(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-danger-sm"
              onClick={() => void handleArchiveConfirm()}
              disabled={archiveReason.trim() === "" || archiveMutation.isPending}
            >
              {archiveMutation.isPending ? "Archiving…" : "Confirm archive"}
            </button>
          </div>
        </div>
      )}

      <section className="detail-section">
        <h3 className="detail-section-title">Lines</h3>
        <div className="quote-line-view-list">
          {quote.lines.map((l) => (
            <QuoteLineViewRow key={l.id} line={l} unitsById={unitsById} />
          ))}
        </div>
      </section>

      {quote.terms && (
        <section className="detail-section">
          <h3 className="detail-section-title">Terms</h3>
          <div className="detail-grid">
            <DetailRow label="Payment terms" value={quote.terms.payment_terms ?? "—"} />
            <DetailRow label="Incoterm" value={quote.terms.incoterm ?? "—"} />
            <DetailRow label="Warranty" value={quote.terms.warranty_terms ?? "—"} full />
            <DetailRow label="Exceptions" value={quote.terms.exceptions ?? "—"} full />
            <DetailRow label="Exclusions" value={quote.terms.exclusions ?? "—"} full />
          </div>
        </section>
      )}

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onClosed}>
          Close
        </button>
      </div>
    </div>
  );
}

// --- list row -------------------------------------------------------

function QuoteRow({ quote, onOpen }: { quote: QuoteSummaryResponse; onOpen: () => void }) {
  const supplierQuery = useSupplier(quote.supplier_id);
  return (
    <li className={`quote-row${quote.status === "superseded" ? " is-muted" : ""}`}>
      <button type="button" className="quote-row-btn" onClick={onOpen}>
        <span className="quote-row-main">
          <span className="quote-row-supplier">
            {supplierQuery.data ? `${supplierQuery.data.code} — ${supplierQuery.data.name}` : quote.supplier_id}
          </span>
          <span className="detail-label">
            {quote.quote_number ?? "No quote #"} · {formatDate(quote.quote_date)} · {quote.currency} ·{" "}
            {quote.line_count} line{quote.line_count === 1 ? "" : "s"}
          </span>
        </span>
        <QuoteStatusBadge status={quote.status} />
      </button>
    </li>
  );
}

// --- section (mounted inside RfqDetail) ---------------------------------

type QuoteDrawerState =
  | { mode: "create" }
  | { mode: "view"; id: string }
  | { mode: "supersede"; base: QuoteResponse }
  | null;

export function QuotesSection({ rfq }: { rfq: RfqResponse }) {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);
  const canArchive = isAdministratorOrAbove(session?.role);

  const quotesQuery = useRfqQuotes(rfq.id, true);
  const suppliersQuery = useRfqSuppliers(rfq.id);
  const items = quotesQuery.data?.items ?? [];
  const invitedSupplierIds = useMemo(
    () => (suppliersQuery.data?.items ?? []).filter((s) => s.excluded_at === null).map((s) => s.supplier_id),
    [suppliersQuery.data],
  );
  const [drawerState, setDrawerState] = useState<QuoteDrawerState>(null);

  // Mirrors QuoteService._check_rfq_eligible — see this file's header.
  const canEnterQuote = canWrite && (rfq.status === "open" || rfq.status === "under_review");

  function closeDrawer() {
    setDrawerState(null);
  }

  const drawerTitle =
    drawerState?.mode === "create"
      ? "Enter quote"
      : drawerState?.mode === "supersede"
        ? "New quote revision"
        : drawerState?.mode === "view"
          ? "Quote detail"
          : "";

  return (
    <section className="detail-section">
      <div className="detail-header-row">
        <h3 className="detail-section-title">Quotes</h3>
        {canEnterQuote && (
          <button type="button" className="btn-secondary-sm" onClick={() => setDrawerState({ mode: "create" })}>
            <PlusIcon /> Enter quote
          </button>
        )}
      </div>
      {quotesQuery.isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : quotesQuery.isError ? (
        <ApiErrorBanner error={quotesQuery.error} />
      ) : items.length === 0 ? (
        <p className="detail-label">No quotes entered yet.</p>
      ) : (
        <ul className="quote-list">
          {items.map((q) => (
            <QuoteRow key={q.id} quote={q} onOpen={() => setDrawerState({ mode: "view", id: q.id })} />
          ))}
        </ul>
      )}

      {/* Second, stacked Drawer for quote entry/detail/supersede — see this
          file's header on the Escape-key stacking caveat. */}
      <Drawer open={drawerState !== null} onClose={closeDrawer} title={drawerTitle}>
        {drawerState?.mode === "create" && (
          <QuoteForm
            rfqId={rfq.id}
            invitedSupplierIds={invitedSupplierIds}
            rfqLines={rfq.lines}
            onSaved={(created) => setDrawerState({ mode: "view", id: created.id })}
            onCancel={closeDrawer}
          />
        )}
        {drawerState?.mode === "supersede" && (
          <QuoteForm
            rfqId={rfq.id}
            invitedSupplierIds={invitedSupplierIds}
            rfqLines={rfq.lines}
            base={drawerState.base}
            onSaved={(created) => setDrawerState({ mode: "view", id: created.id })}
            onCancel={closeDrawer}
          />
        )}
        {drawerState?.mode === "view" && (
          <QuoteDetail
            key={drawerState.id}
            id={drawerState.id}
            rfqStatus={rfq.status}
            canWrite={canWrite}
            canArchive={canArchive}
            onClosed={closeDrawer}
            onSupersede={(q) => setDrawerState({ mode: "supersede", base: q })}
          />
        )}
      </Drawer>
    </section>
  );
}
