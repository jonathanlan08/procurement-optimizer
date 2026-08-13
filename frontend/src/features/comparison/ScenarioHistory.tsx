/** Scenario history - saved `ComparisonScenario` rows for one RFQ
 * (`GET /rfqs/{id}/comparison-scenarios`), with detail expansion, "Clone &
 * re-run" (analyst+), and archive (admin+).
 *
 * Design decisions:
 *  - **Expanding a row IS selecting it.** `ScenarioSummaryResponse` (the
 *    list shape) carries no snapshots and no allocation/scoring payload -
 *    showing the snapshot summary at all requires the same `GET
 *    /comparison-scenarios/{id}` fetch the brief's "selecting a history row
 *    loads its stored results into the panels" behavior needs, so one
 *    expand toggle drives both: `onSelectSaved(fullScenario)` fires once
 *    the detail query resolves, `onSelectSaved(null)` fires on collapse.
 *  - **No expected-cost column in the collapsed row.** `ScenarioSummaryResponse`
 *    has no such field (only the full detail fetch's `allocation_result.
 *    objective_total_cost` does) - see ./api.ts's file header for the full
 *    reasoning. Faking it would mean an N+1 GET per row on every render;
 *    this follows the same "flag the gap, don't fake the list" call
 *    `RfqsPage.tsx`'s own file header already makes for its own list-vs-
 *    detail shape gap. The true expected cost renders once a row expands.
 *  - **The collapsed status badge shows `state`** (draft/running/complete/
 *    failed), not the granular `solver_status` (optimal/feasible/
 *    infeasible/error) AllocationPanel.tsx uses - same reason: only `state`
 *    exists on the summary row. The true solver status is visible in the
 *    AllocationPanel once a row is expanded/selected.
 *  - **"Clone & re-run" does not open the creation form** - `POST
 *    /comparison-scenarios/{id}/clone` re-solves immediately from the
 *    original's stored snapshots with no editable step in between (api/v1/
 *    scenarios.py's own module docstring, "Deviation 2": "a NEW scenario,
 *    freshly solved... not a draft pre-fill"), so there is nothing for an
 *    intermediate form to collect. The clone's result is treated as a fresh
 *    live run (`onCloned`), not a "saved result" - it is functionally
 *    identical to pressing "Run scenario" again with the original's inputs.
 */

import { useEffect, useState } from "react";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import "../../components/workspace.css";
import {
  useArchiveScenario,
  useCloneScenario,
  useRfqScenarios,
  useScenario,
  type ScenarioResponse,
  type ScenarioState,
  type ScenarioSummaryResponse,
} from "./api";
import { formatDecimal, formatMoney, isDecimalString } from "../../lib/money";
import "./comparison.css";

