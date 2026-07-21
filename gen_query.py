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


def driver_substitute(config, payload):
    """SQL-mode: substitute the window (and user filters) into the authored
    ``config.sql.base_query`` -> ``(sql, [])``.

    * Dates fill ``{from_date}`` / ``{to_date}`` / ``{as_of}`` as before.
    * User filters fill a ``{filters}`` placeholder (author-positioned in the
      right scope). If filters were requested but the query has no ``{filters}``
      slot, we RAISE rather than silently drop them — a silently-unfiltered
      number is worse than a clear error.
    * ``table``/dimension mode cannot be expressed against an authored scalar
      query, so a ``group_by_dim`` request is rejected (use the KPI's drilldown
      breakdown or a DSL KPI instead) rather than returning an ungrouped scalar.
    """
    payload = payload or {}
    if payload.get("group_by_dim"):
        raise ValueError(
            "table/dimension mode is not supported for SQL-mode KPI %r: an "
            "authored scalar query has no generic GROUP BY. Use its drilldown "
            "breakdown or a DSL KPI." % config.get("name"))
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
except ImportError:
    sqlglot = None
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
