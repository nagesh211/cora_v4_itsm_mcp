"""OpenSearch connection factory for the KPI config index.

If ``opensearch-py`` is not installed or ``OPENSEARCH_URL`` is not set,
:func:`get_client` returns ``None`` and :class:`cora_mcp.kpi_catalog.KpiCatalog`
raises at construction (OpenSearch is the only backend). Nothing here raises at
import time.

The client is an **AsyncOpenSearch** instance (aiohttp transport); every runtime
call site awaits it.

Env (see .env.example):
  OPENSEARCH_URL           full url e.g. https://host:9201 (host/port/ssl derived)
  OPENSEARCH_HOST          host                (used if OPENSEARCH_URL unset)
  OPENSEARCH_PORT          port                (default: 9200)
  OPENSEARCH_USERNAME      basic-auth user     (optional; OPENSEARCH_USER also accepted)
  OPENSEARCH_PASSWORD      basic-auth password (optional)
  OPENSEARCH_USE_SSL       true|false          (default: true; from URL scheme if set)
  OPENSEARCH_VERIFY_CERTS  true|false          (default: false)
  OPENSEARCH_INDEX         index name          (default: cora-kpi-configs)
  OPENSEARCH_TIMEOUT       seconds             (default: 300)
  OPENSEARCH_MAXSIZE       connection pool     (default: 30)
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Optional
from urllib.parse import urlparse

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

DEFAULT_INDEX = "cora-kpi-configs"


def index_name() -> str:
    return os.getenv("OPENSEARCH_INDEX", DEFAULT_INDEX)


def _truthy(val: Optional[str], default: bool = True) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def get_client():
    """Return a configured ``AsyncOpenSearch`` client, or ``None`` if OpenSearch is
    not available (no host configured, or the library is missing).
    Cached: constructed at most once per process.

    There is no eager ping (it would need an event loop); a dead cluster surfaces as
    an error on the first request."""
    # Accept either OPENSEARCH_URL (e.g. https://host:9201) or the discrete
    # OPENSEARCH_HOST / OPENSEARCH_PORT / OPENSEARCH_USE_SSL trio.
    url = os.getenv("OPENSEARCH_URL")
    host = os.getenv("OPENSEARCH_HOST")
    port = os.getenv("OPENSEARCH_PORT")
    use_ssl = os.getenv("OPENSEARCH_USE_SSL")
    if url:
        parsed = urlparse(url)
        host = host or parsed.hostname
        port = port or (str(parsed.port) if parsed.port else None)
        if use_ssl is None:
            use_ssl = "true" if parsed.scheme == "https" else "false"

    if not host:
        log.warning("neither OPENSEARCH_URL nor OPENSEARCH_HOST is set; "
                    "OpenSearch client unavailable")
        return None

    try:
        from opensearchpy import AsyncOpenSearch, AIOHttpConnection  # type: ignore
    except Exception as exc:  # library / async extra not installed
        log.warning("opensearch-py async not importable (%s); OpenSearch client "
                    "unavailable (install 'opensearch-py[async]')", exc)
        return None

    port = int(port or "9200")
    user = os.getenv("OPENSEARCH_USERNAME") or os.getenv("OPENSEARCH_USER")
    password = os.getenv("OPENSEARCH_PASSWORD")
    http_auth = (user, password) if user and password else None
    use_ssl = _truthy(use_ssl, default=True)
    verify = _truthy(os.getenv("OPENSEARCH_VERIFY_CERTS"), default=False)

    try:
        client = AsyncOpenSearch(
            hosts=[{"host": host, "port": port}],
            http_auth=http_auth,
            use_ssl=use_ssl,
            verify_certs=verify,
            ssl_assert_hostname=False,
            ssl_show_warn=False,
            timeout=int(os.getenv("OPENSEARCH_TIMEOUT", "300")),
            connection_class=AIOHttpConnection,
            maxsize=int(os.getenv("OPENSEARCH_MAXSIZE", "30")),
        )
        # log.info("AsyncOpenSearch client ready: %s:%s index=%s", host, port, index_name())
        return client
    except Exception as exc:
        log.warning("AsyncOpenSearch init failed (%s); OpenSearch client unavailable", exc)
        return None


def reset_client_cache() -> None:
    """Drop the cached client (tests / after env changes)."""
    get_client.cache_clear()


# ---------------------------------------------------------------------------
# Index shape — shared by the runtime backend (read) and the indexer (write),
# so the field set can never drift between them.
# ---------------------------------------------------------------------------
INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "name":             {"type": "keyword", "fields": {"text": {"type": "text"}}},
            "title":            {"type": "text", "analyzer": "english"},
            "module":           {"type": "keyword"},
            "unit":             {"type": "keyword"},
            "execution_mode":   {"type": "keyword"},
            "status":           {"type": "keyword"},
            "synonyms":         {"type": "text", "analyzer": "english"},
            "sample_questions": {"type": "text", "analyzer": "english"},
            "tags":             {"type": "keyword", "fields": {"text": {"type": "text"}}},
            "allowed_filters":  {"type": "keyword"},
            "drilldown_dims":   {"type": "keyword"},
            "primary_table":    {"type": "keyword"},
            "search_blob":      {"type": "text", "analyzer": "english"},
            "updated_at":       {"type": "date"},
            # Full config JSON — stored (returned in _source) but NOT indexed.
            "config":           {"type": "object", "enabled": False},
        }
    }
}

# Field boosts for the BM25 selection query (see kpi_catalog.OpenSearchBackend).
SEARCH_FIELDS = [
    "name.text^5", "title^3", "synonyms^2",
    "sample_questions", "search_blob", "tags.text^2",
]


def config_dimensions(cfg: dict) -> List[str]:
    """The dimensions a KPI can be broken down by, whichever shape declares them.

    Two config shapes are in play and they name this differently:

      * legacy ``config/*.json``  -> ``drilldown.dimensions: ["region_name", …]``
      * pepops ``pepops/*.json``  -> ``allowed_group_by: [{"field": …,
                                       "granularity": [...]?}, …]``

    Reading only the first shape indexes ``drilldown_dims: []`` for every pepops
    KPI, so nothing downstream (the MCP summary, dim resolution, group-by
    validation) knows the KPI *can* be broken down. Entries carrying a
    ``granularity`` list are time-grain date fields for ``mode="series"``
    (``grain=day|week|month|quarter``), not breakdown dimensions, so they are
    skipped — offering ``dim="closed_date"`` would group by a raw timestamp.
    """
    dims = list((cfg.get("drilldown") or {}).get("dimensions") or [])
    if dims:
        return dims
    out: List[str] = []
    for item in cfg.get("allowed_group_by") or []:
        if isinstance(item, str):
            field = item
        elif isinstance(item, dict):
            if item.get("granularity"):
                continue                       # time-grain field, not a dimension
            field = item.get("field")
        else:
            continue
        if field and field not in out:
            out.append(field)
    return out


def build_index_doc(cfg: dict) -> dict:
    """Project a KPI config into an index document: curated searchable fields plus
    the full config verbatim under ``config``. The ``_id`` is the KPI ``name``."""
    nl = cfg.get("nl") or {}
    gov = cfg.get("governance") or {}
    pd = cfg.get("primary_dataset") or {}
    name = cfg.get("name")
    title = cfg.get("title", "") or ""
    synonyms = list(nl.get("synonyms") or [])
    samples = list(nl.get("sample_questions") or [])
    tags = list(gov.get("tags") or [])
    allowed = list((cfg.get("filters") or {}).get("allowed") or [])
    dims = config_dimensions(cfg)
    primary_table = None
    if pd.get("schema") and pd.get("table"):
        primary_table = f"{pd['schema']}.{pd['table']}"
    blob = " ".join([str(name or ""), title, cfg.get("module", "") or "",
                     *synonyms, *samples, *[str(t) for t in tags]])
    return {
        "name": name,
        "title": title,
        "module": cfg.get("module"),
        "unit": cfg.get("unit"),
        "execution_mode": cfg.get("execution_mode"),
        "status": gov.get("status") or (cfg.get("signal") or {}).get("status") or "live",
        "synonyms": synonyms,
        "sample_questions": samples,
        "tags": tags,
        "allowed_filters": allowed,
        "drilldown_dims": dims,
        "primary_table": primary_table,
        "search_blob": blob,
        "updated_at": gov.get("updated_at"),
        "config": cfg,
    }