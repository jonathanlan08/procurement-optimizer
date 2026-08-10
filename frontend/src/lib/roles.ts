/** Client-side role hierarchy helpers.
 *
 * Mirrors the backend's `ROLE_ORDER` (backend/src/app/api/deps.py) purely for
 * UI gating (show/hide affordances). The backend re-checks every request —
 * these helpers never substitute for server-side authorization.
 */

export type Role = "viewer" | "analyst" | "administrator" | "owner";

const ROLE_ORDER: Record<Role, number> = {
  viewer: 0,
  analyst: 1,
  administrator: 2,
  owner: 3,
};

function rank(role: string | undefined): number {
  if (role === undefined) return -1;
  return role in ROLE_ORDER ? ROLE_ORDER[role as Role] : -1;
}

/** analyst, administrator, or owner */
export function isAnalystOrAbove(role: string | undefined): boolean {
  return rank(role) >= ROLE_ORDER.analyst;
}

/** administrator or owner */
export function isAdministratorOrAbove(role: string | undefined): boolean {
  return rank(role) >= ROLE_ORDER.administrator;
}
