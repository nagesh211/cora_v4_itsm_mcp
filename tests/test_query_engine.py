"""Tests for the query_engine bridge to gen_query.py.

Verifies the engine reproduces exactly what gen_query would build for the same
request, across DSL (stat / series / table) and SQL execution modes.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gen_query as gq  # noqa: E402
from cora_mcp import query_engine as _qe  # noqa: E402
from cora_mcp.query_engine import QueryError  # noqa: E402


def _sync(v):
    """Run an awaitable to completion. The query engine is async now; tests use the
    sync file backend so each call resolves without real I/O, letting the sync test
    bodies below stay unchanged."""
    import asyncio
    return asyncio.run(v) if asyncio.iscoroutine(v) else v


def generate_query(*args, **kwargs):
    return _sync(_qe.generate_query(*args, **kwargs))


def test_unknown_kpi_raises():
    with pytest.raises(QueryError):
        generate_query("does-not-exist")


def test_stat_without_period_uses_cytd_comparison_window():
    # No explicit period -> the governed default: CYTD comparison window.
    out = generate_query("emergency", mode="stat")
    assert out["execution_mode"] == "DSL"
    assert len(out["results"]) == 1  # no comparison requested

    cfg = gq.load_config("emergency")
    r = gq.resolve_comparison(cfg, gq.DEF_FROM, gq.DEF_TO)
    payload = gq.build_payload(cfg, "stat", r["cur"], {}, None, None)
    sql, params = gq.build_sql(cfg, payload)
    assert out["results"][0]["sql"] == sql
    assert out["results"][0]["params"] == list(params)


def test_stat_with_explicit_period_honours_that_window():
    # A named period must be used verbatim, NOT expanded to the KPI's CYTD window.
    out = generate_query("emergency", period="last quarter", mode="stat")
    assert len(out["results"]) == 1
    res = out["results"][0]
    assert res["label"] == "requested window"
    frm = out["resolved_from_phrase"]["start_date"]
    to = out["resolved_from_phrase"]["end_date"]
    assert res["window"] == {"from": frm + " 00:00:00", "to": to + " 23:59:59"}
    # the window must be the requested quarter, not year-to-date
    assert not res["window"]["from"].endswith("01-01 00:00:00") or frm.endswith("01-01")


def test_stat_comparison_with_explicit_period_shifts_one_year():
    out = generate_query("emergency", from_date="2026-04-01", to_date="2026-06-30",
                         mode="stat", comparison=True)
    labels = [r["label"] for r in out["results"]]
    assert labels == ["requested window", "previous-year window"]
    assert out["results"][0]["window"] == {"from": "2026-04-01 00:00:00", "to": "2026-06-30 23:59:59"}
    assert out["results"][1]["window"] == {"from": "2025-04-01 00:00:00", "to": "2025-06-30 23:59:59"}


def test_emergency_table_requires_or_defaults_dim():
    # emergency's table view defaults to business_name, so no dim still works
    out = generate_query("emergency", mode="table")
    assert out["dimension"] == "business_name"
    # explicit dim overrides
    out2 = generate_query("emergency", mode="table", dim="region")
    assert out2["dimension"] == "region"
    assert "a.region_name AS grp" in out2["results"][0]["sql"]


def test_table_mode_supports_multiple_dimensions():
    # asking to break down by several dimensions must GROUP BY all of them,
    # not silently keep only the first.
    out = generate_query("emergency", mode="table", dim=["region", "business_name"])
    assert out["dimension"] == ["region", "business_name"]
    sql = out["results"][0]["sql"]
    # first dim keeps the historical `grp` alias; the second gets grp2
    assert "a.region_name AS grp" in sql
    assert "AS grp2" in sql
    # both columns appear in the GROUP BY
    group_clause = sql.split("GROUP BY", 1)[1]
    assert "region_name" in group_clause and "business_name" in group_clause


def test_series_mode_uses_literal_window_and_grain():
    out = generate_query("emergency", from_date="2026-01-01", to_date="2026-03-31",
                         mode="series", grain="month")
    res = out["results"][0]
    assert res["window"]["from"] == "2026-01-01 00:00:00"
    assert res["window"]["to"] == "2026-03-31 23:59:59"
    assert "date_trunc('month'" in res["sql"]


def test_filters_become_bind_params():
    out = generate_query("emergency", mode="table", dim="region",
                         filters={"sector": "Retail", "region": ["EMEA", "APAC"]})
    params = out["results"][0]["params"]
    # sector/region are array_val text[]; values are lowered for case-insensitive
    # element membership (EXISTS ... lower(_e) = ANY(lowered list)).
    assert ["retail"] in params
    assert ["emea", "apac"] in params


def test_sql_mode_config_substitutes_window():
    out = generate_query("cm-major-incident", from_date="2026-01-01",
                         to_date="2026-06-29", mode="stat")
    assert out["execution_mode"] == "SQL"
    sql = out["results"][0]["sql"]
    assert "{from_date}" not in sql and "{to_date}" not in sql
    assert "2026-01-01 00:00:00" in out["results"][0]["preview"]


def test_sql_mode_series_breaks_out_by_month():
    # A comparative "last 2 months" question: the availability KPI is SQL-mode,
    # so a monthly series must yield ONE result per month (not a blended average).
    out = generate_query("availability-percentage", from_date="2026-05-01",
                         to_date="2026-06-30", mode="series", grain="month")
    assert out["execution_mode"] == "SQL"
    assert out["grain"] == "month"
    assert len(out["results"]) == 2

    may, jun = out["results"]
    assert may["window"] == {"from": "2026-05-01 00:00:00", "to": "2026-05-31 23:59:59"}
    assert jun["window"] == {"from": "2026-06-01 00:00:00", "to": "2026-06-30 23:59:59"}
    # each bucket runs the authored query for its own window (no leaked template)
    assert "2026-05-01 00:00:00" in may["preview"] and "2026-05-31 23:59:59" in may["preview"]
    assert "2026-06-01 00:00:00" in jun["preview"] and "2026-06-30 23:59:59" in jun["preview"]
    assert "{from_date}" not in may["sql"] and "{to_date}" not in jun["sql"]
    assert "2026-06" in jun["label"]


def test_sql_mode_series_defaults_grain_to_month():
    out = generate_query("availability-percentage", from_date="2026-04-15",
                         to_date="2026-06-10", mode="series")  # no grain given
    assert out["grain"] == "month"
    # buckets are clamped to the window edges: Apr 15->30, May, Jun 1->10
    windows = [(r["window"]["from"], r["window"]["to"]) for r in out["results"]]
    assert windows == [
        ("2026-04-15 00:00:00", "2026-04-30 23:59:59"),
        ("2026-05-01 00:00:00", "2026-05-31 23:59:59"),
        ("2026-06-01 00:00:00", "2026-06-10 23:59:59"),
    ]


def test_dsl_mode_series_still_single_windowed_query():
    # DSL configs group by the time bucket inside one query, so series stays a
    # single result with date_trunc — the SQL-mode bucketing must not apply here.
    out = generate_query("emergency", from_date="2026-01-01", to_date="2026-03-31",
                         mode="series", grain="month")
    assert out["execution_mode"] == "DSL"
    assert len(out["results"]) == 1
    assert "date_trunc('month'" in out["results"][0]["sql"]


def test_period_phrase_resolves_window():
    out = generate_query("emergency", period="between 2026-02-01 and 2026-02-28",
                         mode="series", grain="week")
    assert out["resolved_from_phrase"]["start_date"] == "2026-02-01"
    assert out["resolved_from_phrase"]["end_date"] == "2026-02-28"
    assert out["results"][0]["window"]["from"] == "2026-02-01 00:00:00"


# ---------------------------------------------------------------------------
# SQL-mode filter injection + validation (regression: filters used to be
# silently dropped for the 14 SQL-mode KPIs).
# ---------------------------------------------------------------------------
def test_sql_mode_filter_is_injected_into_query():
    # availability is SQL-mode; a sector filter must reach the generated SQL as
    # an inline predicate on the mapped column (business_name, array_val -> &&).
    out = generate_query("availability-percentage", from_date="2026-01-01",
                         to_date="2026-06-29", mode="stat",
                         filters={"sector": "CGF", "region": ["APAC", "EMEA"]})
    preview = out["results"][0]["preview"]
    # array_val text[] filters are inlined as case-insensitive element membership
    # (values lowered) so 'CGF' matches stored 'cgf'/'CGF'.
    assert "EXISTS (SELECT 1 FROM unnest(business_name) AS _e WHERE lower(_e::text) = ANY(ARRAY['cgf']))" in preview
    assert "EXISTS (SELECT 1 FROM unnest(region_name) AS _e WHERE lower(_e::text) = ANY(ARRAY['apac', 'emea']))" in preview
    # SQL-mode carries no bind params — filters are inlined like the dates.
    assert out["results"][0]["params"] == []


def test_sql_mode_multi_table_filter_is_alias_qualified():
    # pm-major joins 4 tables; the sector column must be qualified (a.business_name)
    # or Postgres would reject it as ambiguous.
    out = generate_query("pm-major-problem-average-rca-task-duration",
                         from_date="2026-01-01", to_date="2026-06-29",
                         mode="stat", filters={"sector": "CGF"})
    assert ("EXISTS (SELECT 1 FROM unnest(a.business_name) AS _e WHERE lower(_e::text) = ANY(ARRAY['cgf']))"
            in out["results"][0]["preview"])


def test_sql_filter_value_is_escaped():
    # single quotes in a value must be doubled, not break out of the literal
    # (value is also lowered for case-insensitive matching).
    out = generate_query("availability-percentage", mode="stat",
                         filters={"sector": "O'Brien"})
    assert "ARRAY['o''brien']" in out["results"][0]["preview"]


def test_filter_not_allowed_raises_queryerror():
    # DSL: previously raised a raw KeyError (unhandled 500); now a clean QueryError.
    with pytest.raises(QueryError):
        generate_query("emergency", filters={"country": "US"})
    # SQL: a field not in filters.allowed is rejected, not silently dropped.
    with pytest.raises(QueryError):
        generate_query("availability-percentage", filters={"bogus_field": "x"})


def test_sql_mode_table_dimension_fails_loud():
    # An authored scalar query can't express a generic GROUP BY, so table mode is
    # rejected rather than silently returning an ungrouped scalar.
    with pytest.raises(QueryError):
        generate_query("availability-percentage", mode="table", dim="region")


# Module-code resolution moved to cora_mcp.module_registry, where it is derived
# from the configured index rather than a hardcoded map (the codes differ per
# deployment). Covered by tests/test_module_registry.py against both vocabularies.


def test_resolve_dim_word_maps_alias_and_suffix():
    from cora_mcp.query_engine import resolve_dim_word
    cfg = gq.load_config("sd-call-volume")
    # "business" is a filter alias / a drilldown dim named business_name
    assert resolve_dim_word(cfg, "business") == "business_name"
    # a word that is already a real field passes through unchanged
    assert resolve_dim_word(cfg, "region") == "region"
    # unknown word -> None (KPI can't be grouped by it)
    assert resolve_dim_word(cfg, "totally-unknown") is None


def test_run_kpi_dim_word_is_alias_resolved():
    # regression: dim="business" used to raise "field 'business' not defined".
    # It must now resolve to the business_name column and GROUP BY it.
    out = generate_query("sd-call-volume", mode="table", dim="business",
                         period="last month")
    assert out["dimension"] == "business_name"
    assert "GROUP BY a.business_name" in out["results"][0]["sql"]


def test_breakdown_rows_labels_and_unwraps():
    from cora_mcp.query_engine import _breakdown_rows
    rows = [{"grp": ["FINANCE"], "v": 5015}, {"grp": ["IT SERVICES"], "v": 4950}]
    assert _breakdown_rows(["business_name"], rows) == [
        {"business_name": "FINANCE", "value": 5015},
        {"business_name": "IT SERVICES", "value": 4950},
    ]
    # two dimensions -> grp + grp2
    rows2 = [{"grp": ["EMEA"], "grp2": ["FINANCE"], "v": 12}]
    assert _breakdown_rows(["region_name", "business_name"], rows2) == [
        {"region_name": "EMEA", "business_name": "FINANCE", "value": 12},
    ]


async def test_module_overview_breaks_down_dsl_and_sql_mode(monkeypatch):
    import cora_mcp.query_engine as qe
    from cora_mcp import db

    async def fake_run_query(name, mode="stat", **kwargs):
        # DSL KPIs get their breakdown via mode="table" through run_query
        if mode == "table":
            return {"results": [{"window": {"from": "2026-06-01 00:00:00",
                                            "to": "2026-06-30 23:59:59"},
                                 "rows": [{"grp": ["FINANCE"], "v": 10},
                                          {"grp": ["IT SERVICES"], "v": 7}]}]}
        return {"results": [{"window": {"from": "2026-06-01 00:00:00",
                                        "to": "2026-06-30 23:59:59"},
                             "rows": [{"v": 100}]}]}

    async def fake_db_execute(dialect, connection, sql, params, limit=200):
        # SQL-mode KPIs run their authored drilldown.breakdown.query via db.execute
        assert "{" not in sql, "date/dim placeholders must be substituted"
        return {"rows": [{"grp": "FINANCE", "v": 71.89},
                         {"grp": "IT SERVICES", "v": 70.32}],
                "columns": ["grp", "v"], "rowcount": 2}

    monkeypatch.setattr(qe, "run_query", fake_run_query)
    monkeypatch.setattr(db, "execute", fake_db_execute)
    out = await qe.module_overview("service desk", period="last month", dim="business")
    metrics = {m["kpi"]: m for m in out["metrics"]}

    # DSL KPI: keeps the overall value AND gets a per-business breakdown
    cv = metrics["sd-call-volume"]
    assert cv["value"] == 100
    assert cv["dimension"] == "business_name"
    assert cv["breakdown"] == [
        {"business_name": "FINANCE", "value": 10},
        {"business_name": "IT SERVICES", "value": 7},
    ]

    # SQL-mode KPI: now broken down via its authored drilldown.breakdown query
    fcr = metrics["sd-fcr-percentage"]
    assert fcr["value"] == 100
    assert fcr["dimension"] == "business_name"
    assert fcr["breakdown"] == [
        {"business_name": "FINANCE", "value": 71.89},
        {"business_name": "IT SERVICES", "value": 70.32},
    ]


async def test_module_overview_sql_mode_drops_when_filters_applied(monkeypatch):
    # A SQL-mode KPI's authored breakdown query has no filter slot, so a requested
    # filter it DOES support can't be injected -> the breakdown is dropped (not
    # silently computed unfiltered). sd-fcr-percentage allows a 'sector' filter.
    import cora_mcp.query_engine as qe
    from cora_mcp import db

    async def fake_run_query(name, mode="stat", **kwargs):
        return {"results": [{"window": {"from": "2026-06-01 00:00:00",
                                        "to": "2026-06-30 23:59:59"},
                             "rows": [{"v": 100}]}]}

    async def boom(*a, **k):
        raise AssertionError("db.execute must not run when filters are applied")

    monkeypatch.setattr(qe, "run_query", fake_run_query)
    monkeypatch.setattr(db, "execute", boom)
    out = await qe.module_overview("service desk", period="last month",
                                   dim="business", filters={"sector": "Retail"})
    fcr = {m["kpi"]: m for m in out["metrics"]}["sd-fcr-percentage"]
    assert "breakdown" not in fcr
    assert fcr["dropped_dim"] == "business"


# ---------------------------------------------------------------------------
# Schema fallback: a dimension/filter the KPI config omits but the primary
# table declares in schema_v3.yaml is resolved from the schema (all paths).
# ---------------------------------------------------------------------------
def test_resolve_dim_word_falls_back_to_schema():
    # sr-accuracy-of-estimate does NOT declare contact_type in fields/drilldown,
    # but tbl_request_item has it -> resolvable via the schema fallback.
    import cora_mcp.query_engine as qe
    cfg = _sync(qe.get_catalog().get("sr-accuracy-of-estimate"))
    assert "contact_type" not in (cfg.get("fields") or {})
    assert "contact_type" not in ((cfg.get("drilldown") or {}).get("dimensions") or [])
    assert qe.resolve_dim_word(cfg, "contact_type") == "contact_type"
    # a word in neither config nor schema -> None (caller drops it)
    assert qe.resolve_dim_word(cfg, "definitely_not_a_column") is None


def test_resolve_dim_via_schema_is_role_restricted():
    # Only dimension-role columns are offered as group-bys (not measures/ids).
    import cora_mcp.query_engine as qe
    cfg = _sync(qe.get_catalog().get("sr-accuracy-of-estimate"))
    assert qe.resolve_dim_via_schema(cfg, "contact_type") == "contact_type"
    # request_item_id is an identifier, not a dimension -> not offered for group-by
    assert qe.resolve_dim_via_schema(cfg, "request_item_id", roles=("dimension",)) is None


def test_table_mode_groups_by_schema_discovered_dim():
    # KPI path: table mode emits a GROUP BY on a schema-only column.
    out = generate_query("sr-accuracy-of-estimate", period="last 3 months",
                         mode="table", dim="contact_type")
    assert out["dimension"] == "contact_type"
    assert "a.contact_type AS grp" in out["results"][0]["sql"]


def test_filter_falls_back_to_schema_column():
    # Filter path: a real schema column is honoured even past a curated
    # filters.allowed list (it ADDS a user filter, never silently drops one).
    cfg = generate_query("sr-accuracy-of-estimate", period="last 3 months",
                         filters={"contact_type": "Email"})
    assert "contact_type" in cfg["filters"]
    assert "contact_type" in cfg["results"][0]["sql"]


def test_filter_unknown_everywhere_still_raises():
    with pytest.raises(QueryError):
        generate_query("sr-accuracy-of-estimate", period="last 3 months",
                       filters={"definitely_not_a_column": "x"})
