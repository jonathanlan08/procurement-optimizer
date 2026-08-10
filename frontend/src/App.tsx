import { Route, Routes } from "react-router-dom";
import { RequireAuth } from "./auth/session";
import { BomsPage } from "./features/boms/BomsPage";
import { PartsPage } from "./features/parts/PartsPage";
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
        <Route index element={<PlaceholderPage title="Overview" />} />
        <Route path="suppliers" element={<SuppliersPage />} />
        <Route path="parts" element={<PartsPage />} />
        <Route path="boms" element={<BomsPage />} />
        <Route path="rfqs" element={<RfqsPage />} />
        <Route path="scenarios" element={<PlaceholderPage title="Scenarios" />} />
        <Route path="reports" element={<PlaceholderPage title="Reports" />} />
        <Route path="audit" element={<PlaceholderPage title="Audit log" />} />
        <Route path="*" element={<PlaceholderPage title="Not found" />} />
      </Route>
    </Routes>
  );
}
