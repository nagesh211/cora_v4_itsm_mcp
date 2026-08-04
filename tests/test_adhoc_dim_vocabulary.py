"""The ad-hoc builder must honour the schema's declared column vocabulary.

"show me changes closed by sector and type for last month" came back as ONE ungrouped
total with the summary "dataset lacks sector/type disaggregation" — which is false:
``itsm_change.tbl_change`` declares ``alias: sector`` on ``business_name`` and
``canonical: change type, type`` on ``type_description``.

:mod:`cora_mcp.column_resolver` exists to translate exactly those words, and both
``run_kpi`` (via ``resolve_dim_word``) and ``compose_metric`` (via the composer) called
it. ``sql_builder._resolve_col`` — the ad-hoc ``query_dataset`` path — did not: it looked
up the literal name with ``loader.column_info`` and nothing else, so every business word
raised ``BuilderError`` and each dimension was appended to ``dropped_dimensions`` and
silently left out of the GROUP BY.

These tests pin the SQL text rather than executed rows so they need no database.
"""
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import sql_builder as sb  # noqa: E402

_CHANGE = "itsm_change.tbl_change"


def _build(**spec):
    spec.setdefault("base", "itsm_change")
    return sb.build(spec)


def test_sector_and_type_reach_the_group_by():
    """The reported bug, at the layer that dropped them."""
    r = _build(dimensions=["sector", "type"], date_field="closed_date",
               period="last month")
    assert not r.dropped_dimensions, r.dropped_dimensions
    assert "business_name" in r.sql and "type_description" in r.sql
    assert "GROUP BY" in r.sql
    # projected under the user's own words, grouped on the physical columns
    assert "AS sector" in r.sql and "AS type" in r.sql


def test_resolution_is_reported_not_silent():
    """A word that reached a differently-named column must be reported, so the answer
    can say which column ran instead of implying the name matched."""
    r = _build(dimensions=["sector", "type"], date_field="closed_date",
               period="last month")
    assert r.resolved_columns["sector"] == {
        "table": _CHANGE, "column": "business_name", "how": "vocabulary"}
    assert r.resolved_columns["type"]["column"] == "type_description"


def test_a_literal_column_name_is_not_reported_as_resolved():
    r = _build(dimensions=["status_name"], date_field="closed_date",
               period="last month")
    assert not r.dropped_dimensions
    assert "status_name" not in r.resolved_columns


def test_vocabulary_words_work_for_filters_too():
    """Before the fix a filter on 'sector' did not merely drop — it raised, failing the
    whole request."""
    r = _build(dimensions=["type"],
               filters=[{"field": "sector", "op": "=", "values": ["FINANCE"]}],
               date_field="closed_date", period="last month")
    assert "business_name" in r.sql
    assert r.resolved_columns["sector"]["column"] == "business_name"


def test_vocabulary_words_work_for_detail_listings():
    r = _build(drilldown={"detail_columns": ["change_id", "sector", "type", "status"]},
               date_field="closed_date", period="last month")
    for phys in ("change_id", "business_name", "type_description", "status_name"):
        assert phys in r.sql
    assert "GROUP BY" not in r.sql          # a listing stays a listing


def test_unknown_dimension_is_still_dropped_and_still_reported():
    """The fallback must not become a guessing machine: a word that means nothing on
    the table is still refused rather than bent onto a neighbouring column."""
    r = _build(dimensions=["frobnicated"], period="last month")
    assert r.dropped_dimensions == ["frobnicated"]
    assert "frobnicated" not in r.resolved_columns


def test_group_by_cannot_land_on_a_timestamp_via_vocabulary():
    """`roles` narrows the vocabulary search for a breakdown. 'closed' names a clock,
    not a category, so it must be refused rather than grouped on."""
    r = _build(dimensions=["closed"], period="last month")
    assert r.dropped_dimensions == ["closed"]


def test_exact_column_name_still_wins_regardless_of_role():
    """The literal-name path stays role-agnostic, so anything that resolved before the
    change still resolves the same way."""
    r = _build(dimensions=["closed_date"], period="last month")
    assert not r.dropped_dimensions
    assert "closed_date" in r.sql
