"""
Sandboxing for LLM-written python during DWG extraction turns (DQ-5).

Two layers, both keyed off the active workspace being a DWG job folder
(under DATA_DIR/dwg_jobs) — no parameter threading needed, the existing
per-turn workspace contextvar is the single source of truth:

  1. A `sys.addaudithook` preamble prepended to the script content the
     `python` tool executes: live filesystem confinement to the job folder
     (reads additionally allowed from the interpreter/app install, so imports
     and `inspect.getsource(dwg_qty...)` keep working), plus a block on child
     processes and network sockets, neither of which the audit hook could
     otherwise see through. Interpreter-level and cooperative by design —
     not an OS jail (a `ctypes` escape is out of scope; the tool is already
     admin-gated, so the threat model is an admin's own honest mistake).

  2. A Windows Job Object wrapping the subprocess: kernel-level memory /
     active-process caps with kill-on-close, matching the native Windows
     Server production deployment. No-op on non-Windows hosts (the Docker
     dev path already has container-level bounds).

`bash` is excluded from the DWG flow entirely rather than sandboxed
(disabled per-turn in chat_routes, refused here as belt-and-braces).
"""

from __future__ import annotations

import logging
import os
import sys

from src.constants import DWG_JOBS_DIR

logger = logging.getLogger(__name__)

# Resource caps for the Job Object (generous — the cap is a runaway backstop,
# not a working limit; Kildere's full DXF parse peaks well under 1 GiB).
JOB_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB across the job
ACTIVE_PROCESS_LIMIT = 4


def active_dwg_job_dir() -> str | None:
    """The current turn's DWG job folder, or None when this isn't a DWG turn.

    True exactly when the active workspace (set once per turn by
    execute_tool_block) resolves inside DATA_DIR/dwg_jobs.
    """
    from src.tool_execution import get_active_workspace

    ws = get_active_workspace()
    if not ws:
        return None
    root = os.path.normcase(os.path.realpath(DWG_JOBS_DIR))
    resolved = os.path.normcase(os.path.realpath(ws))
    try:
        if os.path.commonpath([resolved, root]) == root:
            return os.path.realpath(ws)
    except ValueError:  # different drives / mixed forms — outside
        pass
    return None


