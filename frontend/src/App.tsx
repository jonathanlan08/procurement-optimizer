import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import { RequireAuth } from "./auth/session";
// `.detail-label` (used by RouteLoadingFallback below) lives in this shared
// stylesheet, not app-shell.css - every OTHER consumer of it is a feature
// page, and those are all lazy now, so without this eager import here the
// very first Suspense fallback (before any route chunk has loaded) would
// briefly render unstyled.
import "./components/workspace.css";
import { AppShell } from "./layout/AppShell";
import { LoginPage } from "./pages/LoginPage";

/** Route-level code splitting (2026-08 audit remediation P2: a single
 * ~585KB pre-gzip main chunk, everything eagerly bundled together). Every
 * routed page becomes its own chunk, fetched only when its route is first
 * visited - AppShell (always needed immediately, every authenticated route
 * renders inside it) and LoginPage (the unauthenticated entry point, so
 * there's nothing to defer it behind) stay eager imports per this task's
 * own scope. `PlaceholderPage` is tiny but is still routed via `path="*"`,
 * so it's split too for consistency rather than as a special case. */
const OverviewPage = lazy(() =>
  import("./features/overview/OverviewPage").then((m) => ({ default: m.OverviewPage })),
);
const SuppliersPage = lazy(() =>
  import("./features/suppliers/SuppliersPage").then((m) => ({ default: m.SuppliersPage })),
);
const PartsPage = lazy(() => import("./features/parts/PartsPage").then((m) => ({ default: m.PartsPage })));
const BomsPage = lazy(() => import("./features/boms/BomsPage").then((m) => ({ default: m.BomsPage })));
const RfqsPage = lazy(() => import("./features/rfqs/RfqsPage").then((m) => ({ default: m.RfqsPage })));
const FxPanel = lazy(() => import("./features/fx/FxPanel").then((m) => ({ default: m.FxPanel })));
const ComparisonPage = lazy(() =>
  import("./features/comparison/ComparisonPage").then((m) => ({ default: m.ComparisonPage })),
);
const ReportsPage = lazy(() =>
  import("./features/reports/ReportsPage").then((m) => ({ default: m.ReportsPage })),
);
const AuditPage = lazy(() => import("./features/audit/AuditPage").then((m) => ({ default: m.AuditPage })));
const PlaceholderPage = lazy(() =>
  import("./pages/PlaceholderPage").then((m) => ({ default: m.PlaceholderPage })),
);

/** Shared Suspense fallback for every lazy route above - minimal, centered,
 * reusing the existing `.page-loading` layout (app-shell.css) and
 * `.detail-label` text styling (components/workspace.css) rather than new
 * spinner art, per this task's own "no spinner art" instruction. Both
 * classes are already loaded by the time any route can suspend (AppShell is
 * eager and imports app-shell.css; every feature page that imports
 * workspace.css does so at the top of its own already-loaded module graph),
 * so no extra CSS import is needed here. */
function RouteLoadingFallback() {
  return (
    <div className="page-loading">
      <p className="detail-label">Loading…</p>
    </div>
  );
}

export function App() {
  return (
    <Suspense fallback={<RouteLoadingFallback />}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route index element={<OverviewPage />} />
          <Route path="suppliers" element={<SuppliersPage />} />
          <Route path="parts" element={<PartsPage />} />
          <Route path="boms" element={<BomsPage />} />
          <Route path="rfqs" element={<RfqsPage />} />
          <Route path="fx" element={<FxPanel />} /> {/* ALLOWED insertion */}
          <Route path="scenarios" element={<ComparisonPage />} /> {/* ALLOWED insertion */}
          <Route path="reports" element={<ReportsPage />} /> {/* ALLOWED insertion */}
          <Route path="audit" element={<AuditPage />} /> {/* ALLOWED insertion */}
          <Route path="*" element={<PlaceholderPage title="Not found" />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
