#!/usr/bin/env python3
"""gen_query.py — standalone query generator.

Everything the engine does to turn a KPI config + a request (dates, filters,
dimensions, grain) into SQL, collapsed into ONE runnable script. No database,
no framework import, no infra. Run it and it prints the exact SQL + bind params
the backend would send to Postgres, plus an inlined preview you can paste into a
SQL client.

It faithfully mirrors backend/framework/core/builder/{driver,resolve,dsl,
dialect,build}.py and util/dates.py — if you change those, re-sync here.

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
  # DSL-mode scalar (the KPI stat value)
  python gen_query.py emergency

  # pick the window
  python gen_query.py emergency --from 2026-01-01 --to 2026-06-29

  # dynamic user filters (repeat --filter; comma = multiple values = IN/ANY)
  python gen_query.py emergency --filter sector=Retail --filter region=EMEA,APAC

  # time series (line/bar) at a chosen grain
  python gen_query.py emergency --mode series --grain month

  # grouped table on a chosen dimension
  python gen_query.py emergency --mode table --dim region

  # show BOTH comparison windows (CYTD + PYTD) like serve_kpi does
  python gen_query.py emergency --comparison

  # SQL-mode config (raw authored base_query with {from_date}/{to_date})
  python gen_query.py cm-major-incident

  # a config by explicit path, and snapshot (as-of) KPIs
  python gen_query.py ../config/em-critical-alerts-active.json --as-of 2026-06-29

  # list everything available
  python gen_query.py --list
"""
import argparse
import glob
import json
import os
import re
import sys

# ============================================================================
# 0. CONFIG LOCATION
# ============================================================================
_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.normpath(os.path.join(_HERE, "config"))

# POC default window (data present through mid-2026) — matches the services.
DEF_FROM, DEF_TO = "2026-01-01", "2026-06-29"


