"""Tests for period-over-period comparison queries.

Three layers, matching where the bug lived:

  1. ``date_resolver`` — "current"/"previous" period words, and a two-sided phrase
     ("last quarter vs current quarter") resolving into BOTH windows.
  2. ``query_engine.generate_query`` — one result per compared side, correctly
     labelled and tagged, for stat / table / series and for SQL-mode configs.
  3. ``query_engine._comparison_summary`` — the delta reported to the caller.

Uses a fixed ``today`` so the windows are stable (FY starts April, weeks start
Sunday, current periods run to-date).
"""
import os
import sys
from datetime import date

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import query_engine as _qe  # noqa: E402
from cora_mcp.date_resolver import (  # noqa: E402
    resolve_comparison,
    resolve_dates,
    split_comparison,
)

TODAY = date(2026, 7, 30)   # a Thursday, in calendar Q3


def r(phrase):
    return resolve_dates(phrase, today=TODAY)


def _sync(v):
    import asyncio
    return asyncio.run(v) if asyncio.iscoroutine(v) else v


def generate_query(*args, **kwargs):
    return _sync(_qe.generate_query(*args, **kwargs))


# ---------------------------------------------------------------------------
# 1. period words
# ---------------------------------------------------------------------------
def _window(phrase):
    out = r(phrase)
    return out["start_date"], out["end_date"], out["matched"]


def test_current_period_is_this_period():
    # "current quarter" used to match nothing and silently fall back to
    # month-to-date — a different window entirely.
    assert _window("current quarter") == _window("this quarter")
    assert _window("current quarter") == ("2026-07-01", "2026-07-30", True)
    assert _window("current month") == _window("this month")
    assert _window("current week") == _window("this week")


def test_previous_period_is_last_period():
    # only "previous quarter" was handled before; week/month/year fell through to
    # month-to-date, i.e. the CURRENT month — the opposite of what was asked.
    for grain in ("week", "month", "quarter", "year"):
        assert _window(f"previous {grain}") == _window(f"last {grain}"), grain
        assert _window(f"previous {grain}")[2] is True, grain
    assert _window("previous month") == ("2026-06-01", "2026-06-30", True)


def test_period_abbreviations():
    assert _window("last qtr") == _window("last quarter")
    assert _window("this qtr") == _window("this quarter")
    assert _window("last 3 mos") == _window("last 3 months")


# ---------------------------------------------------------------------------
# 2. comparison phrases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("phrase", [
    "last quarter vs current quarter",
    "last quarter versus this quarter",
    "current quarter vs last quarter",          # order must not matter
    "last quarter compared to this quarter",
    "last qtr vs this qtr",
])
def test_quarter_comparison_resolves_both_windows(phrase):
    out = r(phrase)
    cmp = out["comparison"]
    assert (cmp["previous"]["start_date"], cmp["previous"]["end_date"]) == \
           ("2026-04-01", "2026-06-30")
    assert (cmp["current"]["start_date"], cmp["current"]["end_date"]) == \
           ("2026-07-01", "2026-07-30")
    # the flat window describes the CURRENT side, whichever way round it was typed
    assert out["start_date"] == "2026-07-01"


def test_month_and_week_comparisons():
    m = r("last month vs current month")["comparison"]
    assert m["previous"]["start_date"] == "2026-06-01"
    assert m["current"]["start_date"] == "2026-07-01"
    w = r("previous week vs current week")["comparison"]
    assert (w["previous"]["start_date"], w["previous"]["end_date"]) == \
           ("2026-07-19", "2026-07-25")
    assert w["current"]["start_date"] == "2026-07-26"


def test_absolute_quarters_compare():
    c = r("Q1 2026 vs Q2 2026")["comparison"]
    assert c["previous"]["start_date"] == "2026-01-01"
    assert c["current"]["end_date"] == "2026-06-30"


def test_split_comparison_only_on_real_operators():
    assert split_comparison("last quarter vs this quarter") == \
           ("last quarter", "this quarter")
    assert split_comparison("last quarter") is None


def test_non_date_comparison_is_not_a_window_pair():
    # "incidents vs changes" is not a time comparison: no invented windows.
    assert resolve_comparison("incidents vs changes", today=TODAY) is None
    assert r("incidents vs changes").get("comparison") is None


def test_same_window_on_both_sides_is_not_a_comparison():
    assert resolve_comparison("this month vs current month", today=TODAY) is None


# ---------------------------------------------------------------------------
# 3. generate_query: one result per side
# ---------------------------------------------------------------------------
def test_stat_comparison_emits_both_windows_labelled():
    out = generate_query("emergency", period="last quarter vs current quarter",
                         mode="stat")
    assert len(out["results"]) == 2
    prev, cur = out["results"]
    assert prev["comparison_side"] == "previous"
    assert cur["comparison_side"] == "current"
    assert prev["label"] == "last quarter"          # the user's own words
    assert cur["label"] == "current quarter"
    assert prev["window"]["from"] < cur["window"]["from"]   # older side first
    assert out["comparison_windows"]["previous"]["phrase"] == "last quarter"
    # each side must carry its OWN window into the SQL, not one shared window
    assert prev["sql"] != cur["sql"] or prev["params"] != cur["params"]


