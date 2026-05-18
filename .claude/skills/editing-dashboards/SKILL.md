---
name: editing-dashboards
description: Modifies an existing dashboard in place by editing its single HTML file with precise anchored replacements. Use this skill when the user asks to change, edit, restyle, recolor, add, remove, rework, or tweak any part of a dashboard that already exists in the current session. Trigger words include "make X rounded", "change the color", "add a section", "remove the timeline", "сделай карточки круглее", "добавь раздел", "убери таблицу". Activate ONLY if an "Active dashboard" UUID is present in the system prompt context.
---

# Editing Dashboards

## When to activate

- The system prompt contains an "Active dashboard" line with a UUID.
- The user's message expresses modification intent ("make X", "change Y",
  "add Z", "remove W", "round", "recolor", "restyle", "поменяй", "добавь",
  "убери", etc.).

If no active dashboard is set, do not activate. Tell the user there's no
dashboard to edit yet, and ask whether they want to create one (route to
`creating-dashboards`).

## Tools you will use

- `Read` — load `dashboards/<uuid>/index.html` and (when needed) refresh on
  `design-system.md`.
- `Edit` — precise old-string / new-string replacement against marker
  anchors. **All edits go through `Edit`.** Do not write a new file.

Do **not** call `mcp__dashboard__create_dashboard` — editing must not mint a
new UUID. The user's existing link continues to work.

## Workflow

1. **Read the file first.** `Read("dashboards/<uuid>/index.html")`. Never
   edit blind. Optionally re-read `design-system.md` to refresh on marker
   formats and palette tokens.
2. **Classify the edit by layer:**
   - **CSS layer** — color, spacing, typography, radius, shadows, layout
     tweaks. Edit inside the `<style>` block. Global changes (e.g. "round all
     cards") usually mean changing a CSS variable in `:root` like
     `--ds-radius`.
   - **HTML layer** — add or remove a section, rearrange content. New
     sections go just before `<!-- === END:sections === -->`. Add matching
     `/* === STYLE:<slug> === */` and `// === SCRIPT:<slug> ===` fences in
     the `<style>` and `<script>` blocks too.
   - **JS layer** — interactivity, chart data, toggles. Edit inside the
     corresponding `// === SCRIPT:<slug> ===` fence.
3. **Plan multi-layer edits up front.** If the user request touches more than
   one layer, plan all `Edit` calls before issuing the first one.
4. **Content-fit check (mandatory for any geometry change).** Before issuing
   an `Edit` that touches size, shape, aspect ratio, padding, margin,
   `overflow`, `height`, `width`, `aspect-ratio`, `text-overflow`, or
   `white-space`, walk through the content-fit invariants in section 9 of
   `design-system.md`. Specifically:

   - Will text still wrap freely? (no `overflow: hidden` on text containers,
     no `text-overflow: ellipsis` on multi-line content)
   - Is `height` fixed where `min-height` would be safer?
   - If the user asked for a shape (square, circle, fixed aspect), does the
     content fit at the existing font-size? If not, enlarge the container or
     apply the shape only to the decoration (border/background) and report
     the trade-off in your reply.
   - Default to *expanding the container to fit content* rather than clipping.

   The user is not expected to spell this out every time. Apply the checklist
   silently on every visual edit; only mention it in the reply when you have
   to make a trade-off the user might want to know about.

5. **Make anchored edits.** For each `Edit`:
   - Include a marker comment or a known unique token in `old_string` so the
     match is guaranteed unique within the file.
   - Never replace a marker comment itself.
   - Common patterns:
     - Round cards globally → edit `--ds-radius: 8px;` inside `:root` (the
       palette comment is unique).
     - Add a section → `Edit` inserts the full
       `<!-- === SECTION:<slug> === --> … <!-- === /SECTION:<slug> === -->`
       block immediately before `<!-- === END:sections === -->`, then a
       second `Edit` adds matching STYLE and SCRIPT fences at the end of
       their blocks.
     - Remove a section → `Edit` replaces the entire span from
       `<!-- === SECTION:<slug> === -->` through
       `<!-- === /SECTION:<slug> === -->` with an empty string, then likewise
       for matching STYLE and SCRIPT fences.
     - Change chart data → `Edit` against a unique substring inside the
       matching `// === SCRIPT:<slug> ===` fence.
6. **On `Edit` failure** (no unique match): re-read the file, find the
   canonical marker context, and try once more with the actual surrounding
   text. If still failing, stop and report — do not make destructive
   guesses.
7. **Confirm to the user**, in their language, with a markdown link:
   `Updated the dashboard — refresh [Open the dashboard](<url>) to see the changes.`
   If any content-fit trade-off was made (e.g. container enlarged, shape
   applied to decoration only), mention it briefly.

## Decline conditions

- The user asks for a structural change incompatible with the design system
  (e.g. "remove all the ds- classes" or "rewrite from scratch"). Explain
  that this would require regenerating the dashboard and offer to do so via
  `creating-dashboards`.
- The user asks to edit a different dashboard than the active one. Ask them
  to clarify which dashboard, or to open it as a new active session.

## Notes

- Source-language preservation applies to edits too: new section titles, new
  card content, etc. stay in the dashboard's existing language.
- All edits keep marker comments intact — they are how future edits stay
  precise.
- The dashboard URL is stable across edits. Do not mint a new one.
