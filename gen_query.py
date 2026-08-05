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
    FROM is a plain real table (chain ends) or it joins to ANOTHER branch
    (two independently-derived sources combined = not this single-branch
    class; refuse rather than guess which one is "the" dimension source)."""
    frm = select_node.args.get("from_")
    joins = select_node.args.get("joins") or []
    if joins or frm is None:
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


def _apply_dimension_chain(chain, base_alias, cols, grp_names):
    """Mutate every SELECT in ``chain`` (outer -> inner, as returned by
    ``_sql_dimension_chain``) in place: project the raw dimension column(s)
    at the innermost (table-owning) level, thread ``grp``/``grp2``/… back up
    through every wrapping level, and add them to the GROUP BY of every level
    that is itself an aggregation point (there can be more than one)."""
    s_table_idx = len(chain) - 1
    s_table_sel, _ = chain[s_table_idx]
    for col, grp in zip(cols, grp_names):
        expr = sqlglot.parse_one("SELECT %s.%s AS %s" % (base_alias, col, grp),
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


def _sql_pushdown_ratio_group_by(tree, config, dim_list, fields, schema, table):
    """Handle the "ratio of two bare aggregate totals" shape: push the
    dimension down into EACH branch independently (reusing
    ``_sql_dimension_chain``/``_apply_dimension_chain``, so each branch ends
    up grouped by it), then turn the branches' unconditional CROSS/comma join
    into a ``FULL OUTER JOIN ON`` the dimension — FULL, not INNER, so a
    dimension value present on only one side (e.g. a sector with numerator
    activity but zero denominator activity) isn't silently dropped — and
    exposes ``COALESCE(branch1.grp, branch2.grp) AS grp`` on the outermost
    SELECT so the result is labelled even when one side is missing that
    value. Raises ``ValueError`` if this isn't that exact shape, or the
    dimension isn't reachable in one of the two branches."""
    ctes = _cte_map(tree)
    two = _sql_two_branch_ratio(tree, ctes)
    if two is None:
        raise ValueError(
            "table/dimension mode is not supported for SQL-mode KPI %r: it "
            "isn't a plain 'two bare totals combined with no join key' ratio "
            "either (an existing join predicate or an already-grouped branch "
            "means the dimension can't be added generically here). Use its "
            "drilldown breakdown or a DSL KPI instead." % config.get("name"))
    (sel1, alias1), (sel2, alias2), join = two

    grp_names = ["grp" if i == 0 else "grp%d" % (i + 1) for i in range(len(dim_list))]
    cols = [fields[d]["column"].rsplit(".", 1)[-1] for d in dim_list]
    for sel, alias in ((sel1, alias1), (sel2, alias2)):
        chain, base_alias = _sql_dimension_chain(sel, ctes, schema, table)
        if chain is None:
            raise ValueError(
                "table/dimension mode is not supported for SQL-mode KPI %r: "
                "its primary table %r isn't reachable in branch %r of this "
                "ratio query." % (config.get("name"), table, alias))
        _apply_dimension_chain(chain, base_alias, cols, grp_names)

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
        tree.append("expressions", expr)
        coalesced.append(expr.this.copy())    # the bare COALESCE(...) call, no alias
    # The outermost SELECT may itself aggregate further over the two branches'
    # per-dimension rows (e.g. AVG of a per-branch ratio) — same as any other
    # level in the single-branch chain, that needs the dimension in ITS GROUP
    # BY too, else it collapses right back across the dimension it just got.
    # Group by the COALESCE expression itself, not the bare `grp` alias: with
    # both branches' `grp` columns in scope from the FULL OUTER JOIN, a bare
    # `GROUP BY grp` is ambiguous between the output alias and either side's
    # input column.
    if _needs_group_by(tree):
        existing = tree.args.get("group")
        if existing:
            for c in coalesced:
                existing.append("expressions", c)
        else:
            tree.set("group", exp.Group(expressions=coalesced))

    return tree.sql(dialect="postgres")


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


