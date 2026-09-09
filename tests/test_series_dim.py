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


# --- SQL-mode per-dimension trend (GROUP BY injection per bucket) -----------
# Configs are built inline here: this path is about the SHAPE of the authored
# query, so a synthetic base_query states the case under test far more clearly
# than any real config, and keeps these tests independent of the config set.
_SIMPLE_BQ = ("select count(*) as v from s.t a "
              "where a.ts between '{from_date}' and '{to_date}' {filters}")
# A shape the group-by rewriter cannot express (no primary table in any scope it
# knows how to push a dimension into).
_UNINJECTABLE_BQ = ("with x as (select 1) select count(*) as v from x "
                    "where '{from_date}' < '{to_date}' {filters}")
_AUTHORED_BREAKDOWN = ("select a.{dim} as grp, count(*) as v from s.t a "
                       "where a.ts between '{from_date}' and '{to_date}' group by a.{dim}")
_WIN = ("2026-02-01 00:00:00", "2026-02-07 23:59:59")


def _sql_cfg(base_query, breakdown=None):
    cfg = {
        "name": "syn-sql-kpi",
        "module": "syn",
        "execution_mode": "SQL",
        "source": {"dialect": "postgres", "connection": "syn"},
        "primary_dataset": {"schema": "s", "table": "t", "name": "s.t"},
        "fields": {"vendor": {"dataset": "s.t", "column": "vendor_name"},
                   "region": {"dataset": "s.t", "column": "region_name"}},
        "allowed_group_by": [{"field": "vendor"}, {"field": "region"}],
        "sql": {"base_query": base_query},
    }
    if breakdown:
        cfg["drilldown"] = {"breakdown": {"query": breakdown}}
    return cfg


def test_sql_series_prefers_group_by_injection():
    # No authored {dim} breakdown query at all — the trend must STILL break down,
    # by rewriting the authored query the way table mode does.
    cfg = _sql_cfg(_SIMPLE_BQ)
    assert qe._breakdown_inner_sql(cfg, ["vendor"]) is None      # nothing to reuse
    style, reason = qe._sql_series_breakdown_style(cfg, ["vendor"], _WIN, {})
    assert (style, reason) == ("inject", None)

    sql, params = qe._sql_bucket_injected_breakdown(cfg, ["vendor"], _WIN, "week 2026-02-01", {})
    assert params == []
    low = sql.lower()
    # same (bucket, grp, v) row shape the authored-template path emits
    assert low.startswith("select 'week 2026-02-01' as bucket, _b.grp as grp, _b.v as v from (")
    assert "vendor_name as grp" in low and "group by" in low
    assert "{" not in sql                                        # every placeholder filled


def test_sql_series_injection_applies_filters():
    # The authored-template path cannot inject filters, so a filtered trend used to
    # lose its dimension. The injected path carries the filter into the query.
    cfg = _sql_cfg(_SIMPLE_BQ)
    style, reason = qe._sql_series_breakdown_style(cfg, ["vendor"], _WIN, {"region": ["EMEA"]})
    assert (style, reason) == ("inject", None)
    sql, _ = qe._sql_bucket_injected_breakdown(cfg, ["vendor"], _WIN, "week 2026-02-01",
                                               {"region": ["EMEA"]})
    assert "region_name" in sql and "emea" in sql.lower()


def test_sql_series_injection_splits_multiple_dims():
    cfg = _sql_cfg(_SIMPLE_BQ)
    sql, _ = qe._sql_bucket_injected_breakdown(cfg, ["vendor", "region"], _WIN, "week 2026-02-01", {})
    low = sql.lower()
    assert "_b.grp as grp" in low and "_b.grp2 as grp2" in low     # one column per dim
    assert "_b.v as v" in low


def test_sql_series_falls_back_to_authored_breakdown_when_injection_impossible():
    # Injection can't express this query shape, but the KPI authored a {dim}
    # breakdown query — use that (the pre-existing path) rather than dropping the dim.
    cfg = _sql_cfg(_UNINJECTABLE_BQ, breakdown=_AUTHORED_BREAKDOWN)
    style, reason = qe._sql_series_breakdown_style(cfg, ["vendor"], _WIN, {})
    assert (style, reason) == ("authored", None)


def test_sql_series_authored_fallback_still_refuses_filters():
    # The authored template has no filter slot; answering an unfiltered breakdown to
    # a filtered question would be a different question, so the dim is dropped.
    cfg = _sql_cfg(_UNINJECTABLE_BQ, breakdown=_AUTHORED_BREAKDOWN)
    style, reason = qe._sql_series_breakdown_style(cfg, ["vendor"], _WIN, {"region": ["EMEA"]})
    assert style is None
    assert "filter" in reason and "region" in reason


