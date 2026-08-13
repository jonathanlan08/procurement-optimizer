import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { ReviewPane } from "./ReviewPane";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sessionFor(role: string): SessionInfo {
  return {
    user_id: "user-1",
    email: "user@example.com",
    full_name: "Test User",
    organization_id: "org-1",
    organization_name: "Test Org",
    organization_slug: "test-org",
    role,
    csrf_token: "csrf-token-abc",
    demo_mode: false,
  };
}

function renderPane(role: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ReviewPane runId="run-1" onClose={() => {}} />
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

const DOCUMENT: Record<string, unknown> = {
  id: "doc-1",
  rfq_id: "rfq-1",
  supplier_id: null,
  original_filename: "acme-quote.pdf",
  detected_mime: "application/pdf",
  declared_mime: "application/pdf",
  size_bytes: 245678,
  content_sha256: null,
  state: "stored",
  quarantine_reason: null,
  page_count: 2,
  uploaded_by_id: "user-1",
  uploaded_at: "2026-08-01T00:00:00Z",
  is_archived: false,
  archived_at: null,
  archive_reason: null,
};

const DOCUMENT_HANDLER = {
  test: (url: string, method: string) => url === "/api/v1/quote-documents/doc-1" && method === "GET",
  respond: () => jsonResponse(200, DOCUMENT),
};

function runFixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "run-1",
    document_id: "doc-1",
    run_number: 1,
    state: "needs_review",
    provider: "mock",
    provider_model: null,
    simulated: true,
    schema_version: "1.0",
    prompt_version: "v1",
    overall_confidence: "0.812345",
    fields_requiring_review: 2,
    injection_suspected: true,
    error: null,
    started_at: "2026-08-01T00:00:00Z",
    finished_at: "2026-08-01T00:00:05Z",
    superseded_at: null,
    ...overrides,
  };
}

function runHandler(run: Record<string, unknown>) {
  return {
    test: (url: string, method: string) => url === "/api/v1/extraction-runs/run-1" && method === "GET",
    respond: () => jsonResponse(200, run),
  };
}

const FIELDS = [
  {
    id: "f-high",
    extraction_run_id: "run-1",
    field_path: "quote_number",
    target_entity: "quote",
    target_line_index: null,
    field_name: "quote_number",
    raw_value: "Q-100",
    normalized_value: "Q-100",
    value_type: "text",
    confidence: "0.980000",
    band: "high",
    requires_confirmation: false,
    is_confirmed: false,
    confirmed_by_id: null,
    confirmed_at: null,
    source_page: 1,
    source_bbox: null,
    injection_flagged: true,
  },
  {
    id: "f-medium",
    extraction_run_id: "run-1",
    field_path: "lines[0].unit_price",
    target_entity: "quote_line",
    target_line_index: 0,
    field_name: "unit_price",
    raw_value: "12.5",
    normalized_value: "12.5",
    value_type: "money",
    confidence: "0.750000",
    band: "medium",
    requires_confirmation: true,
    is_confirmed: false,
    confirmed_by_id: null,
    confirmed_at: null,
    source_page: 1,
    source_bbox: null,
    injection_flagged: true,
  },
  {
    id: "f-low",
    extraction_run_id: "run-1",
    field_path: "lines[0].quantity",
    target_entity: "quote_line",
    target_line_index: 0,
    field_name: "quantity",
    raw_value: "100",
    normalized_value: "100",
    value_type: "decimal",
    confidence: "0.300000",
    band: "low",
    requires_confirmation: true,
    is_confirmed: false,
    confirmed_by_id: null,
    confirmed_at: null,
    source_page: 1,
    source_bbox: null,
    injection_flagged: true,
  },
];

const FIELDS_HANDLER = {
  test: (url: string, method: string) =>
    url === "/api/v1/extraction-runs/run-1/fields" && method === "GET",
  respond: () => jsonResponse(200, { items: FIELDS }),
};

