import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { OverviewPage } from "./OverviewPage";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sessionFor(demoMode: boolean): SessionInfo {
  return {
    user_id: "user-1",
    email: "analyst@example.com",
    full_name: "Test User",
    organization_id: "org-1",
    organization_name: "Meridian Fabrication",
    organization_slug: "meridian",
    role: "analyst",
    csrf_token: "csrf-token-abc",
    demo_mode: demoMode,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <OverviewPage />
        </AuthProvider>
      </QueryClientProvider>
    </MemoryRouter>,
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

const AUTH_HANDLER = (demoMode: boolean) => ({
  test: (url: string, method: string) => url.startsWith("/api/v1/auth/me") && method === "GET",
  respond: () => jsonResponse(200, sessionFor(demoMode)),
});

const RECENT_RFQ = {
  id: "rfq-1",
  name: "Q3 Connector Sourcing",
  internal_reference: "RFQ-1001",
  status: "open",
  base_currency: "USD",
  due_date: "2026-09-01",
  requested_delivery_date: null,
  source_bom_id: null,
  is_archived: false,
  version: 1,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

describe("OverviewPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false` — see parts.test.tsx's
    // identical comment for why this manual cleanup is required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders workspace counts, the latest RFQ's recent scenarios, and quick links from mocked API data", async () => {
    installFetchMock([
      AUTH_HANDLER(true),
      {
        test: (url, method) => url.startsWith("/api/v1/suppliers?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 1, offset: 0, total: 12 } }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/parts?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 1, offset: 0, total: 340 } }),
      },
      {
        test: (url, method) =>
          url.startsWith("/api/v1/rfqs?") && url.includes("status=open") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 1, offset: 0, total: 4 } }),
      },
      {
        test: (url, method) =>
          url.startsWith("/api/v1/rfqs?") && !url.includes("status=open") && method === "GET",
        respond: () => jsonResponse(200, { items: [RECENT_RFQ], page: { limit: 1, offset: 0, total: 9 } }),
      },
      {
        test: (url, method) =>
          url === "/api/v1/rfqs/rfq-1/comparison-scenarios?limit=50&offset=0" && method === "GET",
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "scenario-1",
                rfq_id: "rfq-1",
                name: "Balanced award",
                strategy: "balanced",
                state: "complete",
                created_by_id: "user-1",
                created_at: "2026-08-05T00:00:00Z",
                completed_at: "2026-08-05T00:01:00Z",
                is_archived: false,
              },
              {
                id: "scenario-2",
                rfq_id: "rfq-1",
                name: "Lowest cost draft",
                strategy: "lowest_landed_cost",
                state: "draft",
                created_by_id: "user-1",
                created_at: "2026-08-04T00:00:00Z",
                completed_at: null,
                is_archived: false,
              },
            ],
            page: { limit: 50, offset: 0, total: 2 },
          }),
      },
    ]);

    renderPage();

    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(screen.getByText("340")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();

    expect(screen.getByText(/Demo — synthetic data/i)).toBeInTheDocument();

    expect(await screen.findByText("RFQ-1001")).toBeInTheDocument();
    expect(screen.getByText("Balanced award")).toBeInTheDocument();
    expect(screen.getByText("Complete")).toHaveClass("badge--overview-scenario-complete");
    expect(screen.getByText("Lowest cost draft")).toBeInTheDocument();
    expect(screen.getByText("Draft")).toHaveClass("badge--overview-scenario-draft");

    const linksNav = screen.getByRole("navigation", { name: /workspace quick links/i });
    expect(within(linksNav).getByRole("link", { name: /suppliers/i })).toHaveAttribute("href", "/suppliers");
    expect(within(linksNav).getByRole("link", { name: /parts/i })).toHaveAttribute("href", "/parts");
    expect(within(linksNav).getByRole("link", { name: /audit log/i })).toHaveAttribute("href", "/audit");
  });

  it("shows an honest empty state when the org has no RFQs yet, instead of fabricating scenario data", async () => {
    installFetchMock([
      AUTH_HANDLER(false),
      {
        test: (url, method) => url.startsWith("/api/v1/suppliers?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 1, offset: 0, total: 0 } }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/parts?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 1, offset: 0, total: 0 } }),
      },
      {
        test: (url, method) => url.startsWith("/api/v1/rfqs?") && method === "GET",
        respond: () => jsonResponse(200, { items: [], page: { limit: 1, offset: 0, total: 0 } }),
      },
    ]);

    renderPage();

    expect(await screen.findByText(/No RFQs yet/i)).toBeInTheDocument();
    // Not the seeded "demo" note when demo_mode is false.
    expect(screen.queryByText(/Demo — synthetic data/i)).not.toBeInTheDocument();
  });
});
