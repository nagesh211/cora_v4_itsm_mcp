"""Offline relationship inference (the ``--from-db`` path needs a live Postgres).

These assert the *shape* of what inference produces, not a golden list: the schema
grows, and a test that pins the exact 34 candidates would fail on every addition
without telling anyone anything useful.
"""
import importlib.util
import os

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location(
    "infer_relationships", os.path.join(_ROOT, "tools", "infer_relationships.py"))
infer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(infer)

INC = "itsm_incident.tbl_all_incidents"
MI = "itsm_incident.tbl_major_incidents"
CHG = "itsm_change.tbl_change"
REL_IC = "itsm.tbl_incident_change_relation"


@pytest.fixture(scope="module")
def candidates():
    idx = infer.SchemaIndex()
    return infer.infer_from_schema(idx), idx


def _find(cands, left, right, via=None):
    for c in cands:
        ends = {c["left"], c["right"]}
        if ends == {left, right} and c.get("via") == via:
            return c
    return None


# ---------------------------------------------------------------------------
# the edges the question set actually needs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("right", [
    "itsm_incident.tbl_sla_response",          # response-SLA questions
    "itsm_incident.tbl_incident_sla",
    "itsm_availability.tbl_tableau_outagesv4",  # outage duration / availability
])
def test_incident_child_tables_are_discovered(candidates, right):
    cands, _ = candidates
    edge = _find(cands, INC, right)
    assert edge is not None, f"no inferred edge {INC} <-> {right}"
    assert edge["join_on"]["left_col"] == "incident_system_id"
    assert edge["cardinality"] == "one_to_many"


def test_junction_produces_a_via_edge(candidates):
    """change <-> major incidents through the incident/change relation table."""
    cands, _ = candidates
    edge = _find(cands, CHG, MI, via=REL_IC)
    assert edge is not None
    assert edge["cardinality"] == "many_to_many"
    assert edge["left_on"]["via_col"] and edge["right_on"]["via_col"]


def test_a_shared_key_yields_an_edge_per_owning_table(candidates):
    """incident_system_id keys both all_incidents and major_incidents; dropping the
    ambiguity would throw away every incident edge, so both are emitted."""
    cands, _ = candidates
    assert _find(cands, INC, "itsm_incident.tbl_sla_response") is not None
    assert _find(cands, MI, "itsm_incident.tbl_sla_response") is not None


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------
def test_nothing_already_declared_is_re_proposed(candidates):
    """The curated incident<->sla_resolution edge carries a human name; inference
    must not shadow it with a mechanical duplicate."""
    cands, idx = candidates
    novel = infer.novel_only(cands, idx.declared_edges())
    assert _find(novel, INC, "itsm_incident.tbl_sla_resolution") is None
    assert len(novel) < len(cands)


def test_no_self_edges(candidates):
    cands, _ = candidates
    assert all(c["left"] != c["right"] for c in cands)


def test_unmeasured_edges_have_no_confidence(candidates):
    """Offline inference asserts structure, never overlap. Claiming a confidence it
    did not measure is what --from-db is for."""
    cands, _ = candidates
    assert all(c["confidence"] is None for c in cands)


def test_every_candidate_is_builder_shaped(candidates):
    """Each edge must carry exactly the keys relationships.RelationshipGraph reads."""
    cands, _ = candidates
    for c in cands:
        assert c["left"] and c["right"] and c["name"]
        if c.get("via"):
            assert {"left_col", "via_col"} <= set(c["left_on"])
            assert {"right_col", "via_col"} <= set(c["right_on"])
        else:
            assert {"left_col", "right_col"} <= set(c["join_on"])


# ---------------------------------------------------------------------------
# type-family gate
# ---------------------------------------------------------------------------
def test_incompatible_types_never_join():
    assert infer._family("integer") == "int"
    assert infer._family("varchar") == "text"
    assert infer._family("timestamp") is None      # not joinable at all
    assert infer._family("int8") != infer._family("text")