def load_config(name_or_path):
    """Accept a bare KPI name ('emergency'), a filename, or a full path."""
    cand = name_or_path
    if not os.path.isfile(cand):
        if not cand.endswith(".json"):
            cand = cand + ".json"
        if not os.path.isfile(cand):
            cand = os.path.join(CONFIG_DIR, os.path.basename(cand))
    if not os.path.isfile(cand):
        sys.exit("config not found: %r (looked in %s)" % (name_or_path, CONFIG_DIR))
    with open(cand, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ============================================================================
# 1. resolve.py  — field resolution, joins, operator table
# ============================================================================
JOIN_ALIAS = "bcdefghijklmnopqrstuvwxyz"
P = "\x01"   # param-placeholder sentinel; swapped to %s after % escaping


def get_fields(config):
    return config.get("fields", {})


def resolve_field(field, aliases, fields, base):
    """logical field -> "{alias}.{column}". Raise if unknown / unresolved."""
    meta = fields.get(field)
    if not meta:
        raise KeyError("field '%s' not defined in fields" % field)
    ds = meta["dataset"]
    datasets = ds if isinstance(ds, list) else [ds]
    col = meta["column"]
    # Already alias-qualified (e.g. "b.business_name" for a custom from_raw whose
    # base table isn't aliased "a") -> use verbatim; re-prepending the base alias
    # would produce a broken "a.b.business_name". Configs qualify a column only
    # when they mean that exact alias.
    if "." in col:
        return col
    if base in datasets:
        return "a.%s" % col
    for d in datasets:
        if d in aliases:
            return "%s.%s" % (aliases[d], col)
    raise ValueError("field '%s' dataset %s not in FROM (base=%s, joins=%s)"
                     % (field, datasets, base, list(aliases)))


def get_filter_type(field, fields):
    return (fields.get(field) or {}).get("filter_type") or "in"


def build_joins(config, aliases, base):
    """Structured joins -> ["{type} JOIN schema.table alias ON ...", ...]."""
    out = []
    for j in config.get("joins", []) or []:
        name = j["name"]
        alias = JOIN_ALIAS[len(aliases) - 1]   # base already 'a'
        aliases[name] = alias
        left = aliases.get(j.get("join_from", base), "a")
        on = " AND ".join("%s.%s = %s.%s" % (left, c["left"], alias, c["right"])
                          for c in j["on"])
        for ec in j.get("on_conditions", []) or []:   # authored constants inline
            op = ec["operator"]
            if op in ("=", "!="):
                on += " AND %s.%s %s '%s'" % (alias, ec["column"], op, ec["value"])
            elif op in ("in", "not_in"):
                vals = ", ".join("'%s'" % v for v in ec["values"])
                on += " AND %s.%s %s (%s)" % (alias, ec["column"],
                                              "IN" if op == "in" else "NOT IN", vals)
        out.append("%s JOIN %s.%s %s ON %s"
                   % (j.get("type", "LEFT"), j["schema"], j["table"], alias, on))
    return out


def _all_str(values):
    """True if every value is a (non-empty) string — the signal that a filter is
    over a TEXT column and can be compared case-insensitively via lower(). Numeric
    values return False so lower() is never applied to a number."""
    try:
        vals = list(values)
    except TypeError:
        return False
    return bool(vals) and all(isinstance(v, str) for v in vals)


# --- column-type resolution (so lower() is only applied to TEXT columns) --------
# ``lower(col)`` is only valid on text. Applying it to a boolean/numeric/date
# column raises "function lower(<type>) does not exist" at prepare time. We read
# the column's declared type from schema_v3.yaml so equality/IN filters lower()
# ONLY text columns; non-text columns compare raw (the DB layer coerces the
# string param to the real Python type via the prepared statement).
_TEXT_SQL_TYPES = ("char", "text", "varchar", "keyword", "citext", "name")
_SCHEMA_TYPES = None   # lazy {table_fqn: {column: sql_type}}


def _is_text_sql_type(t):
    return bool(t) and any(k in str(t).lower() for k in _TEXT_SQL_TYPES)


def _is_array_sql_type(t):
    """True for a schema-declared array type, e.g. ``"text []"``/``"text[]"``."""
    return bool(t) and re.search(r"\[\s*\]", str(t)) is not None


def _load_schema_types():
    """Index every column's declared SQL type from schema_v3.yaml, keyed by the
    table's fully-qualified name (e.g. 'itsm_incident.tbl_all_incidents'). Best
    effort: any failure (missing file, no PyYAML) yields an empty map and the
    builder falls back to a crash-safe ``lower(col::text)``."""
    global _SCHEMA_TYPES
    if _SCHEMA_TYPES is not None:
        return _SCHEMA_TYPES
    _SCHEMA_TYPES = {}
    try:
        import yaml
        with open(os.path.join(_HERE, "schema_v3.yaml"), "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for m in doc.get("modules") or []:
            for e in m.get("entities") or []:
                for t in e.get("tables") or []:
                    cols = _SCHEMA_TYPES.setdefault(t.get("name"), {})
                    for c in t.get("columns") or []:
                        cols.setdefault(c.get("name"), c.get("type"))
    except Exception:
        pass
    return _SCHEMA_TYPES


def _column_type(config, field, fields):
    """Declared SQL type for a logical ``field`` of ``config`` (or None). Honours a
    ``type`` already on the field meta; otherwise resolves column -> table -> type
    from schema_v3.yaml. Alias-qualified columns (containing '.') and joined-table
    columns are left as None -> crash-safe ``lower(col::text)`` fallback."""
    meta = (fields or {}).get(field) or {}
    if meta.get("type"):
        return meta["type"]
    col = meta.get("column")
    if not col or "." in col:
        return None
    ds = meta.get("dataset")
    if isinstance(ds, list):
        ds = ds[0] if ds else None
    if not ds:
        return None
    types = _load_schema_types()
    schema = (config.get("source") or {}).get("schema")
    for key in ([f"{schema}.{ds}"] if schema else []) + [ds]:
        t = (types.get(key) or {}).get(col)
        if t:
            return t
    suffix = "." + ds                                   # any schema.<table> match
    for fqn, cols in types.items():
        if (fqn == ds or (fqn or "").endswith(suffix)) and col in cols:
            return cols[col]
    return None


def _ci_lhs(col_expr, col_type, value):
    """Left-hand side + value-folding flag for a case-insensitive comparison.

    Returns ``(lhs_sql, fold_values)``:
      * known TEXT column      -> ("lower(col)", True)
      * known non-text column  -> ("col", False)          # compare raw; DB coerces
      * unknown type + string  -> ("lower(col::text)", True)   # crash-safe
      * unknown type + number  -> ("col", False)
    """
    if col_type is not None:
        if _is_text_sql_type(col_type):
            return "lower(%s)" % col_expr, True
        return col_expr, False
    vals = value if isinstance(value, (list, tuple)) else [value]
    if _all_str(vals):
        return "lower(%s::text)" % col_expr, True
    return col_expr, False


def condition(col_expr, operator, value=None, filter_type="in", tz=None,
              from_cast="", to_cast="", is_user_value=True, col_type=None):
    """(sql_fragment, params) for one condition.

    is_user_value=True -> value(s) become %s bind params (payload/filter_by).
    col_type is the column's declared SQL type (from schema): it decides whether a
    case-insensitive comparison may use ``lower()`` — text only. Non-text columns
    (boolean/numeric/date) compare raw so ``lower(boolean)`` never reaches Postgres.
    """
    op = operator
    if op == "raw":                       # authored verbatim boolean SQL
        return "(" + value + ")", []
    if op == "not_null":
        return col_expr + " IS NOT NULL", []
    if op == "between":
        frm, to = value["from"], value["to"]
        if tz:
            return ("(%s AT TIME ZONE 'GMT' AT TIME ZONE %s) BETWEEN %s%s AND %s%s"
                    % (col_expr, P, P, from_cast, P, to_cast), [tz, frm, to])
        return ("%s BETWEEN %s%s AND %s%s" % (col_expr, P, from_cast, P, to_cast),
                [frm, to])
    if op == "in":
        # Text values compare case-insensitively (user may type 'finance' vs stored
        # 'FINANCE'); non-text columns compare raw (lower() of a bool/number/date is
        # invalid — the DB layer coerces the string param to the real type).
        if filter_type == "array":                 # scalar col, membership in a set
            lhs, fold = _ci_lhs(col_expr, col_type, value)
            vals = [str(v).lower() for v in value] if fold else list(value)
            return "%s = ANY(%s)" % (lhs, P), [vals]
        if filter_type == "array_val":             # col is text[]: element-wise overlap
            if _all_str(value):
                return ("EXISTS (SELECT 1 FROM unnest(%s) AS _e WHERE lower(_e::text) = ANY(%s))"
                        % (col_expr, P), [[str(v).lower() for v in value]])
            return "%s && %s" % (col_expr, P), [list(value)]
        lhs, fold = _ci_lhs(col_expr, col_type, value)
        vals = tuple(str(v).lower() for v in value) if fold else tuple(value)
        return "%s IN %s" % (lhs, P), [vals]
    if op == "not_in":
        lhs, fold = _ci_lhs(col_expr, col_type, value)
        vals = tuple(str(v).lower() for v in value) if fold else tuple(value)
        return "%s NOT IN %s" % (lhs, P), [vals]
    # scalar comparators = != < > <= >=
    if is_user_value:
        if op in ("=", "!="):
            lhs, fold = _ci_lhs(col_expr, col_type, value)
            if fold:
                return "%s %s %s" % (lhs, op, P), [str(value).lower()]
            return "%s %s %s" % (col_expr, op, P), [value]
        return "%s %s %s" % (col_expr, op, P), [value]
    return "%s %s '%s'" % (col_expr, op, value), []


# ============================================================================
# 2. driver.py  — SQL-mode date-window + filter substitution
# ============================================================================
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")


def iso_guard(v):
    if v is None or not _ISO.match(str(v)):
        raise ValueError("driver: non-ISO datetime refused: %r" % v)
    return str(v)


def substitute_dates(text, payload):
    for ph in ("from_date", "to_date", "as_of"):
        if payload.get(ph) is not None:
            text = text.replace("{%s}" % ph, iso_guard(payload[ph]))
    return text


def _inline_filter_fragment(col_expr, values, filter_type, col_type=None):
    """One user filter -> a SQL boolean fragment with INLINE (escaped) literals.

    SQL-mode configs carry no bind params (dates are inlined too), so filters
    are inlined the same way via ``_lit`` (which doubles single quotes). Honours
    the field's ``filter_type`` exactly like the DSL path's ``condition``:
    ``array_val`` -> ``&&``, ``array`` -> ``= ANY``, everything else -> ``IN``.
    ``col_type`` decides case-folding: only text columns get ``lower()`` (a
    non-text column compares raw so Postgres coerces the untyped literal itself).
    """
    vals = list(values)
    if filter_type == "array_val":
        if _all_str(vals):
            low = ", ".join(_lit(str(v).lower()) for v in vals)
            return ("EXISTS (SELECT 1 FROM unnest(%s) AS _e WHERE lower(_e::text) = ANY(ARRAY[%s]))"
                    % (col_expr, low))
        return "%s && ARRAY[%s]" % (col_expr, ", ".join(_lit(v) for v in vals))
    lhs, fold = _ci_lhs(col_expr, col_type, vals)
    lit = (lambda v: _lit(str(v).lower())) if fold else (lambda v: _lit(v))
    if filter_type == "array":
        return "%s = ANY(ARRAY[%s])" % (lhs, ", ".join(lit(v) for v in vals))
    if len(vals) == 1:
        return "%s = %s" % (lhs, lit(vals[0]))
    return "%s IN (%s)" % (lhs, ", ".join(lit(v) for v in vals))


def compile_sql_filters(config, filter_by):
    """Compile user ``filter_by`` into an inline SQL fragment for a SQL-mode KPI.

    Returns a string that begins with ``" AND ..."`` (so it can be dropped into a
    ``{filters}`` slot that follows an existing WHERE condition), or ``""`` when
    there are no filters. Each logical filter is mapped to its real column via the
    config's ``fields`` block; the column string is used verbatim (already alias-
    qualified where the authored query needs it). Raises ``ValueError`` for a
    filter that is not allowed / not defined.
    """
    if not filter_by:
        return ""
    fields = get_fields(config)
    allowed = ((config.get("filters") or {}).get("allowed") or [])
    frags = []
    for key, values in filter_by.items():
        if key == "granularity":                       # internal, not a real filter
            continue
        if allowed and key not in allowed:
            raise ValueError("filter %r not allowed for %s; allowed: %s"
                             % (key, config.get("name"), allowed))
        meta = fields.get(key)
        if not meta:
            raise ValueError("filter %r not defined in fields for %s"
                             % (key, config.get("name")))
        vals = values if isinstance(values, (list, tuple)) else [values]
        frag = _inline_filter_fragment(meta["column"], vals,
                                       meta.get("filter_type") or "in",
                                       col_type=_column_type(config, key, fields))
        frags.append("(%s)" % frag)
    return (" AND " + " AND ".join(frags)) if frags else ""


def sql_allowed_group_by(config):
    """The set of dimension names an SQL-mode KPI's authored query may be grouped
    by, or ``None`` when the config declares no ``allowed_group_by`` at all (the
    author never vetted this query for grouping, so ``group_by_dim`` stays
    rejected). Mirrors ``cora_mcp.opensearch_client.config_dimensions``'s reading
    of ``allowed_group_by``: entries carrying a ``granularity`` are time-grain
    fields for series mode, not breakdown dimensions, so they're skipped here."""
    agb = config.get("allowed_group_by")
    if not agb:
        return None
    out = set()
    for item in agb:
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict) and item.get("field") and not item.get("granularity"):
            out.add(item["field"])
    return out or None


def _sql_primary_table_columns(config):
    """Real columns of a SQL-mode KPI's PRIMARY table -> declared SQL type, read
    from the same ``schema_v3.yaml`` index ``_column_type`` uses. ``{}`` when the
    primary table isn't declared or isn't in the schema catalog."""
    pd = config.get("primary_dataset") or {}
    schema, table = pd.get("schema"), pd.get("table")
    if not table:
        return {}
    types = _load_schema_types()
    for key in ([f"{schema}.{table}"] if schema else []) + [table]:
        cols = types.get(key)
        if cols:
            return cols
    return {}


# Words that can textually follow a `schema.table` reference without being its
# alias (mirrors cora_mcp.sql_alias's list — kept independent since this module
# has no cora_mcp import).
_NOT_AN_ALIAS = {
    "on", "where", "group", "order", "having", "limit", "offset", "union",
    "inner", "left", "right", "full", "cross", "outer", "join", "select",
    "and", "or", "as", "using", "window", "fetch", "except", "intersect",
}
_TABLE_BINDING_RE = re.compile(
    r"\b(?:from|join)\s+([a-z_][\w]*)\.([a-z_][\w]*)\s+(?:as\s+)?([a-z_][\w]*)",
    re.IGNORECASE)


def _sql_outer_scope_tables(sql):
    """``{(schema, table): alias}`` for every table bound directly in the OUTER
    (paren-depth-0) scope of ``sql`` — i.e. NOT nested inside a parenthesized
    derived table/subquery. A plain text scan (no real SQL parser), tracking
    paren depth up to each match's start; good enough to tell "one flat
    FROM/JOIN chain" apart from "wrapped in a subquery" — which is exactly
    what decides whether a column from that table is visible where we're
    about to inject a dimension into the top-level SELECT/GROUP BY."""
    out = {}
    for m in _TABLE_BINDING_RE.finditer(sql):
        alias = m.group(3)
        if alias.lower() in _NOT_AN_ALIAS:
            continue
        depth = sql.count("(", 0, m.start()) - sql.count(")", 0, m.start())
        if depth == 0:
            out[(m.group(1).lower(), m.group(2).lower())] = alias
    return out


def _sql_primary_table_in_outer_scope(config, sql):
    """Whether the KPI's ``primary_dataset`` table is directly reachable (not
    buried inside a derived subquery) in ``sql``'s outer scope. A query shaped
    like ``SELECT ... FROM (SELECT ... FROM real.table a ...) x`` binds ``a``
    one level down — the top-level SELECT can only see what ``x`` re-exports,
    so injecting a raw dimension column there would reach Postgres as
    "missing FROM-clause entry", not a KPI bug but a structural mismatch this
    generic rewrite must refuse rather than ship."""
    pd = config.get("primary_dataset") or {}
    table = (pd.get("table") or "").lower()
    if not table:
        return False
    schema = (pd.get("schema") or "").lower()
    outer = _sql_outer_scope_tables(sql)
    if (schema, table) in outer:
        return True
    return any(t == table for (_, t) in outer)


# ============================================================================
# 2b. SQL-mode dimension PUSHDOWN for wrapped/nested queries (sqlglot AST)
# ============================================================================
# ``_sql_inject_group_by`` above only works when the primary table sits
# directly in the query's outer scope. A large class of authored KPIs instead
# wrap it one or more levels down — a single CTE/subquery the outer SELECT
# just aggregates further (e.g. ``SELECT SUM(x) FROM (SELECT ... FROM
# real.table a ...) d``), or a per-record aggregate CTE re-aggregated by an
# outer SUM. For that SINGLE-BRANCH shape (never a ratio combining two
# independently-aggregated branches — that's a different, harder class left
# alone here) we can still push the dimension all the way down to the real
# table and thread it back up through every wrapping level, using sqlglot's
# AST instead of text splicing so each addition lands in the right scope.
def _cte_map(tree):
    with_ = tree.args.get("with_")
    if not with_:
        return {}
    return {c.alias_or_name.lower(): c.this for c in with_.expressions}


def _own_table_alias(select_node, schema, table):
    """If ``select_node``'s OWN from/joins directly reference (schema, table)
    (real joins for row enrichment are fine here), return the alias used for
    it. Else None — the table isn't bound at this level."""
    frm = select_node.args.get("from_")
    joins = select_node.args.get("joins") or []
    sources = ([frm.this] if frm else []) + [j.this for j in joins]
    for s in sources:
        if isinstance(s, exp.Table) and s.name.lower() == table.lower():
            s_schema = (s.db or "").lower()
            if not schema or not s_schema or s_schema == schema.lower():
                return s.alias_or_name
    return None


def _branch_source(select_node, ctes):
    """The single nested branch `select_node` derives from — a Subquery's
    inner select, or a named CTE's definition — or ``(None, None)`` if its
    FROM is a plain real table (chain ends), or its join(s) can't be proven
    to be plain ENRICHMENT (extra columns from a related table, harmless to
    the dimension chain) rather than a second independently-aggregated
    branch. A join is treated as enrichment only when: this level has no
    GROUP BY / aggregate of its own (an aggregating level combined via a
    join is exactly the two-branch-ratio/keyed shape — a different,
    separately-handled class, not enrichment), AND every joined source is a
    bare real table (a join to a Subquery/CTE is itself a second branch we
    can't tell apart from "the" dimension source, so we refuse rather than
    guess). When that holds, the join is ignored and the chain continues
    through ``FROM``'s own source."""
    frm = select_node.args.get("from_")
    joins = select_node.args.get("joins") or []
    if frm is None:
        return None, None
    if joins:
        if select_node.args.get("group") or _has_own_aggregate(select_node):
            return None, None
        if any(not isinstance(j.this, exp.Table) for j in joins):
            return None, None
    src = frm.this
    if isinstance(src, exp.Subquery):
        return src.this, src.alias_or_name
    if isinstance(src, exp.Table) and src.name.lower() in ctes:
        return ctes[src.name.lower()], src.alias_or_name
    return None, None


def _has_own_aggregate(select_node):
    return any(e.find(exp.AggFunc) for e in select_node.expressions)


def _needs_group_by(select_node):
    """True if this level is itself an aggregation point — already has a
    GROUP BY, or one of its own expressions calls an aggregate function —
    meaning the injected dimension must join ITS GROUP BY too, else its
    aggregate silently collapses across the dimension instead of respecting
    it. A single branch can have more than one such level (e.g. a per-record
    aggregate CTE re-aggregated by an outer SUM)."""
    return bool(select_node.args.get("group")) or _has_own_aggregate(select_node)


def _add_to_group_by(select_node, grp_names):
    existing = select_node.args.get("group")
    cols = [exp.column(g) for g in grp_names]
    if existing:
        for c in cols:
            existing.append("expressions", c)
    else:
        select_node.set("group", exp.Group(expressions=cols))


def _sql_dimension_chain(root, ctes, schema, table):
    """``([(select, child_alias_or_None), ...], base_alias)`` outer -> inner,
    starting from ``root`` (the top-level query, or one branch of a ratio —
    see ``_sql_pushdown_ratio_group_by``) and ending at the SELECT whose own
    FROM/JOIN directly binds (schema, table); ``(None, None)`` if that table
    isn't reachable via a single linear chain of subqueries/CTEs from
    ``root`` (a nested branching/ratio query, or the table genuinely isn't
    referenced there at all). ``ctes`` is the whole query's CTE map (shared
    across branches — CTEs are defined once at the top)."""
    chain = []
    node = root
    while True:
        alias = _own_table_alias(node, schema, table)
        if alias is not None:
            chain.append((node, None))
            return chain, alias
        child, child_alias = _branch_source(node, ctes)
        if child is None:
            return None, None
        chain.append((node, child_alias))
        node = child


def _apply_dimension_chain(chain, base_alias, cols, grp_names, is_array=None):
    """Mutate every SELECT in ``chain`` (outer -> inner, as returned by
    ``_sql_dimension_chain``) in place: project the raw dimension column(s)
    at the innermost (table-owning) level, thread ``grp``/``grp2``/… back up
    through every wrapping level, and add them to the GROUP BY of every level
    that is itself an aggregation point (there can be more than one).

    Each ``col`` is either bare (qualify with the primary table's own
    ``base_alias``) or already ``<alias>.<column>`` — the latter is how
    ``sql_alias.qualify_filter_columns`` marks a dimension whose column
    actually lives on a DIFFERENT table the query joins in (e.g. a "sector"
    dimension backed by a joined lookup table, not the primary table this
    chain was walked to find). Re-qualifying that case with ``base_alias``
    would silently point at the wrong table and Postgres would reject it as
    an undefined column, so an already-qualified column is used verbatim.

    ``is_array`` (parallel to ``cols``/``grp_names``, all ``False`` if
    omitted) marks a schema-declared array column (e.g. ``business_name``
    text []) — projected via ``CROSS JOIN LATERAL unnest(...)`` at this same
    innermost level instead of selected raw, so it groups per ELEMENT (one row
    per sector) rather than per distinct combination of elements."""
    is_array = is_array if is_array is not None else [False] * len(cols)
    s_table_idx = len(chain) - 1
    s_table_sel, _ = chain[s_table_idx]
    for col, grp, arr in zip(cols, grp_names, is_array):
        col_expr = col if "." in col else "%s.%s" % (base_alias, col)
        if arr:
            u = "u_%s" % grp
            s_table_sel.append("joins", _sql_lateral_unnest_join(col_expr, u, grp))
            col_expr = "%s.%s" % (u, grp)
        expr = sqlglot.parse_one("SELECT %s AS %s" % (col_expr, grp),
                                 read="postgres").expressions[0]
        s_table_sel.append("expressions", expr)
    if _needs_group_by(s_table_sel):
        _add_to_group_by(s_table_sel, grp_names)

    for i in range(s_table_idx - 1, -1, -1):
        sel, child_alias = chain[i]
        for grp in grp_names:
            expr = sqlglot.parse_one("SELECT %s.%s AS %s" % (child_alias, grp, grp),
                                     read="postgres").expressions[0]
            sel.append("expressions", expr)
        if _needs_group_by(sel):
            _add_to_group_by(sel, grp_names)


def _thread_up(wrap, grp_names):
    """Project + (if needed) group by ``grp_names`` at every wrapping level in
    ``wrap`` (``[(select, child_alias), ...]`` outer -> inner, as accumulated
    while descending), innermost first. Same threading ``_apply_dimension_chain``
    does for its own chain, factored out for reuse by the union-of-arms and
    two-branch-at-any-level pushdowns below, which need to thread a dimension
    up through wrapping levels that sit ABOVE the shape that actually resolved
    it."""
    for i in range(len(wrap) - 1, -1, -1):
        sel, child_alias = wrap[i]
        for grp in grp_names:
            expr = sqlglot.parse_one("SELECT %s.%s AS %s" % (child_alias, grp, grp),
                                     read="postgres").expressions[0]
            sel.append("expressions", expr)
        if _needs_group_by(sel):
            _add_to_group_by(sel, grp_names)


def _union_arms(node):
    """Flatten a (possibly nested) ``exp.Union`` tree into its leaf SELECTs,
    left to right (a 3+-way UNION parses as nested binary Union nodes)."""
    if isinstance(node, exp.Union):
        return _union_arms(node.this) + _union_arms(node.expression)
    return [node]


def _sql_dimension_chain_to_union(root, ctes, schema, table):
    """Like ``_sql_dimension_chain``, but for a wrapping chain that bottoms out
    at a UNION of arms (e.g. three UNIONed SELECTs each independently
    aggregated, then re-aggregated one or more times above) instead of
    directly at the table. Returns ``(wrap_chain, union_node, arm_chains)`` —
    ``wrap_chain`` is ``[(select, child_alias), ...]`` outer -> the level whose
    FROM is the union; ``arm_chains`` is ``[(chain, base_alias), ...]``, one
    per union arm, each resolved via ``_sql_dimension_chain`` independently.
    ``(None, None, None)`` if this shape doesn't apply: the table is reached
    directly (not this shape — that's the plain linear chain), or the chain
    breaks before ever reaching a union, or ANY arm of a union it does reach
    can't itself resolve the table (refuse rather than guess which arms to
    breakdown and which to leave alone)."""
    chain = []
    node = root
    while True:
        if _own_table_alias(node, schema, table) is not None:
            return None, None, None
        child, child_alias = _branch_source(node, ctes)
        if child is None:
            return None, None, None
        chain.append((node, child_alias))
        if isinstance(child, exp.Union):
            arm_chains = []
            for arm in _union_arms(child):
                c, a = _sql_dimension_chain(arm, ctes, schema, table)
                if c is None:
                    return None, None, None
                arm_chains.append((c, a))
            return chain, child, arm_chains
        node = child


def _sql_dimension_chain_own_table(root, ctes):
    """Like ``_sql_dimension_chain``, but for a branch that may be built from a
    DIFFERENT real table than the KPI's declared ``primary_dataset`` — e.g. a
    ratio combining a major-incident count from one table with an SLA
    duration from a completely different one. Walks the same single-source
    wrapper chain, but stops at whichever real table is directly bound at
    that level rather than requiring a specific (schema, table) match.
    Returns ``(chain, base_alias, schema, table)``, or ``(None, None, None,
    None)`` if no real table is reachable via a single linear chain from
    ``root``. Callers must still verify any dimension column actually exists
    on the table this returns — a shared column name isn't guaranteed."""
    chain = []
    node = root
    while True:
        frm = node.args.get("from_")
        joins = node.args.get("joins") or []
        sources = ([frm.this] if frm else []) + [j.this for j in joins]
        found = next((s for s in sources if isinstance(s, exp.Table)), None)
        if found is not None:
            chain.append((node, None))
            return chain, found.alias_or_name, (found.db or None), found.name
        child, child_alias = _branch_source(node, ctes)
        if child is None or isinstance(child, exp.Union):
            return None, None, None, None
        chain.append((node, child_alias))
        node = child


def _column_on_table(schema, table, col):
    """Whether ``col`` is a real column of (schema, table), per the same
    ``schema_v3.yaml`` index ``_column_type``/``_sql_primary_table_columns``
    read from. ``False`` (not an exception) for an unknown table — callers
    treat that as "can't prove it's safe", the correct conservative default."""
    if not table:
        return False
    types = _load_schema_types()
    for key in ([f"{schema}.{table}"] if schema else []) + [table]:
        cols = types.get(key)
        if cols and col in cols:
            return True
    return False


def _sql_two_branch_ratio(select_node, ctes):
    """``((branch1_select, alias1), (branch2_select, alias2), join_node)`` if
    ``select_node``'s FROM+JOIN is EXACTLY two independently-derived branches
    (a Subquery or named CTE each) combined with NO existing join predicate
    and NO pre-existing GROUP BY on either side — the "ratio of two bare
    totals" shape (e.g. ``NUMERATOR N CROSS JOIN DENOMINATOR D``, or a comma
    join of two aggregate subqueries). ``None`` for anything else: more than
    two sources, an existing ON/USING (already keyed on something — usually a
    time bucket or a hand-picked single dimension, a different and more
    delicate case this doesn't attempt), or a branch that's already grouped
    (same reason)."""
    frm = select_node.args.get("from_")
    joins = select_node.args.get("joins") or []
    if not frm or len(joins) != 1 or joins[0].args.get("on") or joins[0].args.get("using"):
        return None

    def resolve(node):
        if isinstance(node, exp.Subquery):
            return node.this, node.alias_or_name
        if isinstance(node, exp.Table) and node.name.lower() in ctes:
            return ctes[node.name.lower()], node.alias_or_name
        return None, None

    sel1, alias1 = resolve(frm.this)
    sel2, alias2 = resolve(joins[0].this)
    if sel1 is None or sel2 is None:
        return None
    if sel1.args.get("group") or sel2.args.get("group"):
        return None
    return (sel1, alias1), (sel2, alias2), joins[0]


def _pushdown_two_branch(select_node, two, ctes, config, dim_list, fields, schema, table,
                         grp_names, cols, keyed, is_array=None):
    """Shared rewrite for BOTH two-branch shapes (``_sql_two_branch_ratio`` and
    ``_sql_two_branch_keyed`` — ``keyed`` picks which). Pushes the dimension
    into EACH branch independently, then combines them on it with a FULL
    OUTER join (never INNER — a dimension value present on only one side,
    e.g. a sector with numerator activity but zero denominator activity,
    must never be silently dropped) and exposes ``COALESCE(branch1.grp,
    branch2.grp) AS grp`` on ``select_node`` itself (which may be nested
    under further wrapping levels the caller threads the dimension through
    separately — see ``_thread_up``).

    Each branch is resolved against the KPI's declared ``primary_dataset``
    table first (the common case); if that table isn't reachable there at
    all — e.g. a ratio genuinely built across two DIFFERENT physical tables —
    falls back to whatever real table that branch's own chain actually
    binds (``_sql_dimension_chain_own_table``), and only proceeds if the
    requested dimension's column verifiably exists on THAT table too
    (``_column_on_table``) — never assumed, always checked against the
    schema catalog. Raises ``ValueError`` if the dimension isn't reachable
    (safely) in one of the two branches."""
    (sel1, alias1), (sel2, alias2), join = two
    for sel, alias in ((sel1, alias1), (sel2, alias2)):
        chain, base_alias = _sql_dimension_chain(sel, ctes, schema, table)
        if chain is None:
            # The KPI's declared primary table isn't THIS branch's table (a
            # ratio genuinely built across two different physical tables) —
            # only here, not on the common primary-anchored path above, do we
            # need to independently prove the dimension's column is real on
            # whichever table this branch actually uses (a shared column name
            # isn't guaranteed the way it is for the primary table, which the
            # KPI author already vetted by declaring the field at all).
            chain, base_alias, branch_schema, branch_table = \
                _sql_dimension_chain_own_table(sel, ctes)
            if chain is not None:
                # ``cols`` may already be ``<alias>.<column>`` (see
                # ``_apply_dimension_chain``) — the schema catalog indexes
                # bare column names, so strip any qualifier before checking.
                missing = [c for c in cols
                           if not _column_on_table(branch_schema, branch_table,
                                                    c.rsplit(".", 1)[-1])]
                if missing:
                    raise ValueError(
                        "table/dimension mode is not supported for SQL-mode KPI %r: "
                        "column(s) %r don't exist on branch %r's own table %s.%s — "
                        "can't prove the dimension means the same thing on both "
                        "sides of this %s query." % (config.get("name"), missing, alias,
                                                     branch_schema, branch_table,
                                                     "comparison" if keyed else "ratio"))
        if chain is None:
            raise ValueError(
                "table/dimension mode is not supported for SQL-mode KPI %r: "
                "its primary table %r isn't reachable in branch %r of this "
                "%s query. Use its drilldown breakdown or a DSL KPI instead."
                % (config.get("name"), table, alias,
                   "comparison" if keyed else "ratio"))
        _apply_dimension_chain(chain, base_alias, cols, grp_names, is_array)

    if keyed:
        on_expr = join.args.get("on")
        for g in grp_names:
            cond = exp.EQ(this=exp.column(g, table=alias1), expression=exp.column(g, table=alias2))
            on_expr = exp.And(this=on_expr, expression=cond)
        join.set("on", on_expr)
    else:
        on_expr = None
        for g in grp_names:
            cond = exp.EQ(this=exp.column(g, table=alias1), expression=exp.column(g, table=alias2))
            on_expr = cond if on_expr is None else exp.And(this=on_expr, expression=cond)
        join.set("on", on_expr)
        join.set("kind", "OUTER")
        join.set("side", "FULL")

    coalesced = []
    for g in grp_names:
        expr = sqlglot.parse_one("SELECT COALESCE(%s.%s, %s.%s) AS %s"
                                 % (alias1, g, alias2, g, g), read="postgres").expressions[0]
        select_node.append("expressions", expr)
        coalesced.append(expr.this.copy())    # the bare COALESCE(...) call, no alias
    # select_node may itself aggregate further over the two branches' per-
    # dimension rows (e.g. AVG of a per-branch ratio) — same as any other
    # level in a single-branch chain, that needs the dimension in ITS GROUP
    # BY too, else it collapses right back across the dimension it just got.
    # Group by the COALESCE expression itself, not the bare `grp` alias: with
    # both branches' `grp` columns in scope from the FULL OUTER JOIN, a bare
    # `GROUP BY grp` is ambiguous between the output alias and either side's
    # input column.
    if _needs_group_by(select_node):
        existing = select_node.args.get("group")
        if existing:
            for c in coalesced:
                existing.append("expressions", c)
        else:
            select_node.set("group", exp.Group(expressions=coalesced))


def _sql_two_branch_keyed(select_node, ctes):
    """Like ``_sql_two_branch_ratio``, but for the OTHER common two-branch
    comparison shape: each branch is ALREADY an independently aggregated
    ``SELECT ... GROUP BY`` (typically by a shared time grain, e.g. month),
    and the two are combined with a JOIN whose ON-clause is a plain
    AND-of-equalities pinning that shared grain (e.g. a "created vs closed by
    month" ``FULL OUTER JOIN ON created.month = closed.month``). This is the
    shape ``_sql_two_branch_ratio`` deliberately declines (it requires NO
    existing join predicate and NO pre-existing GROUP BY) — a widget already
    keyed on a grain still has a real, generically-addable dimension pushdown
    available, just a different rewrite: widen the grain instead of
    inventing one.

    Returns ``(sel1, alias1), (sel2, alias2), join_node`` or ``None`` if:
    more than two sources, no existing ON, either branch ISN'T already
    grouped (that's ``_sql_two_branch_ratio``'s shape instead), or the ON is
    anything other than a plain conjunction of ``branch1.col = branch2.col``
    equalities (a real relational join condition is out of scope for a
    generic pushdown — refuse rather than guess)."""
    frm = select_node.args.get("from_")
    joins = select_node.args.get("joins") or []
    if not frm or len(joins) != 1:
        return None
    on = joins[0].args.get("on")
    if on is None or joins[0].args.get("using"):
        return None

    def resolve(node):
        if isinstance(node, exp.Subquery):
            return node.this, node.alias_or_name
        if isinstance(node, exp.Table) and node.name.lower() in ctes:
            return ctes[node.name.lower()], node.alias_or_name
        return None, None

    sel1, alias1 = resolve(frm.this)
    sel2, alias2 = resolve(joins[0].this)
    if sel1 is None or sel2 is None:
        return None
    if not (sel1.args.get("group") and sel2.args.get("group")):
        return None

    def _is_cross_alias_equality(node):
        if not isinstance(node, exp.EQ):
            return False
        l, r = node.this, node.expression
        if not (isinstance(l, exp.Column) and isinstance(r, exp.Column)):
            return False
        pair = {(l.table or "").lower(), (r.table or "").lower()}
        return pair == {alias1.lower(), alias2.lower()}

    def _all_equalities(node):
        if isinstance(node, exp.And):
            return _all_equalities(node.this) and _all_equalities(node.expression)
        return _is_cross_alias_equality(node)

    if not _all_equalities(on):
        return None
    return (sel1, alias1), (sel2, alias2), joins[0]


def _sql_two_scalar_subquery_ratio(select_node):
    """If ``select_node`` has NO ``FROM``/``JOIN`` at all, and its single
    output expression contains EXACTLY two scalar (uncorrelated) subqueries
    combined arithmetically — e.g. ``round(100.0 * (SELECT count(...) FROM
    ...) / nullif((SELECT count(...) FROM ...), 0), 2)`` — returns
    ``(subq1, subq2)`` in textual (left-to-right) order. ``None`` for
    anything else: a FROM/JOIN present (that's ``_sql_two_branch_ratio``'s
    shape, a different rewrite), more/fewer than one output column, or
    anything other than exactly two subqueries in that column's expression."""
    if select_node.args.get("from_") is not None or select_node.args.get("joins"):
        return None
    if len(select_node.expressions) != 1:
        return None
    expr0 = select_node.expressions[0]
    # Only the OUTERMOST subqueries in this expression -- pruning descent at
    # each one found, so a subquery's own internal subqueries (e.g. an inner
    # dedup ``(SELECT id FROM t GROUP BY 1)``) aren't also counted.
    subqs = [n for n in expr0.walk(prune=lambda n: isinstance(n, exp.Subquery) and n is not expr0)
             if isinstance(n, exp.Subquery)]
    if len(subqs) != 2:
        return None
    return tuple(subqs)


def _sql_pushdown_scalar_subquery_ratio(select_node, ctes, config, dim_list, fields,
                                        schema, table, grp_names, cols, is_array=None):
    """Handle the "twin scalar subqueries, no FROM at all" shape (see
    ``_sql_two_scalar_subquery_ratio``) — e.g. ``SELECT round(100.0 *
    (SELECT count(...) FROM t a WHERE ...) / nullif((SELECT count(...) FROM t
    a WHERE ...), 0), 2) AS value``, with NOTHING in a FROM clause combining
    the two numbers at all. Pushes the dimension into EACH scalar subquery
    independently (reusing the single-branch chain pushdown, so each ends up
    grouped by it), wraps each as a derived table, combines them with a FULL
    OUTER JOIN on the dimension (a value present on only one side is never
    silently dropped — same reasoning as the FROM+JOIN ratio class), and
    rewrites the ORIGINAL arithmetic expression in place to reference each
    branch's own renamed measure column instead of the now-removed inline
    subquery — so whatever formula the author wrote (a plain ratio, a
    zero-guarded denominator, a percentage, …) survives untouched. Mutates
    ``select_node`` in place. Raises ``ValueError`` if the dimension isn't
    reachable in one of the two subqueries."""
    two = _sql_two_scalar_subquery_ratio(select_node)
    subq1, subq2 = two
    branches = []
    for i, subq in enumerate((subq1, subq2), start=1):
        inner = subq.this
        chain, base_alias = _sql_dimension_chain(inner, ctes, schema, table)
        if chain is None:
            raise ValueError(
                "table/dimension mode is not supported for SQL-mode KPI %r: "
                "its primary table %r isn't reachable in scalar subquery %d "
                "of this ratio. Use its drilldown breakdown or a DSL KPI "
                "instead." % (config.get("name"), table, i))
        _apply_dimension_chain(chain, base_alias, cols, grp_names, is_array)
        measure_alias = "m%d" % i
        meas = inner.expressions[0]
        renamed = (meas.this if isinstance(meas, exp.Alias) else meas).as_(measure_alias)
        inner.set("expressions", [renamed] + inner.expressions[1:])
        branches.append((subq, "br%d" % i, measure_alias))

    (subq1, a1, m1), (subq2, a2, m2) = branches
    d1 = exp.Subquery(this=subq1.this, alias=exp.TableAlias(this=exp.to_identifier(a1)))
    d2 = exp.Subquery(this=subq2.this, alias=exp.TableAlias(this=exp.to_identifier(a2)))
    on_expr = None
    for g in grp_names:
        cond = exp.EQ(this=exp.column(g, table=a1), expression=exp.column(g, table=a2))
        on_expr = cond if on_expr is None else exp.And(this=on_expr, expression=cond)
    join = exp.Join(this=d2, on=on_expr, kind="OUTER", side="FULL")

    orig_expr = select_node.expressions[0]
    subq1.replace(exp.column(m1, table=a1))
    subq2.replace(exp.column(m2, table=a2))
    coalesced = [sqlglot.parse_one("SELECT COALESCE(%s.%s, %s.%s) AS %s"
                                   % (a1, g, a2, g, g), read="postgres").expressions[0]
                 for g in grp_names]
    select_node.set("expressions", coalesced + [orig_expr])
    select_node.set("from_", exp.From(this=d1))
    select_node.set("joins", [join])


def _sql_pushdown_group_by(sql, config, dim_list, fields):
    """Push ``dim_list`` down to the KPI's primary table, trying — at ``tree``
    and then at every wrapping level descended through — each shape this
    module knows how to rewrite, in order:

      1. a plain linear chain straight to the primary table
         (``_sql_dimension_chain``/``_apply_dimension_chain``);
      2. a UNION of arms each independently reaching the table
         (``_sql_dimension_chain_to_union``);
      3. two branches already GROUP BY'd and joined on a shared grain key,
         e.g. a "created vs closed by month" widget (``_sql_two_branch_keyed``);
      4. two bare, independently-aggregated totals combined with no existing
         join key (``_sql_two_branch_ratio``);
      5. (``tree`` only) twin scalar subqueries with no FROM/JOIN combining
         them at all (``_sql_two_scalar_subquery_ratio``).

    Whichever shape resolves the dimension, it's then threaded back up
    through every wrapping level ABOVE where it was found (``_thread_up``).
    Descent between levels only continues through a single, unambiguous
    source (see ``_branch_source``) — anything else means none of the known
    shapes apply, which raises ``ValueError``."""
    if sqlglot is None:
        raise ValueError("sqlglot is not installed; cannot push a dimension "
                         "through a wrapped SQL-mode KPI's nested query")
    pd = config.get("primary_dataset") or {}
    schema, table = pd.get("schema"), pd.get("table")
    if not table:
        raise ValueError("SQL-mode KPI %r has no primary_dataset.table to "
                         "anchor a dimension pushdown on" % config.get("name"))
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception as exc:
        raise ValueError("could not parse SQL-mode KPI %r's query for a "
                         "dimension pushdown: %s" % (config.get("name"), exc)) from exc
    ctes = _cte_map(tree)
    grp_names = ["grp" if i == 0 else "grp%d" % (i + 1) for i in range(len(dim_list))]
    # Kept as declared (bare, or already ``<alias>.<column>`` — see
    # ``_apply_dimension_chain``), NOT stripped to a bare name: a dimension
    # qualified by ``sql_alias.qualify_filter_columns`` against a joined
    # (non-primary) table must keep that alias, or the pushdown below would
    # re-qualify it against the primary table's own alias instead.
    cols = [fields[d]["column"] for d in dim_list]
    # A dim whose schema-declared type is an array (e.g. business_name text [])
    # is unnested at the table level instead of grouped on raw -- same
    # reasoning as the flat-query path (_sql_inject_group_by/_sql_widen_group_by).
    is_array = [_is_array_sql_type(_column_type(config, d, fields)) for d in dim_list]

    if _sql_two_scalar_subquery_ratio(tree) is not None:
        _sql_pushdown_scalar_subquery_ratio(tree, ctes, config, dim_list, fields,
                                            schema, table, grp_names, cols, is_array)
        return tree.sql(dialect="postgres")

    wrap = []
    node = tree
    while True:
        chain, base_alias = _sql_dimension_chain(node, ctes, schema, table)
        if chain is not None:
            _apply_dimension_chain(chain, base_alias, cols, grp_names, is_array)
            _thread_up(wrap, grp_names)
            return tree.sql(dialect="postgres")

        wchain, union_node, arm_chains = _sql_dimension_chain_to_union(node, ctes, schema, table)
        if wchain is not None:
            for arm_chain, arm_base_alias in arm_chains:
                _apply_dimension_chain(arm_chain, arm_base_alias, cols, grp_names, is_array)
            _thread_up(wchain, grp_names)
            _thread_up(wrap, grp_names)
            return tree.sql(dialect="postgres")

        two_k = _sql_two_branch_keyed(node, ctes)
        if two_k is not None:
            _pushdown_two_branch(node, two_k, ctes, config, dim_list, fields, schema, table,
                                 grp_names, cols, keyed=True, is_array=is_array)
            _thread_up(wrap, grp_names)
            return tree.sql(dialect="postgres")

        two_r = _sql_two_branch_ratio(node, ctes)
        if two_r is not None:
            _pushdown_two_branch(node, two_r, ctes, config, dim_list, fields, schema, table,
                                 grp_names, cols, keyed=False, is_array=is_array)
            _thread_up(wrap, grp_names)
            return tree.sql(dialect="postgres")

        child, child_alias = _branch_source(node, ctes)
        if child is None or isinstance(child, exp.Union):
            raise ValueError(
                "table/dimension mode is not supported for SQL-mode KPI %r: "
                "none of the known query shapes (single chain, union of "
                "arms, two-branch ratio/keyed, twin scalar subqueries) "
                "match. Use its drilldown breakdown or a DSL KPI instead."
                % config.get("name"))
        wrap.append((node, child_alias))
        node = child


def _sql_lateral_unnest_join(col_expr, unnest_alias, out_col):
    """A ``CROSS JOIN LATERAL unnest(col_expr) AS unnest_alias(out_col)`` join
    node, for splicing an array dimension's explode into a sqlglot tree via
    ``tree.append("joins", ...)``."""
    stub = sqlglot.parse_one(
        "SELECT 1 FROM _t CROSS JOIN LATERAL unnest(%s) AS %s(%s)"
        % (col_expr, unnest_alias, out_col), read="postgres")
    return stub.args["joins"][0]


def _sql_insert_after_from(sql, insertion):
    """Insert ``insertion`` right after the query's FROM/JOIN chain — i.e.
    right before its outer-scope WHERE/GROUP BY/ORDER BY/LIMIT — or at the very
    end if none of those appear at paren depth 0. Used to splice a ``CROSS JOIN
    LATERAL unnest(...)`` in after text-level (non-sqlglot) rewrites."""
    positions = []
    for kw in (r"\bwhere\b", r"\bgroup\s+by\b", r"\border\s+by\b", r"\blimit\b"):
        for m in re.finditer(kw, sql, re.IGNORECASE):
            depth = sql.count("(", 0, m.start()) - sql.count(")", 0, m.start())
            if depth == 0:
                positions.append(m.start())
                break
    if positions:
        pos = min(positions)
        return sql[:pos] + insertion + " " + sql[pos:]
    return sql + " " + insertion


def _sql_widen_group_by(sql, config, dim_list, fields):
    """Widen an authored query's OWN top-level GROUP BY to ALSO break down by
    ``dim_list``, instead of refusing outright just because it's already
    grouped (e.g. a "top 10 by assignment group" ranking, or a monthly trend
    already grouped by period). The requested dimension(s) are appended as
    NEW ``grp``/``grp2``/… columns at the END of the existing SELECT list —
    never inserted before an existing column — so any ordinal reference an
    existing ``GROUP BY``/``ORDER BY`` makes (``GROUP BY 1``, ``ORDER BY 2``)
    keeps pointing at exactly what it already pointed at. The new column(s)'
    own ordinal position(s) are then appended to the GROUP BY (Postgres
    freely mixes ordinal and named/expression items in one GROUP BY list, so
    this is safe regardless of how the existing clause was authored).

    A pre-grouped "top N" ranking widened this way becomes a top-N ranking of
    the (existing dimension, new dimension) combination, still ordered by the
    same measure — that is the intended, requested behaviour, not a defect.

    Only handles the case ``_sql_inject_group_by`` already confirmed: the
    primary table is bound directly in the query's own top-level FROM/JOIN
    (single flat SELECT, not a nested/ratio shape — those are routed to
    ``_sql_pushdown_group_by`` before this is ever called).
    """
    if sqlglot is None:
        raise ValueError("sqlglot is not installed; cannot widen an already-"
                         "grouped SQL-mode KPI's query for an extra dimension")
    pd = config.get("primary_dataset") or {}
    schema, table = pd.get("schema"), pd.get("table")
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception as exc:
        raise ValueError("could not parse SQL-mode KPI %r's query to widen its "
                         "GROUP BY: %s" % (config.get("name"), exc)) from exc
    base_alias = _own_table_alias(tree, schema, table)
    if base_alias is None:
        raise ValueError(
            "SQL-mode KPI %r: primary table %r is not directly bound in its "
            "own top-level FROM/JOIN; cannot widen its GROUP BY."
            % (config.get("name"), table))
    existing_group = tree.args.get("group")
    if existing_group is None:
        raise ValueError(
            "SQL-mode KPI %r has no existing GROUP BY to widen." % config.get("name"))

    start_idx = len(tree.expressions)
    for i, d in enumerate(dim_list, start=1):
        col = fields[d]["column"]
        col_expr = col if "." in col else "%s.%s" % (base_alias, col)
        alias = "grp" if i == 1 else "grp%d" % i
        # An array-typed dimension (e.g. business_name text []) is exploded via
        # a LATERAL unnest join, one element per row, instead of grouping on the
        # raw array — the latter buckets by distinct COMBINATIONS of elements
        # (e.g. "AMESA, CGF") rather than by each sector on its own.
        if _is_array_sql_type(_column_type(config, d, fields)):
            u = "u%d" % i
            tree.append("joins", _sql_lateral_unnest_join(col_expr, u, alias))
            col_expr = "%s.%s" % (u, alias)
        expr = sqlglot.parse_one("SELECT %s AS %s" % (col_expr, alias),
                                 read="postgres").expressions[0]
        tree.append("expressions", expr)
        existing_group.append("expressions", exp.Literal.number(start_idx + i))
    return tree.sql(dialect="postgres")


def _sql_inject_group_by(sql, config, dim_list, fields):
    """Rewrite an authored aggregate ``SELECT ... FROM ... WHERE ...`` so it also
    groups by ``dim_list`` — one ``grp``/``grp2``/… column per requested
    dimension, added right after ``SELECT`` and via ``GROUP BY`` at the end.

    This reuses whatever measure expression the author wrote (AVG, ratio,
    COUNT, …) instead of requiring a second hand-authored breakdown query, so
    "by sector" always answers with the SAME metric as the ungrouped stat.
    If the query already carries its own top-level ``GROUP BY`` (a
    hand-authored grouped/ranking query), widens that existing GROUP BY
    instead of refusing — see ``_sql_widen_group_by``. Raises if the query has
    no leading ``SELECT`` to anchor on.
    ``fields`` is the (possibly schema-augmented — see ``driver_substitute``)
    fields map to read each dimension's physical column from.

    When the primary table isn't directly reachable at the outer scope (see
    ``_sql_primary_table_in_outer_scope``), falls back to
    ``_sql_pushdown_group_by`` — a sqlglot-based rewrite that pushes the
    dimension down to wherever the table actually lives and threads it back
    up through the wrapping CTEs/subqueries. That fallback itself raises for
    the harder ratio-of-two-branches shape it doesn't yet handle.
    """
    if not _sql_primary_table_in_outer_scope(config, sql):
        return _sql_pushdown_group_by(sql, config, dim_list, fields)
    if re.search(r"(?is)\bgroup\s+by\b", sql):
        return _sql_widen_group_by(sql, config, dim_list, fields)
    m = re.match(r"(?is)^\s*select\s+", sql)
    if not m:
        raise ValueError(
            "SQL-mode KPI %r base_query doesn't start with SELECT; cannot inject "
            "a dimension GROUP BY." % config.get("name"))
    sel_cols, group_idx, lateral_joins = [], [], []
    for i, d in enumerate(dim_list, start=1):
        col_expr = fields[d]["column"]          # already alias-qualified if needed
        alias = "grp" if i == 1 else "grp%d" % i
        # Same array handling as _sql_widen_group_by: unnest instead of grouping
        # on the raw array.
        if _is_array_sql_type(_column_type(config, d, fields)):
            u = "u%d" % i
            lateral_joins.append(
                "CROSS JOIN LATERAL unnest(%s) AS %s(%s)" % (col_expr, u, alias))
            col_expr = "%s.%s" % (u, alias)
        sel_cols.append("%s AS %s" % (col_expr, alias))
        group_idx.append(str(i))
    sql = sql[:m.end()] + ", ".join(sel_cols) + ", " + sql[m.end():]
    if lateral_joins:
        sql = _sql_insert_after_from(sql, " ".join(lateral_joins))
    return sql + " GROUP BY " + ", ".join(group_idx)


def driver_substitute(config, payload):
    """SQL-mode: substitute the window (and user filters) into the authored
    ``config.sql.base_query`` -> ``(sql, [])``.

    * Dates fill ``{from_date}`` / ``{to_date}`` / ``{as_of}`` as before.
    * User filters fill a ``{filters}`` placeholder (author-positioned in the
      right scope). If filters were requested but the query has no ``{filters}``
      slot, we RAISE rather than silently drop them — a silently-unfiltered
      number is worse than a clear error.
    * ``table``/dimension mode: a ``group_by_dim`` request is honoured by
      rewriting the authored query to also SELECT/GROUP BY the requested
      dimension(s) — see ``_sql_inject_group_by``. Groupability is decided by:
        1. an explicit ``allowed_group_by`` — a curated allowlist the author
           vetted; a dim IN it is always honoured.
        2. SCHEMA FALLBACK (checked whether or not ``allowed_group_by`` was
           declared): a dim outside the curated list -- or when none was
           declared at all -- is still groupable if its physical column is a
           real column on the KPI's primary table (``schema_v3.yaml``), the
           same fallback already used for filters and series/table dims
           elsewhere. This ADDS dims past the curated list; it never drops
           one that IS curated. A dim that's neither curated nor a real
           schema column is rejected -- use the KPI's drilldown breakdown or
           a DSL KPI instead.
    """
    payload = payload or {}
    dims = payload.get("group_by_dim")
    dim_list = (list(dims) if isinstance(dims, (list, tuple)) else [dims]) if dims else []
    fields = dict(get_fields(config))       # local copy: schema fallback may add entries
    if dim_list:
        allowed = sql_allowed_group_by(config)
        schema_cols = _sql_primary_table_columns(config)
        pd_name = (config.get("primary_dataset") or {}).get("table") \
            or (config.get("primary_dataset") or {}).get("name")
        for d in dim_list:
            if allowed is not None and d in allowed:
                if d not in fields:
                    raise ValueError(
                        "dimension %r not defined in fields for %s" % (d, config.get("name")))
                continue
            # Not in the curated allowlist (or none was declared) -> fall back to a
            # real schema column. `d` may already be a declared field (use its
            # physical column) or a bare column name the config never declared.
            col = (fields.get(d) or {}).get("column", d)
            bare = col.rsplit(".", 1)[-1]        # strip any authored alias qualifier
            if not schema_cols or bare not in schema_cols:
                raise ValueError(
                    "dimension %r is not groupable for SQL-mode KPI %r: not in its "
                    "curated allowed_group_by (%s) and not a real column on its "
                    "primary table %r. Use its drilldown breakdown or a DSL KPI."
                    % (d, config.get("name"), sorted(allowed) if allowed else [], pd_name))
            if d not in fields:                  # synthesize so injection can use it
                fields[d] = {"dataset": pd_name, "column": bare}
    bq = config["sql"]["base_query"]

    # Build an effective date payload so every window placeholder the base_query
    # needs is filled — even when the resolved window's shape doesn't match the
    # query's placeholders 1:1. A snapshot query (only `{as_of}`) handed a from/to
    # range anchors on the window end; a ranged query handed only an as_of uses it
    # for both bounds. Without this, a stale/mismatched window left a literal
    # `{as_of}` in the SQL and Postgres raised "invalid input syntax for timestamp".
    dates = {k: payload.get(k) for k in ("from_date", "to_date", "as_of")}
    anchor = dates["as_of"] or dates["to_date"] or dates["from_date"]
    if anchor is not None:
        if "{as_of}" in bq and not dates["as_of"]:
            dates["as_of"] = anchor
        if "{to_date}" in bq and not dates["to_date"]:
            dates["to_date"] = anchor
        if "{from_date}" in bq and not dates["from_date"]:
            dates["from_date"] = dates["as_of"] or anchor
    sql = substitute_dates(bq, dates)

    frag = compile_sql_filters(config, payload.get("filter_by") or {})
    if "{filters}" in sql:
        sql = sql.replace("{filters}", frag)
    elif frag:
        raise ValueError(
            "SQL-mode KPI %r cannot apply filters %s: its base_query has no "
            "{filters} placeholder yet." % (
                config.get("name"),
                [k for k in (payload.get("filter_by") or {}) if k != "granularity"]))

    # Never let an unfilled window placeholder reach the database as a literal.
    leftover = [ph for ph in ("{from_date}", "{to_date}", "{as_of}") if ph in sql]
    if leftover:
        raise ValueError(
            "date window not resolved for SQL-mode KPI %r: unfilled %s "
            "(no date supplied for the requested window)."
            % (config.get("name"), leftover))
    if dim_list:
        sql = _sql_inject_group_by(sql, config, dim_list, fields)
    return sql, []


# ============================================================================
# 3. dsl.py  — structured DSL -> (sql, params)
# ============================================================================
_GRAIN = {"daily": "day", "weekly": "week", "monthly": "month",
          "quarterly": "quarter"}


def _measure_expr(expr, fields):
    for fn in fields:                                  # resolve known {field}
        expr = expr.replace("{%s}" % fn, "a." + fields[fn]["column"])
    return re.sub(r"\{([a-z_][a-z0-9_]*)\}", r"a.\1", expr)   # fallback {col}->a.col


def dsl_build(config, payload=None):
    payload = payload or {}
    dsl = config["dsl"]
    fields = get_fields(config)
    pd = config["primary_dataset"]
    base = pd["name"]
    aliases = {base: "a"}
    joins_sql = [] if dsl.get("from_raw") else build_joins(config, aliases, base)

    def col(field):
        return resolve_field(field, aliases, fields, base)

    sel, group, where, params = [], [], [], []
    _unnest_n = [0]

    def project_dim(field, out_alias):
        """(select_expr, group_expr) for one dimension. A field whose schema-
        declared type is an array (e.g. ``business_name`` / text []) is exploded
        via ``CROSS JOIN LATERAL unnest(...)`` so each element groups on its own
        row, instead of grouping on the raw array (which produced one bucket per
        distinct COMBINATION of elements, e.g. "AMESA, CGF"). Non-array columns
        are projected directly, unchanged."""
        c = col(field)
        if _is_array_sql_type(_column_type(config, field, fields)):
            u = "u%d" % _unnest_n[0]
            _unnest_n[0] += 1
            joins_sql.append("CROSS JOIN LATERAL unnest(%s) AS %s(%s)" % (c, u, out_alias))
            ref = "%s.%s" % (u, out_alias)
            return "%s AS %s" % (ref, out_alias), ref
        return "%s AS %s" % (c, out_alias), c

    # ── SELECT ───────────────────────────────────────────────────────────
    dims = payload.get("group_by_dim")
    if dims:
        # accept a single field (str) or several (list) — one GROUP BY column each.
        # first dim keeps the historical `grp` alias; extras get grp2, grp3, …
        dim_list = list(dims) if isinstance(dims, (list, tuple)) else [dims]
        for i, dname in enumerate(dim_list):
            alias = "grp" if i == 0 else "grp%d" % (i + 1)
            s, g = project_dim(dname, alias)
            sel.append(s); group.append(g)
    for gf in dsl.get("group_by_fields", []) or []:
        s, g = project_dim(gf["field"], gf["alias"])
        sel.append(s); group.append(g)
    for d in dsl.get("dimensions", []) or []:
        s, g = project_dim(d["field"], d["alias"])
        sel.append(s); group.append(g)
    tg = payload.get("time_group") or dsl.get("time_group")   # payload = series mode
    if tg:
        grain = (payload.get("filter_by") or {}).get("granularity") or tg["grain"]
        e = "date_trunc('%s', %s)" % (_GRAIN.get(grain, grain), col(tg["field"]))
        sel.append("%s AS %s" % (e, tg.get("alias", "bucket")))
        group.append(e)
    for m in dsl["measures"]:
        sel.append("%s AS %s" % (_measure_expr(m["expression"], fields), m["alias"]))

    # ── WHERE ──────────────────────────────────────────────────────────────
    if dsl.get("where_raw"):
        where.append("(%s)" % substitute_dates(dsl["where_raw"], payload))
    for sf in dsl.get("static_filters", []) or []:
        if sf["operator"] == "raw":
            f, p = condition(None, "raw", sf["expr"]); where.append(f)
        else:
            f, p = condition(col(sf["field"]), sf["operator"], sf.get("value"),
                             get_filter_type(sf["field"], fields), is_user_value=True,
                             col_type=_column_type(config, sf["field"], fields))
            where.append(f); params += p
    for flt in dsl.get("filters", []) or []:          # payload-driven date window
        if flt["operator"] == "between":
            left = flt.get("left_expr") or col(flt["field"])
            val = {"from": payload[flt["payload_from"]], "to": payload[flt["payload_to"]]}
            f, p = condition(left, "between", val, tz=payload.get("timezone"),
                             from_cast=flt.get("from_cast", ""),
                             to_cast=flt.get("to_cast", ""))
            where.append(f); params += p
    for k, v in (payload.get("filter_by") or {}).items():   # user filter surface
        if k == "granularity":
            continue
        vals = v if isinstance(v, (list, tuple)) else [v]
        f, p = condition(col(k), "in", vals, get_filter_type(k, fields),
                         col_type=_column_type(config, k, fields))
        where.append(f); params += p

    # ── FROM / GROUP BY / ORDER BY / LIMIT ──────────────────────────────────
    frm = dsl.get("from_raw") or ("%s.%s a" % (pd["schema"], pd["table"]))
    sql = "SELECT %s FROM %s" % (", ".join(sel), frm)
    if joins_sql:
        sql += " " + " ".join(joins_sql)
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += " GROUP BY " + ", ".join(group)
    ob = dsl.get("order_by") or []
    if ob:
        malias = {m["alias"] for m in dsl["measures"]}
        parts = []
        for o in ob:
            f = o["field"]; d = o.get("direction", "ASC")
            parts.append("%s %s" % (f if f in malias else col(f), d))
        sql += " ORDER BY " + ", ".join(parts)
    if dsl.get("limit"):
        sql += " LIMIT %d" % int(dsl["limit"])

    # ── POST-AGGREGATION ────────────────────────────────────────────────────
    pa = dsl.get("post_aggregations")
    if pa:
        outer = ", ".join("%s AS %s" % (p["expression"], p["alias"]) for p in pa)
        sql = "SELECT %s FROM (%s) agg" % (outer, sql)
    # escape authored literal % iff we pass params, then restore sentinel -> %s
    if params:
        sql = sql.replace("%", "%%")
    sql = sql.replace(P, "%s")
    return sql, params


# ============================================================================
# 4. dialect.py  — config-driven transpilation (no-op for postgres)
# ============================================================================
try:
    import sqlglot
    from sqlglot import exp
except ImportError:
    sqlglot = None
    exp = None
AUTHOR_DIALECT = "postgres"


def to_dialect(sql, dialect):
    if not dialect or dialect == AUTHOR_DIALECT or sqlglot is None:
        return sql
    try:
        return sqlglot.transpile(sql, read=AUTHOR_DIALECT, write=dialect)[0]
    except Exception:
        return sql


# ============================================================================
# 5. build.py  — the single entry point
# ============================================================================
def build_sql(config, payload=None):
    if config.get("execution_mode") == "DSL":
        sql, params = dsl_build(config, payload or {})
    else:
        sql, params = driver_substitute(config, payload or {})
    dialect = (config.get("source") or {}).get("dialect", "postgres")
    return to_dialect(sql, dialect), params


# ============================================================================
# 6. dates.py  — comparison-window resolution (CYTD / PYTD)
# ============================================================================
def _start(d):
    return d + " 00:00:00"


def _end(d):
    return d + " 23:59:59"


def _yr_start(d):
    return d[:4] + "-01-01"


def _shift_years(d, n):
    return str(int(d[:4]) - n) + d[4:]


def _is_snapshot(config):
    sig = config.get("signal", {})
    return (sig.get("snapshot_mode") == "asof"
            or sig.get("archetype") == "snapshot")


def resolve_comparison(config, from_date, to_date):
    comp = config.get("comparison") or {}
    snap = _is_snapshot(config)
    enabled = comp.get("enabled", True)
    basis = comp.get("basis") or ("none" if (not enabled or snap) else "ytd")
    cur_label = comp.get("current_label") or "CYTD"
    prev_label = comp.get("previous_label") or "PYTD"
    anchor = to_date if comp.get("anchor", "to") == "to" else from_date
    off = comp.get("previous_offset") or {"years": 1}
    yrs = off.get("years", 1)

    if snap:
        cur = (None, _end(anchor))
    elif basis == "prior_period":
        cur = (_start(from_date), _end(to_date))
    else:
        cur = (_start(_yr_start(anchor)), _end(anchor))

    prev = None
    if basis == "ytd":
        pa = _shift_years(anchor, yrs)
        prev = (_start(_yr_start(pa)), _end(pa))
    elif basis == "prior_period":
        prev = (_start(_shift_years(from_date, yrs)),
                _end(_shift_years(to_date, yrs)))

    return {"basis": basis, "enabled": prev is not None,
            "cur_label": cur_label, "prev_label": prev_label,
            "as_of": anchor, "cur": cur, "prev": prev}


# ============================================================================
# 7. PAYLOAD ASSEMBLY  — mirrors the service layer (kpi_service / series_service)
# ============================================================================
def _find_view(config, kind):
    for v in (config.get("render") or {}).get("views") or []:
        if v.get("type") == kind:
            return v
    return None


def build_payload(config, mode, window, filter_by, dim, grain):
    """Assemble the payload for a given request, exactly like the services do.

    window = (from_ts|None, to_ts). from_ts None -> snapshot as-of read.
    """
    cf, ct = window
    payload = {"filter_by": filter_by or {}}

    if cf is None:                                   # snapshot (as-of)
        payload["as_of"] = ct
    else:
        payload["from_date"], payload["to_date"] = cf, ct

    if mode == "series":
        view = _find_view(config, "line") or _find_view(config, "bar") or {}
        tg = view.get("time_group") or {}
        field = tg.get("field") or (config.get("time") or {}).get("column")
        payload["time_group"] = {"field": field, "grain": grain or tg.get("grain")
                                 or "week", "alias": "bucket"}
        # A per-dimension trend groups by the dimension AND the time bucket, so each
        # (period, dimension) pair gets its own value. dsl_build already emits both
        # when the payload carries group_by_dim alongside time_group.
        if dim:
            payload["group_by_dim"] = dim
    elif mode == "table":
        view = _find_view(config, "table") or {}
        payload["group_by_dim"] = dim or view.get("by")
        if not payload["group_by_dim"]:
            sys.exit("table mode needs a dimension: pass --dim <field>")
    return payload


# ============================================================================
# 8. PREVIEW  — inline params into %s placeholders (display only, NOT executed)
# ============================================================================
def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, tuple):                         # IN (...)
        return "(" + ", ".join(_lit(x) for x in v) + ")"
    if isinstance(v, list):                          # ANY(ARRAY[...]) / && ARRAY[...]
        return "ARRAY[" + ", ".join(_lit(x) for x in v) + "]"
    return "'" + str(v).replace("'", "''") + "'"


