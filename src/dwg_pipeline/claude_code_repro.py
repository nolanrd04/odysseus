"""
Claude-Code-native reproduction folder for the DWG extraction flow (session
2026_8_3.3, item 9/12 — "Top priority #2").

Provisions a REAL job folder using the exact code the live app uses
(`provision_dwg_job()` / `build_dwg_system_prompt()`) — no hand-reconstructed
scratch copy, no agent turn, no LLM call. Writes a `CLAUDE.md` into the job
folder holding the live system prompt as project context (opening Claude
Code there auto-loads it), plus two local stand-ins for the tools that
prompt references but Claude Code doesn't have natively:

  - `corpus_lookup.py` — calls the real DwgCorpusLookupTool._dispatch() code,
    so results are byte-identical to the live `dwg_corpus_lookup` tool
    (holdout applied the same way).
  - `run_python.py` — replicates PythonTool.execute()'s sandbox preamble
    (fresh `python -I` subprocess per call, doc/msp/dq auto-loaded) so the
    statelessness behavior under test matches production, not just a bare
    `python -c` which would silently skip the preload.

Runs entirely on the user's Claude subscription via Claude Code, not
Odysseus's metered Anthropic API key.

Usage:
    # From a real DWG file — runs the actual ODA conversion + census, same
    # as a live upload.
    python -m src.dwg_pipeline.claude_code_repro --dwg path/to/file.dwg [more.dwg ...]

    # From an existing corpus job — reuses cached DXFs (no ODA needed),
    # self-holdout applied automatically so the model can't read its own
    # answer key via corpus_lookup.py.
    python -m src.dwg_pipeline.claude_code_repro --job WOODMAN
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from src.constants import DWG_JOBS_DIR
from src.dwg_pipeline.context import build_dwg_system_prompt
from src.dwg_pipeline.jobs import DwgJobContext, provision_dwg_job

APP_ROOT = Path(__file__).resolve().parent.parent.parent

_START_MESSAGE = (
    "Extract a quantity takeoff sheet from the attached DWG file(s). Present "
    "the final result as a qty_tbl markdown table, followed by the Case A "
    "(possible extras) and Case B (possible misses) flag lists."
)

_CORPUS_LOOKUP_TEMPLATE = '''#!/usr/bin/env python
"""
Stand-in for the live `dwg_corpus_lookup` tool inside this Claude Code
reproduction folder. Calls the real DwgCorpusLookupTool._dispatch() code
from the Odysseus app root, so results are byte-identical to what the live
app would return for this job (including its holdout_job.txt exclusion,
if this folder has one).

Usage:
    python corpus_lookup.py jobs
    python corpus_lookup.py vocabulary
    python corpus_lookup.py fingerprints
    python corpus_lookup.py fingerprint <job>
    python corpus_lookup.py qty_tbl <job>
"""
import json
import os
import sys

APP_ROOT = __APP_ROOT__
sys.path.insert(0, APP_ROOT)

import src.dwg_pipeline.sandbox as _sandbox

_JOB_DIR = os.path.dirname(os.path.abspath(__file__))
_sandbox.active_dwg_job_dir = lambda: _JOB_DIR  # this folder IS the job dir

from src.agent_tools.dwg_tools import DwgCorpusLookupTool


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "jobs"
    job = sys.argv[2] if len(sys.argv) > 2 else ""
    result = DwgCorpusLookupTool()._dispatch(mode, job)
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
'''

_RUN_PYTHON_TEMPLATE = '''#!/usr/bin/env python
"""
Stand-in for the live `python` tool inside this Claude Code reproduction
folder. Replicates PythonTool.execute() (src/agent_tools/subprocess_tools.py)
exactly: your code runs in a brand-new `python -I` subprocess — nothing you
define persists between calls except `doc`, `msp`, and `dq`, which are
auto-loaded fresh every time via the same preamble the live app prepends
(src/dwg_pipeline/sandbox.py: build_sandbox_preamble()).

Use this instead of running `python`/`python3` directly — a bare python
call here would silently skip the preload and NOT reproduce the
statelessness behavior this harness exists to test.

Usage:
    python run_python.py -c "your code here"
    python run_python.py script.py
