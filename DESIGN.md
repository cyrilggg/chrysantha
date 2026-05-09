---
name: Chrysantha
description: Unified asset dashboard — Ghostfolio fork for cross-platform wealth oversight with A-share integration
colors:
  surface-light: "#fafafa"
  surface-dark: "#191919"
  card-bg: "#ffffff"
  card-bg-dark: "#424242"
  text-primary: "rgba(0,0,0,0.87)"
  text-primary-dark: "rgba(255,255,255,1)"
  text-secondary: "rgba(0,0,0,0.54)"
  text-secondary-dark: "rgba(255,255,255,0.7)"
  divider: "rgba(0,0,0,0.12)"
  divider-dark: "rgba(255,255,255,0.12)"
  brand-teal: "#008583"
  brand-teal-light: "#6bf7f4"
  brand-blue: "#00497c"
  brand-blue-light: "#d1e4ff"
  chrysantha-buffer: "#2e7d32"
  chrysantha-managed: "#e65100"
  chrysantha-investment: "#1565c0"
  chrysantha-personal: "#546e7a"
typography:
  body:
    fontFamily: "'Inter', Roboto, 'Helvetica Neue', sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  display:
    fontFamily: "'Inter', Roboto, 'Helvetica Neue', sans-serif"
    fontSize: "1.75rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  mono:
    fontFamily: "'SF Mono', 'Cascadia Code', 'Fira Code', monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
rounded:
  sm: "4px"
  md: "8px"
  lg: "16px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
  "2xl": "48px"
components:
  card-outlined:
    backgroundColor: "{colors.card-bg}"
    rounded: "{rounded.sm}"
    padding: "{spacing.lg}"
  value-large:
    typography: "{typography.display}"
    textColor: "{colors.text-primary}"
  buffer-card:
    backgroundColor: "{colors.card-bg}"
    rounded: "{rounded.sm}"
    padding: "{spacing.lg}"
  managed-card:
    backgroundColor: "{colors.card-bg}"
    rounded: "{rounded.sm}"
    padding: "{spacing.lg}"
---

## Theme

**Dual-mode with warm-light default.** Light mode uses a warm off-white surface (`#fafafa`) with teal-primary accents in the 500-700 range, never deeper than `#00504e`. Dark mode uses a warm charcoal (`#191919`) with the same teal family shifted lighter for contrast on dark. No pure black or pure white anywhere — every neutral is tinted infinitesimally toward the brand teal hue.

The physical scene: a single person checking net worth on a desktop monitor at 9pm in a softly-lit apartment. The screen should feel like a warm reading surface — contrast that doesn't fatigue, a palette that doesn't shout.

Dark mode is user-selectable via Ghostfolio settings but is not the default. Both modes use the same semantic color channels (buffer green, managed orange, investment blue) only brighter in dark mode to maintain contrast against dark surfaces.

## Color

### Semantic color channels

Colors are data channels, not decoration. Each accent has one job and appears only in that context:

| Token | Hex | Role |
|---|---|---|
| `chrysantha-buffer` | `#2e7d32` | Liquidity reserve accounts (`Cash_*`, `#Reserve`). Appears as card accent and tag background. |
| `chrysantha-managed` | `#e65100` | Entrusted family assets (`Mom_*`, `#ForMom`). Appears as card accent, row indicator, and tag. |
| `chrysantha-investment` | `#1565c0` | Active investment accounts (`#Investment`). Appears in portfolio charts and holding indicators. |
| `chrysantha-personal` | `#546e7a` | Default personal accounts. Neutral, recessive. |

These four channels are the only intentional color in the Chrysantha layer. Ghostfolio's existing teal brand (`#008583`) continues for navigation, buttons, and system chrome — Chrysantha adds semantic color atop it, never replacing it.

### Dark mode overrides

