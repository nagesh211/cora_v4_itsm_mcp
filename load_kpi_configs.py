# #!/usr/bin/env python3
# """Standalone KPI-config loader for OpenSearch. Root-level, self-contained.
#
# Does exactly this and nothing else:
#
#   1. Check whether the index exists.
#   2. If it EXISTS   -> just insert the documents (mapping left untouched).
#   3. If it is MISSING -> create it with the mapping, then insert.
#   4. Each ``<dir>/*.json`` file becomes ONE document (``_id`` = KPI ``name``).
#
# This script imports nothing from the project. Its only third-party dependencies are
# ``opensearch-py`` (synchronous client) and, optionally, ``python-dotenv`` to read a
# local ``.env``. Everything else (mapping, per-file document) is inlined here so the
# file can be copied and run on its own.
#
# USAGE
#   python load_kpi_configs.py                  # ensure index + insert all of ./config
#   python load_kpi_configs.py --dir pepops_bkp     # load a different config directory
#   python load_kpi_configs.py emergency im-*   # insert a subset (name/filename/glob)
#   python load_kpi_configs.py --recreate       # drop + recreate the index first
#   python load_kpi_configs.py --raw            # store each file verbatim (no curation)
#   python load_kpi_configs.py --dry-run        # print docs; connect to nothing
#
# Connection env (read from the environment or a local .env). Same precedence as the
# runtime client in ``cora_mcp/opensearch_client.py``, so the loader and the server
# always talk to the same cluster:
#   OPENSEARCH_URL           full url e.g. https://host:9201 (host/port/ssl derived)
#   OPENSEARCH_HOST          host                (used if OPENSEARCH_URL unset)
#   OPENSEARCH_PORT          port                (default: 9200)
#   OPENSEARCH_USERNAME      basic-auth user     (optional; OPENSEARCH_USER also accepted)
#   OPENSEARCH_PASSWORD      basic-auth password (optional)
#   OPENSEARCH_USE_SSL       true|false          (default: true; from URL scheme if set)
#   OPENSEARCH_VERIFY_CERTS  true|false          (default: false)
#   OPENSEARCH_INDEX         index name          (default: cora-kpi-configs)
#   OPENSEARCH_TIMEOUT       seconds             (default: 300)
# """
# from __future__ import annotations
#
# import argparse
# import fnmatch
# import glob
# import json
# import os
# import sys
# from urllib.parse import urlparse
#
# _HERE = os.path.dirname(os.path.abspath(__file__))
# CONFIG_DIR = os.path.join(_HERE, "config")
#
# # Optional .env support; harmless if python-dotenv is absent.
# try:
#     from dotenv import load_dotenv
#     load_dotenv(os.path.join(_HERE, ".env"))
# except Exception:
#     pass
#
#
# # ---------------------------------------------------------------------------
# # Index mapping (inlined). One doc per KPI, _id = name. Curated searchable
# # fields are analyzed; the full config is stored but NOT indexed.
# # ---------------------------------------------------------------------------
# INDEX_MAPPING = {
#     "mappings": {
#         "properties": {
#             "name":             {"type": "keyword", "fields": {"text": {"type": "text"}}},
#             "title":            {"type": "text", "analyzer": "english"},
#             "module":           {"type": "keyword"},
#             "unit":             {"type": "keyword"},
#             "execution_mode":   {"type": "keyword"},
#             "status":           {"type": "keyword"},
#             "synonyms":         {"type": "text", "analyzer": "english"},
#             "sample_questions": {"type": "text", "analyzer": "english"},
#             "tags":             {"type": "keyword", "fields": {"text": {"type": "text"}}},
#             "allowed_filters":  {"type": "keyword"},
#             "drilldown_dims":   {"type": "keyword"},
#             "primary_table":    {"type": "keyword"},
#             "search_blob":      {"type": "text", "analyzer": "english"},
#             "updated_at":       {"type": "date"},
#             # Full config JSON: stored (returned in _source) but NOT indexed.
#             "config":           {"type": "object", "enabled": False},
#         }
#     }
# }
#
#
# def config_dimensions(cfg: dict) -> list:
#     """The dimensions a KPI can be broken down by, whichever shape declares them:
#     legacy ``drilldown.dimensions`` or pepops_bkp ``allowed_group_by: [{field,
#     granularity?}]``. Entries with a ``granularity`` list are time-grain date
#     fields for series mode, not breakdown dimensions, so they are skipped.
#
#     Mirrors ``cora_mcp.opensearch_client.config_dimensions`` — duplicated on
#     purpose to keep this script standalone; change both together."""
#     dims = list((cfg.get("drilldown") or {}).get("dimensions") or [])
#     if dims:
#         return dims
#     out = []
#     for item in cfg.get("allowed_group_by") or []:
#         if isinstance(item, str):
#             field = item
#         elif isinstance(item, dict):
#             if item.get("granularity"):
#                 continue
#             field = item.get("field")
#         else:
#             continue
#         if field and field not in out:
#             out.append(field)
#     return out
#
#
# def build_index_doc(cfg: dict) -> dict:
#     """Project a KPI config into an index document: curated searchable fields plus
#     the full config verbatim under ``config``. The ``_id`` is the KPI ``name``."""
#     nl = cfg.get("nl") or {}
#     gov = cfg.get("governance") or {}
#     pd = cfg.get("primary_dataset") or {}
#     name = cfg.get("name")
#     title = cfg.get("title", "") or ""
#     synonyms = list(nl.get("synonyms") or [])
#     samples = list(nl.get("sample_questions") or [])
#     tags = list(gov.get("tags") or [])
#     allowed = list((cfg.get("filters") or {}).get("allowed") or [])
#     dims = config_dimensions(cfg)
#     primary_table = None
#     if pd.get("schema") and pd.get("table"):
#         primary_table = f"{pd['schema']}.{pd['table']}"
#     blob = " ".join([str(name or ""), title, cfg.get("module", "") or "",
#                      *synonyms, *samples, *[str(t) for t in tags]])
#     return {
#         "name": name,
#         "title": title,
#         "module": cfg.get("module"),
#         "unit": cfg.get("unit"),
#         "execution_mode": cfg.get("execution_mode"),
#         "status": gov.get("status") or (cfg.get("signal") or {}).get("status") or "live",
#         "synonyms": synonyms,
#         "sample_questions": samples,
#         "tags": tags,
#         "allowed_filters": allowed,
#         "drilldown_dims": dims,
#         "primary_table": primary_table,
#         "search_blob": blob,
#         "updated_at": gov.get("updated_at"),
#         "config": cfg,
#     }
#
#
# def _truthy(val, default=True):
#     if val is None:
#         return default
#     return str(val).strip().lower() in ("1", "true", "yes", "on")
#
#
# def _index_name():
#     return os.getenv("OPENSEARCH_INDEX", "cora-kpi-configs-pep")
#
# def _conn_settings():
#     """(host, port, use_ssl) from OPENSEARCH_URL, or the discrete
#     OPENSEARCH_HOST / OPENSEARCH_PORT / OPENSEARCH_USE_SSL trio."""
#     url = os.getenv("OPENSEARCH_URL")
#     host = os.getenv("OPENSEARCH_HOST")
#     port = os.getenv("OPENSEARCH_PORT")
#     use_ssl = os.getenv("OPENSEARCH_USE_SSL")
#     if url:
#         parsed = urlparse(url)
#         host = host or parsed.hostname
#         port = port or (str(parsed.port) if parsed.port else None)
#         if use_ssl is None:
#             use_ssl = "true" if parsed.scheme == "https" else "false"
#     return host, int(port or "9200"), _truthy(use_ssl, default=True)
#
#
# def _build_client():
#     """Construct a synchronous ``OpenSearch`` client from OPENSEARCH_* env, or exit
#     with a clear message if the host / library is unavailable."""
#     host, port, use_ssl = _conn_settings()
#     if not host:
#         sys.exit("Neither OPENSEARCH_URL nor OPENSEARCH_HOST is set. Set one (or add "
#                  "it to .env), or use --dry-run to preview documents without a cluster.")
#     try:
#         from opensearchpy import OpenSearch
#     except Exception as exc:  # pragma: no cover - env-dependent
#         sys.exit("opensearch-py not importable (%s). Install it: pip install opensearch-py"
#                  % exc)
#
#     user = os.getenv("OPENSEARCH_USERNAME") or os.getenv("OPENSEARCH_USER")
#     password = os.getenv("OPENSEARCH_PASSWORD")
#     http_auth = (user, password) if user and password else None
#     print("connecting to %s:%s (ssl=%s)" % (host, port, use_ssl))
#     return OpenSearch(
#         hosts=[{"host": host, "port": port}],
#         http_auth=http_auth,
#         use_ssl=use_ssl,
#         verify_certs=_truthy(os.getenv("OPENSEARCH_VERIFY_CERTS"), default=False),
#         ssl_assert_hostname=False,
#         ssl_show_warn=False,
#         timeout=int(os.getenv("OPENSEARCH_TIMEOUT", "300")),
#     )
#
#
# def _load_configs(patterns, config_dir=CONFIG_DIR):
#     """Yield (path, config) for each <config_dir>/*.json matching the patterns
#     (empty => all). A pattern may be a bare name, a filename, or a glob."""
#     all_paths = sorted(glob.glob(os.path.join(config_dir, "*.json")))
#     if not patterns:
#         selected = all_paths
#     else:
#         selected = []
#         for p in all_paths:
#             base = os.path.basename(p)
#             stem = base[:-5]  # strip .json
#             if any(fnmatch.fnmatch(stem, pat) or fnmatch.fnmatch(base, pat)
#                    or stem == pat for pat in patterns):
#                 selected.append(p)
#     for path in selected:
#         try:
#             with open(path, encoding="utf-8") as fh:
#                 cfg = json.load(fh)
#         except Exception as exc:
#             print(f"  skip {os.path.basename(path)}: {exc}")
#             continue
#         if cfg.get("name"):
#             yield path, cfg
#         else:
#             print(f"  skip {os.path.basename(path)}: no 'name'")
#
#
# def _to_doc(cfg, raw):
#     """One document per file. Default: curated searchable fields + full config.
#     With --raw: the file's JSON verbatim."""
#     return cfg if raw else build_index_doc(cfg)
#
#
# def _ensure_index(client, index, recreate):
#     """Create the index with the mapping only when needed.
#
#     - exists + --recreate -> drop, then create with mapping.
#     - exists (normal)     -> leave it; documents insert into the live mapping.
#     - missing             -> create with mapping.
#     """
#     exists = client.indices.exists(index=index)
#     if exists and recreate:
#         print(f"dropping index {index!r}")
#         client.indices.delete(index=index)
#         exists = False
#     if exists:
#         print(f"index {index!r} exists -> inserting documents (mapping unchanged)")
#     else:
#         print(f"index {index!r} missing -> creating with mapping")
#         client.indices.create(index=index, body=INDEX_MAPPING)
#
#
# def main():
#     ap = argparse.ArgumentParser(
#         description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
#     ap.add_argument("patterns", nargs="*",
#                     help="config names / filenames / globs (default: all)")
#     ap.add_argument("--dir", dest="config_dir", default=CONFIG_DIR, metavar="DIR",
#                     help="directory of KPI config JSON files (default: ./config)")
#     ap.add_argument("--recreate", action="store_true",
#                     help="drop and recreate the index first (reapplies the mapping)")
#     ap.add_argument("--raw", action="store_true",
#                     help="store each file's JSON verbatim instead of the curated doc")
#     ap.add_argument("--dry-run", action="store_true",
#                     help="print the documents; do not connect or index")
#     args = ap.parse_args()
#
#     config_dir = os.path.abspath(args.config_dir)
#     if not os.path.isdir(config_dir):
#         sys.exit("not a directory: %s" % config_dir)
#     configs = list(_load_configs(args.patterns, config_dir))
#     if not configs:
#         sys.exit("no matching configs found in %s" % config_dir)
#     print("loading %d config(s) from %s" % (len(configs), config_dir))
#
#     if args.dry_run:
#         for path, cfg in configs:
#             doc = _to_doc(cfg, args.raw)
#             print("\n== %s (_id=%s) ==" % (os.path.basename(path), cfg["name"]))
#             if args.raw:
#                 print(json.dumps(doc, indent=2, default=str))
#             else:
#                 preview = {k: v for k, v in doc.items() if k != "config"}
#                 preview["config"] = "<full config: %d keys>" % len(cfg)
#                 print(json.dumps(preview, indent=2, default=str))
#         print("\n[dry-run] %d config(s) would be indexed into %r"
#               % (len(configs), _index_name()))
#         return
#
#     client = _build_client()
#     index = _index_name()
#     try:
#         _ensure_index(client, index, args.recreate)
#         ok = 0
#         for path, cfg in configs:
#             doc = _to_doc(cfg, args.raw)
#             try:
#                 client.index(index=index, id=cfg["name"], body=doc)
#                 ok += 1
#             except Exception as exc:
#                 print(f"  FAILED {cfg['name']}: {exc}")
#         client.indices.refresh(index=index)
#         print("inserted %d/%d document(s) into %r" % (ok, len(configs), index))
#     finally:
#         try:
#             client.close()
#         except Exception:
#             pass
#
#
# if __name__ == "__main__":
#     main()


