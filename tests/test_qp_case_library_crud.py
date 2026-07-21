"""TODO_WW part 1 / TODO_YY: case-library CRUD routes (list/get/create/update/
delete) plus slug sanitization and duplicate-job_name protection. Now DB-backed
(five normalized tables via src/quick_proposal/case_store.py). Route handlers are
pulled directly off the router (same pattern as test_qp_actuals.py) against an
in-memory SQLite DB."""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.quick_proposal_routes as qpr
from routes.quick_proposal_routes import (
    setup_quick_proposal_routes,
    CaseLibraryUpsert,
)
from core.database import Base, QpJobData
from src.quick_proposal import case_store


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def case_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(qpr, "SessionLocal", TestSessionLocal)
    db = TestSessionLocal()
    try:
        db.add(case_store.build_job_rows("alpha_ranch", {
            "schema_version": "1.0",
            "job_name": "Alpha Ranch",
            "identity": {"client": "Sample Builders LLC"},
            "classification": {"job_type": "subdivision_road"},
            "primary_proposal_index": 0,
            "proposals": [{"grand_total": 1000.0, "line_items": [
                {"description": "MOBILIZATION", "qty": 1, "unit": "LS", "ext_price": 1000.0},
            ]}],
        }))
        db.add(case_store.build_job_rows("beta_creek", {
            "job_name": "Beta Creek",
            "proposals": [],
        }))
        db.commit()
    finally:
        db.close()
    return TestSessionLocal


@pytest.fixture
def handlers():
    router = setup_quick_proposal_routes(session_manager=None)
    out = {}
    for r in router.routes:
        for method in getattr(r, "methods", []) or []:
            out[(method, r.path)] = r.endpoint
    base = "/api/quick_proposal/case_library"
    return {
        "list":       out[("GET", base)],
        "line_items": out[("GET", base + "/line_items")],
        "get":        out[("GET", base + "/{slug}")],
        "create":     out[("POST", base)],
        "update":     out[("PUT", base + "/{slug}")],
        "delete":     out[("DELETE", base + "/{slug}")],
    }


def test_list_returns_summaries(case_db, handlers):
    rows = _run(handlers["list"]())
    assert [r["slug"] for r in rows] == ["alpha_ranch", "beta_creek"]
    alpha = rows[0]
    assert alpha["job_name"] == "Alpha Ranch"
    assert alpha["client"] == "Sample Builders LLC"
    assert alpha["job_type"] == "subdivision_road"
    assert alpha["grand_total"] == 1000.0
    assert alpha["line_item_count"] == 1
    beta = rows[1]
    assert beta["proposal_count"] == 0
    assert beta["grand_total"] is None


def test_get_full_record_and_404(case_db, handlers):
    got = _run(handlers["get"]("alpha_ranch"))
    assert got["slug"] == "alpha_ranch"
    assert got["content"]["job_name"] == "Alpha Ranch"
    assert got["content"]["identity"]["client"] == "Sample Builders LLC"
    with pytest.raises(HTTPException) as e:
        _run(handlers["get"]("nope"))
    assert e.value.status_code == 404


@pytest.mark.parametrize("bad", ["../evil", "a/b", "UPPER", "", ".hidden", "a b"])
def test_slug_sanitization(case_db, handlers, bad):
    with pytest.raises(HTTPException) as e:
        _run(handlers["get"](bad))
    assert e.value.status_code == 400


def test_create_auto_slug_and_read_back(case_db, handlers):
    res = _run(handlers["create"](CaseLibraryUpsert(
        content={"job_name": "Gamma Fields PH2", "proposals": []})))
    assert res["slug"] == "gamma_fields_ph2"
    got = _run(handlers["get"]("gamma_fields_ph2"))
    assert got["content"]["job_name"] == "Gamma Fields PH2"


def test_create_rejects_existing_slug_and_duplicate_job_name(case_db, handlers):
    with pytest.raises(HTTPException) as e:
        _run(handlers["create"](CaseLibraryUpsert(
            content={"job_name": "Whatever"}, slug="alpha_ranch")))
    assert e.value.status_code == 409
    # same job_name under a different slug is also rejected (case-insensitive)
    with pytest.raises(HTTPException) as e:
        _run(handlers["create"](CaseLibraryUpsert(
            content={"job_name": "alpha ranch"}, slug="other_slug")))
    assert e.value.status_code == 409


def test_create_validation(case_db, handlers):
    with pytest.raises(HTTPException) as e:
        _run(handlers["create"](CaseLibraryUpsert(content={"job_name": "  "})))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        _run(handlers["create"](CaseLibraryUpsert(
            content={"job_name": "X", "proposals": "not-a-list"})))
    assert e.value.status_code == 422


def test_update_roundtrip_and_404(case_db, handlers):
    content = _run(handlers["get"]("alpha_ranch"))["content"]
    content["identity"]["client"] = "New Client"
    _run(handlers["update"]("alpha_ranch", CaseLibraryUpsert(content=content)))
    assert _run(handlers["get"]("alpha_ranch"))["content"]["identity"]["client"] == "New Client"
    with pytest.raises(HTTPException) as e:
        _run(handlers["update"]("nope", CaseLibraryUpsert(content=content)))
    assert e.value.status_code == 404


def test_update_rejects_stealing_job_name(case_db, handlers):
    content = _run(handlers["get"]("beta_creek"))["content"]
    content["job_name"] = "Alpha Ranch"
    with pytest.raises(HTTPException) as e:
        _run(handlers["update"]("beta_creek", CaseLibraryUpsert(content=content)))
    assert e.value.status_code == 409
    # renaming a job in place (same slug) is allowed
    content2 = _run(handlers["get"]("alpha_ranch"))["content"]
    content2["job_name"] = "Alpha Ranch Renamed"
    _run(handlers["update"]("alpha_ranch", CaseLibraryUpsert(content=content2)))
    assert _run(handlers["get"]("alpha_ranch"))["content"]["job_name"] == "Alpha Ranch Renamed"


def test_update_replaces_children_no_orphans(case_db, handlers):
    # Editing line items should not leave orphaned rows behind (delete-orphan).
    content = _run(handlers["get"]("alpha_ranch"))["content"]
    content["proposals"][0]["line_items"] = [
        {"description": "CLEARING", "qty": 2, "unit": "AC", "ext_price": 5000.0},
    ]
    _run(handlers["update"]("alpha_ranch", CaseLibraryUpsert(content=content)))
    got = _run(handlers["get"]("alpha_ranch"))["content"]
    items = got["proposals"][0]["line_items"]
    assert len(items) == 1 and items[0]["description"] == "CLEARING"


def test_delete_and_404_after(case_db, handlers):
    _run(handlers["delete"]("beta_creek"))
    with pytest.raises(HTTPException) as e:
        _run(handlers["get"]("beta_creek"))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        _run(handlers["delete"]("beta_creek"))
    assert e.value.status_code == 404


def test_line_items_picker_aggregates_across_jobs(case_db, handlers):
    _run(handlers["create"](CaseLibraryUpsert(content={
        "job_name": "Delta Ridge",
        "proposals": [{"line_items": [
            {"description": "Mobilization", "unit": "LS", "category": "MOBILIZATION"},
        ]}],
    })))
    rows = _run(handlers["line_items"]())
    by_desc = {r["description"].lower(): r for r in rows}
    # "MOBILIZATION" (alpha) and "Mobilization" (delta) dedupe to one entry, 2 jobs
    assert by_desc["mobilization"]["job_count"] == 2
