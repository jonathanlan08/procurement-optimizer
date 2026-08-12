# Design System Master File

> **LOGIC:** When building a specific page, first check `design-system/pages/[page-name].md`.
> If that file exists, its rules **override** this Master file.
> If not, strictly follow the rules below.

---

**Project:** Procurement Optimizer
**Generated:** 2026-08-10 02:50:04
**Category:** B2B Service
**Design Dials:** Variance 5/10 (Balanced / Modern) | Motion 3/10 (Subtle) | Density 8/10 (Dense / Dashboard)

---

## Global Rules

### Color Palette

| Role | Hex | CSS Variable |
|------|-----|--------------|
| Primary | `#0F172A` | `--color-primary` |
| On Primary | `#FFFFFF` | `--color-on-primary` |
| Secondary | `#334155` | `--color-secondary` |
| Accent/CTA | `#0369A1` | `--color-accent` |
| Background | `#F8FAFC` | `--color-background` |
| Foreground | `#020617` | `--color-foreground` |
| Muted | `#E8ECF1` | `--color-muted` |
| Border | `#E2E8F0` | `--color-border` |
| Destructive | `#DC2626` | `--color-destructive` |
| Ring | `#0F172A` | `--color-ring` |

**Color Notes:** Professional navy + blue CTA

**Semantic data colors (principal-approved additions for analytical UI):**

| Role | Hex | CSS Variable | Usage |
|------|-----|--------------|-------|
| Positive delta | `#15803D` | `--color-positive` | Savings, favorable deltas (paired with ▲/▼ glyphs, never color alone) |
| Negative delta | `#DC2626` | `--color-negative` | Cost increases, unfavorable deltas |
| Warning | `#B45309` | `--color-warning` | Low-confidence extractions, assumption-dependent results |
| Info/neutral | `#0369A1` | `--color-info` | Informational badges, calculated-value labels |
| Confidence high | `#15803D` | `--color-conf-high` | Extraction confidence ≥ 0.95 |
| Confidence medium | `#B45309` | `--color-conf-med` | Extraction confidence 0.60–0.95 (requires review) |
| Confidence low | `#DC2626` | `--color-conf-low` | Extraction confidence < 0.60 (must confirm) |

> Confidence band thresholds (0.95 / 0.60) are defined once in the backend
> (`app/domain/confidence.py` constant) and mirrored here; the backend constant is the source of truth.

Data-source provenance labels (supplier-provided / user assumption / calculated / AI narrative /
missing) each get a distinct badge style; never rely on color alone — always include a text label.

### Typography

