#!/usr/bin/env python3
"""restructure_schema.py — rewrite schema_v3.yaml into the rich catalog format.

Reads the current ``schema_v3.yaml`` (a flat catalog: modules -> entities ->
tables -> columns with only name/type and a few hints) and rewrites it, adding
per-column semantic metadata the MCP catalog tools need:

  * ``role``      : timestamp | identifier | measure | dimension | metadata
  * ``canonical`` : a stable business name, only when confidently derivable

Per table it adds a ``time:`` block when a timestamp column exists. Per module
it adds ``coverage`` when the entity names indicate cloud-provider coverage
(aws/azure/gcp). Everything already present (alias, possible_values, cross_join,
primary_key, description, deeper_insights, nested ``fields``) is preserved.

Any place where a domain fact would have to be *guessed* (e.g. the exact
primary/secondary currency amount<->code mapping in tables that carry
``*_secondary`` currency columns) is flagged with an ``OPEN_QUESTION`` comment
rather than invented.

The original file is backed up to ``schema_v3.legacy.yaml`` (only on the first
run — an existing backup is never overwritten). The transform is idempotent:
re-running on an already-rich file reproduces the same output.

Usage:
    python tools/restructure_schema.py            # rewrite in place (+ backup)
    python tools/restructure_schema.py --check     # dry run, report only
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
SCHEMA_PATH = os.path.join(_ROOT, "schema_v3.yaml")
BACKUP_PATH = os.path.join(_ROOT, "schema_v3.legacy.yaml")

# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------
_NUMERIC_TYPES = re.compile(
    r"\b(numeric|double|float|bigint|integer|int|long|real|decimal|smallint)\b", re.I)
_TIMESTAMP_TYPES = re.compile(r"\b(timestamp|datetime|date)\b", re.I)
_MEASURE_WORDS = re.compile(
    r"(cost|amount|count|duration|seconds|hours|days|pct|percent|rate|quantity|"
    r"utilization|volume|spend|value|bytes|delta|yhat|mean|total|sum|avg|price|"
    r"score|number|num_)", re.I)

# Column name -> canonical business name. Conservative + unambiguous only.
_CANONICAL = {
    "cost_center": "cost_center",
    "account_name": "account_name",
    "subscription_name": "account_name",
    "account_id": "account_id",
    "billing_account_id": "account_id",
    "resource_id": "resource_id",
    "service_name": "service_name",
    "region_name": "region",
    "region_zone": "region",
    "region": "region",
    "__time": "usage_time",
    "usage_date": "usage_time",
    "service_area": "service_area",
}

_CLOUD_PROVIDERS = {"aws", "azure", "gcp"}


def infer_role(name: str, dtype: str) -> str:
    """Best-effort column role from its name and type."""
    n = (name or "").lower()
    t = (dtype or "").lower()

    # metadata — user tags / free-form bags.
    if n.startswith("tag_") or n.startswith("resource_tags_") or n in ("tags", "additional_info"):
        return "metadata"

    # timestamp — by type, or by a date/time-ish name (even if stored as string).
    if _TIMESTAMP_TYPES.search(t) or re.search(r"(_date_time|_datetime|_dtm|_date|_time)$", n) \
            or n in ("__time", "timestamp"):
        # A numeric *_seconds/_hours/_days column is a measure, not a timestamp.
        if not (_NUMERIC_TYPES.search(t) and re.search(r"(_seconds|_hours|_days|_minutes)$", n)):
            return "timestamp"

    # identifier — surrogate / natural keys.
    if re.search(r"(_system_id|_id)$", n) or n == "id":
        return "identifier"

    # measure — numeric facts.
    if _NUMERIC_TYPES.search(t):
        return "measure"
    if _MEASURE_WORDS.search(n) and t in ("", "number"):
        return "measure"

    # everything else is a dimension.
    return "dimension"


def infer_canonical(col: CommentedMap) -> str | None:
    """Canonical business name, preferring an authored alias, else the dict."""
    name = (col.get("name") or "").lower()
    alias = col.get("alias")
    if alias:
        return str(alias)
    return _CANONICAL.get(name)


def pick_time_field(columns: list) -> CommentedMap | None:
    """Choose the most usage-like timestamp column for a table's time block."""
    ts = [c for c in columns if c.get("role") == "timestamp"]
    if not ts:
        return None
    # Preference order for the canonical time field.
    def score(c):
        n = (c.get("name") or "").lower()
        if c.get("canonical") == "usage_time" or n == "__time":
            return 0
        if re.search(r"(open|usage|created|the)_date", n):
            return 1
        if re.search(r"date_time$", n):
            return 2
        return 5
    return sorted(ts, key=score)[0]


# --------------------------------------------------------------------------
# Column / table / module rewriters
# --------------------------------------------------------------------------
# Desired key order for a rich column.
_COL_ORDER = ["name", "type", "role", "canonical", "alias", "primary_key",
              "deeper_insights", "possible_values", "cross_join", "description", "fields"]


