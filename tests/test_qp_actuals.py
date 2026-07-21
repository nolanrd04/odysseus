"""TODO_B_NEW pairing infra: QpActual upsert, generations list/detail routes,
and the _diff_generation_vs_actual helper. Route handlers are pulled directly
off the router (same pattern as test_admin_wipe_gallery.py) against an
in-memory sqlite DB rather than a full FastAPI TestClient."""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as db_module
from core.database import Base, QpGeneration, QpActual
import routes.quick_proposal_routes as qpr
from routes.quick_proposal_routes import (
    setup_quick_proposal_routes,
    QpActualRequest,
    _diff_generation_vs_actual,
    _build_mapped_actual_values,
    _flatten_nested_fields_for_diff,
    _aggregate_actuals_reliability,
    _build_earthwork_dollar_fields,
    _matches_strip_haul_off,
    _build_paving_quantity_fields,
    _matches_road_paving,
    _matches_pathway_paving,
)


@pytest.fixture
def db_session_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(qpr, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


@pytest.fixture
def handlers():
    router = setup_quick_proposal_routes(session_manager=None)
    by_path = {}
    for r in router.routes:
        by_path.setdefault(r.path, []).append(r)
    return {
        "list": by_path["/api/quick_proposal/generations"][0].endpoint,
        "detail": by_path["/api/quick_proposal/generations/{run_id}"][0].endpoint,
        "upsert": by_path["/api/quick_proposal/actuals/{run_id}"][0].endpoint,
    }


def _seed_generation(db, run_id="run-1", gen_index=1, grand_total=100000.0, extracted=None, final_line_items=None):
    results_snapshot = {"extracted_values": extracted or {}}
    if final_line_items is not None:
        results_snapshot["extracted_data"] = {"final_line_items": final_line_items}
    db.add(QpGeneration(
        id=f"gen-{run_id}-{gen_index}",
        run_id=run_id,
        generation_index=gen_index,
        manager_model="claude-sonnet-5",
        gemini_model="gemini-3.1-pro-preview",
        grand_total=grand_total,
        results_snapshot=results_snapshot,
        messages_snapshot=[],
    ))
    db.commit()


# ── PUT /actuals/{run_id} — upsert ─────────────────────────────────────────────

def test_upsert_actual_inserts_then_updates_in_place(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(db)
    db.close()

    req1 = QpActualRequest(actual_total=95000.0, actual_values={"water_main_lf": 500}, notes="first pass")
    result1 = asyncio.run(handlers["upsert"](run_id="run-1", req=req1))
    assert result1["actual_total"] == 95000.0

    db = db_session_factory()
    assert db.query(QpActual).count() == 1
    first_updated_at = db.query(QpActual).filter(QpActual.run_id == "run-1").first().updated_at
    db.close()

    req2 = QpActualRequest(actual_total=98000.0, actual_values={"water_main_lf": 520}, notes="corrected")
    result2 = asyncio.run(handlers["upsert"](run_id="run-1", req=req2))
    assert result2["actual_total"] == 98000.0
    assert result2["actual_values"] == {"water_main_lf": 520}
    assert result2["notes"] == "corrected"

    db = db_session_factory()
    assert db.query(QpActual).count() == 1  # updated in place, not duplicated
    row = db.query(QpActual).filter(QpActual.run_id == "run-1").first()
    assert row.actual_total == 98000.0
    assert row.updated_at >= first_updated_at
    db.close()


# ── GET /generations, /generations/{run_id} ────────────────────────────────────

def test_list_generations_reports_actual_glance(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(db, run_id="run-1", grand_total=100000.0)
    _seed_generation(db, run_id="run-2", grand_total=50000.0)
    db.add(QpActual(id="act-1", run_id="run-1", actual_total=90000.0, actual_values={}))
    db.commit()
    db.close()

    result = asyncio.run(handlers["list"]())
    runs = {r["run_id"]: r for r in result["runs"]}
    assert runs["run-1"]["has_actual"] is True
    assert runs["run-1"]["actual_total"] == 90000.0
    assert runs["run-2"]["has_actual"] is False
    assert runs["run-2"]["actual_total"] is None


def test_generation_detail_includes_diff_when_actual_present(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(
        db, run_id="run-1", grand_total=100000.0,
        extracted={"water_main_LF": {"value": 500}, "sewer_main_LF": {"value": 2000}},
    )
    db.add(QpActual(
        id="act-1", run_id="run-1", actual_total=90000.0,
        actual_values={
            "line_items": [
                {"description": "8\" WATER MAIN PVC", "quantity": 450, "unit": "LF", "unit_price": 56.0, "ext_price": 25200.0},
                {"description": "8\" SEWER MAIN", "quantity": 2000, "unit": "LF", "unit_price": 31.5, "ext_price": 63000.0},
            ],
        },
    ))
    db.commit()
    db.close()

    result = asyncio.run(handlers["detail"](run_id="run-1"))
    assert result["run_id"] == "run-1"
    gen = result["generations"][0]
    assert gen["grand_total_delta"] == pytest.approx(10000.0)
    diff_by_field = {d["field"]: d for d in gen["diff"]}
    assert diff_by_field["water_main_LF"]["delta"] == 50
    assert diff_by_field["sewer_main_LF"]["delta"] == 0


def test_generation_detail_diff_is_none_without_actual(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(db, run_id="run-1")
    db.close()

    result = asyncio.run(handlers["detail"](run_id="run-1"))
    assert result["generations"][0]["diff"] is None
    assert result["actual"] is None


# ── _diff_generation_vs_actual unit cases ──────────────────────────────────────

def test_diff_matching_numeric_fields():
    diff = _diff_generation_vs_actual(
        {"ballast_tons": {"value": 110}}, {"ballast_tons": 100},
    )
    assert len(diff) == 1
    assert diff[0]["delta"] == 10
    assert diff[0]["delta_pct"] == pytest.approx(10.0)


def test_diff_field_only_in_proposal():
    diff = _diff_generation_vs_actual({"invented_item": {"value": 5000}}, {})
    assert diff[0]["field"] == "invented_item"
    assert diff[0]["actual_value"] is None
    assert diff[0]["delta"] is None


def test_diff_field_only_in_actual():
    diff = _diff_generation_vs_actual({}, {"missed_item": 2000})
    assert diff[0]["field"] == "missed_item"
    assert diff[0]["proposal_value"] is None
    assert diff[0]["delta"] is None


def test_diff_non_numeric_field_passthrough_no_delta():
    diff = _diff_generation_vs_actual(
        {"curb_type": {"value": "granite"}}, {"curb_type": "concrete"},
    )
    assert diff[0]["proposal_value"] == "granite"
    assert diff[0]["actual_value"] == "concrete"
    assert diff[0]["delta"] is None
    assert diff[0]["delta_pct"] is None


def test_diff_zero_actual_avoids_div_by_zero():
    diff = _diff_generation_vs_actual({"x": {"value": 10}}, {"x": 0})
    assert diff[0]["delta"] == 10
    assert diff[0]["delta_pct"] is None


def test_diff_sorted_by_abs_delta_descending():
    diff = _diff_generation_vs_actual(
        {"small": {"value": 101}, "big": {"value": 500}},
        {"small": 100, "big": 100},
    )
    assert [d["field"] for d in diff] == ["big", "small"]


# ── _build_mapped_actual_values (TODO_B_NEW-2) ─────────────────────────────────

_SOLARA_LINE_ITEMS = [
    {"description": "12\" WATER MAIN PVC", "quantity": 592, "unit": "LF", "unit_price": 146.75, "ext_price": 86876.0},
    {"description": "8\" WATER MAIN PVC", "quantity": 2299, "unit": "LF", "unit_price": 56.0, "ext_price": 128744.0},
    {"description": "15\" SEWER MAIN", "quantity": 312, "unit": "LF", "unit_price": 98.7, "ext_price": 30794.4},
    {"description": "8\" SEWER MAIN", "quantity": 2071, "unit": "LF", "unit_price": 31.5, "ext_price": 65236.5},
    {"description": "48\" SEWER MANHOLE", "quantity": 9, "unit": "EA", "unit_price": 4800.0, "ext_price": 43200.0},
    {"description": "60\" DROP MANHOLE", "quantity": 1, "unit": "EA", "unit_price": 13500.0, "ext_price": 13500.0},
    {"description": "FIRE HYDRANTS", "quantity": 4, "unit": "EA", "unit_price": 8800.0, "ext_price": 35200.0},
    {"description": "PED RAMPS", "quantity": 24, "unit": "EA", "unit_price": 2310.0, "ext_price": 55440.0},
    {"description": "SINGLE DRYWELLS", "quantity": 12, "unit": "EA", "unit_price": 3440.0, "ext_price": 41280.0},
    # A non-matching item confirms the matcher doesn't over-grab.
    {"description": "MOBILIZATION", "quantity": 1, "unit": "LS", "unit_price": 24000.0, "ext_price": 24000.0},
]


def test_mapper_sums_water_and_sewer_main_lf():
    mapped = _build_mapped_actual_values(
        {"water_main_LF": {"value": 2850}, "sewer_main_LF": {"value": 2071}}, _SOLARA_LINE_ITEMS,
    )
    assert mapped["water_main_LF"] == 592 + 2299
    assert mapped["sewer_main_LF"] == 312 + 2071


def test_mapper_only_maps_fields_present_on_extracted_side():
    # sewer_main_LF line items exist, but the field isn't present on this
    # generation — shouldn't be injected (nothing to compare it against).
    mapped = _build_mapped_actual_values({"water_main_LF": {"value": 1}}, _SOLARA_LINE_ITEMS)
    assert "sewer_main_LF" not in mapped
    assert "water_main_LF" in mapped


def test_mapper_flattens_manhole_count_via_flattener():
    extracted = {"proposed_manhole_count": {"value": {"total": 10, "count_48in": 9, "count_60in": 1}}}
    mapped = _build_mapped_actual_values(extracted, _SOLARA_LINE_ITEMS)
    assert mapped["proposed_manhole_count"] == 10  # 9 + 1

    flattened = _flatten_nested_fields_for_diff(extracted)
    assert flattened["proposed_manhole_count"] == {"value": 10}
    # Original untouched.
    assert extracted["proposed_manhole_count"]["value"]["total"] == 10


def test_mapper_fire_hydrant_and_ped_ramp():
    mapped = _build_mapped_actual_values(
        {"proposed_fire_hydrant_count": {"value": 7}, "proposed_ped_ramp_count": {"value": 16}},
        _SOLARA_LINE_ITEMS,
    )
    assert mapped["proposed_fire_hydrant_count"] == 4
    assert mapped["proposed_ped_ramp_count"] == 24


def test_mapper_scupper_synonym_group_canonicalizes_onto_scupper_count():
    line_items = _SOLARA_LINE_ITEMS + [
        {"description": "SCUPPERS", "quantity": 4, "unit": "EA", "unit_price": 180.0, "ext_price": 720.0},
    ]
    for field in ("scupper_count", "scuppers_count"):
        mapped = _build_mapped_actual_values({field: {"value": 3}}, line_items)
        assert mapped == {"scupper_count": 4}


def test_mapper_scupper_scope_items_fallback_when_no_dedicated_field():
    # No scupper_count/scuppers_count field at all — most generations only
    # record this via a scope_items entry (gemini_phase3.txt's own example
    # schema), so the mapper must still pick it up and canonicalize it.
    line_items = _SOLARA_LINE_ITEMS + [
        {"description": "SCUPPERS", "quantity": 4, "unit": "EA", "unit_price": 180.0, "ext_price": 720.0},
    ]
    extracted_values = {
        "scope_items": {"value": [
            {"work_type": "SCUPPERS / CURB CUTS", "quantity": 6, "unit": "EA"},
        ]},
    }
    mapped = _build_mapped_actual_values(extracted_values, line_items)
    assert mapped == {"scupper_count": 4}


def test_mapper_scupper_scope_items_null_quantity_is_not_signal():
    # A scope_items entry can flag scuppers are in scope without a firm count
    # yet (quantity=None) — that's not enough to trigger the actual-side sum.
    extracted_values = {
        "scope_items": {"value": [
            {"work_type": "SCUPPERS", "quantity": None, "unit": "EA"},
        ]},
    }
    mapped = _build_mapped_actual_values(extracted_values, _SOLARA_LINE_ITEMS)
    assert "scupper_count" not in mapped


def test_flatten_canonicalizes_scupper_shapes_onto_scupper_count():
    from routes.quick_proposal_routes import _flatten_nested_fields_for_diff

    for extracted_values in (
        {"scupper_count": {"value": 3}},
        {"scuppers_count": {"value": 3}},
        {"scope_items": {"value": [{"work_type": "SCUPPERS", "quantity": 3, "unit": "EA"}]}},
    ):
        flattened = _flatten_nested_fields_for_diff(extracted_values)
        assert flattened["scupper_count"] == {"value": 3}
        assert "scuppers_count" not in flattened


def test_mapper_drywell_synonym_group_picks_whichever_field_is_present():
    for field in ("drywell_count", "drywell_count_final", "proposed_drywell_count"):
        mapped = _build_mapped_actual_values({field: {"value": 10}}, _SOLARA_LINE_ITEMS)
        assert mapped[field] == 12  # single-drywell total, no doubles in this fixture


def test_mapper_single_vs_double_drywell_stay_distinct():
    line_items = _SOLARA_LINE_ITEMS + [
        {"description": "DOUBLE DRYWELLS", "quantity": 3, "unit": "EA", "unit_price": 5000.0, "ext_price": 15000.0},
    ]
    mapped = _build_mapped_actual_values(
        {"single_drywell_count": {"value": 12}, "double_drywell_count": {"value": 3}}, line_items,
    )
    assert mapped["single_drywell_count"] == 12
    assert mapped["double_drywell_count"] == 3


def test_mapper_unmapped_field_not_injected():
    # No matcher exists for an arbitrary field name — should never appear.
    mapped = _build_mapped_actual_values({"curb_type": {"value": "rolled"}}, _SOLARA_LINE_ITEMS)
    assert "curb_type" not in mapped


# ── _aggregate_actuals_reliability ──────────────────────────────────────────────

def test_reliability_aggregates_across_paired_runs(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(db, run_id="run-1", extracted={"water_main_LF": {"value": 550}})
    db.add(QpActual(id="act-1", run_id="run-1", actual_total=1.0, actual_values={
        "line_items": [{"description": "8\" WATER MAIN", "quantity": 500, "unit": "LF", "unit_price": 1, "ext_price": 1}],
    }))
    _seed_generation(db, run_id="run-2", extracted={"water_main_LF": {"value": 480}})
    db.add(QpActual(id="act-2", run_id="run-2", actual_total=1.0, actual_values={
        "line_items": [{"description": "8\" WATER MAIN", "quantity": 500, "unit": "LF", "unit_price": 1, "ext_price": 1}],
    }))
    db.commit()

    report = _aggregate_actuals_reliability(db)
    db.close()

    by_field = {r["field"]: r for r in report}
    wm = by_field["water_main_LF"]
    assert wm["sample_count"] == 2
    assert wm["mean_delta_pct"] == pytest.approx(((550 - 500) / 500 * 100 + (480 - 500) / 500 * 100) / 2)
    assert wm["over_count"] == 1
    assert wm["under_count"] == 1


def test_reliability_uses_latest_generation_per_run(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(db, run_id="run-1", gen_index=1, extracted={"water_main_LF": {"value": 999}})
    _seed_generation(db, run_id="run-1", gen_index=2, extracted={"water_main_LF": {"value": 500}})
    db.add(QpActual(id="act-1", run_id="run-1", actual_total=1.0, actual_values={
        "line_items": [{"description": "8\" WATER MAIN", "quantity": 500, "unit": "LF", "unit_price": 1, "ext_price": 1}],
    }))
    db.commit()

    report = _aggregate_actuals_reliability(db)
    db.close()

    by_field = {r["field"]: r for r in report}
    assert by_field["water_main_LF"]["sample_count"] == 1
    assert by_field["water_main_LF"]["mean_delta_pct"] == pytest.approx(0.0)  # generation 2 (latest) matches exactly


def test_reliability_skips_runs_with_no_generation(db_session_factory, handlers):
    db = db_session_factory()
    db.add(QpActual(id="act-1", run_id="run-with-no-gen", actual_total=1.0, actual_values={
        "line_items": [{"description": "8\" WATER MAIN", "quantity": 500, "unit": "LF", "unit_price": 1, "ext_price": 1}],
    }))
    db.commit()

    report = _aggregate_actuals_reliability(db)
    db.close()
    assert report == []


# ── _build_earthwork_dollar_fields (TODO_B_NEW-2 follow-up) ───────────────────

_ACTUAL_EARTHWORK_LINE_ITEMS = [
    {"description": "STRIP ROW & HAUL OFF TOPSOIL", "quantity": 1, "unit": "LS", "unit_price": 29800.0, "ext_price": 29800.0},
    {"description": "EXC TO EMBANK INCL IMPORT", "quantity": 1, "unit": "LS", "unit_price": 51800.0, "ext_price": 51800.0},
    {"description": "SUBGRADE ROAD", "quantity": 1, "unit": "LS", "unit_price": 4950.0, "ext_price": 4950.0},
    {"description": "12\" BALLAST", "quantity": 6200, "unit": "SY", "unit_price": 12.5, "ext_price": 77500.0},
    {"description": "SIGNS & STRIPING, NO HWY STRIPING", "quantity": 1, "unit": "LS", "unit_price": 18303.0, "ext_price": 18303.0},
]

_PROPOSED_EARTHWORK_LINE_ITEMS = [
    {"description": "STRIP ROW TO STOCKPILE", "unit": "CY", "qty": 1627.1, "unit_price": 2.86, "ext_price": 4653.5},
    {"description": "EXC TO EMBANK", "unit": "LS", "qty": 1, "unit_price": 12500.0, "ext_price": 12500.0},
    {"description": "SUBGRADE ROAD", "unit": "SY", "qty": 2767.4, "unit_price": 1.43, "ext_price": 3957.4},
    {"description": "BALLAST (UNCONFIRMED)", "unit": "SY", "qty": 2440.7, "unit_price": 12.22, "ext_price": 29833.35},
]


def test_strip_haul_off_matcher_excludes_striping():
    assert _matches_strip_haul_off("SIGNS & STRIPING, NO HWY STRIPING") is False
    assert _matches_strip_haul_off("STRIP ROW & HAUL OFF TOPSOIL") is True
    assert _matches_strip_haul_off("STRIPPING EXISTING VEGITATION") is True
    # HAUL alone (no topsoil) shouldn't match — that's a demo/debris haul-off, not earthwork.
    assert _matches_strip_haul_off("DEMO A/C, FENCE, TREES, INCL HAUL OFF") is False
    # HAUL + TOPSOIL without the word STRIP still counts.
    assert _matches_strip_haul_off("OPTIONAL HAUL OFF TOSPOIL") is True


def test_earthwork_dollar_fields_matches_all_four_categories():
    proposed, actual = _build_earthwork_dollar_fields(_PROPOSED_EARTHWORK_LINE_ITEMS, _ACTUAL_EARTHWORK_LINE_ITEMS)
    assert actual["earthwork_strip_haul_off_dollars"] == 29800.0
    assert proposed["earthwork_strip_haul_off_dollars"] == 4653.5
    assert actual["earthwork_exc_to_embank_dollars"] == 51800.0
    assert proposed["earthwork_exc_to_embank_dollars"] == 12500.0
    assert actual["earthwork_subgrade_road_dollars"] == 4950.0
    assert proposed["earthwork_subgrade_road_dollars"] == 3957.4
    assert actual["earthwork_ballast_dollars"] == 77500.0
    assert proposed["earthwork_ballast_dollars"] == 29833.35


def test_earthwork_dollar_fields_empty_when_final_line_items_missing():
    # Older QpGeneration snapshots have no final_line_items at all — must not
    # be treated as a proposed $0 (a false -100% signal), just skipped.
    proposed, actual = _build_earthwork_dollar_fields([], _ACTUAL_EARTHWORK_LINE_ITEMS)
    assert proposed == {}
    assert actual == {}


def test_earthwork_dollar_fields_skips_category_with_no_actual_match():
    line_items_no_ballast = [li for li in _ACTUAL_EARTHWORK_LINE_ITEMS if "BALLAST" not in li["description"]]
    proposed, actual = _build_earthwork_dollar_fields(_PROPOSED_EARTHWORK_LINE_ITEMS, line_items_no_ballast)
    assert "earthwork_ballast_dollars" not in actual
    assert "earthwork_ballast_dollars" not in proposed


def test_generation_detail_diff_includes_earthwork_dollar_fields(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(
        db, run_id="run-1", extracted={},
        final_line_items=_PROPOSED_EARTHWORK_LINE_ITEMS,
    )
    db.add(QpActual(
        id="act-1", run_id="run-1", actual_total=1.0,
        actual_values={"line_items": _ACTUAL_EARTHWORK_LINE_ITEMS},
    ))
    db.commit()
    db.close()

    result = asyncio.run(handlers["detail"](run_id="run-1"))
    diff_by_field = {d["field"]: d for d in result["generations"][0]["diff"]}
    assert diff_by_field["earthwork_exc_to_embank_dollars"]["proposal_value"] == 12500.0
    assert diff_by_field["earthwork_exc_to_embank_dollars"]["actual_value"] == 51800.0


def test_reliability_skips_earthwork_when_final_line_items_absent(db_session_factory, handlers):
    # Regression: without the final_line_items guard, a missing itemized
    # breakdown used to be read as a proposed $0 for every earthwork
    # category, dragging the reliability report to a fabricated -100%.
    db = db_session_factory()
    _seed_generation(db, run_id="run-1", extracted={})  # no final_line_items
    db.add(QpActual(id="act-1", run_id="run-1", actual_total=1.0, actual_values={
        "line_items": _ACTUAL_EARTHWORK_LINE_ITEMS,
    }))
    db.commit()

    report = _aggregate_actuals_reliability(db)
    db.close()
    assert report == []


# ── _build_paving_quantity_fields (TODO_B_NEW-2 follow-up) ────────────────────

_ACTUAL_PAVING_LINE_ITEMS = [
    {"description": "3/6\" PAVING", "quantity": 2721, "unit": "SY", "unit_price": 31.0, "ext_price": 84351.0},
    {"description": "2/4\" PATHWAY PAVING", "quantity": 876, "unit": "SY", "unit_price": 40.0, "ext_price": 35040.0},
    {"description": "3/6\" PAVEMENT PATCHING", "quantity": 477, "unit": "SY", "unit_price": 75.0, "ext_price": 35775.0},
]

_PROPOSED_PAVING_LINE_ITEMS = [
    {"description": "3/6\" PAVING (Access Road)", "unit": "SY", "qty": 2440.7, "unit_price": 31.75, "ext_price": 77492.22},
    {"description": "2/6\" PATHWAY PAVING (Hwy 53 frontage)", "unit": "SY", "qty": 875.7, "unit_price": 43.5, "ext_price": 38092.65},
]


def test_paving_matchers_separate_road_pathway_and_exclude_patching():
    assert _matches_road_paving("3/6\" PAVING") is True
    assert _matches_road_paving("2/4\" PATHWAY PAVING") is False
    assert _matches_pathway_paving("2/4\" PATHWAY PAVING") is True
    assert _matches_pathway_paving("2/4\" PAVING pathway") is True  # lowercase variant seen in real data
    assert _matches_pathway_paving("3/6\" PAVING") is False
    # "PAVEMENT PATCHING" doesn't contain the substring "PAVING" at all —
    # naturally excluded from both categories without a separate patch check.
    assert _matches_road_paving("3/6\" PAVEMENT PATCHING") is False
    assert _matches_pathway_paving("3/6\" PAVEMENT PATCHING") is False


def test_paving_quantity_fields_matches_road_and_pathway_sy():
    proposed, actual = _build_paving_quantity_fields(_PROPOSED_PAVING_LINE_ITEMS, _ACTUAL_PAVING_LINE_ITEMS)
    assert actual["road_paving_SY"] == 2721
    assert proposed["road_paving_SY"] == 2440.7
    assert actual["pathway_paving_SY"] == 876
    assert proposed["pathway_paving_SY"] == 875.7


def test_paving_quantity_fields_ignores_non_sy_units():
    line_items = _ACTUAL_PAVING_LINE_ITEMS + [
        {"description": "2\" PAVING", "quantity": 500, "unit": "TN", "unit_price": 150.0, "ext_price": 75000.0},
    ]
    proposed, actual = _build_paving_quantity_fields(_PROPOSED_PAVING_LINE_ITEMS, line_items)
    assert actual["road_paving_SY"] == 2721  # TN item not folded in


def test_paving_quantity_fields_empty_when_final_line_items_missing():
    proposed, actual = _build_paving_quantity_fields([], _ACTUAL_PAVING_LINE_ITEMS)
    assert proposed == {}
    assert actual == {}


def test_generation_detail_diff_includes_paving_quantity_fields(db_session_factory, handlers):
    db = db_session_factory()
    _seed_generation(
        db, run_id="run-1", extracted={},
        final_line_items=_PROPOSED_PAVING_LINE_ITEMS,
    )
    db.add(QpActual(
        id="act-1", run_id="run-1", actual_total=1.0,
        actual_values={"line_items": _ACTUAL_PAVING_LINE_ITEMS},
    ))
    db.commit()
    db.close()

    result = asyncio.run(handlers["detail"](run_id="run-1"))
    diff_by_field = {d["field"]: d for d in result["generations"][0]["diff"]}
    assert diff_by_field["road_paving_SY"]["proposal_value"] == 2440.7
    assert diff_by_field["road_paving_SY"]["actual_value"] == 2721
    assert diff_by_field["pathway_paving_SY"]["proposal_value"] == 875.7
    assert diff_by_field["pathway_paving_SY"]["actual_value"] == 876


# ── _auto_pair_actual — TODO_B_NEW-2 auto-pairing on generation end ────────────

def _fake_actuals_match():
    return {
        "job_number": "99999",
        "job_name": "TEST JOB",
        "grand_total": 123456.78,
        "line_items": [{"description": "X", "quantity": 1, "unit": "LS", "unit_price": 1.0, "ext_price": 1.0}],
        "source_file": "99999_TEST_JOB.txt",
    }


@pytest.fixture
def auto_pair_env(monkeypatch, db_session_factory):
    # _auto_pair_actual (like _save_generation_snapshot) imports SessionLocal
    # fresh from core.database at call time rather than through the qpr
    # module global, so the fixture's isolated in-memory engine has to be
    # patched onto core.database directly too, not just qpr.SessionLocal.
    monkeypatch.setattr(db_module, "SessionLocal", db_session_factory)
    return db_session_factory


def test_auto_pair_inserts_when_match_found(auto_pair_env, monkeypatch):
    db = auto_pair_env()
    monkeypatch.setattr(qpr, "_load_run_meta", lambda run_id: {"run_name": "TEST JOB"})
    monkeypatch.setattr(qpr.actuals_matcher, "find_actual_match", lambda names: _fake_actuals_match())

    qpr._auto_pair_actual("run-x", {"extracted_values": {}})

    row = db.query(QpActual).filter(QpActual.run_id == "run-x").first()
    assert row is not None
    assert row.actual_total == 123456.78
    assert row.actual_values["job_name"] == "TEST JOB"
    assert row.actual_values["job_number"] == "99999"
    assert "Auto-paired" in row.notes


def test_auto_pair_skips_when_actual_already_exists(auto_pair_env, monkeypatch):
    db = auto_pair_env()
    db.add(QpActual(id="act-existing", run_id="run-x", actual_total=1.0, actual_values={}))
    db.commit()

    called = {"hit": False}

    def _should_not_be_called(names):
        called["hit"] = True
        return _fake_actuals_match()

    monkeypatch.setattr(qpr, "_load_run_meta", lambda run_id: {"run_name": "TEST JOB"})
    monkeypatch.setattr(qpr.actuals_matcher, "find_actual_match", _should_not_be_called)

    qpr._auto_pair_actual("run-x", {"extracted_values": {}})

    assert called["hit"] is False
    rows = db.query(QpActual).filter(QpActual.run_id == "run-x").all()
    assert len(rows) == 1
    assert rows[0].actual_total == 1.0  # untouched, not overwritten by the auto-match


def test_auto_pair_noop_when_no_match(auto_pair_env, monkeypatch):
    db = auto_pair_env()
    monkeypatch.setattr(qpr, "_load_run_meta", lambda run_id: {"run_name": "SOMETHING ELSE"})
    monkeypatch.setattr(qpr.actuals_matcher, "find_actual_match", lambda names: None)

    qpr._auto_pair_actual("run-x", {"extracted_values": {}})

    assert db.query(QpActual).filter(QpActual.run_id == "run-x").first() is None


def test_auto_pair_passes_extracted_job_name_as_a_match_candidate(auto_pair_env, monkeypatch):
    auto_pair_env()
    monkeypatch.setattr(qpr, "_load_run_meta", lambda run_id: {"run_name": ""})
    seen = {}

    def _capture(names):
        seen["names"] = names
        return None

    monkeypatch.setattr(qpr.actuals_matcher, "find_actual_match", _capture)

    qpr._auto_pair_actual("run-x", {"extracted_values": {"job_name": {"value": "HOLECEK VALLEYWAY"}}})

    assert "HOLECEK VALLEYWAY" in seen["names"]
