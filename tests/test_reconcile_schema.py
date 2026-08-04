"""Tests for the schema-vs-database reconciliation logic (no database needed).

The database half is one ``information_schema`` query; the part worth pinning is the
JUDGMENT — which differences are defects, and which corrections may be applied
mechanically. Getting either wrong is expensive in opposite directions: too strict and
the gate cries wolf on ``varchar`` vs ``text``; too loose and it misses ``text[]``
declared on a scalar, which is what decides whether ``sql_builder`` emits
``CROSS JOIN LATERAL unnest(...)``.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.reconcile_schema import (_declared, _family,  # noqa: E402
                                    apply_fixes)


# ---------------------------------------------------------------------------
# type families — the difference that changes generated SQL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("declared,actual", [
    ("varchar", "varchar"),
    ("varchar", "text"),                    # both text: never changes behaviour
    ("character varying", "varchar"),
    ("timestamp", "timestamp"),
    ("timestamp without time zone", "timestamp"),
    ("int4", "int4"),
    ("bool", "bool"),
    ("numeric", "numeric"),
    ("text[]", "_text"),                    # Postgres spells text[] as _text
])
def test_equivalent_types_are_not_reported(declared, actual):
    assert _family(declared) == _family(actual)


@pytest.mark.parametrize("declared,actual", [
    ("text[]", "varchar"),                  # the array/scalar trap
    ("varchar", "_text"),
    ("varchar", "int4"),
    ("int4", "numeric"),
    ("timestamp", "date"),
    ("bool", "varchar"),
])
def test_behaviour_changing_types_are_reported(declared, actual):
    assert _family(declared) != _family(actual)


def test_array_family_keeps_its_inner_type():
    assert _family("text[]") == "array:text"
    assert _family("_int4") == "array:int"
    assert _family("text[]") != _family("int4[]")


def test_unknown_type_is_not_a_mismatch():
    """An unrecognised type must not manufacture a finding — `_family` returning
    'unknown' is how `structure()` skips the comparison."""
    assert _family(None) == "unknown"
    assert _family("") == "unknown"


# ---------------------------------------------------------------------------
# declaration index
# ---------------------------------------------------------------------------
def test_declared_indexes_first_occurrence_of_a_shared_table():
    """A table listed under two entities must resolve the same way SchemaLoader does
    (``_by_table.setdefault``), or the report describes a table the runtime never uses."""
    doc = {"modules": [{"entities": [
        {"name": "a", "tables": [{"name": "s.t", "columns": [{"name": "x", "type": "varchar"}]}]},
        {"name": "b", "tables": [{"name": "s.t", "columns": [{"name": "y", "type": "int4"}]}]},
    ]}]}
    decl = _declared(doc)
    assert set(decl["s.t"]) == {"x", "y"}     # union of columns, first wins per name


def test_declared_reads_the_real_schema():
    from cora_mcp.schema_loader import DEFAULT_SCHEMA_PATH
    from tools.reconcile_schema import _load_schema
    decl = _declared(_load_schema(DEFAULT_SCHEMA_PATH))
    assert "itsm_change.tbl_change" in decl
    assert decl["itsm_change.tbl_change"]["business_name"]["type"].startswith("text")


# ---------------------------------------------------------------------------
# apply_fixes — mechanical only
# ---------------------------------------------------------------------------
def _doc():
    return {"modules": [{"entities": [{"name": "e", "tables": [{
        "name": "s.t",
        "columns": [
            {"name": "gone", "type": "varchar"},
            {"name": "wrong_type", "type": "text[]"},
            {"name": "bad_domain", "type": "varchar", "possible_values": ["NOPE"]},
            {"name": "wide", "type": "varchar", "possible_values": ["A"]},
            {"name": "fine", "type": "varchar", "canonical": "keep me"},
        ]}]}]}]}


def test_absent_column_is_dropped():
    fixed, changes = apply_fixes(
        _doc(), [{"severity": "E2", "table": "s.t", "column": "gone"}], {})
    cols = {c["name"] for c in fixed["modules"][0]["entities"][0]["tables"][0]["columns"]}
    assert "gone" not in cols
    assert any("drop column s.t.gone" in c for c in changes)


def test_type_is_corrected_to_the_database():
    fixed, changes = apply_fixes(
        _doc(), [{"severity": "E3", "table": "s.t", "column": "wrong_type",
                  "actual": "varchar"}], {})
    col = [c for c in fixed["modules"][0]["entities"][0]["tables"][0]["columns"]
           if c["name"] == "wrong_type"][0]
    assert col["type"] == "varchar"
    assert any("retype" in c for c in changes)


def test_bad_domain_is_replaced_with_the_measured_one():
    fixed, _ = apply_fixes(
        _doc(), [{"severity": "E4", "table": "s.t", "column": "bad_domain"}],
        {"s.t": {"bad_domain": ["REAL", "VALUES"]}})
    col = [c for c in fixed["modules"][0]["entities"][0]["tables"][0]["columns"]
           if c["name"] == "bad_domain"][0]
    assert col["possible_values"] == ["REAL", "VALUES"]


def test_unenumerable_domain_is_dropped_not_replaced():
    fixed, changes = apply_fixes(
        _doc(), [{"severity": "W2", "table": "s.t", "column": "wide",
                  "detail": "more than 25 distinct values — ... meaningless ..."}], {})
    col = [c for c in fixed["modules"][0]["entities"][0]["tables"][0]["columns"]
           if c["name"] == "wide"][0]
    assert "possible_values" not in col
    assert any("drop possible_values" in c for c in changes)


def test_undeclared_columns_are_never_added():
    """W1 needs a role and often canonical/alias vocabulary — a machine guess there would
    put a wrong breakdown word into the planner's vocabulary."""
    fixed, changes = apply_fixes(
        _doc(), [{"severity": "W1", "table": "s.t", "column": "new_col",
                  "actual": "varchar"}], {})
    cols = {c["name"] for c in fixed["modules"][0]["entities"][0]["tables"][0]["columns"]}
    assert "new_col" not in cols
    assert not changes


