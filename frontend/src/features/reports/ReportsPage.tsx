/** Reports workspace — routed at `/reports` (replaces the `PlaceholderPage`,
 * per this task's ALLOWED App.tsx edit): generate an export for a scenario,
 * then track/download every report this organization has generated.
 *
 * Design decisions:
 *  - **Scenario picker is RFQ-then-scenario, reusing
 *    `useRfqScenarios(rfqId)`** — the exact same two-step picker
 *    ../comparison/ComparisonPage.tsx already uses, per this task's own
 *    instruction to "reuse whatever scenario-listing call
 *    features/comparison already uses." There is no org-wide
 *    "list every scenario" endpoint in ../comparison/api.ts to pick from
 *    directly.
 *  - **The reports table has a manual "Refresh" control, not a poll
 *    timer.** `state` moves `pending` -> `ready`/`failed` asynchronously
 *    server-side with no push channel in this codebase (mirrors
 *    ../comparison/api.ts's own "no job queue exists" note for scenario
 *    solving) — a predictable manual refresh keeps this page's behavior
 *    observable in tests without timer-driven flakiness, the same call this
 *    codebase already makes everywhere else (DataTable's own `onRetry`).
 *  - **Download is a plain `<a href download>`**, never JS-triggered
 *    `window.location` navigation and never a fetch+blob dance — see
 *    ./api.ts's file header.
 */

import type { ColumnDef } from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../../auth/session";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { DataTable } from "../../components/DataTable";
import { FormField } from "../../components/FormField";
import { PaginationBar } from "../../components/PaginationBar";
import "../../components/workspace.css";
import { isAnalystOrAbove } from "../../lib/roles";
import { zodResolver } from "../../lib/zodResolver";
import { useRfqScenarios, type ScenarioSummaryResponse } from "../comparison/api";
import { useRfqs } from "../rfqs/api";
import {
  REPORT_FORMATS,
  REPORT_TYPES,
  reportContentUrl,
  useCreateReport,
  useReports,
  type ReportFormat,
  type ReportResponse,
  type ReportState,
  type ReportType,
} from "./api";
import "./reports.css";

const LIMIT = 20;

/** Bytes -> human size string. `size_bytes` is a plain JSON number/`null`
 * (see ./api.ts's header), so ordinary arithmetic is safe — duplicated
 * locally rather than importing ../documents/api.ts's `formatBytes`, same
 * "small pure helper per feature" convention ../comparison/ComparisonPage.tsx's
 * file header documents for its own `toPercentDisplay`. */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

const REPORT_TYPE_LABELS: Record<ReportType, string> = Object.fromEntries(
  REPORT_TYPES.map((t) => [t.value, t.label]),
) as Record<ReportType, string>;

const STATE_META: Record<ReportState, { label: string; tone: string }> = {
  pending: { label: "Pending", tone: "info" },
  ready: { label: "Ready", tone: "positive" },
  failed: { label: "Failed", tone: "destructive" },
};

function ReportStateBadge({ report }: { report: ReportResponse }) {
  if (report.purged) {
    return <span className="badge badge--report-purged">Expired / purged</span>;
  }
  const meta = STATE_META[report.state];
  return <span className={`badge badge--report-state-${meta.tone}`}>{meta.label}</span>;
}

function ReportDownloadButton({ report }: { report: ReportResponse }) {
  const disabled = report.state !== "ready" || report.purged;
  if (disabled) {
    return (
      <button type="button" className="btn-ghost-sm" disabled>
        Download
      </button>
    );
  }
  return (
    <a className="btn-secondary-sm" href={reportContentUrl(report.id)} download>
      Download
    </a>
  );
}

// -- generate form ----------------------------------------------------

const reportFormSchema = z.object({
  scenario_id: z.string().min(1, "Select a scenario"),
  report_type: z.string().min(1, "Select a report type"),
  format: z.string().min(1, "Select a format"),
});

type ReportFormValues = z.infer<typeof reportFormSchema>;

const emptyReportForm: ReportFormValues = { scenario_id: "", report_type: "", format: "" };

