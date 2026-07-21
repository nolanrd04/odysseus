"""TODO_ZZ: the manager-facing create_job tool (phase-6 continuation chat only).
Exercises _qp_create_job directly against an in-memory SQLite DB, mirroring the
fixture pattern in test_qp_case_library_crud.py."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.quick_proposal_routes as qpr
from core.database import Base
from src.quick_proposal import case_store


def _run(coro):
    return asyncio.run(coro)


def _index(final_line_items=None, extracted_values=None):
    return SimpleNamespace(
        extracted_data={"final_line_items": final_line_items or []},
        extracted_values=extracted_values or {},
    )


@pytest.fixture
def case_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(qpr, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(qpr, "_run_grand_total", lambda run_id: 50000.0)
    db = TestSessionLocal()
    try:
        # Seed one historical job so category auto-fill has something to match against.
        db.add(case_store.build_job_rows("alpha_ranch", {
            "schema_version": "1.0",
            "job_name": "Alpha Ranch",
            "identity": {"client": "Sample Builders LLC"},
            "classification": {"job_type": "subdivision_road"},
            "primary_proposal_index": 0,
            "proposals": [{"grand_total": 1000.0, "line_items": [
                {"description": "8\" SEWER MAIN", "category": "SEWER", "qty": 1, "unit": "LF", "ext_price": 1000.0},
            ]}],
        }))
        db.commit()
    finally:
        db.close()
    return TestSessionLocal


def test_create_job_happy_path(case_db):
    index = _index(
        final_line_items=[
            {"description": "8\" SEWER MAIN", "unit": "LF", "qty": 500, "unit_price": 45.0,
             "tax_rate": 0.089, "ext_price": 500 * 45.0 * 1.089},
            {"description": "MOBILIZATION", "unit": "LS", "qty": 1, "unit_price": 10000.0,
             "tax_rate": 0, "ext_price": 10000.0},
        ],
        extracted_values={
            "lot_count": {"value": 42},
            # Gemini's real road_LF schema is a per-road array, not a scalar — this is the
            # shape that caused the original "float() argument ... not 'list'" DB crash.
            "road_LF": {"value": [
                {"road_name": "Martini Ln", "length": 700.0, "road_type": "new_construction"},
                {"road_name": "Wilhelm Way", "length": 500.5, "road_type": "new_construction"},
            ]},
        },
    )
    result = _run(qpr._qp_create_job("run_123", index, {
        "job_name": "New Test Job",
        "client": "Acme Developers",
        "job_type": "subdivision_road",
    }))
    assert "added to the case library" in result
    assert "2 line item" in result

    db = case_db()
    try:
        job = case_store.get_job(db, "new_test_job")
        assert job is not None
        content = case_store.reassemble(job)
        assert content["identity"]["client"] == "Acme Developers"
        assert content["derived"]["scale_metrics"]["lot_count"] == 42
        assert content["derived"]["scale_metrics"]["road_LF"] == pytest.approx(1200.5)
        li = {i["description"]: i for i in content["proposals"][0]["line_items"]}
        # Category auto-filled from the historical Alpha Ranch job's matching description.
        assert li["8\" SEWER MAIN"]["category"] == "SEWER"
        assert li["8\" SEWER MAIN"]["tax_rate"] == pytest.approx(0.089)
        assert li["MOBILIZATION"]["category"] == "UNCATEGORIZED"
        assert content["proposals"][0]["grand_total"] == 50000.0
    finally:
        db.close()


def test_create_job_line_item_overrides(case_db):
    index = _index(final_line_items=[
        {"description": "TRAFFIC CONTROL", "unit": "LS", "qty": 1, "unit_price": 500.0,
         "tax_rate": 0, "ext_price": 500.0},
    ])
    result = _run(qpr._qp_create_job("run_456", index, {
        "job_name": "Override Job",
        "client": "Acme Developers",
        "job_type": "subdivision_road",
        "line_item_overrides": [
            {"description": "TRAFFIC CONTROL", "category": "TRAFFIC", "is_optional": True},
        ],
    }))
    assert "added to the case library" in result

    db = case_db()
    try:
        content = case_store.reassemble(case_store.get_job(db, "override_job"))
        item = content["proposals"][0]["line_items"][0]
        assert item["category"] == "TRAFFIC"
        assert item["is_optional"] is True
    finally:
        db.close()


def test_create_job_missing_required_fields(case_db):
    index = _index(final_line_items=[{"description": "X", "unit": "LS", "qty": 1,
                                       "unit_price": 1.0, "tax_rate": 0, "ext_price": 1.0}])
    result = _run(qpr._qp_create_job("run_789", index, {"job_name": "No Client Job"}))
    assert result.startswith("Error:")
    assert "job_name, client, and job_type" in result


def test_create_job_no_priced_line_items(case_db):
    index = _index(final_line_items=[])
    result = _run(qpr._qp_create_job("run_000", index, {
        "job_name": "Empty Job", "client": "Acme", "job_type": "subdivision_road",
    }))
    assert result.startswith("Error:")
    assert "no final_line_items" in result


def test_create_job_skips_non_numeric_scale_fields(case_db):
    """road_subgrade_SY/road_paving_SY/etc. are only ever computed narratively during
    Phase B, not written back to the index as clean scalars — a stray list/dict/string
    shape must be skipped (and noted in the result), never crash the DB write. ROW_SF
    has no road_LF/ROW_width_ft to compute from here, so it's skipped too."""
    index = _index(
        final_line_items=[{"description": "X", "unit": "LS", "qty": 1, "unit_price": 1.0,
                            "tax_rate": 0, "ext_price": 1.0}],
        extracted_values={
            "lot_count": {"value": 10},
            "road_paving_SY": {"value": "unknown"},
        },
    )
    result = _run(qpr._qp_create_job("run_skip", index, {
        "job_name": "Skip Scale Job", "client": "Acme", "job_type": "subdivision_road",
    }))
    assert "added to the case library" in result
    assert "ROW_SF" in result and "road_paving_SY" in result

    db = case_db()
    try:
        content = case_store.reassemble(case_store.get_job(db, "skip_scale_job"))
        scale = content["derived"]["scale_metrics"]
        assert scale["lot_count"] == 10
        assert "ROW_SF" not in scale
        assert "road_paving_SY" not in scale
    finally:
        db.close()