def build_sandbox_preamble(job_dir: str) -> str:
    """Python source prepended to the model's script under `python -I`.

    Also restores the app root on sys.path (isolated mode strips it) so
    `from src.dwg_qty... import ...` works inside the sandboxed script.
    """
    app_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    # repr() gives safely-quoted Windows paths.
    return f'''\
import os as _sb_os, sys as _sb_sys
_sb_sys.path.insert(0, {app_root!r})
_SB_JOB = _sb_os.path.normcase(_sb_os.path.realpath({job_dir!r}))
# ezdxf reads user-level config on import, and BUILDS a font cache there the
# first time it's missing (a fresh container/venv never has one yet) — that
# first run needs a WRITE, not just a read, or `import ezdxf` itself throws.
_SB_EZDXF_ROOTS = tuple(_sb_os.path.normcase(_sb_os.path.realpath(p)) for p in (
    _sb_os.path.expanduser(_sb_os.path.join("~", ".config", "ezdxf")),
    _sb_os.path.expanduser(_sb_os.path.join("~", ".cache", "ezdxf")),
))
_SB_READ_ROOTS = tuple(_sb_os.path.normcase(_sb_os.path.realpath(p)) for p in (
    _SB_JOB,
    {app_root!r},                     # app code: dwg_qty imports, inspect.getsource
    _sb_sys.prefix, _sb_sys.exec_prefix,  # stdlib + venv site-packages
    _sb_sys.base_prefix, _sb_sys.base_exec_prefix,
)) + _SB_EZDXF_ROOTS
_SB_WRITE_ROOTS = (_SB_JOB,) + _SB_EZDXF_ROOTS
def _sb_inside(path, roots):
    try:
        p = _sb_os.path.normcase(_sb_os.path.realpath(_sb_os.fspath(path)))
    except (TypeError, ValueError):
        return True  # fd-based / non-path opens: not a path escape
    for r in roots:
        try:
            if _sb_os.path.commonpath([p, r]) == r:
                return True
        except ValueError:
            continue
    return False
def _sb_hook(event, args):
    if event == "open":
        path, mode = args[0], args[1]
        if path is None or isinstance(path, int):
            return
        writing = bool(mode) and any(c in str(mode) for c in "wax+")
        if writing:
            if not _sb_inside(path, _SB_WRITE_ROOTS):
                raise RuntimeError(
                    f"DWG sandbox: write blocked outside the job folder: {{path!r}} "
                    f"(writes are confined to {{_SB_JOB}})")
        elif not _sb_inside(path, _SB_READ_ROOTS):
            # Only block reads of files that actually exist: libraries probe
            # optional config paths on import (e.g. ~/.config/ezdxf/ezdxf.ini)
            # and a missing file should just FileNotFoundError as normal.
            try:
                exists = _sb_os.path.exists(path)
            except (TypeError, ValueError):
                exists = False
            if exists:
                raise RuntimeError(
                    f"DWG sandbox: read blocked outside the job folder / app install: {{path!r}}")
    elif event in ("os.remove", "os.rmdir", "os.rename", "os.truncate", "shutil.rmtree", "shutil.move"):
        for candidate in args[:2]:
            if candidate is None or isinstance(candidate, int):
                continue
            if not _sb_inside(candidate, (_SB_JOB,)):
                raise RuntimeError(
                    f"DWG sandbox: {{event}} blocked outside the job folder: {{candidate!r}}")
    elif event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec"):
        raise RuntimeError(
            "DWG sandbox: child processes are blocked in DWG extraction turns "
            "(conversion already ran server-side; work from the DXF with ezdxf/dwg_qty)")
    elif event == "socket.connect":
        raise RuntimeError("DWG sandbox: network access is blocked in DWG extraction turns")
_sb_sys.addaudithook(_sb_hook)
# Auto-load convenience (DQ-11 follow-up): every `python` call is a brand-new
# process (see PythonTool.execute — plain `python -I -c <script>`), so a
# script that assumes an earlier call's `doc`/`msp`/`dq` still exist NameErrors
# and burns a whole round. Live testing hit this repeatedly even after
# DWG_RULES.md was told to warn against it — a prose reminder doesn't reliably
# stop the mistake, so make it structurally impossible instead: pre-load the
# job's DXF(s) and the dwg_qty module into every script's globals, matching
# the sys.path restoration above. Failures are caught and reported to stderr
# rather than aborting the script, so a call that doesn't need geometry still
# runs even if e.g. a DXF is transiently unreadable.
import glob as _sb_glob
dq = None
doc = None
msp = None
docs = {{}}
mspaces = {{}}
try:
    import src.dwg_qty as dq
except Exception as _sb_e:
    print(f"DWG sandbox: src.dwg_qty auto-import failed: {{_sb_e}}", file=_sb_sys.stderr)
try:
    import ezdxf
    _sb_dxf_files = sorted(_sb_glob.glob("*.dxf"))
    if len(_sb_dxf_files) == 1:
        doc = ezdxf.readfile(_sb_dxf_files[0])
        msp = doc.modelspace()
    elif len(_sb_dxf_files) > 1:
        for _sb_f in _sb_dxf_files:
            docs[_sb_f] = ezdxf.readfile(_sb_f)
            mspaces[_sb_f] = docs[_sb_f].modelspace()
except Exception as _sb_e:
    print(f"DWG sandbox: DXF auto-load failed: {{_sb_e}}", file=_sb_sys.stderr)
'''


def assign_job_object(pid: int) -> None:
    """Wrap `pid` in a Windows Job Object with memory/process caps and
    kill-on-close. Best-effort: failures are logged, never fatal (the audit
    hook and wall-clock timeout still apply). No-op off Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
        JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        hjob = kernel32.CreateJobObjectW(None, None)
        if not hjob:
            raise ctypes.WinError(ctypes.get_last_error())

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.BasicLimitInformation.ActiveProcessLimit = ACTIVE_PROCESS_LIMIT
        info.JobMemoryLimit = JOB_MEMORY_LIMIT_BYTES
        if not kernel32.SetInformationJobObject(
            hjob, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(hjob, hproc):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(hproc)
        # hjob handle is deliberately NOT closed here: kill-on-close ties the
        # job's lifetime to this (parent) process, so a leaked runaway child
        # dies with the server rather than surviving it. The handle itself is
        # reclaimed when this process exits.
        logger.info("DWG sandbox: pid %s assigned to Job Object (mem=%dMB, procs=%d)",
                    pid, JOB_MEMORY_LIMIT_BYTES // (1024 * 1024), ACTIVE_PROCESS_LIMIT)
    except Exception:
        logger.warning("DWG sandbox: Job Object assignment failed for pid %s", pid, exc_info=True)
