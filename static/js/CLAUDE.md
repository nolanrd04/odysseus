# static/js/ — JavaScript Modules

## Overview
All client-side JavaScript for the Odysseus UI. Entry point is `/static/index.html`, which loads `/static/style.css` and `/static/app.js` plus individual module scripts.

## Subdirectories
| Dir | Purpose |
|-----|---------|
| `calendar/` | Calendar view components |
| `color/` | Color picker utilities |
| `compare/` | Model A/B comparison mode |
| `editor/` | Document editor components |
| `emailLibrary/` | Email library/inbox sub-modules |
| `markdown/` | Markdown renderer sub-modules |
| `quick_proposal/` | Quick Proposal feature — see its own CLAUDE.md |
| `research/` | Deep Research panel |
| `util/` | Shared utility helpers |

## Key Modules
| File | Role |
|------|------|
| `chat.js` | Main chat — streaming, tool call rendering, message CRUD |
| `chatRenderer.js` | `addMessage()`, history re-render, sources/findings boxes |
| `markdown.js` | `processWithThinking()`, `createThinkingSection()`, `createCollapsible()`, thinking toggle handler |
| `streamingRenderer.js` | Live token-stream update helper |
| `app.js` | App init, event listeners, keyboard shortcuts, module wiring |
| `ui.js` | Toast/error, `el()`, scroll, ESC handler (closes thinking blocks) |
| `sessions.js` | Session create/load/switch |
| `theme.js` | Theme application including `--accent` CSS var |
| `models.js` | Model scanning, provider management |
| `settings.js` | Settings panel |

## Chat UI Reusable Components

### Thinking Blocks (`markdown.js`)
- **CSS classes**: `.thinking-section`, `.thinking-header[data-thinking-id]`, `.thinking-content`, `.thinking-toggle`
- **Toggle**: Delegated `document` click handler at `markdown.js:854` — no wiring needed, works everywhere in the document
- **Helper**: `createThinkingSection(text, index, time)` — returns HTML string
- **Generic**: `createCollapsible(contentMarkdown, label)` — same styling, different label

### Tool Call Nodes (`chat.js`)
- **CSS classes**: `.agent-thread > .agent-thread-node(.running|.error|.open) > .agent-thread-header + .agent-thread-content`
- **Output**: `<details class="agent-tool-output"><summary>Output</summary><pre>...</pre></details>`
- **Toggle**: Delegated `document.body` click handler at `chat.js:5006` — guarded by `window.__odysseus_thread_click_bound`
- **CSS source**: `style.css` (also extracted to `static/css/processing_pulse_animation_reused_by_session_star.css`)

## Modularization Note
`static/css/` contains individual CSS files extracted from `style.css`, but `index.html` still loads only `/static/style.css` (37k lines). The modularized files are **not yet wired up** — `index.html` needs to be updated to load them individually (or `style.css` needs `@import` statements). The only exception is `quick_proposal.css`, which is dynamically injected by `quick_proposal/ui.js:injectStyles()`.
