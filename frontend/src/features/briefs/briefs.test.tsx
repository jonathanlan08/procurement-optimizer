import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BriefsPanel, type BriefSupplierOption } from "./BriefsPanel";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
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

const SUPPLIER_OPTIONS: BriefSupplierOption[] = [
  { id: "sup-a", label: "ACME-001 — Acme Components" },
  { id: "sup-b", label: "GLBX-002 — Globex Manufacturing" },
];

const BRIEF_SUMMARY_1 = {
  id: "brief-1",
  scenario_id: "scenario-1",
  supplier_id: "sup-a",
  state: "draft",
  requires_review: true,
  narrative_provider: "template",
  simulated: true,
  created_by_id: "user-1",
  created_at: "2026-08-01T00:00:00Z",
  is_archived: false,
};

const BRIEF_SUMMARY_2 = {
  id: "brief-2",
  scenario_id: "scenario-1",
  supplier_id: "sup-b",
  state: "human_reviewed",
  requires_review: false,
  narrative_provider: "template",
  simulated: false,
  created_by_id: "user-1",
  created_at: "2026-08-02T00:00:00Z",
  is_archived: false,
};

function briefDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: "brief-1",
    scenario_id: "scenario-1",
    supplier_id: "sup-a",
    sections: {
      procurement_objective: {
        provenance: "supplier_provided",
        text: "Reduce total landed cost 10% for this award.",
        data: null,
      },
      supplier_position: {
        provenance: "user_assumption",
        text: "Currently the sole qualified source for this part.",
        data: null,
      },
      quoted_unit_price: {
        provenance: "calculated",
        text: "$12.50/unit",
        data: { amount: "12.50" },
      },
      effective_unit_cost: {
        provenance: "ai_narrative",
        text: "Effective unit cost runs 6% above the quoted price once logistics are included.",
        data: null,
      },
      landed_cost_comparison: {
        provenance: "missing",
        text: "",
        data: null,
      },
      draft_supplier_email: {
        provenance: "calculated",
        text: "Drafted from the price-target gap.",
        data: null,
      },
    },
    price_target: "1000.00",
    stretch_target: "950.00",
    walk_away_threshold: null,
    draft_email_subject: "RE: Widget pricing",
    draft_email_body: "Hi team,\n\nWe would like to propose a revised unit price.",
    narrative_provider: "template",
    simulated: true,
    narrative_is_generated: false,
    state: "draft",
    requires_review: true,
    reviewed_by_id: null,
    reviewed_at: null,
    created_by_id: "user-1",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    version: 1,
    is_archived: false,
    archived_at: null,
    archive_reason: null,
    ...overrides,
  };
}

function renderPanel(canWrite = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BriefsPanel scenarioId="scenario-1" supplierOptions={SUPPLIER_OPTIONS} canWrite={canWrite} />
    </QueryClientProvider>,
  );
}

const LIST_HANDLER = {
  test: (url: string, method: string) =>
    url === "/api/v1/comparison-scenarios/scenario-1/negotiation-briefs" && method === "GET",
  respond: () =>
    jsonResponse(200, {
      items: [BRIEF_SUMMARY_1, BRIEF_SUMMARY_2],
      page: { limit: 50, offset: 0, total: 2 },
    }),
};

function detailHandler(body: Record<string, unknown>) {
  return {
    test: (url: string, method: string) =>
      url === "/api/v1/negotiation-briefs/brief-1" && method === "GET",
    respond: () => jsonResponse(200, body),
  };
}

