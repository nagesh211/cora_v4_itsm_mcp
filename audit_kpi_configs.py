#!/usr/bin/env python3
"""audit_kpi_configs.py — benchmark every KPI config for the two bug classes
originally found in availability-mim-caused-by-change-count.json.

  BUG A (genuine remaining ambiguous-column risk): this mirrors
  ``cora_mcp.sql_alias`` EXACTLY (``_owner_alias`` / ``slot_scope``) rather than
  a naive "column name collides" check, because the live query engine already
  self-heals the common case at request time: ``qualify_filter_columns()``
  rewrites a config's bare ``fields.*.column`` to ``<alias>.<column>`` right
  before ``gen_query.build_sql`` runs, and it ALWAYS prefers the KPI's primary
  table when the primary table declares the column. So a field is only a
  REAL remaining risk when:
    1. the authored base_query has a ``{filters}`` (or similar) slot and >=2
       schema-qualified tables bound in FROM/JOIN (the only case
       ``slot_scope`` ever engages at all — a single-table query is never
       ambiguous), AND
    2. the field's column is bare (no "." — already-qualified columns are
       untouched), AND
    3. the KPI's OWN primary table does NOT declare that column (else the
       primary-table-wins rule silently fixes it — the common case), AND
    4. two or more of the OTHER joined tables declare that column, so
       ``_owner_alias`` can't prove a single owner and leaves it unqualified
       -> Postgres really does reject it as ambiguous.
  A column owned uniquely by the primary table, or by exactly one non-primary
  joined table, is NOT flagged — the runtime already qualifies it correctly.

  BUG B (no groupable dimension for a zero-dim request): a SQL-mode config
  declares filters.allowed (so it clearly has real, curated dimensions) but no
  allowed_group_by. This does NOT break an explicit ``dim=<field>`` request
  (``resolve_dim_word`` resolves a declared ``fields`` entry before ever
  consulting ``allowed_group_by``) — the gap is narrower: ``_effective_dim``
  in ``cora_mcp.query_engine`` only defaults a table-mode request with NO dim
  to a declared ``render.views`` "table" view's ``by``; it never falls back to
  schema. With no such view AND no ``allowed_group_by``, a zero-dim breakdown
  request ("share me the details") dead-ends on ``available: []`` instead of
  a retryable list of real dimensions.

Only checks SQL execution_mode configs (DSL-mode builds its own qualified
`a.<col>` reference via resolve_field, so it isn't exposed to BUG A the same
way, and its group-by story is different).

Usage:
    python audit_kpi_configs.py [config_dir ...]      # default: itsm_new itsm
    python audit_kpi_configs.py --json report.json    # also dump machine-readable report
"""
import argparse
import glob
import json
import os
import re

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(_HERE, "schema_v3.yaml")

# Identical to cora_mcp.sql_alias._BINDING_RE / alias_bindings: only
# schema-qualified `FROM/JOIN schema.table [AS] alias` bindings count — a bare
# `FROM (subquery) alias` is not a table binding sql_alias would ever qualify
# against either.
_TABLE_BINDING_RE = re.compile(
    r"\b(?:from|join)\s+([a-z_][\w]*\.[a-z_][\w]*)\s+(?:as\s+)?([a-z_][\w]*)",
    re.IGNORECASE)
_NOT_AN_ALIAS = {
    "on", "where", "group", "order", "having", "limit", "offset", "union",
    "inner", "left", "right", "full", "cross", "outer", "join", "select",
    "and", "or", "as", "using", "window", "fetch", "except", "intersect",
}


