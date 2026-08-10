"""Tests for the deterministic date resolver.

Uses a fixed `today` so results are stable. FY starts April, weeks start Sunday,
current periods resolve to-date — matching the resolver defaults.
"""
from datetime import date

import pytest

from cora_mcp.date_resolver import bucket_windows, resolve_dates

TODAY = date(2025, 9, 15)  # a Monday


def r(phrase):
    return resolve_dates(phrase, today=TODAY)


def test_last_3_months():
    # docstring example: last 3 *completed* months (excludes current)
    assert r("last 3 months")["start_date"] == "2025-06-01"
    assert r("last 3 months")["end_date"] == "2025-08-31"


def test_n_months_includes_current_to_date():
    out = r("2 months")
    assert out["start_date"] == "2025-08-01"
    assert out["end_date"] == "2025-09-15"


def test_this_month_to_date():
    out = r("this month")
    assert out == {"start_date": "2025-09-01", "end_date": "2025-09-15",
                   "matched": True, "phrase": "this month"}


def test_mtd_qtd_ytd():
    assert r("mtd")["start_date"] == "2025-09-01"
    assert r("ytd") == {"start_date": "2025-01-01", "end_date": "2025-09-15",
                        "matched": True, "phrase": "ytd"}
    # calendar QTD: Q3 starts July
    assert r("qtd")["start_date"] == "2025-07-01"
    assert r("qtd")["end_date"] == "2025-09-15"


def test_absolute_between():
    out = r("between 2025-06-10 and 2025-08-15")
    assert out["start_date"] == "2025-06-10"
    assert out["end_date"] == "2025-08-15"


def test_month_name_year():
    out = r("Aug 2025")
    assert out["start_date"] == "2025-08-01"
    assert out["end_date"] == "2025-08-31"


def test_fiscal_year_april():
    out = r("fy2024")
    assert out["start_date"] == "2024-04-01"
    assert out["end_date"] == "2025-03-31"


def test_rolling_days():
    out = r("last 30 days")
    assert out["end_date"] == "2025-09-15"
    assert out["start_date"] == "2025-08-17"


def test_last_quarter():
    # Q3 is current -> previous calendar quarter is Q2 (Apr-Jun)
    out = r("last quarter")
    assert out["start_date"] == "2025-04-01"
    assert out["end_date"] == "2025-06-30"


def test_unmatched_falls_back_to_mtd():
    out = r("some gibberish with no time")
    assert out["matched"] is False
    assert out["start_date"] == "2025-09-01"
    assert out["end_date"] == "2025-09-15"


def test_prior_to_date_year_not_swallowed_by_ytd():
    # regression: "prior/last/previous year to date" must NOT resolve to CYTD.
    for phrase in ("pytd", "prior year to date", "last year to date",
                   "previous year to date"):
        out = r(phrase)
        assert out["matched"] is True, phrase
        assert out["start_date"] == "2024-01-01", phrase
        assert out["end_date"] == "2024-09-15", phrase


def test_prior_month_quarter_week_to_date():
    assert r("pmtd")["start_date"] == "2025-08-01"
    assert r("pmtd")["end_date"] == "2025-08-15"
    assert r("prior month to date")["end_date"] == "2025-08-15"
    assert r("pqtd") == {"start_date": "2025-04-01", "end_date": "2025-06-16",
                         "matched": True, "phrase": "pqtd"}
    assert r("pwtd")["start_date"] == "2025-09-07"
    assert r("pwtd")["end_date"] == "2025-09-08"


def test_single_day_anchors():
    assert r("today") == {"start_date": "2025-09-15", "end_date": "2025-09-15",
                          "matched": True, "phrase": "today"}
    assert r("yesterday")["start_date"] == "2025-09-14"
    assert r("yesterday")["end_date"] == "2025-09-14"
    assert r("last day")["start_date"] == "2025-09-14"
    assert r("3 days ago") == {"start_date": "2025-09-12", "end_date": "2025-09-12",
                               "matched": True, "phrase": "3 days ago"}


def test_rolling_days_variants():
    # past/previous (not just "last") and singular "day" now match.
    assert r("past 4 days")["start_date"] == "2025-09-12"
    assert r("past 4 days")["end_date"] == "2025-09-15"
    assert r("previous 4 days")["start_date"] == "2025-09-12"
    assert r("last 1 day") == {"start_date": "2025-09-15", "end_date": "2025-09-15",
                               "matched": True, "phrase": "last 1 day"}


