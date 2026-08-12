"""Bridge between the MCP layer and ``gen_query.py``.

``generate_query`` takes a KPI name plus a request (a natural-language period or
explicit dates, a render mode, an optional dimension / grain / filters) and
returns the exact SQL + bind params ``gen_query`` would emit — for **both**
execution-mode families:

  * DSL configs   -> compiled through the structured DSL builder
  * SQL configs   -> the authored ``base_query`` with the date window substituted

``gen_query.py`` is imported as a library (not re-implemented); this module only
does request assembly, date resolution and result shaping. It mirrors the
control flow of ``gen_query.main`` (stat honours the config's comparison
windows; table uses the given window directly).

Two deliberate divergences from ``gen_query.main``:

* **SQL-mode series** — a ``series`` request on a SQL-mode config is expanded into
  one authored-query run per period bucket (see ``bucket_windows``), because the
  authored scalar query cannot ``GROUP BY`` a time bucket the way the DSL builder
  does. This makes "increase or decrease over the last N months" answerable —
  each bucket returns its own value instead of one blended average.
* **Explicit-window stat** — when the caller names a concrete period (``period``
  or ``from_date``/``to_date``), ``stat`` mode uses THAT window verbatim rather
  than expanding it to the KPI's CYTD comparison window. The CYTD/PYTD comparison
  basis only applies to the standard governed tile (no period given), so a
  conversational "…last quarter" is no longer silently answered with year-to-date.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Union

from cora_mcp import db, module_registry, sql_alias
from cora_mcp.date_resolver import bucket_windows, resolve_dates
from cora_mcp.kpi_catalog import get_catalog
from cora_mcp.logging_config import get_logger
from cora_mcp.opensearch_client import available_group_by_terms, config_dimensions

log = get_logger(__name__)

# Make the standalone gen_query.py importable (it lives at the project root).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gen_query as gq  # noqa: E402  (path set up above)

_MODES = ("stat", "series", "table")


def _kpi_citation(config: Dict[str, Any], kpi: Optional[str] = None) -> Dict[str, Any]:
    """Build a single citation entry describing the data source backing a KPI
    config, so answers can say where a number came from instead of just what
    it is."""
    source = config.get("source") or {}
    primary = config.get("primary_dataset") or {}
    governance = config.get("governance") or {}
    schema = primary.get("schema") or source.get("schema")
    table = primary.get("table") or primary.get("name")
    dataset = f"{schema}.{table}" if schema and table else (table or schema)
    return {
        "kpi": kpi or config.get("name"),
        "title": config.get("title"),
        "module": config.get("module"),
        "dataset": dataset,
        "connection": source.get("connection"),
        "dialect": source.get("dialect"),
        "owner": governance.get("owner"),
        "updated_at": governance.get("updated_at"),
    }


class QueryError(ValueError):
    """Raised for bad requests (unknown KPI, missing dimension, bad mode)."""


def _jsonable(value: Any) -> Any:
    """gen_query params may hold tuples (IN lists) — make them JSON friendly."""
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _normalize_filters(filters: Union[Dict[str, Any], List[str], None]) -> Dict[str, List[Any]]:
    """Accept {field: value|[values]} or ['field=v,v'] -> {field: [values]}."""
    if not filters:
        return {}
    if isinstance(filters, dict):
        out: Dict[str, List[Any]] = {}
        for k, v in filters.items():
            out[k] = list(v) if isinstance(v, (list, tuple)) else [v]
        return out
    if isinstance(filters, list):
        return gq.parse_filters(filters)
    raise QueryError(f"unsupported filters type: {type(filters).__name__}")


def _available_filters_desc(config: dict) -> Dict[str, List[str]]:
    """{allowed filter key -> its accepted aliases} for error messages / discovery."""
    from cora_mcp.filter_aliases import get_registry
    reg = get_registry()
    allowed = (config.get("filters") or {}).get("allowed") or []
    return {k: reg.aliases_of(k) for k in allowed}


def resolve_filter_key(config: dict, term: str) -> str:
    """Resolve a user filter term (alias or canonical key) to the KPI's canonical
    filter key. Raises QueryError if the term is unknown or not filterable here."""
    from cora_mcp.filter_aliases import get_registry, normalize
    reg = get_registry()
    allowed = (config.get("filters") or {}).get("allowed") or []
    allowed_by_norm = {normalize(k): k for k in allowed}

    n = normalize(term)
    if n in allowed_by_norm:                     # canonical key (or exact match)
        return allowed_by_norm[n]
    canonical = reg.canonical_for(term)          # alias -> canonical key
    if canonical is not None and normalize(canonical) in allowed_by_norm:
        return allowed_by_norm[normalize(canonical)]

    # Schema fallback: a real column on the KPI's primary table that the config
    # simply didn't declare as a filter. This ADDS a user-requested filter on a
    # verified column (never silently drops one), so — like ad-hoc mode, which
    # filters on any schema column — it is honoured even past a curated
    # allowed-list rather than raising "unknown filter".
    sc = resolve_dim_via_schema(config, term, roles=())       # any role for filters
    if sc:
        return sc

    avail = _available_filters_desc(config)
    # Telemetry: a rejected filter is the signal that a question needed a qualifier the
    # metric layer cannot express. Logged in one structured line so the frequency of
    # each missing term can be counted from the logs, rather than guessing at whether
    # the composer/predicate layer is earning its keep (MULTI_METRIC_ANALYSIS.md §8).
    predicate_hint = None
    try:
        from cora_mcp.predicate_registry import get_registry as _preds
        hit = _preds().resolve(term)
        predicate_hint = hit.name if hit else None
    except Exception:                      # never let telemetry break resolution
        pass
    log.info("FILTER_REJECTED kpi=%s term=%r canonical=%r predicate_match=%r "
             "primary_table=%s available=%s",
             config.get("name"), term, canonical, predicate_hint,
             _kpi_primary_fqn(config), sorted(avail))
    if predicate_hint:
        raise QueryError(
            f"{term!r} is a scope predicate ({predicate_hint!r}), not a filter on KPI "
            f"{config.get('name')!r}. It restricts WHICH records count rather than "
            f"naming a dimension, and on this metric's table it may not be expressible "
            f"at all. Use compose_metric(predicates=[{predicate_hint!r}], ...) to have "
            f"the anchor table chosen accordingly. "
            f"This KPI's own filters: {avail}")
    if canonical is not None:
        raise QueryError(
            f"filter {term!r} (means {canonical!r}) is not available on KPI "
            f"{config.get('name')!r}. available filters: {avail}")
    raise QueryError(
        f"unknown filter {term!r} for KPI {config.get('name')!r}. "
        f"available filters (with aliases): {avail}")


def try_resolve_filter_key(config: dict, term: str) -> Optional[str]:
    """Non-raising variant for callers that skip un-appliable filters (overview)."""
    try:
        return resolve_filter_key(config, term)
    except QueryError:
        return None


def _resolve_filters(config: dict, filter_by: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite user filter keys (aliases) to the KPI's canonical keys. Merges
    values if two aliases resolve to the same key. Raises on any bad term."""
    if not filter_by:
        return {}
    out: Dict[str, Any] = {}
    for key, value in filter_by.items():
        if key == "granularity":                 # internal series knob, passthrough
            out[key] = value
            continue
        canonical = resolve_filter_key(config, key)
        vals = list(value) if isinstance(value, (list, tuple)) else [value]
        out.setdefault(canonical, [])
        for v in vals:
            if v not in out[canonical]:
                out[canonical].append(v)
    return out


