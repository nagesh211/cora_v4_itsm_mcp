#!/usr/bin/env python3
"""check_config_refs.py — do the JSON registries still match the schema?

``predicates.json``, ``filter_aliases.json`` and ``value_aliases.json`` name tables
and columns. ``schema_v3.yaml`` decides which names exist. Nothing keeps the two in
step, and when they diverge the failure is quiet: :mod:`cora_mcp.composer` scores a
binding it cannot satisfy, drops it, and either anchors somewhere else or refuses —
so a question stops working without anything obviously being broken.

A live example this script was written to catch: ``predicates.json`` binds the
``major_incident`` predicate to ``tbl_all_incidents.major_incident_indicator``. That
column exists in the *database* (the server has run queries against it), but the
current ``schema_v3.yaml`` does not declare it — so the binding is dead even though
the SQL it would produce is perfectly valid.

That direction matters. A reference can be broken two ways:

  ``missing``      the column is in neither the schema nor (probably) the database —
                   the reference is simply wrong and should be removed or renamed.
  ``undeclared``   the column is used by working SQL but absent from the schema —
                   the *schema* is what needs fixing, not the reference.

This script cannot tell those apart on its own (it never touches the database), so
it reports the reference as broken and points at ``tools/introspect_facts.py
--drift`` to settle which side is wrong.

Offline: needs nothing but this repo. Exits 1 if any reference is broken.

Usage::

    python tools/check_config_refs.py
    python tools/check_config_refs.py --json refs.json
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp.logging_config import get_logger                  # noqa: E402
from cora_mcp.schema_loader import get_loader                   # noqa: E402

log = get_logger(__name__)


def _suggest(loader, table: str, column: str) -> str:
    cols = list(loader.table_columns(table) or {})
    close = difflib.get_close_matches(column, cols, n=3, cutoff=0.5)
    if close:
        return f" did you mean {close}?"
    elsewhere = loader.tables_with_column(column)
    if elsewhere:
        return f" (it exists on {elsewhere[:3]})"
    return " (not declared on any table)"


# ---------------------------------------------------------------------------
# predicates.json
# ---------------------------------------------------------------------------
def check_predicates(loader) -> List[dict]:
    path = os.path.join(_ROOT, "predicates.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    issues: List[dict] = []
    for pname, pred in (doc.get("predicates") or {}).items():
        bindings = pred.get("bindings") or []
        live = 0
        for i, binding in enumerate(bindings):
            table = binding.get("table")
            where = f"predicates.json::{pname}.bindings[{i}]"
            if not table or not loader.get_table(table):
                issues.append({"where": where, "kind": "unknown_table",
                               "detail": f"table {table!r} is not in schema_v3.yaml"})
                continue
            broken = False
            for cond in binding.get("conditions") or []:
                col = cond.get("field")
                if col and not loader.column_info(table, col):
                    broken = True
                    issues.append({
                        "where": where, "kind": "unknown_column",
                        "detail": f"{table}.{col} is not in schema_v3.yaml."
                                  f"{_suggest(loader, table, col)}"})
            if not broken:
                live += 1
        if bindings and live == 0:
            issues.append({
                "where": f"predicates.json::{pname}", "kind": "predicate_dead",
                "detail": f"every one of the {len(bindings)} binding(s) is broken — "
                          f"this predicate can no longer be applied to any table"})

        gk = pred.get("grain_key")
        if gk and not any(loader.column_info(b.get("table") or "", gk)
                          for b in bindings):
            issues.append({
                "where": f"predicates.json::{pname}", "kind": "unknown_grain_key",
                "detail": f"grain_key {gk!r} is not a column of any bound table; "
                          f"EXISTS semi-joins for this predicate cannot be built"})
    return issues


# ---------------------------------------------------------------------------
# filter_aliases.json / value_aliases.json
# ---------------------------------------------------------------------------
def check_value_aliases(loader) -> List[dict]:
    """Only ``value_aliases.json``'s ``by_column`` block names real columns.

    Its ``global`` block maps value synonyms that apply anywhere, and
    ``filter_aliases.json`` is deliberately NOT checked here: its keys are the
    canonical *filter keys* used by the KPI configs ("sector", "division"), which
    each config then maps to its own physical column. Those are supposed not to be
    schema columns, and flagging them would be noise that trains people to ignore
    this report.
    """
    path = os.path.join(_ROOT, "value_aliases.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    issues: List[dict] = []
    for column in (doc.get("by_column") or {}):
        if column.startswith("_"):
            continue
        if not loader.tables_with_column(column):
            issues.append({
                "where": f"value_aliases.json::by_column.{column}",
                "kind": "unknown_column",
                "detail": f"{column!r} is not a column on any table in "
                          f"schema_v3.yaml, so its value synonyms can never apply"})
    return issues


# ---------------------------------------------------------------------------
# relationships
# ---------------------------------------------------------------------------
def check_relationships(loader) -> List[dict]:
    issues: List[dict] = []
    for module_name in loader.module_names():
        module = loader.get_module(module_name) or {}
        rels = module.get("relationships") or []
        if not rels:
            issues.append({
                "where": f"schema_v3.yaml::{module_name}", "kind": "no_relationships",
                "detail": "module declares no relationships, so every cross-entity "
                          "question fails with NoJoinPathError. Run "
                          "tools/harvest_relationships.py and "
                          "tools/infer_relationships.py --from-db --write"})
            continue
        for rel in rels:
            where = f"schema_v3.yaml::{module_name}.{rel.get('name')}"
            for side in ("left", "right", "via"):
                fqn = rel.get(side)
                if fqn and not loader.get_table(fqn):
                    issues.append({"where": where, "kind": "unknown_table",
                                   "detail": f"{side}={fqn!r} is not in the schema"})
            for spec, tkey, ckey in (("join_on", "left", "left_col"),
                                     ("join_on", "right", "right_col"),
                                     ("left_on", "left", "left_col"),
                                     ("right_on", "right", "right_col")):
                block = rel.get(spec)
                if not block or ckey not in block:
                    continue
                table, col = rel.get(tkey), block[ckey]
                if table and loader.get_table(table) and not loader.column_info(table, col):
                    issues.append({
                        "where": where, "kind": "unknown_column",
                        "detail": f"{spec}.{ckey}={col!r} is not a column of {table}."
                                  f"{_suggest(loader, table, col)}"})
            for spec in ("left_on", "right_on"):
                block = rel.get(spec) or {}
                via = rel.get("via")
                vcol = block.get("via_col")
                if via and vcol and loader.get_table(via) \
                        and not loader.column_info(via, vcol):
                    issues.append({
                        "where": where, "kind": "unknown_column",
                        "detail": f"{spec}.via_col={vcol!r} is not a column of {via}."
                                  f"{_suggest(loader, via, vcol)}"})
    return issues


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="write the findings here")
    args = ap.parse_args()

    loader = get_loader()
    issues: List[dict] = []
    issues += check_predicates(loader)
    issues += check_value_aliases(loader)
    issues += check_relationships(loader)

    print("\n" + "=" * 78)
    print("config -> schema reference check (offline)")
    print("=" * 78)
    if not issues:
        print("\nevery table/column referenced by the registries exists in "
              "schema_v3.yaml.\n")
        return 0

    by_kind: Dict[str, List[dict]] = {}
    for i in issues:
        by_kind.setdefault(i["kind"], []).append(i)
    for kind, items in sorted(by_kind.items()):
        print(f"\n{kind.upper().replace('_', ' ')} ({len(items)}):\n")
        for i in items:
            print(f"  {i['where']}")
            print(f"      {i['detail']}")

    print(f"\n{len(issues)} broken reference(s).")
    print("A column that working SQL uses but the schema omits means the SCHEMA is "
          "stale — confirm with: python tools/introspect_facts.py --drift\n")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(issues, fh, indent=2)
        print(f"wrote {args.json}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())