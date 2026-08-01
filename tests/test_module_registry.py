"""The module vocabulary must be derived from whatever index is configured.

These tests drive the registry with two fake indexes carrying *different* module
vocabularies -- the two-letter codes one deployment's configs use, and the
surface names another's use -- and assert identical behaviour from both. Nothing
in the code may name either set. No live OpenSearch needed.
"""
import asyncio

import pytest

from cora_mcp import module_registry as reg

# ---------------------------------------------------------------------------
# Two deployments, two vocabularies, same code.
# ---------------------------------------------------------------------------
LEGACY = {
    "cm": [("emergency", ["emergency changes", "emergency cr"]),
           ("failure-rate", ["change failure rate"])],
    "im": [("im-total-opened", ["incidents opened", "tickets raised"]),
           ("mttr", ["mean time to repair", "mttr"])],
    "sd": [("sd-call-volume", ["call volume", "calls"])],
}
LEGACY_META = [
    {"code": "cm", "label": "Change Management",
     "aliases": ["change management", "changes", "change", "chg", "cab"]},
    {"code": "im", "label": "Incident Management",
     "aliases": ["incident management", "incidents", "incident", "inc"]},
    {"code": "sd", "label": "Service Desk",
     "aliases": ["service desk", "helpdesk", "help desk", "sd"]},
]

PEPOPS = {
    "changes": [("changes-emergency", ["emergency"]),
                ("changes-failure-rate-pct", ["change failure rate"])],
    # incidents and incops deliberately share metric synonyms verbatim -- this is
    # real in the pepops_bkp corpus and is the case metric vocabulary alone cannot split.
    "incidents": [("incidents-mttr-hrs", ["mean time to repair", "mttr"]),
                  ("incidents-total-created", ["incidents created"])],
    "incops": [("incops-mttr-hrs", ["mean time to repair", "mttr"]),
               ("incops-total-resolved", ["incidents resolved"])],
}
PEPOPS_META = [
    {"code": "changes", "label": "Change Management",
     "aliases": ["change management", "changes", "change", "chg", "cab"]},
    {"code": "incidents", "label": "Incidents",
     "aliases": ["incidents", "incident", "inc", "tickets"]},
    {"code": "incops", "label": "Incident Operations",
     "aliases": ["incops", "inc ops", "incident operations"]},
]


class FakeClient:
    """Answers the three queries the registry makes."""

    def __init__(self, corpus, meta, meta_fails=False):
        self.corpus, self.meta, self.meta_fails = corpus, meta, meta_fails
        self.searches = 0

    async def search(self, index, body):
        self.searches += 1
        if index == reg.meta_index_name():
            if self.meta_fails:
                raise RuntimeError("index_not_found_exception")
            return {"hits": {"hits": [{"_source": m} for m in self.meta]}}
        if body.get("size") == 0:                       # terms agg
            buckets = [{"key": c, "doc_count": len(k)} for c, k in self.corpus.items()]
            return {"aggregations": {"modules": {"buckets": buckets}}}
        hits = [{"_source": {"module": code, "name": name, "synonyms": syns}}
                for code, kpis in self.corpus.items() for name, syns in kpis]
        return {"hits": {"hits": hits}}


@pytest.fixture(autouse=True)
def _clean():
    reg.reset_cache()
    yield
    reg.reset_cache()


def _install(monkeypatch, corpus, meta, meta_fails=False):
    client = FakeClient(corpus, meta, meta_fails)
    monkeypatch.setattr(reg.osc, "get_client", lambda: client)
    monkeypatch.setattr(reg.osc, "index_name", lambda: "kpi-index")
    return client


# ---------------------------------------------------------------------------
def test_modules_derived_from_index_not_hardcoded(monkeypatch):
    """Whatever the index contains IS the vocabulary -- for either deployment."""
    _install(monkeypatch, LEGACY, LEGACY_META)
    assert asyncio.run(reg.known_codes()) == ["cm", "im", "sd"]

    reg.reset_cache()
    _install(monkeypatch, PEPOPS, PEPOPS_META)
    assert asyncio.run(reg.known_codes()) == ["changes", "incidents", "incops"]


def test_kpi_counts_and_labels(monkeypatch):
    _install(monkeypatch, PEPOPS, PEPOPS_META)
    mods = asyncio.run(reg.get_modules())
    assert mods["incops"].label == "Incident Operations"
    assert mods["changes"].kpi_count == 2