def test_create_job_computes_row_sf_from_road_lf_and_widths(case_db):
    """ROW_SF = sum(road length x matching typical-section width), halving any road
    tagged improvement_to_existing/fronting_existing per the same 'Fronting Road
    Adjustments' convention system_prompt.txt uses during Phase B pricing."""
    index = _index(
        final_line_items=[{"description": "X", "unit": "LS", "qty": 1, "unit_price": 1.0,
                            "tax_rate": 0, "ext_price": 1.0}],
        extracted_values={
            "road_LF": {"value": [
                {"road_name": "Martini Ln", "length": 700.0, "road_type": "new_construction"},
                {"road_name": "Wilhelm Way", "length": 500.0, "road_type": "improvement_to_existing"},
            ]},
            "ROW_width_ft": {"value": [
                {"roads": ["Martini Ln", "Wilhelm Way"], "width": 60},
            ]},
        },
    )
    result = _run(qpr._qp_create_job("run_rowsf", index, {
        "job_name": "ROW SF Job", "client": "Acme", "job_type": "subdivision_road",
    }))
    assert "added to the case library" in result

    db = case_db()
    try:
        scale = case_store.reassemble(case_store.get_job(db, "row_sf_job"))["derived"]["scale_metrics"]
        # 700*60 (full) + 500*60/2 (halved, fronting) = 42000 + 15000 = 57000
        assert scale["ROW_SF"] == pytest.approx(57000.0)
    finally:
        db.close()