- **Heading Font:** Lexend
- **Body Font:** Source Sans 3
- **Mood:** corporate, trustworthy, accessible, readable, professional, clean
- **Google Fonts:** [Lexend + Source Sans 3](https://fonts.googleapis.com/css2?family=Lexend:wght@300;400;500;600;700&family=Source+Sans+3:wght@300;400;500;600;700&display=swap)

**CSS Import:**

> **Principal's ruling:** fonts are **self-hosted via `@fontsource/lexend` and
> `@fontsource/source-sans-3`** — no Google Fonts CDN. The strict CSP forbids external hosts and
> the demo/E2E suite must run with zero network egress. Do not add the CDN `@import`.

### Spacing Variables

*Density: 8/10 — Dense / Dashboard*

| Token | Value | Usage |
|-------|-------|-------|
| `--space-xs` | `2px` / `0.125rem` | Tight gaps |
| `--space-sm` | `4px` / `0.25rem` | Icon gaps, inline spacing |
| `--space-md` | `8px` / `0.5rem` | Standard padding |
| `--space-lg` | `12px` / `0.75rem` | Section padding |
| `--space-xl` | `16px` / `1rem` | Large gaps |
| `--space-2xl` | `24px` / `1.5rem` | Section margins |
| `--space-3xl` | `32px` / `2rem` | Hero padding |

### Shadow Depths

| Level | Value | Usage |
|-------|-------|-------|
| `--shadow-sm` | `0 1px 2px rgba(0,0,0,0.05)` | Subtle lift |
| `--shadow-md` | `0 4px 6px rgba(0,0,0,0.1)` | Cards, buttons |
| `--shadow-lg` | `0 10px 15px rgba(0,0,0,0.1)` | Modals, dropdowns |
| `--shadow-xl` | `0 20px 25px rgba(0,0,0,0.15)` | Hero images, featured cards |

---

## Component Specs

### Buttons

```css
/* Primary Button */
.btn-primary {
  background: #0369A1;
  color: white;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
}

.btn-primary:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}

/* Secondary Button */
.btn-secondary {
  background: transparent;
  color: #0F172A;
  border: 2px solid #0F172A;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
}
```

### Cards

```css
/* Cards are white on the page background; cursor/hover-lift ONLY on interactive cards */
.card {
  background: #FFFFFF;
  border: 1px solid #E2E8F0;
  border-radius: 8px;
  padding: 12px;
  box-shadow: var(--shadow-sm);
}

.card--interactive {
  cursor: pointer;
  transition: box-shadow 150ms ease;
}

.card--interactive:hover {
  box-shadow: var(--shadow-md);
}
```

### Inputs

```css
.input {
  padding: 12px 16px;
  border: 1px solid #E2E8F0;
  border-radius: 8px;
  font-size: 16px;
  transition: border-color 200ms ease;
}

.input:focus {
  border-color: #0F172A;
  outline: none;
  box-shadow: 0 0 0 3px #0F172A20;
}
```

### Modals

```css
.modal-overlay {
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
}

.modal {
  background: white;
  border-radius: 16px;
  padding: 32px;
  box-shadow: var(--shadow-xl);
  max-width: 500px;
  width: 90%;
}
```

---

## Style Guidelines (principal's approved direction — overrides the auto-generated mobile style)

> The database's auto-match ("Enterprise SaaS Mobile" + "App Store Landing") was rejected by the
> design director as inappropriate for a desktop-first B2B analytical product. The direction below
> synthesizes the database's **Data-Dense Dashboard** and **Comparative Analysis Dashboard** styles.

**Style:** Data-Dense Analytical Workspace (desktop-first, light-mode primary, dark-ready tokens)

**Keywords:** data-dense, comparative, professional, calm, trustworthy, finance-grade, explainable

**Product shape:** Left sidebar navigation (240px, collapsible) → workspace with breadcrumb header
(56px) → dense content region. Primary surfaces are data tables, KPI strips, comparison views, and
review panes — not marketing sections. No landing-page pattern; the app opens into the workspace.

**Key surfaces:**
- **Data tables:** sticky headers, 36px row height, zebra-free (use row hover highlight), sortable
  columns, right-aligned numerics with tabular figures (`font-variant-numeric: tabular-nums`).
- **KPI cards:** compact (12px padding), label + value + delta badge with direction arrow.
- **Comparison views:** side-by-side supplier columns, delta indicators (▲/▼ + color + text),
  benchmark line for target price, winning-value highlight.
- **Review panes (extraction/matching):** split view — source document on one side, extracted
  fields with confidence badges on the other; uncertain fields visually demand confirmation.
- **Explainability drawers:** every calculated number can expand to show formula, inputs,
  assumptions, and data source. This is the product's signature interaction.

**Key effects:** subtle 150–200ms transitions, row hover highlighting, skeleton loading for data
regions, focus-visible rings, shadow-sm cards. **No gradients on CTAs, no spring/bounce physics,
no bottom sheets, no scroll-linked hero effects.**

---

## Motion (principal's approved direction)

Motion budget is deliberately minimal for an analytical tool: CSS transitions for all
state changes (150–200ms `ease-out` for hover/focus/expand; 200–300ms for drawer/modal
enter, exit faster than enter; skeleton pulse for loading regions), plus — **v2.1
amendment (2026-08, client-requested)** — the motion.dev vanilla `animate` for exactly
three sanctioned entrance patterns, all defined in `frontend/src/lib/motion.ts` and
nowhere else:

1. **Route entrance** — opacity-only fade (220ms) of the routed `<main>` on navigation.
   Opacity only: a transform on `<main>` would re-anchor the `position: fixed` drawers
   rendered inside it.
2. **Staggered surface entrance** — card-level surfaces (login panels, Overview KPI
   cards/workflow steps) fade up 10px, 60ms apart, 350ms, on MOUNT only. Data refetches
   never animate; tables and rows never animate.
3. **Button press** — CSS-only `:active { translateY(1px) }` on the button classes.

All motion behind `prefers-reduced-motion` guards (`motionEnabled()` renders everything
in its final state, instantly). No scroll-reveal choreography — analytical content must
be visible immediately, never faded in on scroll; a brief mount fade is permitted, a
scroll-triggered one is not. Rule of thumb: motion marks *arriving somewhere*, never
*data changing*.

---

## Anti-Patterns (Do NOT Use)

- ❌ Playful design
- ❌ Hidden credentials
- ❌ AI purple/pink gradients

### Additional Forbidden Patterns

- ❌ **Emojis as icons** — Use SVG icons (Heroicons, Lucide, Simple Icons)
- ❌ **Missing cursor:pointer** — All clickable elements must have cursor:pointer
- ❌ **Layout-shifting hovers** — Avoid scale transforms that shift layout
- ❌ **Low contrast text** — Maintain 4.5:1 minimum contrast ratio
- ❌ **Instant state changes** — Always use transitions (150-300ms)
- ❌ **Invisible focus states** — Focus states must be visible for a11y

---

## Pre-Delivery Checklist

Before delivering any UI code, verify:

- [ ] No emojis used as icons (use SVG instead)
- [ ] All icons from consistent icon set (Heroicons/Lucide)
- [ ] `cursor-pointer` on all clickable elements
- [ ] Hover states with smooth transitions (150-300ms)
- [ ] Light mode: text contrast 4.5:1 minimum
- [ ] Focus states visible for keyboard navigation
- [ ] `prefers-reduced-motion` respected
- [ ] Responsive: 375px, 768px, 1024px, 1440px
- [ ] No content hidden behind fixed navbars
- [ ] No horizontal scroll on mobile

---

## v2 — functional hue extension (2026-08)

Client feedback asked for "a more colorful UI." The design director's response is a
**functional hue system**, not a decorative palette: seven workspace-identity colors, one per
top-level section of the app, used only to help a user recognize *which workspace they're in* at
a glance. This section is additive to everything above — the v1 rules (semantic data colors,
buttons, tables, motion, anti-patterns) are unchanged and take precedence wherever the two could
conflict.

### The rule

> **Hues identify PLACES. Semantic colors identify STATES. The two never swap roles.**

A hue (`--hue-suppliers`, `--hue-rfqs`, …) never carries meaning like "this failed" or "this is a
warning" — that's still exclusively `--color-positive`/`--color-negative`/`--color-warning`/
`--color-info` (v1, above). A semantic color never gets reused to imply "you're in the Suppliers
workspace." Badges, buttons, tables, money figures, charts, and score bars are v1-only territory
and stay untouched by hues — this is enforced by omission: hues were only ever wired into the six
element categories below.

### The seven hues

| Workspace | Hex | CSS variable | 10%-tint variable |
|-----------|-----|---------------|--------------------|
| Overview | `#4338CA` | `--hue-overview` | `--hue-overview-tint` |
| Suppliers | `#0F766E` | `--hue-suppliers` | `--hue-suppliers-tint` |
| Parts | `#6D28D9` | `--hue-parts` | `--hue-parts-tint` |
| RFQs | `#A24B08` | `--hue-rfqs` | `--hue-rfqs-tint` |
| Compare | `#0369A1` | `--hue-compare` | `--hue-compare-tint` |
| Reports | `#137337` | `--hue-reports` | `--hue-reports-tint` |
| Audit | `#BE123C` | `--hue-audit` | `--hue-audit-tint` |

Each hue is >=4.5:1 as text against white and against its own 10%-alpha tint pill (verified
per-pair; see frontend/e2e/a11y.spec.ts's zero-violation gate). **BOMs and FX rates have no
assigned hue** — the design director scoped this to seven workspaces, not all nine NAV entries,
so those two keep their plain accent-blue treatment everywhere below rather than this task
inventing colors the spec never defined.

A second set, `--hue-<name>-on-dark`, exists purely as an accessibility derivative: the seven
hues above are calibrated for light surfaces, and the one dark surface in the app (the sidebar,
`--color-primary` navy) fails 4.5:1 for several of them as text. Each `-on-dark` token is the same
hue lightened just enough to clear 4.5:1 against navy specifically — used only for the active
sidebar nav-link *text* color; the nav-link's left border uses the base hue unchanged (a
decorative, non-text element).

### Where hues may appear (and nowhere else)

a. **Sidebar nav** (`layout/AppShell.tsx` + `app-shell.css`) — the active item gets a 3px
   left border in its hue plus hue-colored link text (via the `-on-dark` variant). Inactive items
   are unchanged. The static desktop `<aside>` and the mobile drawer render the same
   `<SidebarNav>` markup, so this is one CSS change applied in both presentations.
b. **Page header eyebrow chip** (`components/PageEyebrow.tsx`, styled in
   `components/workspace.css`) — a small uppercase chip (hue text on its own 10%-tint pill)
   above each hued workspace's `<h1>`, added to the shared `.page-toolbar > .page-heading`
   pattern every page already uses.
c. **Overview KPI cards** (`features/overview/overview.css`) — the Suppliers/Parts/Open-RFQs
   cards each get their hue as: a 10%-tint background, a 3px hue border-top, and hue-colored
   count text. The Workspaces quick-link cards get a 3px hue left border (six of the eight links
   that have an assigned hue; BOMs/FX keep a plain gray left border).
d. **One primary card/panel per workspace page** — a 3px hue border-top, via the shared
   `.hue-panel-top` / `.hue-panel-top--<hue>` classes in `components/workspace.css`:
   - Suppliers, Parts, RFQs, Audit: their one `<Drawer>` (via its new optional `panelClassName`
     prop — every other `Drawer` call site that doesn't pass one is unaffected).
   - Compare: its one `<Drawer>` (the per-line landed-cost "explain" panel).
   - Overview: the "Recent scenarios" card (the page's primary card; the "Workspaces" quick-link
     card beside it is secondary and unmarked).
   - Reports: the "Generate report" section (the page's one write-oriented primary panel; the
     report history table below it is read-only and untouched).
e. **Sidebar brand block** (`.shell-brand`, `app-shell.css`) — `linear-gradient(135deg, #0F172A,
   #312E81)`, navy to indigo. This is the **only** gradient anywhere in the app; the v1
   "no gradients on CTAs" rule is unaffected because this isn't a CTA.
f. **Page background token** — checked, not changed. The instruction was to shift
   `--color-background` to `#F7F8FB` *if it is currently pure white*; it's `#F8FAFC`, not
   `#FFFFFF`, so that condition doesn't hold and the token is untouched. Surfaces
   (`--color-surface`) stay pure white as before.

### Explicitly not recolored

Semantic badges (positive/warning/negative/info), buttons (primary stays `--color-accent` blue),
`DataTable`/`.data-table-wrap` (tables — including the outer wrap that gives them their card-like
border, deliberately left alone since it's part of "the table," not a standalone card),
money figures, charts, and score bars. Density, spacing, and typography are unchanged.
