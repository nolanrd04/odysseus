# Manual Workflows — Manual-Trigger Tasks

**Session:** `2026_6_9.1`
**Status:** Backend complete. Frontend sidebar placement revised — needs reimplementation (see below).
**Handoff:** `documentation/.SESSION_HANDOFFS/quick_automations/2026_6_9.1_quick_automations_manual_trigger_tasks.json`
**Plan:** `documentation/.MY_CHANGES/plans/quick_automations.md`

---

## Problem

The Tasks system only supported automatic triggers (schedule, event, webhook). There was no way to create a task that only runs when you explicitly click it. The user wanted a "Get Updates" button in the sidebar that summarizes new git commits and emails a digest, plus the ability to add more one-click workflows in the future.

---

## Solution

Extended the existing Tasks system with a new `trigger_type="manual"`. Manual tasks:
- Are seeded and managed by the same DB/scheduler/UI as all other tasks
- Never auto-fire (they have `next_run=NULL` and the scheduler's due-tasks query filters by `next_run <= now`)
- Appear in a dedicated **Manual Workflows** sidebar section (same level as Chats, Email, Models, Tools) as a collapsible list with play-button items and a "+" to add more
- Can be created from the Tasks modal using a new "Manual" trigger type option

The first built-in manual task is **Get Updates** — diffs new git commits, summarizes with LLM, and emails the digest.

---

## ⚠️ Sidebar Placement — Design Revision

The initial implementation placed quick-run items as indented sub-items inside `#tools-section` (below the Tasks entry). **This has been revised.**

**New design:** A standalone `div.section#manual-workflows-section` placed in `.sidebar-inner` at the same level as `#sessions-section`, `#email-section`, `#models-section`, and `#tools-section`. It behaves like any other sidebar section: collapsible, with a header row containing a lightning-bolt icon, "Manual Workflows" label, and a "+" button that opens the Tasks modal pre-set to Manual trigger type.

**What needs to change in the frontend:**

| File | Current (wrong) | Target |
|---|---|---|
| `static/index.html` | `<div id="quick-run-section">` inside `#tools-section` | New `<div class="section" id="manual-workflows-section">` as a sibling of `#tools-section` in `.sidebar-inner` |
| `static/js/quickRun.js` | Renders items into `#quick-run-section` with no section chrome | Renders items into the list container inside `#manual-workflows-section`; section header + collapse handled by the section |
| `static/style.css` | `.quick-run-item { padding-left: 28px; }` (indent for sub-item) | Remove indent — items are full-width, same as items in other sections |

The backend (Python routes, scheduler, action) and the tasks.js changes are all correct and do not need to change.

---

## Code Changes

### `src/builtin_actions.py`

**New function `action_get_updates()` (lines 2206–2376)**

- Reads `data/updates_{owner_slug}.json` for `last_commit` (first run falls back to `--since=30 days ago`)
- Runs `git -C {repo_root} log --oneline --no-merges {last_commit}..HEAD` via `asyncio.create_subprocess_exec`
- Raises `TaskNoop` if no new commits (surfaces as "skipped" in Activity view)
- Gets current `HEAD` sha via `git rev-parse HEAD` for the next run's baseline
- Builds LLM messages: system prompt requests a numbered list + 2–3 sentence paragraph, plain text only; user message includes the commit list and up to 3 previous digests for context
- Calls `llm_call_async_with_fallback([(url, model, headers)], ...)` — tries `utility` endpoint first, falls back to `default`
- Sends a `MIMEMultipart("alternative")` email from the user's configured SMTP address to themselves, via `asyncio.to_thread(_smtp_send)` so it doesn't block the event loop
- Falls through gracefully if no SMTP is configured (includes error note in the result string)
- Saves updated state to `data/updates_{owner_slug}.json`: `{last_commit, history: [{date, summary}]}` — capped at 20 history entries
- Returns `(result_string, True)` on success; `TaskNoop` re-raised; generic exceptions caught and returned as `(str(e), False)`

**Registration (lines 2399, 2420)**
```python
# BUILTIN_ACTIONS dict
"get_updates": action_get_updates,

# BUILTIN_ACTION_INFO dict
"get_updates": "Summarize new git commits since last run and email a digest to yourself.",
```

---

### `src/task_scheduler.py`

**Added `get_updates` to `HOUSEKEEPING_DEFAULTS` (line 216)**
```python
"get_updates": {
    "name": "Get Updates",
    "trigger_type": "manual",
    "schedule": None,
    "scheduled_time": None,
    "cron_expression": None,
    "output_target": "none",
},
```
No `ship_paused` — seeds immediately as `status="active"` with `next_run=NULL`. The `ensure_defaults(owner)` loop already handles seeding: it only computes `next_run` when `trigger_type == "schedule"`, so manual tasks are seeded correctly without any additional logic.

---

### `routes/task_routes.py`

**Bug fix — manual task status (line 548)**

Before: `status="active" if (req.trigger_type in ("event", "webhook") or next_run) else "completed"`

After: `status="active" if (req.trigger_type in ("event", "webhook", "manual") or next_run) else "completed"`

Without this fix, creating a manual task would immediately mark it `"completed"` and the task would disappear from the Quick Run sidebar.

**New `GET /api/tasks/manual` endpoint (lines 653–678)**

Placed *before* `@router.get("/{task_id}")` to avoid route shadowing.

```python
@router.get("/manual")
async def list_manual_tasks(request: Request):
    """Return tasks with trigger_type='manual' for the Quick Run sidebar."""
    # Queries trigger_type='manual' AND status != 'completed', filtered by owner.
    # Returns: {tasks: [{id, name, action, task_type, last_run, status}]}
```

---

### `static/js/tasks.js`

**`_TASK_PRESETS` (lines 944–945)** — two new preset cards in the "Add Task" picker:
```js
{ label: 'Manual / on demand', desc: 'Run from the Quick Run sidebar whenever you like', taskType: 'llm',    triggerType: 'manual' },
{ label: 'Action on demand',   desc: 'Built-in action you trigger manually',            taskType: 'action', triggerType: 'manual' },
```

**Trigger toggle button (line 1040)** — added a "Manual" button to the trigger type toggle row in `_showForm()`:
```html
<button class="task-toggle-btn ${curTriggerType === 'manual' ? 'active' : ''}" data-val="manual" ...>
  <svg><!-- play triangle --></svg>Manual
</button>
```

**`renderTriggerOpts()` (line 1284)** — added the manual branch that hides schedule/event/webhook fields and shows a hint:
```js
} else if (triggerType === 'manual') {
  triggerOpts.innerHTML = '<div ...>This task only runs when you click it in the Quick Run sidebar. No schedule needed.</div>';
}
```

**`closeTasks()` (line 2657)** — added custom event dispatch so `quickRun.js` refreshes when the modal closes:
```js
document.dispatchEvent(new CustomEvent('tasks-panel-closed'));
```

---

### `static/index.html`

**Current (interim) state — `#quick-run-section` placeholder (line 906):**
```html
<div id="quick-run-section"></div>   <!-- inside #tools-section, needs to move -->
```

**Target state — replace with a full section sibling of `#tools-section`:**
```html
<div class="section" id="manual-workflows-section">
  <div class="section-header-flex">
    <span class="section-title">
      <svg class="section-icon" ...><!-- lightning bolt --></svg>
      <span>Manual Workflows</span>
    </span>
    <button type="button" class="section-header-btn list-item-plus-btn"
            id="manual-workflow-add-btn" title="Add workflow">
      <!-- + SVG -->
    </button>
  </div>
  <div id="manual-workflows-list"></div>
</div>
```

**Script tag (line 2339)** — already added after `a11y.js` (no change needed):
```html
<script type="module" src="/static/js/quickRun.js"></script>
```

---

### `static/js/quickRun.js` *(new file, 166 lines — needs sidebar target update)*

ES module. Self-initializes on `DOMContentLoaded`.

| Function | Description |
|---|---|
| `_fetchManualTasks()` | `GET /api/tasks/manual` → returns task array |
| `_relativeTime(isoStr)` | Formats `last_run` as "2h ago", "3d ago", etc. |
| `_render()` | Currently targets `#quick-run-section`. **Needs to target `#manual-workflows-list`** and drop the `quick-run-item` indent class. |
| `_runTask(id)` | Adds to `_running` Set, re-renders spinner, `POST /api/tasks/{id}/run`, polls `GET /api/tasks/{id}/runs` every 2s (up to 45 iterations / 90s) for status ≠ queued/running, then calls `_showResult` |
| `_showResult(id, result, error)` | Creates `.qr-result-modal` overlay with task name, result body (or error in red), close button; dismisses on X or backdrop click |
| `init()` | Fetches and renders; exported as `default.refresh` too |

Also needs: wire up `#manual-workflow-add-btn` click to open the Tasks modal pre-set to Manual trigger type.

Event listeners:
- `DOMContentLoaded` → `init()`
- `visibilitychange` → `_render()` (refreshes relative times when tab regains focus)
- `tasks-panel-closed` → `init()` (refreshes list after user creates a task in the modal)

---

### `static/style.css`

**Current state** — block already added (the modal styles are correct and stay; the indent is wrong):
```css
.quick-run-item { padding-left: 28px; }   /* ← remove this indent, items are full-width */
.quick-run-item .quick-run-last-run { font-size: 10px; opacity: 0.4; }
.quick-run-item.quick-run-running { opacity: 0.55; pointer-events: none; }

@keyframes qr-spin { to { transform: rotate(360deg); } }
.qr-spin { transform-origin: center; animation: qr-spin 1.4s linear infinite; }

/* Result modal — these are correct, keep as-is */
.qr-result-modal / .qr-result-inner / .qr-result-header / .qr-result-body / .qr-result-close
```

**Target state** — remove `padding-left: 28px` from `.quick-run-item`. The section collapse behavior uses the existing `.section` / `.section-header-flex` system already in style.css.

---

## Architecture Notes

**Why manual tasks are never auto-dispatched**
The scheduler's `_check_due_tasks()` queries `WHERE status='active' AND next_run <= now`. Manual tasks have `next_run=NULL` so they never appear in this query. No guard was added to the scheduler — the existing filter already handles it.

**State file for Get Updates**
`data/updates_{owner_slug}.json` — created on first run. Shape:
```json
{
  "last_commit": "abc1234",
  "history": [
    { "date": "2026-06-09", "summary": "..." }
  ]
}
```
History is capped at 20 entries. The last 3 are included in the LLM prompt as context so it understands what's already been covered.

**Email sending pattern**
Follows the same pattern as `dispatch_reminder` in `note_routes.py`: calls `_get_email_config()` for SMTP credentials, builds `MIMEMultipart`, calls `asyncio.to_thread(_smtp_send)` to avoid blocking.

**Run result polling**
`quickRun.js` polls `GET /api/tasks/{id}/runs?limit=1` every 2s. The run lifecycle is `queued → running → success/error/skipped`. The poll exits when status is neither `queued` nor `running`. The result and error fields come directly from the `TaskRun` DB row via `_run_to_dict()`.

---

## What to Test

1. Log in — "Get Updates" should auto-appear in the sidebar Quick Run section (seeded by `ensure_defaults`)
2. Click "Get Updates" — spinner, LLM call, email (if SMTP configured), result modal
3. Tasks panel → + → confirm "Manual / on demand" and "Action on demand" presets exist
4. Create a custom manual LLM task → save → confirm it appears in Quick Run immediately
5. No email configured → Get Updates should still succeed, result shows "Email not sent: No SMTP configured"
6. No new commits → Task shows as "skipped" in Activity, Quick Run shows no error