import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { BomsPage } from "./BomsPage";

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

const BOM_V1_SUMMARY: Record<string, unknown> = {
  id: "11111111-1111-1111-1111-111111111111",
  root_bom_id: "11111111-1111-1111-1111-111111111111",
  version_number: 1,
  previous_version_id: null,
  name: "Widget Assembly",
  product_name: "Widget",
  status: "superseded",
  notes: null,
  created_by_id: "user-1",
  is_archived: false,
  archived_at: null,
  archive_reason: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const BOM_V2_SUMMARY: Record<string, unknown> = {
  ...BOM_V1_SUMMARY,
  id: "22222222-2222-2222-2222-222222222222",
  version_number: 2,
  previous_version_id: "11111111-1111-1111-1111-111111111111",
  status: "active",
  updated_at: "2026-02-01T00:00:00Z",
};

const BOM_V2_DETAIL: Record<string, unknown> = { ...BOM_V2_SUMMARY, lines: [] };

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BomsPage />
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

describe("BomsPage", () => {
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

  it("renders BOM rows from the mocked list response, including the status badge", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/boms?") && method === "GET",
        respond: () => jsonResponse(200, { items: [BOM_V2_SUMMARY], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    expect(await screen.findByText("Widget Assembly")).toBeInTheDocument();
    expect(screen.getByText("Widget")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("hides mutation buttons for a viewer", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("viewer")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/boms?") && method === "GET",
        respond: () => jsonResponse(200, { items: [BOM_V2_SUMMARY], page: { limit: 50, offset: 0, total: 1 } }),
      },
    ]);

    renderPage();

    await screen.findByText("Widget Assembly");
    expect(screen.queryByRole("button", { name: /new bom/i })).not.toBeInTheDocument();
  });

  it("renders the version chain in ascending order with the current version highlighted", async () => {
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/boms?") && method === "GET",
        respond: () => jsonResponse(200, { items: [BOM_V2_SUMMARY], page: { limit: 50, offset: 0, total: 1 } }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/boms/22222222-2222-2222-2222-222222222222" && method === "GET",
        respond: () => jsonResponse(200, BOM_V2_DETAIL),
      },
      {
        test: (url, method) =>
          url === "/api/v1/boms/22222222-2222-2222-2222-222222222222/versions" && method === "GET",
        respond: () => jsonResponse(200, { items: [BOM_V1_SUMMARY, BOM_V2_SUMMARY] }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/units") && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
    ]);

    renderPage();

    const row = await screen.findByText("Widget Assembly");
    fireEvent.click(row);

    // Wait for the version-history section (chain) to finish loading, then
    // scope to its `<ul>` — the only list rendered in the detail drawer —
    // so this doesn't get tangled up with the "v2" text that also appears
    // in the underlying table row and the Overview section's Version field.
    await screen.findByText("Version history");
    const chainList = screen.getByRole("list");
    const chainButtons = within(chainList).getAllByRole("button");
    expect(chainButtons).toHaveLength(2);
    const [firstButton, secondButton] = chainButtons;
    if (!firstButton || !secondButton) throw new Error("expected two version-chain buttons");

    // v1 (superseded) renders before v2 (active/current) — ascending by
    // version_number, per GET /boms/{id}/versions's documented order.
    expect(firstButton).toHaveTextContent("v1");
    expect(firstButton).not.toHaveAttribute("aria-current");
    expect(secondButton).toHaveTextContent("v2");
    expect(secondButton).toHaveAttribute("aria-current", "true");
  });

  it("requires at least one line and validates quantity as a decimal string on the create form", async () => {
    let createCalls = 0;
    installFetchMock([
      {
        test: (url, method) => url.startsWith("/api/v1/auth/me") && method === "GET",
        respond: () => jsonResponse(200, sessionFor("analyst")),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/boms?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/units") && method === "GET",
        respond: () => jsonResponse(200, { items: [] }),
      },
      {
        test: (url, method) => url === "/api/v1/boms" && method === "POST",
        respond: () => {
          createCalls += 1;
          return jsonResponse(201, { ...BOM_V2_DETAIL, id: "created-id" });
        },
      },
    ]);

    renderPage();

    const newBomBtn = await screen.findByRole("button", { name: /new bom/i });
    fireEvent.click(newBomBtn);

    // Exactly one line renders by default, and it cannot be removed — the
    // form structurally enforces "at least one line required" by hiding the
    // remove control whenever only one line is present.
    await screen.findByLabelText(/^Quantity/i);
    expect(screen.queryByRole("button", { name: /remove line 1/i })).not.toBeInTheDocument();

    // Adding a line makes both lines removable.
    fireEvent.click(screen.getByRole("button", { name: /add line/i }));
    expect(await screen.findByRole("button", { name: /remove line 1/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /remove line 2/i })).toBeInTheDocument();

    // Remove the second line again so only one "Quantity" field remains.
    fireEvent.click(screen.getByRole("button", { name: /remove line 2/i }));
    expect(screen.queryByRole("button", { name: /remove line 1/i })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^Name/i), { target: { value: "Test BOM" } });
    fireEvent.change(screen.getByLabelText(/^Product/i), { target: { value: "Test Product" } });

    const quantityInput = screen.getByLabelText(/^Quantity/i);
    fireEvent.change(quantityInput, { target: { value: "not-a-number" } });

    fireEvent.click(screen.getByRole("button", { name: /create bom/i }));

    expect(await screen.findByText(/must be a decimal number/i)).toBeInTheDocument();
    expect(createCalls).toBe(0);
  });
});
