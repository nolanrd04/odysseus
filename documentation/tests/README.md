# Test Suite (`tests/`)

> Parent: [`../README.md`](../README.md)

---

## Overview

200+ test files covering the full application. Tests are written with `pytest` and `pytest-asyncio`. The test suite is integration-heavy — most tests hit the actual database and in-process application rather than mocking.

---

## Running Tests

```bash
# All tests
pytest

# Specific file
pytest tests/test_auth.py

# Specific test
pytest tests/test_auth.py::test_login_success

# With output
pytest -s

# Stop on first failure
pytest -x
```

Config: `pyproject.toml` — sets asyncio mode, test paths, and markers.

---

## Test Categories

### Authentication
| File pattern | What's tested |
|-------------|---------------|
| `test_auth*.py` | Login, logout, session validation, 2FA |
| `test_session_endpoints.py` | Session token lifecycle, expiry |

### Security & Isolation
| File pattern | What's tested |
|-------------|---------------|
| `test_*_owner_scope*.py` | Multi-user data isolation (one user can't read another's data) |
| `test_endpoint_isolation*.py` | Route-level auth enforcement |
| `test_agent_tool_confinement*.py` | Shell/file tool path restrictions |
| `test_prompt_injection*.py` | Tool name and argument sanitization |

### Chat & AI
| File pattern | What's tested |
|-------------|---------------|
| `test_chat*.py` | Message routing, context building, streaming |
| `test_*model*.py` | Model switching, token estimation, context window |
| `test_image_handling*.py` | Multimodal message handling |

### Agent & Tools
| File pattern | What's tested |
|-------------|---------------|
| `test_tool*.py` | Individual tool implementations |
| `test_agent_loop*.py` | Multi-round tool use, round limits |
| `test_mcp*.py` | MCP server registration and tool proxying |

### Documents & Uploads
| File pattern | What's tested |
|-------------|---------------|
| `test_document*.py` | PDF parsing, HTML rendering, orphan cleanup |
| `test_upload*.py` | File type detection, size limits |

### Email
| File pattern | What's tested |
|-------------|---------------|
| `test_email*.py` | IMAP timeout handling, SMTP security, thread parsing |

### Calendar
| File pattern | What's tested |
|-------------|---------------|
| `test_calendar*.py` | Recurrence expansion, CalDAV writeback, owner scoping |

### Database
| File pattern | What's tested |
|-------------|---------------|
| `test_db*.py` | Schema integrity, atomic operations |

### UI / Frontend
| File pattern | What's tested |
|-------------|---------------|
| `test_*keyboard*.py` | Keyboard shortcut bindings |
| `test_*theme*.py` | Theme switching |
| `test_*dropdown*.py` | UI component behavior |

### Infrastructure
| File pattern | What's tested |
|-------------|---------------|
| `test_hwfit*.py` | Hardware detection, quantization format detection |
| `test_search*.py` | Search indexing and cache invalidation |
| `test_cache*.py` | Multi-layer cache behavior |

---

## Test Fixtures

Common fixtures are defined in `tests/conftest.py`:
- `test_client` — FastAPI `TestClient` with a fresh test database
- `auth_headers` — pre-authenticated request headers
- `admin_headers` — admin user request headers
- `test_user` — created user fixture

Tests use the `test_client` for all HTTP calls and a temporary SQLite database that's reset between test runs.

---

## Writing New Tests

1. Add your test file to `tests/` following the naming convention `test_{feature}.py`
2. Use `pytest-asyncio` for async tests: `@pytest.mark.asyncio`
3. Use the `test_client` fixture for API calls
4. Always test owner scope if adding any endpoint that stores user data
5. For tool tests, verify path confinement and timeout enforcement

---

## Related

- Security model → [`/SECURITY.md`](/SECURITY.md)
- Threat model → [`/THREAT_MODEL.md`](/THREAT_MODEL.md)
- Contributing guide → [`/CONTRIBUTING.md`](/CONTRIBUTING.md)