"""
import os
import subprocess
import sys

APP_ROOT = __APP_ROOT__
sys.path.insert(0, APP_ROOT)

from src.dwg_pipeline.sandbox import build_sandbox_preamble

JOB_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if sys.argv[1] == "-c":
        if len(sys.argv) < 3:
            raise SystemExit("run_python.py -c requires a code argument")
        code = sys.argv[2]
    else:
        code = open(sys.argv[1], encoding="utf-8").read()

    content = build_sandbox_preamble(JOB_DIR) + "\\n" + code
    proc = subprocess.run([sys.executable, "-I", "-c", content], cwd=JOB_DIR)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
'''

_HARNESS_NOTE_TEMPLATE = """# Claude-Code-native DWG reproduction

This folder is a faithful, offline reproduction of one Odysseus DWG-extraction
turn — provisioned by the same code the live app uses
(`provision_dwg_job()` / `build_dwg_system_prompt()` in
`src/dwg_pipeline/jobs.py` / `context.py`). No agent turn has run and no LLM
has been called yet. Everything below the divider is the exact system-prompt
text the live app would attach to this job's turn — two of the tools it
references aren't real Claude Code tools; use these local stand-ins instead
so results stay byte-identical to production:

| Live-app tool | In this folder, use instead |
|---|---|
| `python` | `python run_python.py -c "<code>"` (or a `.py` file). Fresh, isolated `python -I` subprocess per call, `doc`/`msp`/`dq` auto-loaded — do **not** run bare `python`/`python3`, it skips the preload and breaks statelessness fidelity. |
| `dwg_corpus_lookup` | `python corpus_lookup.py <mode> [job]` — calls the real tool's dispatch code directly, same holdout exclusion. |

**`bash` (arbitrary shell) is fully disabled in the live app for DWG turns —
there is no shell access in production at all**, only the two tools above
plus general file tools (list/glob/read a file). Claude Code has no separate
"python tool" of its own, so its Bash tool is unavoidably the *mechanism*
used here to invoke `run_python.py` / `corpus_lookup.py` — but each live-app
tool call is one discrete, separately-counted round. To keep round counts
comparable to production:
  - Run exactly ONE `run_python.py` or `corpus_lookup.py` invocation per
    Bash call — do not chain it with `cd`, `ls`, `echo`, `&&`, pipes, or
    other commands in the same call. This folder is already your working
    directory; no `cd` is needed.
  - Use Claude Code's native Read/Glob/Grep tools (not Bash) for listing or
    reading files — those map to the live app's still-enabled `ls`/`glob`/
    `read_file` tools, which is a known, separately-tracked inefficiency
    (see session 2026_8_3.3 item 3) — reproduce it as-is, don't self-restrict
    further or work around it by hand.

Start the run with the same message the live app's DWG entry point sends:

> {start_message}

---

