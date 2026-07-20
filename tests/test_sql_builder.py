"""Tests for the deterministic schema-driven SQL builder."""
import pytest

from cora_mcp.db import resolve_dsn
from cora_mcp.sql_builder import BuilderError, QuerySpec, build, _guard_readonly


def test_adhoc_count_by_dimension_with_dates():
    r = build({"base": "itsm_incident",
               "measure": {"agg": "count_distinct", "column": "incident_id"},
               "dimensions": ["region_name"], "period": "last month"})
    assert r.base_table == "itsm_incident.tbl_all_incidents"
    assert "count(distinct a.incident_id) AS value" in r.sql
    # region_name is text[] -> unnested to a scalar for grouping
    assert "u0.region_name AS region_name" in r.sql
    assert "GROUP BY u0.region_name" in r.sql
    assert "BETWEEN %s AND %s" in r.sql
    assert r.sql.strip().upper().startswith("SELECT")
    assert "LIMIT" in r.sql
    assert len(r.params) == 2  # date window


def test_default_measure_is_count_star():
    r = build({"base": "itsm_incident", "dimensions": ["priority_code"]})
    assert "count(*) AS value" in r.sql


def test_unknown_dimension_dropped_not_rejected():
    # An unknown dimension is dropped (query runs ungrouped) and reported, rather
    # than failing the whole request.
    r = build({"base": "itsm_incident", "dimensions": ["not_a_column"]})
    assert "not_a_column" in r.dropped_dimensions
    assert "count(*) AS value" in r.sql
    assert "GROUP BY" not in r.sql


def test_unknown_filter_column_still_rejected():
    # Filters are NOT dropped — a bad filter column would silently broaden results,
    # so it must still raise.
    with pytest.raises(BuilderError):
        build({"base": "itsm_incident",
               "filters": [{"field": "not_a_column", "op": "=", "values": ["x"]}]})


def test_unknown_base_rejected():
    with pytest.raises(BuilderError):
        build({"base": "nope_entity"})


def test_filter_ops_and_params():
    # priority_code is text -> case-insensitive ANY; region_name is text[] -> not_null
    r = build({"base": "itsm_incident",
               "filters": [{"field": "priority_code", "op": "in", "values": ["P1", "P2"]},
                           {"field": "region_name", "op": "not_null"}]})
    assert "lower(a.priority_code) = ANY(%s)" in r.sql
    assert "a.region_name IS NOT NULL" in r.sql
    assert r.params == [["p1", "p2"]]


def test_limit_is_capped():
    r = build({"base": "itsm_incident", "limit": 999999})
    assert "LIMIT 5000" in r.sql


def test_series_grain_adds_bucket():
    r = build({"base": "itsm_incident", "grain": "month", "period": "this year"})
    assert "date_trunc('month'" in r.sql
    assert "AS bucket" in r.sql


def test_guard_rejects_non_select():
    with pytest.raises(BuilderError):
        _guard_readonly("DELETE FROM x")
    with pytest.raises(BuilderError):
        _guard_readonly("SELECT 1; DROP TABLE x")


def test_cross_entity_join_plans_relation_and_type_const():
    r = build({"base": "itsm_incident", "join_with": ["itsm_change"],
               "measure": {"agg": "count_distinct", "column": "incident_id"},
               "period": "this year", "date_field": "open_date_time"})
    assert "INNER JOIN itsm.tbl_incident_change_relation" in r.sql
    assert "INNER JOIN itsm_change.tbl_change" in r.sql
    assert "b.type = %s" in r.sql                       # the discriminator
    assert "Caused By Change" in r.params
    assert r.joined_tables == ["itsm.tbl_incident_change_relation", "itsm_change.tbl_change"]


def test_cross_entity_dimension_from_joined_table_resolves():
    # risk_description lives on the change table, reached only via the join
    r = build({"base": "itsm_incident", "join_with": ["itsm_change"],
               "dimensions": ["risk_description"]})
    assert "c.risk_description AS risk_description" in r.sql


def test_left_join_override():
    r = build({"base": "itsm_incident", "join_with": ["itsm_change"], "join_type": "left"})
    assert "LEFT JOIN itsm.tbl_incident_change_relation" in r.sql


def test_array_filter_uses_case_insensitive_membership():
    # region_name is text[]; IN/= becomes a case-insensitive element membership test
    r = build({"base": "itsm_incident",
               "filters": [{"field": "region_name", "op": "in", "values": ["Asia Pacific"]}]})
    assert "EXISTS (SELECT 1 FROM unnest(a.region_name) AS _e WHERE lower(_e::text) = ANY(%s))" in r.sql
    assert "IN %s" not in r.sql
    assert r.params == [["asia pacific"]]        # lowered list (array), not a tuple


def test_array_dimension_unnests():
    r = build({"base": "itsm_incident", "dimensions": ["region_name"]})
    assert "CROSS JOIN LATERAL unnest(a.region_name) AS u0(region_name)" in r.sql
    assert "u0.region_name AS region_name" in r.sql
    assert "GROUP BY u0.region_name" in r.sql


def test_text_filter_is_case_insensitive():
    # text equality/IN compares lower(col) so 'EMERGENCY' matches stored 'emergency'
    r = build({"base": "itsm_change",
               "filters": [{"field": "type_description", "op": "in", "values": ["EMERGENCY"]}]})
    assert "lower(a.type_description) = ANY(%s)" in r.sql
    assert r.params == [["emergency"]]
    r2 = build({"base": "itsm_change",
                "filters": [{"field": "type_description", "op": "=", "values": ["Emergency"]}]})
    assert "lower(a.type_description) = %s" in r2.sql
    assert r2.params == ["emergency"]


def test_text_like_is_case_insensitive():
    r = build({"base": "itsm_change",
               "filters": [{"field": "type_description", "op": "like", "values": ["%EMER%"]}]})
    assert "lower(a.type_description) LIKE %s" in r.sql
    assert r.params == ["%emer%"]


def test_numeric_filter_still_uses_in():
    # non-text columns keep plain IN with a tuple param (no lower())
    r = build({"base": "itsm_change",
               "filters": [{"field": "successful_indicator", "op": "in", "values": [0, 1]}]})
    assert "a.successful_indicator IN %s" in r.sql
    assert "lower(" not in r.sql
    assert r.params == [(0, 1)]


# ---- live DB (skipped without a DSN) -------------------------------------
pytestmark_live = pytest.mark.skipif(
    resolve_dsn("vtx5") is None, reason="no Postgres DSN configured")


@pytestmark_live
async def test_adhoc_executes_live():
    from cora_mcp.query_engine import run_dataset_query
    out = await run_dataset_query({
        "base": "itsm_incident",
        "measure": {"agg": "count_distinct", "column": "incident_id"},
        "dimensions": ["region_name"], "period": "last month"})
    assert "error" not in out, out.get("error")
    assert out["columns"] == ["region_name", "value"]
    assert out["rowcount"] >= 0
