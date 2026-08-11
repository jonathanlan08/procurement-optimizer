/** Authentication: login, logout, the role-gated read-only workspace, and
 * the shared error-envelope surfaced on a bad password.
 *
 * Deliberately does NOT use the pre-authenticated storage state
 * (helpers/roles.ts) — this file's whole job is to prove the interactive
 * login/logout UI itself works, so every test here starts unauthenticated.
 */

import { expect, test } from "@playwright/test";
import { DEMO_ANALYST, DEMO_VIEWER } from "./helpers/roles";

test.use({ storageState: { cookies: [], origins: [] } });

async function login(page: import("@playwright/test").Page, email: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test.describe("authentication", () => {
  test("analyst can sign in, reach the workspace, and sign out", async ({ page }) => {
    await login(page, DEMO_ANALYST.email, DEMO_ANALYST.password);

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
    // demo_mode banner (Settings.demo_mode, true by default per seed) —
    // confirms the app knows it's running against synthetic data only.
    await expect(page.getByText("Demo — synthetic data")).toBeVisible();
    await expect(page.locator(".shell-user")).toContainText("Ada Chen");
    await expect(page.locator(".shell-user")).toContainText("analyst");

    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login$/);
    // signed out for real, not just a client-side redirect: a protected
    // route bounces back to /login on a fresh navigation too.
    await page.goto("/rfqs");
    await expect(page).toHaveURL(/\/login$/);
  });

  test("a wrong password surfaces the shared API error envelope's message", async ({ page }) => {
    await login(page, DEMO_ANALYST.email, "not-the-real-password-at-all");

    const alert = page.getByRole("alert");
    await expect(alert).toBeVisible();
    await expect(alert).toHaveText("Invalid email or password.");
    // never navigated away — the SPA renders the error inline, no reload
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  test("viewer role: reads the real seeded workspace with mutation controls absent", async ({
    page,
  }) => {
    await login(page, DEMO_VIEWER.email, DEMO_VIEWER.password);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator(".shell-user")).toContainText("viewer");

    await page.getByRole("link", { name: "RFQs" }).click();
    await expect(page).toHaveURL(/\/rfqs$/);

    // real seeded data is still visible read-only...
    await expect(page.getByRole("row", { name: /RFQ-2026-ENC-PILOT/ })).toBeVisible();
    // ...but the write affordance the analyst role gets is gone entirely
    // (RfqsPage.tsx: `{canWrite && <button>New RFQ</button>}`), not merely
    // disabled — isAnalystOrAbove(role) is false for "viewer".
    await expect(page.getByRole("button", { name: "New RFQ" })).toHaveCount(0);

    // the server enforces this independently of the UI: a direct mutating
    // call as this same viewer session is rejected with 403 forbidden_role,
    // not merely hidden client-side.
    const me = await page.request.get("/api/v1/auth/me");
    const { csrf_token: csrfToken } = (await me.json()) as { csrf_token: string };
    const response = await page.request.post("/api/v1/rfqs", {
      data: { name: "should be rejected" },
      headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": csrfToken },
    });
    expect(response.status()).toBe(403);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe("forbidden_role");
  });
});