All Chrysantha semantic colors shift lighter in dark mode:
- Buffer: `#66bb6a` on `#1b5e20` background
- Managed: `#ff9800` on `#e65100` background
- Investment: `#42a5f5` on `#0d47a1` background
- Personal: `#90a4ae` on `#37474f` background

### Color strategy: Restrained

One accent ≤10% of surface area. The dashboard is data-forward — color appears only as semantic markers on specific accounts, never as decorative washes. Ghostfolio's own teal brand color provides the sole non-semantic accent for interactive elements.

### Never

- Pure `#000` or `#fff` as background
- Color without a semantic anchor
- Red for anything except Ghostfolio's warn palette in error states
- More than one semantic color on a single card or row

## Typography

**Inter** is the sole typeface, with system fallbacks to Roboto and Helvetica Neue. No serif, no display face, no secondary type family.

Scale uses weight contrast rather than size contrast. Numbers (values, balances, performance percentages) use `font-weight: 600` while labels use `400`. The largest type on any dashboard view is the buffer/managed card totals at `1.5rem` with `font-weight: 600`.

For tabular data (holdings, activities), use tabular-nums variant where available. Monospace is reserved for symbol codes (SH600519) and API responses only.

Line length for prose (empty states, onboarding copy) is capped at 65ch. Numbers and data displays have no line-length cap.

## Layout

### Dashboard grid

The Overview page uses a single-column centered layout (`max-width: 50rem`) for the performance chart, followed by a two-column card row (buffer left, managed right) below it. On mobile, the two-column row collapses to a single stack.

Spacing rhythm: `16px` between sections, `24px` between major content blocks, `8px` within card internals. Cards never nest inside other cards.

### Card guidelines

Use MatCard with `appearance="outlined"` only. No elevation, no filled cards, no glass. The 1px divider-stroke border is sufficient boundary. Card headers use `mat-card-title-group` with icon + title inline, subtitle below. Card body padding is `16px`.

Cards are appropriate for the buffer and managed summary widgets because they are distinct data modules. Do not use cards for simple value displays that could be a row or a `<dl>`.

### Responsive behavior

Bootstrap 4 grid (`row`/`col`) with Material breakpoints. Primary breakpoints:
- `col-12` (mobile, <576px): full-width stack
- `col-md-6` (tablet+, ≥768px): side-by-side cards
- `col-md-8 offset-md-2` (desktop, ≥768px): centered single-column content

## Components

### gf-home-buffer
Liquidity reserve summary card. Icon: `shield-checkmark-outline`. Accent: `chrysantha-buffer` green. Shows total + per-account breakdown. Empty state shows instructional copy in `text-muted`.

### gf-home-managed
Entrusted asset summary card. Icon: `people-outline`. Accent: `chrysantha-managed` orange. Shows total + per-account breakdown with colored dot indicators. Empty state shows instructional copy.

### gf-portfolio-performance
Ghostfolio native. Performance metrics grid (today/WTD/MTD/YTD/1Y/5Y/Max) with ROAI percentages. Currency values displayed via `gf-value`.

### gf-value
Currency/number display utility. Formats with `Intl.NumberFormat`, respects locale and precision, appends currency unit when `isCurrency` is true. Used everywhere a monetary value appears.

### gf-line-chart
Ghostfolio native. SVG-based performance curve. Percentage unit, gradient fill option, animated transitions. Responsive aspect ratio (`16:9` container).

## Icons

Ionicons exclusively, outline variant only. No filled, no sharp, no Material Icons. Icons are 1.25rem within card titles, 1rem within body text, and `large` on mobile tab bar. Never use an icon purely decoratively — every icon in Chrysantha carries meaning (shield = safety, people = entrusted, chart = performance).

## Motion

No custom animations in the Chrysantha layer. Ghostfolio's existing chart animations (line-chart transitions on date-range change) remain. Card appearance is instant — no fade-in, no slide-up, no stagger. This is a data tool, not a narrative experience.
