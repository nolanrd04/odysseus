# Calendar Routes

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Files

### [`routes/calendar_routes.py`](/routes/calendar_routes.py) (~1288 lines)
Calendar CRUD and CalDAV synchronization endpoints.

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/calendar/events` | List events (date range) |
| `POST` | `/api/calendar/events` | Create new event |
| `PUT` | `/api/calendar/events/{id}` | Update event |
| `DELETE` | `/api/calendar/events/{id}` | Delete event |
| `POST` | `/api/calendar/sync` | Manual CalDAV sync (pull remote → local) |
| `GET` | `/api/calendar/export` | Export events as `.ics` file |
| `POST` | `/api/calendar/import` | Import `.ics` file |
| `GET` | `/api/calendar/recurrence` | Expand a recurring event rule |

**Data model:** Events stored in `calendar_events` table (`core/database.py`). Recurrence rules stored as RFC 5545 RRULE strings and expanded on query.

---

### [`src/caldav_sync.py`](/src/caldav_sync.py) (~13KB)
CalDAV protocol client for two-way sync.

**Supported CalDAV servers:**
- Apple Calendar (iCloud)
- Fastmail
- Nextcloud
- Radicale (self-hosted)
- Any standards-compliant CalDAV server

**Sync direction:**
- **Pull** — fetches remote events, creates/updates/deletes local records
- **Push** — sends local changes back to the CalDAV server

**Key functions:**
- `sync_calendar(account_config)` — full bidirectional sync
- `push_event(event)` — write a single event to CalDAV
- `delete_remote_event(event_uid)` — delete from CalDAV

CalDAV account credentials are stored per-user in the database (not in `.env`).

---

## Recurrence Handling

Recurring events are stored as a single DB record with an RRULE. Expansion (generating individual occurrences) is done at query time using `python-dateutil`. The frontend receives pre-expanded occurrences for the requested date range.

---

## Related

- Contact lookup (CalDAV addressbook) → `routes/contacts_routes.py` — see [`README.md`](README.md)
- Calendar sync background job → [`../architecture.md`](../architecture.md) (Background Jobs)
- ICS parsing library → `icalendar` (in `requirements.txt`)