function GenerateReportForm({
  scenarios,
  scenariosEnabled,
  onCreated,
}: {
  scenarios: ScenarioSummaryResponse[];
  scenariosEnabled: boolean;
  onCreated: () => void;
}) {
  const createMutation = useCreateReport();
  const {
    register,
    handleSubmit,
    reset,
    setValue,
    formState: { errors },
  } = useForm<ReportFormValues>({
    resolver: zodResolver(reportFormSchema),
    defaultValues: emptyReportForm,
  });

  useEffect(() => {
    // The RFQ picker (outside this form) drives which scenarios are
    // selectable; clear a stale selection whenever that set changes.
    setValue("scenario_id", "");
  }, [scenarios, setValue]);

  async function onValid(values: ReportFormValues) {
    try {
      await createMutation.mutateAsync({
        scenario_id: values.scenario_id,
        report_type: values.report_type as ReportType,
        format: values.format as ReportFormat,
      });
      reset(emptyReportForm);
      onCreated();
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <div className="form-grid">
        <FormField label="Scenario" error={errors.scenario_id?.message}>
          <select {...register("scenario_id")} disabled={!scenariosEnabled}>
            <option value="">
              {scenariosEnabled ? "Select scenario…" : "Select an RFQ first"}
            </option>
            {scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Report type" error={errors.report_type?.message}>
          <select {...register("report_type")}>
            <option value="">Select type…</option>
            {REPORT_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Format" error={errors.format?.message}>
          <select {...register("format")}>
            <option value="">Select format…</option>
            {REPORT_FORMATS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </FormField>
      </div>
      <ApiErrorBanner error={createMutation.error} />
      <div className="form-actions">
        <button type="submit" className="btn-primary-sm" disabled={createMutation.isPending}>
          {createMutation.isPending ? "Generating…" : "Generate report"}
        </button>
      </div>
    </form>
  );
}

// -- page ---------------------------------------------------------------

export function ReportsPage() {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);

  const rfqsQuery = useRfqs({ limit: 100, offset: 0 });
  const [rfqId, setRfqId] = useState("");
  const scenariosQuery = useRfqScenarios(rfqId || null);
  const scenarios = scenariosQuery.data?.items ?? [];

  const [offset, setOffset] = useState(0);
  const reportsQuery = useReports(LIMIT, offset);

  function handleCreated() {
    setOffset(0);
    void reportsQuery.refetch();
  }

  const columns = useMemo<ColumnDef<ReportResponse>[]>(
    () => [
      {
        id: "report_type",
        accessorKey: "report_type",
        header: "Type",
        cell: (ctx) => REPORT_TYPE_LABELS[ctx.getValue<ReportType>()],
      },
      {
        id: "format",
        accessorKey: "format",
        header: "Format",
        cell: (ctx) => ctx.getValue<string>().toUpperCase(),
      },
      {
        id: "state",
        header: "State",
        enableSorting: false,
        cell: (ctx) => <ReportStateBadge report={ctx.row.original} />,
      },
      {
        id: "generated_at",
        accessorKey: "generated_at",
        header: "Generated",
        cell: (ctx) => {
          const v = ctx.getValue<string | null>();
          return v ? new Date(v).toLocaleString() : "—";
        },
      },
      {
        id: "size",
        accessorKey: "size_bytes",
        header: "Size",
        meta: { align: "right" },
        cell: (ctx) => {
          const v = ctx.getValue<number | null>();
          return v === null ? "—" : formatBytes(v);
        },
      },
      {
        id: "details",
        header: "Details",
        enableSorting: false,
        cell: (ctx) => {
          const r = ctx.row.original;
          if (r.purged) return <span className="detail-label">Content no longer available.</span>;
          if (r.state === "failed") {
            return (
              <span className="report-error-text">
                {r.error_message ?? "Report generation failed."}
              </span>
            );
          }
          return null;
        },
      },
      {
        id: "download",
        header: "",
        enableSorting: false,
        cell: (ctx) => <ReportDownloadButton report={ctx.row.original} />,
      },
    ],
    [],
  );

  return (
    <section className="reports-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <h1>Reports</h1>
          <p>
            Generate and download exports — supplier comparisons, CFO recommendations,
            negotiation briefs, scenario summaries, and audit history.
          </p>
        </div>
      </header>

      {canWrite && (
        <section className="detail-section">
          <h3 className="detail-section-title">Generate report</h3>
          <div className="form-grid">
            <FormField label="RFQ">
              <select value={rfqId} onChange={(e) => setRfqId(e.target.value)}>
                <option value="">Select RFQ…</option>
                {(rfqsQuery.data?.items ?? []).map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.internal_reference} — {r.name}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          <GenerateReportForm
            scenarios={scenarios}
            scenariosEnabled={rfqId !== ""}
            onCreated={handleCreated}
          />
        </section>
      )}

      <div className="reports-toolbar">
        <h3 className="detail-section-title" style={{ margin: 0 }}>
          Generated reports
        </h3>
        <button
          type="button"
          className="btn-ghost-sm"
          onClick={() => void reportsQuery.refetch()}
          disabled={reportsQuery.isFetching}
        >
          {reportsQuery.isFetching ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      <DataTable
        ariaLabel="Reports"
        columns={columns}
        data={reportsQuery.data?.items ?? []}
        getRowId={(r) => r.id}
        isLoading={reportsQuery.isLoading}
        isError={reportsQuery.isError}
        errorMessage={reportsQuery.error instanceof Error ? reportsQuery.error.message : undefined}
        onRetry={() => void reportsQuery.refetch()}
        emptyTitle="No reports yet"
        emptyDescription="Generate a report above to get started."
      />
      <PaginationBar
        total={reportsQuery.data?.page.total ?? 0}
        limit={LIMIT}
        offset={offset}
        onOffsetChange={setOffset}
      />
    </section>
  );
}
