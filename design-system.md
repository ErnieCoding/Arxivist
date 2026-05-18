# Arxivist Dashboard Design System

This file is the single source of truth for the look, structure, and
edit-anchors of any dashboard produced by the agent. Read this file (via the
`Read` tool) every time you create or edit a dashboard. The agent never
executes anything here — copy what you need into the dashboard HTML.

The dashboard is **one self-contained HTML file** per dashboard. No external
CSS/JS files. Chart.js is the only external resource, loaded by CDN.

---

## 1. Palette — copy verbatim into the dashboard's `<style>`

```css
:root {
  --ds-bg: #0f1117;
  --ds-surface: #1a1d27;
  --ds-surface-2: #22253a;
  --ds-text: #e6e8ef;
  --ds-text-dim: #9aa0b4;
  --ds-accent: #5b6ef5;
  --ds-critical: #ef4444;
  --ds-high: #f59e0b;
  --ds-low: #10b981;
  --ds-border: #2a2e3f;
  --ds-radius: 8px;
}
```

Always use the variables. Never hard-code colors. The user edits the look by
changing variable values, so every color reference in the dashboard must read
`var(--ds-*)`.

---

## 2. Naming convention

Classes (all begin with `ds-`):
- `.ds-section` — top-level section wrapper.
- `.ds-section-title` — H2 inside a section.
- `.ds-card` — a card.
- `.ds-card-grid` — flex/grid container of cards.
- `.ds-card-title` — H3 inside a card.
- `.ds-timeline` — vertical timeline container.
- `.ds-timeline-item` — single event.
- `.ds-pill` — small inline label (use modifiers `.ds-pill--critical`, `.ds-pill--high`, `.ds-pill--low`).
- `.ds-stat` — a single metric (label + value).
- `.ds-stat-value` — large numeric value.
- `.ds-stat-label` — small caption.
- `.ds-kbd` — keyboard / code-style chip.
- `.ds-chart` — a chart's `<canvas>` wrapper (sets sizing).

IDs:
- `#section-<slug>` — one per section, slug is kebab-case (e.g. `section-overview`, `section-key-findings`).
- `#card-<slug>` — optional per-card ID for direct addressing.
- `#chart-<slug>` — `<canvas>` element for a chart.

If you must invent a new class, prefix it with `ds-` and add a code comment
explaining it inline.

---

## 3. Anchor markers — DO NOT VARY

These exact strings are how the editing flow finds regions safely. Always
emit them when creating a section; never delete a marker when editing.

HTML (inside `<body>`):
```
<!-- === SECTION:<slug> === -->
<section id="section-<slug>" class="ds-section"> ... </section>
<!-- === /SECTION:<slug> === -->
```

CSS (inside the `<style>` block, one fence per section):
```
/* === STYLE:<slug> === */
#section-<slug> .ds-card { ... }
/* === /STYLE:<slug> === */
```

JS (inside the `<script>` block at end of body, one fence per section that needs interactivity):
```
// === SCRIPT:<slug> ===
(function() {
  // section-scoped code
})();
// === /SCRIPT:<slug> ===
```

End-of-sections insertion point — must appear once, just before `</main>`:
```
<!-- === END:sections === -->
```

When adding a new section: insert SECTION block immediately before
`<!-- === END:sections === -->`, then add matching STYLE and SCRIPT fences in
their respective blocks.

When removing a section: remove the SECTION block, its STYLE fence, and its
SCRIPT fence.

---

## 4. HTML skeleton — start from this

