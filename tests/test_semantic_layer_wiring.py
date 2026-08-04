"""Tests for the semantic layer's two hand-maintained registries.

An audit of ``predicates.json`` + ``record_prefixes.json`` found three classes of defect
that shared one cause — a fact was hand-transcribed where it could have been derived or
declared, and nothing reported the mistake:

  1. an ``entity_aliases`` slug that does not exist is DROPPED silently by
     ``_identify_entities`` (it filters on ``loader.all_entities()``), so
     ``itsm_servicerequest`` disabled every service-request question and a missing
     ``availability`` entry sent an availability question to the major-incident table
  2. the entity id column was *derived* by a heuristic that returns the first non-system
     ``*_id`` — ``change_id`` for ``itsm_release``, ``first_task_id`` for
     ``itsm_service_request``, both wrong — while ``record_prefixes.json`` declared the
     right value all along
  3. ``predicates.json`` covered 11 of 31 declared tables, with nothing reporting the
     other 20

So these tests cover derivation, declared-value precedence, layer precedence and the
coverage report, rather than any single wrong value.
"""
import json
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import predicate_registry as pr  # noqa: E402
from cora_mcp import record_lookup as rl  # noqa: E402
from cora_mcp.schema_loader import get_loader  # noqa: E402


# ---------------------------------------------------------------------------
# 1 — entity aliases are derived, not transcribed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("alias,slug", [
    ("incident", "itsm_incident"),
    ("incidents", "itsm_incident"),
    ("change", "itsm_change"),
    ("changes", "itsm_change"),
    ("problem", "itsm_problem"),
    ("problems", "itsm_problem"),
    ("release", "itsm_release"),
    ("releases", "itsm_release"),
    ("availability", "itsm_availability"),
    ("service request", "itsm_service_request"),
    ("service requests", "itsm_service_request"),
    ("major incident", "itsm_major_incidents"),
    ("major incidents", "itsm_major_incidents"),
])
def test_obvious_entity_aliases_are_derived_from_the_schema(alias, slug):
    """None of these appear in record_prefixes.json any more — they follow from the
    entity name, so they can neither be forgotten nor typo'd."""
    assert rl._derived_entity_aliases().get(alias) == slug


def test_derived_aliases_cover_every_entity():
    derived = set(rl._derived_entity_aliases().values())
    assert derived == set(get_loader().entity_slugs())


def test_plural_handles_the_y_case():
    """'availability' -> 'availabilities', not 'availabilitys'."""
    assert rl._plural("availability") == "availabilities"
    assert rl._plural("incident") == "incidents"
    assert rl._plural("status") == "statuses"


def test_overlay_only_holds_non_derivable_vocabulary():
    """The file must not restate what derivation produces — a restated alias is a second
    source of truth and the one that can go stale."""
    doc = json.load(open(os.path.join(_ROOT, "record_prefixes.json"), encoding="utf-8"))
    derived = rl._derived_entity_aliases()
    restated = {a: s for a, s in (doc.get("entity_aliases") or {}).items()
                if a in derived}
    assert not restated, "record_prefixes.json restates derived aliases: %s" % restated


def test_registry_merges_derived_and_overlay():
    reg = rl.RecordRegistry()
    assert reg.entity_for_alias("incidents") == "itsm_incident"     # derived
    assert reg.entity_for_alias("ritm") == "itsm_service_request"   # overlay
    assert reg.entity_for_alias("downtime") == "itsm_availability"  # overlay


# ---------------------------------------------------------------------------
# 2 — the declared id column beats the heuristic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("slug,expected", [
    ("itsm_release", "release_number"),
    ("itsm_service_request", "request_item_id"),
    ("itsm_incident", "incident_id"),
    ("itsm_problem", "problem_id"),
    ("itsm_change", "change_id"),
])
def test_entity_id_column_prefers_the_declared_value(slug, expected):
    assert rl._entity_id_column(slug) == expected


def test_the_heuristic_alone_is_wrong_for_release_and_service_request():
    """Pins WHY the declared value is needed, so nobody deletes id_column from
    record_prefixes.json as redundant — it is not."""
    assert rl._human_id_column("itsm_release") == "change_id"
    assert rl._human_id_column("itsm_service_request") == "first_task_id"


def test_declared_id_column_is_accepted_despite_its_schema_role():
    """release_number is declared role=dimension, not identifier. The identifier-list
    check in _human_id_column therefore rejects it, which is the bug."""
    detail = get_loader().entity_detail("itsm_release")
    assert "release_number" not in detail["identifiers"]      # the trap
    assert rl._entity_id_column("itsm_release") == "release_number"   # handled anyway


# ---------------------------------------------------------------------------
# 3 — predicate layering
# ---------------------------------------------------------------------------
def _write(tmp_path, name, predicates):
    p = tmp_path / name
    p.write_text(json.dumps({"predicates": predicates}), encoding="utf-8")
    return str(p)


