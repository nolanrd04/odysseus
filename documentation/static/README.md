# Frontend (`static/`)

> Parent: [`../README.md`](../README.md) | Architecture: [`../architecture.md`](../architecture.md)

The frontend is a single-page application (SPA) served from `static/`. It communicates with the backend via REST API calls and Server-Sent Events (SSE) for streaming.

---

## Key Files

| File | Purpose |
|------|---------|
| `static/index.html` | Main SPA entry point (~194KB) — all app HTML |
| `static/app.js` | Compiled/bundled JS (~176KB) — legacy bundle |
| `static/style.css` | Monolithic stylesheet (~1.1MB) — all themes and layouts |
| `static/login.html` | Login page (separate from SPA) |
| `static/landing.html` | Landing/marketing page |
| `static/sw.js` | Service worker for PWA offline support |
| `static/manifest.json` | PWA manifest (app name, icons, display mode) |

---

## JavaScript Module Map (`static/js/`)

### Chat & AI
| File | Purpose |
|------|---------|
| `chat.js` | Main chat component — message list, input, submit |
| `chatRenderer.js` | Renders messages: Markdown, code blocks, tool output |
| `chatStream.js` | SSE consumer — reads streaming tokens, updates UI |
| `assistant.js` | Agent mode UI — tool call display, iteration progress |

### Documents & Editor
| File | Purpose |
|------|---------|
| `document.js` | Document editor main component |
| `editor/` | Editor submodules (toolbar, extensions, formatting) |
| `fileHandler.js` | File drag-and-drop, upload integration |

### Email & Calendar
| File | Purpose |
|------|---------|
| `email.js`, `emailCompose.js`, `emailThread.js` | Email inbox, compose, thread view |
| `calendar.js` | Calendar main component |
| `calendar/` | Calendar submodules (month view, event modal, sync) |

### Research & Gallery
| File | Purpose |
|------|---------|
| `research.js`, `researchReport.js`, `researchStream.js` | Deep research UI and progress display |
| `gallery.js`, `galleryEditor.js` | Image generation and inpainting UI |

### Model Management
| File | Purpose |
|------|---------|
| `cookbook.js` | Cookbook main component |
| `cookbookDownload.js` | Model download progress |
| `cookbookServe.js` | Model server controls |
| `compare/` | Multi-model A/B comparison UI |

### Notes & Tasks
| File | Purpose |
|------|---------|
| `note_routes.js` | Notes UI |
| `task.js`, `taskScheduler.js` | Scheduled tasks UI |

### Memory & Skills
| File | Purpose |
|------|---------|
| `memory.js` | Vector memory management UI |

### Infrastructure & Utilities
| File | Purpose |
|------|---------|
| `init.js` | App initialization, feature detection |
| `admin.js` | Admin panel UI |
| `modalManager.js` | Modal dialog system |
| `keyboard-shortcuts.js` | Keybinding system |
| `a11y.js` | Accessibility helpers |
| `dragSort.js` | Drag-and-drop sorting |
| `langIcons.js` | Language icons for syntax highlighting |
| `emoji.js`, `emojiPicker.js` | Emoji picker |
| `markdown/` | Markdown rendering utilities |
| `color/` | Color picker component |

### Third-party Libraries
| Path | Contents |
|------|---------|
| `static/lib/` | Vendored third-party JS (highlight.js, marked, etc.) |
| `static/fonts/` | Custom web fonts |

---

## Styling

`static/style.css` is a single monolithic file (~1.1MB) containing:
- Base styles and CSS custom properties (design tokens)
- All component styles
- Multiple color themes (light, dark, and variants)
- Responsive layout breakpoints

Themes are applied via a `data-theme` attribute on `<body>`. To add a theme, add a new `[data-theme="your-theme"]` block in `style.css`.

---

## SSE / Streaming Pattern

The frontend consumes LLM streaming via `chatStream.js`:
```
POST /api/chat → backend starts streaming
chatStream.js opens EventSource / ReadableStream
Each chunk: {"type": "token", "content": "..."}
chatRenderer.js appends token to the message display
Final chunk: {"type": "done"}
```

---

## PWA

The app is installable as a PWA. `static/sw.js` caches static assets for offline use. `static/manifest.json` defines app metadata for installation prompts.

---

## Related

- Chat API → [`../routes/chat.md`](../routes/chat.md)
- All API routes → [`../routes/README.md`](../routes/README.md)
- Config (app bind, port) → [`../config/README.md`](../config/README.md)
