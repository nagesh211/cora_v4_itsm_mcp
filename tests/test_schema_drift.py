"""Drift comparison between schema_v3.yaml and measured database facts.

The measuring itself needs a live Postgres; the comparison is pure and is what
actually decides whether a deploy is blocked, so it is the part worth pinning.
"""
import importlib.util
import os

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location(
    "introspect_facts", os.path.join(_ROOT, "tools", "introspect_facts.py"))
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)


def _measured(columns, exists=True):
    return {"table": "s.t", "exists": exists, "columns": columns}


def _kinds(issues):
    return {i["kind"] for i in issues}


# ---------------------------------------------------------------------------
# type families
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("declared,live", [
    ("varchar", "character varying"),
    ("int", "bigint"),
    ("timestamp", "timestamp without time zone"),
    ("bool", "boolean"),
    ("numeric", "double precision"),
])
def test_equivalent_types_are_not_drift(declared, live):
    assert facts._types_agree(declared, live)


@pytest.mark.parametrize("declared,live", [
    ("varchar", "integer"),
    ("timestamp", "varchar"),
    ("bool", "numeric"),
])
def test_genuinely_different_types_are_drift(declared, live):
    assert not facts._types_agree(declared, live)


# ---------------------------------------------------------------------------
# structural drift
# ---------------------------------------------------------------------------
def test_missing_table_is_reported_once_and_stops_there():
    issues = facts.compare("s.t", {"a": {"type": "varchar"}}, _measured({}, exists=False))
    assert len(issues) == 1
    assert issues[0]["kind"] == "missing_table"


def test_declared_column_absent_from_the_database():
    issues = facts.compare("s.t", {"gone": {"type": "varchar"}}, _measured({}))
    assert _kinds(issues) == {"missing_column"}


def test_type_mismatch_names_both_sides():
    issues = facts.compare("s.t", {"a": {"type": "integer"}},
                           _measured({"a": {"type": "character varying"}}))
    assert _kinds(issues) == {"type_mismatch"}
    assert "'integer'" in issues[0]["detail"] and "'character varying'" in issues[0]["detail"]


def test_a_column_the_yaml_does_not_declare_is_not_drift():
    """Extra columns in the database are normal; only what the YAML *claims* matters."""
    issues = facts.compare("s.t", {}, _measured({"surprise": {"type": "varchar"}}))
    assert issues == []


# ---------------------------------------------------------------------------
# value-domain drift -- the class that produces wrong answers rather than errors
# ---------------------------------------------------------------------------
def test_declared_value_that_never_occurs():
    issues = facts.compare(
        "s.t", {"a": {"type": "varchar", "possible_values": ["YES", "NO", "MAYBE"]}},
        _measured({"a": {"type": "varchar", "domain": ["YES", "NO"]}}))
    assert _kinds(issues) == {"phantom_values"}
    assert "maybe" in issues[0]["detail"]


def test_real_value_the_yaml_omits_is_flagged_as_a_false_rejection():
    issues = facts.compare(
        "s.t", {"a": {"type": "varchar", "possible_values": ["YES", "NO"]}},
        _measured({"a": {"type": "varchar", "domain": ["YES", "NO", "UNKNOWN"]}}))
    assert _kinds(issues) == {"unlisted_values"}
    assert "value_resolver will reject" in issues[0]["detail"]


def test_domain_comparison_is_case_insensitive():
    issues = facts.compare(
        "s.t", {"a": {"type": "varchar", "possible_values": ["Yes", "no"]}},
        _measured({"a": {"type": "varchar", "domain": ["YES", "NO"]}}))
    assert issues == []


def test_no_measured_domain_means_no_domain_verdict():
    """A high-cardinality column has no captured domain; silence beats a guess."""
    issues = facts.compare(
        "s.t", {"a": {"type": "varchar", "possible_values": ["YES"]}},
        _measured({"a": {"type": "varchar", "samples": ["ABC", "DEF"]}}))
    assert issues == []