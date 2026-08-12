/** Overview workspace — routed at "/" (replaces the `PlaceholderPage` that
 * used to sit at the index route, per this task's ALLOWED App.tsx edit; P1
 * audit finding: "the Overview screen is still an under-construction
 * placeholder").
 *
 * Design decisions:
 *  - **Three KPI tiles, not a dashboard of charts.** MASTER.md's "KPI
 *    cards" spec ("compact, label + value") plus this task's own "keep it
 *    lean and dense; no charts" instruction — suppliers/parts/open-RFQ
 *    totals are each a `page.total` from a `limit=1` list call (./api.ts),
 *    the cheapest honest count without a dedicated stats endpoint.
 *  - **"Recent scenarios" shows the latest RFQ's own scenario history, not
 *    a fabricated org-wide feed.** See ./api.ts's file header for why no
 *    "list every scenario" endpoint exists to build one from, and why an
 *    unbounded per-RFQ scan to fake one is exactly the N+1 this codebase
 *    avoids elsewhere.
 *  - **Quick links mirror AppShell's own NAV list** (minus Overview
 *    itself) — the workspace's actual navigation vocabulary, not an
 *    independently invented set of destinations.
 *  - **The "Demo — synthetic data" posture is not duplicated as a second
 *    badge.** AppShell.tsx's header already renders that badge globally
 *    (layout/** is out of scope for this task); this page's subtitle only
 *    echoes the same fact in one line of text when `session.demo_mode` is
 *    true, rather than re-implementing the badge.
 */

import { Link } from "react-router-dom";
import { useAuth } from "../../auth/session";
import { useEntranceStagger } from "../../lib/motion";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import "../../components/badges.css";
import { PageEyebrow, type HueName } from "../../components/PageEyebrow";
import "../../components/workspace.css";
import { COMPARISON_STRATEGIES, useRfqScenarios, type ScenarioState } from "../comparison/api";
import "./overview.css";
import { useMostRecentRfq, useOpenRfqTotal, usePartTotal, useSupplierTotal } from "./api";

const RECENT_SCENARIOS_LIMIT = 5;

// `hue` (v2 extension) mirrors AppShell's own NAV list — see that file's
// comment: only the seven design-director-assigned workspaces get one, BOMs
// and FX rates render as before (no `overview-link-card--*` modifier applied).
const WORKSPACE_LINKS: { to: string; label: string; description: string; hue?: HueName }[] = [
  { to: "/suppliers", label: "Suppliers", description: "Master data, performance, and contacts.", hue: "suppliers" },
  { to: "/parts", label: "Parts", description: "Catalogue master data and approved alternatives.", hue: "parts" },
  { to: "/boms", label: "BOMs", description: "Bills of materials and version history." },
  { to: "/rfqs", label: "RFQs", description: "Supplier outreach, from draft through award.", hue: "rfqs" },
  { to: "/fx", label: "FX rates", description: "Foreign-exchange rates used in landed cost." },
  { to: "/scenarios", label: "Compare", description: "Score and allocate supplier quotes.", hue: "compare" },
  { to: "/reports", label: "Reports", description: "Generate and download exports.", hue: "reports" },
  { to: "/audit", label: "Audit log", description: "Full change history across the workspace.", hue: "audit" },
];

// The end-to-end workflow spelled out for a first-time user — the reviewer's
// "biggest missing UX element". Each step deep-links to where it happens.
const WORKFLOW_STEPS: { to: string; title: string; description: string }[] = [
  { to: "/suppliers", title: "Add suppliers & parts", description: "Set up your vendors and the parts you buy — or start from the seeded demo data." },
  { to: "/rfqs", title: "Create an RFQ & invite suppliers", description: "Open a request for quotation, add the lines you need, and invite vendors to quote." },
  { to: "/rfqs", title: "Enter or upload quotes", description: "Type quotes in by hand, or upload a supplier's document and let extraction read it." },
  { to: "/scenarios", title: "Set assumptions & run a scenario", description: "Fill in any costs the quotes don't state, then compute landed cost and the best allocation." },
  { to: "/scenarios", title: "Review the allocation & brief", description: "See who wins each line and why, and generate a negotiation brief (nothing is ever emailed)." },
  { to: "/reports", title: "Generate reports", description: "Export a supplier comparison or CFO recommendation as CSV, XLSX, or PDF." },
];

const SCENARIO_STATE_LABELS: Record<ScenarioState, string> = {
  draft: "Draft",
  running: "Running",
  complete: "Complete",
  failed: "Failed",
};

function ScenarioStateBadge({ state }: { state: ScenarioState }) {
  return <span className={`badge badge--overview-scenario-${state}`}>{SCENARIO_STATE_LABELS[state]}</span>;
}

function strategyLabel(strategy: string): string {
  return COMPARISON_STRATEGIES.find((s) => s.value === strategy)?.label ?? strategy;
}

/** Structural match for whatever `useSuppliers`/`useParts`/`useRfqs` return
 * — every one of those list hooks' response carries `page.total`, so this
 * card doesn't need to know which resource it's counting. */
interface TotalQuery {
  isLoading: boolean;
  isError: boolean;
  data?: { page: { total: number } } | undefined;
}

function totalDisplay(query: TotalQuery): string {
  if (query.isLoading) return "…";
  if (query.isError) return "—";
  return (query.data?.page.total ?? 0).toLocaleString();
}

