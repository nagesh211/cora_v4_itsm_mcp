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