_GEN = {
    "status_name_closed": {
        "synonyms": ["closed", "closed thing"], "entity": "x", "grain_key": "k",
        "bindings": [{"table": "t", "conditions": [
            {"field": "status_name", "op": "=", "values": ["CLOSED"]}]}],
    },
}


def test_curated_entry_replaces_a_generated_one_of_the_same_name(tmp_path):
    curated = {"status_name_closed": {
        "synonyms": ["shut"], "entity": "y", "grain_key": "k2", "bindings": []}}
    reg = pr.PredicateRegistry(_write(tmp_path, "c.json", curated),
                              generated_path=_write(tmp_path, "g.json", _GEN))
    assert reg.get("status_name_closed").entity == "y"
    assert not reg.is_generated("status_name_closed")


def test_curated_synonym_takes_the_term_from_a_generated_predicate(tmp_path):
    curated = {"closed_incident": {
        "synonyms": ["closed"], "entity": "incident", "grain_key": "incident_id",
        "bindings": []}}
    reg = pr.PredicateRegistry(_write(tmp_path, "c.json", curated),
                              generated_path=_write(tmp_path, "g.json", _GEN))
    assert reg.resolve("closed").name == "closed_incident"      # curated wins
    assert reg.resolve("closed thing").name == "status_name_closed"  # rest survives
    assert reg.shadowed_terms()["closed"] == ("status_name_closed", "closed_incident")


def test_a_generated_collision_never_raises(tmp_path):
    """Machine output over hundreds of values must not be able to stop startup — unlike
    the curated file, where a duplicate is an authoring error worth failing on."""
    gen = {
        "a_x": {"synonyms": ["dup"], "entity": "e", "grain_key": "k", "bindings": []},
        "b_x": {"synonyms": ["dup"], "entity": "e", "grain_key": "k", "bindings": []},
    }
    reg = pr.PredicateRegistry(_write(tmp_path, "c.json", {}),
                               generated_path=_write(tmp_path, "g.json", gen))
    assert reg.resolve("dup") is not None


def test_curated_duplicate_still_raises(tmp_path):
    bad = {"a": {"synonyms": ["dup"], "entity": "x", "grain_key": "k", "bindings": []},
           "b": {"synonyms": ["dup"], "entity": "x", "grain_key": "k", "bindings": []}}
    with pytest.raises(pr.PredicateConfigError):
        pr.PredicateRegistry(_write(tmp_path, "c.json", bad), generated_path=None)


def test_missing_generated_file_is_normal(tmp_path):
    reg = pr.PredicateRegistry(generated_path=str(tmp_path / "absent.json"))
    assert len(reg.names()) >= 23
    assert reg.curated_names() == reg.names()


# ---------------------------------------------------------------------------
# 4 — the coverage report
# ---------------------------------------------------------------------------
def test_coverage_reports_every_declared_table():
    rep = pr.coverage()
    assert rep["table_count"] == len(
        [t for _m, _s, e in get_loader().all_entities() for t in (e.get("tables") or [])])
    assert rep["covered_tables"] >= 11


def test_coverage_names_the_uncovered_tables():
    rep = pr.coverage()
    uncovered = set(rep["uncovered_tables"])
    # a join table legitimately has none; the point is that it is REPORTED, because the
    # gap being invisible is what let a question route to the wrong population
    assert "itsm.tbl_incident_change_relation" in uncovered
    assert "itsm_availability.tbl_tableau_outagesv4" not in uncovered


def test_scope_shape_excludes_breakdown_columns():
    """The generator's gate must not treat a breakdown as a scope: sector/region/vendor
    are filters (filter_aliases.json + column_resolver), and making them predicates would
    turn 'support' and 'analytics' into scope phrases."""
    from tools.gen_predicates import _is_scope_column
    for scope in ("status_name", "state", "type_description", "risk_description",
                  "methodology", "closure_code", "outage_type",
                  "major_incident_indicator", "sla_breached_indicator"):
        assert _is_scope_column(scope), scope
    for breakdown in ("business_name", "sub_business_name", "region_name", "country",
                      "city", "it_vendor_name", "assignment_group_name",
                      "configuration_item_name", "category_description",
                      "active_indicator_type"):
        assert not _is_scope_column(breakdown), breakdown


def test_every_curated_predicate_binds_only_scope_columns():
    """Sanity check on the gate itself: if a curated predicate binds a column the gate
    calls a breakdown, one of the two is wrong and worth looking at."""
    from tools.gen_predicates import _is_scope_column
    reg = pr.PredicateRegistry(generated_path=None)
    off = []
    for name in reg.curated_names():
        for b in reg.get(name).bindings():
            for c in b.conditions:
                if not _is_scope_column(c["field"]):
                    off.append("%s: %s" % (name, c["field"]))
    assert not off, "curated predicates bind non-scope columns: %s" % off