function KpiCard({
  label,
  query,
  to,
  linkLabel,
  hue,
}: {
  label: string;
  query: TotalQuery;
  to: string;
  linkLabel: string;
  hue: HueName;
}) {
  return (
    <div className={`overview-kpi-card overview-kpi-card--${hue}`}>
      <span className="overview-kpi-label">{label}</span>
      <span
        className={`overview-kpi-value overview-kpi-value--${hue}`}
        title={query.isError ? "Failed to load — try refreshing." : undefined}
      >
        {totalDisplay(query)}
      </span>
      <Link className="overview-kpi-link" to={to}>
        {linkLabel} →
      </Link>
    </div>
  );
}

function RecentScenariosCard() {
  const recentRfqQuery = useMostRecentRfq();
  const recentRfq = recentRfqQuery.data?.items[0] ?? null;
  const scenariosQuery = useRfqScenarios(recentRfq?.id ?? null);
  const scenarios = (scenariosQuery.data?.items ?? []).slice(0, RECENT_SCENARIOS_LIMIT);

  return (
    // v2 hue extension: this is the page's PRIMARY card (the "Workspaces"
    // quick-link card beside it is secondary), so it's the one that gets
    // the 3px hue border-top — "one per page, not every card".
    <section className="detail-section overview-card hue-panel-top hue-panel-top--overview">
      <div className="detail-header-row">
        <h3 className="detail-section-title">Recent scenarios</h3>
        <Link className="btn-ghost-sm" to="/scenarios">
          Open Compare
        </Link>
      </div>

      {recentRfqQuery.isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : recentRfqQuery.isError ? (
        <ApiErrorBanner error={recentRfqQuery.error} />
      ) : !recentRfq ? (
        <p className="detail-label">No RFQs yet — create one to start comparing suppliers.</p>
      ) : (
        <>
          <p className="overview-recent-context detail-label">
            Latest RFQ: <span className="detail-value mono">{recentRfq.internal_reference}</span> —{" "}
            {recentRfq.name}
          </p>
          {scenariosQuery.isLoading ? (
            <p className="detail-label">Loading scenarios…</p>
          ) : scenariosQuery.isError ? (
            <ApiErrorBanner error={scenariosQuery.error} />
          ) : scenarios.length === 0 ? (
            <p className="detail-label">
              No scenarios yet for this RFQ — open Compare to score and allocate suppliers.
            </p>
          ) : (
            <ul className="overview-scenario-list">
              {scenarios.map((s) => (
                <li key={s.id} className="overview-scenario-row">
                  <span className="overview-scenario-name">{s.name}</span>
                  <span className="detail-label">{strategyLabel(s.strategy)}</span>
                  <ScenarioStateBadge state={s.state} />
                  <span className="detail-label">{new Date(s.created_at).toLocaleDateString()}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

export function OverviewPage() {
  const { session } = useAuth();
  const supplierTotalQuery = useSupplierTotal();
  const partTotalQuery = usePartTotal();
  const openRfqTotalQuery = useOpenRfqTotal();

  // Sanctioned motion pattern 2 (lib/motion.ts): KPI cards, the workflow
  // steps, and the two lower cards fade up in sequence on arrival. Mount-time
  // only — count refetches never re-trigger it.
  const pageRef = useEntranceStagger<HTMLElement>(
    ".overview-kpi-card, .overview-workflow-step, .overview-columns > .overview-card",
  );

  return (
    <section className="overview-page" ref={pageRef}>
      <header className="page-toolbar">
        <div className="page-heading">
          <PageEyebrow hue="overview">Overview</PageEyebrow>
          <h1>Overview</h1>
          <p>
            Workspace summary for {session?.organization_name ?? "your organization"}.
            {session?.demo_mode ? " Demo — synthetic data throughout." : ""}
          </p>
        </div>
      </header>

      <div className="overview-kpi-row">
        <KpiCard label="Suppliers" query={supplierTotalQuery} to="/suppliers" linkLabel="View suppliers" hue="suppliers" />
        <KpiCard label="Parts" query={partTotalQuery} to="/parts" linkLabel="View parts" hue="parts" />
        <KpiCard label="Open RFQs" query={openRfqTotalQuery} to="/rfqs" linkLabel="View RFQs" hue="rfqs" />
      </div>

      <section className="detail-section overview-card overview-workflow">
        <h3 className="detail-section-title">How it works</h3>
        <p className="detail-label">
          The workflow, end to end — each step links to where you do it.
        </p>
        <ol className="overview-workflow-list">
          {WORKFLOW_STEPS.map((step, i) => (
            // keyed by title: several steps deep-link to the same route
            <li key={step.title} className="overview-workflow-step">
              <span className="overview-workflow-num" aria-hidden="true">
                {i + 1}
              </span>
              <div className="overview-workflow-body">
                <Link className="overview-workflow-title" to={step.to}>
                  {step.title}
                </Link>
                <span className="detail-label">{step.description}</span>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <div className="overview-columns">
        <RecentScenariosCard />

        <section className="detail-section overview-card">
          <h3 className="detail-section-title">Workspaces</h3>
          <nav className="overview-links" aria-label="Workspace quick links">
            {WORKSPACE_LINKS.map((l) => (
              <Link
                key={l.to}
                to={l.to}
                className={l.hue ? `overview-link-card overview-link-card--${l.hue}` : "overview-link-card"}
              >
                <span className="overview-link-label">{l.label}</span>
                <span className="detail-label">{l.description}</span>
              </Link>
            ))}
          </nav>
        </section>
      </div>
    </section>
  );
}
