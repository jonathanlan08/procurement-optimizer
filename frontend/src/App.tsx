import { Route, Routes } from "react-router-dom";
import { RequireAuth } from "./auth/session";
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
        <Route path="suppliers" element={<PlaceholderPage title="Suppliers" />} />
        <Route path="parts" element={<PlaceholderPage title="Parts" />} />
        <Route path="boms" element={<PlaceholderPage title="Bills of materials" />} />
        <Route path="rfqs" element={<PlaceholderPage title="RFQs" />} />
        <Route path="scenarios" element={<PlaceholderPage title="Scenarios" />} />
        <Route path="reports" element={<PlaceholderPage title="Reports" />} />
        <Route path="audit" element={<PlaceholderPage title="Audit log" />} />
        <Route path="*" element={<PlaceholderPage title="Not found" />} />
      </Route>
    </Routes>
  );
}
