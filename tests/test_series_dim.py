"""Trend (series) + dimension behaviour — DB-free.

Covers the fix for "trend by <dimension>" silently dropping the dimension, and the
duration-derived grain (a month-long trend buckets weekly, a multi-month trend
monthly). Exercises the pure helpers and SQL assembly; no OpenSearch/DB needed.
"""
import glob
import json
import os

import gen_query as gq
from cora_mcp import query_engine as qe

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _load(name):
    return json.load(open(os.path.join(_ROOT, "config", f"{name}.json"), encoding="utf-8"))


def _first_dsl_with_dim():
    for p in sorted(glob.glob(os.path.join(_ROOT, "config", "*.json"))):
        c = json.load(open(p, encoding="utf-8"))
        if c.get("execution_mode") == "DSL" and (c.get("drilldown") or {}).get("dimensions"):
            return c
    return None


# --- duration-derived grain -------------------------------------------------
def test_auto_grain_follows_span():
    assert qe._auto_grain("2026-06-01", "2026-06-30", "month") == "week"   # month + coarse -> week
    assert qe._auto_grain("2026-06-01", "2026-06-30", None) == "week"
    assert qe._auto_grain("2026-01-01", "2026-06-30", "month") == "month"  # 6 months kept monthly
    assert qe._auto_grain("2026-01-01", "2026-06-30", None) == "month"
    assert qe._auto_grain("2026-07-15", "2026-07-21", None) == "day"       # a week -> daily
    assert qe._auto_grain("2020-01-01", "2026-06-30", None) == "quarter"   # 6 years -> quarterly


# --- SQL-mode per-dimension trend (authored breakdown per bucket) -----------
def test_sql_bucket_breakdown_wraps_and_inlines():
    cfg = _load("sd-nps-percentage")            # SQL-mode, has a {dim} breakdown query
    sql, params = qe._sql_bucket_breakdown(
        cfg, "business_name",
        ("2026-01-01 00:00:00", "2026-01-31 23:59:59"), "month 2026-01")
    assert params == []
    assert sql.lower().startswith(
        "select 'month 2026-01' as bucket, _b.grp as grp, _b.v as v from (")
    assert "group by a.business_name" in sql
    assert "{" not in sql                        # every placeholder filled


def test_sql_bucket_breakdown_multiple_dimensions():
    # Two dimensions fold into a composite key, then split back into grp/grp2.
    cfg = _load("sd-nps-percentage")
    sql, params = qe._sql_bucket_breakdown(
        cfg, ["business_name", "region_name"],
        ("2026-01-01 00:00:00", "2026-01-31 23:59:59"), "month 2026-01")
    assert params == []
    # composite group key inside, split apart in the wrapper
    assert "a.business_name::text || chr(31) || a.region_name::text" in sql
    assert "split_part(_b.grp, chr(31), 1) as grp" in sql.lower()
    assert "split_part(_b.grp, chr(31), 2) as grp2" in sql.lower()
    assert "{" not in sql
    # the fold leaves a single composite key in the authored GROUP BY (no stray a.{dim})
    assert "a.{dim}" not in sql


def test_breakdown_maps_field_name_to_physical_column():
    # "region" is a filter FIELD whose real column is region_name; the breakdown
    # template must use the physical column, not the field name.
    cfg = _load("sd-nps-percentage")
    assert qe._dim_to_column(cfg, "region") == "region_name"
    assert qe._dim_to_column(cfg, "business_name") == "business_name"   # drilldown dim passthrough
    sql, _ = qe._sql_bucket_breakdown(
        cfg, ["business_name", "region"],
        ("2026-01-01 00:00:00", "2026-01-31 23:59:59"), "month 2026-01")
    assert "a.business_name::text || chr(31) || a.region_name::text" in sql
    assert "a.region::text" not in sql                 # never the bare field name


def test_breakdown_inner_none_when_template_has_no_dim_slot():
    # availability-percentage's breakdown is hardcoded (no {dim}) -> can't take a dim.
    cfg = _load("availability-percentage")
    assert qe._breakdown_inner_sql(cfg, ["business_name"]) is None


# --- DSL-mode per-dimension trend groups by BOTH time bucket and dimension --
def test_dsl_series_with_dim_groups_by_both():
    cfg = _first_dsl_with_dim()
    assert cfg is not None
    dim = cfg["drilldown"]["dimensions"][0]
    payload = gq.build_payload(
        cfg, "series", ("2026-01-01 00:00:00", "2026-06-30 23:59:59"), {}, dim, "month")
    assert "time_group" in payload
    assert payload.get("group_by_dim") == dim
    sql, _ = gq.build_sql(cfg, payload)
    group_by = sql.lower().split("group by", 1)[1]
    assert "date_trunc" in group_by              # time bucket
    assert dim.lower() in group_by               # dimension


def test_dsl_series_multiple_dims_group_by_all():
    cfg = _first_dsl_with_dim()
    dims = (cfg["drilldown"]["dimensions"] or [])[:2]
    if len(dims) < 2:
        return
    payload = gq.build_payload(
        cfg, "series", ("2026-01-01 00:00:00", "2026-06-30 23:59:59"), {}, dims, "month")
    assert payload.get("group_by_dim") == dims
    sql, _ = gq.build_sql(cfg, payload)
    gb = sql.lower().split("group by", 1)[1]
    assert "date_trunc" in gb
    for d in dims:
        assert d.lower() in gb


def test_dsl_series_without_dim_has_no_group_by_dim():
    cfg = _first_dsl_with_dim()
    payload = gq.build_payload(
        cfg, "series", ("2026-01-01 00:00:00", "2026-06-30 23:59:59"), {}, None, "month")
    assert "group_by_dim" not in payload         # dimensionless trend unchanged


# --- dimension resolution now applies in series mode ------------------------
def test_effective_dim_resolves_in_series_mode():
    cfg = _load("sd-nps-percentage")
    # "business" should resolve to a real groupable column (business_name) for series,
    # exactly as it does for table mode.
    assert qe._effective_dim(cfg, "series", "business") == "business_name"
    assert qe._effective_dim(cfg, "series", None) is None