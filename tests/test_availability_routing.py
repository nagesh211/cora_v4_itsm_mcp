"""Regression tests for the availability-vs-incident routing bug.

"Give me Incident Ids which impacted availability percentage in last month" answered
from ``itsm_incident.tbl_major_incidents`` — the major-incident list, a different
population from the one the ``availability-percentage`` KPI measures. Three independent
defects lined up, and each is pinned here:

  1. ``record_prefixes.json`` declared no alias for the availability entity, so
     :func:`cora_mcp.adhoc._identify_entities` matched only "incident" and the ad-hoc
     planner could never reach ``itsm_availability`` at all.
  2. An alias pointing at a slug that does not exist is dropped *silently* (the
     function filters on ``loader.all_entities()``), which is how
     ``itsm_servicerequest`` — the real slug is ``itsm_service_request`` — disabled
     every service-request plan without anyone noticing.
  3. ``predicates.json`` had no availability predicate, so the only qualifier the agent
     could find for "incidents" was ``major_incident`` and it substituted it.
"""
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import composer  # noqa: E402
from cora_mcp.adhoc import _identify_entities  # noqa: E402
from cora_mcp.predicate_registry import PredicateRegistry  # noqa: E402
from cora_mcp.record_lookup import RecordRegistry  # noqa: E402
from cora_mcp.schema_loader import get_loader  # noqa: E402

_OUTAGES = "itsm_availability.tbl_tableau_outagesv4"
_MAJOR = "itsm_incident.tbl_major_incidents"
_THE_QUESTION = ("Show me the Incident IDs of incidents that impacted the "
                 "availability percentage during last month.")


# ---------------------------------------------------------------------------
# 1 + 2 — entity aliases
# ---------------------------------------------------------------------------
def test_every_entity_alias_points_at_a_real_slug():
    """The guard that was missing: a typo'd slug is silently dropped, not raised, so
    nothing but a test can catch it."""
    valid = set(get_loader().entity_slugs())
    dangling = {alias: slug for alias, slug in RecordRegistry()._entity_aliases.items()
                if slug not in valid}
    # itsm_servicedesk has no entity in this deployment's schema; it stays declared for
    # deployments that do ship one, so it is the one accepted exception.
    dangling = {a: s for a, s in dangling.items() if s != "itsm_servicedesk"}
    assert not dangling, "entity_aliases point at non-existent slugs: %s" % dangling


def test_availability_question_routes_to_the_availability_entity():
    ranked = _identify_entities(_THE_QUESTION, None, 4)
    slugs = [c["slug"] for c in ranked]
    assert slugs, "no candidate entity — the availability question is unplannable"
    # "availability percentage" is a longer, more specific phrase than "incident", so
    # it must outrank it rather than merely appear somewhere in the list.
    assert slugs[0] == "itsm_availability", ranked


def test_service_request_question_is_plannable():
    """Fell to zero candidates while the alias pointed at itsm_servicerequest."""
    slugs = [c["slug"] for c in _identify_entities("list service requests last month",
                                                   None, 4)]
    assert "itsm_service_request" in slugs


# ---------------------------------------------------------------------------
# 3 — the predicate and the table it anchors on
# ---------------------------------------------------------------------------
def test_availability_impacting_resolves_from_the_users_own_words():
    reg = PredicateRegistry()
    for phrase in ("impacted availability", "availability impacting",
                   "impacted availability percentage", "caused downtime"):
        hit = reg.resolve(phrase)
        assert hit is not None and hit.name == "availability_impacting", phrase


def test_availability_impacting_scopes_to_the_kpis_own_population():
    """outage_type='OUTAGE' alone is not enough: without it the table's service-day
    rows (outage_type IS NULL) are counted as outages, and without the criticality
    condition the listing is not the population the KPI measures."""
    b = PredicateRegistry().get("availability_impacting").binding_for(_OUTAGES)
    assert b is not None and not b.is_free
    assert {(c["field"], tuple(c["values"])) for c in b.conditions} == {
        ("outage_type", ("OUTAGE",)),
        ("business_criticality_value", ("1 - most critical",)),
    }


def test_generic_outage_predicate_does_not_inherit_the_criticality_scope():
    """"How many outages last month" must not silently narrow to most-critical
    services just because the availability KPI does."""
    b = PredicateRegistry().get("outage").binding_for(_OUTAGES)
    assert [c["field"] for c in b.conditions] == ["outage_type"]


def test_incident_ids_impacting_availability_anchor_on_outages_not_major_incidents():
    """The bug itself, at the layer that chose the wrong table."""
    plan = composer.plan(predicates=["availability_impacting"],
                         select=["incident_id"], period="last month")
    assert plan["anchor_table"] == _OUTAGES
    assert plan["anchor_table"] != _MAJOR
    assert plan["entity"] == "availability"
    assert plan["shape"] == "listing"
    assert plan["spec"]["drilldown"]["detail_columns"] == ["incident_id"]
    assert plan["spec"]["date_field"] == "the_date"


def test_availability_listing_declares_that_it_is_a_superset():
    """The predicate cannot reproduce the KPI's hypercare / non-IT exclusions (no IS
    NULL op, no subquery op). Presenting the listing as exact would be the same class
    of quiet error as answering with the wrong table, so the caveat must reach the
    caller — not sit unread in the binding's maintainer note."""
    plan = composer.plan(predicates=["availability_impacting"], select=["incident_id"],
                         period="last month")
    caveats = [n for n in plan["notes"] if n.startswith("availability_impacting:")]
    assert len(caveats) == 1, plan["notes"]
    assert "superset" in caveats[0]


def test_a_predicate_with_no_caveat_adds_no_note():
    plan = composer.plan(predicates=["major_incident"], select=["incident_id"],
                         period="last month")
    assert not [n for n in plan["notes"] if n.startswith("major_incident:")]