def _validate_filters(config: dict, filter_by: Dict[str, Any]) -> None:
    """Reject any requested filter the KPI can't honour, with a clear message.

    Guards both families up front: without this a DSL config raised a raw
    ``KeyError`` for an unknown field (unhandled 500) and a SQL config silently
    accepted — then dropped — a bogus field. A filter must be declared in the
    config's ``filters.allowed`` list (when present) and map to a real column in
    ``fields`` — OR be a real column on the KPI's primary table discovered via the
    schema fallback (which ``_augment_fields`` has already added to ``fields``).
    """
    if not filter_by:
        return
    allowed = (config.get("filters") or {}).get("allowed") or []
    fields = config.get("fields") or {}
    schema_cols = schema_columns_for(config)
    for key in filter_by:
        if key == "granularity":            # internal series knob, not a filter
            continue
        # An explicit allowed-list caps the *curated* filters, but a real schema
        # column the user named is still honoured (mirrors ad-hoc filtering).
        if allowed and key not in allowed and key not in schema_cols:
            raise QueryError(
                f"filter {key!r} is not allowed for KPI {config.get('name')!r}. "
                f"allowed filters: {allowed}")
        if key not in fields:
            raise QueryError(
                f"filter {key!r} is not a known field for KPI "
                f"{config.get('name')!r}. allowed filters: {allowed or sorted(fields)}")


def _field_possible_values(config: dict, field_meta: dict) -> Optional[List[Any]]:
    """Best-effort lookup of a config field's schema ``possible_values``.

    A KPI ``fields`` entry carries ``{dataset, column}`` but not the enum; the
    domain lives in ``schema_v3.yaml``. Reconstruct the table fqn from the field's
    dataset (or the config's primary_dataset) and read the column off the loader.
    Returns ``None`` if it can't be resolved (then values pass through unchanged).
    """
    try:
        from cora_mcp.schema_loader import get_loader
        loader = get_loader()
        col = field_meta.get("column")
        if not col or "." in col:            # alias-qualified custom columns: skip
            return None
        pd = config.get("primary_dataset") or {}
        schema = pd.get("schema")
        ds = field_meta.get("dataset")
        candidates = []
        if schema and ds:
            candidates.append(f"{schema}.{ds}")
        if pd.get("schema") and pd.get("table"):
            candidates.append(f"{pd['schema']}.{pd['table']}")
        for fqn in candidates:
            ci = loader.column_info(fqn, col)
            if ci and ci.get("possible_values"):
                return list(ci["possible_values"])
    except Exception as exc:                 # never let a lookup break query gen
        log.debug("possible_values lookup failed: %s", exc)
    return None


def _resolve_filter_values(config: dict, filter_by: Dict[str, Any]) -> Dict[str, Any]:
    """Map user filter VALUES to real stored values against each column's domain
    (schema ``possible_values``), mirroring the dataset path. 'completed' ->
    'CLOSED'; an unknown value raises QueryError with the valid list. Columns with
    no declared domain are left untouched."""
    from cora_mcp import value_resolver
    if not filter_by:
        return filter_by
    fields = config.get("fields") or {}
    out: Dict[str, Any] = {}
    for key, value in filter_by.items():
        if key == "granularity":
            out[key] = value
            continue
        pv = _field_possible_values(config, fields.get(key) or {})
        if not pv:
            out[key] = value
            continue
        vals = list(value) if isinstance(value, (list, tuple)) else [value]
        out[key] = value_resolver.resolve_or_raise(key, vals, pv, QueryError)
    return out


def _kpi_primary_fqn(config: dict) -> Optional[str]:
    """The schema-qualified table name for a KPI's primary_dataset, or None."""
    pd = config.get("primary_dataset") or {}
    schema, table = pd.get("schema"), pd.get("table")
    if schema and table:
        return "%s.%s" % (schema, table)
    return None


def schema_columns_for(config: dict) -> Dict[str, dict]:
    """All schema columns available on a KPI's PRIMARY table -> {name: column_info}.

    Only the primary table (base alias ``a``) is considered — columns living in
    joined/other tables need a join to reference, which the fallback deliberately
    does not attempt. Returns {} when the table isn't in ``schema_v3.yaml``.
    """
    fqn = _kpi_primary_fqn(config)
    if not fqn:
        return {}
    try:
        from cora_mcp.schema_loader import get_loader
        return get_loader().table_columns(fqn)
    except Exception as exc:                          # never let a lookup break query gen
        log.debug("schema_columns_for(%s) failed: %s", fqn, exc)
        return {}


def resolve_dim_via_schema(
    config: dict, word: str, roles: tuple = ("dimension",)
) -> Optional[str]:
    """Fallback: map a dimension/filter word to a real column on the KPI's primary
    table from ``schema_v3.yaml`` when the KPI config itself doesn't declare it.

    Restricted to columns whose schema ``role`` is in ``roles`` (dimensions only, by
    default) so a breakdown never groups by a measure/timestamp/identifier; pass
    ``roles=()`` for filtering, where any role is fair game.

    Delegates to :func:`cora_mcp.column_resolver.resolve_column`, so this path now also
    honours a column's declared ``canonical`` / ``alias`` vocabulary — metadata the
    schema has always carried but nothing consulted. That is what lets a question say
    "sla breached" or "capability" and reach ``sla_breached_indicator`` /
    ``service_area`` without a per-KPI alias entry.
    """
    if not word:
        return None
    fqn = _kpi_primary_fqn(config)
    if not fqn:
        return None
    from cora_mcp.column_resolver import resolve_column
    return resolve_column(fqn, word, roles=roles)


def resolve_dim_word(config: dict, word: str) -> Optional[str]:
    """Map a user dimension word to a groupable field/dim name for THIS KPI.

    A breakdown word ("business") is rarely the literal column: it may be a field
    the KPI declares, a drilldown dimension ("business_name"), the same word with a
    "_name"/"_description" suffix, or a filter alias ("business"->"sector"). When
    the KPI config declares none of these, fall back to the schema: any dimension
    column on the KPI's primary table is groupable even if the config omitted it.
    Returns a name usable by the SQL builder's column resolver, or None if neither
    the config nor the schema offers that word.
    """
    if not word:
        return None
    fields = config.get("fields") or {}
    dims = config_dimensions(config)
    w = str(word).strip()
    wl = w.lower()
    if w in fields:                                   # already a real field name
        return w
    for d in dims:                                    # exact drilldown dimension
        if d.lower() == wl:
            return d
    candidates = {wl, wl + "_name", wl + "_description"}
    for d in list(dims) + list(fields.keys()):        # suffix-aware match
        dl = d.lower()
        if dl in candidates:
            return d
        for suf in ("_name", "_description"):
            if dl.endswith(suf) and dl[: -len(suf)] == wl:
                return d
    alias = try_resolve_filter_key(config, w)          # filter alias -> canonical field
    if alias:
        return alias
    return resolve_dim_via_schema(config, w)           # schema fallback (primary table)


def _synth_field_meta(config: dict, col: str, ci: Optional[dict] = None) -> dict:
    """A ``fields`` entry for a schema-discovered column on the KPI's primary table
    (base alias ``a``), so the DSL builder can emit it as ``a.<col>``."""
    pd = config.get("primary_dataset") or {}
    meta = {"dataset": pd.get("table"), "column": col, "filter_type": "in"}
    if ci and ci.get("type"):
        meta["type"] = ci["type"]
    return meta


def _augment_fields(config: dict, names) -> dict:
    """Return ``config`` (shallow copy) with synthetic ``fields`` entries for any
    requested dimension/filter name that is a real primary-table column but was
    not declared in the config. Names absent from the schema are left out — the
    caller then handles the miss (drop the dim / raise for a bad filter)."""
    fields = config.get("fields") or {}
    wanted = [n for n in names if n and n not in fields]
    if not wanted:
        return config
    schema_cols = schema_columns_for(config)
    add = {n: _synth_field_meta(config, n, schema_cols.get(n))
           for n in wanted if n in schema_cols}
    if not add:
        return config
    new = dict(config)
    new["fields"] = {**fields, **add}
    return new


def _effective_dim(
    config: dict, mode: str, dim: Union[str, List[str], None]
) -> Union[str, List[str], None]:
    # Dimensions are groupable in BOTH table mode (breakdown) and series mode (a
    # per-dimension trend — a time bucket AND a group-by at once).
    if mode not in ("table", "series"):
        return dim
    if not dim:
        if mode == "table":
            view = gq._find_view(config, "table") or {}
            return view.get("by")
        return None                       # series without a dimension = overall trend
    # Normalise each requested breakdown word to a groupable name for this KPI.
    # An unresolved word is left as-is so the builder raises its clear column error.
    if isinstance(dim, (list, tuple)):
        return [resolve_dim_word(config, d) or d for d in dim]
    return resolve_dim_word(config, dim) or dim


