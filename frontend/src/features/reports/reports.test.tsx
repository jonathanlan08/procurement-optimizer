import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { ReportsPage } from "./ReportsPage";

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
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ReportsPage />
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
          status: "awarded",
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

const SCENARIOS_HANDLER = {
  test: (url: string, method: string) =>
    url === "/api/v1/rfqs/rfq-1/comparison-scenarios?limit=50&offset=0" && method === "GET",
  respond: () =>
    jsonResponse(200, {
      items: [
        {
          id: "scenario-1",
          rfq_id: "rfq-1",
          name: "Q3 widget award",
          strategy: "balanced",
          state: "complete",
          created_by_id: "user-1",
          created_at: "2026-08-01T00:00:00Z",
          completed_at: "2026-08-01T00:05:00Z",
          is_archived: false,
        },
      ],
      page: { limit: 50, offset: 0, total: 1 },
    }),
};

const REPORT_PENDING = {
  id: "report-1",
  scenario_id: "scenario-1",
  report_type: "supplier_comparison",
  format: "csv",
  state: "pending",
  size_bytes: null,
  content_sha256: null,
  parameters: {},
  calculation_version: null,
  generated_by_id: "user-1",
  generated_at: null,
  expires_at: null,
  purged: false,
  error_message: null,
};

const REPORT_READY = {
  id: "report-2",
  scenario_id: "scenario-1",
  report_type: "cfo_recommendation",
  format: "pdf",
  state: "ready",
  size_bytes: 20480,
  content_sha256: "abc123",
  parameters: {},
  calculation_version: "v1",
  generated_by_id: "user-1",
  generated_at: "2026-08-05T00:00:00Z",
  expires_at: "2026-09-05T00:00:00Z",
  purged: false,
  error_message: null,
};

const REPORT_FAILED = {
  id: "report-3",
  scenario_id: "scenario-1",
  report_type: "scenario_summary",
  format: "xlsx",
  state: "failed",
  size_bytes: null,
  content_sha256: null,
  parameters: {},
  calculation_version: null,
  generated_by_id: "user-1",
  generated_at: null,
  expires_at: null,
  purged: false,
  error_message: "Solver output unavailable.",
};

const REPORT_PURGED = {
  id: "report-4",
  scenario_id: "scenario-1",
  report_type: "audit_history",
  format: "csv",
  state: "ready",
  size_bytes: 1024,
  content_sha256: "def456",
  parameters: {},
  calculation_version: "v1",
  generated_by_id: "user-1",
  generated_at: "2026-07-01T00:00:00Z",
  expires_at: "2026-07-08T00:00:00Z",
  purged: true,
  error_message: null,
};

const REPORTS_LIST_HANDLER = {
  test: (url: string, method: string) =>
    url === "/api/v1/reports?limit=20&offset=0" && method === "GET",
  respond: () =>
    jsonResponse(200, {
      items: [REPORT_PENDING, REPORT_READY, REPORT_FAILED, REPORT_PURGED],
      page: { limit: 20, offset: 0, total: 4 },
    }),
};

