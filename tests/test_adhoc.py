"""Tests for the ad-hoc query planner: plan_query + preflight."""
import pytest

from cora_mcp import adhoc
from cora_mcp.schema_loader import get_loader

CHANGE_STATUS = ["SCHEDULED", "REVIEW", "CLOSED", "ASSESS", "AUTHORIZE", "NEW", "IMPLEMENT"]


# ---- plan_query --------------------------------------------------------------
def test_plan_identifies_single_entity():
    out = adhoc.plan_query("show me the change ids and descriptions for changes")
    slugs = [c["slug"] for c in out["candidates"]]
    assert "itsm_change" in slugs
    assert "itsm_change" in out["entities"]
    assert "guidance" in out and out["guidance"]


def test_plan_identifies_cross_entity_and_relationship():
    out = adhoc.plan_query("change ids and descriptions for the releases completed last month")
    slugs = [c["slug"] for c in out["candidates"]]
    assert "itsm_release" in slugs and "itsm_change" in slugs
    # the release<->change relationship should be surfaced
    rel_names = {r["name"] for r in out["relationships"]}
    assert "release_causes_change" in rel_names


def test_plan_flags_metric_in_disguise():
    out = adhoc.plan_query("how many changes were completed last month")
    assert out["maybe_metric"] and out["maybe_metric"]["looks_like_aggregate"]


def _entity_column(slug, col):
    """The column dict entity_detail() actually iterates (the change table is
    duplicated across entity blocks, so column_info's copy differs)."""
    loader = get_loader()
    _mod, entity = loader.get_entity(slug)
    for t in entity.get("tables") or []:
        for c in t.get("columns") or []:
            if c.get("name") == col:
                return c
    return None


def test_plan_surfaces_value_domain_when_present():
    ci = _entity_column("itsm_change", "status_name")
    assert ci is not None
    prev = ci.get("possible_values")
    ci["possible_values"] = list(CHANGE_STATUS)
    try:
        out = adhoc.plan_query("list changes by status")
        dwd = out["entities"]["itsm_change"]["dimensions_with_domain"]
        assert any(d["name"] == "status_name" and "CLOSED" in d["possible_values"] for d in dwd)
    finally:
        if prev is None:
            ci.pop("possible_values", None)
        else:
            ci["possible_values"] = prev


# ---- preflight ---------------------------------------------------------------
def test_preflight_ok_for_valid_spec():
    r = adhoc.preflight({"base": "itsm_incident",
                         "measure": {"agg": "count", "column": "*"},
                         "period": "last month"})
    assert r["ok"] and not r["errors"]


def test_preflight_unknown_base_suggests_entities():
    r = adhoc.preflight({"base": "nope_entity"})
    assert not r["ok"]
    assert any("unknown base" in e for e in r["errors"])


def test_preflight_unknown_dimension_is_dropped_with_warning():
    # An unknown DIMENSION is not fatal: it's dropped (query runs ungrouped) and
    # surfaced as a warning with a suggestion, not an error.
    r = adhoc.preflight({"base": "itsm_incident", "dimensions": ["regionn_name"]})
    assert r["ok"]
    assert not r["errors"]
    assert any("regionn_name" in w and "dropped" in w for w in r["warnings"])


def test_preflight_cross_entity_join_ok():
    r = adhoc.preflight({"base": "itsm_release", "join_with": ["itsm_change"],
                         "select": ["change_id"]})
    # release_causes_change exists -> no join error
    assert not any("no declared relationship" in e for e in r["errors"])


def test_preflight_rejects_undeclared_join():
    r = adhoc.preflight({"base": "itsm_release", "join_with": ["itsm_availability"]})
    assert not r["ok"]
    assert any("no declared relationship" in e for e in r["errors"])


def test_preflight_rejects_bad_value_with_domain():
    loader = get_loader()
    ci = loader.column_info("itsm_change.tbl_change", "status_name")
    prev = ci.get("possible_values")
    ci["possible_values"] = list(CHANGE_STATUS)
    try:
        r = adhoc.preflight({"base": "itsm_change",
                             "filters": [{"field": "status_name", "op": "=",
                                          "values": ["banana"]}]})
        assert not r["ok"]
        assert any("not valid for 'status_name'" in e for e in r["errors"])
    finally:
        if prev is None:
            ci.pop("possible_values", None)
        else:
            ci["possible_values"] = prev


def test_preflight_notes_value_autocorrection():
    loader = get_loader()
    ci = loader.column_info("itsm_change.tbl_change", "status_name")
    prev = ci.get("possible_values")
    ci["possible_values"] = list(CHANGE_STATUS)
    try:
        r = adhoc.preflight({"base": "itsm_change",
                             "filters": [{"field": "status_name", "op": "=",
                                          "values": ["completed"]}]})
        assert r["ok"]
        assert r["resolved"]["corrected_values"] == [
            {"field": "status_name", "from": "completed", "to": "CLOSED"}]
    finally:
        if prev is None:
            ci.pop("possible_values", None)
        else:
            ci["possible_values"] = prev