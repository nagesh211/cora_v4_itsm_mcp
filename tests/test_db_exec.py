"""Live DB execution test — runs ONLY when a Postgres DSN is configured.

Set CORA_DB_VTX5 (or CORA_PG_DSN) in the environment / .env to enable. Without
a DSN the whole module is skipped, so CI stays green offline.
"""
import os

import pytest

from cora_mcp.db import resolve_dsn

pytestmark = pytest.mark.skipif(
    resolve_dsn("vtx5") is None,
    reason="no Postgres DSN configured (set CORA_DB_VTX5 or CORA_PG_DSN)")


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