def inline_preview(sql, params):
    """Replace %s with quoted params and %% with % — for a copy-pasteable view."""
    out, i, n = [], 0, len(sql)
    pit = iter(params or [])
    while i < n:
        ch = sql[i]
        if ch == "%" and i + 1 < n:
            nxt = sql[i + 1]
            if nxt == "%":
                out.append("%"); i += 2; continue
            if nxt == "s":
                try:
                    out.append(_lit(next(pit)))
                except StopIteration:
                    out.append("%s")
                i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


# ============================================================================
# 9. CLI
# ============================================================================
def parse_filters(pairs):
    """['sector=Retail', 'region=EMEA,APAC'] -> {'sector':['Retail'],
    'region':['EMEA','APAC']}. Comma splits into a multi-value (IN/ANY)."""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit("bad --filter %r (expected field=value[,value])" % p)
        k, v = p.split("=", 1)
        out[k.strip()] = [s.strip() for s in v.split(",") if s.strip()]
    return out


def emit(title, config, payload):
    sql, params = build_sql(config, payload)
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    print("payload:", json.dumps(payload, default=str))
    print("\n-- SQL (with %s bind placeholders) ------------------------------")
    print(sql)
    print("\n-- params -------------------------------------------------------")
    print(params)
    print("\n-- inlined preview (display only — DO NOT execute) --------------")
    print(inline_preview(sql, params))


