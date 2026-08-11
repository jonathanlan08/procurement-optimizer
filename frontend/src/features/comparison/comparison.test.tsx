import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

function renderPage(role = "analyst") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ComparisonPage />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { ...result, role };
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
      invited_supplier_count: 2,
      lines: [
        {
          id: "line-1",
          line_number: 1,
          part_id: "part-1",
          required_quantity: "100.000000",
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

const QUOTES_LIST_HANDLER = {
  test: (url: string, method: string) =>
    url.startsWith("/api/v1/rfqs/rfq-1/quotes?") && method === "GET",
  respond: () =>
    jsonResponse(200, {
      items: [
        {
          id: "quote-a",
          rfq_id: "rfq-1",
          supplier_id: "sup-a",
          quote_number: "Q-A",
          quote_date: "2026-08-01",
          expiration_date: null,
          currency: "USD",
          status: "confirmed",
          source: "manual",
          superseded_by_id: null,
          is_archived: false,
          version: 1,
          created_at: "2026-08-01T00:00:00Z",
          updated_at: "2026-08-01T00:00:00Z",
          line_count: 1,
        },
        {
          id: "quote-b",
          rfq_id: "rfq-1",
          supplier_id: "sup-b",
          quote_number: "Q-B",
          quote_date: "2026-08-02",
          expiration_date: null,
          currency: "USD",
          status: "draft",
          source: "manual",
          superseded_by_id: null,
          is_archived: false,
          version: 1,
          created_at: "2026-08-02T00:00:00Z",
          updated_at: "2026-08-02T00:00:00Z",
          line_count: 1,
        },
      ],
    }),
};

function quoteLine(id: string, unitPrice: string, leadTime: number) {
  return {
    id,
    line_number: 1,
    matched_rfq_line_id: "line-1",
    part_id: "part-1",
    quoted_part_number: null,
    quoted_mpn: null,
    description: null,
    quantity: "100.000000",
    unit_definition_id: "unit-1",
    unit_price: unitPrice,
    moq: null,
    tooling_cost: null,
    setup_cost: null,
    packaging_cost: null,
    shipping_cost: null,
    insurance_cost: null,
    other_fixed_cost: null,
    tariff_amount: null,
    duty_amount: null,
    customs_fee: null,
    tax_amount: null,
    country_of_origin: null,
    lead_time_days: leadTime,
    production_capacity: null,
    notes: null,
    match_status: "confirmed",
    price_breaks: [],
  };
}

function quoteDetailHandler(id: string, supplierId: string, lineId: string, unitPrice: string, leadTime: number) {
  return {
    test: (url: string, method: string) => url === `/api/v1/quotes/${id}` && method === "GET",
    respond: () =>
      jsonResponse(200, {
        id,
        rfq_id: "rfq-1",
        supplier_id: supplierId,
        quote_number: id.toUpperCase(),
        quote_date: "2026-08-01",
        expiration_date: null,
        currency: "USD",
        status: "confirmed",
        source: "manual",
        notes: null,
        superseded_by_id: null,
        is_archived: false,
        archived_at: null,
        archive_reason: null,
        version: 1,
        created_at: "2026-08-01T00:00:00Z",
        updated_at: "2026-08-01T00:00:00Z",
        lines: [quoteLine(lineId, unitPrice, leadTime)],
        terms: {
          payment_terms: "Net 30",
          payment_terms_days: 30,
          incoterm: null,
          shipping_terms: null,
          warranty_terms: null,
          validity_days: null,
          exceptions: null,
          exclusions: null,
          notes: null,
        },
      }),
  };
}

function supplierHandler(id: string, code: string, name: string) {
  return {
    test: (url: string, method: string) => url === `/api/v1/suppliers/${id}` && method === "GET",
    respond: () =>
      jsonResponse(200, {
        id,
        code,
        name,
        country_code: "US",
        supported_currencies: ["USD"],
        standard_payment_terms: null,
        standard_incoterm: null,
        typical_lead_time_days: null,
        capacity_units_per_month: null,
        default_moq: null,
        is_active: true,
        is_archived: false,
        archived_at: null,
        archive_reason: null,
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      }),
  };
}

const SEVEN_COMPONENTS = [
  {
    component: "extended_material",
    amount: "1250.000000",
    formula: "12.50000000 * 100.000000",
    inputs: { unit_price: "12.50000000", quantity: "100.000000" },
    provenance: "supplier",
    is_assumed: false,
    is_missing: false,
  },
  {
    component: "allocated_fixed",
    amount: "0.000000",
    formula: "no fixed costs stated",
    inputs: {},
    provenance: "missing",
    is_assumed: false,
    is_missing: true,
  },
  {
    component: "logistics",
    amount: "50.000000",
    formula: "shipping 50.00",
    inputs: { shipping: "50.000000" },
    provenance: "supplier",
    is_assumed: false,
    is_missing: false,
  },
  {
    component: "import",
    amount: "0.000000",
    formula: "no import charges stated or assumed",
    inputs: {},
    provenance: "missing",
    is_assumed: false,
    is_missing: true,
  },
  {
    component: "quality_risk",
    amount: "25.000000",
    formula: "0.020000 * 1250.000000",
    inputs: { quality_risk_rate: "0.020000" },
    provenance: "user_assumption",
    is_assumed: true,
    is_missing: false,
  },
  {
    component: "delay_risk",
    amount: "0.000000",
    formula: "no delay",
    inputs: {},
    provenance: "missing",
    is_assumed: false,
    is_missing: true,
  },
  {
    component: "financing",
    amount: "25.500000",
    formula: "financing benefit/cost calculation",
    inputs: {},
    provenance: "calculated",
    is_assumed: false,
    is_missing: false,
  },
];

function landedCostResult(
  id: string,
  quoteLineId: string,
  total: string,
  effective: string,
  completeness: string,
) {
  return {
    id,
    quote_line_id: quoteLineId,
    accepted_quantity: "100.000000",
    currency: "USD",
    total_landed_cost: total,
    effective_unit_cost: effective,
    completeness,
    calculation_version: "1.0.0",
    calculated_at: "2026-08-05T00:00:00Z",
    calculated_by_id: "user-1",
    missing_inputs: [],
    assumptions: [
      {
        key: "quality_risk_rate",
        value: "0.020000",
        description: "Quality risk assumption",
        provenance: "user_assumption",
      },
    ],
    components: SEVEN_COMPONENTS,
  };
}

const LANDED_COSTS_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/rfqs/rfq-1/landed-costs" && method === "GET",
  respond: () =>
    jsonResponse(200, {
      items: [
        landedCostResult("lc-a", "qline-a", "1350.500000", "13.50500000", "complete"),
        landedCostResult("lc-b", "qline-b", "1180.000000", "11.80000000", "assumption_dependent"),
      ],
    }),
};

const SCORING_CONFIGS_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/scoring-configurations" && method === "GET",
  respond: () =>
    jsonResponse(200, {
      items: [
        {
          id: "cfg-1",
          name: "Balanced",
          weights: [
            { criterion: "total_landed_cost", weight: "0.350000", direction: "lower_is_better", label: "Total landed cost", is_sample_weight: true },
          ],
          weight_sum: "1.000000",
          is_sample: true,
          is_archived: false,
          archived_at: null,
          archive_reason: null,
          version: 1,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          notes: [],
        },
      ],
    }),
};

