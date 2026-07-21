"""Round-trip fidelity for the normalized DB-backed case library (TODO_YY).

Shreds every shipped case-library JSON into the five tables and asserts the
reassembled dict is *semantically* equal to the original: missing keys are
treated the same as null values (consumers use ``d.get(k)``), and numbers are
compared as floats so SQLite int/float type-affinity coercion doesn't count as a
difference. This is the safety net that lets normalization be lossless in practice.
"""

import glob
import json
import math
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, QpJobData
from src.quick_proposal.case_store import build_job_rows, reassemble, load_all_records

CASE_DIR = Path(__file__).resolve().parent.parent / "src" / "quick_proposal" / "case_library"
CASE_FILES = sorted(glob.glob(str(CASE_DIR / "*.json")))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _semantic_eq(a, b, path=""):
    """Deep semantic equality: null == absent, int == float, recursive.
    Returns (ok: bool, first_diff: str)."""
    # Numbers (bool is a subclass of int — keep it separate)
    a_num = isinstance(a, (int, float)) and not isinstance(a, bool)
    b_num = isinstance(b, (int, float)) and not isinstance(b, bool)
    if a_num and b_num:
        if math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9):
            return True, ""
        return False, f"{path}: number {a!r} != {b!r}"
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        for k in keys:
            av, bv = a.get(k), b.get(k)
            # null == absent
            if (k not in a or av is None) and (k not in b or bv is None):
                continue
            ok, diff = _semantic_eq(av, bv, f"{path}.{k}")
            if not ok:
                return False, diff
        return True, ""
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False, f"{path}: list len {len(a)} != {len(b)}"
        for i, (av, bv) in enumerate(zip(a, b)):
            ok, diff = _semantic_eq(av, bv, f"{path}[{i}]")
            if not ok:
                return False, diff
        return True, ""
    if a is None or b is None:
        # one side null, other absent-with-default already handled at dict level;
        # here a bare None vs a value is a real difference
        if a is None and b is None:
            return True, ""
        return False, f"{path}: {a!r} != {b!r}"
    if a == b:
        return True, ""
    return False, f"{path}: {a!r} != {b!r}"


def test_case_files_exist():
    assert CASE_FILES, "no case-library JSON files found to test against"


@pytest.mark.parametrize("path", CASE_FILES, ids=[Path(p).stem for p in CASE_FILES])
def test_roundtrip_per_file(session, path):
    original = json.loads(Path(path).read_text())
    slug = Path(path).stem
    session.add(build_job_rows(slug, original))
    session.commit()
    session.expire_all()  # force a real reload from the DB, not the identity map
    job = session.get(QpJobData, slug)
    got = reassemble(job)
    ok, diff = _semantic_eq(original, got)
    assert ok, f"{slug}: round-trip diff at {diff}"


def test_load_all_records_returns_every_job(session):
    for path in CASE_FILES:
        session.add(build_job_rows(Path(path).stem, json.loads(Path(path).read_text())))
    session.commit()
    session.expire_all()
    records = load_all_records(session)
    assert len(records) == len(CASE_FILES)
    names = {r.get("job_name") for r in records}
    assert all(names)  # no empty job_names