def main():
    ap = argparse.ArgumentParser(
        description="Generate the SQL a KPI config would run, for chosen "
                    "dates / filters / dimensions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("config", nargs="?", help="KPI name or path to a config json")
    ap.add_argument("--mode", choices=["stat", "series", "table"], default="stat",
                    help="stat = scalar value (default); series = time buckets; "
                         "table = grouped by a dimension")
    ap.add_argument("--from", dest="frm", default=DEF_FROM, help="from date YYYY-MM-DD")
    ap.add_argument("--to", dest="to", default=DEF_TO, help="to date YYYY-MM-DD")
    ap.add_argument("--as-of", dest="as_of", help="snapshot as-of date (overrides window)")
    ap.add_argument("--filter", action="append", metavar="FIELD=VAL[,VAL]",
                    help="user filter; repeat for more dims, comma for multiple values")
    ap.add_argument("--dim", help="dimension to group by (table mode)")
    ap.add_argument("--grain", choices=["day", "week", "month", "quarter"],
                    help="time grain (series mode)")
    ap.add_argument("--comparison", action="store_true",
                    help="emit BOTH current + previous windows (like serve_kpi)")
    ap.add_argument("--list", action="store_true", help="list available configs")
    args = ap.parse_args()

    if args.list:
        for f in sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json"))):
            try:
                c = json.load(open(f, encoding="utf-8"))
                print("%-34s %-4s %-6s %s" % (c.get("name"), c.get("module"),
                      c.get("execution_mode"), c.get("title")))
            except Exception as e:
                print("  (skip %s: %s)" % (os.path.basename(f), e))
        return
    if not args.config:
        ap.error("give a config name (or --list). e.g. gen_query.py emergency")

    config = load_config(args.config)
    filter_by = parse_filters(args.filter)
    print("config : %s  (%s / execution_mode=%s)"
          % (config["name"], config.get("module"), config.get("execution_mode")))
    print("mode   : %s   filters: %s" % (args.mode, filter_by or "none"))

    # stat mode can honour the config's comparison windows; series/table use the
    # given window directly (that is what the widget endpoints do).
    if args.mode == "stat":
        if args.as_of:                               # explicit snapshot override
            windows = [("as-of %s" % args.as_of, (None, _end(args.as_of)))]
        else:
            r = resolve_comparison(config, args.frm, args.to)
            windows = [("%s window" % r["cur_label"], r["cur"])]
            if args.comparison and r["prev"]:
                windows.append(("%s window" % r["prev_label"], r["prev"]))
    else:
        windows = [("%s window" % args.mode,
                    (_start(args.frm), _end(args.to)))]

    for label, win in windows:
        payload = build_payload(config, args.mode, win, filter_by, args.dim, args.grain)
        emit("%s  [%s]" % (config["name"], label), config, payload)


if __name__ == "__main__":
    main()
