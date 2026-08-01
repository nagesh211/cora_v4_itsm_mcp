"""Tests for ``cora_mcp.predicate_registry`` — the per-table binding contract.

The value of this layer is that ONE user word compiles to DIFFERENT SQL per table.
``sla_breached`` is integer ``1`` on ``tbl_incident_sla``, varchar ``'1'`` on
``tbl_sla_response`` and boolean ``true`` on ``tbl_tableau_major_incdnt`` — all
measured against the live database. If a binding ever silently becomes a single global
constant, questions start returning zero on some tables, so those type differences are
pinned here explicitly.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import predicate_registry as pr  # noqa: E402


def _reg():
    return pr.PredicateRegistry()


# ---------------------------------------------------------------------------
# synonym resolution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("term,expected", [
    ("major", "major_incident"),
    ("Major Incident", "major_incident"),
    ("major_incident", "major_incident"),
    ("sev 1", "major_incident"),
    ("sla breached", "sla_breached"),
    ("SLA_Breach", "sla_breached"),       # case + underscore folding is intentional
    ("sla breachez", None),               # near-miss must NOT fuzzy-match to a predicate
    ("breached sla", "sla_breached"),
    ("missed sla", "sla_breached"),
    ("within sla", "sla_met"),
    ("emergency change", "emergency_change"),
    ("high risk", "high_risk_change"),
])
def test_resolve_synonyms(term, expected):
    hit = _reg().resolve(term)
    assert (hit.name if hit else None) == expected


def test_resolution_is_whole_string_not_substring():
    """"business" must never be answered by a predicate merely containing it."""
    assert _reg().resolve("major incident count and other things") is None


def test_unknown_term_returns_none_rather_than_guessing():
    assert _reg().resolve("frobnicated") is None
    assert _reg().resolve("") is None


# ---------------------------------------------------------------------------
# per-table bindings — the core contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table,expected_value", [
    ("itsm_incident.tbl_incident_sla", 1),                      # integer
    ("itsm_incident.tbl_sla_resolution", 1),                    # integer
    ("itsm_incident.tbl_sla_response", "1"),                    # varchar
    ("itsm_availability.tbl_tableau_major_incdnt", True),       # boolean
])
def test_sla_breached_compiles_per_table_type(table, expected_value):
    b = _reg().get("sla_breached").binding_for(table)
    assert b is not None, f"sla_breached should bind on {table}"
    filters = b.as_filters()
    assert len(filters) == 1
    assert filters[0]["values"] == [expected_value]
    assert type(filters[0]["values"][0]) is type(expected_value)


def test_predicate_values_are_marked_resolved():
    """Binding values are already the real stored values. They must be flagged so
    sql_builder skips value_resolver — schema possible_values were measured to be
    wrong on several columns and would reject a correct value."""
    for f in _reg().get("sla_breached").binding_for(
            "itsm_incident.tbl_incident_sla").as_filters():
        assert f["resolved"] is True


def test_free_binding_costs_no_where_clause():
    """tbl_major_incidents contains only major incidents, so the predicate is
    satisfied by choosing that table rather than by filtering."""
    b = _reg().get("major_incident").binding_for("itsm_incident.tbl_major_incidents")
    assert b.is_free
    assert b.as_filters() == []


def test_non_free_binding_is_not_free():
    b = _reg().get("major_incident").binding_for("itsm_incident.tbl_all_incidents")
    assert not b.is_free
    assert b.as_filters()[0]["values"] == [True]      # boolean column, not '1'


def test_binding_absent_for_unrelated_table():
    assert _reg().get("major_incident").binding_for("itsm_change.tbl_change") is None


def test_primary_binding_is_declared_and_unique_per_predicate():
    """A generic 'breached' must resolve to the table covering BOTH SLA clocks, so
    exactly one binding is primary — otherwise the tie-break is arbitrary and the
    answer silently narrows to one clock."""
    reg = _reg()
    for name in reg.names():
        primaries = [b.table for b in reg.get(name).bindings() if b.primary]
        assert len(primaries) <= 1, f"{name} declares {len(primaries)} primaries"
    sla = reg.get("sla_breached")
    assert [b.table for b in sla.bindings() if b.primary] == \
        ["itsm_incident.tbl_incident_sla"]


# ---------------------------------------------------------------------------
# entity / grain integrity
# ---------------------------------------------------------------------------
def test_service_request_breach_is_a_separate_predicate():
    """SR breach lives at a different grain (request_item_id, not incident_id). If it
    were folded into sla_breached, the composer could emit a semi-join keyed on a
    column the SR table does not have."""
    reg = _reg()
    sr = reg.resolve("request sla breached")
    assert sr is not None and sr.name == "sr_sla_breached"
    assert sr.entity == "service_request"
    assert sr.grain_key == "request_item_id"
    assert "itsm_servicerequest.tbl_request_item" not in reg.get("sla_breached").tables()


def test_every_predicate_declares_entity_and_grain_key():
    reg = _reg()
    for name in reg.names():
        p = reg.get(name)
        assert p.entity, f"{name} has no entity"
        assert p.grain_key, f"{name} has no grain_key"
        assert p.tables(), f"{name} has no bindings"


def test_bound_columns_exist_in_the_schema():
    """Every column a binding references must really be declared. This is the guard
    against the drift that made a live KPI return 0: schema_v3.yaml was found to
    declare columns the database does not have, and vice versa."""
    from cora_mcp.schema_loader import get_loader
    loader = get_loader()
    reg = _reg()
    missing = []
    for name in reg.names():
        p = reg.get(name)
        for b in p.bindings():
            for cond in b.conditions:
                if not loader.column_info(b.table, cond["field"]):
                    missing.append(f"{name}: {b.table}.{cond['field']}")
        # the grain key must exist wherever the predicate can be tested
        for tbl in p.tables():
            if not loader.column_info(tbl, p.grain_key):
                missing.append(f"{name}: {tbl}.{p.grain_key} (grain key)")
    assert not missing, "bindings reference undeclared columns: %s" % missing


def test_duplicate_synonym_across_predicates_is_rejected(tmp_path):
    bad = tmp_path / "predicates.json"
    bad.write_text(
        '{"predicates": {'
        ' "a": {"entity": "x", "grain_key": "k", "synonyms": ["dup"], "bindings": []},'
        ' "b": {"entity": "x", "grain_key": "k", "synonyms": ["dup"], "bindings": []}}}',
        encoding="utf-8")
    with pytest.raises(pr.PredicateConfigError):
        pr.PredicateRegistry(str(bad))