# API Routes (`routes/`)

> Parent: [`../README.md`](../README.md) | Architecture: [`../architecture.md`](../architecture.md)

The `routes/` directory contains ~50 FastAPI router files. All are registered in [`app.py`](/app.py) with an `/api/` prefix (except static file routes).

---

## Sub-documents

| Area | File | Key routes |
|------|------|-----------|
| Chat & sessions | [`chat.md`](chat.md) | `chat_routes.py`, `session_routes.py` |
| Email | [`email.md`](email.md) | `email_routes.py`, `email_pollers.py` |
| Calendar | [`calendar.md`](calendar.md) | `calendar_routes.py` |
| Media (images, TTS, STT) | [`media.md`](media.md) | `gallery_routes.py`, `tts_routes.py`, `stt_routes.py` |
| Auth, users, admin | [`admin.md`](admin.md) | `auth_routes.py`, `model_routes.py`, `admin_wipe_routes.py` |

---

## All Route Files at a Glance

### Chat & Conversations
| File | Prefix | Purpose |
|------|--------|---------|
| `chat_routes.py` | `/api/chat` | Chat streaming, agent mode, RAG queries |
| `session_routes.py` | `/api/sessions` | Session CRUD, folders, archiving |

### Users & Auth
| File | Prefix | Purpose |
|------|--------|---------|
| `auth_routes.py` | `/api/auth` | Login, signup, logout, 2FA, password reset |
| `api_token_routes.py` | `/api/tokens` | Bearer token management |
| `admin_wipe_routes.py` | `/api/admin` | Admin data deletion operations |

### Memory & Documents
| File | Prefix | Purpose |
|------|--------|---------|
| `memory_routes.py` | `/api/memory` | Vector memory CRUD, import/export |
| `document_routes.py` | `/api/documents` | Multi-tab editor, AI editing |
| `upload_routes.py` | `/api/upload` | File upload with vision processing |
| `embedding_routes.py` | `/api/embeddings` | Embedding model management, RAG config |
| `skills_routes.py` | `/api/skills` | User-defined skills (RAG-indexed, agent-callable) |

### Communication
| File | Prefix | Purpose |
|------|--------|---------|
| `email_routes.py` | `/api/email` | IMAP inbox, SMTP send, search, triage |
| `email_pollers.py` | — | Background email polling workers |
| `calendar_routes.py` | `/api/calendar` | CalDAV sync, .ics import/export, CRUD |
| `contacts_routes.py` | `/api/contacts` | Contact management, CalDAV person lookup |
| `note_routes.py` | `/api/notes` | Notes with reminders and todo lists |

### AI Features
| File | Prefix | Purpose |
|------|--------|---------|
| `research_routes.py` | `/api/research` | Deep research sessions and reports |
| `compare_routes.py` | `/api/compare` | Multi-model blind A/B comparison |
| `gallery_routes.py` | `/api/gallery` | Image generation, inpainting, upscaling |
| `tts_routes.py` | `/api/tts` | Text-to-speech |
| `stt_routes.py` | `/api/stt` | Speech-to-text |

### Configuration & Infrastructure
| File | Prefix | Purpose |
|------|--------|---------|
| `model_routes.py` | `/api/models` | LLM endpoint config, provider setup |
| `mcp_routes.py` | `/api/mcp` | MCP server management, tool discovery |
| `search_routes.py` | `/api/search` | Web search (SearXNG), provider setup |
| `task_routes.py` | `/api/tasks` | Scheduled task CRUD, webhook triggers |
| `shell_routes.py` | `/api/shell` | Shell command execution (sandboxed) |
| `webhook_routes.py` | `/api/webhooks` | Outgoing webhooks for automation |
| `cookbook_routes.py` | `/api/cookbook` | Hardware detection, model download/serving |
| `hwfit_routes.py` | `/api/hwfit` | Hardware fitting recommendations |
| `vault_routes.py` | `/api/vault` | Encrypted backup/restore |
| `backup_routes.py` | `/api/backup` | Data export/import |
| `editor_draft_routes.py` | `/api/drafts` | Document editor draft management |

---

## Adding a New Route

1. Create `routes/your_feature_routes.py` with a `router = APIRouter()` object
2. Define your endpoints on `router`
3. Register in `app.py`: `app.include_router(your_feature_routes.router, prefix="/api")`
4. Auth is enforced automatically by `core/middleware.py` — mark public endpoints in the middleware allowlist if needed

---

## Authentication on Routes

All `/api/*` routes require a valid session cookie or `Authorization: Bearer <token>` header unless explicitly listed in `core/middleware.py`'s public allowlist. The current user is available as a dependency:

```python
from core.auth import get_current_user

@router.get("/example")
async def example(user = Depends(get_current_user), db = Depends(get_db)):
    ...
```

Multi-user scoping: always filter DB queries by `owner_id = user.id`. See [`../core/README.md`](../core/README.md).
