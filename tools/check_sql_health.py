#!/usr/bin/env python3
"""check_sql_health.py — is every KPI config still valid against the live database?

A KPI config is written once and then trusted forever. The database underneath it
is not frozen: a column gets renamed, a view is rebuilt, a schema is reorganised.
Today the way that gets discovered is that somebody asks a question and gets an
error, because generating a query and running it are the same action — there is no
step that checks a config without paying to execute it.

:func:`cora_mcp.db.validate` is that step. Postgres parse-analyzes each generated
statement (resolving every table, column, operator and type) and reports what is
wrong, without reading a row. Sweeping the whole catalog costs seconds.

Each config is checked in every shape it advertises, because they fail
independently — a config whose ``stat`` query is fine can still have a broken
``breakdown`` query written against a stale column::

    stat        the scalar value
    series      the time series, at the config's own grain
    table       one query per advertised drilldown dimension

Exit code is 1 if anything is broken, so this can gate a deploy.

Usage::

    python tools/check_sql_health.py                  # sweep everything
    python tools/check_sql_health.py --module itsm    # one module
    python tools/check_sql_health.py --kpi emergency  # one config
    python tools/check_sql_health.py --sql-only       # generate, don't reach Postgres
    python tools/check_sql_health.py --json health.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv                                  # noqa: E402
load_dotenv(os.path.join(_ROOT, ".env"), override=False)

# The shared OpenSearch client defaults to a 300s timeout, which is reasonable for a
# server absorbing a slow query and useless for a batch sweep -- an unreachable
# cluster would stall this script for five minutes per call before saying so. Fail
# fast instead, unless the operator has set a value deliberately.
os.environ.setdefault("OPENSEARCH_TIMEOUT", "15")

from cora_mcp.logging_config import get_logger                  # noqa: E402

log = get_logger(__name__)


def _short(exc: Any, n: int = 200) -> str:
    return str(exc).replace("\n", " ").strip()[:n]


async def shapes_for(config: dict) -> List[Dict[str, Any]]:
    """The query shapes a config claims to support, as generate_query kwargs."""
    from cora_mcp import opensearch_client as osc

    out: List[Dict[str, Any]] = [{"mode": "stat"}, {"mode": "series", "grain": "month"}]
    for dim in (osc.config_dimensions(config) or [])[:6]:
        name = dim if isinstance(dim, str) else (dim.get("name") or dim.get("field"))
        if name:
            out.append({"mode": "table", "dim": name})
    return out


async def check_config(name: str, sql_only: bool) -> List[Dict[str, Any]]:
    from cora_mcp import db as dbmod
    from cora_mcp.kpi_catalog import get_catalog
    from cora_mcp.query_engine import generate_query

    catalog = get_catalog()
    config = await catalog.get(name)
    if config is None:
        return [{"kpi": name, "shape": "-", "status": "missing",
                 "detail": "config not found in the catalog"}]

    rows: List[Dict[str, Any]] = []
    for shape in await shapes_for(config):
        label = shape.get("dim") and f"table:{shape['dim']}" or shape["mode"]
        try:
            out = await generate_query(kpi=name, **shape)
        except Exception as exc:
            rows.append({"kpi": name, "shape": label, "status": "generate_failed",
                         "detail": f"{type(exc).__name__}: {_short(exc)}"})
            continue
        sql = out.get("sql") or (out.get("queries") or [{}])[0].get("sql")
        params = out.get("params") or []
        if not sql:
            rows.append({"kpi": name, "shape": label, "status": "no_sql",
                         "detail": "generator returned no SQL"})
            continue
        if sql_only:
            rows.append({"kpi": name, "shape": label, "status": "generated",
                         "detail": f"{len(sql)} chars (not validated)"})
            continue
        try:
            await dbmod.validate("postgres", None, sql, params)
        except dbmod.DBNotConfigured as exc:
            rows.append({"kpi": name, "shape": label, "status": "skipped",
                         "detail": _short(exc)})
        except dbmod.DBError as exc:
            rows.append({"kpi": name, "shape": label, "status": "invalid",
                         "detail": _short(exc), "sql": sql})
        else:
            rows.append({"kpi": name, "shape": label, "status": "ok", "detail": ""})
    return rows


async def run(module: Optional[str], only: Optional[str],
              sql_only: bool) -> List[Dict[str, Any]]:
    from cora_mcp.kpi_catalog import get_catalog

    try:
        catalog = get_catalog()
        names = ([only] if only
                 else await (catalog.by_module(module) if module else catalog.names()))
    except Exception as exc:
        raise SystemExit(f"cannot reach the KPI catalog: {_short(exc)}\n"
                         f"(the catalog is OpenSearch-only; set OPENSEARCH_URL in .env)")
    if not names:
        raise SystemExit("no KPI configs found")
    log.info("checking %d config(s)", len(names))

    rows: List[Dict[str, Any]] = []
    for name in names:
        rows.extend(await check_config(name, sql_only))
    return rows


def report(rows: List[Dict[str, Any]]) -> int:
    bad = [r for r in rows if r["status"] in ("invalid", "generate_failed",
                                              "no_sql", "missing")]
    skipped = [r for r in rows if r["status"] == "skipped"]
    ok = [r for r in rows if r["status"] in ("ok", "generated")]

    print("\n" + "=" * 78)
    print(f"KPI config health — {len({r['kpi'] for r in rows})} config(s), "
          f"{len(rows)} query shape(s)")
    print("=" * 78)

    if bad:
        print(f"\nBROKEN ({len(bad)}):\n")
        by_kpi: Dict[str, List[Dict[str, Any]]] = {}
        for r in bad:
            by_kpi.setdefault(r["kpi"], []).append(r)
        for kpi, items in sorted(by_kpi.items()):
            print(f"  {kpi}")
            for r in items:
                print(f"      [{r['shape']}] {r['status']}: {r['detail']}")

    if skipped:
        print(f"\nNOT CHECKED ({len(skipped)}): {skipped[0]['detail']}")

    print(f"\nSummary: {len(ok)} ok, {len(bad)} broken, {len(skipped)} not checked\n")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", help="only configs in this module")
    ap.add_argument("--kpi", help="only this config")
    ap.add_argument("--sql-only", action="store_true",
                    help="generate SQL but do not contact Postgres")
    ap.add_argument("--json", help="write the full result set here")
    args = ap.parse_args()

    rows = asyncio.run(run(args.module, args.kpi, args.sql_only))
    code = report(rows)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, default=str)
        print(f"wrote {args.json}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())