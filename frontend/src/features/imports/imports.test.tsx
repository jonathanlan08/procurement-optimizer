import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../../auth/session";
import { PartImportPanel } from "./PartImportPanel";

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
        <PartImportPanel />
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

function makeCsvFile(name = "parts.csv"): File {
  return new File(["internal_part_number,name\nPN-1,Widget\n"], name, { type: "text/csv" });
}

function selectFile(input: HTMLElement, file: File) {
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  fireEvent.change(input);
}

const PREVIEW_WITH_ERROR = {
  batch_id: "batch-1",
  rows_total: 2,
  rows_valid: 1,
  rows_invalid: 1,
  rows_duplicate: 0,
  sample_rows: [
    {
      row_number: 1,
      raw_values: { internal_part_number: "PN-1", name: "Widget" },
      normalized_values: { internal_part_number: "PN-1", name: "Widget" },
      disposition: "create",
      errors: [],
      resulting_part_id: null,
    },
    {
      row_number: 2,
      raw_values: { internal_part_number: "", name: "" },
      normalized_values: null,
      disposition: "error",
      errors: [{ field: "internal_part_number", issue: "required value is missing" }],
      resulting_part_id: null,
    },
  ],
  errors: [{ row_number: 2, field: "internal_part_number", issue: "required value is missing" }],
};

const PREVIEW_CLEAN = {
  batch_id: "batch-2",
  rows_total: 2,
  rows_valid: 1,
  rows_invalid: 0,
  rows_duplicate: 1,
  sample_rows: [
    {
      row_number: 1,
      raw_values: { internal_part_number: "PN-1", name: "Widget" },
      normalized_values: { internal_part_number: "PN-1", name: "Widget" },
      disposition: "create",
      errors: [],
      resulting_part_id: null,
    },
    {
      row_number: 2,
      raw_values: { internal_part_number: "PN-1", name: "Widget dup" },
      normalized_values: { internal_part_number: "PN-1", name: "Widget dup" },
      disposition: "skip_duplicate",
      errors: [],
      resulting_part_id: null,
    },
  ],
  errors: [],
};

const AUTH_HANDLER = (role: string) => ({
  test: (url: string, method: string) => url.startsWith("/api/v1/auth/me") && method === "GET",
  respond: () => jsonResponse(200, sessionFor(role)),
});

describe("PartImportPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false` — see parts.test.tsx's
    // identical comment for why this manual cleanup is required.
    cleanup();
    vi.unstubAllGlobals();
  });

  it("hides the import affordance for a viewer", async () => {
    const fetchMock = installFetchMock([AUTH_HANDLER("viewer")]);
    renderPanel();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /import csv\/xlsx/i })).not.toBeInTheDocument();
  });

  it("uploads a file, renders per-row disposition verdicts, and gates commit on hard errors", async () => {
    let uploadCalls = 0;
    installFetchMock([
      AUTH_HANDLER("analyst"),
      {
        test: (url, method) => url === "/api/v1/part-imports" && method === "POST",
        respond: () => {
          uploadCalls += 1;
          return jsonResponse(201, PREVIEW_WITH_ERROR);
        },
      },
    ]);

    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /import csv\/xlsx/i }));
    const dialog = await screen.findByRole("dialog");

    const fileInput = within(dialog).getByLabelText(/part import file/i);
    selectFile(fileInput, makeCsvFile());
    fireEvent.click(within(dialog).getByRole("button", { name: /^upload$/i }));

    await within(dialog).findByText(/previewing/i);
    expect(uploadCalls).toBe(1);

    expect(within(dialog).getByText("Will create")).toHaveClass("badge--import-create");
    expect(within(dialog).getByText("Invalid")).toHaveClass("badge--import-error");
    expect(within(dialog).getByText(/required value is missing/i)).toBeInTheDocument();

    const commitBtn = within(dialog).getByRole("button", { name: /commit import/i });
    expect(commitBtn).toBeDisabled();
    expect(within(dialog).getByText(/1 row\(s\) have errors/i)).toBeInTheDocument();
  });

  it("commits a clean batch, shows the success summary, and posts to the commit route", async () => {
    let commitCalls = 0;
    installFetchMock([
      AUTH_HANDLER("analyst"),
      {
        test: (url, method) => url === "/api/v1/part-imports" && method === "POST",
        respond: () => jsonResponse(201, PREVIEW_CLEAN),
      },
      {
        test: (url, method) => url === "/api/v1/part-imports/batch-2/commit" && method === "POST",
        respond: () => {
          commitCalls += 1;
          return jsonResponse(200, { created: 1, updated: 0, skipped: 1 });
        },
      },
    ]);

    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /import csv\/xlsx/i }));
    const dialog = await screen.findByRole("dialog");

    selectFile(within(dialog).getByLabelText(/part import file/i), makeCsvFile());
    fireEvent.click(within(dialog).getByRole("button", { name: /^upload$/i }));
    await within(dialog).findByText(/previewing/i);

    const commitBtn = within(dialog).getByRole("button", { name: /commit import/i });
    expect(commitBtn).not.toBeDisabled();
    fireEvent.click(commitBtn);

    await within(dialog).findByText(/import committed/i);
    expect(commitCalls).toBe(1);
    expect(within(dialog).getByText(/created/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/skipped/i)).toBeInTheDocument();
    // created=1 and skipped=1 both render as a bare "1" in their own <strong>;
    // updated=0 is the one unambiguous value.
    expect(within(dialog).getAllByText("1")).toHaveLength(2);
    expect(within(dialog).getByText("0")).toBeInTheDocument();
  });

  it("cancels a batch with errors and reports no parts were created", async () => {
    let cancelCalls = 0;
    installFetchMock([
      AUTH_HANDLER("analyst"),
      {
        test: (url, method) => url === "/api/v1/part-imports" && method === "POST",
        respond: () => jsonResponse(201, PREVIEW_WITH_ERROR),
      },
      {
        test: (url, method) => url === "/api/v1/part-imports/batch-1/cancel" && method === "POST",
        respond: () => {
          cancelCalls += 1;
          return jsonResponse(200, {
            id: "batch-1",
            state: "rolled_back",
            source_filename: "parts.csv",
            format: "csv",
            rows_total: 2,
            rows_valid: 1,
            rows_invalid: 1,
            rows_duplicate: 0,
            created_by_id: "user-1",
            created_at: "2026-08-01T00:00:00Z",
            committed_at: null,
          });
        },
      },
    ]);

    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /import csv\/xlsx/i }));
    const dialog = await screen.findByRole("dialog");

    selectFile(within(dialog).getByLabelText(/part import file/i), makeCsvFile());
    fireEvent.click(within(dialog).getByRole("button", { name: /^upload$/i }));
    await within(dialog).findByText(/previewing/i);

    fireEvent.click(within(dialog).getByRole("button", { name: /cancel import/i }));

    await within(dialog).findByText(/was cancelled/i);
    expect(cancelCalls).toBe(1);
  });
});