def test_create_job_row_sf_flags_unmatched_road(case_db):
    index = _index(
        final_line_items=[{"description": "X", "unit": "LS", "qty": 1, "unit_price": 1.0,
                            "tax_rate": 0, "ext_price": 1.0}],
        extracted_values={
            "road_LF": {"value": [
                {"road_name": "Martini Ln", "length": 700.0, "road_type": "new_construction"},
                {"road_name": "No Width Rd", "length": 300.0, "road_type": "new_construction"},
            ]},
            "ROW_width_ft": {"value": [{"roads": ["Martini Ln"], "width": 60}]},
        },
    )
    result = _run(qpr._qp_create_job("run_unmatched", index, {
        "job_name": "Unmatched Road Job", "client": "Acme", "job_type": "subdivision_road",
    }))
    assert "added to the case library" in result
    assert "No Width Rd" in result
    assert "undercount" in result

    db = case_db()
    try:
        scale = case_store.reassemble(case_store.get_job(db, "unmatched_road_job"))["derived"]["scale_metrics"]
        assert scale["ROW_SF"] == pytest.approx(700.0 * 60)
    finally:
        db.close()


def test_create_job_proposal_date_defaults_to_today(case_db):
    index = _index(final_line_items=[{"description": "X", "unit": "LS", "qty": 1,
                                       "unit_price": 1.0, "tax_rate": 0, "ext_price": 1.0}])
    _run(qpr._qp_create_job("run_date", index, {
        "job_name": "Date Default Job", "client": "Acme", "job_type": "subdivision_road",
    }))
    db = case_db()
    try:
        content = case_store.reassemble(case_store.get_job(db, "date_default_job"))
        assert content["proposals"][0]["proposal_date"] == time.strftime("%Y-%m-%d")
    finally:
        db.close()


def test_create_job_proposal_date_override(case_db):
    index = _index(final_line_items=[{"description": "X", "unit": "LS", "qty": 1,
                                       "unit_price": 1.0, "tax_rate": 0, "ext_price": 1.0}])
    _run(qpr._qp_create_job("run_date2", index, {
        "job_name": "Date Override Job", "client": "Acme", "job_type": "subdivision_road",
        "proposal_date": "2024-03-15",
    }))
    db = case_db()
    try:
        content = case_store.reassemble(case_store.get_job(db, "date_override_job"))
        assert content["proposals"][0]["proposal_date"] == "2024-03-15"
    finally:
        db.close()


def test_create_job_rejects_duplicate_job_name(case_db):
    index = _index(final_line_items=[{"description": "X", "unit": "LS", "qty": 1,
                                       "unit_price": 1.0, "tax_rate": 0, "ext_price": 1.0}])
    result = _run(qpr._qp_create_job("run_dup", index, {
        "job_name": "Alpha Ranch", "client": "Someone Else", "job_type": "subdivision_road",
    }))
    assert result.startswith("Error:")


# --- _qp_suggest_job_type: project_type -> job_type hint (job_type vocabulary mismatch) ---

def test_suggest_job_type_unambiguous_residential():
    index = _index(extracted_values={"project_type": {"value": "residential_subdivision"}})
    suggestion, project_type = qpr._qp_suggest_job_type(index)
    assert suggestion == "subdivision_road"
    assert project_type == "residential_subdivision"


def test_suggest_job_type_unambiguous_commercial():
    index = _index(extracted_values={"project_type": {"value": "commercial_development"}})
    suggestion, project_type = qpr._qp_suggest_job_type(index)
    assert suggestion == "commercial_site"
    assert project_type == "commercial_development"


def test_suggest_job_type_ambiguous_rural_falls_back():
    """rural_access has no case-library equivalent — no suggestion, but the raw
    project_type is still surfaced so the tool description can name it."""
    index = _index(extracted_values={"project_type": {"value": "rural_access"}})
    suggestion, project_type = qpr._qp_suggest_job_type(index)
    assert suggestion is None
    assert project_type == "rural_access"


def test_suggest_job_type_ambiguous_mixed_falls_back():
    """'mixed' (pipeline) is deliberately not auto-mapped to 'mixed_use' (case-library)
    — the two vocabularies don't reliably mean the same thing."""
    index = _index(extracted_values={"project_type": {"value": "mixed"}})
    suggestion, project_type = qpr._qp_suggest_job_type(index)
    assert suggestion is None
    assert project_type == "mixed"


def test_suggest_job_type_no_project_type_extracted():
    index = _index(extracted_values={})
    suggestion, project_type = qpr._qp_suggest_job_type(index)
    assert suggestion is None
    assert project_type is None
