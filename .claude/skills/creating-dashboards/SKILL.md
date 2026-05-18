---
name: creating-dashboards
description: Generates a single self-contained HTML dashboard from an uploaded document, pasted text, or downloaded arXiv paper, and publishes it at a sharable URL. Use this skill when the user asks to make, build, generate, create, render, or produce a dashboard, summary page, infographic, visual overview, briefing page, or report page from any source material. Trigger words across languages include "dashboard", "summary page", "overview page", "сделай дашборд", "построй страницу", "визуальный обзор", "инфографика". Do NOT use this skill for plain text summaries (use summarizing-papers) or for arXiv search alone (use searching-arxiv).
---

# Creating Dashboards

## Tools you will use
- `Read` — to load `design-system.md`, uploaded files, and (for PDFs) the original document.
- `mcp__dashboard__extract_text` — to extract text from non-PDF files outside `uploads/` (e.g. `downloads/*.pdf` is the **wrong** target for this tool; for `downloads/*.pdf` use `Read` directly to leverage Claude's native PDF parsing).
- `mcp__dashboard__create_dashboard` — to publish the final HTML.

Always start by reading `design-system.md` at the project root.

## When to activate vs decline

Activate when the user asks for a "dashboard / summary page / overview page /
visual summary / инфографика / дашборд / страница-обзор" — anything that
implies a styled, multi-section, published HTML page.

Decline (and route elsewhere) when:
- The user wants a plain summary or TL;DR → use `summarizing-papers`.
- The user only wants to find papers, no page requested → use `searching-arxiv`.
- The user wants to **edit** an existing dashboard → use `editing-dashboards`
  (look for an "Active dashboard" UUID in your system context).

Chained workflow: if the user says "find papers on X **and make a dashboard**",
first run `searching-arxiv` to populate `downloads/`, then run this skill
sourcing from those PDFs.

## Source resolution

The system prompt will list attached files with their `file_id`, name, and
`parse_mode`. Pick the right reading strategy per file:

| parse_mode | What to do |
|---|---|
| `pdf-native` | `Read("uploads/<file_id>/original.pdf")`. For PDFs >20 pages, pass `pages: "1-20"` and call again for later ranges as needed. Claude sees images, tables, and layout natively. |
| `extracted` (DOCX) | `Read("uploads/<file_id>/text.md")`. Tables are already markdown-formatted. |
| `plain` (MD/TXT) | `Read("uploads/<file_id>/text.md")` (we save MD and TXT alike here). |

Other sources:
- **Pasted text** — already in the user's message; use it directly.
- **arXiv-chained PDFs** — `Read("downloads/<filename>.pdf")` (full vision fidelity).
- **arXiv abstracts in session state** — already in your prompt context after
  `searching-arxiv` runs.

If multiple sources exist, combine them.

## Step-by-step workflow

1. **Load the design system.** `Read("design-system.md")`. Re-read on every
   run — palette, naming, marker formats, and the HTML skeleton live here.
2. **Gather source content** per the table above.
3. **Detect source language.** Note it; you will keep all human-visible
   strings in this language.
4. **Plan the dashboard as a JSON intermediate** in your own reasoning
   (not on disk, not passed to any tool). Follow the schema in section 6 of
   `design-system.md`. Pick a sensible subset of section types — typically
   overview + key-findings + 1–2 type-specific sections. Cap at 8 sections.
5. **Render the HTML.** Start from the skeleton in section 4 of
   `design-system.md`. Copy the palette `:root` block verbatim. For each
   planned section, emit:
   - `<!-- === SECTION:<slug> === -->` … `<!-- === /SECTION:<slug> === -->`
     inside `<main>`, before `<!-- === END:sections === -->`.
   - `/* === STYLE:<slug> === */` … `/* === /STYLE:<slug> === */` inside the
     `<style>` block (empty if no per-section styles needed).
   - `// === SCRIPT:<slug> ===` … `// === /SCRIPT:<slug> ===` inside the
     `<script>` block (empty if no per-section JS needed).
   Use only documented `ds-` classes and `#section-<slug>` IDs.
6. **Publish.** Call `mcp__dashboard__create_dashboard(html=<full document>, session_id=<from system prompt>)`. The tool returns `{uuid, url, title}`.
7. **Reply** with one or two sentences in the user's language plus a markdown
   link: `[Open the dashboard](<url>)`.

## Language preservation

Detect the source language and write the dashboard's title, subtitle, section
titles, card titles, body text, chart labels, axis labels, and legends in
that language. Class names, IDs, marker comments, and CSS variable names
stay in English. Set `<html lang="…">` to the detected language tag.

## Error handling

- If `create_dashboard` rejects the HTML (structural sanity check), read its
  error message, fix the missing element (DOCTYPE / `<style>` / SECTION
  marker), and try once more.
- For any other tool error, surface it verbatim to the user — don't retry
  blindly.

## Notes

- Output the complete HTML in one response — do not split across messages.
- Keep dashboards compact: ≤ 8 sections, ≤ 6 cards per grid, bullets over
  prose.
- For charts, Chart.js is loaded by the skeleton's `<head>`. Initialize each
  chart inside a `// === SCRIPT:<slug> ===` IIFE.
- This skill does not gate by source size — the system assumes any source
  the user has provided is sufficient.
