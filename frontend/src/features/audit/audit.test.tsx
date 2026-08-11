import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuditPage } from "./AuditPage";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuditPage />
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

const EVENT_1 = {
  id: "evt-1",
  occurred_at: "2026-08-01T10:00:00Z",
  event_type: "rfq.status_changed",
  entity_type: "rfq",
  entity_id: "rfq-1",
  actor_user_id: "user-1",
  explanation: "RFQ moved from draft to open.",
  before_state: { status: "draft" },
  after_state: { status: "open" },
  request_id: "req-abc",
};

const EVENT_2 = {
  id: "evt-2",
  occurred_at: "2026-08-02T11:00:00Z",
  event_type: "quote.created",
  entity_type: "quote",
  entity_id: "quote-1",
  actor_user_id: null,
  explanation: null,
  before_state: null,
  after_state: { status: "draft" },
};

const EVENT_3 = {
  id: "evt-3",
  occurred_at: "2026-08-03T12:00:00Z",
  event_type: "scenario.completed",
  entity_type: "comparison_scenario",
  entity_id: "scenario-1",
  actor_user_id: "user-2",
  explanation: "Scenario solved optimally.",
  before_state: { state: "running" },
  after_state: { state: "complete" },
};

const FIRST_PAGE_HANDLER = {
  test: (url: string, method: string) =>
    url.startsWith("/api/v1/audit-events?") && !url.includes("cursor=") && method === "GET",
  respond: () =>
    jsonResponse(200, { items: [EVENT_1, EVENT_2], next_cursor: "cursor-2" }),
};

function secondPageHandler(calls: string[]) {
  return {
    test: (url: string, method: string) => url.includes("cursor=cursor-2") && method === "GET",
    respond: (url: string) => {
      calls.push(url);
      return jsonResponse(200, { items: [EVENT_3], next_cursor: null });
    },
  };
}

describe("AuditPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself — see
    // ../comparison/comparison.test.tsx's identical comment for why this is
    // required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders audit events from mocked API data", async () => {
    installFetchMock([FIRST_PAGE_HANDLER]);

    renderPage();

    expect(await screen.findByText("rfq.status_changed")).toBeInTheDocument();
    const row1 = screen.getByText("rfq.status_changed").closest("tr");
    expect(within(row1 as HTMLElement).getByText("rfq")).toBeInTheDocument();
    expect(within(row1 as HTMLElement).getByText("rfq-1")).toBeInTheDocument();
    expect(within(row1 as HTMLElement).getByText("user-1")).toBeInTheDocument();

    // system-initiated event (no actor, no explanation) renders raw
    // placeholders, never a fabricated name.
    const row2 = screen.getByText("quote.created").closest("tr");
    expect(within(row2 as HTMLElement).getByText("— system")).toBeInTheDocument();
  });

  it("appends items on Load more and passes next_cursor as the cursor param", async () => {
    const secondPageCalls: string[] = [];
    installFetchMock([FIRST_PAGE_HANDLER, secondPageHandler(secondPageCalls)]);

    renderPage();

    await screen.findByText("rfq.status_changed");
    expect(screen.queryByText("scenario.completed")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /load more/i }));

    expect(await screen.findByText("scenario.completed")).toBeInTheDocument();
    // first page's rows are still present — Load more appends, not replaces.
    expect(screen.getByText("rfq.status_changed")).toBeInTheDocument();
    expect(screen.getByText("quote.created")).toBeInTheDocument();

    expect(secondPageCalls).toHaveLength(1);
    expect(secondPageCalls[0]).toContain("cursor=cursor-2");

    // next_cursor is null on the second page — no further "Load more".
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("opens a drawer with the explanation and a pretty-printed before/after diff on row click", async () => {
    installFetchMock([FIRST_PAGE_HANDLER]);

    renderPage();

    fireEvent.click(await screen.findByText("rfq.status_changed"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("RFQ moved from draft to open.")).toBeInTheDocument();
    expect(within(dialog).getByText(/"status": "draft"/)).toBeInTheDocument();
    expect(within(dialog).getByText(/"status": "open"/)).toBeInTheDocument();
    expect(within(dialog).getByText("req-abc")).toBeInTheDocument();
  });

  it("splits a comma-separated event-type filter into repeated query params", async () => {
    let lastUrl = "";
    installFetchMock([
      {
        test: (url: string, method: string) =>
          url.startsWith("/api/v1/audit-events?") && method === "GET",
        respond: (url: string) => {
          lastUrl = url;
          return jsonResponse(200, { items: [], next_cursor: null });
        },
      },
    ]);

    renderPage();
    await screen.findByText("No audit events found");

    fireEvent.change(screen.getByPlaceholderText(/scenario.created/i), {
      target: { value: "rfq.status_changed, quote.created" },
    });

    await waitFor(() => {
      expect(lastUrl).toContain("event_type=rfq.status_changed");
      expect(lastUrl).toContain("event_type=quote.created");
    });
  });
});