@pytest.mark.parametrize("phrase,expected", [
    ("cm", "cm"), ("change management", "cm"), ("changes", "cm"),
    ("service desk", "sd"), ("helpdesk", "sd"),
    ("incidents", "im"), ("nonsense", None),
])
def test_resolve_code_legacy(monkeypatch, phrase, expected):
    _install(monkeypatch, LEGACY, LEGACY_META)
    assert asyncio.run(reg.resolve_code(phrase)) == expected


@pytest.mark.parametrize("phrase,expected", [
    ("changes", "changes"), ("change management", "changes"),
    ("incops", "incops"), ("incident operations", "incops"),
    ("incidents", "incidents"), ("nonsense", None),
])
def test_resolve_code_pepops(monkeypatch, phrase, expected):
    """The exact phrases that raised 'unknown module' before -- incops has no
    entry in any hardcoded map."""
    _install(monkeypatch, PEPOPS, PEPOPS_META)
    assert asyncio.run(reg.resolve_code(phrase)) == expected


def test_detect_module_uses_metric_vocabulary(monkeypatch):
    _install(monkeypatch, PEPOPS, PEPOPS_META)
    assert asyncio.run(reg.detect_module("what is the change failure rate")) == "changes"


def test_alias_outweighs_shared_metric_synonyms(monkeypatch):
    """incidents and incops own byte-identical MTTR synonyms; only the curated
    module alias can separate them. Bare 'mttr' stays ambiguous -> no routing."""
    _install(monkeypatch, PEPOPS, PEPOPS_META)
    assert asyncio.run(reg.detect_module("mttr for incops")) == "incops"
    assert asyncio.run(reg.detect_module("mttr for incidents")) == "incidents"
    assert asyncio.run(reg.detect_module("what is mttr")) is None


def test_missing_meta_index_falls_back_to_derived(monkeypatch):
    """No curated metadata at all: labels/aliases derive from the code, and
    routing still works off the code and its singular/plural variant."""
    _install(monkeypatch, PEPOPS, [], meta_fails=True)
    mods = asyncio.run(reg.get_modules())
    assert set(mods) == {"changes", "incidents", "incops"}
    assert mods["incops"].label == "Incops"
    assert asyncio.run(reg.resolve_code("change")) == "changes"   # plural folded
    assert asyncio.run(reg.detect_module("incops numbers")) == "incops"


def test_cache_is_ttl_bounded_and_refreshable(monkeypatch):
    client = _install(monkeypatch, PEPOPS, PEPOPS_META)
    asyncio.run(reg.get_modules())
    calls = client.searches
    asyncio.run(reg.get_modules())
    assert client.searches == calls, "second call must be served from cache"
    asyncio.run(reg.refresh_modules())
    assert client.searches > calls, "refresh must re-query"


def test_ttl_zero_always_reloads(monkeypatch):
    monkeypatch.setenv("CORA_MODULE_CACHE_TTL", "0")
    client = _install(monkeypatch, PEPOPS, PEPOPS_META)
    asyncio.run(reg.get_modules())
    calls = client.searches
    asyncio.run(reg.get_modules())
    assert client.searches > calls


def test_resolve_code_sync_is_cold_cache_safe(monkeypatch):
    _install(monkeypatch, PEPOPS, PEPOPS_META)
    assert reg.resolve_code_sync("changes") is None      # never loaded
    asyncio.run(reg.get_modules())
    assert reg.resolve_code_sync("changes") == "changes"


def test_no_opensearch_client_degrades_quietly(monkeypatch):
    monkeypatch.setattr(reg.osc, "get_client", lambda: None)
    assert asyncio.run(reg.get_modules()) == {}
    assert asyncio.run(reg.detect_module("emergency changes")) is None
    assert asyncio.run(reg.resolve_code("changes")) is None


def test_routing_mode_env(monkeypatch):
    monkeypatch.delenv("CORA_MODULE_ROUTING", raising=False)
    assert reg.routing_mode() == "boost"
    for value in ("filter", "off", "BOOST"):
        monkeypatch.setenv("CORA_MODULE_ROUTING", value)
        assert reg.routing_mode() == value.lower()
    monkeypatch.setenv("CORA_MODULE_ROUTING", "garbage")
    assert reg.routing_mode() == "boost"