def rewrite_column(col: CommentedMap) -> CommentedMap:
    """Return a new column map with role/canonical added, keys ordered, all
    existing keys preserved. Recurses into nested ``fields``."""
    name = col.get("name")
    dtype = col.get("type", "")
    role = infer_role(name, dtype)

    new = CommentedMap()
    new["name"] = name
    new["type"] = dtype
    new["role"] = role
    canon = infer_canonical(col)
    if canon:
        new["canonical"] = canon
    # Preserve the rest in a stable order.
    for key in ("alias", "primary_key", "deeper_insights", "possible_values",
                "cross_join", "description"):
        if key in col:
            new[key] = col[key]
    # Nested sub-columns (json / nested opensearch types).
    if "fields" in col and isinstance(col["fields"], list):
        new["fields"] = [rewrite_column(f) for f in col["fields"]]
    # Carry any unexpected keys we did not explicitly handle.
    for key in col:
        if key not in new and key not in _COL_ORDER:
            new[key] = col[key]
    return new


_CURRENCY_SECONDARY = re.compile(r"(currency|cost|amount|price).*_secondary$", re.I)


def rewrite_table(tbl: CommentedMap) -> CommentedMap:
    cols_in = tbl.get("columns") or []
    cols = [rewrite_column(c) for c in cols_in]

    new = CommentedMap()
    new["name"] = tbl.get("name")
    if "description" in tbl:
        new["description"] = tbl["description"]

    time_field = pick_time_field(cols)
    if time_field is not None:
        tblock = CommentedMap()
        tblock["field"] = time_field.get("name")
        tblock["type"] = time_field.get("type")
        tblock["grain"] = "day"
        new["time"] = tblock

    new["columns"] = cols

    # Carry any other table-level keys (rare) verbatim.
    for key in tbl:
        if key not in new:
            new[key] = tbl[key]

    # Flag unresolved currency mapping rather than inventing a currencies block.
    if any(_CURRENCY_SECONDARY.search((c.get("name") or "")) for c in cols):
        new.yaml_set_comment_before_after_key(
            "columns", indent=12,
            before="OPEN_QUESTION: table has *_secondary currency/cost columns; "
                   "primary/secondary amount<->currency-code mapping not resolved.")
    return new


def rewrite_entity(ent: CommentedMap) -> CommentedMap:
    new = CommentedMap()
    new["name"] = ent.get("name")
    if "description" in ent:
        new["description"] = ent["description"]
    new["tables"] = [rewrite_table(t) for t in (ent.get("tables") or [])]
    for key in ent:
        if key not in new:
            new[key] = ent[key]
    return new


def rewrite_module(mod: CommentedMap) -> CommentedMap:
    entities = [rewrite_entity(e) for e in (mod.get("entities") or [])]

    new = CommentedMap()
    new["name"] = mod.get("name")
    for key in ("database_type", "code", "is_date_applicable", "description"):
        if key in mod:
            new[key] = mod[key]

    # coverage — derived (not invented) from cloud-provider entity names.
    providers = [e.get("name") for e in entities
                 if str(e.get("name", "")).lower() in _CLOUD_PROVIDERS]
    if providers:
        cov = CommentedMap()
        for p in providers:
            cov[str(p).lower()] = "present"
        new["coverage"] = cov

    new["entities"] = entities
    for key in mod:
        if key not in new:
            new[key] = mod[key]
    return new


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # do not wrap long descriptions
    y.indent(mapping=2, sequence=2, offset=0)
    return y


def transform(doc: CommentedMap) -> CommentedMap:
    modules = doc.get("modules") or []
    doc["modules"] = [rewrite_module(m) for m in modules]
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="dry run: report counts, do not write")
    ap.add_argument("--schema", default=SCHEMA_PATH, help="path to schema_v3.yaml")
    args = ap.parse_args()

    yaml = _yaml()
    with open(args.schema, "r", encoding="utf-8") as fh:
        doc = yaml.load(fh)

    n_mod = len(doc.get("modules") or [])
    n_ent = sum(len(m.get("entities") or []) for m in doc["modules"])
    n_tbl = sum(len(e.get("tables") or []) for m in doc["modules"] for e in (m.get("entities") or []))
    print(f"loaded: {n_mod} modules, {n_ent} entities, {n_tbl} tables")

    doc = transform(doc)

    # Tally roles for a sanity summary.
    roles: dict[str, int] = {}
    for m in doc["modules"]:
        for e in m.get("entities") or []:
            for t in e.get("tables") or []:
                for c in t.get("columns") or []:
                    roles[c.get("role", "?")] = roles.get(c.get("role", "?"), 0) + 1
    print("column roles:", dict(sorted(roles.items(), key=lambda kv: -kv[1])))

    if args.check:
        print("--check: no files written")
        return 0

    if not os.path.exists(BACKUP_PATH):
        # Back up the raw original bytes exactly once.
        with open(args.schema, "r", encoding="utf-8") as src:
            original = src.read()
        with open(BACKUP_PATH, "w", encoding="utf-8", newline="\n") as dst:
            dst.write(original)
        print(f"backup written: {BACKUP_PATH}")
    else:
        print(f"backup already exists, not overwriting: {BACKUP_PATH}")

    with open(args.schema, "w", encoding="utf-8", newline="\n") as fh:
        yaml.dump(doc, fh)
    print(f"rewrote: {args.schema}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
