/** Global setup — logs in once per role via the real HTTP API (no browser
 * needed) and persists each session's cookies to disk so most specs can
 * start already authenticated instead of repeating the login UI flow. This
 * keeps total `/api/v1/auth/login` calls low (auth.spec.ts is the only file
 * that still exercises the interactive form) and well under the backend's
 * per-IP auth rate limit even across two full suite runs.
 *
 * auth.spec.ts deliberately does NOT use this storage state — it is the one
 * spec whose job is to prove the interactive login/logout UI itself works.
 */

import { request } from "@playwright/test";
import fs from "node:fs";
import { AUTH_DIR, ANALYST_STORAGE, DEMO_ANALYST, DEMO_VIEWER, VIEWER_STORAGE } from "./helpers/roles";

const BASE_URL = process.env["E2E_BASE_URL"] ?? "http://localhost:5173";

async function loginAndSave(email: string, password: string, storagePath: string): Promise<void> {
  const ctx = await request.newContext({
    baseURL: BASE_URL,
    // OriginCheckMiddleware (backend/src/app/api/middleware.py) requires a
    // matching Origin header on every mutating request, including login —
    // a real browser sends this automatically; this API-only context must
    // set it by hand.
    extraHTTPHeaders: { Origin: BASE_URL },
  });
  try {
    const resp = await ctx.post("/api/v1/auth/login", { data: { email, password } });
    if (!resp.ok()) {
      throw new Error(
        `global-setup: login failed for ${email}: ${resp.status()} ${await resp.text()}`,
      );
    }
    await ctx.storageState({ path: storagePath });
  } finally {
    await ctx.dispose();
  }
}

export default async function globalSetup(): Promise<void> {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  await loginAndSave(DEMO_ANALYST.email, DEMO_ANALYST.password, ANALYST_STORAGE);
  await loginAndSave(DEMO_VIEWER.email, DEMO_VIEWER.password, VIEWER_STORAGE);
}