describe("BriefsPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself — same reason
    // ../comparison/comparison.test.tsx/../fx/fx.test.tsx call this by hand.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the brief list from mocked API data with state chips", async () => {
    installFetchMock([LIST_HANDLER]);

    renderPanel();

    const row1 = (await screen.findByText("ACME-001 — Acme Components")).closest("button");
    expect(row1).not.toBeNull();
    expect(within(row1 as HTMLElement).getByText("Draft")).toBeInTheDocument();
    expect(within(row1 as HTMLElement).getByText("Needs review")).toBeInTheDocument();
    expect(within(row1 as HTMLElement).getByText("Template")).toBeInTheDocument();

    const row2 = (await screen.findByText("GLBX-002 — Globex Manufacturing")).closest("button");
    expect(within(row2 as HTMLElement).getByText("Human reviewed")).toBeInTheDocument();
    expect(within(row2 as HTMLElement).queryByText("Needs review")).not.toBeInTheDocument();
  });

  it("shows all five provenance badge styles and the requires-review banner for a draft brief", async () => {
    installFetchMock([LIST_HANDLER, detailHandler(briefDetail())]);

    renderPanel();

    fireEvent.click(await screen.findByText("ACME-001 — Acme Components"));

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Draft — requires human review")).toBeInTheDocument();
    expect(
      within(dialog).getByText("Template narrative — deterministic, not AI-generated"),
    ).toBeInTheDocument();

    expect(within(dialog).getByText("Supplier-provided")).toHaveClass(
      "badge--brief-provenance-supplier_provided",
    );
    expect(within(dialog).getByText("User assumption")).toHaveClass(
      "badge--brief-provenance-user_assumption",
    );
    expect(within(dialog).getAllByText("Calculated")[0]).toHaveClass(
      "badge--brief-provenance-calculated",
    );
    expect(within(dialog).getByText("AI narrative")).toHaveClass(
      "badge--brief-provenance-ai_narrative",
    );
    expect(within(dialog).getByText("Missing")).toHaveClass("badge--brief-provenance-missing");

    // draft-email callout: unmistakable "never sends" warning + copy-only button
    expect(within(dialog).getByText(/this system never sends emails/i)).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: /copy email to clipboard/i }),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("link")).not.toBeInTheDocument();
  });

  it("does not show the requires-review banner once a brief is human_reviewed", async () => {
    installFetchMock([
      LIST_HANDLER,
      detailHandler(
        briefDetail({
          id: "brief-1",
          state: "human_reviewed",
          requires_review: false,
          reviewed_by_id: "user-2",
          reviewed_at: "2026-08-05T00:00:00Z",
        }),
      ),
    ]);

    renderPanel();

    fireEvent.click(await screen.findByText("ACME-001 — Acme Components"));

    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText(/^Reviewed/);
    expect(within(dialog).queryByText("Draft — requires human review")).not.toBeInTheDocument();
  });

  it("fires the review POST with the entered notes and updates the brief in place", async () => {
    let reviewBody: unknown = null;
    let reviewCalls = 0;
    installFetchMock([
      LIST_HANDLER,
      detailHandler(briefDetail()),
      {
        test: (url: string, method: string) =>
          url === "/api/v1/negotiation-briefs/brief-1/review" && method === "POST",
        respond: (_url, init) => {
          reviewCalls += 1;
          reviewBody = init?.body ? JSON.parse(init.body as string) : null;
          return jsonResponse(
            200,
            briefDetail({
              state: "human_reviewed",
              requires_review: false,
              reviewed_by_id: "user-1",
              reviewed_at: "2026-08-06T00:00:00Z",
            }),
          );
        },
      },
    ]);

    renderPanel();

    fireEvent.click(await screen.findByText("ACME-001 — Acme Components"));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("Draft — requires human review");

    fireEvent.change(within(dialog).getByPlaceholderText("Notes for the record"), {
      target: { value: "Confirmed price target with the buyer." },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /mark reviewed/i }));

    // BRIEF_SUMMARY_2's list row also reads "Human reviewed" — scope the
    // wait to the drawer's own review paragraph, not the ambient list.
    await within(dialog).findByText(/^Reviewed/);
    expect(reviewCalls).toBe(1);
    expect(reviewBody).toEqual({
      approved: true,
      reviewer_notes: "Confirmed price target with the buyer.",
    });
    expect(within(dialog).queryByText("Draft — requires human review")).not.toBeInTheDocument();
  });

  it("posts the correct body when generating a brief with objective and target overrides", async () => {
    let createBody: unknown = null;
    installFetchMock([
      LIST_HANDLER,
      {
        test: (url: string, method: string) =>
          url === "/api/v1/comparison-scenarios/scenario-1/negotiation-briefs" &&
          method === "POST",
        respond: (_url, init) => {
          createBody = init?.body ? JSON.parse(init.body as string) : null;
          return jsonResponse(201, briefDetail({ id: "brief-3", supplier_id: "sup-b" }));
        },
      },
      {
        test: (url: string, method: string) =>
          url === "/api/v1/negotiation-briefs/brief-3" && method === "GET",
        respond: () => jsonResponse(200, briefDetail({ id: "brief-3", supplier_id: "sup-b" })),
      },
    ]);

    renderPanel();

    // Opens the form; the same toggle button's label flips to "Cancel" while
    // open, so the form's own submit button is the only "Generate brief"
    // match from this point on.
    fireEvent.click(await screen.findByRole("button", { name: /generate brief/i }));

    fireEvent.change(screen.getByLabelText(/^Supplier/i), { target: { value: "sup-b" } });
    fireEvent.change(screen.getByPlaceholderText(/reduce unit price/i), {
      target: { value: "Hold lead time, cut price 8%" },
    });
    fireEvent.change(screen.getByLabelText(/^Price target/i), { target: { value: "900.00" } });

    fireEvent.click(screen.getByRole("button", { name: /^generate brief$/i }));

    // Success re-opens the drawer on the newly created brief; wait on its
    // requires-review banner (briefDetail()'s default) as the completion signal.
    await screen.findByText("Draft — requires human review");

    expect(createBody).toEqual({
      supplier_id: "sup-b",
      objective: "Hold lead time, cut price 8%",
      targets: { price_target: "900.00" },
    });
  });

  it("hides the generate-brief affordance for read-only roles", async () => {
    installFetchMock([LIST_HANDLER]);

    renderPanel(false);

    await screen.findByText("ACME-001 — Acme Components");
    expect(screen.queryByRole("button", { name: /generate brief/i })).not.toBeInTheDocument();
  });
});