# Trend grain is derived from the requested window span, not dictated by the caller:
# a month-long window trends WEEKLY, a multi-month window MONTHLY, a multi-year window
# QUARTERLY. An explicit grain is honoured only when it still yields a real trend
# (>= 2 buckets and not absurdly many) — so "monthly over last 6 months" is kept, but
# "monthly over last month" (1 bucket) is corrected to weekly.
def _span_days(frm: str, to: str) -> int:
    from datetime import date
    a = date.fromisoformat(str(frm)[:10])
    b = date.fromisoformat(str(to)[:10])
    return abs((b - a).days) + 1


def _auto_grain(frm: str, to: str, requested: Optional[str]) -> str:
    span = _span_days(frm, to)
    if span <= 14:
        natural = "day"
    elif span <= 45:
        natural = "week"
    elif span <= 750:
        natural = "month"
    else:
        natural = "quarter"
    if requested:
        try:
            n = len(bucket_windows(frm, to, requested))
        except Exception:
            n = 0
        if 2 <= n <= 60:
            return requested
    return natural


# ASCII Unit Separator — folds several group-by columns into one composite key so a
# single-{dim} authored template can group by multiple dimensions; it never occurs in
# real dimension values, so splitting the key back apart is lossless.
_MDIM_SEP = "chr(31)"


def _dim_to_column(config: dict, name: str) -> str:
    """The PHYSICAL column for a resolved dimension name. A KPI's filter/dimension
    can be a *field* whose real column differs (``region`` -> ``region_name``); the
    authored ``a.{dim}`` breakdown template needs the real column, not the field name.
    Drilldown dims are already column names, so they pass through unchanged."""
    meta = (config.get("fields") or {}).get(name) or {}
    col = meta.get("column")
    return col if col and "." not in col else name


def _breakdown_inner_sql(config: dict, dim_cols: List[str]) -> Optional[str]:
    """Substitute a KPI's authored ``{dim}`` breakdown query for ONE or MORE group-by
    columns (each mapped to its physical column). Multiple columns are folded into a
    single composite key ``(a.c1::text || sep || a.c2::text)`` so the template — which
    uses ``a.{dim}`` in SELECT, GROUP BY and any self-joins — stays valid with one
    substitution. Returns ``None`` when the template can't express the request (no
    ``{dim}`` slot, or a multi-dim fold left an un-prefixed ``{dim}``)."""
    query = ((config.get("drilldown") or {}).get("breakdown") or {}).get("query")
    if not query or "{dim}" not in query:
        return None
    cols = [_dim_to_column(config, c) for c in dim_cols]
    if len(cols) == 1:
        return query.replace("{dim}", cols[0])
    expr = "(" + (" || %s || " % _MDIM_SEP).join("a.%s::text" % c for c in cols) + ")"
    out = query.replace("a.{dim}", expr)
    return None if "{dim}" in out else out


def _grp_select(dim_cols: List[str]) -> str:
    """Projection that exposes the (possibly composite) group key as grp, grp2, … —
    the shape ``_breakdown_rows`` and the DSL table path already use."""
    if len(dim_cols) == 1:
        cols = ["_b.grp AS grp"]
    else:
        cols = ["split_part(_b.grp, %s, %d) AS %s"
                % (_MDIM_SEP, i + 1, "grp" if i == 0 else "grp%d" % (i + 1))
                for i in range(len(dim_cols))]
    return ", ".join(cols)


def _sql_bucket_breakdown(config: dict, dim_cols, win, label: str):
    """One time bucket of a SQL-mode trend broken down by one or more dimensions.

    The authored scalar query can't GROUP BY, but the KPI's ``{dim}`` breakdown query
    can — run it for this bucket's window and tag every row with the bucket label,
    yielding ``(bucket, grp[, grp2, …], v)`` rows. Dates are inlined (the template
    carries no bind params), so ``params`` is empty."""
    dim_cols = list(dim_cols) if isinstance(dim_cols, (list, tuple)) else [dim_cols]
    inner_tmpl = _breakdown_inner_sql(config, dim_cols)
    if inner_tmpl is None:
        raise QueryError("SQL-mode KPI has no reusable breakdown query for these dimensions")
    cf, ct = win
    inner = gq.substitute_dates(inner_tmpl, {"from_date": cf, "to_date": ct, "as_of": ct})
    if "{" in inner:
        raise QueryError("SQL-mode breakdown query has an unfilled placeholder for the "
                         "trend bucket window")
    sql = ("SELECT %s AS bucket, %s, _b.v AS v FROM (%s) _b"
           % (gq._lit(label), _grp_select(dim_cols), inner))
    return sql, []


