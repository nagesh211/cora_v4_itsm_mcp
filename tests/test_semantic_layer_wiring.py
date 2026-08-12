"""Tests for the entity-identity metadata declared directly on each entity in
``schema_v3.yaml`` (``id_prefix`` / ``id_column`` / ``aliases``).

An audit of the registries that used to hold this data (``record_prefixes.json``,
since folded into schema_v3.yaml) found two classes of defect that shared one
cause — a fact was hand-transcribed where it could have been derived or
declared, and nothing reported the mistake:

  1. an ``entity_aliases`` slug that does not exist is DROPPED silently by
     ``_identify_entities`` (it filters on ``loader.all_entities()``), so
     ``itsm_servicerequest`` disabled every service-request question and a missing
     ``availability`` entry sent an availability question to the major-incident table
  2. the entity id column was *derived* by a heuristic that returns the first non-system
     ``*_id`` — ``change_id`` for ``itsm_release``, ``first_task_id`` for
     ``itsm_service_request``, both wrong — while the entity's declared ``id_column``
     in ``schema_v3.yaml`` gives the right value all along

So these tests cover derivation and declared-value precedence, rather than any
single wrong value. Declaring both directly on the entity (instead of a separate
prefix-mapping file) also means a prefix/alias can never point at a slug that
doesn't exist — it's read off the same entity it names.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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
    """None of these need declaring on the entity in schema_v3.yaml — they follow
    from the entity name, so they can neither be forgotten nor typo'd."""
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
    """Each entity's declared `aliases` in schema_v3.yaml must not restate what
    derivation produces — a restated alias is a second source of truth and the one
    that can go stale."""
    overlay = get_loader().declared_entity_aliases()
    derived = rl._derived_entity_aliases()
    restated = {a: s for a, s in overlay.items() if a in derived}
    assert not restated, "schema_v3.yaml aliases restate derived aliases: %s" % restated


def test_registry_merges_derived_and_overlay():
    reg = rl.RecordRegistry()
    assert reg.entity_for_alias("incidents") == "itsm_incident"     # derived
    assert reg.entity_for_alias("ritm") == "itsm_service_request"   # overlay
    assert reg.entity_for_alias("downtime") == "itsm_availability"  # overlay


def test_prefix_map_points_at_real_entities():
    """A prefix is read directly off the entity that declares it, so it can never
    reference a slug absent from the schema — structurally, not by convention."""
    valid = set(get_loader().entity_slugs())
    for pfx, slug in get_loader().id_prefix_map().items():
        assert slug in valid, "prefix %r -> unknown entity %r" % (pfx, slug)


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
    """Pins WHY the declared value is needed, so nobody deletes `id_column` from
    schema_v3.yaml as redundant — it is not."""
    assert rl._human_id_column("itsm_release") == "change_id"
    assert rl._human_id_column("itsm_service_request") == "first_task_id"


def test_declared_id_column_is_accepted_despite_its_schema_role():
    """release_number is declared role=dimension, not identifier. The identifier-list
    check in _human_id_column therefore rejects it, which is the bug."""
    detail = get_loader().entity_detail("itsm_release")
    assert "release_number" not in detail["identifiers"]      # the trap
    assert rl._entity_id_column("itsm_release") == "release_number"   # handled anyway
