"""
DWG -> DXF conversion via the ODA File Converter CLI.

Validated across all 4 jobs studied so far (Woodman, Settlement MT, Raghorn,
Kildere Meadows) using ODAFileConverter 27.1.0, targeting ACAD2018 DXF. The
converter operates on FOLDERS, not single files, so this wrapper stages the
input DWG into an isolated temp folder before invoking it, to avoid the
converter touching sibling files in the job's real dwg_files/ directory.

Cross-platform as of 2026-07-29 (DQ-1): the Linux QT6 build of the same
27.1.0 converter runs headlessly under Xvfb, given the xcb/Qt runtime libs
listed in the repo Dockerfile -- confirmed by reproducing all 12
validate_against_kildere.py checks from a DXF converted entirely inside the
odysseus Docker container. On Linux the converter has no GUI-suppression
flag equivalent to Windows, so it must be wrapped in `xvfb-run`.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TARGET_VERSION = "ACAD2018"


def _default_oda_exe() -> Path:
    if platform.system() == "Windows":
        return Path(r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe")
    # Linux: installed by the repo Dockerfile via the .deb package, on PATH
    # as a bare command name rather than a fixed absolute path.
    return Path("ODAFileConverter")


DEFAULT_ODA_EXE = _default_oda_exe()


def _oda_exe_available(oda_exe: Path) -> bool:
    if oda_exe.is_absolute():
        return oda_exe.exists()
    return shutil.which(str(oda_exe)) is not None


def convert_dwg_to_dxf(
    dwg_path: Path | str,
    out_dir: Path | str,
    oda_exe: Path | str = DEFAULT_ODA_EXE,
    target_version: str = DEFAULT_TARGET_VERSION,
    timeout: int = 120,
) -> Path:
    """
    Convert a single DWG file to DXF and return the path to the resulting file.

    Stages `dwg_path` into a fresh temp input folder (so the converter's
    folder-based invocation can't pick up or clobber unrelated files),
    then runs:
        ODAFileConverter.exe <in_folder> <out_dir> <target_version> DXF 0 1

    Raises FileNotFoundError if the ODA executable or input DWG don't exist,
    RuntimeError if conversion completes but no output DXF is produced
    (the converter reports success via exit code even on some failures,
    so output-file existence is the real check).
    """
    dwg_path = Path(dwg_path)
    out_dir = Path(out_dir)
    oda_exe = Path(oda_exe)

    if not _oda_exe_available(oda_exe):
        raise FileNotFoundError(f"ODA File Converter not found at {oda_exe}")
    if not dwg_path.exists():
        raise FileNotFoundError(f"Input DWG not found at {dwg_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dwg_qty_convert_") as tmp:
        staged = Path(tmp) / dwg_path.name
        shutil.copy2(dwg_path, staged)

        cmd = [str(oda_exe), tmp, str(out_dir), target_version, "DXF", "0", "1"]
        # No GUI-suppression flag on the Linux build (unlike Windows, where
        # the converter never shows a window) -- it needs a real or virtual
        # X display to initialize its Qt platform plugin at all.
        if platform.system() != "Windows":
            cmd = ["xvfb-run", "-a"] + cmd

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        expected_out = out_dir / (dwg_path.stem + ".dxf")
        if not expected_out.exists():
            raise RuntimeError(
                f"ODA File Converter did not produce {expected_out}. "
                f"exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        return expected_out


def convert_job_dwgs(
    dwg_dir: Path | str,
    out_dir: Path | str,
    oda_exe: Path | str = DEFAULT_ODA_EXE,
    target_version: str = DEFAULT_TARGET_VERSION,
) -> list[Path]:
    """Convert every *.dwg in `dwg_dir` (non-recursive) to DXF in `out_dir`."""
    dwg_dir = Path(dwg_dir)
    return [
        convert_dwg_to_dxf(p, out_dir, oda_exe, target_version)
        for p in sorted(dwg_dir.glob("*.dwg"))
    ]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("Usage: python -m dwg_qty.convert <dwg_path_or_dir> <out_dir>")
        raise SystemExit(1)

    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    if src.is_dir():
        results = convert_job_dwgs(src, out)
    else:
        results = [convert_dwg_to_dxf(src, out)]
    for r in results:
        print(r)