describe("ReportsPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself - see
    // ../comparison/comparison.test.tsx's identical comment for why this is
    // required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the reports table from mocked API data with type/format/state", async () => {
    installFetchMock([AUTH_HANDLER("analyst"), RFQ_LIST_HANDLER, REPORTS_LIST_HANDLER]);

    renderPage();

    // "Supplier comparison"/"CSV"/etc. are also option labels in the
    // generate form's selects (and two rows share the CSV format) - scope
    // every lookup to its own table row to avoid ambiguous matches.
    const table = await screen.findByRole("table", { name: "Reports" });
    const pendingRow = within(table).getByText("Supplier comparison").closest("tr") as HTMLElement;
    expect(within(pendingRow).getByText("CSV")).toBeInTheDocument();
    expect(within(pendingRow).getByText("Pending")).toBeInTheDocument();

    const readyRow = within(table).getByText("CFO recommendation").closest("tr") as HTMLElement;
    expect(within(readyRow).getByText("PDF")).toBeInTheDocument();
    expect(within(readyRow).getByText("Ready")).toBeInTheDocument();

    const failedRow = within(table).getByText("Scenario summary").closest("tr") as HTMLElement;
    expect(within(failedRow).getByText("Failed")).toBeInTheDocument();
    expect(within(failedRow).getByText("Solver output unavailable.")).toBeInTheDocument();

    const purgedRow = within(table).getByText("Audit history").closest("tr") as HTMLElement;
    expect(within(purgedRow).getByText("CSV")).toBeInTheDocument();
    expect(within(purgedRow).getByText("Expired / purged")).toBeInTheDocument();
  });

  it("gates the download button on state=ready and not purged", async () => {
    installFetchMock([AUTH_HANDLER("analyst"), RFQ_LIST_HANDLER, REPORTS_LIST_HANDLER]);

    renderPage();

    const table = await screen.findByRole("table", { name: "Reports" });
    const pendingRow = within(table).getByText("Supplier comparison").closest("tr");
    expect(pendingRow).not.toBeNull();
    expect(within(pendingRow as HTMLElement).getByRole("button", { name: /download/i })).toBeDisabled();

    const readyRow = within(table).getByText("CFO recommendation").closest("tr");
    expect(readyRow).not.toBeNull();
    const readyLink = within(readyRow as HTMLElement).getByRole("link", { name: /download/i });
    expect(readyLink).toHaveAttribute("href", "/api/v1/reports/report-2/content");

    const failedRow = within(table).getByText("Scenario summary").closest("tr");
    expect(within(failedRow as HTMLElement).getByRole("button", { name: /download/i })).toBeDisabled();

    const purgedRow = within(table).getByText("Audit history").closest("tr");
    expect(within(purgedRow as HTMLElement).getByRole("button", { name: /download/i })).toBeDisabled();
  });

  it("posts the correct body once an RFQ, scenario, type, and format are selected", async () => {
    let createBody: unknown = null;
    installFetchMock([
      AUTH_HANDLER("analyst"),
      RFQ_LIST_HANDLER,
      SCENARIOS_HANDLER,
      REPORTS_LIST_HANDLER,
      {
        test: (url: string, method: string) => url === "/api/v1/reports" && method === "POST",
        respond: (_url, init) => {
          createBody = init?.body ? JSON.parse(init.body as string) : null;
          return jsonResponse(201, { ...REPORT_PENDING, id: "report-5" });
        },
      },
    ]);

    renderPage();

    fireEvent.change(await screen.findByLabelText(/^RFQ/i), { target: { value: "rfq-1" } });
    // wait for the scenario `<select>`'s option to actually populate before
    // setting its value - setting a <select>'s value to a not-yet-rendered
    // option is a silent no-op.
    await screen.findByRole("option", { name: "Q3 widget award" });
    fireEvent.change(screen.getByLabelText(/^Scenario/i), {
      target: { value: "scenario-1" },
    });
    fireEvent.change(screen.getByLabelText(/^Report type/i), {
      target: { value: "cfo_recommendation" },
    });
    fireEvent.change(screen.getByLabelText(/^Format/i), { target: { value: "pdf" } });

    fireEvent.click(screen.getByRole("button", { name: /generate report/i }));

    await screen.findByText(/generate report/i);
    expect(createBody).toEqual({
      scenario_id: "scenario-1",
      report_type: "cfo_recommendation",
      format: "pdf",
    });
  });

  it("hides the generate-report form for a read-only viewer", async () => {
    installFetchMock([AUTH_HANDLER("viewer"), RFQ_LIST_HANDLER, REPORTS_LIST_HANDLER]);

    renderPage("viewer");

    await screen.findByText("Supplier comparison");
    expect(screen.queryByRole("button", { name: /generate report/i })).not.toBeInTheDocument();
  });
});
