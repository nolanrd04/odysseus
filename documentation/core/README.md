# Core Infrastructure (`core/`)

> Parent: [`../README.md`](../README.md) | Architecture: [`../architecture.md`](../architecture.md)

The `core/` directory contains the foundational infrastructure that every other layer depends on: authentication, the database ORM, security middleware, and shared models.

---

## Files

### [`core/auth.py`](/core/auth.py)
Multi-user authentication system.

**Responsibilities:**
- Password hashing and verification (bcrypt)
- Session token creation, validation, and expiry
- Bearer token support (for API access)
- 2FA enforcement (TOTP via `pyotp`)
- User role management (admin vs. regular user)

**Key functions to look for:**
- `validate_session(token)` — used by middleware on every request
- `create_session(user_id)` — called on login
- `hash_password / verify_password` — bcrypt wrappers
- `verify_totp(code, secret)` — 2FA check

**Storage:** credentials in `data/auth.json`, session tokens in `data/sessions.json`.

---

### [`core/database.py`](/core/database.py) (~84KB)
SQLAlchemy ORM — the largest file in the codebase. Defines all database models and provides the session factory.

**Models defined here:**
| Model | Table | Purpose |
|-------|-------|---------|
| `User` | `users` | User accounts, roles |
| `Session` | `sessions` | Chat sessions (conversations) |
| `Message` | `messages` | Individual chat messages |
| `Task` | `tasks` | Scheduled automation tasks |
| `CalendarEvent` | `calendar_events` | Calendar entries |
| `MCPServer` | `mcp_servers` | Registered MCP server configs |
| `APIToken` | `api_tokens` | Bearer tokens for API access |
| `Note` | `notes` | User notes with reminders |
| `Contact` | `contacts` | Address book entries |
| `Document` | `documents` | Editor documents |

**Key patterns:**
- Database URL from `DATABASE_URL` env var (defaults to `sqlite:///./data/app.db`)
- `get_db()` — FastAPI dependency for getting a DB session
- All models have `owner_id` for multi-user scoping (security-critical)

> When changing schema: add columns with `nullable=True` or a default to avoid breaking existing databases. There is no migration framework — schema changes are applied via `setup.py`.

---

### [`core/middleware.py`](/core/middleware.py)
ASGI middleware stack applied in `app.py`.

**What it does:**
- Enforces authentication on all non-public routes (redirects to `/login` or returns 401)
- Adds security response headers (CSP, X-Frame-Options, etc.)
- Request timeout enforcement (except SSE streaming endpoints)
- `LOCALHOST_BYPASS` dev mode: skips auth when request is from localhost

**Public routes** (no auth required): `/login`, `/static/*`, `/api/auth/*`, and a few others defined in the middleware's allowlist.

---

### [`core/models.py`](/core/models.py)
Pure Python dataclasses (no DB). Lightweight in-memory representations used across `src/`.

- `ChatMessage` — a single message in a conversation (role, content, tool_calls, etc.)
- `Session` — active session metadata

These are distinct from the SQLAlchemy models in `database.py`. The ORM models handle persistence; these handle runtime state.

---

### [`core/constants.py`](/core/constants.py)
App-wide constants that don't vary per environment:
- Default file paths
- Timeout values
- API config defaults
- Supported file types

For environment-variable-driven config, see [`src/config.py`](/src/config.py) and [`../config/README.md`](../config/README.md).

---

### [`core/session_manager.py`](/core/session_manager.py)
Session lifecycle management — creating, loading, persisting, and expiring chat sessions (conversations). Wraps DB access for session-related operations.

---

### [`core/exceptions.py`](/core/exceptions.py)
Custom exception classes raised by `core/` and `src/` modules. Caught by route handlers to return appropriate HTTP responses.

---

### [`core/platform_compat.py`](/core/platform_compat.py)
Cross-platform helpers for path handling and OS-specific behavior (Linux vs. macOS vs. Windows).

---

## Relationships

- **All routes** depend on `core/auth.py` (via `middleware.py`) and `core/database.py` (via `get_db()`).
- **`src/`** modules use `core/models.py` for in-memory types and `core/constants.py` for defaults.
- **`setup.py`** initializes the database schema defined in `core/database.py`.

See [`../architecture.md`](../architecture.md) for the authentication flow.