def test_stat_comparison_does_not_add_the_prior_year_window():
    # `comparison=True` means the prior-YEAR window; a two-sided phrase already
    # named both windows, so the flag must not tack a third one on.
    out = generate_query("emergency", period="last month vs current month",
                         mode="stat", comparison=True)
    assert len(out["results"]) == 2
    assert {r["comparison_side"] for r in out["results"]} == {"previous", "current"}


def test_table_comparison_without_dimension_groups_by_period():
    # `availability-percentage` has no default table dimension, so table mode
    # normally errors. With a comparison the two periods ARE the grouping.
    out = generate_query("availability-percentage",
                         period="last month vs current month", mode="table")
    assert len(out["results"]) == 2
    assert out["mode_note"]                       # says the periods are the grouping
    assert [x["comparison_side"] for x in out["results"]] == ["previous", "current"]


def test_table_without_dimension_still_errors_when_not_a_comparison():
    from cora_mcp.query_engine import QueryError
    with pytest.raises(QueryError):
        generate_query("availability-percentage", period="last month", mode="table")


def test_series_comparison_buckets_each_side_for_sql_mode():
    out = generate_query("availability-percentage",
                         period="last quarter vs current quarter", mode="series")
    sides = [x["comparison_side"] for x in out["results"]]
    assert set(sides) == {"previous", "current"}
    assert sides[0] == "previous" and sides[-1] == "current"    # older side first
    assert sides == sorted(sides, key=lambda s: s != "previous")  # no interleaving
    # grain is derived from BOTH sides, so a quarter comparison is monthly — not
    # ~19 weekly windows chosen from the shorter side alone
    assert out["grain"] == "month"
    assert all(r["label"].endswith(")") for r in out["results"])   # tagged per side


def test_no_comparison_phrase_keeps_the_old_single_window_behaviour():
    out = generate_query("emergency", period="last quarter", mode="stat")
    assert len(out["results"]) == 1
    assert out["results"][0]["label"] == "requested window"
    assert "comparison_windows" not in out
    assert "comparison_side" not in out["results"][0]


# ---------------------------------------------------------------------------
# 4. the delta handed to the caller
# ---------------------------------------------------------------------------
def _stat_out(prev_value, cur_value, mode="stat"):
    return {
        "mode": mode,
        "comparison_windows": {
            "previous": {"phrase": "last quarter", "start_date": "2026-04-01",
                         "end_date": "2026-06-30"},
            "current": {"phrase": "current quarter", "start_date": "2026-07-01",
                        "end_date": "2026-07-30"},
        },
        "results": [
            {"comparison_side": "previous", "rows": [{"v": prev_value}]},
            {"comparison_side": "current", "rows": [{"v": cur_value}]},
        ],
    }


def test_comparison_summary_reports_delta_and_direction():
    s = _qe._comparison_summary(_stat_out(80, 100))
    assert s["previous"]["value"] == 80.0
    assert s["current"]["value"] == 100.0
    assert s["delta"] == 20.0
    assert s["pct_change"] == 25.0
    assert s["direction"] == "up"
    assert s["previous"]["phrase"] == "last quarter"


def test_comparison_summary_skipped_when_a_side_failed():
    out = _stat_out(80, 100)
    out["results"][1] = {"comparison_side": "current", "error": "DBError: boom"}
    assert _qe._comparison_summary(out) is None


def test_adhoc_aggregate_groups_by_period_and_matches_both_windows():
    from cora_mcp import sql_builder as sb
    built = sb.build({"base": "itsm_incident",
                      "period": "last month vs current month"})
    assert built.implicit_grain == "month"           # grouping applied automatically
    assert built.sql.count("BETWEEN") == 2           # both windows, OR'd
    assert " OR " in built.sql
    assert "GROUP BY" in built.sql and "ORDER BY bucket ASC" in built.sql
    assert [p[:10] for p in built.params] == ["2026-06-01", "2026-06-30",
                                              "2026-07-01", "2026-07-30"]


def test_adhoc_comparison_does_not_span_the_gap_between_disjoint_sides():
    from cora_mcp import sql_builder as sb
    built = sb.build({"base": "itsm_incident", "period": "Q1 2026 vs Q3 2026"})
    # Q2 must NOT be swept in by one wide window
    assert built.params[1].startswith("2026-03-31")
    assert built.params[2].startswith("2026-07-01")


def test_adhoc_detail_listing_keeps_both_windows_without_grouping():
    from cora_mcp import sql_builder as sb
    built = sb.build({"base": "itsm_incident",
                      "period": "last quarter vs current quarter",
                      "drilldown": {"detail_columns": ["incident_id"]}})
    assert built.implicit_grain is None              # a listing is never grouped
    assert "GROUP BY" not in built.sql
    assert built.sql.count("BETWEEN") == 2


def test_adhoc_plain_period_is_unchanged():
    from cora_mcp import sql_builder as sb
    built = sb.build({"base": "itsm_incident", "period": "last month"})
    assert built.implicit_grain is None
    assert built.sql.count("BETWEEN") == 1
    assert "GROUP BY" not in built.sql


def test_comparison_summary_skipped_for_series_and_breakdowns():
    # summing buckets would be wrong for a percentage; picking one group would be
    # wrong for a breakdown — so no delta is invented for those shapes.
    assert _qe._comparison_summary(_stat_out(80, 100, mode="series")) is None
    assert _qe._comparison_summary(_stat_out(80, 100, mode="table")) is None