# !/usr/bin/env python3
"""Async KPI config loader for OpenSearch with bulk insertion."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from opensearchpy import AsyncOpenSearch

# ---------------------------------------------------------------------------
# Index mapping
# ---------------------------------------------------------------------------
INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "name": {"type": "keyword", "fields": {"text": {"type": "text"}}},
            "title": {"type": "text", "analyzer": "english"},
            "module": {"type": "keyword"},
            "unit": {"type": "keyword"},
            "execution_mode": {"type": "keyword"},
            "status": {"type": "keyword"},
            "synonyms": {"type": "text", "analyzer": "english"},
            "sample_questions": {"type": "text", "analyzer": "english"},
            "tags": {"type": "keyword", "fields": {"text": {"type": "text"}}},
            "allowed_filters": {"type": "keyword"},
            "drilldown_dims": {"type": "keyword"},
            "primary_table": {"type": "keyword"},
            "search_blob": {"type": "text", "analyzer": "english"},
            "updated_at": {"type": "date"},
            "config": {"type": "object", "enabled": False},
        }
    }
}


def config_dimensions(cfg: dict) -> list:
    """Extract dimensions from KPI config."""
    dims = list((cfg.get("drilldown") or {}).get("dimensions") or [])
    if dims:
        return dims
    out = []
    for item in cfg.get("allowed_group_by") or []:
        if isinstance(item, str):
            field = item
        elif isinstance(item, dict):
            if item.get("granularity"):
                continue
            field = item.get("field")
        else:
            continue
        if field and field not in out:
            out.append(field)
    return out


def build_index_doc(cfg: dict) -> dict:
    """Build a curated document for indexing."""
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


def load_configs_from_path(config_path: str) -> list[tuple[str, dict]]:
    """Load all JSON files from the given path."""
    path = Path(config_path)
    configs = []

    if path.is_file():
        # Single file
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if cfg.get("name"):
            configs.append((path.name, cfg))
        return configs

    if path.is_dir():
        # Directory of JSON files
        for json_file in sorted(path.glob("*.json")):
            try:
                with open(json_file, encoding="utf-8") as fh:
                    cfg = json.load(fh)
                if cfg.get("name"):
                    configs.append((json_file.name, cfg))
            except Exception as exc:
                print(f"  skip {json_file.name}: {exc}")
        return configs

    raise ValueError(f"Path does not exist: {config_path}")


async def ensure_index(client: AsyncOpenSearch, index: str) -> None:
    """Create index with mapping if it doesn't exist."""
    exists = await client.indices.exists(index=index)
    if exists:
        print(f"index {index!r} exists -> ready for insertion")
    else:
        print(f"index {index!r} missing -> creating with mapping")
        await client.indices.create(index=index, body=INDEX_MAPPING)


