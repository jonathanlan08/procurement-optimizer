/** Session-probe resilience — QA-sweep regression: the mount-time
 * `/api/v1/auth/me` probe used to treat ANY failure as "signed out", so a
 * 429 from the shared-IP rate limiter (or a 5xx/network blip) bounced a
 * valid session to the login page on refresh. Only a 401 proves
 * unauthenticated; other failures get exactly one retry.
 */

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth, type SessionInfo } from "./session";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorBody(status: number, code: string): unknown {
  return {
    error: {
      code,
      message: `${code} error`,
      status,
      details: [],
      request_id: "req-1",
      timestamp: "2026-08-12T00:00:00Z",
    },
  };
}

function sessionFor(): SessionInfo {
  return {
    user_id: "user-1",
    email: "analyst@example.com",
    full_name: "Test User",
    organization_id: "org-1",
    organization_name: "Test Org",
    organization_slug: "test-org",
    role: "analyst",
    csrf_token: "csrf-token-abc",
    demo_mode: false,
  };
}

function Probe() {
  const { session } = useAuth();
  return <div data-testid="probe">{session ? session.email : "signed-out"}</div>;
}

function installProbeMock(responses: Array<() => Response>) {
  let call = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.endsWith("/api/v1/auth/me")) {
      const make = responses[Math.min(call, responses.length - 1)];
      call += 1;
      if (!make) throw new Error("no probe response configured");
      return make();
    }
    throw new Error(`Unhandled fetch in session test: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("mount-time session probe", () => {
  it("treats a 401 as signed out without retrying", async () => {
    const fetchMock = installProbeMock([() => jsonResponse(401, errorBody(401, "unauthenticated"))]);
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("signed-out"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("recovers the session when a transient 429 is followed by a 200", async () => {
    vi.useFakeTimers();
    const fetchMock = installProbeMock([
      () => jsonResponse(429, errorBody(429, "rate_limited")),
      () => jsonResponse(200, sessionFor()),
    ]);
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await vi.advanceTimersByTimeAsync(1600);
    vi.useRealTimers();
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("analyst@example.com"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("gives up (signed out) after the single retry also fails", async () => {
    vi.useFakeTimers();
    const fetchMock = installProbeMock([() => jsonResponse(429, errorBody(429, "rate_limited"))]);
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await vi.advanceTimersByTimeAsync(1600);
    vi.useRealTimers();
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("signed-out"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