def test_curated_metadata_survives_a_fix_pass():
    """canonical/alias is the hand-written half of the schema; a reconcile pass that
    dropped it would silently break every business-vocabulary lookup."""
    fixed, _ = apply_fixes(
        _doc(), [{"severity": "E2", "table": "s.t", "column": "gone"}], {})
    col = [c for c in fixed["modules"][0]["entities"][0]["tables"][0]["columns"]
           if c["name"] == "fine"][0]
    assert col["canonical"] == "keep me"


def test_no_findings_means_no_changes():
    fixed, changes = apply_fixes(_doc(), [], {})
    assert not changes
    assert len(fixed["modules"][0]["entities"][0]["tables"][0]["columns"]) == 5


# ---------------------------------------------------------------------------
# E5 — a case mismatch is a rename, never a deletion
# ---------------------------------------------------------------------------
def test_case_mismatch_renames_and_does_not_drop():
    """Found on the first live run: ``itsm_release.tbl_pepops_release_mgmt`` declares
    ``BUSINESS_NAME`` while Postgres holds ``business_name``. Reporting that as absent
    (E2) would have made --out DELETE a column that exists."""
    doc = {"modules": [{"entities": [{"name": "e", "tables": [{
        "name": "s.t", "columns": [{"name": "BUSINESS_NAME", "type": "varchar",
                                    "canonical": "sector"}]}]}]}]}
    fixed, changes = apply_fixes(
        doc, [{"severity": "E5", "table": "s.t", "column": "BUSINESS_NAME",
               "actual": "business_name"}], {})
    cols = fixed["modules"][0]["entities"][0]["tables"][0]["columns"]
    assert [c["name"] for c in cols] == ["business_name"]
    assert cols[0]["canonical"] == "sector"          # curation follows the rename
    assert any("rename" in c for c in changes)


def test_a_rename_and_a_retype_compose():
    doc = {"modules": [{"entities": [{"name": "e", "tables": [{
        "name": "s.t", "columns": [{"name": "COL", "type": "date"}]}]}]}]}
    fixed, _ = apply_fixes(doc, [
        {"severity": "E5", "table": "s.t", "column": "COL", "actual": "col"},
        {"severity": "E3", "table": "s.t", "column": "COL", "actual": "timestamp"},
    ], {})
    col = fixed["modules"][0]["entities"][0]["tables"][0]["columns"][0]
    assert col["name"] == "col" and col["type"] == "timestamp"


# ---------------------------------------------------------------------------
# E6 — the declared time field
# ---------------------------------------------------------------------------
def test_time_fields_are_indexed_per_table():
    from cora_mcp.schema_loader import DEFAULT_SCHEMA_PATH
    from tools.reconcile_schema import _declared_time_fields, _load_schema
    tf = _declared_time_fields(_load_schema(DEFAULT_SCHEMA_PATH))
    assert tf["itsm_change.tbl_change"]["field"] == "open_date"
    assert tf["itsm_availability.tbl_tableau_outagesv4"]["field"] == "the_date"


def test_a_text_time_field_is_not_temporal():
    """``itsm.tbl_incident_change_relation.dw_last_updt_dtm`` is declared the time field
    and is varchar in the database — a period filter would compare text to a window."""
    assert _family("varchar") not in ("timestamp", "date")
    assert _family("timestamp without time zone") == "timestamp"
    assert _family("date") == "date"