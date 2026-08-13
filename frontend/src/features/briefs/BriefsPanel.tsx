/** Negotiation-briefs panel — attaches to the comparison workspace
 * (../comparison/ComparisonPage.tsx inserts this once a scenario's scoring
 * result is loaded, per this task's ALLOWED insertion point).
 *
 * Design decisions:
 *  - **Supplier pick-list comes from the caller, not a fresh fetch.** The
 *    task brief: "offer the pick-list from the scenario's scoring result
 *    already loaded in the workspace" — `ComparisonPage.tsx` already holds
 *    `scenario.scoring_result.scores` (every scored, non-excluded supplier
 *    carries `supplier_name` inline), so this panel takes a plain
 *    `{id, label}[]` prop instead of re-deriving it from another endpoint.
 *  - **The brief list only fetches summaries; the drawer fetches full
 *    detail on open** — see ./api.ts's file header for why (mirrors
 *    ../comparison/ScenarioHistory.tsx's summary-vs-detail split).
 *  - **The `draft_supplier_email` section key gets special-cased** in the
 *    fixed `SECTION_ORDER` walk: rather than the generic text/data
 *    rendering every other section gets, it renders the brief's top-level
 *    `draft_email_subject`/`draft_email_body` inside the mandated
 *    "this system never sends emails" callout with a copy-only button (no
 *    `mailto:` link, per the task brief) — its own `sections` entry still
 *    supplies the provenance badge shown alongside.
 *  - **"Mark reviewed" always posts `approved`** (default checked) plus
 *    optional notes; the two documented states (`draft`/`human_reviewed`)
 *    give no third "rejected" outcome to model, so one form covers the
 *    brief's whole review action.
 */

import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { DetailRow } from "../../components/DetailRow";
import { Drawer } from "../../components/Drawer";
import { FormField } from "../../components/FormField";
import "../../components/workspace.css";
import { formatMoney, isDecimalString } from "../../lib/money";
import { zodResolver } from "../../lib/zodResolver";
import {
  SECTION_ORDER,
  useBrief,
  useCreateBrief,
  useReviewBrief,
  useScenarioBriefs,
  type BriefProvenance,
  type BriefResponse,
  type BriefSectionKey,
  type BriefSectionResponse,
  type BriefState,
  type BriefTargetsInput,
} from "./api";
import "./briefs.css";

export interface BriefSupplierOption {
  id: string;
  label: string;
}

const PROVENANCE_LABELS: Record<BriefProvenance, string> = {
  supplier_provided: "Supplier-provided",
  user_assumption: "User assumption",
  calculated: "Calculated",
  ai_narrative: "AI narrative",
  missing: "Missing",
};

function ProvenanceBadge({ provenance }: { provenance: BriefProvenance }) {
  return (
    <span className={`badge badge--brief-provenance-${provenance}`}>
      {PROVENANCE_LABELS[provenance]}
    </span>
  );
}

const BRIEF_STATE_LABELS: Record<BriefState, string> = {
  draft: "Draft",
  human_reviewed: "Human reviewed",
};

function BriefStateBadge({ state }: { state: BriefState }) {
  return <span className={`badge badge--brief-state-${state}`}>{BRIEF_STATE_LABELS[state]}</span>;
}

const SPECIAL_SECTION_LABELS: Partial<Record<BriefSectionKey, string>> = {
  batna: "BATNA",
};

function sectionLabel(key: BriefSectionKey): string {
  return (
    SPECIAL_SECTION_LABELS[key] ??
    key
      .split("_")
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ")
  );
}

/** Renders a section's free-form `data` JSONB defensively — same "genuinely
 * untyped, narrow at the render site" convention
 * ../comparison/ScenarioHistory.tsx documents for its own snapshot fields. */
