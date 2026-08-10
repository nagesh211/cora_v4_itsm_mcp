"""Loads and searches the KPI configs.

Each config is a KPI definition consumed by ``gen_query.py``. This module exposes
:class:`KpiCatalog` with a small, stable surface (``get``, ``search``, ``summary``,
``by_module``, ``names``, ``configs_for_tables``) that the MCP layer relies on.

The only backend is :class:`OpenSearchBackend`, which treats an OpenSearch index as
the source of truth: BM25 ``search``, get-by-``_id`` for ``get``, term queries for
``by_module`` / ``names`` / ``configs_for_tables``. **Stateless — no config is cached
in RAM**, so a KPI added/edited in the index is live on the next request. OpenSearch
must be configured and reachable (see :mod:`cora_mcp.opensearch_client`); there is no
disk fallback.
"""
from __future__ import annotations

import json
import os
import time
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from cora_mcp import opensearch_client as osc
from cora_mcp.logging_config import get_logger

log = get_logger(__name__)


def _module_boost() -> float:
    """How hard an *inferred* module tilts the ranking (see ``search_scored``).
    Env-tunable so it can be adjusted without a redeploy."""
    try:
        return float(os.getenv("CORA_MODULE_BOOST", "4"))
    except ValueError:
        log.warning("CORA_MODULE_BOOST=%r is not a number; using 4",
                    os.getenv("CORA_MODULE_BOOST"))
        return 4.0


def _dump(obj) -> str:
    """Compact JSON for logging an OpenSearch request body (never raises)."""
    try:
        return json.dumps(obj, default=str, separators=(",", ":"))
    except Exception:  # pragma: no cover - defensive
        return repr(obj)


def _filter_alias_map(allowed: List[str]) -> Dict[str, List[str]]:
    """{allowed filter key -> accepted aliases}, so the summary advertises the
    exact vocabulary the resolver understands. Empty list = only the key name."""
    from cora_mcp.filter_aliases import get_registry
    reg = get_registry()
    return {k: reg.aliases_of(k) for k in (allowed or [])}


def summary_from_config(cfg: dict) -> dict:
    """Build the compact KPI summary the MCP tools return, from a full config dict."""
    nl = cfg.get("nl") or {}
    allowed = (cfg.get("filters") or {}).get("allowed", [])
    return {
        "name": cfg.get("name"),
        "module": cfg.get("module"),
        "title": cfg.get("title"),
        "unit": cfg.get("unit"),
        "execution_mode": cfg.get("execution_mode"),
        "allowed_filters": allowed,
        # Accepted user-facing aliases per filter, so a question can say
        # "business"/"p&l" (-> sector) or "team" (-> assignment_group) and be
        # resolved deterministically. Pass any of these as a `filters` key.
        "filter_aliases": _filter_alias_map(allowed),
        # Curated dims plus any schema 'dimension' column on the primary table the
        # config itself never declared (query_engine.available_group_by_terms mirrors
        # the schema fallback dim resolution already honours for a named dim=).
        "drilldown_dimensions": osc.available_group_by_terms(cfg),
        "sample_questions": nl.get("sample_questions", []),
    }


