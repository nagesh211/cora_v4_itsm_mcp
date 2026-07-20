"""Tests for user-facing filter-alias resolution (filter_aliases.json).

An alias ("business", "p&l", "team") must resolve deterministically to the KPI's
canonical filter key, fold spacing/case, stay disambiguated ("sub_business" is
NOT "business"), and fail loud for unknown / unavailable terms.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp.filter_aliases import AliasRegistry, get_registry, normalize  # noqa: E402
from cora_mcp.kpi_catalog import get_catalog  # noqa: E402
from cora_mcp.query_engine import (  # noqa: E402
    QueryError, generate_query, resolve_filter_key, try_resolve_filter_key,
)

CAT = get_catalog()


def test_registry_loads_without_alias_conflicts():
    reg = AliasRegistry()          # would raise AliasConfigError on a dup alias
    assert reg.canonical_for("business") == "sector"
    assert reg.canonical_for("division") == "division"


def test_normalize_folds_case_space_underscore_hyphen():
    assert normalize("Service Area") == normalize("service_area") == normalize("service-area")
    reg = get_registry()
    assert reg.canonical_for("Service-Area") == "service_area"
    assert reg.canonical_for("P&L") == "sector"          # punctuation preserved
    assert reg.canonical_for("RISK TYPE") == "risk"


def test_business_resolves_to_sector_not_division():
    cfg = CAT.get("availability-percentage")
    assert resolve_filter_key(cfg, "business") == "sector"
    assert resolve_filter_key(cfg, "p&l") == "sector"
    # the ambiguous-looking sibling stays distinct (full-string, not substring)
    assert resolve_filter_key(cfg, "sub_business") == "division"
    assert resolve_filter_key(cfg, "division") == "division"


def test_canonical_key_is_accepted_backcompat():
    cfg = CAT.get("availability-percentage")
    assert resolve_filter_key(cfg, "sector") == "sector"
    assert resolve_filter_key(cfg, "region") == "region"


def test_team_maps_to_assignment_group():
    cfg = CAT.get("avg-sla-resolution")           # allows assignment_group
    assert resolve_filter_key(cfg, "team") == "assignment_group"
    assert resolve_filter_key(cfg, "owner group") == "assignment_group"


def test_unknown_term_fails_loud():
    cfg = CAT.get("availability-percentage")
    with pytest.raises(QueryError):
        resolve_filter_key(cfg, "totally_made_up")


def test_alias_for_filter_not_on_this_kpi_is_rejected():
    # 'risk' is a real filter, but availability doesn't expose it -> reject, not guess.
    cfg = CAT.get("availability-percentage")
    with pytest.raises(QueryError):
        resolve_filter_key(cfg, "risk")
    assert try_resolve_filter_key(cfg, "risk") is None


def test_alias_flows_into_generated_sql():
    # SQL-mode: 'business' -> sector -> business_name (&&, array_val)
    out = generate_query("availability-percentage", filters={"business": "ADMINISTRATION"})
    assert list(out["filters"]) == ["sector"]
    assert ("EXISTS (SELECT 1 FROM unnest(business_name) AS _e WHERE lower(_e::text) = ANY(ARRAY['administration']))"
            in out["results"][0]["preview"])

    # 'division' -> sub_business_name
    out2 = generate_query("availability-percentage", filters={"division": "ADMINISTRATION"})
    assert list(out2["filters"]) == ["division"]
    assert ("EXISTS (SELECT 1 FROM unnest(sub_business_name) AS _e WHERE lower(_e::text) = ANY(ARRAY['administration']))"
            in out2["results"][0]["preview"])


def test_summary_advertises_aliases():
    s = CAT.summary("availability-percentage")
    assert s["filter_aliases"]["sector"] == ["business", "p&l"]
    assert "capability" in s["filter_aliases"]["service_area"]