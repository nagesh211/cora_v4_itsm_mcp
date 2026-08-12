"""Regression tests for the availability-vs-incident routing bug, and for the
run_kpi(mode='records') fix that replaced compose_metric/predicate_registry.

"Give me Incident Ids which impacted availability percentage in last month" once
answered from ``itsm_incident.tbl_major_incidents`` — the major-incident list, a
different population from the one the ``availability-percentage`` KPI measures.
Two independent defects lined up, both pinned here:

  1. the entity-alias registry declared no alias for the availability entity, so
     :func:`cora_mcp.adhoc._identify_entities` matched only "incident" and the ad-hoc
     planner could never reach ``itsm_availability`` at all.
  2. An alias pointing at a slug that does not exist is dropped *silently* (the
     function filters on ``loader.all_entities()``), which is how
     ``itsm_servicerequest`` — the real slug is ``itsm_service_request`` — disabled
     every service-request plan without anyone noticing.

A related, later bug: "outages last month" (aggregated) and "detailed report of
the outages last month" resolved to two DIFFERENT populations, because the
"detailed" phrasing routed to compose_metric with a bare `outage` predicate (no
business-rule filters) instead of the governed KPI's own curated SQL
(business_criticality/NON-IT/hypercare exclusions). compose_metric and
predicate_registry are retired; run_kpi(mode='records') replaces that path by
reusing the SAME curated WHERE as the KPI's own aggregate modes — proven below.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp.adhoc import _identify_entities  # noqa: E402
from cora_mcp.record_lookup import RecordRegistry  # noqa: E402
from cora_mcp.schema_loader import get_loader  # noqa: E402
from cora_mcp import query_engine as qe  # noqa: E402

_THE_QUESTION = ("Show me the Incident IDs of incidents that impacted the "
                 "availability percentage during last month.")

# A trimmed but representative stand-in for the real
# availability-availability-outage-count KPI config: SQL-mode, with the same
# curated business-rule scoping (most-critical only, no NON-IT, no hypercare)
# repeated in both the aggregate base_query and the records-mode detail_query.
_WHERE = ("outage_type = 'OUTAGE' AND business_criticality_value = "
          "'1 - most critical' AND hypercare_project_system_id IS NULL")
_AVAILABILITY_KPI = {
    "name": "availability-availability-outage-count",
    "title": "Availability Outage Count Widget",
    "module": "availability",
    "execution_mode": "SQL",
    "source": {"dialect": "postgres"},
    "primary_dataset": {"schema": "itsm_availability", "table": "tbl_tableau_outagesv4"},
    "fields": {},
    "filters": {"allowed": []},
    "sql": {
        "base_query": (
            "select count(distinct incident_id) as v "
            "from itsm_availability.tbl_tableau_outagesv4 a "
            "where the_date_time between '{from_date}' and '{to_date}' "
            "and " + _WHERE + " {filters}"),
        "detail_query": (
            "select a.incident_id, a.assignment_group_name "
            "from itsm_availability.tbl_tableau_outagesv4 a "
            "where the_date_time between '{from_date}' and '{to_date}' "
            "and " + _WHERE + " {filters}"),
    },
}


# ---------------------------------------------------------------------------
# 1 + 2 — entity aliases
# ---------------------------------------------------------------------------
def test_every_entity_alias_points_at_a_real_slug():
    """The guard that was missing: a typo'd slug is silently dropped, not raised, so
    nothing but a test can catch it."""
    valid = set(get_loader().entity_slugs())
    dangling = {alias: slug for alias, slug in RecordRegistry()._entity_aliases.items()
                if slug not in valid}
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
# 3 — run_kpi(mode='records') reuses the KPI's own curated WHERE
# ---------------------------------------------------------------------------
class _FakeCatalog:
    def __init__(self, cfg):
        self._cfg = cfg

    async def get(self, name):
        return self._cfg if name == self._cfg["name"] else None

    async def search(self, query, limit=5, **kw):
        return []


@pytest.mark.asyncio
async def test_records_mode_applies_the_same_curated_where_as_the_aggregate(monkeypatch):
    """The bug itself, fixed: 'how many' (mode='stat') and 'which ones'
    (mode='records') must scope to the SAME population — the criticality/NON-IT/
    hypercare conditions appear in the records SQL exactly as in the aggregate."""
    monkeypatch.setattr(qe, "get_catalog", lambda: _FakeCatalog(_AVAILABILITY_KPI))

    agg = await qe.generate_query(
        "availability-availability-outage-count",
        from_date="2026-07-01", to_date="2026-07-31", mode="stat")
    records = await qe.generate_query(
        "availability-availability-outage-count",
        from_date="2026-07-01", to_date="2026-07-31",
        mode="records", limit=200)

    agg_sql = agg["results"][0]["sql"]
    rec_sql = records["results"][0]["sql"]
    assert _WHERE in agg_sql
    assert _WHERE in rec_sql, "records mode dropped the KPI's own business-rule scoping"
    assert "group by" not in rec_sql.lower(), "records mode must not aggregate"
    assert "limit 200" in rec_sql.lower()


@pytest.mark.asyncio
async def test_records_mode_fails_closed_without_an_authored_detail_query(monkeypatch):
    """No generic fallback exists for a SQL-mode KPI's raw listing — unlike
    compose_metric's predicate layer, which could re-derive (and get wrong) a
    business-rule WHERE nobody actually authored for that shape."""
    cfg = dict(_AVAILABILITY_KPI)
    cfg["sql"] = {"base_query": _AVAILABILITY_KPI["sql"]["base_query"]}   # no detail_query
    monkeypatch.setattr(qe, "get_catalog", lambda: _FakeCatalog(cfg))

    with pytest.raises(qe.QueryError, match="no authored sql.detail_query"):
        await qe.generate_query(cfg["name"], from_date="2026-07-01",
                                to_date="2026-07-31", mode="records")


@pytest.mark.asyncio
async def test_records_mode_rejects_dim_and_comparison(monkeypatch):
    monkeypatch.setattr(qe, "get_catalog", lambda: _FakeCatalog(_AVAILABILITY_KPI))
    with pytest.raises(qe.QueryError, match="doesn't take"):
        await qe.generate_query(_AVAILABILITY_KPI["name"], mode="records", dim="priority")
