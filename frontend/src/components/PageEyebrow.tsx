/** Small uppercase "workspace identity" chip shown above a page's `<h1>` in
 * its `.page-heading` block (v2 functional hue extension, 2026-08 — see
 * design-system/procurement-optimizer/MASTER.md's own "v2" section and
 * tokens.css's hue token comments). No shared page-header *component*
 * existed before this — every page already follows the same
 * `<header className="page-toolbar"><div className="page-heading"><h1>…`
 * markup (components/workspace.css), so this slots into that existing
 * pattern rather than inventing a new header shape.
 *
 * Only the seven workspaces the design director assigned a hue to render
 * one (Overview/Suppliers/Parts/RFQs/Compare/Reports/Audit) — BOMs and FX
 * have no `HueName` values here and are intentionally left off (no color to
 * apply that the spec actually defined).
 */
import "./workspace.css";

export type HueName = "overview" | "suppliers" | "parts" | "rfqs" | "compare" | "reports" | "audit";

export function PageEyebrow({ hue, children }: { hue: HueName; children: string }) {
  return <span className={`page-eyebrow page-eyebrow--${hue}`}>{children}</span>;
}
