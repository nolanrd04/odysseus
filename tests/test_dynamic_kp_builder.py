"""TODO_WW part 2: dynamic knowledge-pack generation
(src/quick_proposal/knowledge_pack/derive_patterns.py) — include-list scoped
load_cases, build_kp_for_jobs output shape, and the manual_rules.json overlay
that carries hand-authored rules (e.g. scuppers_from_curb_LF) into every
generated pack."""
import json

import pytest

from src.quick_proposal.knowledge_pack.derive_patterns import (
    CASE_LIBRARY_DIR,
    MANUAL_RULES_PATH,
    _merge_manual_rules,
    build_kp_for_jobs,
    load_cases,
)


def _all_case_dicts():
    return [json.loads(f.read_text()) for f in sorted(CASE_LIBRARY_DIR.glob("*.json"))]


def _sample_job_names(n):
    names = []
    for f in sorted(CASE_LIBRARY_DIR.glob("*.json")):
        names.append(json.loads(f.read_text())["job_name"])
        if len(names) == n:
            break
    return names


def test_load_cases_include_filter_case_insensitive():
    names = _sample_job_names(2)
    cases = load_cases(include_jobs=[names[0].upper(), names[1].lower()])
    assert sorted(c["job_name"] for c in cases) == sorted(names)


def test_load_cases_include_none_returns_all():
    all_cases = load_cases()
    assert len(all_cases) == len(list(CASE_LIBRARY_DIR.glob("*.json")))


def test_build_kp_for_jobs_scoped_pack(tmp_path):
    names = _sample_job_names(3)
    kp_path = build_kp_for_jobs(names, tmp_path)
    assert kp_path == tmp_path / "knowledge_pack.json"
    pack = json.loads(kp_path.read_text())
    assert pack["stats"]["total_jobs"] == 3
    # every expected section is present
    for key in ("unit_price_distributions", "derivation_rules", "item_prevalence",
                "earthwork_balance_prior", "market_context"):
        assert key in pack
    # human-readable review is written alongside
    assert (tmp_path / "patterns_review.md").stat().st_size > 0


def test_build_kp_from_in_memory_cases_matches_files(tmp_path):
    """TODO_YY: the app now passes DB-loaded records via `cases=` instead of
    letting the builder read the JSON files. Same selection → identical pack."""
    names = _sample_job_names(3)
    from_files = json.loads(build_kp_for_jobs(names, tmp_path / "a").read_text())
    from_cases = json.loads(
        build_kp_for_jobs(names, tmp_path / "b", cases=_all_case_dicts()).read_text())
    # `built_at` timestamps are equal within a run; compare the derived content.
    from_files.pop("built_at", None); from_cases.pop("built_at", None)
    assert from_files == from_cases


def test_build_kp_cases_filters_to_selection(tmp_path):
    names = _sample_job_names(2)
    pack = json.loads(
        build_kp_for_jobs(names, tmp_path, cases=_all_case_dicts()).read_text())
    assert pack["stats"]["total_jobs"] == 2


def test_build_kp_carries_manual_rules(tmp_path):
    assert MANUAL_RULES_PATH.exists(), "manual_rules.json missing from knowledge_pack dir"
    manual_names = {r["name"] for r in
                    json.loads(MANUAL_RULES_PATH.read_text())["derivation_rules"]}
    assert "scuppers_from_curb_LF" in manual_names
    kp_path = build_kp_for_jobs(_sample_job_names(2), tmp_path)
    pack = json.loads(kp_path.read_text())
    by_name = {r["name"]: r for r in pack["derivation_rules"]}
    for name in manual_names:
        assert name in by_name, f"manual rule {name} missing from generated pack"
        assert by_name[name].get("manual") is True


def test_merge_manual_rules_no_duplicates():
    pack = {"derivation_rules": [{"name": "scuppers_from_curb_LF", "formula": "existing"}]}
    _merge_manual_rules(pack)
    names = [r["name"] for r in pack["derivation_rules"]]
    assert names.count("scuppers_from_curb_LF") == 1
    # the pre-existing entry wins
    assert pack["derivation_rules"][0]["formula"] == "existing"


def test_build_kp_for_jobs_unknown_selection_raises(tmp_path):
    with pytest.raises(ValueError):
        build_kp_for_jobs(["No Such Job Anywhere"], tmp_path)