```html
<!DOCTYPE html>
<html lang="<source-lang-tag>">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><dashboard title in source language></title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    /* ===== PALETTE — do not remove ===== */
    :root {
      --ds-bg: #0f1117;
      --ds-surface: #1a1d27;
      --ds-surface-2: #22253a;
      --ds-text: #e6e8ef;
      --ds-text-dim: #9aa0b4;
      --ds-accent: #5b6ef5;
      --ds-critical: #ef4444;
      --ds-high: #f59e0b;
      --ds-low: #10b981;
      --ds-border: #2a2e3f;
      --ds-radius: 8px;
    }

    /* ===== BASE ===== */
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: var(--ds-bg);
      color: var(--ds-text);
      line-height: 1.6;
      padding: 2rem 1.5rem 4rem;
    }
    main { max-width: 1100px; margin: 0 auto; }
    h1 { font-size: 1.8rem; margin-bottom: 0.25rem; }
    .ds-subtitle { color: var(--ds-text-dim); margin-bottom: 2rem; font-size: 0.95rem; }

    .ds-section {
      background: var(--ds-surface);
      border: 1px solid var(--ds-border);
      border-radius: var(--ds-radius);
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.25rem;
    }
    .ds-section-title { font-size: 1.15rem; margin-bottom: 0.85rem; color: var(--ds-text); }

    .ds-card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 0.85rem; }
    .ds-card {
      background: var(--ds-surface-2);
      border: 1px solid var(--ds-border);
      border-radius: var(--ds-radius);
      padding: 0.95rem 1.05rem;
    }
    .ds-card-title { font-size: 0.98rem; margin-bottom: 0.4rem; color: var(--ds-text); }
    .ds-card p { color: var(--ds-text-dim); font-size: 0.9rem; }

    .ds-pill {
      display: inline-block;
      font-size: 0.72rem;
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      background: var(--ds-surface-2);
      color: var(--ds-text-dim);
      border: 1px solid var(--ds-border);
    }
    .ds-pill--critical { background: rgba(239, 68, 68, 0.12); color: var(--ds-critical); border-color: rgba(239, 68, 68, 0.4); }
    .ds-pill--high     { background: rgba(245, 158, 11, 0.12); color: var(--ds-high);     border-color: rgba(245, 158, 11, 0.4); }
    .ds-pill--low      { background: rgba(16, 185, 129, 0.12); color: var(--ds-low);      border-color: rgba(16, 185, 129, 0.4); }

    .ds-stat { display: flex; flex-direction: column; gap: 0.2rem; }
    .ds-stat-value { font-size: 1.6rem; font-weight: 600; color: var(--ds-text); }
    .ds-stat-label { font-size: 0.78rem; color: var(--ds-text-dim); text-transform: uppercase; letter-spacing: 0.06em; }

    .ds-timeline { display: flex; flex-direction: column; gap: 0.6rem; }
    .ds-timeline-item {
      display: flex;
      gap: 0.75rem;
      padding: 0.6rem 0.85rem;
      background: var(--ds-surface-2);
      border-left: 3px solid var(--ds-accent);
      border-radius: var(--ds-radius);
    }

    .ds-chart { position: relative; height: 280px; width: 100%; }

    .ds-kbd {
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.82rem;
      background: var(--ds-bg);
      color: var(--ds-text-dim);
      padding: 0.1em 0.4em;
      border: 1px solid var(--ds-border);
      border-radius: 4px;
    }

    /* ===== per-section style fences appear below ===== */
  </style>
</head>
<body>
  <main>
    <h1><dashboard title></h1>
    <p class="ds-subtitle"><short subtitle / source attribution></p>

    <!-- ===== SECTIONS GO HERE ===== -->
    <!-- Add each section using the SECTION marker comments. -->

    <!-- === END:sections === -->
  </main>

  <script>
    // ===== per-section script fences appear below =====
  </script>
</body>
</html>
```

---

## 5. Starter section examples

Each example shows the canonical marker comments plus a minimal content
shape. Adapt content to the source document. Keep total section count ≤ 8.

### Overview

```html
<!-- === SECTION:overview === -->
<section id="section-overview" class="ds-section">
  <h2 class="ds-section-title">Overview</h2>
  <p>1–3 sentence summary of the source document, in the source language.</p>
</section>
<!-- === /SECTION:overview === -->
```

