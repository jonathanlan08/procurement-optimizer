/** Workspace shell: sidebar navigation + header. PRINCIPAL-OWNED structure. */

import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth/session";
import "./app-shell.css";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/suppliers", label: "Suppliers" },
  { to: "/parts", label: "Parts" },
  { to: "/boms", label: "BOMs" },
  { to: "/rfqs", label: "RFQs" },
  { to: "/fx", label: "FX rates" }, // ALLOWED insertion: one NAV entry
  { to: "/scenarios", label: "Compare" }, // ALLOWED insertion: clarifies this is the comparison workspace
  { to: "/reports", label: "Reports" },
  { to: "/audit", label: "Audit log" },
];

export function AppShell() {
  const { session, logout } = useAuth();
  return (
    <div className="shell">
      <aside className="shell-sidebar">
        <div className="shell-brand">
          <span className="shell-brand-mark" aria-hidden="true" />
          Procurement Optimizer
        </div>
        <nav aria-label="Primary">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end ?? false}
              className={({ isActive }) =>
                isActive ? "shell-nav-link is-active" : "shell-nav-link"
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="shell-main">
        <header className="shell-header">
          {session?.demo_mode && (
            <span className="demo-badge" title="All data in this workspace is synthetic">
              Demo — synthetic data
            </span>
          )}
          <div className="shell-header-spacer" />
          <span className="shell-user">
            {session?.full_name}
            <span className="shell-role">{session?.role}</span>
          </span>
          <button type="button" className="btn-ghost" onClick={() => void logout()}>
            Sign out
          </button>
        </header>
        <main className="shell-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
