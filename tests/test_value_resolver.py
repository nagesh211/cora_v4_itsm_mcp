"""Tests for filter-VALUE resolution against a column's declared domain."""
import pytest

from cora_mcp import value_resolver as vr
from cora_mcp.sql_builder import BuilderError, build
from cora_mcp.schema_loader import get_loader

CHANGE_STATUS = ["SCHEDULED", "REVIEW", "CLOSED", "ASSESS", "AUTHORIZE", "NEW", "IMPLEMENT"]


# ---- pure unit: resolve_value ------------------------------------------------
def test_exact_match_is_case_insensitive():
    r = vr.resolve_value("status_name", "closed", CHANGE_STATUS)
    assert r.matched and r.method == "exact" and r.resolved == "CLOSED"


def test_synonym_completed_maps_to_closed():
    r = vr.resolve_value("status_name", "completed", CHANGE_STATUS)
    assert r.matched and r.method == "synonym" and r.resolved == "CLOSED"


def test_synonym_picks_first_candidate_present_in_domain():
    # 'completed' -> [CLOSED, COMPLETE, ...]; only CLOSED is in this domain
    r = vr.resolve_value("state", "done", ["Complete", "Open"])
    assert r.matched and r.resolved == "Complete"  # COMPLETE candidate present


def test_fuzzy_near_match():
    r = vr.resolve_value("status_name", "scheduld", CHANGE_STATUS)   # typo
    assert r.matched and r.method == "fuzzy" and r.resolved == "SCHEDULED"


def test_reject_unknown_value_keeps_domain():
    r = vr.resolve_value("status_name", "banana", CHANGE_STATUS)
    assert not r.matched and r.method == "reject"
    assert r.valid_values == CHANGE_STATUS


def test_passthrough_when_no_domain():
    r = vr.resolve_value("description_text", "anything", None)
    assert r.matched and r.method == "passthrough" and r.resolved == "anything"


def test_by_column_type_synonym():
    r = vr.resolve_value("type", "caused by change", ["Caused By Change", "Change"])
    assert r.matched and r.resolved == "Caused By Change"


# ---- resolve_or_raise --------------------------------------------------------
def test_resolve_or_raise_maps_and_rejects():
    assert vr.resolve_or_raise("status_name", ["completed"], CHANGE_STATUS) == ["CLOSED"]
    with pytest.raises(ValueError) as e:
        vr.resolve_or_raise("status_name", ["nope"], CHANGE_STATUS)
    assert "valid values" in str(e.value)


# ---- integration through the real builder ------------------------------------
@pytest.fixture
def change_status_domain():
    """Temporarily give itsm_change.status_name a domain in the live loader."""
    loader = get_loader()
    ci = loader.column_info("itsm_change.tbl_change", "status_name")
    assert ci is not None, "schema shape changed: itsm_change.status_name missing"
    prev = ci.get("possible_values")
    ci["possible_values"] = list(CHANGE_STATUS)
    yield
    if prev is None:
        ci.pop("possible_values", None)
    else:
        ci["possible_values"] = prev


def test_builder_corrects_completed_to_closed(change_status_domain):
    r = build({"base": "itsm_change",
               "drilldown": {"detail_columns": ["change_id", "description_text"],
                             "detail_table": "itsm_change.tbl_change"},
               "filters": [{"field": "status_name", "op": "=", "values": ["completed"]}]})
    assert "lower(a.status_name) = %s" in r.sql
    assert r.params == ["closed"]          # 'completed' -> 'CLOSED' -> lowered for compare


def test_builder_rejects_unknown_status(change_status_domain):
    with pytest.raises(BuilderError) as e:
        build({"base": "itsm_change",
               "filters": [{"field": "status_name", "op": "=", "values": ["approved-ish"]}]})
    assert "valid values" in str(e.value)


def test_builder_passthrough_for_domainless_column():
    # description_text has no possible_values -> value used as-is (no reject)
    r = build({"base": "itsm_change",
               "filters": [{"field": "description_text", "op": "like", "values": ["%db%"]}]})
    assert "lower(a.description_text) LIKE %s" in r.sql