Matching CSS fence (often empty, but keep the markers):
```css
/* === STYLE:overview === */
/* === /STYLE:overview === */
```

### Key findings (card grid)

```html
<!-- === SECTION:key-findings === -->
<section id="section-key-findings" class="ds-section">
  <h2 class="ds-section-title">Key findings</h2>
  <div class="ds-card-grid">
    <article class="ds-card" id="card-finding-1">
      <h3 class="ds-card-title">…</h3>
      <p>…</p>
    </article>
    <!-- repeat -->
  </div>
</section>
<!-- === /SECTION:key-findings === -->
```

### Metrics (stat row)

```html
<!-- === SECTION:metrics === -->
<section id="section-metrics" class="ds-section">
  <h2 class="ds-section-title">Metrics</h2>
  <div class="ds-card-grid">
    <div class="ds-card ds-stat">
      <span class="ds-stat-value">42</span>
      <span class="ds-stat-label">Units shipped</span>
    </div>
    <!-- repeat -->
  </div>
</section>
<!-- === /SECTION:metrics === -->
```

### Timeline

```html
<!-- === SECTION:timeline === -->
<section id="section-timeline" class="ds-section">
  <h2 class="ds-section-title">Timeline</h2>
  <ol class="ds-timeline">
    <li class="ds-timeline-item">
      <span class="ds-pill ds-pill--high">2026-05-08</span>
      <div>Description of event.</div>
    </li>
    <!-- repeat -->
  </ol>
</section>
<!-- === /SECTION:timeline === -->
```

### Risks (priority-coded)

```html
<!-- === SECTION:risks === -->
<section id="section-risks" class="ds-section">
  <h2 class="ds-section-title">Risks</h2>
  <div class="ds-card-grid">
    <article class="ds-card" id="card-risk-1">
      <h3 class="ds-card-title">Title <span class="ds-pill ds-pill--critical">critical</span></h3>
      <p>…</p>
    </article>
    <!-- repeat -->
  </div>
</section>
<!-- === /SECTION:risks === -->
```

### References / sources

```html
<!-- === SECTION:references === -->
<section id="section-references" class="ds-section">
  <h2 class="ds-section-title">References</h2>
  <ul>
    <li><a href="…" target="_blank">…</a></li>
  </ul>
</section>
<!-- === /SECTION:references === -->
```

### Chart (Chart.js)

```html
<!-- === SECTION:chart-progress === -->
<section id="section-chart-progress" class="ds-section">
  <h2 class="ds-section-title">Progress over time</h2>
  <div class="ds-chart"><canvas id="chart-progress"></canvas></div>
</section>
<!-- === /SECTION:chart-progress === -->
```

Matching JS fence:
```js
// === SCRIPT:chart-progress ===
(function() {
  const ctx = document.getElementById('chart-progress');
  if (!ctx) return;
  new Chart(ctx, {
    type: 'bar',
    data: { labels: ['Q1','Q2','Q3','Q4'], datasets: [{ label: 'Шт.', data: [3,5,8,12], backgroundColor: 'rgba(91,110,245,0.5)', borderColor: 'rgba(91,110,245,1)', borderWidth: 1 }] },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, ticks: { color: '#9aa0b4' } }, x: { ticks: { color: '#9aa0b4' } } }, plugins: { legend: { labels: { color: '#e6e8ef' } } } }
  });
})();
// === /SCRIPT:chart-progress ===
```

---

## 6. JSON intermediate schema

This is the planning artifact you assemble in your reasoning *before* writing
HTML. Do not write it to disk and do not pass it to any tool — it exists
purely so you compose a clean outline first.

```ts
type DashboardPlan = {
  title: string;          // in source language
  subtitle: string;       // short subtitle / source description
  sections: Section[];    // ordered, 1..8 entries
};

type Section = {
  slug: string;                 // kebab-case, unique within this plan
  type:
    | "overview"
    | "key-findings"
    | "metrics"
    | "timeline"
    | "risks"
    | "references"
    | string;                   // custom — must still follow naming
  title: string;                // in source language
  content: unknown;             // type-specific (cards array, stats array, timeline events, etc.)
};
```

