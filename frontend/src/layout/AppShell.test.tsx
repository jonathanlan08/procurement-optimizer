/** Mobile nav drawer tests — audit P0 fix: at <=768px the sidebar used to
 * `display: none` with no replacement navigation. These tests exercise the
 * replacement (hamburger -> slide-in drawer reusing the sidebar's nav
 * markup) at the component level: open/close, aria state, focus in on open
 * / focus back to the trigger on close, scrim-click close, and
 * route-change close. CSS media queries aren't evaluated by jsdom, so both
 * the static desktop nav and the drawer's nav are present in the DOM
 * regardless of viewport — tests scope queries to the dialog via `within`
 * where a query would otherwise be ambiguous.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, type SessionInfo } from "../auth/session";
import { AppShell } from "./AppShell";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
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

function installFetchMock() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.endsWith("/api/v1/auth/me")) {
      return jsonResponse(200, sessionFor());
    }
    throw new Error(`Unhandled fetch in AppShell test: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The drawer panel is `aria-hidden="true"` while closed, and
 * dom-testing-library's `getByRole` accessible-name computation does not
 * resolve `aria-label` on an `aria-hidden` element even with `{hidden:
 * true}` (that option only stops the *element* from being filtered out of
 * the search, it doesn't change name computation) — so it's queried
 * directly here instead of via a role+name filter, and its `aria-label` is
 * asserted separately where it matters. */
function getDrawerPanel(): HTMLElement {
  const panel = document.querySelector(".shell-drawer-panel");
  if (!panel) throw new Error("drawer panel not found");
  return panel as HTMLElement;
}

function renderShell(initialPath = "/") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <AuthProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div>Overview content</div>} />
            <Route path="suppliers" element={<div>Suppliers content</div>} />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("AppShell mobile nav drawer", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    installFetchMock();
  });

  afterEach(() => {
    // vite.config.ts sets `test.globals: false`, so @testing-library/react's
    // automatic post-test cleanup never registers (see suppliers.test.tsx's
    // identical comment) — without this, each render() stacks on the
    // previous test's still-mounted DOM.
    cleanup();
    vi.unstubAllGlobals();
    document.body.style.overflow = "";
  });

  it("renders a closed drawer behind a hamburger button by default", async () => {
    renderShell();
    const menuBtn = await screen.findByRole("button", { name: "Open navigation" });
    expect(menuBtn).toHaveAttribute("aria-expanded", "false");

    const dialog = getDrawerPanel();
    expect(dialog).toHaveAttribute("data-state", "closed");
    expect(dialog).toHaveAttribute("aria-hidden", "true");
    expect(dialog).toHaveAttribute("role", "dialog");
    expect(dialog).toHaveAttribute("aria-label", "Primary navigation");
  });

  it("opens on hamburger click, moves focus into the drawer, and Escape closes it and returns focus", async () => {
    renderShell();
    const menuBtn = await screen.findByRole("button", { name: "Open navigation" });

    fireEvent.click(menuBtn);

    expect(menuBtn).toHaveAttribute("aria-expanded", "true");
    const dialog = getDrawerPanel();
    expect(dialog).toHaveAttribute("data-state", "open");
    expect(dialog).toHaveAttribute("aria-hidden", "false");

    await waitFor(() => {
      expect(within(dialog).getByRole("button", { name: "Close navigation" })).toHaveFocus();
    });

    fireEvent.keyDown(document, { key: "Escape" });

    expect(dialog).toHaveAttribute("data-state", "closed");
    expect(menuBtn).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(menuBtn).toHaveFocus());
  });

  it("closes on scrim click", async () => {
    renderShell();
    const menuBtn = await screen.findByRole("button", { name: "Open navigation" });
    fireEvent.click(menuBtn);

    const dialog = getDrawerPanel();
    expect(dialog).toHaveAttribute("data-state", "open");

    const scrim = document.querySelector(".shell-nav-overlay");
    expect(scrim).not.toBeNull();
    fireEvent.click(scrim as Element);

    expect(dialog).toHaveAttribute("data-state", "closed");
  });

  it("closes the drawer when a nav link inside it is clicked and the route changes", async () => {
    renderShell("/");
    await screen.findByText("Overview content");

    const menuBtn = await screen.findByRole("button", { name: "Open navigation" });
    fireEvent.click(menuBtn);

    const dialog = getDrawerPanel();
    const suppliersLink = within(dialog).getByRole("link", { name: "Suppliers" });
    fireEvent.click(suppliersLink);

    await screen.findByText("Suppliers content");
    expect(dialog).toHaveAttribute("data-state", "closed");
    expect(dialog).toHaveAttribute("aria-hidden", "true");
    expect(menuBtn).toHaveAttribute("aria-expanded", "false");
  });

  it("reuses the same nav link list in both the static sidebar and the drawer", async () => {
    renderShell();
    const links = screen.getAllByRole("link", { name: "Suppliers", hidden: true });
    // One in the static <aside>, one in the drawer panel — "one nav, two presentations".
    expect(links).toHaveLength(2);
  });
});
