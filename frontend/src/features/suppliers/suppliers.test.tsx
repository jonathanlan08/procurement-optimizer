import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { SuppliersPage } from "./SuppliersPage";

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

const ACME: Record<string, unknown> = {
  id: "11111111-1111-1111-1111-111111111111",
  code: "ACME",
  name: "Acme Manufacturing",
  country_code: "US",
  supported_currencies: ["USD", "EUR"],
  standard_payment_terms: "Net 30",
  standard_incoterm: "FOB",
  typical_lead_time_days: 14,
  capacity_units_per_month: "1000.000000",
  default_moq: "250.000000",
  is_active: true,
  is_archived: false,
  archived_at: null,
  archive_reason: null,
  version: 3,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <SuppliersPage />
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

describe("SuppliersPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup (which checks for a *global* `afterEach`)
    // never registers itself - without this, each `render()` in this file
    // would stack on top of the previous test's still-mounted DOM.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders supplier rows from the mocked list response", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/suppliers?") && method === "GET",
        respond: () => jsonResponse(200, { items: [ACME], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    expect(await screen.findByText("Acme Manufacturing")).toBeInTheDocument();
    expect(screen.getByText("ACME")).toBeInTheDocument();
    expect(screen.getByText("US")).toBeInTheDocument();
    expect(screen.getByText("USD, EUR")).toBeInTheDocument();
  });

  it("hides mutation buttons for a viewer", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("viewer")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/suppliers?") && method === "GET",
        respond: () => jsonResponse(200, { items: [ACME], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    await screen.findByText("Acme Manufacturing");
    expect(screen.queryByRole("button", { name: /new supplier/i })).not.toBeInTheDocument();
  });

  it("shows New supplier for an analyst, validates MOQ as a decimal string, and surfaces a 409 conflict", async () => {
    let createCalls = 0;
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/suppliers?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
      },
      {
        test: (url, method) => url === "/api/v1/suppliers" && method === "POST",
        respond: () => {
          createCalls += 1;
          return jsonResponse(409, {
            error: {
              code: "conflict_duplicate",
              message: "A supplier with this code already exists.",
              status: 409,
              details: [{ field: "code", issue: "must be unique" }],
              request_id: "req-1",
              timestamp: "2026-08-10T00:00:00Z",
            },
          });
        },
      },
    ]);

    renderPage();

    const newSupplierBtn = await screen.findByRole("button", { name: /new supplier/i });
    fireEvent.click(newSupplierBtn);

    const moqInput = await screen.findByLabelText(/^MOQ/i);
    fireEvent.change(moqInput, { target: { value: "not-a-number" } });
    fireEvent.click(screen.getByRole("button", { name: /create supplier/i }));

    expect(await screen.findByText(/must be a decimal number/i)).toBeInTheDocument();
    expect(createCalls).toBe(0);

    // Anchored to the *start* only (not `$`): once a field has an error, the
    // implicit <label> also wraps that error text, so the accessible name is
    // "Code" + the error message concatenated, not "Code" alone.
    fireEvent.change(screen.getByLabelText(/^Code/i), { target: { value: "ACME2" } });
    fireEvent.change(screen.getByLabelText(/^Name/i), { target: { value: "Acme Two" } });
    fireEvent.change(screen.getByLabelText(/Country code/i), { target: { value: "US" } });
    fireEvent.change(screen.getByLabelText(/Currencies/i), { target: { value: "USD" } });
    fireEvent.change(moqInput, { target: { value: "10.50" } });

    fireEvent.click(screen.getByRole("button", { name: /create supplier/i }));

    expect(await screen.findByText("A supplier with this code already exists.")).toBeInTheDocument();
    expect(createCalls).toBe(1);
  });
});
