import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import type { RfqResponse } from "../rfqs/api";
import { QuotesSection } from "./QuotesSection";

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

const RFQ_ID = "11111111-1111-1111-1111-111111111111";
const SUPPLIER_ID = "22222222-2222-2222-2222-222222222222";
const QUOTE_ID = "33333333-3333-3333-3333-333333333333";

function rfqFixture(overrides: Partial<RfqResponse> = {}): RfqResponse {
  return {
    id: RFQ_ID,
    name: "Q3 Connector Sourcing",
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
    line_count: 0,
    invited_supplier_count: 1,
    lines: [],
    ...overrides,
  };
}

const SUPPLIER_RESPONSE: Record<string, unknown> = {
  id: SUPPLIER_ID,
  code: "SUP-1",
  name: "Acme Components",
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
};

const QUOTE_SUMMARY: Record<string, unknown> = {
  id: QUOTE_ID,
  rfq_id: RFQ_ID,
  supplier_id: SUPPLIER_ID,
  quote_number: "Q-100",
  quote_date: "2026-02-01",
  expiration_date: null,
  currency: "USD",
  status: "draft",
  source: "manual",
  superseded_by_id: null,
  is_archived: false,
  version: 1,
  created_at: "2026-02-01T00:00:00Z",
  updated_at: "2026-02-01T00:00:00Z",
  line_count: 1,
};

const RFQ_SUPPLIER_ROW: Record<string, unknown> = {
  id: "44444444-4444-4444-4444-444444444444",
  rfq_id: RFQ_ID,
  supplier_id: SUPPLIER_ID,
  invited_at: "2026-01-05T00:00:00Z",
  excluded_at: null,
  exclusion_reason: null,
  notes: null,
};

function renderSection(rfq: RfqResponse) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <QuotesSection rfq={rfq} />
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

function baseHandlers(role: string) {
  return [
    {
      test: (url: string, method: string) => url.startsWith("/api/v1/auth/me") && method === "GET",
      respond: () => jsonResponse(200, sessionFor(role)),
    },
    {
      test: (url: string, method: string) =>
        url === `/api/v1/rfqs/${RFQ_ID}/suppliers` && method === "GET",
      respond: () => jsonResponse(200, { items: [RFQ_SUPPLIER_ROW] }),
    },
    {
      test: (url: string, method: string) => url === `/api/v1/suppliers/${SUPPLIER_ID}` && method === "GET",
      respond: () => jsonResponse(200, SUPPLIER_RESPONSE),
    },
    {
      test: (url: string, method: string) => url.startsWith("/api/v1/units") && method === "GET",
      respond: () => jsonResponse(200, { items: [{ id: "unit-1", code: "EA", name: "Each", dimension: "count", is_global: true }] }),
    },
  ];
}

describe("QuotesSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself — see rfqs.test.tsx
    // / boms.test.tsx's own identical comment for why this is required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the quotes list with the resolved supplier name and a status badge", async () => {
    installFetchMock([
      ...baseHandlers("analyst"),
      {
        test: (url, method) =>
          url.startsWith(`/api/v1/rfqs/${RFQ_ID}/quotes?`) && method === "GET",
        respond: () => jsonResponse(200, { items: [QUOTE_SUMMARY] }),
      },
    ]);

    renderSection(rfqFixture());

    expect(await screen.findByText("SUP-1 — Acme Components")).toBeInTheDocument();
    expect(screen.getByText("Draft")).toHaveClass("quote-badge--draft");
  });

  it("hides 'Enter quote' for a viewer even while the RFQ is open", async () => {
    installFetchMock([
      ...baseHandlers("viewer"),
      {
        test: (url, method) =>
          url.startsWith(`/api/v1/rfqs/${RFQ_ID}/quotes?`) && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
    ]);

    renderSection(rfqFixture({ status: "open" }));

    await screen.findByText("No quotes entered yet.");
    expect(screen.queryByRole("button", { name: /enter quote/i })).not.toBeInTheDocument();
  });

  it("hides 'Enter quote' for an analyst once the RFQ is closed", async () => {
    installFetchMock([
      ...baseHandlers("analyst"),
      {
        test: (url, method) =>
          url.startsWith(`/api/v1/rfqs/${RFQ_ID}/quotes?`) && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
    ]);

    renderSection(rfqFixture({ status: "closed" }));

    await screen.findByText("No quotes entered yet.");
    expect(screen.queryByRole("button", { name: /enter quote/i })).not.toBeInTheDocument();
  });

  it("flags an overlapping price-break tier inline, before submit", async () => {
    installFetchMock([
      ...baseHandlers("analyst"),
      {
        test: (url, method) =>
          url.startsWith(`/api/v1/rfqs/${RFQ_ID}/quotes?`) && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
    ]);

    renderSection(rfqFixture());

    fireEvent.click(await screen.findByRole("button", { name: /enter quote/i }));
    await screen.findByText("Line 1");

    const addTierBtn = screen.getByRole("button", { name: /add tier/i });
    fireEvent.click(addTierBtn);
    fireEvent.click(addTierBtn);

    const minInputs = screen.getAllByLabelText(/^Min qty/i);
    expect(minInputs).toHaveLength(2);
    fireEvent.change(minInputs[0]!, { target: { value: "1" } });
    const maxInputs = screen.getAllByLabelText(/^Max qty/i);
    fireEvent.change(maxInputs[0]!, { target: { value: "10" } });
    // Second tier's min (5) falls inside the first tier's 1-10 range —
    // mirrors QuoteService._validate_price_breaks's overlap rule.
    fireEvent.change(minInputs[1]!, { target: { value: "5" } });

    expect(await screen.findByText(/overlaps the next tier/i)).toBeInTheDocument();
  });

  it("leaves per-line cost fields empty by default — never prefilled 0", async () => {
    installFetchMock([
      ...baseHandlers("analyst"),
      {
        test: (url, method) =>
          url.startsWith(`/api/v1/rfqs/${RFQ_ID}/quotes?`) && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
    ]);

    renderSection(rfqFixture());

    fireEvent.click(await screen.findByRole("button", { name: /enter quote/i }));
    await screen.findByText("Line 1");

    fireEvent.click(screen.getByRole("button", { name: /add costs/i }));

    for (const label of [/^Tooling/i, /^Setup/i, /^Packaging/i, /^Shipping/i, /^Insurance/i, /^Other/i, /^Tariff/i, /^Duty/i, /^Customs/i, /^Tax/i]) {
      expect(screen.getByLabelText(label)).toHaveValue("");
    }
  });
});
