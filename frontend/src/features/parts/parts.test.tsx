import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { PartsPage } from "./PartsPage";

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

const BOLT: Record<string, unknown> = {
  id: "22222222-2222-2222-2222-222222222222",
  internal_part_number: "PN-100",
  manufacturer_part_number: "MPN-500",
  manufacturer: "Fastenal",
  name: "M6 Hex Bolt",
  description: "Stainless steel hex bolt",
  category: "Fasteners",
  unit_definition_id: "33333333-3333-3333-3333-333333333333",
  required_specifications: null,
  target_price: "0.12500000",
  target_price_currency: "USD",
  is_active: true,
  is_archived: false,
  archived_at: null,
  archive_reason: null,
  version: 2,
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
        <PartsPage />
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

describe("PartsPage", () => {
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

  it("renders part rows from the mocked list response", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/parts?") && method === "GET",
        respond: () => jsonResponse(200, { items: [BOLT], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    expect(await screen.findByText("M6 Hex Bolt")).toBeInTheDocument();
    expect(screen.getByText("PN-100")).toBeInTheDocument();
    expect(screen.getByText("MPN-500")).toBeInTheDocument();
    expect(screen.getByText("Fasteners")).toBeInTheDocument();
    // "0.12500000" rounds half-even at 2dp: 12 is already even, so it stays "0.12".
    expect(screen.getByText("$0.12")).toBeInTheDocument();
  });

  it("hides mutation buttons for a viewer", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("viewer")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/parts?") && method === "GET",
        respond: () => jsonResponse(200, { items: [BOLT], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    await screen.findByText("M6 Hex Bolt");
    expect(screen.queryByRole("button", { name: /new part/i })).not.toBeInTheDocument();
  });

  it("shows New part for an analyst, validates target price as a decimal string, and surfaces a 409 conflict", async () => {
    let createCalls = 0;
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/parts?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
      },
      {
        test: (url, method) => url === "/api/v1/parts" && method === "POST",
        respond: () => {
          createCalls += 1;
          return jsonResponse(409, {
            error: {
              code: "conflict_duplicate",
              message: "A part with this internal part number already exists.",
              status: 409,
              details: [{ field: "internal_part_number", issue: "must be unique" }],
              request_id: "req-1",
              timestamp: "2026-08-10T00:00:00Z",
            },
          });
        },
      },
    ]);

    renderPage();

    const newPartBtn = await screen.findByRole("button", { name: /new part/i });
    fireEvent.click(newPartBtn);

    const priceInput = await screen.findByLabelText(/^Target price optional/i);
    fireEvent.change(priceInput, { target: { value: "not-a-number" } });
    fireEvent.click(screen.getByRole("button", { name: /create part/i }));

    expect(await screen.findByText(/must be a decimal number/i)).toBeInTheDocument();
    expect(createCalls).toBe(0);

    // Anchored to the *start* only (not `$`): once a field has an error, the
    // implicit <label> also wraps that error text, so the accessible name is
    // "Name" + the error message concatenated, not "Name" alone.
    fireEvent.change(screen.getByLabelText(/Internal part number/i), { target: { value: "PN-200" } });
    fireEvent.change(screen.getByLabelText(/^Name/i), { target: { value: "Test Part" } });
    fireEvent.change(screen.getByLabelText(/Unit definition ID/i), {
      target: { value: "33333333-3333-3333-3333-333333333333" },
    });
    fireEvent.change(priceInput, { target: { value: "10.50" } });
    fireEvent.change(screen.getByLabelText(/Target price currency/i), { target: { value: "USD" } });

    fireEvent.click(screen.getByRole("button", { name: /create part/i }));

    expect(
      await screen.findByText("A part with this internal part number already exists."),
    ).toBeInTheDocument();
    expect(createCalls).toBe(1);
  });
});