describe("ReviewPane", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers itself - see
    // features/fx/fx.test.tsx's own identical comment for why this is
    // required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the Simulated chip and the injection-suspected warning banner in the run header", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      DOCUMENT_HANDLER,
      runHandler(runFixture()),
      FIELDS_HANDLER,
    ]);

    renderPane("analyst");

    expect(await screen.findByText("Simulated")).toHaveClass("badge--simulated");
    expect(await screen.findByText(/possible prompt injection detected/i)).toBeInTheDocument();
    expect(
      screen.getByText(/ignored during extraction and flagged/i),
    ).toBeInTheDocument();
  });

  it("renders a confidence badge per field, colored by band", async () => {
    installFetchMock([
      AUTH_HANDLER("analyst"),
      DOCUMENT_HANDLER,
      runHandler(runFixture()),
      FIELDS_HANDLER,
    ]);

    renderPane("analyst");

    expect(await screen.findByText("High")).toHaveClass("badge--conf-high");
    expect(screen.getByText("Medium")).toHaveClass("badge--conf-medium");
    expect(screen.getByText("Low")).toHaveClass("badge--conf-low");
  });

  it("hides Confirm/Correct actions from a viewer", async () => {
    installFetchMock([
      AUTH_HANDLER("viewer"),
      DOCUMENT_HANDLER,
      runHandler(runFixture()),
      FIELDS_HANDLER,
    ]);

    renderPane("viewer");

    // Wait for the fields table to render (its own extracted value, unique
    // among page text) before asserting on the actions column's absence.
    expect(await screen.findByText("Q-100")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^confirm$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^correct$/i })).not.toBeInTheDocument();
  });

  it("renders a 409 callout listing the blocking fields when materialize is rejected", async () => {
    const readyRun = runFixture({ state: "ready", injection_suspected: false, fields_requiring_review: 1 });
    installFetchMock([
      AUTH_HANDLER("analyst"),
      DOCUMENT_HANDLER,
      runHandler(readyRun),
      FIELDS_HANDLER,
      {
        test: (url: string, method: string) =>
          url === "/api/v1/rfqs/rfq-1/suppliers" && method === "GET",
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "rs-1",
                rfq_id: "rfq-1",
                supplier_id: "sup-1",
                invited_at: "2026-07-01T00:00:00Z",
                excluded_at: null,
                exclusion_reason: null,
                notes: null,
              },
            ],
          }),
      },
      {
        test: (url: string, method: string) => url === "/api/v1/suppliers/sup-1" && method === "GET",
        respond: () =>
          jsonResponse(200, {
            id: "sup-1",
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
          }),
      },
      {
        test: (url: string, method: string) =>
          url === "/api/v1/extraction-runs/run-1/confirm" && method === "POST",
        respond: () =>
          jsonResponse(409, {
            error: {
              code: "conflict_state",
              message: "Cannot materialize: one or more low-confidence fields are not yet confirmed.",
              status: 409,
              details: [
                { field: "lines[0].quantity", issue: "low-confidence field is not confirmed" },
              ],
              request_id: "req-1",
              timestamp: "2026-08-01T00:00:10Z",
            },
          }),
      },
    ]);

    renderPane("analyst");

    // Wait for the actual <option> (populated only once useRfqSuppliers +
    // its per-row useSupplier both resolve) before selecting it - selecting
    // a value with no matching <option> yet mounted is a silent no-op.
    await screen.findByRole("option", { name: /acme components/i });
    const supplierSelect = screen.getByLabelText(/supplier/i);
    fireEvent.change(supplierSelect, { target: { value: "sup-1" } });
    fireEvent.click(screen.getByRole("button", { name: /create quote/i }));

    // Scoped to the error banner itself: "lines[0].quantity" also appears in
    // the fields table's own field-path column for that same field.
    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByText(/one or more low-confidence fields are not yet confirmed/i),
    ).toBeInTheDocument();
    expect(within(alert).getByText(/low-confidence field is not confirmed/i)).toBeInTheDocument();
    expect(within(alert).getByText(/lines\[0\]\.quantity/i)).toBeInTheDocument();
  });

  // -- "Confirm all remaining" bulk-confirm button (2026-08 audit
  // remediation, wave B) -----------------------------------------------

  describe("Confirm all remaining", () => {
    it("shows the button with the live unconfirmed-requires-confirmation count, for an analyst", async () => {
      installFetchMock([
        AUTH_HANDLER("analyst"),
        DOCUMENT_HANDLER,
        runHandler(runFixture()),
        FIELDS_HANDLER,
      ]);

      renderPane("analyst");

      // FIELDS fixture: f-medium and f-low both requires_confirmation +
      // !is_confirmed; f-high requires_confirmation is false. Count is 2.
      const button = await screen.findByRole("button", { name: /confirm all remaining \(2\)/i });
      expect(button).toBeInTheDocument();
      expect(
        screen.getByText(
          /confirms every remaining flagged field at once - review the values above first/i,
        ),
      ).toBeInTheDocument();
    });

    it("arming shows an explicit warning and can be cancelled without confirming", async () => {
      installFetchMock([
        AUTH_HANDLER("analyst"),
        DOCUMENT_HANDLER,
        runHandler(runFixture()),
        FIELDS_HANDLER,
      ]);

      renderPane("analyst");

      fireEvent.click(
        await screen.findByRole("button", { name: /confirm all remaining \(2\)/i }),
      );
      expect(
        screen.getByText(/including values the document never stated/i),
      ).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
      expect(
        await screen.findByRole("button", { name: /confirm all remaining \(2\)/i }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /yes, confirm/i }),
      ).not.toBeInTheDocument();
    });

    it("hides the button for a viewer even though fields remain unconfirmed", async () => {
      installFetchMock([
        AUTH_HANDLER("viewer"),
        DOCUMENT_HANDLER,
        runHandler(runFixture()),
        FIELDS_HANDLER,
      ]);

      renderPane("viewer");

      await screen.findByText("Q-100");
      expect(
        screen.queryByRole("button", { name: /confirm all remaining/i }),
      ).not.toBeInTheDocument();
    });

    it("hides the button once nothing remains requiring confirmation", async () => {
      const allConfirmed = FIELDS.map((f) => ({ ...f, is_confirmed: true }));
      installFetchMock([
        AUTH_HANDLER("analyst"),
        DOCUMENT_HANDLER,
        runHandler(runFixture({ state: "ready", fields_requiring_review: 0 })),
        {
          test: (url: string, method: string) =>
            url === "/api/v1/extraction-runs/run-1/fields" && method === "GET",
          respond: () => jsonResponse(200, { items: allConfirmed }),
        },
      ]);

      renderPane("analyst");

      await screen.findByText("Q-100");
      expect(
        screen.queryByRole("button", { name: /confirm all remaining/i }),
      ).not.toBeInTheDocument();
    });

    it("posts to fields/confirm-all and reflects the returned Ready run state on success", async () => {
      const readyRun = runFixture({ state: "ready", fields_requiring_review: 0 });
      let confirmAllCalled = false;
      installFetchMock([
        AUTH_HANDLER("analyst"),
        DOCUMENT_HANDLER,
        runHandler(runFixture()),
        {
          // Stateful: mirrors the real API's `is_confirmed` flip so the
          // fields-list invalidation this mutation triggers (./api.ts) is
          // observable here too, not just the run-cache seed.
          test: (url: string, method: string) =>
            url === "/api/v1/extraction-runs/run-1/fields" && method === "GET",
          respond: () =>
            jsonResponse(200, {
              items: confirmAllCalled ? FIELDS.map((f) => ({ ...f, is_confirmed: true })) : FIELDS,
            }),
        },
        {
          test: (url: string, method: string) =>
            url === "/api/v1/extraction-runs/run-1/fields/confirm-all" && method === "POST",
          respond: () => {
            confirmAllCalled = true;
            return jsonResponse(200, readyRun);
          },
        },
      ]);

      renderPane("analyst");

      // Two-step arm/confirm (2026-08 external review P1/P2: one-click bulk
      // confirmation encouraged blind approval) - arming alone must NOT post.
      const button = await screen.findByRole("button", { name: /confirm all remaining \(2\)/i });
      fireEvent.click(button);
      expect(confirmAllCalled).toBe(false);
      expect(
        screen.getByText(/marks all 2 remaining flagged fields as human-confirmed/i),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /yes, confirm 2 fields/i }));

      // Pane refreshes to the returned run state (run-state badge flips to
      // Ready) and the fields-list invalidation this mutation triggers
      // eventually removes the now-zero-count bar too.
      expect(await screen.findByText("Ready")).toHaveClass("badge--run-ready");
      expect(confirmAllCalled).toBe(true);
      await waitFor(() => {
        expect(
          screen.queryByRole("button", { name: /confirm all remaining/i }),
        ).not.toBeInTheDocument();
      });
    });
  });
});