const BASE_HANDLERS = [
  RFQ_LIST_HANDLER,
  RFQ_DETAIL_HANDLER,
  PART_HANDLER,
  QUOTES_LIST_HANDLER,
  quoteDetailHandler("quote-a", "sup-a", "qline-a", "12.50000000", 14),
  quoteDetailHandler("quote-b", "sup-b", "qline-b", "11.00000000", 21),
  supplierHandler("sup-a", "SUP-A", "Acme Co"),
  supplierHandler("sup-b", "SUP-B", "Beta Supply"),
  LANDED_COSTS_HANDLER,
  SCORING_CONFIGS_HANDLER,
];

async function selectRfq() {
  const rfqSelect = await screen.findByLabelText(/^RFQ$/i);
  fireEvent.change(rfqSelect, { target: { value: "rfq-1" } });
}

describe("ComparisonPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself — see
    // features/fx/fx.test.tsx's own identical comment for why this is
    // required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the comparison table's decimal-string values without precision loss", async () => {
    installFetchMock([AUTH_HANDLER("analyst"), ...BASE_HANDLERS]);
    renderPage();
    await selectRfq();

    expect(await screen.findByText("Acme Co", { exact: false })).toBeInTheDocument();
    expect(await screen.findByText("$12.50")).toBeInTheDocument();
    expect(await screen.findByText("$1,350.50")).toBeInTheDocument();
  });

  it("renders a completeness badge per compared supplier", async () => {
    installFetchMock([AUTH_HANDLER("analyst"), ...BASE_HANDLERS]);
    renderPage();
    await selectRfq();

    expect(await screen.findByText("Complete")).toHaveClass("badge--completeness-complete");
    expect(await screen.findByText("Assumption-dependent")).toHaveClass(
      "badge--completeness-assumption_dependent",
    );
  });

  it("opens the explainability drawer with all seven landed-cost components", async () => {
    installFetchMock([AUTH_HANDLER("analyst"), ...BASE_HANDLERS]);
    renderPage();
    await selectRfq();

    const totalCostCell = await screen.findByText("$1,350.50");
    fireEvent.click(totalCostCell);

    // Scoped to the drawer dialog itself: "Quality risk" is also the
    // Assumptions form's field label elsewhere on the same page.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Extended material cost")).toBeInTheDocument();
    expect(within(dialog).getByText("Allocated fixed cost")).toBeInTheDocument();
    expect(within(dialog).getByText("Logistics")).toBeInTheDocument();
    expect(within(dialog).getByText("Import (tariffs / duties / customs)")).toBeInTheDocument();
    expect(within(dialog).getByText("Quality risk")).toBeInTheDocument();
    expect(within(dialog).getByText("Delay risk")).toBeInTheDocument();
    expect(within(dialog).getByText("Financing")).toBeInTheDocument();
  });

  it("runs a scenario and renders its ranked supplier scores", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      ...BASE_HANDLERS,
      {
        test: (url: string, method: string) =>
          url.startsWith("/api/v1/rfqs/rfq-1/comparison-scenarios?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
      },
      {
        test: (url: string, method: string) =>
          url === "/api/v1/rfqs/rfq-1/suppliers" && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url: string, method: string) =>
          url === "/api/v1/rfqs/rfq-1/comparison-scenarios" && method === "POST",
        respond: () =>
          jsonResponse(201, {
            id: "scenario-1",
            rfq_id: "rfq-1",
            name: "Test run",
            strategy: "balanced",
            scoring_configuration_id: "cfg-1",
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
            allocation_result: {
              solver_status: "optimal",
              status_explanation:
                "A provably optimal allocation was found within the deterministic search budget.",
              objective_total_cost: { amount: "1350.500000", currency: "USD" },
              objective_source: "exact_decimal_recomputation",
              allocations: [],
              binding_constraints: [],
              infeasibility_explanation: null,
              rejected_alternatives: [],
              stats: null,
              optimization_version: "1.0.0",
              error_message: null,
            },
            scoring_result: {
              scores: [
                {
                  supplier_id: "sup-a",
                  supplier_name: "Acme Co",
                  total_score: "0.820000",
                  rank: 1,
                  criterion_scores: [
                    {
                      criterion: "total_landed_cost",
                      raw_value: "1350.500000",
                      normalized_score: "1.000000",
                      effective_weight: "0.350000",
                      weighted_contribution: "0.350000",
                      reason: "lowest landed cost in the compared cohort",
                    },
                  ],
                  missing_criteria: [],
                  weights_renormalized: false,
                  excluded: false,
                  exclusion_reason: null,
                },
                {
                  supplier_id: "sup-b",
                  supplier_name: "Beta Supply",
                  total_score: "0.410000",
                  rank: 2,
                  criterion_scores: [
                    {
                      criterion: "total_landed_cost",
                      raw_value: "1180.000000",
                      normalized_score: "0.500000",
                      effective_weight: "0.350000",
                      weighted_contribution: "0.175000",
                      reason: "mid-range landed cost in the compared cohort",
                    },
                  ],
                  missing_criteria: ["spec_compliance"],
                  weights_renormalized: true,
                  excluded: false,
                  exclusion_reason: null,
                },
              ],
              weights_used: [
                {
                  criterion: "total_landed_cost",
                  weight: "0.350000",
                  direction: "lower_is_better",
                  label: "Total landed cost",
                  is_sample_weight: true,
                },
              ],
              cohort_size: 2,
              notes: [],
              scoring_version: "1.0.0",
            },
          }),
      },
    ]);
    renderPage();
    await selectRfq();

    const nameInput = await screen.findByLabelText(/scenario name/i);
    fireEvent.change(nameInput, { target: { value: "Test run" } });
    const strategySelect = screen.getByLabelText(/^strategy$/i);
    fireEvent.change(strategySelect, { target: { value: "balanced" } });
    const configSelect = await screen.findByLabelText(/scoring configuration/i);
    fireEvent.change(configSelect, { target: { value: "cfg-1" } });
    fireEvent.click(screen.getByRole("button", { name: /run scenario/i }));

    expect(await screen.findByText("#1")).toBeInTheDocument();
    expect(screen.getByText("Beta Supply")).toBeInTheDocument();
    expect(screen.getByText(/missing: spec compliance/i)).toBeInTheDocument();
    expect(screen.getByText(/weights renormalized/i)).toBeInTheDocument();
  });
});
