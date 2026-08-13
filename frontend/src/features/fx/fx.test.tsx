import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { FxPanel } from "./FxPanel";

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

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <FxPanel />
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

// `GET /exchange-rates` serves two shapes off one route (api/v1/fx.py) -
// the effective-lookup branch always carries `as_of=`, the plain override-
// history list never does (see features/fx/api.ts's file header). These two
// handlers distinguish the branches by that query param alone, mirroring
// the backend's own all-three-or-none dispatch.
const OVERRIDE_LIST_HANDLER = {
  test: (url: string, method: string) =>
    url.startsWith("/api/v1/exchange-rates?") && !url.includes("as_of=") && method === "GET",
  respond: () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
};

function effectiveHandler(body: Record<string, unknown>) {
  return {
    test: (url: string, method: string) =>
      url.startsWith("/api/v1/exchange-rates?") && url.includes("as_of=") && method === "GET",
    respond: () => jsonResponse(200, body),
  };
}

describe("FxPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself - see rfqs.test.tsx
    // / boms.test.tsx's own identical comment for why this is required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the synthetic-fixture source badge when the fixture answers the lookup", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      effectiveHandler({
        base_currency: "USD",
        quote_currency: "EUR",
        as_of: "2026-08-10",
        rate: "0.920000000000",
        source: "synthetic_fixture",
        is_manual_override: false,
        override_reason: null,
        effective_date: "2026-08-10",
        exchange_rate_id: null,
      }),
      OVERRIDE_LIST_HANDLER,
    ]);

    renderPanel();

    expect(await screen.findByText("Synthetic")).toHaveClass("fx-badge--synthetic");
  });

  it("renders the manual-override source badge when a stored override answers the lookup", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      effectiveHandler({
        base_currency: "USD",
        quote_currency: "EUR",
        as_of: "2026-08-10",
        rate: "0.910000000000",
        source: "manual",
        is_manual_override: true,
        override_reason: "Treasury-confirmed rate",
        effective_date: "2026-08-01",
        exchange_rate_id: "eff-1",
      }),
      OVERRIDE_LIST_HANDLER,
    ]);

    renderPanel();

    expect(await screen.findByText("Override")).toHaveClass("fx-badge--manual");
  });

  it("requires a non-blank reason before submitting a new override", async () => {
    let overrideCalls = 0;
    installFetchMock([
      AUTH_HANDLER("analyst"),
      effectiveHandler({
        base_currency: "USD",
        quote_currency: "EUR",
        as_of: "2026-08-10",
        rate: "0.920000000000",
        source: "synthetic_fixture",
        is_manual_override: false,
        override_reason: null,
        effective_date: "2026-08-10",
        exchange_rate_id: null,
      }),
      OVERRIDE_LIST_HANDLER,
      {
        test: (url, method) => url === "/api/v1/exchange-rates" && method === "POST",
        respond: () => {
          overrideCalls += 1;
          return jsonResponse(201, {});
        },
      },
    ]);

    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /add override/i }));
    fireEvent.change(await screen.findByLabelText(/^Rate/i), { target: { value: "0.930000000000" } });

    // `override_reason` is left blank - ExchangeRateOverrideCreate requires
    // it non-blank (fx.py: `Field(min_length=1, ...)`, backed by a DB CHECK).
    fireEvent.click(screen.getByRole("button", { name: /save override/i }));

    expect(await screen.findByText(/reason is required/i)).toBeInTheDocument();
    expect(overrideCalls).toBe(0);
  });
});
