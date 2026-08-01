#!/usr/bin/env python3
"""Load curated module metadata (label + aliases) into the module-meta index.

This is the *optional* second source behind :mod:`cora_mcp.module_registry`.
Which modules exist is derived from the KPI config index and needs no
configuration at all; this index only supplies the two things configs cannot:

  * a human **label** for a module code ("incops" -> "Incident Operations");
  * curated module-level **aliases** that no single metric mentions ("cab",
    "help desk", "inc ops") — the vocabulary that lets sibling modules with
    identical metric synonyms be told apart.

Each deployment has its own KPI index and therefore its own module vocabulary,
so pass the seed file for the deployment you are loading:

  python load_module_meta.py module_meta/pepops_bkp.json
  python load_module_meta.py module_meta/internal.json --recreate
  python load_module_meta.py module_meta/pepops_bkp.json --dry-run

The seed file is a JSON list of {"code", "label", "aliases"} objects. ``_id`` is
the module code, so re-running is idempotent.

Like ``load_kpi_configs.py`` this is a **standalone operational script**, not part
of the server: it imports nothing from the project, is deployment-specific, and is
not needed in production once the meta index has been seeded. The runtime half
lives in ``cora_mcp/module_registry.py``, which only ever *reads* this index.

Connection env is identical to load_kpi_configs.py (OPENSEARCH_URL or
OPENSEARCH_HOST/PORT, OPENSEARCH_USERNAME/OPENSEARCH_USER, OPENSEARCH_PASSWORD).
The target index comes from OPENSEARCH_MODULE_META_INDEX (default
``cora-module-meta``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))

# Optional .env support; harmless if python-dotenv is absent.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"))
except Exception:
    pass

DEFAULT_INDEX = "cora-module-meta"

INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "code":    {"type": "keyword"},
            "label":   {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "aliases": {"type": "keyword"},
        }
    }
}


def _truthy(val, default=True):
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _index_name():
    return os.getenv("OPENSEARCH_MODULE_META_INDEX", DEFAULT_INDEX)


def _conn_settings():
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
    return host, int(port or "9200"), _truthy(use_ssl, default=True)


def _build_client():
    host, port, use_ssl = _conn_settings()
    if not host:
        sys.exit("Neither OPENSEARCH_URL nor OPENSEARCH_HOST is set. Set one (or add "
                 "it to .env), or use --dry-run.")
    try:
        from opensearchpy import OpenSearch
    except Exception as exc:  # pragma: no cover - env-dependent
        sys.exit("opensearch-py not importable (%s). Install it: pip install opensearch-py"
                 % exc)
    user = os.getenv("OPENSEARCH_USERNAME") or os.getenv("OPENSEARCH_USER")
    password = os.getenv("OPENSEARCH_PASSWORD")
    print("connecting to %s:%s (ssl=%s)" % (host, port, use_ssl))
    return OpenSearch(
        hosts=[{"host": host, "port": port}],
        http_auth=(user, password) if user and password else None,
        use_ssl=use_ssl,
        verify_certs=_truthy(os.getenv("OPENSEARCH_VERIFY_CERTS"), default=False),
        ssl_assert_hostname=False,
        ssl_show_warn=False,
        timeout=int(os.getenv("OPENSEARCH_TIMEOUT", "300")),
    )


def _load_seed(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):                     # {code: {...}} also accepted
        data = [{"code": k, **v} for k, v in data.items()]
    docs = []
    for entry in data:
        code = (entry.get("code") or "").strip()
        if not code:
            print("  skip entry with no 'code': %r" % entry)
            continue
        docs.append({"code": code,
                     "label": entry.get("label") or code,
                     "aliases": [a for a in (entry.get("aliases") or []) if a]})
    return docs


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seed", help="JSON seed file (e.g. module_meta/pepops_bkp.json)")
    ap.add_argument("--recreate", action="store_true",
                    help="drop and recreate the index first")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the documents; do not connect or index")
    args = ap.parse_args()

    if not os.path.isfile(args.seed):
        sys.exit("no such seed file: %s" % args.seed)
    docs = _load_seed(args.seed)
    if not docs:
        sys.exit("no module entries found in %s" % args.seed)

    if args.dry_run:
        for d in docs:
            print("== %s (_id=%s) ==" % (d["label"], d["code"]))
            print("   aliases: %s" % ", ".join(d["aliases"]))
        print("\n[dry-run] %d module(s) would be indexed into %r"
              % (len(docs), _index_name()))
        return

    client = _build_client()
    index = _index_name()
    try:
        exists = client.indices.exists(index=index)
        if exists and args.recreate:
            print("dropping index %r" % index)
            client.indices.delete(index=index)
            exists = False
        if not exists:
            print("index %r missing -> creating with mapping" % index)
            client.indices.create(index=index, body=INDEX_MAPPING)
        ok = 0
        for d in docs:
            try:
                client.index(index=index, id=d["code"], body=d)
                ok += 1
            except Exception as exc:
                print("  FAILED %s: %s" % (d["code"], exc))
        client.indices.refresh(index=index)
        print("indexed %d/%d module(s) into %r" % (ok, len(docs), index))
        print("call the refresh_kpi_modules MCP tool (or wait for the cache TTL) "
              "for a running server to pick this up.")
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()