"""


def _job_from_corpus(job_query: str) -> DwgJobContext:
    from src.dwg_pipeline.build_corpus import CORPUS_DIR
    from src.dwg_pipeline.census import census_job

    records = json.loads((CORPUS_DIR / "qty_tbl_records.json").read_text(encoding="utf-8"))
    matches = [r for r in records if job_query.lower() in r["job_folder"].lower()]
    if not matches:
        raise SystemExit(f"no corpus job matches {job_query!r} (see corpus_lookup.py jobs mode)")
    job_folder = matches[0]["job_folder"]
    dxf_dir = CORPUS_DIR / "dxf_cache" / job_folder
    dxfs = sorted(dxf_dir.glob("*.dxf"))
    if not dxfs:
        raise SystemExit(
            f"no cached DXFs for {job_folder} — run `python -m src.dwg_pipeline.build_corpus` first"
        )

    job_id = f"{time.strftime('%Y%m%d_%H%M%S')}_repro_{job_folder[:32]}"
    job_dir = Path(DWG_JOBS_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    for p in dxfs:
        shutil.copy2(p, job_dir / p.name)
    # This job IS the corpus job under test — hold out its own answer key,
    # same rule provision_dwg_job()/run_eval.py apply for self-tests.
    (job_dir / "holdout_job.txt").write_text(job_folder, encoding="utf-8")
    original_filenames = [p.name for p in dxfs]
    (job_dir / "original_filenames.json").write_text(
        json.dumps(original_filenames, indent=1), encoding="utf-8"
    )

    census = census_job(sorted(job_dir.glob("*.dxf")))
    (job_dir / "census.json").write_text(json.dumps(census, indent=1), encoding="utf-8")

    return DwgJobContext(
        job_dir=str(job_dir),
        job_id=job_id,
        dxf_files=original_filenames,
        census=census,
        newly_provisioned=True,
        holdout_job=job_folder,
        original_filenames=original_filenames,
    )


def _write_stand_ins(job_dir: Path) -> None:
    app_root_literal = repr(str(APP_ROOT))
    (job_dir / "corpus_lookup.py").write_text(
        _CORPUS_LOOKUP_TEMPLATE.replace("__APP_ROOT__", app_root_literal), encoding="utf-8"
    )
    (job_dir / "run_python.py").write_text(
        _RUN_PYTHON_TEMPLATE.replace("__APP_ROOT__", app_root_literal), encoding="utf-8"
    )


def _write_claude_md(job_dir: Path, ctx: DwgJobContext, forced: bool) -> None:
    system_prompt = build_dwg_system_prompt(ctx.census, ctx.job_dir, ctx.dxf_files, forced=forced)
    note = _HARNESS_NOTE_TEMPLATE.format(start_message=_START_MESSAGE)
    (job_dir / "CLAUDE.md").write_text(note + system_prompt + "\n", encoding="utf-8")


def _regen(job_dir_arg: str, forced: bool) -> DwgJobContext:
    """Reload an already-provisioned repro folder's census/dxf/holdout and
    rewrite CLAUDE.md + stand-ins in place — for iterating on DWG_RULES.md /
    the prompt templates without re-running conversion or census."""
    job_dir = Path(job_dir_arg).resolve()
    census_path = job_dir / "census.json"
    if not census_path.exists():
        raise SystemExit(f"{job_dir} has no census.json — not a provisioned job folder")
    census = json.loads(census_path.read_text(encoding="utf-8"))
    dxf_files = sorted(p.name for p in job_dir.glob("*.dxf"))
    holdout = ""
    if (job_dir / "holdout_job.txt").exists():
        holdout = (job_dir / "holdout_job.txt").read_text(encoding="utf-8").strip()
    return DwgJobContext(
        job_dir=str(job_dir), job_id=job_dir.name, dxf_files=dxf_files,
        census=census, holdout_job=holdout,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--dwg", nargs="+", help="one or more real DWG files to convert (runs ODA + census)"
    )
    src.add_argument(
        "--job",
        help="corpus job-folder substring to reproduce from cached DXFs (self-holdout applied)",
    )
    src.add_argument(
        "--regen",
        metavar="JOB_DIR",
        help="rewrite CLAUDE.md/stand-ins for an already-provisioned repro folder in place "
        "(picks up DWG_RULES.md/prompt-template edits without re-converting/re-censusing)",
    )
    ap.add_argument(
        "--holdout",
        default="",
        help="[--dwg mode only] corpus job substring to exclude from dwg_corpus_lookup, "
        "if this DWG is itself a corpus job",
    )
    ap.add_argument(
        "--not-forced",
        action="store_true",
        help="use the attached-not-forced intent text instead of the sidebar-forced one",
    )
    args = ap.parse_args()

    if args.regen:
        ctx = _regen(args.regen, forced=not args.not_forced)
    elif args.job:
        ctx = _job_from_corpus(args.job)
    else:
        dwg_paths = [str(Path(p).resolve()) for p in args.dwg]
        ctx = provision_dwg_job(
            dwg_paths,
            session_id=None,
            holdout_job=args.holdout,
            original_filenames=[Path(p).name for p in dwg_paths],
        )
        if ctx.errors:
            for e in ctx.errors:
                print(f"WARNING: {e}", file=sys.stderr)
        if not ctx.dxf_files:
            raise SystemExit("no DXF produced — see warnings above")

    job_dir = Path(ctx.job_dir)
    _write_stand_ins(job_dir)
    _write_claude_md(job_dir, ctx, forced=not args.not_forced)

    print(f"Reproduction folder ready: {job_dir}")
    print(f"  DXF file(s): {', '.join(ctx.dxf_files)}")
    if ctx.holdout_job:
        print(f"  Holdout: {ctx.holdout_job!r} excluded from dwg_corpus_lookup")
    print()
    print("Next steps:")
    print(f'  1. cd "{job_dir}"')
    print("  2. claude   (opens Claude Code — CLAUDE.md auto-loads the DWG system prompt)")
    print("  3. Paste this as your first message:")
    print(f"     {_START_MESSAGE}")


if __name__ == "__main__":
    main()