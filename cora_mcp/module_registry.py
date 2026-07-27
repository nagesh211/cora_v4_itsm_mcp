"""Module vocabulary derived from the KPI index — never hardcoded.

Each deployment points at its own OpenSearch index holding its own KPI configs,
and those configs use their own module vocabulary. One site's configs carry
two-letter codes (``am``, ``cm``, ``im``…); another's carry surface names
(``changes``, ``incidents``, ``incops``…). The same build serves both, so
nothing here may name either set: every module fact is read at runtime from
whichever index is configured.

Two sources, both in OpenSearch:

  1. **The KPI config index** — authoritative for *which* modules exist. A terms
     aggregation on ``module`` yields the codes; a scan of each KPI's
     ``name``/``synonyms`` yields the per-metric routing vocabulary. Adding a KPI
     for a brand-new module makes that module appear with no configuration at all.
  2. **An optional module-meta index** (``OPENSEARCH_MODULE_META_INDEX``, default
     ``cora-module-meta``) — one small doc per module carrying the human label and
     the curated module-level aliases that no single metric mentions ("cab",
     "help desk", "incident operations")::

         {"code": "incops", "label": "Incident Operations",
          "aliases": ["incident operations", "incops", "inc ops"]}

Source 2 is entirely optional. A module with no meta doc gets a humanized label
and its own code (plus the obvious singular/plural variant) as aliases, so it
routes on metric vocabulary alone and simply gets sharper as you curate it.

This replaces four hardcoded things: ``cora_mcp/module_catalog.json``,
``module_router._phrases_by_module``, and ``query_engine._MODULE_LABELS`` /
``_MODULE_SYNONYMS``.

Caching is TTL-based (``CORA_MODULE_CACHE_TTL``, default 300s) rather than
``lru_cache``, so a module added to the index becomes visible without a restart.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from cora_mcp import opensearch_client as osc
from cora_mcp.filter_aliases import normalize
from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

DEFAULT_META_INDEX = "cora-module-meta"
DEFAULT_TTL_SECONDS = 300

# A curated module-level alias is a far stronger signal than one metric's
# synonym: sibling modules often share metric vocabulary verbatim (two modules
# can both own an "MTTR (Hrs)" KPI with identical synonyms) and differ only in
# what the module itself is called. Weighting aliases higher is what lets those
# siblings be told apart at all.
ALIAS_WEIGHT = 3
METRIC_WEIGHT = 1


def meta_index_name() -> str:
    return os.getenv("OPENSEARCH_MODULE_META_INDEX", DEFAULT_META_INDEX)


def _ttl_seconds() -> float:
    raw = os.getenv("CORA_MODULE_CACHE_TTL")
    if raw is None:
        return float(DEFAULT_TTL_SECONDS)
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("CORA_MODULE_CACHE_TTL=%r is not a number; using %ds",
                    raw, DEFAULT_TTL_SECONDS)
        return float(DEFAULT_TTL_SECONDS)


@dataclass(frozen=True)
class ModuleInfo:
    """One module as the runtime sees it. All phrases are normalized."""
    code: str
    label: str
    aliases: Tuple[str, ...] = ()
    metric_phrases: Tuple[str, ...] = ()
    kpi_count: int = 0

    def score(self, padded_question: str) -> int:
        """Match score against a question already normalized and space-padded.
        Word-boundary safe; longer phrases count for more."""
        total = 0
        for phrase, weight in ((p, ALIAS_WEIGHT) for p in self.aliases):
            if (" %s " % phrase) in padded_question:
                total += weight * len(phrase.split())
        for phrase in self.metric_phrases:
            if (" %s " % phrase) in padded_question:
                total += METRIC_WEIGHT * len(phrase.split())
        return total


def _humanize(code: str) -> str:
    return code.replace("_", " ").replace("-", " ").strip().title() or code


def _derived_aliases(code: str) -> List[str]:
    """Fallback vocabulary for a module with no curated meta doc: the code
    itself plus the obvious singular/plural variant ("changes" <-> "change")."""
    base = normalize(code)
    out = {base}
    if base.endswith("s") and len(base) > 3:
        out.add(base[:-1])
    elif base:
        out.add(base + "s")
    out.discard("")
    return sorted(out)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
async def _module_counts(client, index: str) -> Dict[str, int]:
    """{module code -> KPI count} straight from the config index."""
    body = {"size": 0,
            "aggs": {"modules": {"terms": {"field": "module", "size": 500}}}}
    resp = await client.search(index=index, body=body)
    buckets = (resp.get("aggregations", {}).get("modules", {}) or {}).get("buckets", [])
    return {b["key"]: b.get("doc_count", 0) for b in buckets if b.get("key")}


async def _metric_phrases(client, index: str) -> Dict[str, set]:
    """{module code -> normalized metric phrases} from every KPI's name and
    synonyms. ``tags`` is deliberately skipped: it carries the module name and
    generic words ("count") that would match everything."""
    body = {"size": 1000, "query": {"match_all": {}},
            "_source": ["module", "name", "synonyms"]}
    resp = await client.search(index=index, body=body)
    out: Dict[str, set] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        code = src.get("module")
        if not code:
            continue
        phrases = out.setdefault(code, set())
        name = src.get("name")
        if name:
            phrases.add(normalize(name))   # "changes-emergency" -> "changes emergency"
        for syn in src.get("synonyms") or []:
            if syn:
                phrases.add(normalize(syn))
        phrases.discard("")
    return out


async def _curated_meta(client) -> Dict[str, dict]:
    """{code -> {label, aliases}} from the optional meta index. A missing index
    is normal (nothing curated yet) and must not break routing."""
    index = meta_index_name()
    try:
        resp = await client.search(index=index,
                                   body={"size": 500, "query": {"match_all": {}}})
    except Exception as exc:
        log.info("module meta index %r unavailable (%s); using derived labels/aliases",
                 index, exc)
        return {}
    out: Dict[str, dict] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        code = src.get("code") or hit.get("_id")
        if code:
            out[code] = src
    log.info("module meta index %r -> %d curated module(s)", index, len(out))
    return out


async def _load() -> Dict[str, ModuleInfo]:
    client = osc.get_client()
    if client is None:
        log.warning("OpenSearch client unavailable; module registry is empty")
        return {}
    index = osc.index_name()
    counts, phrases, meta = await asyncio.gather(
        _module_counts(client, index),
        _metric_phrases(client, index),
        _curated_meta(client),
    )
    # The config index decides which modules exist; a meta doc for a module with
    # no KPIs is ignored rather than advertised as routable.
    modules: Dict[str, ModuleInfo] = {}
    for code, count in sorted(counts.items()):
        m = meta.get(code) or {}
        aliases = [normalize(a) for a in (m.get("aliases") or []) if a]
        if not aliases:
            aliases = _derived_aliases(code)
        modules[code] = ModuleInfo(
            code=code,
            label=m.get("label") or _humanize(code),
            aliases=tuple(sorted({a for a in aliases if a})),
            metric_phrases=tuple(sorted(phrases.get(code, set()))),
            kpi_count=count,
        )
    log.info("module registry loaded from %s: %s", index,
             {c: m.kpi_count for c, m in modules.items()})
    return modules


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------
_cache: Dict[str, ModuleInfo] = {}
_loaded_at: float = 0.0
_lock = asyncio.Lock()


async def get_modules(force: bool = False) -> Dict[str, ModuleInfo]:
    """{code -> ModuleInfo} for the configured index, cached for
    ``CORA_MODULE_CACHE_TTL`` seconds."""
    global _cache, _loaded_at
    ttl = _ttl_seconds()
    if not force and _loaded_at and (time.monotonic() - _loaded_at) < ttl:
        return _cache
    async with _lock:
        # Another coroutine may have refreshed while we waited for the lock.
        if not force and _loaded_at and (time.monotonic() - _loaded_at) < ttl:
            return _cache
        try:
            _cache = await _load()
            _loaded_at = time.monotonic()
        except Exception as exc:
            # Serving a stale vocabulary beats failing the user's question; an
            # empty cache simply means unrouted (still correct) search.
            log.warning("module registry refresh failed (%s); keeping %d cached module(s)",
                        exc, len(_cache))
            if not _loaded_at:
                return {}
    return _cache


async def refresh_modules() -> Dict[str, ModuleInfo]:
    """Force an immediate reload (after indexing new configs)."""
    return await get_modules(force=True)


refresh = refresh_modules   # short alias for interactive use


def reset_cache() -> None:
    """Drop the cache entirely (tests)."""
    global _cache, _loaded_at
    _cache = {}
    _loaded_at = 0.0


def cached_modules() -> Dict[str, ModuleInfo]:
    """The last loaded snapshot without touching OpenSearch — empty if never
    loaded. For sync call sites where the module is only a scoring hint."""
    return _cache


# ---------------------------------------------------------------------------
# Resolution / detection
# ---------------------------------------------------------------------------
def _resolve_in(modules: Dict[str, ModuleInfo], module: str) -> Optional[str]:
    if not module:
        return None
    raw = module.strip().lower()
    if raw in modules:                                  # already a code
        return raw
    norm = normalize(module)
    for code, info in modules.items():
        if norm == normalize(code) or norm in info.aliases:
            return code
    for code, info in modules.items():                  # loose contains match
        for alias in info.aliases:
            if alias and alias in norm:
                return code
    return None


async def resolve_code(module: str) -> Optional[str]:
    """Map a code ('sd', 'incops') or a phrase ('service desk', 'change
    management') to a module code that exists in THIS deployment's index."""
    return _resolve_in(await get_modules(), module)


def resolve_code_sync(module: str) -> Optional[str]:
    """Best-effort resolution against the cached snapshot only — never touches
    OpenSearch, returns ``None`` if the registry has not been loaded yet. Use
    only where the module is an optional hint, not a correctness requirement."""
    return _resolve_in(_cache, module)


async def detect_module(question: str) -> Optional[str]:
    """Best-guess module code for a free-text question, or ``None`` when not
    confident (nothing matched, or the top two modules tie).

    The caller applies this as a *boost*, not a filter (see
    ``module_router.routing_mode``), so a wrong guess only re-ranks."""
    modules = await get_modules()
    if not modules:
        return None
    padded = " %s " % normalize(question)
    if not padded.strip():
        return None
    scores = {code: info.score(padded) for code, info in modules.items()}
    scores = {c: s for c, s in scores.items() if s}
    if not scores:
        return None
    ranked: List[Tuple[str, int]] = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
        return ranked[0][0]
    return None                                   # ambiguous -> don't route


_ROUTING_MODES = ("boost", "filter", "off")


def routing_mode() -> str:
    """How a *detected* module is applied to the KPI search.

      * ``boost``  (default) — rank that module's KPIs higher, exclude nothing.
        A misdetection can only re-order results, never hide one.
      * ``filter`` — hard ``term`` filter (retries unfiltered when it yields
        nothing).
      * ``off``    — don't infer a module at all; always search unfiltered.

    Read from ``CORA_MODULE_ROUTING`` per call, so the behaviour can be changed
    without a redeploy."""
    mode = (os.getenv("CORA_MODULE_ROUTING") or "boost").strip().lower()
    if mode not in _ROUTING_MODES:
        log.warning("CORA_MODULE_ROUTING=%r is not one of %s; using 'boost'",
                    mode, _ROUTING_MODES)
        return "boost"
    return mode


async def module_choices() -> List[Dict[str, object]]:
    """Compact listing of this deployment's modules, for MCP tools and prompts."""
    modules = await get_modules()
    return [{"code": m.code, "label": m.label, "kpi_count": m.kpi_count,
             "aliases": list(m.aliases)}
            for m in sorted(modules.values(), key=lambda x: x.code)]


async def known_codes() -> List[str]:
    return sorted(await get_modules())