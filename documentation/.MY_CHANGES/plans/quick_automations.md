# Plan: Manual-Trigger Tasks + "Get Updates" Built-in

## Overview

Extend the existing Tasks system with a `"manual"` trigger type. Manual tasks never auto-fire — they only run when the user explicitly clicks them. "Get Updates" ships as the first built-in manual action. Users can create additional manual tasks (arbitrary LLM prompts, any output target) from the existing task modal.

A new **Quick Run** sidebar section surfaces all manual tasks as one-click buttons so they're always accessible without opening the Tasks panel.

This is additive — nothing about the existing scheduled/event/webhook task flow changes.

---

## What Changes

| Area | Change |
|------|--------|
| `core/database.py` | No schema change needed — `trigger_type` is a free-form string column |
| `src/task_scheduler.py` | Skip manual tasks in the schedule poll loop; add `get_updates` built-in action handler |
| `routes/task_routes.py` | Allow `trigger_type = "manual"` in validation (remove the schedule-required guard for it); expose `GET /api/tasks/manual` convenience endpoint |
| `static/js/tasks.js` | Add "Manual" option to trigger type select in the create/edit modal |
| `static/index.html` | Add `#quick-run-section` in the sidebar (after Tasks entry) |
| `static/js/quickRun.js` | **New** — sidebar Quick Run section: fetch manual tasks, render, run on click |
| `static/style.css` | Quick Run item styles + running spinner state |

---

## Implementation Steps

### 1. Scheduler — `src/task_scheduler.py`

**a. Skip manual tasks in the poll loop**

In `_check_due_tasks` (the loop that fires scheduled tasks), add a guard:
```python
if (task.trigger_type or "schedule") == "manual":
    continue
```
Manual tasks are only dispatched via `run_task_now`.

**b. Add `get_updates` built-in action**

Add to `HOUSEKEEPING_DEFAULTS`:
```python
"get_updates": {
    "name": "Get Updates",
    "trigger_type": "manual",
    "schedule": None,
    "scheduled_time": None,
    "cron_expression": None,
    "ship_paused": False,
},
```

Add `get_updates` handler inside the `_execute_builtin_action` / `_run_builtin_action` dispatch block. The handler:
1. Reads `data/updates_{owner}.json` to find `last_commit` (or defaults to 30-days-ago).
2. Runs `git log --oneline --no-merges {last_commit}..HEAD` via `asyncio.create_subprocess_exec` in the repo root.
3. If no new commits → returns early with a "no new commits" result (still counts as a successful run).
4. Calls `llm_call_async_with_fallback` with prompt:
   ```
   Summarize these git commits for a developer digest.
   
   New commits:
   {commit_list}
   
   Previous digests (context only):
   {last_3_summaries}
   
   Return:
   1. A numbered list of changes (one line each).
   2. A 2–3 sentence paragraph on what was most important.
   
   Plain text only, no markdown headers.
   ```
5. Sends the summary email to the task owner's configured SMTP `from_address` (send-to-self) using the existing `_send_smtp_message` pattern from `email_routes.py`.
6. Saves updated `data/updates_{owner}.json` with new `last_commit` and prepends to `history` (cap at 20 entries).
7. Returns the summary text as the task result (gets stored in `TaskRun.result` as normal).

### 2. Routes — `routes/task_routes.py`

In the `TaskCreate` validation block, the current guard is:
```python
if req.trigger_type == "schedule" and not req.schedule:
    raise HTTPException(400, "schedule is required for trigger_type=schedule")
```
Add a bypass so `trigger_type = "manual"` skips schedule validation entirely. No other route changes needed — `run_task_now` already exists and works.

Add a lightweight convenience endpoint for the sidebar:
```python
@router.get("/manual")
async def list_manual_tasks(request: Request):
    """Return only tasks with trigger_type='manual', for the Quick Run sidebar."""
```
Returns `id`, `name`, `action`, `last_run`, `status` — nothing else needed.

### 3. Tasks UI — `static/js/tasks.js`

In the trigger-type select dropdown (create/edit modal), add:
```html
<option value="manual">Manual (run on demand)</option>
```
When "Manual" is selected, hide the schedule/time/cron fields (same pattern as "event" trigger hides them). Show a note: "This task only runs when you click Run."

### 4. Sidebar — `static/index.html`

Add after `#tool-tasks-btn` and before `#tool-theme-btn`:

```html
<div id="quick-run-section">
  <!-- items injected by quickRun.js -->
</div>
```

No wrapper section header — quick-run items are visually indented under Tasks, making them feel like sub-items of the Tasks panel rather than a new top-level section. If users have zero manual tasks, this div stays empty (no visual noise).

### 5. Frontend Module — `static/js/quickRun.js`

New ES module, imported in `index.html`.

```js
// Key functions:
initQuickRun()          // fetch /api/tasks/manual, render list, set up click handlers
renderQuickRun(tasks)   // build list-item divs, append to #quick-run-section
runManualTask(id, btn)  // POST /api/tasks/{id}/run-now, spinner on btn, toast result
```

Each rendered item:
```html
<div class="list-item quick-run-item" data-task-id="{id}">
  <svg><!-- play icon --></svg>
  <span class="grow">{name}</span>
  <span class="quick-run-last-run">{last_run_relative}</span>  <!-- e.g. "2h ago" -->
</div>
```

On click → show inline spinner on that item → await run-now → show result in a dismissible toast modal (reusing the existing modal system). On success, update the last-run label.

### 6. CSS — `static/style.css`

```css
.quick-run-item { padding-left: 28px; }           /* indent under Tasks */
.quick-run-item.running { opacity: 0.6; }
.quick-run-item .quick-run-last-run { font-size: 10px; opacity: 0.45; }
```

---

## "Get Updates" Email Format

Subject: `Odysseus Update Digest — {date}`

Body:
```
New commits since {last_check}:

1. ...
2. ...
3. ...

Summary: ...
```

---

## User Flow: Creating a Custom Manual Task

1. Open Tasks panel (sidebar → Tasks).
2. Click "+".
3. Set trigger type → "Manual (run on demand)".
4. Enter name, prompt, output target (session/email/etc.).
5. Save.
6. Task immediately appears in the Quick Run sidebar section.

---

## Out of Scope

- Prompt template variables (`{{date}}`, etc.) — straightforward to add later, not needed for launch.
- Scheduling a manual task as both manual AND on a schedule — keep them separate types.
- "Get Updates" for non-git repos — git is assumed for now.