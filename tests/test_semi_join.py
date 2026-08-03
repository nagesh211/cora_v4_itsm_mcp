"""Tests for the ``SemiJoin`` primitive in ``cora_mcp.sql_builder``.

An EXISTS test is used instead of a JOIN because the SLA tables hold **one row per SLA
clock** per incident. A JOIN there multiplies the base rows, which is harmless for
``count(distinct id)`` but silently wrong for ``avg``/``sum``/``count(*)``. The
regression these tests guard is a measure quietly inflating by the number of matching
child rows, which produces a plausible number rather than an error.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import sql_builder as sb  # noqa: E402

BASE = "itsm_incident.tbl_major_incidents"
SLA = "itsm_incident.tbl_incident_sla"


def _spec(**over):
    spec = {
        "base": BASE,
        "measure": {"agg": "count_distinct", "column": "incident_id", "alias": "v"},
        "semi_joins": [{"table": SLA, "key_right": "incident_id",
                        "filters": [{"field": "sla_breached_indicator", "op": "=",
                                     "values": [1], "resolved": True}],
                        "label": "sla_breached"}],
        "limit": 10,
    }
    spec.update(over)
    return spec


def test_emits_exists_correlated_on_the_key():
    built = sb.build(_spec())
    assert "EXISTS (SELECT 1 FROM %s sj0 WHERE sj0.incident_id = a.incident_id" % SLA \
        in built.sql


def test_does_not_emit_a_join():
    """The whole point: no row multiplication."""
    sql = sb.build(_spec()).sql
    assert "JOIN" not in sql.upper().replace("SEMI", "")
    assert " FROM %s a" % BASE in sql


def test_subquery_conditions_are_parameterised():
    built = sb.build(_spec())
    # sla_breached_indicator is varchar ('0'/'1'/'YES'/'NO'), so the comparison is
    # case-folded like every other text filter.
    assert "lower(sj0.sla_breached_indicator) = %s" in built.sql
    # The value binds exactly as supplied. Type coercion is deliberately NOT done
    # here: db._coerce_for_pgtype converts it against the column's *real* Postgres
    # type, read from the prepared statement, rather than against the schema YAML's
    # declared type (which has been measured wrong on several columns).
    assert "1" in built.params


def test_negate_emits_not_exists():
    spec = _spec()
    spec["semi_joins"][0]["negate"] = True
    sql = sb.build(spec).sql
    assert "NOT EXISTS (SELECT 1 FROM" in sql


def test_multiple_semi_joins_get_distinct_aliases():
    spec = _spec()
    spec["semi_joins"].append({
        "table": "itsm_incident.tbl_sla_response", "key_right": "incident_id",
        "filters": [{"field": "sla_breached_indicator", "op": "=",
                     "values": ["1"], "resolved": True}]})
    sql = sb.build(spec).sql
    assert "sj0" in sql and "sj1" in sql


def test_semi_joined_tables_are_reported_separately_from_joins():
    """They restrict rows but contribute no columns, so callers can explain scoping."""
    built = sb.build(_spec())
    assert built.semi_joined_tables == [SLA]
    assert built.joined_tables == []


def test_params_are_ordered_to_match_placeholders():
    """Semi-join params must land AFTER the date-window params, or every bind shifts."""
    built = sb.build(_spec(period="last 3 months", date_field="open_date_time"))
    assert built.sql.index("BETWEEN") < built.sql.index("EXISTS")
    assert built.params[-1] == "1"                   # breach value bound last
    assert len(built.params) == 3


def test_unknown_semi_join_table_is_rejected():
    with pytest.raises(sb.BuilderError) as exc:
        sb.build(_spec(semi_joins=[{"table": "nope.nope", "key_right": "incident_id"}]))
    assert "unknown semi-join table" in str(exc.value)


def test_unknown_key_on_joined_table_is_rejected():
    with pytest.raises(sb.BuilderError) as exc:
        sb.build(_spec(semi_joins=[{"table": SLA, "key_right": "no_such_key"}]))
    assert "semi-join key" in str(exc.value)


def test_unknown_column_inside_the_subquery_is_rejected():
    """A bad column must fail at build time, never reach the database."""
    with pytest.raises(sb.BuilderError) as exc:
        sb.build(_spec(semi_joins=[{
            "table": SLA, "key_right": "incident_id",
            "filters": [{"field": "no_such_column", "op": "=", "values": [1]}]}]))
    assert "no_such_column" in str(exc.value)


def test_resolved_flag_bypasses_value_domain_resolution():
    """A predicate value is already the real stored value. Re-resolving it against the
    column's declared possible_values would reject it, because those domains were
    measured to disagree with the database on several columns."""
    spec = _spec(base="itsm_incident.tbl_all_incidents", semi_joins=[])
    # OPENED is a real stored value that schema_v3.yaml does not list for status_name
    spec["filters"] = [{"field": "status_name", "op": "=", "values": ["OPENED"],
                        "resolved": True}]
    built = sb.build(spec)
    assert "opened" in [str(p).lower() for p in built.params]

    spec["filters"][0]["resolved"] = False
    with pytest.raises(sb.BuilderError) as exc:
        sb.build(spec)
    assert "not valid for" in str(exc.value)


def test_read_only_guard_still_applies():
    built = sb.build(_spec())
    assert built.sql.lstrip().upper().startswith("SELECT")
    assert ";" not in built.sql