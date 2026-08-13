/** Scenario surface tests: status banners (optimal/feasible/infeasible),
 * the infeasibility explainer's relaxation callout, allocation-table decimal
 * rendering, scenario-history role gating (Clone & re-run / Archive), and
 * saved-result version stamps. Companion to comparison.test.tsx, which keeps
 * the landed-cost comparison table + scoring-result rendering tests; split
 * out per the instruction ("extend comparison.test.tsx or add
 * allocation.test.tsx") to keep each file focused on one half of the page.
 *
 * Fixtures are intentionally self-contained (not shared with
 * comparison.test.tsx) - same "every test file owns its own fixtures"
 * convention that file's own header attributes to features/fx/fx.test.tsx.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { ComparisonPage } from "./ComparisonPage";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sessionFor(role: string): SessionInfo {
  return {
    user_id: "user-1",
    email: "analyst@example.com",
    full_name: "Test User",
    organization_id: "org-1",
    organization_name: "Test Org",
    organization_slug: "test-org",
    role,
    csrf_token: "csrf-token-abc",
    demo_mode: false,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ComparisonPage />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

type FetchHandler = (url: string, init: RequestInit | undefined) => Response | Promise<Response>;

function installFetchMock(
  handlers: Array<{ test: (url: string, method: string) => boolean; respond: FetchHandler }>,
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const handler = handlers.find((h) => h.test(url, method));
    if (!handler) throw new Error(`Unhandled fetch: ${method} ${url}`);
    return handler.respond(url, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const AUTH_HANDLER = (role: string) => ({
  test: (url: string, method: string) => url.startsWith("/api/v1/auth/me") && method === "GET",
  respond: () => jsonResponse(200, sessionFor(role)),
});

const RFQ_LIST_HANDLER = {
  test: (url: string, method: string) => url.startsWith("/api/v1/rfqs?") && method === "GET",
  respond: () =>
    jsonResponse(200, {
      items: [
        {
          id: "rfq-1",
          name: "Widget Sourcing",
          internal_reference: "RFQ-1001",
          status: "open",
          base_currency: "USD",
          due_date: "2026-09-01",
          requested_delivery_date: null,
          source_bom_id: null,
          is_archived: false,
          version: 1,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
      page: { limit: 100, offset: 0, total: 1 },
    }),
};

const RFQ_DETAIL_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/rfqs/rfq-1" && method === "GET",
  respond: () =>
    jsonResponse(200, {
      id: "rfq-1",
      name: "Widget Sourcing",
      internal_reference: "RFQ-1001",
      status: "open",
      base_currency: "USD",
      requested_payment_terms: null,
      requested_incoterm: null,
      due_date: "2026-09-01",
      requested_delivery_date: null,
      notes: null,
      source_bom_id: null,
      created_by_id: "user-1",
      is_archived: false,
      archived_at: null,
      archive_reason: null,
      version: 1,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      line_count: 1,
      invited_supplier_count: 0,
      lines: [
        {
          id: "line-1",
          line_number: 1,
          part_id: "part-1",
          required_quantity: "500.000000",
          unit_definition_id: "unit-1",
          required_specifications: null,
          notes: null,
        },
      ],
    }),
};

const PART_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/parts/part-1" && method === "GET",
  respond: () =>
    jsonResponse(200, {
      id: "part-1",
      internal_part_number: "IPN-1",
      name: "Widget",
      description: null,
      category: null,
      manufacturer_part_number: null,
      manufacturer_name: null,
      default_unit_definition_id: "unit-1",
      specifications: null,
      is_active: true,
      is_archived: false,
      archived_at: null,
      archive_reason: null,
      version: 1,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    }),
};

const EMPTY_QUOTES_HANDLER = {
  test: (url: string, method: string) =>
    url.startsWith("/api/v1/rfqs/rfq-1/quotes?") && method === "GET",
  respond: () => jsonResponse(200, { items: [] }),
};

const EMPTY_LANDED_COSTS_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/rfqs/rfq-1/landed-costs" && method === "GET",
  respond: () => jsonResponse(200, { items: [] }),
};

const EMPTY_RFQ_SUPPLIERS_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/rfqs/rfq-1/suppliers" && method === "GET",
  respond: () => jsonResponse(200, { items: [] }),
};

const EMPTY_SCORING_CONFIGS_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/scoring-configurations" && method === "GET",
  respond: () => jsonResponse(200, { items: [] }),
};

const EMPTY_SCENARIOS_LIST_HANDLER = {
  test: (url: string, method: string) =>
    url.startsWith("/api/v1/rfqs/rfq-1/comparison-scenarios?") && method === "GET",
  respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
};

const BASE_HANDLERS = [
  RFQ_LIST_HANDLER,
  RFQ_DETAIL_HANDLER,
  PART_HANDLER,
  EMPTY_QUOTES_HANDLER,
  EMPTY_LANDED_COSTS_HANDLER,
  EMPTY_RFQ_SUPPLIERS_HANDLER,
  EMPTY_SCORING_CONFIGS_HANDLER,
];

// -- ScenarioResponse / AllocationResultResponse builders -------------------

function allocationResult(overrides: Record<string, unknown> = {}) {
  return {
    solver_status: "optimal",
    status_explanation:
      "A provably optimal allocation was found within the deterministic search budget.",
    objective_total_cost: { amount: "4750.000000", currency: "USD" },
    objective_source: "exact_decimal_recomputation",
    allocations: [
      {
        rfq_line_id: "line-1",
        quote_line_id: "qline-a",
        supplier_id: "sup-a",
        supplier_label: "SUP-A - Acme Co",
        quantity: 500,
        price_break: { min_quantity: 100, max_quantity: 999, unit_price: "9.500000" },
        line_landed_cost: "4750.000000",
      },
    ],
    binding_constraints: [],
    infeasibility_explanation: null,
    rejected_alternatives: [],
    stats: {
      status_raw: "OPTIMAL",
      deterministic_time: 0.02,
      model_hash: "abcdef1234567890",
      num_variables: 4,
      num_constraints: 6,
    },
    optimization_version: "1.0.0",
    error_message: null,
    ...overrides,
  };
}

function scenarioResponse(overrides: Record<string, unknown> = {}) {
  return {
    id: "scenario-1",
    rfq_id: "rfq-1",
    name: "Test scenario",
    strategy: "lowest_landed_cost",
    scoring_configuration_id: null,
    state: "complete",
    notes: null,
    calculation_version: "1.0.0",
    solver_version: "1.0.0",
    constraints_snapshot: {},
    assumptions_snapshot: {},
    fx_snapshot: [],
    quote_snapshot_refs: [],
    weights_snapshot: [],
    version: 1,
    created_by_id: "user-1",
    created_at: "2026-08-05T00:00:00Z",
    updated_at: "2026-08-05T00:00:00Z",
    completed_at: "2026-08-05T00:00:05Z",
    is_archived: false,
    archived_at: null,
    archive_reason: null,
    scoring_result: { scores: [], weights_used: [], cohort_size: 0, notes: [], scoring_version: "1.0.0" },
    allocation_result: allocationResult(),
    ...overrides,
  };
}

function createScenarioHandler(body: Record<string, unknown>) {
  return {
    test: (url: string, method: string) =>
      url === "/api/v1/rfqs/rfq-1/comparison-scenarios" && method === "POST",
    respond: () => jsonResponse(201, body),
  };
}

async function selectRfq() {
  const rfqSelect = await screen.findByLabelText(/^RFQ$/i);
  fireEvent.change(rfqSelect, { target: { value: "rfq-1" } });
}

async function runScenario() {
  const nameInput = await screen.findByLabelText(/scenario name/i);
  fireEvent.change(nameInput, { target: { value: "Test run" } });
  const strategySelect = screen.getByLabelText(/^strategy$/i);
  fireEvent.change(strategySelect, { target: { value: "lowest_landed_cost" } });
  fireEvent.click(screen.getByRole("button", { name: /run scenario/i }));
}

describe("scenario surface", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself - see
    // comparison.test.tsx's own identical comment for why this is required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows an optimal-toned banner for a proven-optimal allocation", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      ...BASE_HANDLERS,
      EMPTY_SCENARIOS_LIST_HANDLER,
      createScenarioHandler(scenarioResponse()),
    ]);
    renderPage();
    await selectRfq();
    await runScenario();

    const pill = await screen.findByText("Optimal");
    expect(pill).toHaveClass("allocation-status-pill--positive");
    expect(
      screen.getByText(/provably optimal allocation was found/i),
    ).toBeInTheDocument();
  });

  it("shows a distinct info-toned banner for a feasible-but-not-proven allocation", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      ...BASE_HANDLERS,
      EMPTY_SCENARIOS_LIST_HANDLER,
      createScenarioHandler(
        scenarioResponse({
          allocation_result: allocationResult({
            solver_status: "feasible",
            status_explanation:
              "A feasible allocation was found but optimality was not proven within the deterministic search budget.",
          }),
        }),
      ),
    ]);
    renderPage();
    await selectRfq();
    await runScenario();

    const pill = await screen.findByText("Feasible");
    expect(pill).toHaveClass("allocation-status-pill--info");
    expect(screen.getByText(/optimality was not proven/i)).toBeInTheDocument();
  });

  it("renders the infeasibility explainer with conflicting groups and a relaxation callout, not the allocation table", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      ...BASE_HANDLERS,
      EMPTY_SCENARIOS_LIST_HANDLER,
      createScenarioHandler(
        scenarioResponse({
          allocation_result: allocationResult({
            solver_status: "infeasible",
            status_explanation: "No allocation satisfies every constraint; see infeasibility_explanation.",
            objective_total_cost: null,
            allocations: [],
            infeasibility_explanation: {
              conflicting_constraint_groups: ["max_supplier_count", "budget_limit"],
              narrative: "The budget limit cannot be met with at most 1 supplier.",
              minimal_relaxation: "Raising the budget limit to $6,000.00 restores feasibility.",
            },
          }),
        }),
      ),
    ]);
    renderPage();
    await selectRfq();
    await runScenario();

    const pill = await screen.findByText("Infeasible");
    expect(pill).toHaveClass("allocation-status-pill--warning");
    expect(screen.getByText("Max Supplier Count")).toBeInTheDocument();
    expect(screen.getByText("Budget Limit")).toBeInTheDocument();
    expect(
      screen.getByText(/budget limit cannot be met with at most 1 supplier/i),
    ).toBeInTheDocument();
    expect(screen.getByText("Suggested relaxation")).toBeInTheDocument();
    expect(screen.getByText(/raising the budget limit to \$6,000\.00/i)).toBeInTheDocument();
    expect(screen.queryByText("Line cost")).not.toBeInTheDocument();
  });

  it("renders the allocation table's decimal-string values without precision loss", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      ...BASE_HANDLERS,
      EMPTY_SCENARIOS_LIST_HANDLER,
      createScenarioHandler(scenarioResponse()),
    ]);
    renderPage();
    await selectRfq();
    await runScenario();

    expect(await screen.findByText("SUP-A - Acme Co")).toBeInTheDocument();
    expect(screen.getByText("500")).toBeInTheDocument();
    expect(screen.getByText("100-999")).toBeInTheDocument();
    expect(screen.getAllByText("$4,750.00").length).toBeGreaterThan(0);
    expect(screen.getByText("100.0%")).toBeInTheDocument();
  });

  it("does not show Clone & re-run or Archive in scenario history for a viewer", async () => {
    installFetchMock([
      AUTH_HANDLER("viewer"),
      ...BASE_HANDLERS,
      {
        test: (url: string, method: string) =>
          url.startsWith("/api/v1/rfqs/rfq-1/comparison-scenarios?") && method === "GET",
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "scenario-1",
                rfq_id: "rfq-1",
                name: "Saved run",
                strategy: "lowest_landed_cost",
                state: "complete",
                created_by_id: "user-1",
                created_at: "2026-08-05T00:00:00Z",
                completed_at: "2026-08-05T00:00:05Z",
                is_archived: false,
              },
            ],
            page: { limit: 50, offset: 0, total: 1 },
          }),
      },
      {
        test: (url: string, method: string) =>
          url === "/api/v1/comparison-scenarios/scenario-1" && method === "GET",
        respond: () => jsonResponse(200, scenarioResponse()),
      },
    ]);
    renderPage();
    await selectRfq();

    const row = await screen.findByText("Saved run");
    fireEvent.click(row);

    // wait for the detail fetch to resolve and the snapshot summary to render
    expect(await screen.findByText("Assumptions used")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /clone & re-run/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^archive$/i })).not.toBeInTheDocument();
  });

  it("shows Clone & re-run and Archive in scenario history for an administrator", async () => {
    installFetchMock([
      AUTH_HANDLER("administrator"),
      ...BASE_HANDLERS,
      {
        test: (url: string, method: string) =>
          url.startsWith("/api/v1/rfqs/rfq-1/comparison-scenarios?") && method === "GET",
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "scenario-1",
                rfq_id: "rfq-1",
                name: "Saved run",
                strategy: "lowest_landed_cost",
                state: "complete",
                created_by_id: "user-1",
                created_at: "2026-08-05T00:00:00Z",
                completed_at: "2026-08-05T00:00:05Z",
                is_archived: false,
              },
            ],
            page: { limit: 50, offset: 0, total: 1 },
          }),
      },
      {
        test: (url: string, method: string) =>
          url === "/api/v1/comparison-scenarios/scenario-1" && method === "GET",
        respond: () => jsonResponse(200, scenarioResponse()),
      },
    ]);
    renderPage();
    await selectRfq();

    const row = await screen.findByText("Saved run");
    fireEvent.click(row);

    expect(await screen.findByRole("button", { name: /clone & re-run/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^archive$/i })).toBeInTheDocument();
  });

  it("shows version stamps and a read-only note for a saved result loaded from history", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      ...BASE_HANDLERS,
      {
        test: (url: string, method: string) =>
          url.startsWith("/api/v1/rfqs/rfq-1/comparison-scenarios?") && method === "GET",
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "scenario-1",
                rfq_id: "rfq-1",
                name: "Saved run",
                strategy: "lowest_landed_cost",
                state: "complete",
                created_by_id: "user-1",
                created_at: "2026-08-05T00:00:00Z",
                completed_at: "2026-08-05T00:00:05Z",
                is_archived: false,
              },
            ],
            page: { limit: 50, offset: 0, total: 1 },
          }),
      },
      {
        test: (url: string, method: string) =>
          url === "/api/v1/comparison-scenarios/scenario-1" && method === "GET",
        respond: () =>
          jsonResponse(
            200,
            scenarioResponse({
              calculation_version: "1.2.0",
              solver_version: "2.0.0",
              scoring_result: {
                scores: [],
                weights_used: [],
                cohort_size: 0,
                notes: [],
                scoring_version: "1.1.0",
              },
              allocation_result: allocationResult({ optimization_version: "3.0.0" }),
            }),
          ),
      },
    ]);
    renderPage();
    await selectRfq();

    const row = await screen.findByText("Saved run");
    fireEvent.click(row);

    expect(await screen.findByText("Saved result")).toBeInTheDocument();
    expect(screen.getByText("Calculation v1.2.0")).toBeInTheDocument();
    expect(screen.getByText("Solver v2.0.0")).toBeInTheDocument();
    expect(screen.getByText("Scoring v1.1.0")).toBeInTheDocument();
    expect(screen.getByText("Optimization v3.0.0")).toBeInTheDocument();
    expect(screen.getByText(/showing a saved result from scenario history/i)).toBeInTheDocument();
  });
});
