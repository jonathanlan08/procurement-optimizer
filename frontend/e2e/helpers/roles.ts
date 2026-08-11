/** Demo credentials + storage-state paths shared by every spec.
 *
 * Credentials are seeded (and publicly documented) by
 * backend/src/app/seed/demo_dataset.py — see the repo README's "Demo
 * credentials" table. Synthetic organization, synthetic accounts, no real
 * secrets.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const AUTH_DIR = path.join(HERE, "..", ".auth");

export interface DemoCredentials {
  email: string;
  password: string;
}

export const DEMO_ANALYST: DemoCredentials = {
  email: "demo-analyst@meridianfab.example",
  password: "demo-analyst-2026",
};

export const DEMO_VIEWER: DemoCredentials = {
  email: "demo-viewer@meridianfab.example",
  password: "demo-viewer-2026",
};

export const DEMO_OWNER: DemoCredentials = {
  email: "demo-owner@meridianfab.example",
  password: "demo-owner-2026",
};

/** Storage state written by ./global-setup.ts — pre-authenticated cookie
 * jars so specs that aren't specifically testing the login flow itself
 * (auth.spec.ts) can skip the UI login round trip and start authenticated. */
export const ANALYST_STORAGE = path.join(AUTH_DIR, "analyst.json");
export const VIEWER_STORAGE = path.join(AUTH_DIR, "viewer.json");