async def generate_query(
    kpi: str,
    period: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    as_of: Optional[str] = None,
    mode: str = "stat",
    dim: Union[str, List[str], None] = None,
    grain: Optional[str] = None,
    filters: Union[Dict[str, Any], List[str], None] = None,
    comparison: bool = False,
) -> Dict[str, Any]:
    """Generate SQL for a KPI config. See module docstring for semantics.

    Async because the config is fetched from the (async) config store; the SQL
    build itself (gen_query) is pure CPU. `dim` (table mode) may be a single field
    or a list of fields to break the KPI down by more than one dimension (e.g.
    ["region_name", "priority"])."""
    catalog = get_catalog()
    config = await catalog.get(kpi)
    if config is None:
        near = [r["name"] for r in await catalog.search(kpi, limit=5)]
        raise QueryError(f"unknown KPI {kpi!r}. Closest matches: {near or 'none'}")

    if mode not in _MODES:
        raise QueryError(f"mode must be one of {_MODES}, got {mode!r}")

    # ---- resolve the date window ----------------------------------------
    resolved_from_phrase = None
    cmp_pair: Optional[Dict[str, Dict[str, Any]]] = None
    if from_date and to_date:
        frm, to = from_date, to_date
    elif period:
        r = resolve_dates(period)
        frm, to = r["start_date"], r["end_date"]
        # A two-sided phrase ("last quarter vs current quarter") carries BOTH
        # windows; every mode below then runs once per side instead of collapsing
        # the question onto whichever side the phrase scanner matched first.
        cmp_pair = r.get("comparison")
        resolved_from_phrase = {"phrase": period, "matched": r["matched"],
                                "start_date": frm, "end_date": to}
        if cmp_pair:
            resolved_from_phrase["comparison"] = {
                side: {k: w[k] for k in ("phrase", "start_date", "end_date")}
                for side, w in cmp_pair.items()}
    else:
        frm, to = gq.DEF_FROM, gq.DEF_TO
    # Did the caller name a concrete window? If so, stat mode honours it literally
    # instead of overriding it with the KPI's default CYTD comparison window.
    explicit_window = bool((from_date and to_date) or period)

    mode_note: Optional[str] = None
    if mode == "stat" and dim:
        # A stat request that ALSO names a breakdown dimension means "give me
        # this metric AS A BREAKDOWN", not "compute the overall number and
        # silently ignore the dimension" -- stat mode's payload has no
        # group_by_dim slot at all (gen_query.build_payload never sets one for
        # it), so honouring `dim` here means actually switching to table mode,
        # the same way the opposite case just below falls back FROM table mode
        # TO stat mode when no dimension was given. `comparison` (prior-YEAR
        # window) is a stat-only concept table mode has no slot for either --
        # rather than silently drop one of the two things the caller asked
        # for, require they be requested separately.
        if comparison:
            raise QueryError(
                f"can't combine dim={dim!r} (a breakdown) with comparison=True "
                f"(the prior-year window) in one mode='stat' call for {kpi!r} -- "
                "these need different modes. Run the breakdown as mode='table' "
                "(no comparison), or the comparison as mode='stat' (no dim).")
        mode_note = (f"dim={dim!r} was requested with mode='stat'; a breakdown "
                     "needs mode='table', so this ran as mode='table' instead.")
        mode = "table"

    eff_dim = _effective_dim(config, mode, dim)
    if mode == "table" and not eff_dim:
        if cmp_pair:
            # A period comparison IS the grouping: one value per compared window.
            # Better than rejecting the request for want of a dimension the caller
            # never asked for.
            mode_note = ("no breakdown dimension was given, so the two compared "
                         "periods are the grouping (one value per period).")
            mode = "stat"
        else:
            dims = available_group_by_terms(config)
            raise QueryError(
                f"table mode needs a dimension for {kpi!r}; pass dim=<field>. "
                f"available: {dims}")

    filter_by = _resolve_filters(config, _normalize_filters(filters))
    # Schema fallback: a dimension/filter the config didn't declare but that the
    # primary table really has is still emittable — inject a synthetic field so the
    # builder and validator treat it like any declared field. Applies to table AND
    # series (a per-dimension trend also groups by the dimension).
    dim_names = ((eff_dim if isinstance(eff_dim, (list, tuple)) else [eff_dim])
                 if (mode in ("table", "series") and eff_dim) else [])
    # Names not already in the KPI's curated `fields` are schema-fallback resolved --
    # kept so a live-execution failure on one of them (schema_v3.yaml claims the
    # column exists but the real table doesn't have it) can be diagnosed as schema
    # drift rather than surfaced as an opaque driver error (see run_query below).
    pre_fields = config.get("fields") or {}
    schema_fallback_cols = sorted({n for n in [*dim_names, *filter_by.keys()]
                                   if n and n not in pre_fields})
    config = _augment_fields(config, [*dim_names, *filter_by.keys()])
    _validate_filters(config, filter_by)
    filter_by = _resolve_filter_values(config, filter_by)
    # A SQL-mode KPI inlines filters (and, for table mode, an injected GROUP BY
    # dimension column) using each field's bare column name. When the authored
    # query joins tables that share that column, the bare reference is ambiguous
    # and Postgres rejects the whole query — so qualify both with the alias the
    # authored query bound.
    config = sql_alias.qualify_filter_columns(config, [*dim_names, *filter_by])

    # ---- build the request windows (mirrors gen_query.main) --------------
    is_sql = config.get("execution_mode") != "DSL"
    # Trend grain follows the duration (weekly for ~a month, monthly for longer),
    # so "trend for last month" buckets by week and "trend for last 6 months" by
    # month — no matter what grain the caller guessed.
    # For a comparison the span is BOTH sides (previous.start .. current.end), so a
    # quarter-vs-quarter trend buckets monthly rather than into ~19 weekly windows
    # picked from the shorter side alone.
    span = ((cmp_pair["previous"]["start_date"], cmp_pair["current"]["end_date"])
            if cmp_pair else (frm, to))
    eff_grain = _auto_grain(span[0], span[1], grain) if mode == "series" else grain

    # A trend broken down by one or more dimensions. Keep only the dimensions this
    # KPI can actually group by — a declared drilldown dim, a real column on its
    # primary table, or a declared field — and break the trend down by that VALID
    # subset; anything unavailable (e.g. NPS "by CI name", where the survey table has
    # no CI column) is recorded so the answer says so, never silently dropped.
    #   * DSL KPIs group by the valid dims directly (bucket + dims).
    #   * SQL-mode KPIs fold the valid dims into a composite key and run the authored
    #     {dim} breakdown query per bucket; per-KPI filters can't be added there.
    sql_series_dim_cols: Optional[List[str]] = None
    dimension_note: Optional[str] = None
    if mode == "series" and eff_dim:
        dims_list = list(eff_dim) if isinstance(eff_dim, (list, tuple)) else [eff_dim]
        schema_cols = set(schema_columns_for(config))
        dims_allowed = set(config_dimensions(config))
        # A dim is groupable when its PHYSICAL column is a real column on the KPI's
        # primary table (or it's a declared drilldown dimension) — so "region"
        # (field -> region_name) counts, but "ci name" (no such column) does not.
        def _ok(d):
            return d in dims_allowed or _dim_to_column(config, d) in schema_cols
        valid = [d for d in dims_list if _ok(d)]
        invalid = [d for d in dims_list if not _ok(d)]
        real_filters = [k for k in filter_by if k != "granularity"]

        if not valid:
            dimension_note = ("this KPI can't break its trend down by %s, so the overall "
                              "trend is shown." % dims_list)
            eff_dim = None
        elif is_sql and _breakdown_inner_sql(config, valid) is None:
            dimension_note = ("this SQL-mode KPI has no reusable breakdown query, so the "
                              "overall trend is shown (not per %s)." % valid)
            eff_dim = None
        elif is_sql and real_filters:
            dimension_note = ("this SQL-mode breakdown can't also apply filter(s) %s, so the "
                              "overall trend is shown." % real_filters)
            eff_dim = None
        else:
            eff_dim = valid if len(valid) > 1 else valid[0]
            if is_sql:
                sql_series_dim_cols = valid
            if invalid:                       # partial: valid subset kept, rest unavailable
                dimension_note = ("broke the trend down by %s; %s not available on this "
                                  "metric." % (valid, invalid))

    side_of_label: Dict[str, str] = {}   # result label -> "previous" | "current"

    def _windows_for(wfrm: str, wto: str, side: Optional[str] = None):
        """The (label, window) list for ONE date range, honouring the mode.

        SQL-mode series still expands into per-bucket runs (an authored scalar
        query can't GROUP BY a time bucket); every other mode is a single window.
        ``side`` tags the label with the phrase it came from, so a comparison's
        rows are self-describing ("2026-04 (last quarter)")."""
        if mode == "series" and is_sql:
            return [("%s %s%s" % (eff_grain, blabel, " (%s)" % side if side else ""),
                     (gq._start(bf), gq._end(bt)))
                    for blabel, (bf, bt) in bucket_windows(wfrm, wto, eff_grain)]
        return [(side or "%s window" % mode, (gq._start(wfrm), gq._end(wto)))]

    if as_of:
        windows = [("as-of %s" % as_of, (None, gq._end(as_of)))]
    elif cmp_pair:
        # Period-over-period comparison: run the SAME request once per side, older
        # first, each labelled with the user's own phrase for it. The KPI's own
        # CYTD/PYTD basis and the `comparison` flag's previous-year window are NOT
        # applied on top — the phrase already named both windows.
        windows = []
        for side in ("previous", "current"):
            w = cmp_pair[side]
            side_windows = _windows_for(w["start_date"], w["end_date"],
                                        (w.get("phrase") or side).strip())
            for lbl, _win in side_windows:
                side_of_label[lbl] = side
            windows += side_windows
    elif mode == "stat" and explicit_window:
        # The user named a period (e.g. "last quarter") -> use THAT window as-is.
        # resolve_comparison would expand a YTD-basis KPI to Jan-1..anchor (CYTD),
        # silently discarding the requested range; only do that when no period was
        # given (the standard governed-tile default, handled in the branch below).
        windows = [("requested window", (gq._start(frm), gq._end(to)))]
        if comparison:
            pf, pt = gq._shift_years(frm, 1), gq._shift_years(to, 1)
            windows.append(("previous-year window", (gq._start(pf), gq._end(pt))))
    elif mode == "stat":
        r = gq.resolve_comparison(config, frm, to)
        windows = [("%s window" % r["cur_label"], r["cur"])]
        if comparison and r["prev"]:
            windows.append(("%s window" % r["prev_label"], r["prev"]))
    elif mode == "series" and is_sql:
        # SQL-mode configs run an authored scalar query and cannot GROUP BY a
        # time bucket, so driver_substitute would blend the whole window into one
        # number. Build the series by running that query once per period bucket —
        # this is what comparative questions ("increase or decrease over the last
        # N months", "trend") need: a value per period, not a single average.
        windows = [("%s %s" % (eff_grain, label), (gq._start(bf), gq._end(bt)))
                   for label, (bf, bt) in bucket_windows(frm, to, eff_grain)]
    else:
        windows = [("%s window" % mode, (gq._start(frm), gq._end(to)))]

    # ---- generate SQL per window ----------------------------------------
    results: List[Dict[str, Any]] = []
    for label, win in windows:
        if sql_series_dim_cols:
            # SQL-mode per-dimension trend: authored breakdown query per bucket.
            sql, params = _sql_bucket_breakdown(config, sql_series_dim_cols, win, label)
        else:
            payload = gq.build_payload(config, mode, win, filter_by, eff_dim, eff_grain)
            try:
                sql, params = gq.build_sql(config, payload)
            except ValueError as exc:
                # gen_query raises ValueError for requests it can't honour (e.g. a
                # SQL-mode KPI asked for a dimension, or filters with no {filters}
                # slot). Surface these as clean QueryErrors, not unhandled 500s.
                raise QueryError(str(exc)) from exc
        cf, ct = win
        entry = {
            "label": label,
            "window": {"from": cf, "to": ct},
            "sql": sql,
            "params": [_jsonable(p) for p in params],   # JSON-safe (display)
            "_exec_params": params,                      # raw (tuples preserved) for execution
            "preview": gq.inline_preview(sql, params),
        }
        if label in side_of_label:
            entry["comparison_side"] = side_of_label[label]
        results.append(entry)

    out = {
        "kpi": kpi,
        "title": config.get("title"),
        "module": config.get("module"),
        "execution_mode": config.get("execution_mode"),
        "mode": mode,
        "dimension": eff_dim,
        "grain": eff_grain,
        "filters": filter_by or None,
        "resolved_from_phrase": resolved_from_phrase,
        "comparison": comparison,
        "results": results,
        "citations": [_kpi_citation(config, kpi)],
    }
    if dimension_note:                        # a requested breakdown couldn't be honoured
        out["dropped_dim"] = dim
        out["dimension_note"] = dimension_note
    if mode_note:
        out["mode_note"] = mode_note
    if schema_fallback_cols:
        out["schema_fallback_columns"] = schema_fallback_cols
    if cmp_pair:
        out["comparison_windows"] = resolved_from_phrase["comparison"]
    log.info("generate_query kpi=%s mode=%s grain=%s window=%s..%s dim=%s -> %d result(s)%s%s",
             kpi, mode, eff_grain, frm, to, eff_dim, len(results),
             " [dim dropped]" if dimension_note else "",
             " [period comparison]" if cmp_pair else "")
    return out