function statusLabel(raw: string): string {
  return raw
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

const STATE_META: Record<ScenarioState, { label: string; tone: string }> = {
  draft: { label: "Draft", tone: "muted" },
  running: { label: "Running", tone: "info" },
  complete: { label: "Complete", tone: "positive" },
  failed: { label: "Failed", tone: "destructive" },
};

function ScenarioStateBadge({ state }: { state: ScenarioState }) {
  const meta = STATE_META[state];
  return <span className={`badge badge--scenario-state-${meta.tone}`}>{meta.label}</span>;
}

function formatDateTime(raw: string): string {
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

/** Renders a `dict[str, Any]`/`list[Any]` JSONB snapshot field defensively -
 * see ./api.ts's file header on why these are genuinely untyped. */
function fxRateLabel(raw: unknown): string {
  if (typeof raw !== "object" || raw === null) return JSON.stringify(raw);
  const fx = raw as Record<string, unknown>;
  const from = typeof fx.source_currency === "string" ? fx.source_currency : "?";
  const to = typeof fx.target_currency === "string" ? fx.target_currency : "?";
  const rate = typeof fx.provider_rate === "string" ? fx.provider_rate : String(fx.provider_rate ?? "?");
  return `${from} → ${to} @ ${formatDecimal(rate)}`;
}

function weightLabel(raw: unknown): string {
  if (typeof raw !== "object" || raw === null) return JSON.stringify(raw);
  const w = raw as Record<string, unknown>;
  const label = typeof w.label === "string" && w.label ? w.label : String(w.criterion ?? "?");
  const weight = typeof w.weight === "string" ? w.weight : String(w.weight ?? "?");
  return `${label} (${weight})`;
}

function ScenarioSnapshotSummary({ scenario }: { scenario: ScenarioResponse }) {
  const assumptions = scenario.assumptions_snapshot;
  const setAssumptions = Object.entries(assumptions).filter(
    ([key, value]) => key !== "assume_missing_costs_zero" && value !== null,
  );

  return (
    <div className="scenario-snapshot-summary">
      <div className="scenario-snapshot-block">
        <span className="detail-label">Assumptions used</span>
        {setAssumptions.length === 0 ? (
          <p className="detail-value">None supplied.</p>
        ) : (
          <ul className="explain-list">
            {setAssumptions.map(([key, value]) => (
              <li className="explain-list-item" key={key}>
                {statusLabel(key)}:{" "}
                <span className="mono">
                  {typeof value === "string" && isDecimalString(value)
                    ? formatDecimal(value)
                    : String(value)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="scenario-snapshot-block">
        <span className="detail-label">Weights used</span>
        {scenario.weights_snapshot.length === 0 ? (
          <p className="detail-value">None recorded.</p>
        ) : (
          <div className="chip-row">
            {scenario.weights_snapshot.map((w, i) => (
              <span className="chip" key={i}>
                {weightLabel(w)}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="scenario-snapshot-block">
        <span className="detail-label">FX rates used</span>
        {scenario.fx_snapshot.length === 0 ? (
          <p className="detail-value">No currency conversion was needed.</p>
        ) : (
          <ul className="explain-list">
            {scenario.fx_snapshot.map((fx, i) => (
              <li className="explain-list-item" key={i}>
                {fxRateLabel(fx)}
              </li>
            ))}
          </ul>
        )}
      </div>
      {scenario.allocation_result.objective_total_cost && (
        <div className="scenario-snapshot-block">
          <span className="detail-label">Expected total cost</span>
          <p className="detail-value num" title={scenario.allocation_result.objective_total_cost.amount}>
            {formatMoney(scenario.allocation_result.objective_total_cost.amount, {
              currency: scenario.allocation_result.objective_total_cost.currency,
            })}
          </p>
        </div>
      )}
    </div>
  );
}

function ScenarioHistoryRow({
  row,
  expanded,
  onToggle,
  canClone,
  canArchive,
  onCloned,
  rfqId,
}: {
  row: ScenarioSummaryResponse;
  expanded: boolean;
  onToggle: () => void;
  canClone: boolean;
  canArchive: boolean;
  onCloned: (scenario: ScenarioResponse) => void;
  rfqId: string;
}) {
  const detailQuery = useScenario(expanded ? row.id : null);
  const cloneMutation = useCloneScenario();
  const archiveMutation = useArchiveScenario();
  const [archiving, setArchiving] = useState(false);
  const [archiveReason, setArchiveReason] = useState("");

  async function handleClone() {
    try {
      const cloned = await cloneMutation.mutateAsync({ rfqId, scenarioId: row.id });
      onCloned(cloned);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  async function handleArchiveConfirm() {
    if (archiveReason.trim() === "") return;
    try {
      await archiveMutation.mutateAsync({ rfqId, scenarioId: row.id, reason: archiveReason.trim() });
      setArchiving(false);
      setArchiveReason("");
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <li className="scenario-history-row">
      <button
        type="button"
        className="scenario-history-row-header"
        aria-expanded={expanded}
        onClick={onToggle}
      >
        <span className="scenario-history-name">{row.name}</span>
        <span className="scenario-history-strategy">{statusLabel(row.strategy)}</span>
        <ScenarioStateBadge state={row.state} />
        <span className="scenario-history-date">{formatDateTime(row.created_at)}</span>
        {row.is_archived && <span className="badge badge--archived">Archived</span>}
      </button>

      {expanded && (
        <div className="scenario-history-detail">
          {detailQuery.isLoading && <p className="detail-label">Loading scenario…</p>}
          <ApiErrorBanner error={detailQuery.error} />
          {detailQuery.data && <ScenarioSnapshotSummary scenario={detailQuery.data} />}

          <div className="scenario-history-actions">
            {canClone && !row.is_archived && (
              <button
                type="button"
                className="btn-secondary-sm"
                onClick={() => void handleClone()}
                disabled={cloneMutation.isPending}
              >
                {cloneMutation.isPending ? "Cloning…" : "Clone & re-run"}
              </button>
            )}
            {canArchive && !row.is_archived && !archiving && (
              <button type="button" className="btn-danger-sm" onClick={() => setArchiving(true)}>
                Archive
              </button>
            )}
          </div>
          <ApiErrorBanner error={cloneMutation.error} />

          {archiving && (
            <div className="archive-panel">
              <label className="field">
                <span className="field-label">Archive reason</span>
                <textarea
                  value={archiveReason}
                  onChange={(e) => setArchiveReason(e.target.value)}
                  placeholder="Why is this scenario being archived?"
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
              <ApiErrorBanner error={archiveMutation.error} />
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export function ScenarioHistory({
  rfqId,
  canClone,
  canArchive,
  onSelectSaved,
  onCloned,
}: {
  rfqId: string;
  canClone: boolean;
  canArchive: boolean;
  onSelectSaved: (scenario: ScenarioResponse | null) => void;
  onCloned: (scenario: ScenarioResponse) => void;
}) {
  const listQuery = useRfqScenarios(rfqId);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const detailQuery = useScenario(expandedId);

  useEffect(() => {
    if (expandedId !== null && detailQuery.data) {
      onSelectSaved(detailQuery.data);
    }
  }, [expandedId, detailQuery.data, onSelectSaved]);

  function toggle(id: string) {
    if (expandedId === id) {
      setExpandedId(null);
      onSelectSaved(null);
    } else {
      setExpandedId(id);
    }
  }

  function handleCloned(scenario: ScenarioResponse) {
    setExpandedId(null);
    onCloned(scenario);
  }

  const items = listQuery.data?.items ?? [];

  return (
    <section className="detail-section">
      <h3 className="detail-section-title">Scenario history</h3>
      {listQuery.isLoading ? (
        <p className="detail-label">Loading scenario history…</p>
      ) : items.length === 0 ? (
        <p className="detail-label">No scenarios have been run for this RFQ yet.</p>
      ) : (
        <ul className="scenario-history-list">
          {items.map((row) => (
            <ScenarioHistoryRow
              key={row.id}
              row={row}
              rfqId={rfqId}
              expanded={expandedId === row.id}
              onToggle={() => toggle(row.id)}
              canClone={canClone}
              canArchive={canArchive}
              onCloned={handleCloned}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
