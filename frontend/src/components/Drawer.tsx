/** Right-side detail/edit drawer, shared by Suppliers and Parts.
 *
 * Design decision (documented per task brief): row click opens a drawer
 * rather than navigating to a detail route. MASTER.md's own "Explainability
 * drawers" pattern ("every calculated number can expand ... This is the
 * product's signature interaction") already establishes drawers as this
 * workspace's idiom for progressive disclosure over a table row, so detail +
 * edit + archive all live in one drawer instead of a `/suppliers/:id` route.
 *
 * Focus-trapped, closes on Escape, restores focus to the trigger on close,
 * and respects `prefers-reduced-motion` (see Drawer.css).
 */

import { useEffect, useRef, type ReactNode } from "react";
import { XIcon } from "./icons";
import "./Drawer.css";

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Stack of open drawer ids so Escape only closes the topmost drawer when
// drawers are stacked (e.g. quote entry on top of the RFQ detail drawer).
const drawerStack: symbol[] = [];

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  /** v2 hue extension (2026-08): extra class(es) appended to the panel's own
   * className, e.g. `"hue-panel-top hue-panel-top--suppliers"` — see
   * components/workspace.css's `.hue-panel-top` comment. Optional and
   * additive only, so every existing `<Drawer>` caller that doesn't pass it
   * (nested/generic drawers with no page-level hue context) renders exactly
   * as before. */
  panelClassName?: string;
}

export function Drawer({ open, onClose, title, children, footer, panelClassName }: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const drawerId = useRef<symbol>(Symbol("drawer"));

  useEffect(() => {
    if (!open) return;

    drawerStack.push(drawerId.current);
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const focusables = panel
      ? Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      : [];
    (focusables[0] ?? panel)?.focus();

    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        // only the topmost drawer in the stack responds
        if (drawerStack[drawerStack.length - 1] !== drawerId.current) return;
        e.stopPropagation();
        onClose();
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
      const idx = drawerStack.indexOf(drawerId.current);
      if (idx >= 0) drawerStack.splice(idx, 1);
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = originalOverflow;
      previouslyFocused.current?.focus();
    };
  }, [open, onClose]);

  return (
    <>
      <div
        className="drawer-overlay"
        data-state={open ? "open" : "closed"}
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        className={panelClassName ? `drawer-panel ${panelClassName}` : "drawer-panel"}
        data-state={open ? "open" : "closed"}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        aria-hidden={!open}
        ref={panelRef}
      >
        <div className="drawer-header">
          <h2 className="drawer-title">{title}</h2>
          <button
            type="button"
            className="drawer-close-btn"
            onClick={onClose}
            aria-label="Close panel"
          >
            <XIcon size={16} />
          </button>
        </div>
        <div className="drawer-body">{children}</div>
        {footer && <div className="drawer-footer">{footer}</div>}
      </div>
    </>
  );
}
