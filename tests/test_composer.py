"""Tests for ``cora_mcp.composer`` — anchor selection and the refusal invariant.

Two properties matter more than the SQL text:

1. **Anchor choice is principled, not incidental.** A table already scoped to the
   subset must win (the predicate then costs no WHERE clause), and where several tables
   could express a predicate the registry's PRIMARY one must win — otherwise a generic
   "SLA breached" silently narrows to one SLA clock.
2. **A constraint that cannot be expressed is refused, never dropped.** Answering
   "major incidents that breached SLA" without the breach test returns a much larger,
   confidently wrong number. Every test that asserts a refusal is guarding against a
   wrong answer, not merely an error path.

These run offline: planning is pure combinatorics over the schema and the registry.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import composer, sql_builder as sb  # noqa: E402

COUNT_INCIDENTS = {"agg": "count_distinct", "column": "incident_id", "alias": "v"}


# ---------------------------------------------------------------------------
# anchor selection
# ---------------------------------------------------------------------------
def test_free_predicate_picks_the_prescoped_table_and_emits_no_filter():
    p = composer.plan(predicates=["major"], measure=COUNT_INCIDENTS,
                      period="last 3 months")
    assert p["anchor_table"] == "itsm_incident.tbl_major_incidents"
    assert p["predicates"]["free"] == ["major_incident"]
    assert p["predicates"]["direct"] == []
    assert p["predicates"]["semi_join"] == []
    # the predicate contributed nothing to WHERE — it was satisfied by table choice
    assert p["spec"]["filters"] == []
    sql = sb.build(p["spec"]).sql
    assert "major_incident_indicator" not in sql


def test_generic_breach_anchors_on_the_primary_binding():
    """tbl_incident_sla covers BOTH the response and resolution clocks; the
    single-clock tables must not win a generic breach question."""
    p = composer.plan(predicates=["sla breached"], measure=COUNT_INCIDENTS,
                      period="last month")
    assert p["anchor_table"] == "itsm_incident.tbl_incident_sla"
    assert p["predicates"]["direct"] == ["sla_breached"]


def test_direct_binding_uses_the_tables_own_type():
    """The integer column must bind to 1, not the string '1' — the whole point of
    per-table bindings."""
    p = composer.plan(predicates=["sla breached"], measure=COUNT_INCIDENTS,
                      period="last month")
    breach = [f for f in p["spec"]["filters"]
              if f["field"] == "sla_breached_indicator"]
    assert breach and breach[0]["values"] == [1]
    assert breach[0]["resolved"] is True


def test_second_predicate_becomes_a_semi_join_not_a_join():
    """No single table carries major scope, breach state AND sector, so one predicate
    must be tested with EXISTS. It must be EXISTS rather than a JOIN: the SLA tables
    hold one row per clock, and a join would fan out and corrupt the measure."""
    p = composer.plan(predicates=["major", "sla breached"], measure=COUNT_INCIDENTS,
                      filters={"business": "PBNA"}, period="last 3 months")
    assert p["predicates"]["semi_join"] == ["sla_breached"]
    built = sb.build(p["spec"])
    assert "EXISTS (SELECT 1 FROM itsm_incident.tbl_incident_sla" in built.sql
    assert "JOIN itsm_incident.tbl_incident_sla" not in built.sql
    assert built.semi_joined_tables == ["itsm_incident.tbl_incident_sla"]


def test_semi_join_is_keyed_on_the_grain_column():
    p = composer.plan(predicates=["major", "sla breached"], measure=COUNT_INCIDENTS,
                      period="last month")
    sj = p["spec"]["semi_joins"][0]
    assert sj["key_right"] == "incident_id" and sj["key_left"] == "incident_id"
    assert "sj0.incident_id = a.incident_id" in sb.build(p["spec"]).sql


def test_planning_is_deterministic():
    kwargs = dict(predicates=["major", "sla breached"], measure=COUNT_INCIDENTS,
                  filters={"business": "PBNA"}, period="last 3 months")
    a, b = composer.plan(**kwargs), composer.plan(**kwargs)
    assert a["anchor_table"] == b["anchor_table"]
    assert sb.build(a["spec"]).sql == sb.build(b["spec"]).sql


@pytest.mark.parametrize("preds,expected_table", [
    (["emergency change"], "itsm_change.tbl_change"),
    (["high risk"], "itsm_change.tbl_change"),
    (["major problem"], "itsm_problem.tbl_problem"),
    (["major release"], "itsm_release.tbl_pepops_release_mgmt"),
    (["agile"], "itsm_release.tbl_pepops_release_mgmt"),
])
def test_predicates_route_to_their_own_entity(preds, expected_table):
    """The layer is generic across entities, not tuned to one example question."""
    p = composer.plan(predicates=preds, measure={"agg": "count", "column": "*"},
                      period="last quarter")
    assert p["anchor_table"] == expected_table


def test_multiple_predicates_on_one_table_all_bind_directly():
    p = composer.plan(predicates=["emergency change", "high risk"],
                      measure={"agg": "count", "column": "*"}, period="last quarter")
    assert sorted(p["predicates"]["direct"]) == ["emergency_change", "high_risk_change"]
    assert p["spec"]["semi_joins"] == []


# ---------------------------------------------------------------------------
# the refusal invariant — never silently drop a constraint
# ---------------------------------------------------------------------------
def test_unknown_predicate_is_refused_with_the_valid_list():
    with pytest.raises(composer.ComposeError) as exc:
        composer.plan(predicates=["frobnicated"], measure=COUNT_INCIDENTS,
                      period="last month")
    assert "unknown scope predicate" in str(exc.value)
    assert "sla_breached" in str(exc.value)          # tells the caller what IS valid


def test_predicates_from_different_entities_are_refused():
    """Incidents and changes are different populations; intersecting them in one
    query would be meaningless, so it must not be attempted."""
    with pytest.raises(composer.ComposeError) as exc:
        composer.plan(predicates=["major incident", "emergency change"],
                      measure={"agg": "count", "column": "*"}, period="last month")
    msg = str(exc.value)
    assert "multiple entities" in msg and "separate questions" in msg


def test_unbindable_filter_is_refused_rather_than_ignored():
    with pytest.raises(composer.ComposeError) as exc:
        composer.plan(predicates=["sla breached"], measure=COUNT_INCIDENTS,
                      filters={"unicorn_colour": "pink"}, period="last month")
    msg = str(exc.value)
    assert "unicorn_colour" in msg
    assert "different question" in msg               # says WHY it refused


def test_unavailable_breakdown_is_reported_not_silently_absent():
    """An unusable dimension does not fail the query (the number is still right), but
    it must be surfaced so the answer cannot imply a breakdown that never happened."""
    p = composer.plan(predicates=["sla breached"], measure=COUNT_INCIDENTS,
                      dimensions=["unicorn_colour"], period="last month")
    assert p["dropped_dimensions"] == ["unicorn_colour"]
    assert any("not available" in n for n in p["notes"])
    assert p["spec"]["dimensions"] == []


def test_notes_explain_how_each_predicate_was_applied():
    p = composer.plan(predicates=["major", "sla breached"], measure=COUNT_INCIDENTS,
                      period="last 3 months")
    joined = " ".join(p["notes"])
    assert "needed no filter" in joined              # the free one
    assert "EXISTS" in joined                        # the semi-joined one


# ---------------------------------------------------------------------------
# time handling
# ---------------------------------------------------------------------------
def test_explicit_date_field_is_honoured():
    p = composer.plan(predicates=["major"], measure=COUNT_INCIDENTS,
                      period="last month", date_field="closed_date_time")
    assert p["spec"]["date_field"] == "closed_date_time"
    assert "closed_date_time" in sb.build(p["spec"]).sql


def test_date_field_absent_from_every_candidate_is_refused():
    """"Opened last month" and "closed last month" are different questions, so a table
    lacking the named clock must not answer with a different one."""
    with pytest.raises(composer.ComposeError) as exc:
        composer.plan(predicates=["major"], measure=COUNT_INCIDENTS,
                      period="last month", date_field="no_such_timestamp")
    assert "time column" in str(exc.value)


def test_period_produces_a_bounded_window():
    p = composer.plan(predicates=["major"], measure=COUNT_INCIDENTS, period="last month")
    built = sb.build(p["spec"])
    assert "BETWEEN" in built.sql
    assert len([x for x in built.params if isinstance(x, str) and ":" in x]) == 2


# ---------------------------------------------------------------------------
# generated SQL safety
# ---------------------------------------------------------------------------
def test_generated_sql_is_read_only_and_parameterised():
    p = composer.plan(predicates=["major", "sla breached"], measure=COUNT_INCIDENTS,
                      filters={"business": "PBNA"}, period="last 3 months")
    built = sb.build(p["spec"])
    assert built.sql.lstrip().upper().startswith("SELECT")
    assert ";" not in built.sql
    assert "PBNA" not in built.sql and "pbna" not in built.sql   # value is bound
    assert ["pbna"] in built.params

# ---------------------------------------------------------------------------
# detail listings — a qualified "show me the records" question
# ---------------------------------------------------------------------------
def test_listing_projects_columns_with_no_aggregate():
    """The gap this closes: a qualified COUNT worked while the matching DETAILS
    question fell through to query_dataset(join_with=...) and failed on the empty
    relationship graph. A listing needs only the same EXISTS test."""
    p = composer.plan(predicates=["major", "sla breached"],
                      select=["incident_id", "status_name"], period="last month")
    assert p["shape"] == "listing"
    assert p["spec"]["measure"] is None
    assert p["spec"]["dimensions"] == []
    assert p["spec"]["drilldown"]["detail_columns"] == ["incident_id", "status_name"]
    sql = sb.build(p["spec"]).sql
    assert "count(" not in sql.lower() and "GROUP BY" not in sql
    assert "EXISTS (SELECT 1 FROM itsm_incident.tbl_incident_sla" in sql


def test_empty_select_means_listing_with_default_columns():
    """select=[] is falsy but must still mean 'a listing'; it exists so the caller
    never has to invent column names."""
    p = composer.plan(predicates=["major"], select=[], period="last month")
    assert p["shape"] == "listing"
    cols = p["spec"]["drilldown"]["detail_columns"]
    assert len(cols) > 3
    assert "incident_id" in cols
    assert not any("system_id" in c for c in cols)     # surrogate keys excluded


def test_select_requesting_a_qualifier_column_flips_the_anchor():
    """Asking to SEE the breach flag must anchor on the table that has it, rather than
    returning a listing silently missing the column the question was about."""
    p = composer.plan(predicates=["major", "sla breached"],
                      select=["incident_id", "sla_breached_indicator"],
                      period="last month")
    assert p["anchor_table"] == "itsm_incident.tbl_incident_sla"
    assert p["predicates"]["direct"] == ["sla_breached"]
    assert p["predicates"]["semi_join"] == ["major_incident"]
    assert "sla_breached_indicator" in p["spec"]["drilldown"]["detail_columns"]


def test_listing_warns_that_semi_joined_columns_cannot_be_shown():
    p = composer.plan(predicates=["major", "sla breached"],
                      select=["incident_id"], period="last month")
    assert any("cannot be shown" in n for n in p["notes"])


def test_no_false_row_grain_warning_on_an_entity_grain_table():
    """tbl_major_incidents is one row per incident, so a duplicate-rows caveat there
    would be a false alarm."""
    p = composer.plan(predicates=["major"], select=[], period="last month")
    assert not any("may therefore appear more than once" in n for n in p["notes"])


def test_row_grain_warning_when_the_anchor_is_a_child_table():
    p = composer.plan(predicates=["sla breached"],
                      select=["incident_id", "sla_breached_indicator"],
                      period="last month")
    assert p["anchor_table"] == "itsm_incident.tbl_incident_sla"
    assert any("may therefore appear more than once" in n for n in p["notes"])


def test_unprojectable_select_column_is_reported():
    p = composer.plan(predicates=["major"], select=["incident_id", "short_description"],
                      period="last month")
    assert any("short_description" in n and "do not exist" in n for n in p["notes"])
    assert "short_description" not in p["spec"]["drilldown"]["detail_columns"]


# ---------------------------------------------------------------------------
# default measure
# ---------------------------------------------------------------------------
def test_default_measure_counts_distinct_entities_not_rows():
    """count(*) on a child table would report SLA clocks as if they were incidents."""
    p = composer.plan(predicates=["sla breached"], period="last month")
    assert p["spec"]["measure"]["agg"] == "count_distinct"
    assert p["spec"]["measure"]["column"] == "incident_id"
