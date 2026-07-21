#!/usr/bin/env python3
"""Index KPI configs into OpenSearch.

Reads ``config/*.json`` and upserts one document per KPI (``_id`` = KPI name) into
the ``cora-kpi-configs`` index: curated searchable fields + the full config blob
(see :func:`cora_mcp.opensearch_client.build_index_doc`). Idempotent — safe to
re-run; a re-run refreshes every doc so edits go live on the next request.

USAGE
  # index every config/*.json
  python tools/index_configs.py

  # index a subset (bare name, filename, or glob)
  python tools/index_configs.py emergency
  python tools/index_configs.py "im-*"

  # drop and recreate the index first (applies the mapping cleanly)
  python tools/index_configs.py --recreate

  # show the doc that WOULD be indexed, hit nothing (no cluster needed)
  python tools/index_configs.py emergency --dry-run

Connection comes from the same env as the server (OPENSEARCH_URL, OPENSEARCH_USER,
OPENSEARCH_PASSWORD, OPENSEARCH_INDEX, OPENSEARCH_VERIFY_CERTS). See .env.example.
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except Exception:
    pass

from cora_mcp import opensearch_client as osc  # noqa: E402

CONFIG_DIR = os.path.join(_ROOT, "config")


def _load_configs(patterns):
    """Yield (path, config) for each config/*.json matching the given patterns
    (empty patterns => all). A pattern may be a bare name, a filename, or a glob."""
    all_paths = sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json")))
    if not patterns:
        selected = all_paths
    else:
        selected = []
        for p in all_paths:
            base = os.path.basename(p)
            stem = base[:-5]  # strip .json
            if any(fnmatch.fnmatch(stem, pat) or fnmatch.fnmatch(base, pat)
                   or stem == pat for pat in patterns):
                selected.append(p)
    for path in selected:
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception as exc:
            print(f"  skip {os.path.basename(path)}: {exc}")
            continue
        if cfg.get("name"):
            yield path, cfg
        else:
            print(f"  skip {os.path.basename(path)}: no 'name'")


async def _ensure_index(client, index, recreate):
    exists = await client.indices.exists(index=index)
    if exists and recreate:
        print(f"dropping index {index!r}")
        await client.indices.delete(index=index)
        exists = False
    if not exists:
        print(f"creating index {index!r}")
        await client.indices.create(index=index, body=osc.INDEX_MAPPING)


async def _index_all(client, index, configs, recreate):
    await _ensure_index(client, index, recreate)
    ok = 0
    for path, cfg in configs:
        doc = osc.build_index_doc(cfg)
        try:
            await client.index(index=index, id=doc["name"], body=doc)
            ok += 1
        except Exception as exc:
            print(f"  FAILED {doc['name']}: {exc}")
    await client.indices.refresh(index=index)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patterns", nargs="*", help="config names / filenames / globs (default: all)")
    ap.add_argument("--recreate", action="store_true", help="drop and recreate the index first")
    ap.add_argument("--dry-run", action="store_true", help="print docs; do not connect/index")
    args = ap.parse_args()

    configs = list(_load_configs(args.patterns))
    if not configs:
        sys.exit("no matching configs found in %s" % CONFIG_DIR)

    if args.dry_run:
        for path, cfg in configs:
            doc = osc.build_index_doc(cfg)
            doc_preview = {k: v for k, v in doc.items() if k != "config"}
            doc_preview["config"] = "<full config: %d keys>" % len(cfg)
            print("\n== %s (_id=%s) ==" % (os.path.basename(path), doc["name"]))
            print(json.dumps(doc_preview, indent=2, default=str))
        print("\n[dry-run] %d config(s) would be indexed into %r"
              % (len(configs), osc.index_name()))
        return

    osc.reset_client_cache()
    client = osc.get_client()
    if client is None:
        sys.exit("OpenSearch not available: set OPENSEARCH_URL (and install "
                 "'opensearch-py[async]'). See .env.example. Use --dry-run to preview "
                 "docs without a cluster.")

    index = osc.index_name()

    async def _run():
        try:
            return await _index_all(client, index, configs, args.recreate)
        finally:
            await client.close()

    ok = asyncio.run(_run())
    print("indexed %d/%d config(s) into %r" % (ok, len(configs), index))


if __name__ == "__main__":
    main()