# static/js/quick_proposal/ — Quick Proposal JS Modules

## Overview
Three-file JS module for the Quick Proposal feature. Injected into the main Odysseus app as a modal/panel — shares `document`, all delegated handlers, and CSS classes from `style.css`.

## Files

### `ui.js` — Main UI (1500+ lines)
Entry point. Exports `openQuickProposal()` called from elsewhere in the app.

**Key functions:**
| Function | Purpose |
|----------|---------|
| `openQuickProposal()` | Creates and mounts the QP overlay |
| `injectStyles()` | Injects inline `STYLES` const + loads `/static/css/quick_proposal.css` as a link |
| `handleRun()` | Starts a new extraction run — uploads file, calls `/api/quick_proposal/run` |
| `buildViewPanel()` | Renders the view for an existing run (thumbnails, index panel, etc.) |
| `initExtractionPanel()` | Creates the Phase 3 extraction log + index panel DOM |
| `appendExtractionMessage()` | Renders one extraction log entry — see Component Rendering below |
| `updateLiveIndex()` | Updates the live index values panel |
| `addRegionPreview()` | Adds region image tiles to the sidebar |
| `showPreview()` | Shows a page preview, saves current content for Back button restore |

**Helpers (module-private):**
| Function | Purpose |
|----------|---------|
| `_esc(str)` | HTML-escape for innerHTML use |
| `_extractThinking(text)` | Splits `<think>...</think>` from text |
| `_buildThinkingSection(thinking)` | Returns `.thinking-section` HTML (uses existing chat UI CSS) |
| `loadModels()` | Populates a `<select>` with available models |

### `stream.js` — SSE Stream Handler
Wraps `EventSource` for the Phase 3 extraction stream. Accepts callbacks: `onPageClassified`, `onExtractionMessage`, `onIndexUpdate`, `onRegionPreview`, `onError`, `onDone`.

**SSE event types emitted by backend:**
| Event | Data fields |
|-------|------------|
| `extraction_message` | `role`, plus role-specific fields below |
| `index_update` | `key`, `value`, `confidence` |
| `region_preview` | `bbox_id`, `image_url` |
| `page_classified` | `page_idx`, `sheet_type`, `importance`, `regions` |
| `phase_start` / `phase_complete` | `phase`, `label` |
| `error` | `message`, `phase` |

**`extraction_message` roles:**
| `role` | Extra fields | Rendered as |
|--------|-------------|-------------|
| `claude` | `text`, `model` | Manager chat bubble (with thinking section if `<think>` present) |
| `claude_to_gemini` | `text` | Collapsible `→ Gemini instruction` block |
| `tool_call` | `tool_id`, `tool`, `args` | `.agent-thread-node running` with expandable Input |
| `tool_result` | `tool_id`, `tool`, `result` | Updates node to done with expandable Output |
| `gemini` | `text` | Gemini chat bubble |

### `index.js` — Module Entry
Registers the quick_proposal section in the sidebar and wires the open button.

## Component Rendering (appendExtractionMessage)
Reuses existing chat UI CSS classes — no extra wiring needed:
- **Thinking**: `.thinking-section` + `data-thinking-id` → toggled by `markdown.js:854` delegated handler
- **Tool nodes**: `.agent-thread` + `.agent-thread-node` → toggled by `chat.js:5006` delegated handler
- **Tool output**: `<details class="agent-tool-output">` — native HTML, no JS needed
- **Tool node registry**: `container._toolNodes` Map (keyed by `tool_id`) links `tool_call` nodes to `tool_result` updates

## CSS
- `STYLES` const in `ui.js` — QP structural styles (layout, panels, thumbnails, buttons)
- `/static/css/quick_proposal.css` — QP extraction log styles (bubbles, avatars, tool thread tweaks)
- All `agent-thread*` and `thinking-*` classes come from `/static/style.css` (already loaded)