def _sql_pushdown_keyed_branch_group_by(tree, config, dim_list, fields, schema, table):
    """Handle the "two branches, each already GROUP BY'd and joined on a
    shared grain key" shape (see ``_sql_two_branch_keyed``) — e.g. a
    "created vs closed by month" widget. Pushes ``dim_list`` down into EACH
    branch's OWN GROUP BY (widening it alongside the existing grain column,
    not replacing it — the widget's time grain and the requested breakdown
    both survive), widens the join's ON-clause to also equate the new
    dimension (so rows only line up when both the grain AND the dimension
    match), and exposes ``COALESCE(branch1.grp, branch2.grp) AS grp`` on the
    outer SELECT — same reasoning as ``_sql_pushdown_ratio_group_by``: FULL
    OUTER semantics mean a dimension value present on only one side (e.g. a
    vendor with created activity but zero closed activity that period) is
    never silently dropped. Raises ``ValueError`` if this isn't that shape,
    or the dimension isn't reachable in one of the two branches."""
    ctes = _cte_map(tree)
    two = _sql_two_branch_keyed(tree, ctes)
    if two is None:
        raise ValueError(
            "table/dimension mode is not supported for SQL-mode KPI %r: its "
            "two-branch comparison query isn't the plain grain-keyed shape "
            "either (either branch isn't already GROUP BY'd, or the join "
            "predicate is more than a simple key equality) — cannot "
            "generically add a dimension here. Use its drilldown breakdown "
            "or a DSL KPI instead." % config.get("name"))
    (sel1, alias1), (sel2, alias2), join = two

    grp_names = ["grp" if i == 0 else "grp%d" % (i + 1) for i in range(len(dim_list))]
    cols = [fields[d]["column"].rsplit(".", 1)[-1] for d in dim_list]
    for sel, alias in ((sel1, alias1), (sel2, alias2)):
        chain, base_alias = _sql_dimension_chain(sel, ctes, schema, table)
        if chain is None:
            raise ValueError(
                "table/dimension mode is not supported for SQL-mode KPI %r: "
                "its primary table %r isn't reachable in branch %r of this "
                "comparison query." % (config.get("name"), table, alias))
        _apply_dimension_chain(chain, base_alias, cols, grp_names)

    on_expr = join.args.get("on")
    for g in grp_names:
        cond = exp.EQ(this=exp.column(g, table=alias1), expression=exp.column(g, table=alias2))
        on_expr = exp.And(this=on_expr, expression=cond)
    join.set("on", on_expr)

    coalesced = []
    for g in grp_names:
        expr = sqlglot.parse_one("SELECT COALESCE(%s.%s, %s.%s) AS %s"
                                 % (alias1, g, alias2, g, g), read="postgres").expressions[0]
        tree.append("expressions", expr)
        coalesced.append(expr.this.copy())
    if _needs_group_by(tree):
        existing = tree.args.get("group")
        if existing:
            for c in coalesced:
                existing.append("expressions", c)
        else:
            tree.set("group", exp.Group(expressions=coalesced))

    return tree.sql(dialect="postgres")


def _sql_pushdown_group_by(sql, config, dim_list, fields):
    """Push ``dim_list`` down to the KPI's primary table through a chain of
    wrapping CTEs/subqueries, and thread it back up to the outermost SELECT —
    see the module comment above. Falls back, in order, to
    ``_sql_pushdown_keyed_branch_group_by`` (two branches already GROUP BY'd
    and joined on a shared grain key — e.g. a "created vs closed by month"
    widget) and then ``_sql_pushdown_ratio_group_by`` (two bare,
    independently-aggregated totals with no existing join key) for the
    two-branch shapes a single chain can't reach. Returns the rewritten SQL,
    or raises ``ValueError`` if the query matches none of the three shapes."""
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
    chain, base_alias = _sql_dimension_chain(tree, ctes, schema, table)
    if chain is not None:
        grp_names = ["grp" if i == 0 else "grp%d" % (i + 1) for i in range(len(dim_list))]
        cols = [fields[d]["column"].rsplit(".", 1)[-1] for d in dim_list]
        _apply_dimension_chain(chain, base_alias, cols, grp_names)
        return tree.sql(dialect="postgres")

    if _sql_two_branch_keyed(tree, ctes) is not None:
        return _sql_pushdown_keyed_branch_group_by(tree, config, dim_list, fields, schema, table)
    return _sql_pushdown_ratio_group_by(tree, config, dim_list, fields, schema, table)


