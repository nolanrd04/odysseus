"""TODO_DDD: the manager-facing generate_line_items_workbook / generate_proposal_document
tools (phase-6 continuation chat only). Exercises the _qp_generate_* handlers directly
against an in-memory SQLite DB, mirroring the fixture pattern in test_qp_create_job_tool.py.
Both tools only ever write a Document row (text) — the actual .xlsx/.docx conversion happens
client-side (document.js), so these tests stop at "the right Document content was saved."
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.quick_proposal_routes as qpr
from core.database import Base, Document


def _run(coro):
    return asyncio.run(coro)


def _index(final_line_items=None, extracted_values=None):
    return SimpleNamespace(
        extracted_data={"final_line_items": final_line_items or []},
        extracted_values=extracted_values or {},
    )


@pytest.fixture
def doc_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(qpr, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(qpr, "_run_grand_total", lambda run_id: 50000.0)
    monkeypatch.setattr(qpr, "_load_run_meta", lambda run_id: {})
    return TestSessionLocal


_LINE_ITEMS = [
    {"description": "8\" SEWER MAIN", "unit": "LF", "qty": 500, "unit_price": 45.0,
     "tax_rate": 0.089, "ext_price": 500 * 45.0 * 1.089},
    {"description": "MOBILIZATION", "unit": "LS", "qty": 1, "unit_price": 10000.0,
     "tax_rate": 0, "ext_price": 10000.0},
]


def test_generate_workbook_happy_path(doc_db):
    index = _index(final_line_items=_LINE_ITEMS, extracted_values={"lot_count": {"value": 42}})
    result = _run(qpr._qp_generate_workbook("run_wb", None, index, {}))
    parsed = json.loads(result)
    assert parsed["doc_id"]
    assert "Line Items" in parsed["title"]

    db = doc_db()
    try:
        doc = db.query(Document).filter(Document.id == parsed["doc_id"]).first()
        assert doc is not None
        assert doc.language == "csv"
        assert doc.session_id is None  # library doc, per explicit user decision
        # CSV-quoted because the description itself contains a `"` character.
        assert '"8"" SEWER MAIN"' in doc.current_content
        assert "MOBILIZATION" in doc.current_content
        assert "Grand Total" in doc.current_content
        assert "50000" in doc.current_content
        assert "lot_count" in doc.current_content
    finally:
        db.close()


def test_generate_workbook_title_override(doc_db):
    index = _index(final_line_items=_LINE_ITEMS)
    result = _run(qpr._qp_generate_workbook("run_wb2", None, index, {"title": "Custom Workbook Title"}))
    parsed = json.loads(result)
    assert parsed["title"] == "Custom Workbook Title"


def test_generate_workbook_no_line_items(doc_db):
    index = _index(final_line_items=[])
    result = _run(qpr._qp_generate_workbook("run_wb3", None, index, {}))
    assert result.startswith("Error:")
    assert "no final_line_items" in result


def test_generate_proposal_doc_happy_path(doc_db):
    # _LINE_ITEMS: 8" SEWER MAIN is taxed (500 * 45.00 = $22,500 pretax, 8.9% tax
    # = $2,002.50); MOBILIZATION is untaxed ($10,000). Matches the reference
    # company template's non-taxed/taxed section split + single tax line (TODO_DDD).
    index = _index(final_line_items=_LINE_ITEMS)
    result = _run(qpr._qp_generate_proposal_doc("run_pd", None, index, {}))
    parsed = json.loads(result)
    assert parsed["doc_id"]
    assert "Proposal" in parsed["title"]

    db = doc_db()
    try:
        doc = db.query(Document).filter(Document.id == parsed["doc_id"]).first()
        assert doc is not None
        assert doc.language == "markdown"
        assert doc.session_id is None
        content = doc.current_content
        assert content.startswith("# Estimate #:")
        # Company letterhead (hardcoded per explicit user decision).
        assert "Terra Underground, LLC" in content
        assert "Brian Rush" in content
        # Non-taxed / taxed grouping with the correct pretax/tax math.
        assert "NON-TAXED ITEMS" in content
        assert "MOBILIZATION" in content
        assert "$10,000.00" in content  # non-taxed item + its subtotal
        assert "TAXED ITEMS" in content
        assert '8" SEWER MAIN' in content
        assert "$22,500.00" in content  # taxed subtotal (pretax)
        assert "Tax (8.9%)" in content
        assert "$2,002.50" in content
        assert "GRAND TOTAL" in content
        assert "$50,000.00" in content
        # Standard boilerplate sections all present, no manager-authored prose.
        for heading in ("## Notes", "## Inclusions", "## Exclusions", "## Payable as Follows", "## Acceptance of Proposal"):
            assert heading in content
        # Scale metrics belong in the internal workbook export, not a client-facing proposal.
        assert "Scale Metrics" not in content
    finally:
        db.close()


def test_generate_proposal_doc_all_non_taxed_omits_tax_section(doc_db):
    index = _index(final_line_items=[
        {"description": "MOBILIZATION", "unit": "LS", "qty": 1, "unit_price": 5000.0,
         "tax_rate": 0, "ext_price": 5000.0},
    ])
    result = _run(qpr._qp_generate_proposal_doc("run_pd_notax", None, index, {}))
    parsed = json.loads(result)
    db = doc_db()
    try:
        content = db.query(Document).filter(Document.id == parsed["doc_id"]).first().current_content
        assert "**TAXED ITEMS**" not in content  # not "NON-TAXED ITEMS", which legitimately contains "TAXED ITEMS"
        assert "Tax (" not in content
        assert "GRAND TOTAL" in content
    finally:
        db.close()


def test_generate_proposal_doc_no_line_items(doc_db):
    index = _index(final_line_items=[])
    result = _run(qpr._qp_generate_proposal_doc("run_pd2", None, index, {}))
    assert result.startswith("Error:")
    assert "no final_line_items" in result


def test_generate_proposal_doc_title_defaults_to_job_name(doc_db):
    index = _index(final_line_items=_LINE_ITEMS, extracted_values={"job_name": {"value": "Alpha Ranch"}})
    result = _run(qpr._qp_generate_proposal_doc("run_pd3", None, index, {}))
    parsed = json.loads(result)
    assert parsed["title"] == "Alpha Ranch — Proposal"
