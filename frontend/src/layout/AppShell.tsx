/** Workspace shell: sidebar navigation + header. PRINCIPAL-OWNED structure.
 *
 * Mobile (<=768px) fix for the audit's P0 release blocker: app-shell.css used
 * to `display: none` the sidebar at <=768px with no replacement navigation
 * ("a never-fulfilled promise" per the audit). It's replaced here with a top
 * app-bar hamburger that opens the SAME sidebar markup (`<SidebarNav>`,
 * rendered once, used twice — "one nav, two presentations" per the design
 * contract) as a slide-in drawer from the left: overlay + scrim, Escape and
 * scrim-click close, focus moves into the drawer on open and returns to the
 * hamburger button on close, and a route change closes the drawer. The
 * focus-trap/Escape/restore-focus shape mirrors components/Drawer.tsx (this
 * codebase's existing drawer idiom) rather than reusing that component
 * directly, since Drawer.tsx is a right-docked content panel with its own
 * `title`/`children` contract — this is a left-docked nav drawer at the
 * shell layer with a fixed link list, not a generic content slot.
 */

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/session";
import "./app-shell.css";

// `hue` (v2 functional hue extension, 2026-08 — see tokens.css's own "v2"
// comment + design-system/procurement-optimizer/MASTER.md) is only set for
// the seven workspaces the design director actually assigned a hue to.
// BOMs and FX rates have none — they keep today's plain accent-blue active
// state unchanged (app-shell.css's base `.shell-nav-link.is-active` rule),
// rather than this task inventing colors the design spec doesn't define.
const NAV: { to: string; label: string; end?: boolean; hue?: string }[] = [
  { to: "/", label: "Overview", end: true, hue: "overview" },
  { to: "/suppliers", label: "Suppliers", hue: "suppliers" },
  { to: "/parts", label: "Parts", hue: "parts" },
  { to: "/boms", label: "BOMs" },
  { to: "/rfqs", label: "RFQs", hue: "rfqs" },
  { to: "/fx", label: "FX rates" }, // ALLOWED insertion: one NAV entry
  { to: "/scenarios", label: "Compare", hue: "compare" }, // ALLOWED insertion: clarifies this is the comparison workspace
  { to: "/reports", label: "Reports", hue: "reports" },
  { to: "/audit", label: "Audit log", hue: "audit" },
];

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function MenuIcon() {
  return (
    <svg
      width={18}
      height={18}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      width={16}
      height={16}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" />
    </svg>
  );
}

/** The sidebar's brand + link list — shared markup for the static desktop
 * column and the mobile drawer panel (see file header). `identity` is an
 * optional slot between the brand and the nav list: the static desktop
 * <aside> never passes one (the header already shows name/role there), but
 * the mobile drawer does (audit P2 — see AppShell's own render below), since
 * hiding the header's name/role chip at <=768px would otherwise drop that
 * information entirely on mobile rather than relocating it. */
function SidebarNav({ identity }: { identity?: ReactNode }) {
  return (
    <>
      <div className="shell-brand">
        <span className="shell-brand-mark" aria-hidden="true" />
        Procurement Optimizer
      </div>
      {identity}
      <nav aria-label="Primary">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end ?? false}
            data-hue={item.hue}
            className={({ isActive }) =>
              isActive ? "shell-nav-link is-active" : "shell-nav-link"
            }
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
    </>
  );
}

export function AppShell() {
  const { session, logout } = useAuth();
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  const drawerPanelRef = useRef<HTMLDivElement>(null);

  // A route change closes the mobile nav drawer.
  useEffect(() => {
    setDrawerOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  // Open/close side effects: move focus in, trap Tab, close on Escape,
  // lock body scroll, and restore focus to the trigger button on close.
  useEffect(() => {
    if (!drawerOpen) return;

    const panel = drawerPanelRef.current;
    const focusables = panel
      ? Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      : [];
    (focusables[0] ?? panel)?.focus();

    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        setDrawerOpen(false);
        return;
      }
      if (e.key !== "Tab" || !panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) {
        e.preventDefault();
        return;
      }
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = originalOverflow;
      menuBtnRef.current?.focus();
    };
  }, [drawerOpen]);

  function handlePanelKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    // Escape is also handled by the document-level listener above (needed
    // so it works even if focus is on the scrim); this is a harmless no-op
    // duplicate guard kept out of the way — the effect owns the real logic.
    if (e.key === "Escape") e.stopPropagation();
  }

  return (
    <div className="shell">
      <aside className="shell-sidebar">
        <SidebarNav />
      </aside>

      <div
        className="shell-nav-overlay"
        data-state={drawerOpen ? "open" : "closed"}
        onClick={() => setDrawerOpen(false)}
        aria-hidden="true"
      />
      <div
        className="shell-sidebar shell-drawer-panel"
        data-state={drawerOpen ? "open" : "closed"}
        role="dialog"
        aria-modal="true"
        aria-label="Primary navigation"
        aria-hidden={!drawerOpen}
        ref={drawerPanelRef}
        onKeyDown={handlePanelKeyDown}
      >
        <button
          type="button"
          className="shell-drawer-close-btn"
          onClick={() => setDrawerOpen(false)}
          aria-label="Close navigation"
        >
          <CloseIcon />
        </button>
        <SidebarNav
          identity={
            session && (
              <div className="shell-drawer-identity">
                <span className="shell-drawer-identity-name">{session.full_name}</span>
                <span className="shell-role">{session.role}</span>
              </div>
            )
          }
        />
      </div>

      <div className="shell-main">
        <header className="shell-header">
          <button
            type="button"
            className="shell-menu-btn"
            aria-label="Open navigation"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
            ref={menuBtnRef}
          >
            <MenuIcon />
          </button>
          {session?.demo_mode && (
            <span className="demo-badge" title="All data in this workspace is synthetic">
              {/* Audit P2 fix: at <=768px the full label collapsed the header
                  into multiple wrapped rows alongside the username/role chip
                  and Sign out button. Both spans render always — CSS (the
                  <=768px block at the bottom of app-shell.css) shows exactly
                  one per viewport, the same "one nav, two presentations"
                  display:none swap AppShell already uses for the sidebar. */}
              <span className="demo-badge-full">Demo — synthetic data</span>
              <span className="demo-badge-compact">Demo</span>
            </span>
          )}
          <div className="shell-header-spacer" />
          {/* Hidden at <=768px (app-shell.css) — the name/role chip is what
              made the mobile header wrap; it's not lost, just relocated: see
              .shell-drawer-identity below, rendered at the top of the mobile
              nav drawer instead. */}
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