def _sql_inject_group_by(sql, config, dim_list, fields):
    """Rewrite an authored aggregate ``SELECT ... FROM ... WHERE ...`` so it also
    groups by ``dim_list`` — one ``grp``/``grp2``/… column per requested
    dimension, added right after ``SELECT`` and via ``GROUP BY`` at the end.

    This reuses whatever measure expression the author wrote (AVG, ratio,
    COUNT, …) instead of requiring a second hand-authored breakdown query, so
    "by sector" always answers with the SAME metric as the ungrouped stat.
    Raises if the query has no leading ``SELECT`` to anchor on, or already
    carries its own top-level ``GROUP BY`` (a hand-authored grouped query — we
    won't guess how to merge a second one in safely).
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
        raise ValueError(
            "SQL-mode KPI %r cannot add a dimension GROUP BY: its base_query "
            "already has one." % config.get("name"))
    m = re.match(r"(?is)^\s*select\s+", sql)
    if not m:
        raise ValueError(
            "SQL-mode KPI %r base_query doesn't start with SELECT; cannot inject "
            "a dimension GROUP BY." % config.get("name"))
    sel_cols, group_idx = [], []
    for i, d in enumerate(dim_list, start=1):
        col_expr = fields[d]["column"]          # already alias-qualified if needed
        alias = "grp" if i == 1 else "grp%d" % i
        sel_cols.append("%s AS %s" % (col_expr, alias))
        group_idx.append(str(i))
    sql = sql[:m.end()] + ", ".join(sel_cols) + ", " + sql[m.end():]
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
           vetted; a dim outside it is rejected even if it's a real column.
        2. no ``allowed_group_by`` declared at all -> SCHEMA FALLBACK: a dim is
           still groupable if its physical column is a real column on the KPI's
           primary table (``schema_v3.yaml``), the same fallback already used
           for filters and series/table dims elsewhere. A dim that's neither
           curated nor a real schema column is rejected — use the KPI's
           drilldown breakdown or a DSL KPI instead.
    """
    payload = payload or {}
    dims = payload.get("group_by_dim")
    dim_list = (list(dims) if isinstance(dims, (list, tuple)) else [dims]) if dims else []
    fields = dict(get_fields(config))       # local copy: schema fallback may add entries
    if dim_list:
        allowed = sql_allowed_group_by(config)
        schema_cols = None if allowed is not None else _sql_primary_table_columns(config)
        pd_name = (config.get("primary_dataset") or {}).get("table") \
            or (config.get("primary_dataset") or {}).get("name")
        for d in dim_list:
            if allowed is not None:
                if d not in allowed:
                    raise ValueError(
                        "dimension %r is not groupable for SQL-mode KPI %r; allowed: %s"
                        % (d, config.get("name"), sorted(allowed)))
                if d not in fields:
                    raise ValueError(
                        "dimension %r not defined in fields for %s" % (d, config.get("name")))
                continue
            # No allowed_group_by declared -> fall back to a real schema column.
            # `d` may already be a declared field (use its physical column) or a
            # bare column name the config never declared as a field at all.
            col = (fields.get(d) or {}).get("column", d)
            bare = col.rsplit(".", 1)[-1]        # strip any authored alias qualifier
            if not schema_cols or bare not in schema_cols:
                raise ValueError(
                    "table/dimension mode is not supported for SQL-mode KPI %r: "
                    "dimension %r is neither in an allowed_group_by list (none "
                    "declared) nor a real column on its primary table %r. Use "
                    "its drilldown breakdown or a DSL KPI." % (config.get("name"), d, pd_name))
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

    # ── SELECT ───────────────────────────────────────────────────────────
    dims = payload.get("group_by_dim")
    if dims:
        # accept a single field (str) or several (list) — one GROUP BY column each.
        # first dim keeps the historical `grp` alias; extras get grp2, grp3, …
        dim_list = list(dims) if isinstance(dims, (list, tuple)) else [dims]
        for i, dname in enumerate(dim_list):
            c = col(dname)
            alias = "grp" if i == 0 else "grp%d" % (i + 1)
            sel.append("%s AS %s" % (c, alias)); group.append(c)
    for g in dsl.get("group_by_fields", []) or []:
        c = col(g["field"]); sel.append("%s AS %s" % (c, g["alias"])); group.append(c)
    for d in dsl.get("dimensions", []) or []:
        c = col(d["field"]); sel.append("%s AS %s" % (c, d["alias"])); group.append(c)
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