---

## 7. Language rules

- Detect the source document's language. Produce all human-visible text
  (title, subtitle, section titles, card titles, body, chart labels, axis
  labels, legends) in that language.
- Class names, IDs, marker comments, and CSS variable names stay English as
  documented above.
- `<html lang="…">` should reflect the source language (e.g. `ru`, `en`).

---

## 8. Chart guidance

- Chart.js is the only chart library. Loaded via
  `<script src="https://cdn.jsdelivr.net/npm/chart.js">` in `<head>` (already
  present in the skeleton).
- Each chart goes inside its own section with a `<canvas id="chart-<slug>">`.
- Initialize charts inside a `// === SCRIPT:<slug> ===` IIFE so each chart's
  setup code is editable independently.
- Use palette colors via direct hex (Chart.js doesn't read CSS variables);
  pick from the palette listed in section 1.

---

## 9. Content-fit invariants — apply on every create AND every edit

The dashboard must always remain readable. Visual changes (size, shape,
spacing) must never clip, hide, or scroll away content that was previously
visible. When a request changes geometry (size, aspect ratio, padding, etc.),
treat it as a *joint* change to geometry **and** the content sizing inside
that geometry so nothing is cut off.

Hard rules:

- **No silent clipping.** Do not use `overflow: hidden` on a container that
  holds text, list items, headings, or stat values. If overflow is requested
  explicitly, prefer `overflow: visible` and grow the container instead.
- **No `text-overflow: ellipsis` on body content.** Allowed only on
  single-line metadata (filenames in lists, sidebar labels). Card titles,
  card body text, stat labels, and timeline entries must wrap freely.
- **No fixed `height` on content containers.** Use `min-height` so the box
  grows with its content. Fixed `height` is allowed only on chart canvases
  (`.ds-chart`) and decorative elements with no text.
- **Shape changes must scale with content.**
  - Squares / fixed aspect ratios: use `aspect-ratio` only if the content
    inside is a single short label or a number (e.g. `.ds-stat-value`). If
    the content is multi-line text, do **not** use `aspect-ratio` — grow the
    container vertically instead.
  - When asked to "make X square / round / circular / a specific shape," if
    that shape would clip the content, do one of: (a) keep the shape but
    enlarge the container until the text fits at the existing font-size; or
    (b) shape only the visual decoration (border, background) and let the
    content extend beyond the decoration. Never shrink a font below 0.78rem
    without saying so to the user.
- **Prefer flexible layouts.** Use `grid-template-columns: repeat(auto-fit,
  minmax(<floor>, 1fr))` for card grids so cards reflow to fit content. Use
  `flex-wrap: wrap` on rows. Never use a fixed pixel column count on grids
  that hold variable-length text.
- **Backgrounds and borders are decoration, not boundaries.** Shaping a card
  via `border-radius` or `clip-path` is a visual change. It does not give
  permission to lose content — make the container big enough.

If a user request is ambiguous (e.g. "make the cards squares"), default to
*expanding the container to fit content while preserving the new shape*. Do
not silently clip. If the geometry the user asked for is fundamentally
incompatible with the content (e.g. a fixed 80×80 px square holding a 30-word
paragraph), apply the geometry as decoration only and report in the reply
what trade-off you made so the user can decide.

## 10. Final rules of thumb

- Output a single complete document starting `<!DOCTYPE html>` — never split.
- ≤ 8 sections per dashboard, ≤ 6 cards per card grid.
- Prefer bullets and short labels over prose paragraphs.
- Preserve source language for content; keep code/markers English.
- Always include the palette block and the `<!-- === END:sections === -->` marker.
- Apply the content-fit invariants in section 9 on every create and every edit.