def test_half_year_and_calendar_quarter_and_wtd():
    assert r("H1 2025") == {"start_date": "2025-01-01", "end_date": "2025-06-30",
                            "matched": True, "phrase": "H1 2025"}
    assert r("H2 2024")["start_date"] == "2024-07-01"
    assert r("H2 2024")["end_date"] == "2024-12-31"
    assert r("first half of 2025")["end_date"] == "2025-06-30"
    assert r("Q1 2025") == {"start_date": "2025-01-01", "end_date": "2025-03-31",
                            "matched": True, "phrase": "Q1 2025"}
    assert r("Q3 2025")["start_date"] == "2025-07-01"
    assert r("Q3 2025")["end_date"] == "2025-09-30"
    assert r("wtd")["start_date"] == "2025-09-14"
    assert r("wtd")["end_date"] == "2025-09-15"


def test_bucket_windows_month_clamps_to_edges():
    # partial first/last months are clamped to the requested window edges
    assert bucket_windows("2026-05-01", "2026-06-30", "month") == [
        ("2026-05", ("2026-05-01", "2026-05-31")),
        ("2026-06", ("2026-06-01", "2026-06-30")),
    ]
    assert bucket_windows("2026-04-15", "2026-06-10", "month") == [
        ("2026-04", ("2026-04-15", "2026-04-30")),
        ("2026-05", ("2026-05-01", "2026-05-31")),
        ("2026-06", ("2026-06-01", "2026-06-10")),
    ]


def test_bucket_windows_quarter_and_single_bucket():
    assert bucket_windows("2026-01-01", "2026-06-30", "quarter") == [
        ("2026-Q1", ("2026-01-01", "2026-03-31")),
        ("2026-Q2", ("2026-04-01", "2026-06-30")),
    ]
    # a window inside one month yields exactly one bucket
    assert bucket_windows("2026-05-03", "2026-05-20", "month") == [
        ("2026-05", ("2026-05-03", "2026-05-20")),
    ]


def test_comparison_truncates_prior_side_to_match_to_date_span():
    # regression: "this month vs last month" used to compare a partial
    # to-date window (15 days into September) against a FULL prior month (31
    # days) -- the prior side always looked bigger regardless of the real
    # trend. The prior side must now cover the same elapsed span.
    out = r("this month vs last month")
    cmp = out["comparison"]
    assert cmp["current"] == {"start_date": "2025-09-01", "end_date": "2025-09-15",
                              "matched": True, "phrase": "this month"}
    assert cmp["previous"] == {"start_date": "2025-08-01", "end_date": "2025-08-15",
                               "matched": True, "phrase": "last month"}

    out = r("this quarter vs last quarter")
    cmp = out["comparison"]
    assert cmp["current"]["end_date"] == "2025-09-15"
    # 77 days elapsed into Q3 (Jul 1 -> Sep 15) mirrored onto Q2 (Apr 1 start)
    assert cmp["previous"]["end_date"] == "2025-06-16"


def test_comparison_leaves_two_complete_periods_untouched():
    # neither side is a to-date window here, so no truncation applies.
    out = r("Q1 2025 vs Q2 2025")
    cmp = out["comparison"]
    assert cmp["previous"] == {"start_date": "2025-01-01", "end_date": "2025-03-31",
                               "matched": True, "phrase": "Q1 2025"}
    assert cmp["current"] == {"start_date": "2025-04-01", "end_date": "2025-06-30",
                              "matched": True, "phrase": "Q2 2025"}


def test_range_ending_in_current_month_clamps_to_today():
    # regression: a multi-month trend range ("Mar 2025 to Sep 2025") whose end
    # falls in the CURRENT, still-in-progress month must clamp to today
    # instead of padding the last bucket with days that have no data yet.
    for phrase in ("mar 2025 to sep 2025 trend", "mar25 to sep25 trend",
                   "march 2025 to september 2025 trend"):
        out = r(phrase)
        assert out["start_date"] == "2025-03-01", phrase
        assert out["end_date"] == "2025-09-15", phrase


def test_named_single_period_still_returns_full_boundaries():
    # a standalone named period (not a range) keeps its calendar boundaries
    # even when "today" falls inside it -- only ranges/trends clamp.
    assert r("Q3 2025")["end_date"] == "2025-09-30"
    assert r("sep 2025")["end_date"] == "2025-09-30"


def test_range_ending_in_a_future_month_is_left_alone():
    # a genuinely future target month (entirely ahead of today) is an
    # intentional forward window, not an in-progress one -- don't clamp it.
    out = r("mar 2025 to dec 2025 trend")
    assert out["start_date"] == "2025-03-01"
    assert out["end_date"] == "2025-12-31"