def _schema_drift_hint(exc_msg: str, fallback_columns: List[str]) -> Optional[str]:
    """When a DB error is an undefined-column failure on a column that was only
    reachable through the schema fallback (not the KPI's curated config), say so
    plainly instead of leaving the caller to interpret a raw driver message.

    ``schema_v3.yaml`` is maintained by hand and can drift ahead of the live
    database (a column declared there was renamed/dropped in reality); the
    fallback that lets ``dim=`` / ``filters=`` reach schema-only columns
    (:func:`resolve_dim_via_schema`) has no way to verify the column actually
    exists live before the query runs, so this is the first point such drift can
    be caught."""
    if "does not exist" not in exc_msg:
        return None
    for col in fallback_columns:
        if f'"{col}"' in exc_msg:
            return (f"{col!r} is declared on this KPI's table in schema_v3.yaml but "
                    f"doesn't actually exist in the live database (schema drift) -- "
                    f"retry without it, or use one of this KPI's curated fields/filters.")
    return None


async def run_query(
    kpi: str,
    period: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    as_of: Optional[str] = None,
    mode: str = "stat",
    dim: Union[str, List[str], None] = None,
    grain: Optional[str] = None,
    filters: Union[Dict[str, Any], List[str], None] = None,
    comparison: bool = False,
    limit: int = 200,
) -> Dict[str, Any]:
    """Generate SQL for a KPI *and execute it*, returning result rows.

    Same request shape as :func:`generate_query`; each result additionally
    carries ``columns`` / ``rows`` / ``rowcount`` (or an ``error`` string if
    execution failed — e.g. the connection is not configured). The generated
    ``sql`` / ``preview`` are kept for transparency.
    """
    out = await generate_query(kpi, period=period, from_date=from_date, to_date=to_date,
                               as_of=as_of, mode=mode, dim=dim, grain=grain,
                               filters=filters, comparison=comparison)

    source = ((await get_catalog().get(kpi)) or {}).get("source") or {}
    dialect = source.get("dialect", "postgres")
    connection = source.get("connection")
    out["source"] = {"connection": connection, "dialect": dialect,
                     "schema": source.get("schema")}

    for res in out["results"]:
        # Execute with the RAW params (tuples preserved so IN %s expands to IN (...)),
        # not the JSON-ified display params (which would turn tuples into arrays).
        exec_params = res.pop("_exec_params", res["params"])
        try:
            exec_out = await db.execute(dialect, connection, res["sql"], exec_params,
                                        limit=limit)
            res.update(exec_out)
        except db.DBError as exc:
            log.warning("run_query execution error for %s: %s", kpi, exc)
            res["error"] = str(exc)
            res["error_type"] = type(exc).__name__
            drift = _schema_drift_hint(str(exc), out.get("schema_fallback_columns") or [])
            if drift:
                res["error"] += " -- " + drift
                res["schema_drift"] = True
    if out.get("comparison_windows"):
        summary = _comparison_summary(out)
        if summary:
            out["comparison_summary"] = summary
    log.info("run_query kpi=%s mode=%s -> executed %d window(s)", kpi, mode, len(out["results"]))
    return out


