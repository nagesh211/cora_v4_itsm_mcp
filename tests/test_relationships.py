"""Tests for the relationship graph + join planner."""
import pytest

from cora_mcp.relationships import NoJoinPathError, get_graph

INC = "itsm_incident.tbl_all_incidents"
CHG = "itsm_change.tbl_change"
PRB = "itsm_problem.tbl_problem"
REL_IC = "itsm.tbl_incident_change_relation"
REL = "itsm_release.tbl_pepops_release_mgmt"


def test_relationships_loaded():
    g = get_graph()
    names = {r["name"] for r in g.relationships("itsm")}
    assert {"incident_caused_by_change", "incident_has_problem",
            "release_causes_change", "incident_sla_resolution"} <= names


def test_incident_to_change_uses_relation_table_and_type_const():
    g = get_graph()
    clauses = g.plan_join(INC, [CHG])
    tables = [c["table"] for c in clauses]
    assert tables == [REL_IC, CHG]        # hop through the relation table
    # the relation table carries the type='Caused By Change' discriminator
    via = clauses[0]
    assert via["const"] == [(REL_IC, "type", "Caused By Change")]
    assert via["on"] == [(INC, "incident_system_id", REL_IC, "incident_system_id")]
    # the change table joins to the relation table on change_system_id
    assert clauses[1]["on"] == [(REL_IC, "change_system_id", CHG, "change_system_id")]


def test_incident_to_problem_via_relation():
    g = get_graph()
    clauses = g.plan_join(INC, [PRB])
    assert [c["table"] for c in clauses] == ["itsm.tbl_incident_problem_relation", PRB]
    assert clauses[0]["const"] == []       # no discriminator declared


def test_release_to_incident_is_multi_hop():
    g = get_graph()
    clauses = g.plan_join(REL, [INC])
    tables = [c["table"] for c in clauses]
    # release -> change (direct), then change -> incident (via relation table)
    assert CHG in tables and REL_IC in tables and INC in tables
    assert tables.index(CHG) < tables.index(INC)


def test_unreachable_target_raises():
    g = get_graph()
    with pytest.raises(NoJoinPathError):
        g.plan_join(INC, ["itsm_servicedesk.tbl_happy_signals"])


def test_base_equals_target_no_clauses():
    assert get_graph().plan_join(INC, [INC]) == []
