"""Externalize inline base64 chat attachments to disk-backed blob files.

Inline base64 image/audio attachments blow up chat_messages (and its FTS
shadow tables) with megabytes of base64 per row — one uploaded multi-page PDF
can turn a single row into tens of MB. These helpers move the actual bytes to
disk on persist and reinline them on load, so the in-memory conversation (and
therefore the live LLM call and any resumed session) is unaffected but the DB
row/FTS index stays small.

Kept dependency-free of core.database / core.session_manager so it can be
imported from either without risking a circular import.
"""

import base64
import glob
import logging
import mimetypes
import os

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

BLOB_DIR = os.path.join(DATA_DIR, "chat_attachment_blobs")
BLOB_SCHEME = "odysseus-blob:"


def externalize_blobs(content: list, msg_id: str) -> list:
    """Replace inline base64 data URIs in image_url/audio blocks with a
    reference to a file written under BLOB_DIR. Non-matching blocks pass
    through unchanged."""
    result = []
    blob_idx = 0
    for blk in content:
        if not isinstance(blk, dict) or blk.get("type") not in ("image_url", "audio"):
            result.append(blk)
            continue
        key = blk["type"]
        url = (blk.get(key) or {}).get("url", "")
        if not (isinstance(url, str) and url.startswith("data:") and ";base64," in url):
            result.append(blk)
            continue
        header, b64data = url.split(";base64,", 1)
        mime = header[len("data:"):] or "application/octet-stream"
        try:
            os.makedirs(BLOB_DIR, exist_ok=True)
            ext = mimetypes.guess_extension(mime) or ""
            blob_name = f"{msg_id}_{blob_idx}{ext}"
            with open(os.path.join(BLOB_DIR, blob_name), "wb") as f:
                f.write(base64.b64decode(b64data))
            blk = {**blk, key: {"url": f"{BLOB_SCHEME}{blob_name}", "_mime": mime}}
            blob_idx += 1
        except Exception as e:
            logger.warning(f"Failed to externalize attachment blob for message {msg_id}: {e}")
        result.append(blk)
    return result


def reinline_blobs(content: list) -> list:
    """Reverse of externalize_blobs — turn a stored blob reference back into
    the full data URI so downstream code (LLM calls, history display) sees
    the same shape it would have before externalization."""
    result = []
    for blk in content:
        if not isinstance(blk, dict) or blk.get("type") not in ("image_url", "audio"):
            result.append(blk)
            continue
        key = blk["type"]
        inner = blk.get(key) or {}
        url = inner.get("url", "")
        if not (isinstance(url, str) and url.startswith(BLOB_SCHEME)):
            result.append(blk)
            continue
        blob_name = url[len(BLOB_SCHEME):]
        mime = inner.get("_mime", "application/octet-stream")
        try:
            with open(os.path.join(BLOB_DIR, blob_name), "rb") as f:
                b64data = base64.b64encode(f.read()).decode("ascii")
            blk = {**blk, key: {"url": f"data:{mime};base64,{b64data}"}}
        except Exception as e:
            logger.warning(f"Failed to reinline attachment blob {blob_name}: {e}")
            blk = {**blk, key: {"url": ""}}
        result.append(blk)
    return result


def cleanup_blobs_for_messages(msg_ids: list) -> None:
    """Delete blob files belonging to messages that are being deleted."""
    if not msg_ids or not os.path.isdir(BLOB_DIR):
        return
    for msg_id in msg_ids:
        for fname in glob.glob(os.path.join(BLOB_DIR, f"{msg_id}_*")):
            try:
                os.remove(fname)
            except OSError:
                pass