def load_schema_tables():
    """{table_fqn_lower: set(column_names_lower)} across all modules/entities."""
    with open(SCHEMA_FILE, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    tables = {}
    for m in doc.get("modules") or []:
        for e in m.get("entities") or []:
            for t in e.get("tables") or []:
                name = (t.get("name") or "").lower()
                cols = {(c.get("name") or "").lower() for c in t.get("columns") or []}
                tables[name] = cols
    return tables


def bound_tables(sql):
    """[(schema.table_lower, alias_lower), ...] for every schema-qualified
    FROM/JOIN in sql — identical matching to cora_mcp.sql_alias.alias_bindings."""
    out = []
    for m in _TABLE_BINDING_RE.finditer(sql or ""):
        table, alias = m.group(1).lower(), m.group(2)
        if alias.lower() in _NOT_AN_ALIAS:
            continue
        out.append((table, alias.lower()))
    return out


def _primary_fqn(cfg):
    """Mirrors cora_mcp.sql_alias._primary_fqn exactly."""
    pd = cfg.get("primary_dataset") or {}
    schema = pd.get("schema") or (cfg.get("source") or {}).get("schema")
    table = pd.get("table") or pd.get("name")
    if schema and table:
        return f"{schema}.{table}".lower()
    return None


def check_config(path, cfg, schema_tables):
    issues = []
    if (cfg.get("execution_mode") or "").upper() != "SQL":
        return issues

    name = cfg.get("name") or os.path.basename(path)
    sql = ((cfg.get("sql") or {}).get("base_query")) or ""
    fields = cfg.get("fields") or {}
    allowed = (cfg.get("filters") or {}).get("allowed") or []

    # --- BUG A: genuine remaining ambiguity, mirroring sql_alias._owner_alias -
    # slot_scope only ever engages when the query has a {filters} slot AND >=2
    # DISTINCT tables bound in scope; a single-table query is never ambiguous
    # and qualify_filter_columns is a no-op for it (sql_alias.slot_scope).
    all_binds = bound_tables(sql)
    distinct_tables = {t for t, _ in all_binds}
    has_slot = "{filters}" in sql
    if has_slot and len(distinct_tables) >= 2:
        primary_fqn = _primary_fqn(cfg)
        primary_cols = schema_tables.get(primary_fqn or "", set())
        other_tables = distinct_tables - {primary_fqn}
        for fname, meta in fields.items():
            col = (meta.get("column") or "").lower()
            if not col or "." in col:          # unqualified only -> already safe otherwise
                continue
            if col in primary_cols:
                continue                       # primary-table-wins rule: self-healed live
            owners = [t for t in other_tables if col in schema_tables.get(t, set())]
            if len(owners) >= 2:
                issues.append({
                    "config": name, "path": path, "bug": "A_ambiguous_column",
                    "field": fname, "column": col,
                    "allowed_filter": fname in allowed,
                    "primary_table": primary_fqn,
                    "owning_tables": owners,
                })

    # --- BUG B: filterable dims exist, but a zero-dim breakdown request has
    # nothing to fall back to (see module docstring — narrower than "SQL breaks").
    agb = cfg.get("allowed_group_by")
    has_agb = bool(agb)
    has_table_view = any(
        (v.get("type") == "table" and v.get("by"))
        for v in (cfg.get("render") or {}).get("views") or []
    )
    if allowed and not has_agb and not has_table_view:
        issues.append({
            "config": name, "path": path, "bug": "B_no_group_by",
            "allowed_filters": allowed,
            "group_by_mode": cfg.get("group_by_mode"),
        })

    return issues


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="*", default=["itsm_new", "itsm"],
                     help="config directories to scan (default: itsm_new itsm)")
    ap.add_argument("--json", dest="json_out", help="also write a JSON report to this path")
    args = ap.parse_args()

    schema_tables = load_schema_tables()

    all_issues = []
    scanned = 0
    per_dir_sql_count = {}
    for d in args.dirs:
        d_abs = os.path.join(_HERE, d) if not os.path.isabs(d) else d
        files = sorted(glob.glob(os.path.join(d_abs, "*.json")))
        n_sql = 0
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except Exception as exc:
                print("  skip %s: %s" % (f, exc))
                continue
            scanned += 1
            if (cfg.get("execution_mode") or "").upper() == "SQL":
                n_sql += 1
            all_issues.extend(check_config(os.path.relpath(f, _HERE), cfg, schema_tables))
        per_dir_sql_count[d] = n_sql

    bug_a = [i for i in all_issues if i["bug"] == "A_ambiguous_column"]
    bug_b = [i for i in all_issues if i["bug"] == "B_no_group_by"]
    configs_with_a = sorted({i["config"] for i in bug_a})
    configs_with_b = sorted({i["config"] for i in bug_b})
    configs_with_either = sorted(set(configs_with_a) | set(configs_with_b))

    print("=" * 78)
    print("KPI CONFIG AUDIT")
    print("=" * 78)
    print("scanned %d config file(s) across %s (SQL-mode: %s)"
          % (scanned, args.dirs, per_dir_sql_count))
    print()
    print("BUG A — ambiguous unqualified column (join-column-name collision): "
          "%d config(s), %d field occurrence(s)" % (len(configs_with_a), len(bug_a)))
    for name in configs_with_a:
        occ = [i for i in bug_a if i["config"] == name]
        fields_hit = ", ".join(sorted({o["field"] for o in occ}))
        flagged_allowed = any(o["allowed_filter"] for o in occ)
        marker = " [reachable via allowed filter]" if flagged_allowed else " [field unused by filters.allowed]"
        print("  - %-55s fields: %s%s" % (name, fields_hit, marker))
    print()
    print("BUG B — no allowed_group_by despite having filterable dimensions: "
          "%d config(s)" % len(configs_with_b))
    for name in configs_with_b:
        print("  - %s" % name)
    print()
    print("TOTAL distinct configs with >=1 issue: %d" % len(configs_with_either))

    if args.json_out:
        report = {
            "scanned": scanned,
            "sql_mode_counts": per_dir_sql_count,
            "bug_a_ambiguous_column": bug_a,
            "bug_b_no_group_by": bug_b,
            "configs_with_bug_a": configs_with_a,
            "configs_with_bug_b": configs_with_b,
            "configs_with_either": configs_with_either,
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print("\nwrote JSON report -> %s" % args.json_out)


if __name__ == "__main__":
    main()