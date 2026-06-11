# Compare Mode — File Attachments

**Session:** `2026_6_10.1`
**Status:** Complete
**Plan:** `documentation/.MY_CHANGES/plans/model_comparison_updates.md` (item 1)

---

## Problem

The model comparison view had no way to attach files to a prompt. The attach button (`overflow-attach-btn`) and the overflow menu button (`overflow-plus-btn`) were both forcibly hidden during compare mode, making it impossible to send images or documents alongside a comparison prompt.

---

## Solution

Kept the overflow "+" button visible during compare (the irrelevant menu items — RAG, Documents, TTS — are already individually hidden), wired the file upload flow into `handleCompareSubmit`, and forwarded the uploaded file IDs to every `streamToPane` call so all panes receive the same attachments.

---

## Code Changes

### `static/js/compare/index.js`

**Import added (top of file)**
```js
import fileHandlerModule from '../fileHandler.js';
```

**`_buildCompareUI` — step 11: restore `attach-strip`**

The `attach-strip` element (which renders the file chips) is a sibling of `.chat-input-bar` in the DOM. When compare mode hides all `chat-container` children in step 7, the strip gets hidden too. Step 11 now also re-appends it:

```js
// Before moving the input bar, restore attach-strip so file chips are visible
const attachStrip = document.getElementById('attach-strip');
if (attachStrip) {
  attachStrip.style.display = '';
  if (attachStrip.dataset.cmpHidden) delete attachStrip.dataset.cmpHidden;
  container.appendChild(attachStrip);
}
```

**`_buildCompareUI` — step 12: removed two IDs from the hide list**

`overflow-plus-btn` and `overflow-attach-btn` were removed. The overflow menu now remains accessible during compare, exposing only "Attach files" and "Prompt" (all other menu items are still individually hidden).

Before:
```js
['overflow-tts-btn', 'overflow-attach-btn', 'overflow-rag-btn', 'overflow-research-btn',
 'overflow-doc-btn', 'rag-indicator-btn', 'web-toggle-btn', 'bash-toggle-btn', 'overflow-plus-btn']
```

After:
```js
['overflow-tts-btn', 'overflow-rag-btn', 'overflow-research-btn',
 'overflow-doc-btn', 'rag-indicator-btn', 'web-toggle-btn', 'bash-toggle-btn']
```

**`handleCompareSubmit` — made async, added file upload**

- Changed `function` → `async function`
- Checks `fileHandlerModule.getPendingCount()` so a prompt with only files and no text is valid
- Captures `pendingInfo` (name/mime/previewUrl per file) before upload so the display data isn't lost when `uploadPending()` clears `pendingFiles`
- Calls `fileHandlerModule.uploadPending()` and gets back an array of server-assigned file IDs
- Bails early (without sending) if upload failed and there's no text message
- Passes `attachmentIds` and `pendingInfo` to `_executeCompare`

**`_executeCompare(message, attachmentIds, pendingInfo)` — new parameters**

- Added `attachmentIds` and `pendingInfo` params (both default to `[]`)
- User message bubble now conditionally renders an `.attach-cards` row with one `.attach-card` chip per file, so each pane's history shows what was attached
- Both the parallel and sequential `streamToPane` calls now pass `{ ..., attachmentIds }` in opts

### `static/js/compare/stream.js`

**`streamToPane` — forward attachment IDs to backend**

After the existing `fd.append('session', sessionId)` line:
```js
if (opts.attachmentIds && opts.attachmentIds.length) {
  fd.append('attachments', JSON.stringify(opts.attachmentIds));
}
```

This is the same `attachments` field that `/api/chat_stream` already accepts from the main chat flow. No backend changes were required.

---

## Architecture Notes

**Why the same IDs go to every pane**

Each compare pane has its own ephemeral session, but all sessions share the uploaded file objects stored server-side under the same IDs. Sending the same ID array to every pane is safe — the backend reads the file by ID from the upload store; it is not consumed or mutated per-request.

**Reroll is unaffected**

`panes.js rerollPane` calls `streamToPane` without `attachmentIds` in opts. The new guard (`opts.attachmentIds && opts.attachmentIds.length`) treats `undefined` as empty and skips the `fd.append`, so reroll continues to work unchanged.

**Overflow menu during compare**

With `overflow-plus-btn` visible, the menu shows:
- **Attach files** — functional
- **Prompt** — functional (presets still work in compare)
- Documents — hidden (individually)
- RAG — hidden (individually)
- TTS — hidden (individually)

**`attach-strip` lifecycle**

The strip is appended to `chat-container` as a child (before `chat-input-bar`) during compare setup. On deactivate, `location.href = location.pathname` reloads the page, which resets the DOM to its original structure.

---

## What to Test

1. Enter compare mode → click "+" in the input bar → "Attach files" appears
2. Attach one or more files → filename chips show in the strip above the input
3. Type a prompt (or leave empty) and send → each pane's user bubble shows the filename chip(s); all panes receive and process the file
4. Follow-up prompt with no files → works normally (no attachments forwarded)
5. Follow-up prompt with new files → new files uploaded and forwarded; previous pane history preserved
6. Reroll a pane → uses the original session history, no file re-upload attempted