def _comparison_summary(out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Delta between the two sides of a period comparison, computed once here so
    every caller reports the SAME number instead of re-deriving it from the rows.

    Only for ``stat`` mode, where each side is exactly ONE scalar window. A series
    comparison (many buckets per side) and a dimensional breakdown are deliberately
    left alone: adding up buckets would be wrong for a percentage/average metric and
    picking one group would be wrong for a breakdown. Also ``None`` when either side
    errored or isn't numeric — better no delta than a delta against a missing half.
    """
    if out.get("mode") != "stat":
        return None
    windows = out.get("comparison_windows") or {}
    totals: Dict[str, float] = {}
    for side in ("previous", "current"):
        sides = [r for r in (out.get("results") or [])
                 if r.get("comparison_side") == side and not r.get("error")]
        if len(sides) != 1:
            return None
        value = _scalar_value(sides[0].get("rows") or [])
        try:
            totals[side] = float(value)
        except (TypeError, ValueError):
            return None

    prev, cur = totals["previous"], totals["current"]
    summary: Dict[str, Any] = {
        "previous": {**windows.get("previous", {}), "value": round(prev, 4)},
        "current": {**windows.get("current", {}), "value": round(cur, 4)},
        "delta": round(cur - prev, 4),
    }
    if prev:
        summary["pct_change"] = round((cur - prev) / abs(prev) * 100, 2)
    summary["direction"] = ("up" if cur > prev else "down" if cur < prev else "flat")
    return summary


# ---------------------------------------------------------------------------
# Module overview  — a one-call rollup of a module's KPIs for a filter/period
# ---------------------------------------------------------------------------
# Module codes and their human labels are NOT listed here: they differ per
# deployment (one site's configs use "cm", another's use "changes") and are
# derived from the configured index by cora_mcp.module_registry.


def _scalar_value(rows: List[Dict[str, Any]]) -> Any:
    """Pull the single metric value out of a stat result's rows."""
    if not rows:
        return None
    row = rows[0]
    # prefer a column literally named 'v'/'value', else the first column.
    for key in ("v", "value"):
        if key in row:
            return row[key]
    return next(iter(row.values()), None)


def _dim_label(v: Any) -> Any:
    """Unwrap a single-element array dim value (text[] columns come back as lists)."""
    if isinstance(v, (list, tuple)):
        return v[0] if len(v) == 1 else list(v)
    return v


def _breakdown_rows(dim_names: List[str], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn table-mode rows ({grp, grp2, …, v}) into labelled breakdown records:
    one {<dim>: value, …, "value": <measure>} per group."""
    grp_cols = {"grp"} | {"grp%d" % (i + 1) for i in range(len(dim_names))}
    out: List[Dict[str, Any]] = []
    for r in rows:
        item: Dict[str, Any] = {}
        for i, dn in enumerate(dim_names):
            col = "grp" if i == 0 else "grp%d" % (i + 1)
            item[dn] = _dim_label(r.get(col))
        # measure value: prefer v/value, else the first non-group column
        if "v" in r:
            item["value"] = r["v"]
        elif "value" in r:
            item["value"] = r["value"]
        else:
            item["value"] = next((v for k, v in r.items() if k not in grp_cols), None)
        out.append(item)
    return out


def _status_for(config: dict, value: Any) -> Optional[str]:
    """RAG level for a value against the config's status bands (absolute mode)."""
    status = config.get("status") or {}
    bands = status.get("bands") or []
    if value is None or not bands:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    higher_better = status.get("direction", "higher_better") == "higher_better"
    # bands are ordered best->worst with a 'min' threshold; last is the floor.
    for band in bands:
        if "min" not in band:
            return band.get("level")            # catch-all (e.g. red)
        if higher_better and v >= band["min"]:
            return band.get("level")
        if not higher_better and v <= band["min"]:
            return band.get("level")
    return bands[-1].get("level")


async def module_overview(
    module: str,
    period: Optional[str] = None,
    filters: Union[Dict[str, Any], List[str], None] = None,
    dim: Union[str, List[str], None] = None,
    limit_kpis: Optional[int] = None,
) -> Dict[str, Any]:
    """Run every KPI in a module for one period/filter and return a rollup.

    For each KPI it executes the stat value (with prior-window comparison) and
    reports value, unit, delta vs the comparison window, target and RAG status.
    Filters are applied per KPI: a requested filter is passed only to KPIs whose
    ``filters.allowed`` includes it (others are recorded under
    ``dropped_filters`` for that KPI, never silently mis-applied).

    When ``dim`` is given (a field/word or a list — e.g. "business",
    ["business", "region"]) each KPI additionally returns a ``breakdown`` grouped
    by that dimension, on top of the overall value. The dimension word is resolved
    per KPI (``business`` -> ``business_name``); a KPI that cannot be grouped by it
    (an unknown dimension, or a SQL-mode KPI with no generic GROUP BY) records the
    request under ``dropped_dim`` and keeps just its scalar value.
    """
    catalog = get_catalog()
    code = await module_registry.resolve_code(module)
    if not code:
        raise QueryError(
            f"unknown module {module!r}. known: {await module_registry.known_codes()}")

    requested = _normalize_filters(filters)
    want_dims = [dim] if isinstance(dim, str) else list(dim or [])
    names = await catalog.by_module(code)
    if limit_kpis:
        names = names[:limit_kpis]

    async def _one(name: str) -> Optional[Dict[str, Any]]:
        cfg = await catalog.get(name)
        # A module also indexes dashboard "widget" docs (chart configs meant for a
        # UI drilldown, not a standalone governed metric) alongside real "kpi" docs.
        # The overview/health rollup only makes sense over the latter — running a
        # widget's query here would execute it as if it were its own metric and
        # report a bogus value/status for something that was never meant to stand
        # alone. Configs with no ``type`` at all predate this field; treat them as
        # KPIs rather than silently dropping them.
        if cfg and cfg.get("type") not in (None, "kpi"):
            return None
        # Resolve each requested term (alias or canonical) against THIS KPI; apply
        # the ones it supports, record the rest as dropped (never mis-applied).
        applied: Dict[str, Any] = {}
        dropped: List[str] = []
        for term, value in requested.items():
            canonical = try_resolve_filter_key(cfg, term)
            if canonical:
                applied[canonical] = value
            else:
                dropped.append(term)
        entry: Dict[str, Any] = {
            "kpi": name, "title": cfg.get("title"), "unit": cfg.get("unit"),
            "applied_filters": applied or None, "dropped_filters": dropped or None,
            "citations": [_kpi_citation(cfg, name)],
        }
        try:
            out = await run_query(name, period=period, filters=applied or None,
                                  mode="stat", comparison=True, limit=1)
            res = out.get("results") or []
            cur = res[0] if res else {}
            if cur.get("error"):
                entry["error"] = cur["error"]
            else:
                value = _scalar_value(cur.get("rows") or [])
                entry["value"] = value
                entry["window"] = cur.get("window")
                entry["status"] = _status_for(cfg, value)
                tgt = cfg.get("target") or {}
                if tgt:
                    entry["target"] = tgt.get("value")
                if len(res) > 1 and not res[1].get("error"):
                    prev = _scalar_value(res[1].get("rows") or [])
                    entry["previous"] = prev
                    # The comparison basis isn't always "last month" -- an
                    # explicit window (like "current month") compares against
                    # the SAME calendar dates a year ago, not the prior month,
                    # so both windows are surfaced with their own label
                    # (e.g. "previous-year window") rather than leaving the
                    # caller to guess what "previous" was measured against.
                    entry["previous_window"] = res[1].get("window")
                    entry["previous_label"] = res[1].get("label")
                    try:
                        entry["delta"] = round(float(value) - float(prev), 4)
                    except (TypeError, ValueError):
                        entry["delta"] = None
        except QueryError as exc:
            entry["error"] = str(exc)

        # optional per-KPI breakdown by the requested dimension(s)
        if want_dims and "error" not in entry:
            await _attach_breakdown(name, cfg, entry, want_dims, applied)
        return entry

    def _drop_dim(entry, want_dims, err=None):
        entry["dropped_dim"] = want_dims if len(want_dims) > 1 else want_dims[0]
        if err:
            entry["breakdown_error"] = err

    async def _attach_breakdown(name, cfg, entry, want_dims, applied):
        resolved: List[str] = []
        for w in want_dims:
            rd = resolve_dim_word(cfg, w)
            if rd is None:                       # this KPI can't group by that word
                _drop_dim(entry, want_dims,
                          f"KPI {name!r} has no dimension matching {w!r}; "
                          f"the requested breakdown was not applied.")
                return
            resolved.append(rd)

        # Try the generic GROUP BY path first. This now works for BOTH execution
        # families: DSL always could, and a SQL-mode KPI that declares
        # `allowed_group_by` for this dimension can too (gen_query.driver_substitute
        # rewrites its authored query to group by it). Unlike the authored {dim}
        # breakdown fallback below, this path also still applies the requested
        # filters, so it's always preferred when it succeeds.
        try:
            out = await run_query(name, period=period, filters=applied or None,
                                  mode="table", dim=resolved, limit=100)
            res = out.get("results") or []
            cur = res[0] if res else {}
            if cur.get("error"):
                raise QueryError(cur["error"])
            entry["dimension"] = resolved if len(resolved) > 1 else resolved[0]
            entry["breakdown"] = _breakdown_rows(resolved, cur.get("rows") or [])
            return
        except QueryError as exc:
            generic_reason = str(exc)

        if cfg.get("execution_mode") == "DSL" or applied:
            # A DSL rejection is final (there's no second grouping mechanism for
            # it). A SQL-mode rejection WITH filters requested is also final here:
            # the fallback breakdown query below can't apply filters either, so
            # falling back would silently answer a different (unfiltered) question.
            _drop_dim(entry, want_dims, generic_reason)
            return
        await _sql_mode_breakdown(cfg, entry, want_dims, resolved, generic_reason)

    async def _sql_mode_breakdown(cfg, entry, want_dims, resolved, generic_reason):
        dims_allowed = config_dimensions(cfg)
        win = entry.get("window") or {}
        # The authored template exposes a single {dim} and no filter slot. We fold one
        # OR MORE columns into a composite {dim} key (each must be a real column on the
        # KPI's primary table, since the template groups by `a.{dim}`), but per-KPI
        # filters can't be injected and we need a concrete window. Whenever we can't
        # honour the request we record a PRECISE reason (surfaced) — never a silent drop.
        schema_cols = set(schema_columns_for(cfg))
        allowed_set = set(dims_allowed)
        bad = [c for c in resolved
               if c not in allowed_set and _dim_to_column(cfg, c) not in schema_cols]
        inner_tmpl = _breakdown_inner_sql(cfg, resolved)
        reason = None
        if bad:
            reason = (f"KPI {cfg.get('name')!r} cannot break down by {bad!r}; "
                      f"allowed breakdown dimensions: {dims_allowed}.")
        elif inner_tmpl is None:
            # No authored fallback query either — surface the generic (GROUP BY)
            # rejection reason, which names the KPI's actual allowed_group_by list.
            reason = generic_reason
        elif not (win.get("from") and win.get("to")):
            reason = (f"no resolved date window for the {cfg.get('name')!r} breakdown.")
        if reason:
            _drop_dim(entry, want_dims, reason)
            return
        # Snapshot-style breakdown templates use {as_of} (the window end) rather
        # than a from/to range — anchor it on the window end like driver_substitute.
        payload = {"from_date": win["from"], "to_date": win["to"], "as_of": win["to"]}
        inner = gq.substitute_dates(inner_tmpl, payload)
        if "{" in inner:                  # an unfilled placeholder -> don't execute
            _drop_dim(entry, want_dims,
                      f"unfilled placeholder in the {cfg.get('name')!r} breakdown query")
            return
        sql = "SELECT %s, _b.v AS v FROM (%s) _b" % (_grp_select(resolved), inner)
        source = cfg.get("source") or {}
        try:
            exec_out = await db.execute(source.get("dialect", "postgres"),
                                        source.get("connection"), sql, [], limit=100)
            entry["dimension"] = resolved if len(resolved) > 1 else resolved[0]
            entry["breakdown"] = _breakdown_rows(resolved, exec_out.get("rows") or [])
        except db.DBError as exc:
            _drop_dim(entry, want_dims, str(exc))

    # Run the module's KPIs concurrently (the asyncpg pool bounds real parallelism).
    import asyncio
    metrics: List[Dict[str, Any]] = [m for m in await asyncio.gather(*(_one(n) for n in names))
                                     if m is not None]

    resolved = None
    if period:
        r = resolve_dates(period)
        resolved = {"phrase": period, "matched": r["matched"],
                    "start_date": r["start_date"], "end_date": r["end_date"]}
    log.info("module_overview module=%s code=%s kpis=%d filters=%s",
             module, code, len(metrics), requested or None)
    known = await module_registry.get_modules()
    info = known.get(code)
    seen_kpis = set()
    citations: List[Dict[str, Any]] = []
    for m in metrics:
        for c in m.get("citations") or []:
            if c.get("kpi") not in seen_kpis:
                seen_kpis.add(c.get("kpi"))
                citations.append(c)
    return {
        "module": code,
        "module_label": info.label if info else code,
        "period": resolved,
        "requested_filters": requested or None,
        "kpi_count": len(metrics),
        "metrics": metrics,
        "citations": citations,
    }


async def run_dataset_query(spec: Union[Dict[str, Any], "object"],
                            connection: str = "vtx5", dialect: str = "postgres",
                            limit: int = 200) -> Dict[str, Any]:
    """Build a query from a structured QuerySpec (schema-validated) and execute it.

    Handles ad-hoc single-table queries, cross-entity joins (spec.join_with) and
    drill-down (spec.drilldown). If ``spec.metric`` is set, the metric anchors the
    base table + measure expression + date field (from its config), while
    dimensions/filters may use any schema column of that table.
    """
    from cora_mcp import sql_builder
    from cora_mcp.kpi_catalog import get_catalog

    spec = sql_builder._norm_spec(spec)

    # Metric anchor: pull base table / measure expression / date field from config.
    if spec.metric:
        cfg = await get_catalog().get(spec.metric)
        if not cfg:
            raise QueryError(f"unknown metric {spec.metric!r}")
        pd = cfg.get("primary_dataset") or {}
        base_fqn = f"{pd.get('schema')}.{pd.get('table')}"
        if not spec.base:
            spec.base = base_fqn
        dsl = cfg.get("dsl") or {}
        measures = dsl.get("measures") or []
        wants_detail_only = bool(spec.drilldown and spec.drilldown.detail_columns)
        if measures and spec.measure is None:
            spec.measure = sql_builder.Measure(expression=measures[0]["expression"],
                                               alias=measures[0].get("alias", "value"))
        elif not measures and spec.measure is None and not wants_detail_only:
            # SQL-mode KPI: its metric formula lives in raw base_query, not in a
            # decomposable measure, so we CANNOT reuse it here. Refuse rather than
            # silently returning count(*) mislabeled as the metric. This does NOT
            # apply to a pure detail listing (drilldown/select, no aggregation) --
            # that never needed a measure in the first place, only the anchored
            # base table + date field, both of which come from config either way.
            raise QueryError(
                f"metric {spec.metric!r} is a SQL-mode KPI whose formula can't be "
                f"reused by query_dataset. Either call run_kpi/generate_query with "
                f"its allowed filters, or pass an explicit `measure` here to define "
                f"what to aggregate.")
        if not spec.date_field:
            spec.date_field = (cfg.get("time") or {}).get("column")

    built = sql_builder.build(spec)
    citations: List[Dict[str, Any]] = []
    if spec.metric:
        citations.append(_kpi_citation(cfg, spec.metric))
    else:
        tables = [built.base_table, *(built.joined_tables or [])]
        citations = [{"dataset": t, "connection": connection, "dialect": dialect}
                     for t in tables if t]
    result: Dict[str, Any] = {
        "base_table": built.base_table,
        "joined_tables": built.joined_tables,
        "date_window": built.date_window,
        "sql": built.sql,
        "params": [_jsonable(p) for p in built.params],
        "preview": gq.inline_preview(built.sql, built.params),
        "citations": citations,
    }
    if built.implicit_grain:
        # A comparison period grouped the rows by period even though the caller
        # didn't pass a grain — say so instead of returning unexplained buckets.
        result["implicit_grain"] = built.implicit_grain
        result["grouping_note"] = (
            "the period is a comparison, so rows are grouped per %s (one row per "
            "compared period) — see date_window.comparison for both windows."
            % built.implicit_grain)
    if built.dropped_dimensions:
        # Dimensions the schema didn't recognise were dropped; the query ran
        # ungrouped. Surface it so the answer can say so instead of pretending.
        from cora_mcp.column_resolver import resolvable_words
        result["dropped_dimensions"] = built.dropped_dimensions
        result["dropped_dimensions_note"] = (
            "%s could not be resolved to a column on %s, so the rows are NOT broken "
            "down by %s — do not describe the result as if they were. Available "
            "breakdown words: %s"
            % (built.dropped_dimensions, built.base_table,
               " / ".join(built.dropped_dimensions),
               resolvable_words(built.base_table)[:30]))
    if built.resolved_columns:
        # A word that named no column literally but resolved through the schema's
        # declared vocabulary. Reported so the answer names the column that ran.
        result["resolved_columns"] = built.resolved_columns
    try:
        exec_out = await db.execute(dialect, connection, built.sql, built.params, limit=limit)
        result.update(exec_out)
    except db.DBError as exc:
        log.warning("run_dataset_query execution error: %s", exc)
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
        fallback_cols = [info["column"] for info in (built.resolved_columns or {}).values()]
        drift = _schema_drift_hint(str(exc), fallback_cols)
        if drift:
            result["error"] += " -- " + drift
            result["schema_drift"] = True

    # Empty but valid? Diagnose WHY so the answer is useful, not a dead "no records".
    if not result.get("error") and result.get("rowcount") == 0:
        diag = await _diagnose_empty(spec, connection, dialect)
        if diag:
            result["diagnostics"] = diag

    log.info("run_dataset_query base=%s joins=%s -> %s", built.base_table,
             built.joined_tables, "error" if result.get("error") else f"{result.get('rowcount')} rows")
    return result


async def _diagnose_empty(spec, connection: str, dialect: str) -> List[Dict[str, Any]]:
    """A valid query returned 0 rows — relax ONE restriction at a time and count,
    so the caller can explain the zero ("0 with both, but 44 majors: 44 with a
    problem, 0 with a change") instead of just saying nothing was found.

    Each variant becomes a plain count(*): detail/dimension/grain/order are dropped
    but any drill-down entity pin (a specific record id) is kept as a filter."""
    from cora_mcp import sql_builder

    def _count_spec(mutate) -> Optional["sql_builder.QuerySpec"]:
        s = spec.model_copy(deep=True)
        s.measure = None            # -> count(*)
        s.dimensions = []
        s.grain = None
        s.order_by = None
        if s.drilldown:             # keep the record pin, drop the detail SELECT list
            s.drilldown.detail_columns = []
        mutate(s)
        return s

    variants: List[tuple] = []
    if spec.join_with:
        for jw in spec.join_with:   # keep ONLY this one link
            variants.append((f"only linked via {jw}",
                             _count_spec(lambda s, jw=jw: setattr(s, "join_with", [jw]))))
        variants.append(("base rows, no links required",
                         _count_spec(lambda s: setattr(s, "join_with", []))))
    if spec.period:
        variants.append(("same query, any time (no period)",
                         _count_spec(lambda s: setattr(s, "period", None))))

    out: List[Dict[str, Any]] = []
    for label, vspec in variants[:6]:               # bounded
        if vspec is None:
            continue
        try:
            built = sql_builder.build(vspec)
            r = await db.execute(dialect, connection, built.sql, built.params, limit=1)
        except (sql_builder.BuilderError, db.DBError, Exception):
            continue                                 # a variant that can't build is just skipped
        rows = r.get("rows") or []
        count = next(iter(rows[0].values()), None) if rows else 0
        out.append({"relaxed": label, "count": count})
    log.info("diagnose_empty -> %s", out)
    return out


# ---------------------------------------------------------------------------
# Record detail  — a specific record's own columns + its linked records.
# ---------------------------------------------------------------------------
def _primary_table_to_slug() -> Dict[str, str]:
    """Map each entity's PRIMARY table fqn -> its slug.

    Entities can share physical tables (they're analytical groupings), so a
    table can belong to several entities. Relationship endpoints are always an
    entity's primary table, so mapping by primary table resolves a linked target
    unambiguously; on the rare collision, prefer the entity whose slug matches the
    table's schema prefix."""
    from cora_mcp.schema_loader import get_loader
    out: Dict[str, str] = {}
    for _mod, slug, entity in get_loader().all_entities():
        tables = entity.get("tables") or []
        if not tables or not tables[0].get("name"):
            continue
        primary = tables[0]["name"]
        schema = primary.split(".", 1)[0]
        if primary in out and out[primary] != slug:
            # keep whichever slug matches the schema prefix
            if out[primary] == schema:
                continue
        out[primary] = slug
    return out


def _reachable_related(base_slug: str) -> List[Dict[str, str]]:
    """Entities reachable from `base_slug` in one relationship hop.

    Returns [{entity, relationship, target_table}] — deterministic, driven by the
    schema's declared relationships (so new links self-register)."""
    from cora_mcp.relationships import get_graph
    from cora_mcp.schema_loader import get_loader
    loader = get_loader()
    base_primary = loader.entity_primary_table(base_slug)
    if not base_primary:
        return []
    t2slug = _primary_table_to_slug()
    out: List[Dict[str, str]] = []
    seen = set()
    for rel in get_graph().relationships():
        left, right = rel.get("left"), rel.get("right")
        if base_primary == left:
            target = right
        elif base_primary == right:
            target = left
        else:
            continue
        tslug = t2slug.get(target)
        if not tslug or tslug == base_slug or tslug in seen:
            continue
        seen.add(tslug)
        out.append({"entity": tslug, "relationship": rel.get("name"), "target_table": target})
    return out


async def get_record_detail(
    record_id: str,
    entity: Optional[str] = None,
    related: Union[List[str], str, None] = "all",
    limit: int = 50,
) -> Dict[str, Any]:
    """Fetch a specific record's own detail columns and (optionally) the records
    linked to it — NEVER a count/metric.

    * ``record_id`` — e.g. ``"INC0353896"``. Its prefix picks the entity + human
      id column via ``record_prefixes.json`` (override with ``entity``).
    * ``related`` — ``"all"`` (default: every entity reachable in one relationship
      hop), a list of entity names/slugs (e.g. ``["change", "problem"]``), or
      ``"none"``/``[]`` for just the record itself.

    Each sub-query is built deterministically (schema-validated) and executed; the
    return exposes them under ``results`` (label + sql + preview + rows) so the
    same transparency/summary path as run_kpi renders them.
    """
    from cora_mcp import record_lookup as rl
    from cora_mcp.schema_loader import get_loader

    rid = (record_id or "").strip().upper()
    m = rl._RECORD_RE.match(rid)
    prefix = m.group(1) if m else None
    slug, id_col = rl.resolve_entity(entity=entity, prefix=prefix)
    if not id_col:
        raise QueryError(f"could not determine a human id column for entity {slug!r}")

    loader = get_loader()
    base_primary = loader.entity_primary_table(slug)

    results: List[Dict[str, Any]] = []

    # 1) the record's own detail row(s)
    cols = rl.curated_detail_columns(slug, id_col, table=base_primary)
    detail_spec = {
        "base": slug,
        "drilldown": {
            "detail_columns": cols,
            "detail_table": base_primary,
            "entity_filter": {"field": id_col, "op": "=", "values": [rid]},
        },
        "limit": limit,
    }
    detail_out = await run_dataset_query(detail_spec, limit=limit)
    detail_out["label"] = f"{slug} detail"
    detail_out["relationship"] = None
    results.append(detail_out)
    found = bool(detail_out.get("rows"))

    # 2) linked records
    if related in (None, "all"):
        targets = _reachable_related(slug)
    elif isinstance(related, str) and related.lower() in ("none", ""):
        targets = []
    else:
        terms = [related] if isinstance(related, str) else list(related)
        reachable = {t["entity"]: t for t in _reachable_related(slug)}
        alias_to_slug = {term: rl.registry().entity_for_alias(term) or term for term in terms}
        targets = []
        for term, tslug in alias_to_slug.items():
            if tslug in reachable:
                targets.append(reachable[tslug])
            else:
                results.append({"label": f"linked {term}", "relationship": None,
                                "entity": tslug,
                                "error": f"no declared relationship from {slug!r} to "
                                         f"{tslug!r}; reachable: {sorted(reachable)}"})

    for tgt in targets:
        tslug = tgt["entity"]
        tprimary = loader.entity_primary_table(tslug)
        # Declared id column first: the bare heuristic returns change_id for
        # itsm_release and first_task_id for itsm_service_request, so a linked-record
        # listing showed the wrong identifier for those entities.
        tid = rl._entity_id_column(tslug)
        tcols = rl.curated_detail_columns(tslug, tid, table=tprimary)
        spec = {
            "base": slug,
            "join_with": [tslug],
            "join_type": "inner",
            "drilldown": {
                "detail_columns": tcols,
                "detail_table": tprimary,
                "entity_filter": {"field": id_col, "op": "=", "values": [rid]},
            },
            "limit": limit,
        }
        try:
            sub = await run_dataset_query(spec, limit=limit)
        except (QueryError, Exception) as exc:  # keep other links even if one fails
            from cora_mcp.sql_builder import BuilderError
            if not isinstance(exc, (QueryError, BuilderError)):
                raise
            sub = {"error": str(exc)}
        sub["label"] = f"linked {tslug} ({tgt.get('relationship')})"
        sub["relationship"] = tgt.get("relationship")
        sub["entity"] = tslug
        results.append(sub)

    log.info("get_record_detail id=%s entity=%s found=%s links=%d",
             rid, slug, found, len(results) - 1)
    return {
        "tool": "get_record",
        "record_id": rid,
        "entity": slug,
        "id_column": id_col,
        "found": found,
        "results": results,
    }
