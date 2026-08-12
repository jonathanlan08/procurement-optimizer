/** Overview data layer — composes existing feature hooks/endpoints into the
 * workspace summary shown at "/" (see OverviewPage.tsx, which replaces the
 * `PlaceholderPage` that used to sit at the index route — P1 audit finding:
 * "the Overview screen is still an under-construction placeholder").
 *
 * No new backend routes: every count here is `page.total` from a `limit=1`
 * call against a list endpoint the app already calls elsewhere
 * (suppliers/parts/rfqs), and "recent scenarios" walks the same per-RFQ
 * `GET /rfqs/{id}/comparison-scenarios` route ../comparison/api.ts already
 * exposes for ReportsPage.tsx's own scenario picker.
 *
 * **"Recent scenarios" deliberately does not attempt an org-wide feed.**
 * There is no "list every scenario" endpoint — ../comparison/api.ts's own
 * file header and ../reports/ReportsPage.tsx's file header both document
 * this same gap ("There is no org-wide 'list every scenario' endpoint...to
 * pick from directly"). Reconstructing one client-side would mean fetching
 * scenarios for every RFQ in the organization, an unbounded N+1 this
 * codebase explicitly avoids elsewhere (RfqsPage.tsx's own file header: "to
 * keep the list query from becoming an N+1"). Instead, this file exposes
 * `useMostRecentRfq()` — the single most-recently-created RFQ (`GET /rfqs`
 * orders by `created_at DESC` server-side, backend/src/app/repositories/
 * rfq_repository.py) — and OverviewPage.tsx shows *that RFQ's* scenario
 * history (also `created_at DESC` server-side, backend/src/app/services/
 * scenario_service.py's `list_scenarios`) via the existing
 * `useRfqScenarios(rfqId)` hook. This is bounded to exactly two extra
 * requests total (one for the RFQ, one for its scenarios), never one per
 * RFQ, and is an honest "your latest RFQ's scenarios" rather than a
 * fabricated cross-org "recent" feed.
 */

import { useParts } from "../parts/api";
import { useRfqs } from "../rfqs/api";
import { useSuppliers } from "../suppliers/api";

/** `page.total` from a `limit=1` supplier list call — cheapest way to get a
 * count without a dedicated stats endpoint (none exists; see this file's
 * header on not inventing routes). */
export function useSupplierTotal() {
  return useSuppliers({ limit: 1, offset: 0 });
}

export function usePartTotal() {
  return useParts({ limit: 1, offset: 0 });
}

/** Counts RFQs whose workflow `status` is literally `"open"` (the RFQ
 * status vocabulary's own term, RFQ_STATUSES in ../rfqs/api.ts) — not "not
 * yet archived," which would also fold in draft/under_review/awarded/closed. */
export function useOpenRfqTotal() {
  return useRfqs({ status: ["open"], limit: 1, offset: 0 });
}

/** The single most recently created RFQ across the org — see this file's
 * header for why "recent scenarios" is anchored on this RFQ rather than an
 * unbounded per-RFQ scan. */
export function useMostRecentRfq() {
  return useRfqs({ limit: 1, offset: 0 });
}