function dataEntryValue(v: unknown): string {
  if (v === null) return "null";
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

function BriefSectionRow({
  sectionKey,
  section,
}: {
  sectionKey: BriefSectionKey;
  section: BriefSectionResponse;
}) {
  const dataEntries = section.data ? Object.entries(section.data) : [];
  return (
    <div className="brief-section-row">
      <div className="brief-section-header">
        <span className="brief-section-title">{sectionLabel(sectionKey)}</span>
        <ProvenanceBadge provenance={section.provenance} />
      </div>
      {section.text ? (
        <p className="brief-section-text">{section.text}</p>
      ) : (
        <p className="brief-section-text brief-section-text--empty">Not available.</p>
      )}
      {dataEntries.length > 0 && (
        <ul className="brief-data-list">
          {dataEntries.map(([k, v]) => (
            <li className="brief-data-list-item" key={k}>
              <strong>{k}:</strong> {dataEntryValue(v)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DraftEmailSection({
  brief,
  onCopy,
  copied,
}: {
  brief: BriefResponse;
  onCopy: () => void;
  copied: boolean;
}) {
  const section = brief.sections.draft_supplier_email;
  const hasEmail = brief.draft_email_subject !== null || brief.draft_email_body !== null;
  return (
    <div className="brief-section-row">
      <div className="brief-section-header">
        <span className="brief-section-title">{sectionLabel("draft_supplier_email")}</span>
        <ProvenanceBadge provenance={section?.provenance ?? "missing"} />
      </div>
      <div className="brief-email-callout" role="note">
        <strong>DRAFT — this system never sends emails.</strong> Review the content below and send
        it manually from your own email client.
      </div>
      {hasEmail ? (
        <div className="brief-email-content">
          <div>
            <span className="detail-label">Subject</span>
            <p className="detail-value">{brief.draft_email_subject || "—"}</p>
          </div>
          <div>
            <span className="detail-label">Body</span>
            <pre className="brief-email-body-text">{brief.draft_email_body || "—"}</pre>
          </div>
          <div>
            <button type="button" className="btn-secondary-sm" onClick={onCopy}>
              {copied ? "Copied" : "Copy email to clipboard"}
            </button>
          </div>
        </div>
      ) : (
        <p className="detail-label">No draft email was generated.</p>
      )}
      {section?.text && <p className="brief-section-text">{section.text}</p>}
    </div>
  );
}

const targetFieldNames = ["price_target", "stretch_target", "walk_away_threshold"] as const;

const briefFormSchema = z
  .object({
    supplier_id: z.string().min(1, "Select a supplier"),
    objective: z.string(),
    price_target: z.string(),
    stretch_target: z.string(),
    walk_away_threshold: z.string(),
  })
  .superRefine((val, ctx) => {
    for (const key of targetFieldNames) {
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

type BriefFormValues = z.infer<typeof briefFormSchema>;

const emptyBriefForm: BriefFormValues = {
  supplier_id: "",
  objective: "",
  price_target: "",
  stretch_target: "",
  walk_away_threshold: "",
};

function GenerateBriefForm({
  scenarioId,
  supplierOptions,
  onCreated,
}: {
  scenarioId: string;
  supplierOptions: BriefSupplierOption[];
  onCreated: (brief: BriefResponse) => void;
}) {
  const createMutation = useCreateBrief();
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<BriefFormValues>({
    resolver: zodResolver(briefFormSchema),
    defaultValues: emptyBriefForm,
  });

  async function onValid(values: BriefFormValues) {
    const targets: BriefTargetsInput = {};
    if (values.price_target.trim()) targets.price_target = values.price_target.trim();
    if (values.stretch_target.trim()) targets.stretch_target = values.stretch_target.trim();
    if (values.walk_away_threshold.trim())
      targets.walk_away_threshold = values.walk_away_threshold.trim();
    try {
      const brief = await createMutation.mutateAsync({
        scenarioId,
        supplierId: values.supplier_id,
        objective: values.objective.trim() || null,
        targets: Object.keys(targets).length > 0 ? targets : null,
      });
      reset(emptyBriefForm);
      onCreated(brief);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <form className="brief-generate-form" onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <div className="form-grid">
        <FormField label="Supplier" error={errors.supplier_id?.message}>
          <select {...register("supplier_id")}>
            <option value="">Select supplier…</option>
            {supplierOptions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Objective" hint="optional" full>
          <textarea
            {...register("objective")}
            placeholder="e.g. Reduce unit price 8% while holding lead time"
          />
        </FormField>
      </div>
      <p className="brief-targets-hint">
        Target overrides below are your assumption — will be marked{" "}
        <span className="badge badge--brief-provenance-user_assumption">User assumption</span> in
        the brief.
      </p>
      <div className="form-grid">
        <FormField label="Price target" hint="optional" error={errors.price_target?.message}>
          <input {...register("price_target")} inputMode="decimal" placeholder="not stated" />
        </FormField>
        <FormField label="Stretch target" hint="optional" error={errors.stretch_target?.message}>
          <input {...register("stretch_target")} inputMode="decimal" placeholder="not stated" />
        </FormField>
        <FormField
          label="Walk-away threshold"
          hint="optional"
          error={errors.walk_away_threshold?.message}
        >
          <input
            {...register("walk_away_threshold")}
            inputMode="decimal"
            placeholder="not stated"
          />
        </FormField>
      </div>
      <ApiErrorBanner error={createMutation.error} />
      <div className="form-actions">
        <button type="submit" className="btn-primary-sm" disabled={createMutation.isPending}>
          {createMutation.isPending ? "Generating…" : "Generate brief"}
        </button>
      </div>
    </form>
  );
}

function BriefDrawer({
  briefId,
  open,
  onClose,
  canReview,
}: {
  briefId: string | null;
  open: boolean;
  onClose: () => void;
  canReview: boolean;
}) {
  const briefQuery = useBrief(open ? briefId : null);
  const reviewMutation = useReviewBrief();
  const [approved, setApproved] = useState(true);
  const [notes, setNotes] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!open) {
      setApproved(true);
      setNotes("");
      setCopied(false);
    }
  }, [open, briefId]);

  const brief = briefQuery.data;

  async function handleCopyEmail() {
    if (!brief) return;
    const text = `Subject: ${brief.draft_email_subject ?? ""}\n\n${brief.draft_email_body ?? ""}`;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // clipboard permission denied/unavailable — button stays actionable
    }
  }

  async function handleMarkReviewed() {
    if (!brief) return;
    try {
      await reviewMutation.mutateAsync({
        id: brief.id,
        scenarioId: brief.scenario_id,
        approved,
        reviewerNotes: notes.trim() || null,
      });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <Drawer open={open} onClose={onClose} title="Negotiation brief">
      {briefQuery.isLoading && <p className="detail-label">Loading…</p>}
      <ApiErrorBanner error={briefQuery.error} />
      {brief && (
        <div className="brief-detail">
          {brief.requires_review && (
            <div className="brief-review-banner" role="status">
              Draft — requires human review
            </div>
          )}
          {brief.simulated && (
            <div className="brief-simulated-label">
              Template narrative — deterministic, not AI-generated
            </div>
          )}

          <div className="brief-meta-row">
            <BriefStateBadge state={brief.state} />
            <span className="detail-label">Narrative provider: {brief.narrative_provider}</span>
            <span className="detail-label">v{brief.version}</span>
          </div>

          {(brief.price_target !== null ||
            brief.stretch_target !== null ||
            brief.walk_away_threshold !== null) && (
            <div className="detail-grid brief-targets-grid">
              {brief.price_target !== null && (
                <DetailRow label="Price target" value={formatMoney(brief.price_target)} num />
              )}
              {brief.stretch_target !== null && (
                <DetailRow label="Stretch target" value={formatMoney(brief.stretch_target)} num />
              )}
              {brief.walk_away_threshold !== null && (
                <DetailRow
                  label="Walk-away threshold"
                  value={formatMoney(brief.walk_away_threshold)}
                  num
                />
              )}
            </div>
          )}

          <div className="brief-sections">
            {SECTION_ORDER.map((key) => {
              if (key === "draft_supplier_email") {
                return (
                  <DraftEmailSection
                    key={key}
                    brief={brief}
                    onCopy={() => void handleCopyEmail()}
                    copied={copied}
                  />
                );
              }
              const section = brief.sections[key];
              if (!section) return null;
              return <BriefSectionRow key={key} sectionKey={key} section={section} />;
            })}
          </div>

          {brief.state === "human_reviewed" ? (
            <section className="detail-section brief-review-form">
              <h3 className="detail-section-title">Review</h3>
              <p className="detail-value">
                Reviewed {brief.reviewed_at ? new Date(brief.reviewed_at).toLocaleString() : ""}
                {brief.reviewed_by_id ? (
                  <>
                    {" by "}
                    {/* the resolved name, falling back to a truncated id (full
                        value on hover) when the reviewer can't be resolved —
                        a bare UUID here was a 2026-08 external-review P3 */}
                    <span title={brief.reviewed_by_id}>
                      {brief.reviewed_by_full_name ?? `${brief.reviewed_by_id.slice(0, 8)}…`}
                    </span>
                  </>
                ) : null}
                .
              </p>
            </section>
          ) : (
            canReview && (
              <section className="detail-section brief-review-form">
                <h3 className="detail-section-title">Mark reviewed</h3>
                <FormField label="Approved" checkbox>
                  <input
                    type="checkbox"
                    checked={approved}
                    onChange={(e) => setApproved(e.target.checked)}
                  />
                </FormField>
                <FormField label="Reviewer notes" hint="optional">
                  <textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="Notes for the record"
                  />
                </FormField>
                <ApiErrorBanner error={reviewMutation.error} />
                <div className="form-actions">
                  <button
                    type="button"
                    className="btn-primary-sm"
                    onClick={() => void handleMarkReviewed()}
                    disabled={reviewMutation.isPending}
                  >
                    {reviewMutation.isPending ? "Saving…" : "Mark reviewed"}
                  </button>
                </div>
              </section>
            )
          )}
        </div>
      )}
    </Drawer>
  );
}

export function BriefsPanel({
  scenarioId,
  supplierOptions,
  canWrite,
}: {
  scenarioId: string;
  supplierOptions: BriefSupplierOption[];
  canWrite: boolean;
}) {
  const listQuery = useScenarioBriefs(scenarioId);
  const [openBriefId, setOpenBriefId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const items = listQuery.data?.items ?? [];

  function supplierLabel(id: string): string {
    return supplierOptions.find((s) => s.id === id)?.label ?? id;
  }

  function handleCreated(brief: BriefResponse) {
    setShowForm(false);
    setOpenBriefId(brief.id);
  }

  return (
    <section className="detail-section briefs-panel">
      <div className="detail-header-row">
        <h3 className="detail-section-title">Negotiation briefs</h3>
        {canWrite && supplierOptions.length > 0 && (
          <button
            type="button"
            className="btn-secondary-sm"
            onClick={() => setShowForm((v) => !v)}
          >
            {showForm ? "Cancel" : "Generate brief"}
          </button>
        )}
      </div>

      {canWrite && supplierOptions.length === 0 && (
        <p className="detail-label">
          No scored, non-excluded suppliers are available to brief for this scenario.
        </p>
      )}

      {showForm && (
        <GenerateBriefForm
          scenarioId={scenarioId}
          supplierOptions={supplierOptions}
          onCreated={handleCreated}
        />
      )}

      {listQuery.isLoading ? (
        <p className="detail-label">Loading briefs…</p>
      ) : items.length === 0 ? (
        <p className="detail-label">No negotiation briefs have been generated for this scenario yet.</p>
      ) : (
        <ul className="briefs-list">
          {items.map((b) => (
            <li key={b.id}>
              <button
                type="button"
                className="briefs-list-row"
                onClick={() => setOpenBriefId(b.id)}
              >
                <span className="briefs-list-supplier">{supplierLabel(b.supplier_id)}</span>
                <BriefStateBadge state={b.state} />
                {b.requires_review && (
                  <span className="badge badge--brief-needs-review">Needs review</span>
                )}
                {b.simulated && <span className="badge badge--brief-simulated">Template</span>}
                <span className="detail-label">{new Date(b.created_at).toLocaleDateString()}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <BriefDrawer
        briefId={openBriefId}
        open={openBriefId !== null}
        onClose={() => setOpenBriefId(null)}
        canReview={canWrite}
      />
    </section>
  );
}
