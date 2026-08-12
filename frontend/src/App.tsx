import { Route, Routes } from "react-router-dom";
import { RequireAuth } from "./auth/session";
import { AuditPage } from "./features/audit/AuditPage"; // ALLOWED insertion: /audit route
import { BomsPage } from "./features/boms/BomsPage";
import { ComparisonPage } from "./features/comparison/ComparisonPage"; // ALLOWED insertion: /scenarios route
import { FxPanel } from "./features/fx/FxPanel"; // ALLOWED insertion: /fx route
import { OverviewPage } from "./features/overview/OverviewPage";
import { PartsPage } from "./features/parts/PartsPage";
import { ReportsPage } from "./features/reports/ReportsPage"; // ALLOWED insertion: /reports route
import { RfqsPage } from "./features/rfqs/RfqsPage";
import { SuppliersPage } from "./features/suppliers/SuppliersPage";
import { AppShell } from "./layout/AppShell";
import { LoginPage } from "./pages/LoginPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";

export function App() {
  return (
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
  );
}