def test_sql_series_drops_dim_only_when_neither_path_works():
    cfg = _sql_cfg(_UNINJECTABLE_BQ)          # not injectable, no authored breakdown
    style, reason = qe._sql_series_breakdown_style(cfg, ["vendor"], _WIN, {})
    assert style is None
    # the reason must name BOTH failures, not just "no breakdown query"
    assert "group its authored query" in reason and "no reusable breakdown query" in reason


def test_outer_measure_alias_picks_the_non_group_projection():
    sql = "SELECT vendor_name AS grp, count(*) AS incident_count FROM s.t GROUP BY 1"
    assert qe._outer_measure_alias(sql, ["grp"]) == "incident_count"
    # ambiguous (two non-group projections) -> no guess
    sql2 = "SELECT vendor_name AS grp, count(*) AS c, avg(x) AS m FROM s.t GROUP BY 1"
    assert qe._outer_measure_alias(sql2, ["grp"]) is None
    assert qe._outer_measure_alias("not sql at all ((", ["grp"]) is None


def test_series_with_dim_survives_end_to_end_for_sql_kpi(monkeypatch):
    """The regression this path exists for: mode='series' + dim on a SQL-mode KPI
    with no authored breakdown query used to return an UNGROUPED trend."""
    import asyncio

    cfg = _sql_cfg(_SIMPLE_BQ)

    async def fake_get(name):
        return cfg

    monkeypatch.setattr(qe.get_catalog(), "get", fake_get)
    out = asyncio.run(qe.generate_query("syn-sql-kpi", from_date="2026-02-01",
                                        to_date="2026-02-28", mode="series", dim="vendor"))
    assert out["dimension"] == "vendor"
    assert "dropped_dim" not in out and not out.get("dimension_note")
    assert len(out["results"]) == 4                     # a month trends weekly
    for res in out["results"]:
        low = res["sql"].lower()
        assert "as bucket" in low and "_b.grp as grp" in low and "group by" in low


# --- single-bucket "trend by <dim>" is a breakdown (series -> table) --------
def _gen(monkeypatch, cfg, **kw):
    import asyncio

    async def fake_get(name):
        return cfg

    monkeypatch.setattr(qe.get_catalog(), "get", fake_get)
    return asyncio.run(qe.generate_query("syn-sql-kpi", **kw))


def test_single_bucket_series_with_dim_promotes_to_table(monkeypatch):
    # "monthly by vendor for February" = ONE monthly bucket -> a breakdown, answered
    # with one grouped query instead of being re-bucketed into weekly points.
    out = _gen(monkeypatch, _sql_cfg(_SIMPLE_BQ), from_date="2026-02-01",
               to_date="2026-02-28", mode="series", dim="vendor", grain="month")
    assert out["mode"] == "table"
    assert out["grain"] is None                      # no time bucketing in table mode
    assert out["dimension"] == "vendor"
    assert len(out["results"]) == 1
    assert "mode_note" in out and "table" in out["mode_note"]
    low = out["results"][0]["sql"].lower()
    assert "group by" in low
    assert "as bucket" not in low                    # not a per-bucket series wrapper


def test_multi_bucket_series_with_dim_stays_a_trend(monkeypatch):
    # Several buckets IS a trend — the promotion must not touch it.
    out = _gen(monkeypatch, _sql_cfg(_SIMPLE_BQ), from_date="2026-01-01",
               to_date="2026-06-30", mode="series", dim="vendor", grain="month")
    assert out["mode"] == "series"
    assert out["grain"] == "month"
    assert len(out["results"]) == 6
    assert "mode_note" not in out


def test_single_bucket_series_without_dim_stays_a_series(monkeypatch):
    # No dimension requested -> nothing to promote to; a bucketed trend was asked for.
    out = _gen(monkeypatch, _sql_cfg(_SIMPLE_BQ), from_date="2026-02-01",
               to_date="2026-02-28", mode="series", grain="month")
    assert out["mode"] == "series"
    assert "mode_note" not in out


def test_single_bucket_series_not_promoted_when_table_mode_cannot_group(monkeypatch):
    # The breakdown only exists as an authored {dim} query, which table mode can't
    # use — promoting would turn a working (if bucketed) breakdown into a hard error.
    out = _gen(monkeypatch, _sql_cfg(_UNINJECTABLE_BQ, breakdown=_AUTHORED_BREAKDOWN),
               from_date="2026-02-01", to_date="2026-02-28", mode="series",
               dim="vendor", grain="month")
    assert out["mode"] == "series"
    assert out["dimension"] == "vendor"              # still broken down
    assert "mode_note" not in out
