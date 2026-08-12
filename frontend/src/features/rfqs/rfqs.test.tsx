import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { RfqsPage } from "./RfqsPage";

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

const RFQ_DRAFT_SUMMARY: Record<string, unknown> = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Q3 Connector Sourcing",
  internal_reference: "RFQ-1001",
  status: "draft",
  base_currency: "USD",
  due_date: "2026-09-01",
  requested_delivery_date: "2026-10-01",
  source_bom_id: null,
  is_archived: false,
  version: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const RFQ_DRAFT_DETAIL: Record<string, unknown> = {
  ...RFQ_DRAFT_SUMMARY,
  requested_payment_terms: "Net 30",
  requested_incoterm: "FOB",
  notes: null,
  created_by_id: "user-1",
  archived_at: null,
  archive_reason: null,
  line_count: 0,
  invited_supplier_count: 0,
  lines: [],
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RfqsPage />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

type FetchHandler = (url: string, init: RequestInit | undefined) => Response | Promise<Response>;

function installFetchMock(handlers: Array<{ test: (url: string, method: string) => boolean; respond: FetchHandler }>) {
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

describe("RfqsPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup (which checks for a *global* `afterEach`)
    // never registers itself — without this, each `render()` in this file
    // would stack on top of the previous test's still-mounted DOM.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders RFQ rows from the mocked list response, including the status badge", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () => jsonResponse(200, { items: [RFQ_DRAFT_SUMMARY], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    expect(await screen.findByText("Q3 Connector Sourcing")).toBeInTheDocument();
    expect(screen.getByText("RFQ-1001")).toBeInTheDocument();
    // Scoped to the table: "Draft" also appears as a status-filter chip
    // label in the toolbar, so an unscoped query would match two elements.
    const table = screen.getByRole("table", { name: /requests for quotation/i });
    expect(within(table).getByText("Draft")).toHaveClass("rfq-badge--draft");
  });

  it("hides mutation controls for a viewer", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("viewer")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () => jsonResponse(200, { items: [RFQ_DRAFT_SUMMARY], page: { limit: 50, offset: 0, total: 1 } }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111" && method === "GET",
        respond: () => jsonResponse(200, RFQ_DRAFT_DETAIL),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111/status-history" && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111/suppliers" && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/units") && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
    ]);

    renderPage();

    await screen.findByText("Q3 Connector Sourcing");
    expect(screen.queryByRole("button", { name: /new rfq/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("Q3 Connector Sourcing"));
    await screen.findByText("Status timeline");

    // No status-change, edit, add-line, or invite-supplier controls for a viewer.
    expect(screen.queryByRole("button", { name: /^edit$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /move to/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add line/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /invite suppliers/i })).not.toBeInTheDocument();
  });

  it("renders only the legal next-status buttons for a draft RFQ (Open + Archived)", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () => jsonResponse(200, { items: [RFQ_DRAFT_SUMMARY], page: { limit: 50, offset: 0, total: 1 } }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111" && method === "GET",
        respond: () => jsonResponse(200, RFQ_DRAFT_DETAIL),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111/status-history" && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111/suppliers" && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/units") && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
    ]);

    renderPage();

    fireEvent.click(await screen.findByText("Q3 Connector Sourcing"));
    await screen.findByText("Status timeline");

    // ALLOWED_RFQ_TRANSITIONS["draft"] = ["open", "archived"] — exactly two
    // transition buttons, no "Under Review"/"Awarded"/"Closed" buttons.
    const moveButtons = screen.getAllByRole("button", { name: /^move to/i });
    expect(moveButtons).toHaveLength(2);
    expect(screen.getByRole("button", { name: /move to open/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /move to archived/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /move to under review/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /move to awarded/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /move to closed/i })).not.toBeInTheDocument();
  });

  it("requires a name and at least one line (structurally, via a non-removable first line) on the create form", async () => {
    let createCalls = 0;
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/units") && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) => url === "/api/v1/rfqs" && method === "POST",
        respond: () => {
          createCalls += 1;
          return jsonResponse(201, { ...RFQ_DRAFT_DETAIL, id: "created-id" });
        },
      },
    ]);

    renderPage();

    const newRfqBtn = await screen.findByRole("button", { name: /new rfq/i });
    fireEvent.click(newRfqBtn);

    // Default mode is "inline" with exactly one line, which cannot be
    // removed — the form structurally enforces "at least one line" the same
    // way BomsPage.tsx's create form does.
    await screen.findByLabelText(/^Quantity/i);
    expect(screen.queryByRole("button", { name: /remove line 1/i })).not.toBeInTheDocument();

    // Submitting with no name and no part selected surfaces both errors at
    // once: the required-name error and the per-line "select a part" error.
    fireEvent.click(screen.getByRole("button", { name: /create rfq/i }));

    expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
    expect(screen.getByText(/select a part/i)).toBeInTheDocument();
    expect(createCalls).toBe(0);
  });

  it("renders the list 'Lines' column from line_count when present, and the Specs cell honestly (missing vs supplied)", async () => {
    const RFQ_WITH_LINE_COUNT = { ...RFQ_DRAFT_SUMMARY, line_count: 3 };
    const RFQ_DETAIL_WITH_LINES = {
      ...RFQ_DRAFT_DETAIL,
      line_count: 2,
      lines: [
        {
          id: "line-1",
          line_number: 1,
          part_id: "part-1",
          required_quantity: "10.000000",
          unit_definition_id: "unit-1",
          required_specifications: null,
          notes: null,
        },
        {
          id: "line-2",
          line_number: 2,
          part_id: "part-2",
          required_quantity: "5.000000",
          unit_definition_id: "unit-1",
          required_specifications: { length_mm: "25", tolerance: "0.1" },
          notes: null,
        },
      ],
    };

    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () =>
          jsonResponse(200, { items: [RFQ_WITH_LINE_COUNT], page: { limit: 50, offset: 0, total: 1 } }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111" && method === "GET",
        respond: () => jsonResponse(200, RFQ_DETAIL_WITH_LINES),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111/status-history" && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/11111111-1111-1111-1111-111111111111/suppliers" && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/units") && method === "GET",
        respond: () =>
          jsonResponse(200, {
            items: [{ id: "unit-1", code: "EA", name: "Each", dimension: "count", is_global: true }],
          }),
      },
      {
        test: (url, method) => url === "/api/v1/parts/part-1" && method === "GET",
        respond: () =>
          jsonResponse(200, {
            id: "part-1",
            internal_part_number: "PN-001",
            manufacturer_part_number: null,
            manufacturer: null,
            name: "Widget A",
            description: null,
            category: null,
            unit_definition_id: "unit-1",
            required_specifications: null,
            target_price: null,
            target_price_currency: null,
            is_active: true,
            is_archived: false,
            archived_at: null,
            archive_reason: null,
            version: 1,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          }),
      },
      {
        test: (url, method) => url === "/api/v1/parts/part-2" && method === "GET",
        respond: () =>
          jsonResponse(200, {
            id: "part-2",
            internal_part_number: "PN-002",
            manufacturer_part_number: null,
            manufacturer: null,
            name: "Widget B",
            description: null,
            category: null,
            unit_definition_id: "unit-1",
            required_specifications: null,
            target_price: null,
            target_price_currency: null,
            is_active: true,
            is_archived: false,
            archived_at: null,
            archive_reason: null,
            version: 1,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          }),
      },
    ]);

    renderPage();

    const table = await screen.findByRole("table", { name: /requests for quotation/i });
    expect(within(table).getByText("3")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Q3 Connector Sourcing"));
    await screen.findByText("Status timeline");

    // line-1 has no required_specifications: honest "— not stated", never a
    // fabricated "0" (P2 audit finding).
    expect(await screen.findByText("— not stated")).toBeInTheDocument();
    // line-2 has two real spec keys: a plain count, since that IS supplied data.
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("renders '—' in the list 'Lines' column when line_count is absent (backward compat)", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () =>
          jsonResponse(200, { items: [RFQ_DRAFT_SUMMARY], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    const table = await screen.findByRole("table", { name: /requests for quotation/i });
    const row = within(table).getByText("Q3 Connector Sourcing").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("—")).toBeInTheDocument();
  });

  it("switches to 'From BOM' mode and requires a BOM to be selected instead of lines", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/units") && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/boms?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 100, offset: 0, total: 0 } }),
      },
    ]);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /new rfq/i }));
    fireEvent.change(await screen.findByLabelText(/^Name/i), { target: { value: "From BOM RFQ" } });

    fireEvent.click(screen.getByRole("button", { name: /from bom/i }));
    // Switching modes hides the inline line editor entirely.
    expect(screen.queryByLabelText(/^Quantity/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /create rfq/i }));
    expect(await screen.findByText(/select a bom/i)).toBeInTheDocument();
  });
});
