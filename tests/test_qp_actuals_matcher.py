"""src/quick_proposal/actuals_matcher.py — Terra-format actuals parsing and
name-matching used by both the live auto-pair-on-generation-end hook and
scripts/backfill_qp_actuals.py (TODO_B_NEW-2 follow-up)."""
import pytest

from src.quick_proposal import actuals_matcher as am

_SAMPLE_TXT = """\
JOB NUMBER: 25901
JOB NAME:   VALLEY WAY LONG PLAT
ESTIMATE #: 25043 - Valleyway Long Plat
DATE:       07/11/2025
VERSION:    single version
SOURCE:     25901_VALLEY_WAY_LONG_PLAT/Proposal.pdf

============================================================
LINE ITEMS
============================================================
DESCRIPTION                                              QUAN UNIT   UNIT PRICE      EXT PRICE
------------------------------------------------------------
EXC TO EMBANK ROW                                           1 LS     $13,000.00     $13,000.00
TYPE C CURB                                               723 LF         $24.00     $17,352.00
------------------------------------------------------------
TAX (8.9%)                                                                        $2,672.13

============================================================
TOTALS
============================================================
ITEM COUNT                                                                                2
SUM OF LINE ITEMS                                                                $30,352.00
GRAND TOTAL                                                                      $33,024.13

============================================================
OPTIONAL / ALTERNATE ITEMS (listed after Grand Total; NOT included in totals above)
============================================================
OPTIONAL HAUL OFF TOPSOIL                               1,600 CY         $17.00     $27,200.00
"""


def test_parse_actuals_file_extracts_header_and_totals(tmp_path):
    f = tmp_path / "25901_VALLEY_WAY_LONG_PLAT.txt"
    f.write_text(_SAMPLE_TXT, encoding="utf-8")

    parsed = am.parse_actuals_file(f)

    assert parsed["job_number"] == "25901"
    assert parsed["job_name"] == "VALLEY WAY LONG PLAT"
    assert parsed["grand_total"] == 33024.13
    assert parsed["source_file"] == f.name


def test_parse_actuals_file_excludes_optional_section_line_items(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text(_SAMPLE_TXT, encoding="utf-8")

    parsed = am.parse_actuals_file(f)

    descriptions = [li["description"] for li in parsed["line_items"]]
    assert "EXC TO EMBANK ROW" in descriptions
    assert "TYPE C CURB" in descriptions
    assert "OPTIONAL HAUL OFF TOPSOIL" not in descriptions
    assert len(parsed["line_items"]) == 2


def test_parse_actuals_file_line_item_shape(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text(_SAMPLE_TXT, encoding="utf-8")

    parsed = am.parse_actuals_file(f)
    curb = next(li for li in parsed["line_items"] if li["description"] == "TYPE C CURB")

    assert curb == {
        "description": "TYPE C CURB",
        "quantity": 723.0,
        "unit": "LF",
        "unit_price": 24.0,
        "ext_price": 17352.0,
    }


def test_parse_actuals_file_returns_none_for_missing_fields(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("not a real actuals file\n", encoding="utf-8")

    assert am.parse_actuals_file(f) is None


def test_list_parsed_actuals_returns_empty_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(am, "ACTUALS_DIR", tmp_path / "does_not_exist")
    assert am.list_parsed_actuals() == []


def test_normalize_job_name_strips_punctuation_and_case():
    assert am.normalize_job_name("Valley Way, Long Plat!") == "VALLEYWAYLONGPLAT"
    assert am.normalize_job_name(None) == ""


@pytest.mark.parametrize("candidates,expect_match", [
    (["HOLECEK VALLEYWAY LONG PLAT"], True),   # extracted job_name superset-contains the actual's name
    (["VALLEY WAY BASELINE"], False),          # run_name alone doesn't share the full job-name substring
    (["Totally Unrelated Job"], False),
    ([], False),
])
def test_find_actual_match_is_conservative_substring_only(monkeypatch, tmp_path, candidates, expect_match):
    f = tmp_path / "25901_VALLEY_WAY_LONG_PLAT.txt"
    f.write_text(_SAMPLE_TXT, encoding="utf-8")
    monkeypatch.setattr(am, "ACTUALS_DIR", tmp_path)

    match = am.find_actual_match(candidates)

    if expect_match:
        assert match is not None
        assert match["job_number"] == "25901"
    else:
        assert match is None


def test_build_actual_values_shape_matches_existing_db_rows(tmp_path, monkeypatch):
    f = tmp_path / "sample.txt"
    f.write_text(_SAMPLE_TXT, encoding="utf-8")
    parsed = am.parse_actuals_file(f)

    actual_values = am.build_actual_values(parsed)

    assert set(actual_values.keys()) == {"job_number", "job_name", "line_items"}
    assert actual_values["job_number"] == "25901"
    assert actual_values["job_name"] == "VALLEY WAY LONG PLAT"