async def bulk_insert(
        client: AsyncOpenSearch,
        index: str,
        configs: list[tuple[str, dict]],
        chunk_size: int = 100,
) -> int:
    """Bulk insert KPI configs into OpenSearch."""
    operations = []

    for filename, cfg in configs:
        doc = build_index_doc(cfg)
        kpi_id = cfg.get("name")

        # Bulk API format: action line + doc line
        operations.append({"index": {"_index": index, "_id": kpi_id}})
        operations.append(doc)

    # Insert in chunks
    inserted = 0
    for i in range(0, len(operations), chunk_size * 2):  # *2 because each doc has 2 lines
        chunk = operations[i: i + chunk_size * 2]
        try:
            resp = await client.bulk(body=chunk)
            if resp.get("errors"):
                print(f"  errors in bulk chunk: {resp['errors']}")
            inserted += (len(chunk) // 2)
        except Exception as exc:
            print(f"  bulk insert failed: {exc}")

    await client.indices.refresh(index=index)
    return inserted


async def main(
        config_path: str,
        host: str = "localhost",
        port: int = 9200,
        index: str = "cora-kpi-configs",
        username: str | None = None,
        password: str | None = None,
        use_ssl: bool = False,
) -> None:
    """Load and insert KPI configs."""
    print(f"loading configs from {config_path}...")
    configs = load_configs_from_path(config_path)

    if not configs:
        print("no configs found")
        return

    print(f"loaded {len(configs)} config(s)")

    # Build client
    http_auth = (username, password) if username and password else None
    client = AsyncOpenSearch(
        hosts=[{"host": host, "port": port}],
        http_auth=http_auth,
        use_ssl=use_ssl,
        verify_certs=False,
        ssl_show_warn=False,
    )

    try:
        await ensure_index(client, index)
        inserted = await bulk_insert(client, index, configs)
        print(f"inserted {inserted}/{len(configs)} document(s) into {index!r}")
    finally:
        await client.close()


if __name__ == "__main__":
    # Customize these values
    CONFIG_PATH = "./itsm_new"  # Path to JSON files or single JSON file
    OPENSEARCH_HOST = "10.64.4.28"
    OPENSEARCH_PORT = 9201
    OPENSEARCH_INDEX = "cora-kpi-configs-pep"
    OPENSEARCH_USERNAME = "admin"  # Set if needed
    OPENSEARCH_PASSWORD = "EdxiPassw0rd!"  # Set if needed
    OPENSEARCH_USE_SSL = True  # Set to True if using HTTPS

    asyncio.run(main(
        config_path=CONFIG_PATH,
        host=OPENSEARCH_HOST,
        port=OPENSEARCH_PORT,
        index=OPENSEARCH_INDEX,
        username=OPENSEARCH_USERNAME,
        password=OPENSEARCH_PASSWORD,
        use_ssl=OPENSEARCH_USE_SSL,
    ))