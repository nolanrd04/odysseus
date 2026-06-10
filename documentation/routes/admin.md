# Auth, Users & Admin Routes

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Authentication

### [`routes/auth_routes.py`](/routes/auth_routes.py) (~24KB)
All authentication endpoints — public routes that bypass the auth middleware.

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/auth/login` | Login with username + password (+ TOTP if 2FA enabled) |
| `POST` | `/api/auth/logout` | Invalidate current session |
| `POST` | `/api/auth/signup` | Create new account (if open registration enabled) |
| `POST` | `/api/auth/change-password` | Change own password |
| `POST` | `/api/auth/reset-password` | Admin-triggered password reset |
| `GET` | `/api/auth/me` | Get current user info |
| `POST` | `/api/auth/2fa/setup` | Generate 2FA TOTP QR code |
| `POST` | `/api/auth/2fa/enable` | Enable 2FA after verifying code |
| `POST` | `/api/auth/2fa/disable` | Disable 2FA |
| `GET` | `/api/auth/validate` | Check if current session is valid |

**Session tokens** are set as `HttpOnly` cookies on login. Cookie security (`Secure`, `SameSite`) depends on `SECURE_COOKIES` env var.

**See also:** [`../core/README.md`](../core/README.md) for auth internals.

---

## Model Configuration

### [`routes/model_routes.py`](/routes/model_routes.py) (~1698 lines)
LLM endpoint and provider configuration.

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/models` | List available models from configured LLM host |
| `POST` | `/api/models/config` | Set active LLM provider/endpoint |
| `POST` | `/api/models/probe` | Test connectivity to an LLM endpoint |
| `GET` | `/api/models/presets` | List saved model presets |
| `POST` | `/api/models/presets` | Save a model preset |

Model config is persisted in `data/settings.json` and also in the DB per-user.

---

## Cookbook (Local Model Management)

### [`routes/cookbook_routes.py`](/routes/cookbook_routes.py) (~2089 lines)
Hardware detection, model download, and local serving — wraps the `llmfit` tool.

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/cookbook/hardware` | Detect GPU/RAM for model recommendations |
| `GET` | `/api/cookbook/models` | List downloadable models |
| `POST` | `/api/cookbook/download` | Download a model |
| `POST` | `/api/cookbook/serve` | Start serving a downloaded model |
| `POST` | `/api/cookbook/stop` | Stop a running model server |
| `GET` | `/api/cookbook/status` | Check running model server status |

**Frontend:** `static/js/cookbook.js`, `static/js/cookbookDownload.js`, `static/js/cookbookServe.js`

---

## API Tokens

### [`routes/api_token_routes.py`](/routes/api_token_routes.py)
Bearer token management for programmatic API access.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/tokens` | List active API tokens |
| `POST` | `/api/tokens` | Create new token |
| `DELETE` | `/api/tokens/{id}` | Revoke token |

Tokens are stored in the `api_tokens` table and validated by `core/auth.py` on each request.

---

## Admin Operations

### [`routes/admin_wipe_routes.py`](/routes/admin_wipe_routes.py) (~6KB)
Admin-only data management. All endpoints require admin role.

| Method | Path | Purpose |
|--------|------|---------|
| `DELETE` | `/api/admin/wipe/messages` | Delete all messages for a user |
| `DELETE` | `/api/admin/wipe/sessions` | Delete all sessions |
| `DELETE` | `/api/admin/wipe/memories` | Wipe vector memory |
| `DELETE` | `/api/admin/wipe/user/{id}` | Delete a user account |

**Frontend:** `static/js/admin.js`

---

## Settings Persistence

User settings (model preferences, theme, feature flags) are managed through `data/settings.json` (global) and the `users` table (per-user). No dedicated settings route file — settings are embedded in the relevant feature routes.

---

## Related

- Auth internals → [`../core/README.md`](../core/README.md)
- Config / env vars → [`../config/README.md`](../config/README.md)
- LLM provider setup → [`../src/llm.md`](../src/llm.md)
