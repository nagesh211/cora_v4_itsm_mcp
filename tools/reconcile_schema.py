#!/usr/bin/env python3
"""Reconcile ``schema_v3.yaml`` against the live database — the drift gate.

Why this is the prerequisite under everything else
--------------------------------------------------
``schema_v3.yaml`` is the planner's whole world: ``sql_builder`` resolves every column
through it, ``value_resolver`` validates filter values against its ``possible_values``,
and ``compose_metric`` scores candidate tables from it. When it disagrees with the
database, the failure is silent and confident rather than loud:

  * a column declared but ABSENT produces SQL referencing a column that does not exist —
    the query dies at execution, after the answer has been promised
  * a column present but UNDECLARED is invisible to the planner. The
    ``availability-percentage`` KPI scopes on ``support_group_system_id`` and
    ``hypercare_project_system_id``; neither is declared, so no predicate or filter can
    reproduce that KPI's population (``availability_impacting`` carries a caveat saying
    exactly this)
  * a wrong TYPE changes the SQL shape. ``sql_builder._is_array`` decides between
    ``a.col`` and ``CROSS JOIN LATERAL unnest(a.col)`` from the declared type alone, so
    ``text[]`` declared on a scalar column emits SQL Postgres rejects — and a scalar
    declared on an array silently compares a whole array to one value
  * wrong ``possible_values`` makes ``value_resolver`` reject values that really occur.
    This is already measured: ``target`` declares ``RESPONSE/RESOLUTION`` and holds
    ``8 HOURS, 4 HOURS``; ``priority_description`` declares P-codes and holds ``1, 2, 3``;
    ``active_indicator_type`` is not ``NO/YES`` but service classes; ``outage_type``
    declares one value where the column is NULL on 304,696 of 320,814 rows. It is why
    ``predicates.json`` marks its values ``resolved`` to BYPASS the schema domain —
    a workaround for this file being wrong.

Severities
----------
``E`` findings are defects that produce wrong SQL or a wrong refusal, and set a non-zero
exit so this can gate a build. ``W`` findings need a human decision (which undeclared
columns are worth declaring, and with what role/canonical vocabulary) and never fail.

  E1  declared table absent from the database
  E2  declared column absent from the table
  E3  declared type family disagrees (array vs scalar, text vs numeric, …)
  E4  a declared possible_value matches no row
  W1  column present in the database but not declared
  W2  real domain contains values the schema omits

``unreadable`` is tracked separately and never counted as drift: this link has been
observed to time out mid-run, and a network blip must not be reported as a data-model
defect (same discipline as ``predicate_registry.verify``).

Two phases, because they cost very differently
----------------------------------------------
``--structure`` (default) is ONE ``information_schema`` query per schema — seconds, and
enough for CI. ``--domains`` measures a ``GROUP BY`` per column that declares
``possible_values`` (141 of them) and takes minutes, so it is opt-in.

Writing a corrected schema
--------------------------
``--out FILE`` never touches ``schema_v3.yaml``. It writes a reconciled copy applying
only mechanical corrections — drop absent columns, fix type families, replace or drop
``possible_values`` from the measured domain — and leaves every judgment call alone: it
does NOT add undeclared columns (they need a role, and often a ``canonical``/``alias``
vocabulary, which only a human can assign) and does NOT drop tables (a table absent here
may exist in another deployment). Review the diff, then swap the file in.

USAGE
  python tools/reconcile_schema.py                      # structural report + exit code
  python tools/reconcile_schema.py --all                # also measure value domains
  python tools/reconcile_schema.py --table itsm_change.tbl_change
  python tools/reconcile_schema.py --all --out schema_v3.reconciled.yaml
  python tools/reconcile_schema.py --json               # machine-readable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_ROOT, ".env"), override=False)

from cora_mcp import db  # noqa: E402
from cora_mcp.logging_config import get_logger, setup_logging  # noqa: E402
from cora_mcp.schema_loader import DEFAULT_SCHEMA_PATH  # noqa: E402

log = get_logger("reconcile_schema")

_RETRIES = 3
# Above this, a column is a free-text/high-cardinality field and an enumerated domain is
# meaningless — the schema should declare no possible_values at all rather than a sample.
_MAX_DOMAIN = 25

# Postgres udt_name -> the family that changes generated SQL. Only a FAMILY difference is
# reported: varchar vs text never changes behaviour, whereas array vs scalar decides
# whether the builder emits CROSS JOIN LATERAL unnest(...).
_FAMILIES = {
    "text": "text", "varchar": "text", "bpchar": "text", "char": "text", "name": "text",
    "int2": "int", "int4": "int", "int8": "int",
    "numeric": "numeric", "float4": "numeric", "float8": "numeric", "money": "numeric",
    "bool": "bool",
    "timestamp": "timestamp", "timestamptz": "timestamp",
    "date": "date", "time": "time", "timetz": "time", "interval": "interval",
    "json": "json", "jsonb": "json", "uuid": "text",
}


def _family(raw: Any) -> str:
    """Normalize either vocabulary (the schema's ``text[]``/``int4``/``character
    varying``, or Postgres' ``udt_name``) to a behaviour family. ``array:<inner>`` keeps
    the distinction the builder actually acts on."""
    s = str(raw or "").strip().lower()
    if not s:
        return "unknown"
    if s.endswith("[]"):
        return "array:%s" % _family(s[:-2])
    if s.startswith("_"):                       # Postgres spells text[] as _text
        return "array:%s" % _family(s[1:])
    s = s.replace(" ", "")
    for token, fam in (("withouttimezone", "timestamp"), ("withtimezone", "timestamp")):
        if s.endswith(token):
            s = s[: -len(token)]
    if s in _FAMILIES:
        return _FAMILIES[s]
    if s.startswith("charactervarying") or s.startswith("character"):
        return "text"
    if s.startswith("timestamp"):
        return "timestamp"
    if s.startswith("double") or s.startswith("real") or s.startswith("decimal"):
        return "numeric"
    if s.startswith("int") or s in ("bigint", "smallint"):
        return "int"
    if s.startswith("bool"):
        return "bool"
    return s


def _load_schema(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _declared_time_fields(doc: dict) -> Dict[str, dict]:
    """{table fqn -> its ``time`` block}. Checked separately and at the highest severity:
    ``sql_builder`` reads ``table_time_field()`` for every ``period``, so a time field
    that does not exist — or is not temporal — breaks EVERY dated question on that table,
    not just one column's worth."""
    out: Dict[str, dict] = {}
    for m in doc.get("modules") or []:
        for e in m.get("entities") or []:
            for t in e.get("tables") or []:
                if t.get("time"):
                    out.setdefault(t.get("name"), t["time"])
    return out


def _declared(doc: dict) -> Dict[str, Dict[str, dict]]:
    """{table fqn -> {column name -> column dict}}. The same table can be listed under
    several entities; the first wins, matching ``SchemaLoader._by_table``."""
    out: Dict[str, Dict[str, dict]] = {}
    for m in doc.get("modules") or []:
        for e in m.get("entities") or []:
            for t in e.get("tables") or []:
                out.setdefault(t.get("name"), {})
                for c in t.get("columns") or []:
                    out[t["name"]].setdefault(c.get("name"), c)
    return out


async def _run(sql: str, params: Optional[list] = None, limit: int = 100000,
               connection: str = "vtx5"):
    """Execute with backoff. Returns rows, or raises the last error.

    Backoff rather than immediate retries because the observed failure is the TCP connect
    timing out, not the query: hammering it again in the same millisecond reproduces the
    same timeout. A reconciliation run that dies halfway would otherwise report the
    unreached half of the schema as clean."""
    last = None
    for attempt in range(_RETRIES):
        try:
            out = await db.execute("postgres", connection, sql, params or [], limit=limit)
            return out.get("rows") or []
        except Exception as exc:
            last = exc
            if attempt < _RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
    raise last


async def _live_columns(schemas: List[str], connection: str) -> Dict[str, Dict[str, str]]:
    """{table fqn -> {column -> udt_name}} for every table in the given schemas."""
    inlist = ",".join("'%s'" % s for s in sorted(schemas))
    sql = ("select table_schema||'.'||table_name as t, column_name, udt_name "
           "from information_schema.columns where table_schema in (%s)" % inlist)
    rows = await _run(sql, connection=connection)
    live: Dict[str, Dict[str, str]] = defaultdict(dict)
    for r in rows:
        live[r["t"]][r["column_name"]] = r["udt_name"]
    return live


async def structure(declared: Dict[str, Dict[str, dict]], connection: str,
                    time_fields: Optional[Dict[str, dict]] = None) -> Dict[str, Any]:
    schemas = sorted({t.split(".")[0] for t in declared if t and "." in t})
    live = await _live_columns(schemas, connection)

    findings: List[dict] = []
    for table, tf in sorted((time_fields or {}).items()):
        if table not in live or table not in declared:
            continue                          # already reported as E1
        field = tf.get("field")
        folded = {c.lower(): c for c in live[table]}
        real = folded.get(str(field).lower()) if field else None
        if not real:
            findings.append({
                "severity": "E6", "table": table, "column": field,
                "detail": "declared time.field does not exist — EVERY question with a "
                          "period on this table fails"})
            continue
        fam = _family(live[table][real])
        if fam not in ("timestamp", "date"):
            findings.append({
                "severity": "E6", "table": table, "column": field,
                "actual": live[table][real],
                "detail": "declared time.field is %s, not a date/timestamp — a period "
                          "filter compares a %s to a date window" % (fam, fam)})
    for table in sorted(declared):
        if table not in live:
            findings.append({"severity": "E1", "table": table,
                             "detail": "declared table is absent from the database"})
            continue
        # Postgres stores unquoted identifiers folded to lower case, so a declaration
        # differing only in case names a real column — it must NOT be reported absent and
        # above all must not be DROPPED by --out. It is still a defect: schema lookups
        # (``SchemaLoader.table_columns``) are case-sensitive dict hits, so a question
        # asking for `business_name` misses a column declared `BUSINESS_NAME` and the
        # dimension is silently dropped. The fix is a rename, not a deletion.
        folded = {c.lower(): c for c in live[table]}
        for col, ci in sorted(declared[table].items()):
            actual_name = live[table].get(col) is not None and col or folded.get(col.lower())
            if actual_name is None:
                findings.append({
                    "severity": "E2", "table": table, "column": col,
                    "detail": "declared column is absent; any query naming it fails at "
                              "execution"})
                continue
            if actual_name != col:
                findings.append({
                    "severity": "E5", "table": table, "column": col,
                    "actual": actual_name,
                    "detail": "declared spelling differs in case from the database "
                              "(%r vs %r); schema lookups are case-sensitive, so a "
                              "question naming the real column resolves to nothing and "
                              "the dimension is dropped" % (col, actual_name)})
            want = _family(ci.get("type"))
            got = _family(live[table][actual_name])
            if want != got and "unknown" not in (want, got):
                findings.append({
                    "severity": "E3", "table": table, "column": col,
                    "declared": ci.get("type"), "actual": live[table][actual_name],
                    "detail": "type family %s vs %s%s" % (
                        want, got,
                        " — array/scalar decides CROSS JOIN LATERAL unnest()"
                        if want.startswith("array") != got.startswith("array") else "")})
        declared_folded = {c.lower() for c in declared[table]}
        for col in sorted(live[table]):
            if col.lower() not in declared_folded:
                findings.append({
                    "severity": "W1", "table": table, "column": col,
                    "actual": live[table][col],
                    "detail": "present in the database, invisible to the planner"})
    return {"findings": findings, "live": {k: dict(v) for k, v in live.items()}}


async def domains(declared: Dict[str, Dict[str, dict]], live: Dict[str, Dict[str, str]],
                  connection: str) -> Dict[str, Any]:
    """Measure every column that DECLARES ``possible_values``. Bounded on purpose: that
    is exactly the set where a wrong domain makes ``value_resolver`` reject a real
    value, and measuring every dimension column instead would be hundreds of scans."""
    findings: List[dict] = []
    unreadable: List[str] = []
    measured: Dict[str, Dict[str, list]] = defaultdict(dict)
    for table in sorted(declared):
        if table not in live:
            continue
        for col, ci in sorted(declared[table].items()):
            want = ci.get("possible_values")
            if not want or col not in live[table]:
                continue
            is_array = _family(live[table][col]).startswith("array")
            expr = "unnest(%s)" % col if is_array else col
            sql = ("select x as v from (select distinct %s as x from %s) s "
                   "where x is not null limit %d" % (expr, table, _MAX_DOMAIN + 1))
            try:
                rows = await _run(sql, limit=_MAX_DOMAIN + 1, connection=connection)
            except Exception as exc:
                unreadable.append("%s.%s: %s" % (table, col, str(exc)[:90]))
                continue
            # Blank is stored data, not a domain member: writing '' into
            # possible_values would advertise it as a filterable value and let
            # value_resolver accept an empty filter. NULL is already excluded by the
            # query; '' has to be excluded here (same rule as gen_predicates._measure).
            real = [r["v"] for r in rows if str(r["v"]).strip() != ""]
            measured[table][col] = real
            if len(real) > _MAX_DOMAIN:
                findings.append({
                    "severity": "W2", "table": table, "column": col,
                    "detail": "more than %d distinct values — an enumerated domain is "
                              "meaningless here; possible_values should be dropped"
                              % _MAX_DOMAIN})
                continue
            norm_real = {str(v).strip().lower() for v in real}
            missing = [v for v in want
                       if str(v).strip().lower() not in norm_real]
            extra = [v for v in real
                     if str(v).strip().lower() not in
                     {str(w).strip().lower() for w in want}]
            if missing:
                findings.append({
                    "severity": "E4", "table": table, "column": col,
                    "declared": list(want), "actual": real, "never_occurs": missing,
                    "detail": "declared value(s) match no row — value_resolver accepts a "
                              "value the table never holds, and rejects ones it does"})
            elif extra:
                findings.append({
                    "severity": "W2", "table": table, "column": col,
                    "declared": list(want), "actual": real, "undeclared_values": extra,
                    "detail": "real domain has values the schema omits"})
    return {"findings": findings, "unreadable": unreadable,
            "measured": {k: dict(v) for k, v in measured.items()}}


def apply_fixes(doc: dict, findings: List[dict],
                measured: Dict[str, Dict[str, list]]) -> Tuple[dict, List[str]]:
    """Return (corrected doc, list of what changed).

    Mechanical corrections only. Anything needing a decision is deliberately left for a
    human: an undeclared column needs a role and often a ``canonical``/``alias``
    vocabulary, and a missing table may simply not exist in THIS environment.
    """
    drop = {(f["table"], f["column"]) for f in findings if f["severity"] == "E2"}
    rename = {(f["table"], f["column"]): f["actual"]
              for f in findings if f["severity"] == "E5"}
    retype = {(f["table"], f["column"]): f["actual"]
              for f in findings if f["severity"] == "E3"}
    bad_domain = {(f["table"], f["column"]) for f in findings if f["severity"] == "E4"}
    wide_domain = {(f["table"], f["column"]) for f in findings
                   if f["severity"] == "W2" and "meaningless" in f.get("detail", "")}
    changes: List[str] = []

    for m in doc.get("modules") or []:
        for e in m.get("entities") or []:
            for t in e.get("tables") or []:
                table = t.get("name")
                kept = []
                for c in t.get("columns") or []:
                    key = (table, c.get("name"))
                    if key in drop:
                        changes.append("drop column %s.%s (absent from the database)"
                                       % key)
                        continue
                    if key in rename:
                        # Rename, never drop: the column exists, only the declared
                        # spelling is wrong.
                        changes.append("rename %s.%s -> %s (case mismatch)"
                                       % (table, c["name"], rename[key]))
                        c["name"] = rename[key]
                    if key in retype:
                        changes.append("retype %s.%s: %r -> %r"
                                       % (table, c["name"], c.get("type"),
                                          retype[key]))
                        c["type"] = retype[key]
                    if key in wide_domain and "possible_values" in c:
                        changes.append("drop possible_values on %s.%s (too many "
                                       "distinct values to enumerate)" % key)
                        c.pop("possible_values")
                    elif key in bad_domain:
                        real = measured.get(table, {}).get(c["name"])
                        if real is not None:
                            changes.append("replace possible_values on %s.%s: %r -> %r"
                                           % (table, c["name"],
                                              c.get("possible_values"), real))
                            c["possible_values"] = real
                    kept.append(c)
                t["columns"] = kept
    return doc, changes


def _print_report(struct: dict, dom: Optional[dict]) -> Tuple[int, int]:
    findings = list(struct["findings"]) + list((dom or {}).get("findings") or [])
    by_sev: Dict[str, List[dict]] = defaultdict(list)
    for f in findings:
        by_sev[f["severity"]].append(f)

    titles = {
        "E1": "declared table ABSENT from the database",
        "E2": "declared column ABSENT — queries naming it fail at execution",
        "E3": "declared TYPE disagrees — changes the SQL the builder emits",
        "E4": "declared possible_value matches NO row — value_resolver rejects real values",
        "E5": "declared name differs in CASE — schema lookups are case-sensitive",
        "E6": "declared time.field is missing or not temporal — breaks EVERY dated question",
        "W1": "in the database but NOT DECLARED — invisible to the planner",
        "W2": "possible_values incomplete or unenumerable",
    }
    for sev in ("E6", "E1", "E2", "E5", "E3", "E4", "W1", "W2"):
        items = by_sev.get(sev) or []
        if not items:
            continue
        print("\n%s  %s  (%d)" % (sev, titles[sev], len(items)))
        print("-" * 100)
        if sev == "W1":                      # summarise: can be hundreds
            per_table: Dict[str, List[str]] = defaultdict(list)
            for f in items:
                per_table[f["table"]].append(f["column"])
            for table, cols in sorted(per_table.items()):
                print("   %-46s %2d  %s" % (table, len(cols), ", ".join(sorted(cols)[:8])))
        else:
            for f in items:
                where = f["table"] + ("." + f["column"] if f.get("column") else "")
                print("   %-58s %s" % (where, f["detail"]))
                if f.get("declared") is not None and sev in ("E3", "E4", "W2"):
                    print("   %-58s declared=%r" % ("", f["declared"]))
                    print("   %-58s actual  =%r" % ("", f.get("actual")))
    errors = sum(len(by_sev.get(s) or []) for s in ("E1", "E2", "E3", "E4", "E5", "E6"))
    warns = sum(len(by_sev.get(s) or []) for s in ("W1", "W2"))
    unreadable = (dom or {}).get("unreadable") or []
    if unreadable:
        print("\nUNREADABLE (infrastructure, not drift) (%d)" % len(unreadable))
        for u in unreadable:
            print("   " + u)
    print("\n%d error(s), %d warning(s), %d unreadable" % (errors, warns, len(unreadable)))
    print("VERDICT:", "ok" if not errors else "SCHEMA DRIFT")
    return errors, warns


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="also measure value domains (slow: a scan per column that "
                         "declares possible_values)")
    ap.add_argument("--domains", action="store_true", help="domains only, no structure")
    ap.add_argument("--table", default=None, help="restrict to one table fqn")
    ap.add_argument("--schema-path", default=DEFAULT_SCHEMA_PATH)
    ap.add_argument("--connection", default="vtx5")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--out", default=None,
                    help="write a reconciled schema here (never edits the original)")
    args = ap.parse_args()
    setup_logging()

    doc = _load_schema(args.schema_path)
    decl = _declared(doc)
    if args.table:
        decl = {k: v for k, v in decl.items() if k == args.table}
        if not decl:
            print("no declared table %r" % args.table)
            return 2

    async def _go():
        struct = await structure(decl, args.connection,
                                 _declared_time_fields(doc))
        dom = None
        if args.all or args.domains:
            dom = await domains(decl, struct["live"], args.connection)
        if args.as_json:
            print(json.dumps({"structure": struct["findings"],
                              "domains": (dom or {}).get("findings") or [],
                              "unreadable": (dom or {}).get("unreadable") or []},
                             indent=2, default=str))
            errors = len([f for f in struct["findings"]
                          if f["severity"].startswith("E")])
            errors += len([f for f in ((dom or {}).get("findings") or [])
                           if f["severity"].startswith("E")])
        else:
            errors, _warns = _print_report(struct, dom)

        if args.out:
            findings = list(struct["findings"]) + list((dom or {}).get("findings") or [])
            fixed, changes = apply_fixes(_load_schema(args.schema_path), findings,
                                         (dom or {}).get("measured") or {})
            with open(args.out, "w", encoding="utf-8") as fh:
                yaml.safe_dump(fixed, fh, sort_keys=False, default_flow_style=False,
                               allow_unicode=True, width=10000)
            print("\nwrote %s with %d mechanical correction(s):" % (args.out, len(changes)))
            for c in changes[:40]:
                print("   " + c)
            if len(changes) > 40:
                print("   ... and %d more" % (len(changes) - 40))
            print("\nNOT applied (need a human): undeclared columns (W1) need a role and "
                  "often canonical/alias vocabulary; absent tables (E1) may exist in "
                  "another environment. Review the diff before swapping the file in.")
        await db.close_pools()
        return 1 if errors else 0

    return asyncio.run(_go())


if __name__ == "__main__":
    raise SystemExit(_main())