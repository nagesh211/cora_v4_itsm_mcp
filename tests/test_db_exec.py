"""Live DB execution test — runs ONLY when a Postgres DSN is configured.

Set CORA_PG_DSN in the environment / .env to enable. Without a DSN the whole
module is skipped, so CI stays green offline.
"""
import os

import pytest

from cora_mcp.db import resolve_dsn

pytestmark = pytest.mark.skipif(
    resolve_dsn() is None,
    reason="no Postgres DSN configured (set CORA_PG_DSN)")


@pytest.mark.asyncio
async def test_trivial_execute():
    from cora_mcp.db import execute
    # cast the placeholders so asyncpg can infer types in a bare SELECT
    out = await execute("postgres", "vtx5", "SELECT %s::int AS n, %s::text AS s", [1, "hi"])
    assert out["rows"] == [{"n": 1, "s": "hi"}]
    assert out["columns"] == ["n", "s"]


@pytest.mark.asyncio
async def test_run_kpi_emergency_stat():
    from cora_mcp.query_engine import run_query
    out = await run_query("emergency", from_date="2026-01-01", to_date="2026-06-29",
                          mode="stat")
    res = out["results"][0]
    assert "error" not in res, res.get("error")
    assert res["columns"]  # e.g. ['v']


@pytest.mark.asyncio
async def test_query_dataset_adhoc_by_region():
    from cora_mcp.query_engine import run_dataset_query
    out = await run_dataset_query({
        "base": "itsm_incident",
        "measure": {"agg": "count_distinct", "column": "incident_id"},
        "dimensions": ["region_name"], "period": "last month"})
    assert "error" not in out, out.get("error")
    assert out["columns"] == ["region_name", "value"]


@pytest.mark.asyncio
async def test_query_dataset_cross_entity_inner_join():
    from cora_mcp.query_engine import run_dataset_query
    out = await run_dataset_query({
        "base": "itsm_incident", "join_with": ["itsm_change"],
        "measure": {"agg": "count_distinct", "column": "incident_id"},
        "period": "this year", "date_field": "open_date_time"})
    assert "error" not in out, out.get("error")
    assert "INNER JOIN itsm.tbl_incident_change_relation" in out["sql"]
    assert out["rows"] and isinstance(out["rows"][0]["value"], int)


@pytest.mark.asyncio
async def test_availability_impacting_predicate_matches_raw_sql_exactly():
    """The availability_impacting predicate's hypercare + NON-IT group exclusions
    (is_null condition + a binding-level semi_join) must reproduce the outage-count
    KPI's own scoping logic exactly, not merely plausibly -- a predicate that's a
    silent superset/subset is exactly the failure mode this layer exists to prevent.

    Compared against hand-written raw SQL using the SAME date-window semantics the
    composer applies (plain cast, no timezone shift) rather than against run_kpi's
    own stat output -- the KPI's raw SQL additionally converts the_date_time from
    GMT to America/Chicago before windowing, which query_dataset's generic date
    builder does not do (a separate, pre-existing gap, not part of this predicate).
    """
    from cora_mcp import composer, db
    from cora_mcp.query_engine import run_dataset_query

    period_from, period_to = "2026-06-01", "2026-06-30 23:59:59"
    ref = await db.execute("postgres", "vtx5", """
        select count(distinct outage_system_id) as n,
               sum(outage_seconds) as secs
        from itsm_availability.tbl_tableau_outagesv4
        where the_date_time between %s and %s
          and outage_type = 'OUTAGE'
          and business_criticality_value = '1 - most critical'
          and hypercare_project_system_id is null
          and support_group_system_id in (
              select group_system_id from itsm.tbl_group_hierarchy
              where service_area != 'NON-IT')
        """, [period_from, period_to])
    ref_count = ref["rows"][0]["n"]
    ref_secs = ref["rows"][0]["secs"]

    spec = composer.plan(predicates=["availability_impacting"],
                         measure={"agg": "count_distinct", "column": "outage_system_id"},
                         period="June 2026", date_field="the_date_time")
    pred_out = await run_dataset_query(spec["spec"])
    assert "error" not in pred_out, pred_out.get("error")
    assert pred_out["rows"][0]["v"] == ref_count

    spec_hours = composer.plan(predicates=["availability_impacting"],
                               measure={"agg": "sum", "column": "outage_seconds"},
                               period="June 2026", date_field="the_date_time")
    hours_out = await run_dataset_query(spec_hours["spec"])
    assert "error" not in hours_out, hours_out.get("error")
    assert hours_out["rows"][0]["v"] == ref_secs


@pytest.mark.asyncio
async def test_query_dataset_drilldown_change_reason():
    from cora_mcp import db
    from cora_mcp.query_engine import run_dataset_query
    sample = await db.execute(
        "postgres", "vtx5",
        "select change_id from itsm_change.tbl_change where change_id is not null limit 1", [])
    cid = sample["rows"][0]["change_id"]
    out = await run_dataset_query({
        "base": "itsm_change",
        "drilldown": {
            "detail_columns": ["change_id", "risk_description", "closure_code_description",
                               "risk_impact_analysis_text"],
            "entity_filter": {"field": "change_id", "op": "=", "values": [cid]}}})
    assert "error" not in out, out.get("error")
    assert out["rows"][0]["change_id"] == cid
    assert "risk_impact_analysis_text" in out["columns"]
