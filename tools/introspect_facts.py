#!/usr/bin/env python3
"""introspect_facts.py — measure the database instead of describing it.

``schema_v3.yaml`` is authored: a human (or a one-off transformer) wrote down each
column's type, role and ``possible_values``, and everything downstream trusts it.
``cora_mcp.value_resolver`` rejects a filter value that is not in the declared
``possible_values``; ``cora_mcp.column_resolver`` picks a column by its declared
role. When the YAML and the database disagree, the YAML wins and the answer is
wrong — and the repo already knows this happens: ``sql_builder.Filter.resolved``
exists specifically to bypass the declared domain because *"those were measured to
be wrong on several columns"*.

This script measures the facts the YAML is guessing at:

  ``cardinality``   distinct values (``pg_stats.n_distinct``, negative = a ratio of
                    the row count) — tells you which of ``status_name`` / ``state``
                    is the real status column rather than a near-duplicate.
  ``null_frac``     fraction of NULLs — a column that is 98% NULL is not the
                    dimension anybody means.
  ``samples``       five real values, so a filter can be written against what is
                    actually stored ('CLOSED', not 'completed').
  ``domain``        the full distinct set for low-cardinality columns — this is what
                    ``possible_values`` should have been.
  ``min``/``max``   for time columns: the real data bounds, so a question about a
                    window with no data can say so instead of answering ``0``.

Two modes:

  (default)   write ``schema_facts.json`` — the measured facts, for tooling and for
              a human to read before editing the YAML.
  ``--drift`` compare the YAML against the database and report only the
              disagreements: columns the YAML declares that no longer exist, type
              mismatches, and declared ``possible_values`` that the data contradicts.
              Exits 1 when drift is found, so it can gate a deploy.

Every statistic is wrapped: a failure degrades to fewer facts for that column and
never aborts the run, because a partial measurement is still better than the
assumption it replaces.

Usage::

    python tools/introspect_facts.py                       # write schema_facts.json
    python tools/introspect_facts.py --drift               # report YAML vs database
    python tools/introspect_facts.py --table itsm_incident.tbl_all_incidents
    python tools/introspect_facts.py --max-domain 50       # widen the domain capture
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

from cora_mcp.logging_config import get_logger                  # noqa: E402
from cora_mcp.schema_loader import get_loader                   # noqa: E402

log = get_logger(__name__)

FACTS_PATH = os.path.join(_ROOT, "schema_facts.json")
DEFAULT_MAX_DOMAIN = 30
SAMPLE_N = 5

_TEXTY = ("char", "text", "varchar", "citext")
_TIMEY = ("date", "timestamp", "time")


async def _safe(coro, default=None, what: str = ""):
    """Run a stats query; degrade to ``default`` rather than aborting the sweep."""
    try:
        return await coro
    except Exception as exc:
        log.debug("stat failed (%s): %s", what, exc)
        return default


async def table_facts(con, fqn: str, declared: Dict[str, dict],
                      max_domain: int) -> Dict[str, Any]:
    schema, table = fqn.split(".", 1)

    live_cols = await _safe(con.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema=$1 AND table_name=$2 ORDER BY ordinal_position",
        schema, table), default=[], what=f"columns {fqn}")
    if not live_cols:
        return {"table": fqn, "exists": False, "columns": {}}

    row_count = await _safe(con.fetchval(
        "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass($1)", fqn),
        default=None, what=f"reltuples {fqn}")
    if not row_count or row_count < 0:
        row_count = await _safe(con.fetchval(f"SELECT count(*) FROM {fqn}"),
                                default=0, what=f"count {fqn}")

    stats = {r["attname"]: r for r in await _safe(con.fetch(
        "SELECT attname, n_distinct, null_frac FROM pg_stats "
        "WHERE schemaname=$1 AND tablename=$2", schema, table),
        default=[], what=f"pg_stats {fqn}")}

    out: Dict[str, Any] = {}
    for rec in live_cols:
        name, dtype = rec["column_name"], rec["data_type"]
        col: Dict[str, Any] = {"type": dtype, "nullable": rec["is_nullable"] == "YES"}

        st = stats.get(name)
        if st is not None:
            nd = st["n_distinct"]
            if nd is not None:
                # pg encodes a negative n_distinct as a ratio of the row count.
                col["cardinality"] = int(-nd * row_count) if nd < 0 else int(nd)
            if st["null_frac"] is not None:
                col["null_frac"] = round(float(st["null_frac"]), 4)

        low = dtype.lower()
        if any(k in low for k in _TIMEY):
            row = await _safe(con.fetchrow(
                f'SELECT min("{name}") AS lo, max("{name}") AS hi FROM {fqn}'),
                default=None, what=f"minmax {fqn}.{name}")
            if row:
                col["min"] = str(row["lo"]) if row["lo"] is not None else None
                col["max"] = str(row["hi"]) if row["hi"] is not None else None
        elif any(k in low for k in _TEXTY):
            card = col.get("cardinality")
            if card is not None and 0 < card <= max_domain:
                vals = await _safe(con.fetch(
                    f'SELECT DISTINCT "{name}" AS v FROM {fqn} '
                    f'WHERE "{name}" IS NOT NULL ORDER BY 1 LIMIT {max_domain + 1}'),
                    default=[], what=f"domain {fqn}.{name}")
                if vals and len(vals) <= max_domain:
                    col["domain"] = [str(r["v"]) for r in vals]

        if "domain" not in col:
            samples = await _safe(con.fetch(
                f'SELECT "{name}" AS v FROM {fqn} WHERE "{name}" IS NOT NULL '
                f'LIMIT {SAMPLE_N}'), default=[], what=f"sample {fqn}.{name}")
            if samples:
                col["samples"] = [str(r["v"]) for r in samples]

        out[name] = col

    return {"table": fqn, "exists": True, "row_count": int(row_count or 0),
            "columns": out}


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------
def compare(fqn: str, declared: Dict[str, dict], measured: Dict[str, Any]) -> List[dict]:
    """Every disagreement between the YAML and the database, as actionable items."""
    issues: List[dict] = []
    if not measured.get("exists"):
        return [{"table": fqn, "column": None, "kind": "missing_table",
                 "detail": "declared in schema_v3.yaml but not present in the database"}]

    live = measured["columns"]
    for name, spec in declared.items():
        if name not in live:
            issues.append({"table": fqn, "column": name, "kind": "missing_column",
                           "detail": "declared in schema_v3.yaml, absent from the "
                                     "database (queries using it will fail)"})
            continue
        lo = live[name]

        declared_type = (spec.get("type") or "").lower()
        live_type = (lo.get("type") or "").lower()
        if declared_type and not _types_agree(declared_type, live_type):
            issues.append({"table": fqn, "column": name, "kind": "type_mismatch",
                           "detail": f"declared {declared_type!r}, database says "
                                     f"{live_type!r}"})

        pv = spec.get("possible_values")
        domain = lo.get("domain")
        if pv and domain:
            declared_set = {str(v).strip().lower() for v in pv}
            live_set = {v.strip().lower() for v in domain}
            phantom = sorted(declared_set - live_set)
            unlisted = sorted(live_set - declared_set)
            if phantom:
                issues.append({
                    "table": fqn, "column": name, "kind": "phantom_values",
                    "detail": f"possible_values lists {phantom} which do not occur in "
                              f"the data — a filter on them is rejected for no reason"})
            if unlisted:
                issues.append({
                    "table": fqn, "column": name, "kind": "unlisted_values",
                    "detail": f"the data contains {unlisted[:8]} which possible_values "
                              f"omits — value_resolver will reject these as invalid"})
    return issues


def _types_agree(declared: str, live: str) -> bool:
    families = [
        {"varchar", "character varying", "text", "char", "character", "citext"},
        {"int", "integer", "int4", "int8", "bigint", "smallint", "int2"},
        {"numeric", "decimal", "real", "double precision", "float4", "float8"},
        {"timestamp", "timestamp without time zone", "timestamp with time zone",
         "timestamptz", "date"},
        {"bool", "boolean"},
    ]
    d, l = declared.strip(), live.strip()
    if d == l:
        return True
    return any(d in fam and l in fam for fam in families)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
async def run(tables: Optional[List[str]], max_domain: int) -> Dict[str, Any]:
    from cora_mcp import db as dbmod

    dsn = dbmod.resolve_dsn(None)
    if not dsn:
        raise SystemExit("no Postgres DSN configured. Set CORA_PG_DSN in .env")
    import asyncpg

    loader = get_loader()
    targets = tables or sorted(loader._by_table)
    con = await asyncpg.connect(dsn=dsn, timeout=30)
    facts: Dict[str, Any] = {}
    try:
        for fqn in targets:
            declared = loader.table_columns(fqn)
            log.info("introspecting %s (%d declared column(s))", fqn, len(declared))
            facts[fqn] = await table_facts(con, fqn, declared, max_domain)
    finally:
        await con.close()
    return facts


def report_drift(facts: Dict[str, Any]) -> int:
    loader = get_loader()
    issues: List[dict] = []
    for fqn, measured in facts.items():
        issues.extend(compare(fqn, loader.table_columns(fqn), measured))

    print("\n" + "=" * 78)
    print(f"schema drift — {len(facts)} table(s) checked")
    print("=" * 78)
    if not issues:
        print("\nno drift: schema_v3.yaml agrees with the database.\n")
        return 0

    by_kind: Dict[str, List[dict]] = {}
    for i in issues:
        by_kind.setdefault(i["kind"], []).append(i)
    for kind, items in sorted(by_kind.items()):
        print(f"\n{kind.upper().replace('_', ' ')} ({len(items)}):\n")
        for i in items:
            where = f"{i['table']}.{i['column']}" if i["column"] else i["table"]
            print(f"  {where}")
            print(f"      {i['detail']}")
    print(f"\n{len(issues)} disagreement(s) between schema_v3.yaml and the database\n")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", action="append", help="only this table (repeatable)")
    ap.add_argument("--drift", action="store_true",
                    help="report YAML/database disagreements instead of writing facts")
    ap.add_argument("--max-domain", type=int, default=DEFAULT_MAX_DOMAIN,
                    help=f"capture the full value set for columns with at most this "
                         f"many distinct values (default {DEFAULT_MAX_DOMAIN})")
    ap.add_argument("--out", default=FACTS_PATH)
    args = ap.parse_args()

    facts = asyncio.run(run(args.table, args.max_domain))

    if args.drift:
        return report_drift(facts)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=2, default=str)
    n_cols = sum(len(t.get("columns") or {}) for t in facts.values())
    print(f"wrote {args.out}: {len(facts)} table(s), {n_cols} column(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())