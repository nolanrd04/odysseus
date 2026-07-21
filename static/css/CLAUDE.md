# static/css/ — Modularized CSS Files

## Status: EXTRACTED BUT NOT YET LOADED

These files were extracted from `/static/style.css` as a modularization effort. However, **`/static/index.html` still only loads `/static/style.css`** (37k lines, no `@import`). These individual files are not yet referenced by any HTML or JS — they exist as reference/future use.

**Exceptions**:
- `quick_proposal.css` IS actively loaded — injected dynamically by `static/js/quick_proposal/ui.js:injectStyles()`.
- `memory.css` IS actively loaded — `<link>` in `index.html` after `style.css`. Holds Memories-modal additions/overrides (Global Memories tab, scrollable memory bubbles).
- `tasks_run_status.css` IS actively loaded — `<link>` in `index.html`. Task-card live run status chip + Run-button running state (distinct from the unwired `tasks.css` extract).

## To complete the modularization
- Either add `@import "./css/file.css"` entries to `style.css`, OR
- Replace the single `<link rel="stylesheet" href="/static/style.css">` in `index.html` with individual `<link>` tags per file.

## Key Files
| File | Contains |
|------|---------|
| `variables.css` | CSS custom properties / design tokens |
| `layout.css` | Page layout, sidebar, panel sizing |
| `components_from_style_css.css` | Chat bubbles, thinking blocks (`.thinking-section`), sources boxes, code blocks |
| `processing_pulse_animation_reused_by_session_star.css` | Agent thread nodes (`.agent-thread`, `.agent-thread-node`), tool output, wave animations |
| `quick_proposal.css` | Quick Proposal feature — **actively loaded** |
| `notes.css` | Notes feature styles |
| `tasks.css` | Task management styles |
| `calendar.css` | Calendar view styles |
| `cookbook.css` | Cookbook/recipe feature |
| `gallery_image_library.css` | Image gallery |
| `research_synapse_visualization.css` | Research Synapse graph view |

## Reusable Component CSS (in style.css / components_from_style_css.css)
- `.thinking-section` / `.thinking-header` / `.thinking-content` — collapsible thinking blocks
- `.agent-thread` / `.agent-thread-node` — tool call thread UI
- `.agent-tool-output` — expandable `<details>` for tool input/output
- `.sources-section` — collapsible sources box