# ===========================================================================
# Backend
# ===========================================================================
class OpenSearchBackend:
    """Stateless **async** OpenSearch-backed store. The OpenSearch index is the
    single source of truth; any OpenSearch error propagates to the caller."""

    def __init__(self, client, index: str):
        self._client = client
        self._index = index

    # -- helpers -----------------------------------------------------------
    async def _scan_names(self, query_body: dict, op: str) -> List[str]:
        body = {**query_body, "_source": ["name"], "size": 1000}
        log.info("executing on %s/opensearch [%s]: %s", self._index, op, _dump(body))
        t0 = time.perf_counter()
        resp = await self._client.search(index=self._index, body=body)
        out = []
        for h in resp.get("hits", {}).get("hits", []):
            nm = (h.get("_source") or {}).get("name") or h.get("_id")
            if nm:
                out.append(nm)
        log.info("executing on %s/opensearch [%s] -> %d name(s) %s in %.1fms",
                 self._index, op, len(out), out, (time.perf_counter() - t0) * 1000)
        return out

    # -- API ---------------------------------------------------------------
    async def get(self, name: str) -> Optional[dict]:
        log.info("executing on %s/opensearch [GET]: id=%s", self._index, name)
        t0 = time.perf_counter()
        try:
            resp = await self._client.get(index=self._index, id=name)
        except Exception as exc:
            # NotFoundError (unindexed KPI) or a transport error.
            log.info("executing on %s/opensearch [GET] id=%s -> miss/error (%s)",
                     self._index, name, exc)
            return None
        cfg = (resp.get("_source") or {}).get("config")
        ms = (time.perf_counter() - t0) * 1000
        if not cfg:
            log.warning("executing on %s/opensearch [GET] id=%s -> doc has no 'config'",
                        self._index, name)
            return None
        log.info("executing on %s/opensearch [GET] id=%s -> hit (title=%r) in %.1fms",
                 self._index, name, cfg.get("title"), ms)
        return cfg

    async def names(self) -> List[str]:
        return await self._scan_names({"query": {"match_all": {}}}, op="SCAN names")

    async def by_module(self, module: str) -> List[str]:
        return await self._scan_names({"query": {"term": {"module": module}}},
                                      op="SCAN by_module")

    async def configs_for_tables(self, fqns: List[str]) -> List[str]:
        names = await self._scan_names({"query": {"terms": {"primary_table": list(fqns)}}},
                                       op="SCAN configs_for_tables")
        return sorted(set(names))

    async def search_scored(self, query: str, module: Optional[str], limit: int,
                            boost_only: bool = False) -> List[Tuple[dict, float]]:
        """BM25 over the config index, optionally scoped to a module.

        ``boost_only`` distinguishes *how* the module was chosen:

          * ``True``  — it was INFERRED from the question (module_registry). The
            module becomes a ``should`` boost: its KPIs rank higher but nothing
            is excluded, so a misdetection can only re-rank, never hide a hit.
          * ``False`` — the caller PINNED it explicitly. Honour it as a hard
            ``filter``; an explicit scope should mean what it says.
        """
        if not (query or "").strip():
            return []
        mm = {"multi_match": {"query": query, "fields": osc.SEARCH_FIELDS,
                              "type": "best_fields"}}
        if not module:
            q = mm
        elif boost_only:
            q = {"bool": {"must": [mm],
                          "should": [{"term": {"module": {"value": module,
                                                          "boost": _module_boost()}}}]}}
        else:
            q = {"bool": {"must": [mm], "filter": [{"term": {"module": module}}]}}
        body = {"size": limit, "query": q, "_source": ["config"]}
        log.info(f"OpenSearch executed query on {self._index}: {_dump(body)}")
        t0 = time.perf_counter()
        resp = await self._client.search(index=self._index, body=body)
        out: List[Tuple[dict, float]] = []
        for h in resp.get("hits", {}).get("hits", []):
            cfg = (h.get("_source") or {}).get("config")
            if cfg:
                out.append((cfg, h.get("_score", 0.0)))
        hits = [(c.get("name"), round(s, 3)) for c, s in out]
        took_ms = (time.perf_counter() - t0) * 1000
        log.info(f"OpenSearch query on {self._index} returned {len(out)} hit(s) {hits} in {took_ms:.1f}ms")
        return out


# ===========================================================================
# Facade
# ===========================================================================
class KpiCatalog:
    def __init__(self):
        client = osc.get_client()
        if client is None:
            raise RuntimeError(
                "OpenSearch is not available: set OPENSEARCH_URL (or OPENSEARCH_HOST) "
                "and install 'opensearch-py[async]'. See .env.example."
            )
        self._backend = OpenSearchBackend(client, osc.index_name())
        log.info("KpiCatalog backend: OpenSearch (index=%s)", osc.index_name())

    # ---- access ----------------------------------------------------------
    async def names(self) -> List[str]:
        return await self._backend.names()

    async def get(self, name: str) -> Optional[dict]:
        return await self._backend.get(name)

    async def exists(self, name: str) -> bool:
        return (await self.get(name)) is not None

    async def summary(self, name: str) -> Optional[dict]:
        cfg = await self.get(name)
        return summary_from_config(cfg) if cfg else None

    async def by_module(self, module: str) -> List[str]:
        return await self._backend.by_module(module)

    async def configs_for_tables(self, fqns: List[str]) -> List[str]:
        """KPI names whose primary dataset is one of the given schema.table names."""
        return await self._backend.configs_for_tables(fqns)

    # ---- search ----------------------------------------------------------
    async def search(self, query: str, module: Optional[str] = None, limit: int = 8,
                     boost_only: bool = False) -> List[dict]:
        """Ranked KPI summaries. Pass ``boost_only=True`` when ``module`` was
        inferred rather than supplied by the caller (see ``search_scored``)."""
        scored = await self._backend.search_scored(query, module, limit,
                                                   boost_only=boost_only)
        results = [summary_from_config(cfg) | {"score": sc} for cfg, sc in scored]
        log.info("search %r (module=%s%s) -> %s", query, module,
                 " boost" if boost_only else "",
                 [(r["name"], r.get("score")) for r in results])
        return results


@lru_cache(maxsize=1)
def get_catalog() -> KpiCatalog:
    return KpiCatalog()
