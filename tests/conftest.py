"""Shared pytest fixtures.

The catalog is OpenSearch-only, and its client is a process-cached AsyncOpenSearch
whose aiohttp session binds to the event loop that first uses it. The sync test
harness runs each async assertion through ``asyncio.run()``, i.e. a *fresh* event
loop per call -- so a client cached on test A's (now closed) loop breaks test B.

Resetting the client cache (and the catalog's lru_cache) before every test makes each
test build a client bound to its own loop.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True)
def _fresh_opensearch_client():
    from cora_mcp import opensearch_client as osc
    from cora_mcp.kpi_catalog import get_catalog
    osc.reset_client_cache()
    get_catalog.cache_clear()
    yield
    osc.reset_client_cache()
    get_catalog.cache_clear()