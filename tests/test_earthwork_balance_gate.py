"""TODO_JJ gate: earthwork_balance was migrated from a single scalar value to a
per-road list (see gemini_phase3.txt). _earthwork_balance_present() is the code-side
presence check that gates the mandatory kp_lookup('SECTION: earthwork_balance_prior')
call in end_generation — these tests guard both the new per-road shape and the legacy
scalar shape so the gate can't silently stop firing on either."""
from routes.quick_proposal_routes import _earthwork_balance_present


def test_present_when_a_road_has_a_real_balance():
    extracted_values = {
        "earthwork_balance": {
            "value": [
                {"road_name": "Bogie Drive", "balance": "import_needed"},
                {"road_name": "Arlis Street", "balance": "roughly_balanced"},
            ],
        }
    }
    assert _earthwork_balance_present(extracted_values) is True


def test_present_when_any_single_road_qualifies_among_unknowns():
    extracted_values = {
        "earthwork_balance": {
            "value": [
                {"road_name": "Greensferry Road", "balance": "unknown"},
                {"road_name": "Lesa Loop", "balance": "mixed"},
            ],
        }
    }
    assert _earthwork_balance_present(extracted_values) is True


def test_not_present_when_all_roads_unknown_or_null():
    extracted_values = {
        "earthwork_balance": {
            "value": [
                {"road_name": "Bogie Drive", "balance": "unknown"},
                {"road_name": "Arlis Street", "balance": None},
                {"road_name": "Lesa Loop", "balance": ""},
            ],
        }
    }
    assert _earthwork_balance_present(extracted_values) is False


def test_not_present_when_value_is_empty_list():
    extracted_values = {"earthwork_balance": {"value": []}}
    assert _earthwork_balance_present(extracted_values) is False


def test_not_present_when_key_missing():
    assert _earthwork_balance_present({}) is False


def test_not_present_when_entry_is_not_a_dict():
    extracted_values = {"earthwork_balance": "roughly_balanced"}
    assert _earthwork_balance_present(extracted_values) is False


def test_legacy_scalar_shape_present():
    extracted_values = {
        "earthwork_balance": {"value": "roughly_balanced", "confidence": "high"}
    }
    assert _earthwork_balance_present(extracted_values) is True


def test_legacy_scalar_shape_unknown_not_present():
    extracted_values = {
        "earthwork_balance": {"value": "unknown", "confidence": "low"}
    }
    assert _earthwork_balance_present(extracted_values) is False


def test_legacy_scalar_shape_null_value_not_present():
    extracted_values = {"earthwork_balance": {"value": None}}
    assert _earthwork_balance_present(extracted_values) is False
