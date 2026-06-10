# Email Routes

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Files

### [`routes/email_routes.py`](/routes/email_routes.py) (~3198 lines)
Full IMAP/SMTP email client implementation exposed as API endpoints.

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/email/inbox` | Fetch inbox messages (IMAP) |
| `GET` | `/api/email/{id}` | Read a specific email + full thread |
| `POST` | `/api/email/send` | Send email (SMTP) |
| `POST` | `/api/email/reply` | Reply to thread |
| `DELETE` | `/api/email/{id}` | Delete/trash email |
| `POST` | `/api/email/triage` | AI triage: categorize and suggest actions |
| `POST` | `/api/email/auto-reply` | AI draft reply for an email |
| `GET` | `/api/email/search` | Search inbox (IMAP SEARCH) |
| `GET` | `/api/email/folders` | List IMAP folders/labels |

**IMAP/SMTP config** is stored per-user in the database. Users configure their mail server via the UI (Settings → Email).

**AI triage:** calls `src/ai_interaction.py` with the email content and a triage prompt. Returns categories (action needed, newsletter, etc.) and suggested replies.

---

### [`routes/email_pollers.py`](/routes/email_pollers.py) (~1102 lines)
Background email polling workers.

**What it does:**
- Runs on a timer (configurable interval) per configured email account
- Fetches new messages via IMAP
- Updates unread counts
- Triggers notifications (if ntfy is configured)
- Optionally runs AI auto-summarize on new messages

**Coordination:** Uses `src/event_bus.py` to notify the frontend of new mail without polling from the browser.

---

## Email Thread Parsing

Thread reconstruction from IMAP is handled by [`src/email_thread_parser.py`](/src/email_thread_parser.py). See [`../src/documents.md`](../src/documents.md) for details.

---

## Configuration

Email accounts are configured per-user via the UI. The following are stored in the user's account settings (DB, not `.env`):

- IMAP host, port, SSL/TLS
- SMTP host, port, SSL/TLS
- Username and password (encrypted at rest)

No global email config in `.env` — each user configures their own account.

---

## Related

- Email thread parsing → [`../src/documents.md`](../src/documents.md)
- AI interaction for triage → [`../src/llm.md`](../src/llm.md)
- Background job coordination → [`../architecture.md`](../architecture.md) (Background Jobs section)
- Webhook triggers from email → [`README.md`](README.md) → `webhook_routes.py`
