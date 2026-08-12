"""Ad-hoc query planner: ``plan_query`` (discovery) + ``preflight`` (validation).

These two helpers turn a free-form question that no governed KPI answers into a
*correct* ``QuerySpec`` for the existing ``query_dataset`` path — without either
of them writing SQL. They exist because the hard part of an ad-hoc / cross-entity
question is not building SQL (``sql_builder`` already does that safely) but making
four decisions correctly:

  1. which entity is the FILTER subject vs. the PROJECTION subject (base vs. join)
  2. which column is the status / date (``status_name`` vs ``state``; open vs closed)
  3. what the real filter VALUE is ('completed' -> 'CLOSED')  [value_resolver]
  4. is it actually a metric in disguise ("how many …") -> route to run_kpi

``plan_query`` hands the model a compact, real-name schema context (columns by
role, value domains, and the relationships between the candidate entities) so it
can build a spec against names that exist. ``preflight`` then checks that spec
against the schema BEFORE anything executes, returning *all* problems at once with
actionable messages (unknown column -> suggestions, undeclared join -> reachable
targets, bad value -> valid list, wrong date role, 1:N fan-out risk).

Neither touches the existing tools: ``query_dataset`` / ``sql_builder`` / ``db``
still enforce every guard at build+exec time; this is a friendlier front door.
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Tuple

from cora_mcp import value_resolver
from cora_mcp.kpi_catalog import get_catalog
from cora_mcp.logging_config import get_logger
from cora_mcp.relationships import get_graph
from cora_mcp.schema_loader import get_loader

log = get_logger(__name__)

# words that mean "this is an aggregate/metric, not a row list"
_METRIC_WORDS = ("how many", "number of", "count of", "count ", "total ", "sum of",
                 "average", "avg ", "mean ", "percent", "percentage", "rate of",
                 "ratio", "trend")

# verb/phrase -> the timestamp role a question likely means, as guidance only.
_DATE_HINTS = {
    "completed": "closed/completed timestamp (e.g. closed_date_time), NOT the open date",
    "closed": "closed/completed timestamp (e.g. closed_date_time)",
    "resolved": "resolution timestamp (e.g. resolved_date_time / closed_date_time)",
    "created": "creation timestamp (e.g. created_date_time / open_date_time)",
    "opened": "open/created timestamp (e.g. open_date_time)",
    "raised": "open/created timestamp (e.g. open_date_time)",
}


def _norm(s: Any) -> str:
    return value_resolver.normalize(s)


# ---------------------------------------------------------------------------
# plan_query — discovery
# ---------------------------------------------------------------------------
def _alias_to_slug() -> Dict[str, str]:
    """Every entity alias phrase -> entity slug (derived from schema_v3.yaml entity
    names, plus each entity's declared `aliases` overlay)."""
    from cora_mcp.record_lookup import registry
    return dict(registry()._entity_aliases)   # alias(lower) -> slug


def _identify_entities(question: str, module: Optional[str],
                       max_entities: int) -> List[Dict[str, Any]]:
    """Rank candidate entities a question is about. Longest alias phrase that
    occurs in the question wins; a module hint boosts its entities."""
    nq = _norm(question)
    loader = get_loader()
    valid_slugs = {slug for _m, slug, _e in loader.all_entities()}
    scores: Dict[str, Dict[str, Any]] = {}

    # entity-alias phrase matches (longest phrase first so 'service request' beats 'request')
    for alias, slug in sorted(_alias_to_slug().items(), key=lambda kv: -len(kv[0])):
        if slug not in valid_slugs:
            continue
        if f" {alias} " in f" {nq} ":
            s = scores.setdefault(slug, {"slug": slug, "score": 0, "why": []})
            s["score"] += 5 + len(alias.split())        # multiword hits score higher
            s["why"].append(f"matched '{alias}'")

    # module hint boosts its entities
    if module:
        # Sync context and this is only a +3 scoring hint, so use the cached
        # registry snapshot: a cold cache yields None and simply skips the boost.
        from cora_mcp.module_registry import resolve_code_sync
        code = resolve_code_sync(module)
        for _m, slug, entity in loader.all_entities():
            if code and (slug.startswith(f"itsm_{code}") or (entity.get("name") or "").lower().startswith(code)):
                s = scores.setdefault(slug, {"slug": slug, "score": 0, "why": []})
                s["score"] += 3
                s["why"].append(f"module hint '{module}'")

    ranked = sorted(scores.values(), key=lambda x: -x["score"])
    return ranked[:max_entities]


def _entity_context(slug: str, max_plain_dims: int = 40) -> Optional[Dict[str, Any]]:
    """Compact, model-ready description of one entity: dimensions (with value
    domains where declared), timestamps, identifiers, a few measures, tables."""
    loader = get_loader()
    detail = loader.entity_detail(slug)
    if not detail:
        return None
    pv = detail.get("possible_values") or {}
    dims_with_domain = [{"name": n, "possible_values": pv[n]} for n in detail.get("dimensions", []) if n in pv]
    plain_dims = [n for n in detail.get("dimensions", []) if n not in pv]
    return {
        "slug": slug,
        "entity": detail.get("entity"),
        "primary_table": loader.entity_primary_table(slug),
        "tables": [t.get("name") for t in detail.get("tables", [])],
        "dimensions_with_domain": dims_with_domain,     # the enum columns — pick values from here
        "dimensions": plain_dims[:max_plain_dims],
        "timestamps": detail.get("timestamps", []),
        "identifiers": detail.get("identifiers", []),
        "measures": detail.get("measures", [])[:8],
    }


def _relationships_among(slugs: List[str]) -> Tuple[List[dict], Dict[str, List[dict]]]:
    """Declared relationships touching the candidate entities, plus a per-entity
    one-hop reachability map (so cross-entity joins are discoverable)."""
    from cora_mcp.query_engine import _reachable_related
    graph = get_graph()
    loader = get_loader()
    tables = {loader.entity_primary_table(s) for s in slugs if loader.entity_primary_table(s)}
    rels = []
    for r in graph.relationships():
        if r.get("left") in tables or r.get("right") in tables:
            rels.append({k: r.get(k) for k in ("name", "description", "left", "right", "via", "const")
                         if r.get(k) is not None})
    reachable = {s: _reachable_related(s) for s in slugs}
    return rels, reachable


async def plan_query(question: str, module: Optional[str] = None,
                     max_entities: int = 4) -> Dict[str, Any]:
    """Identify the entity(ies) a free-form question is about and return a compact
    schema context — columns by role, value domains, and the relationships between
    candidates — so a ``QuerySpec`` can be built against real names. This does NOT
    build or run SQL; feed its output back as the plan for ``query_dataset``.

    Use for ad-hoc / cross-entity questions no governed KPI answers. For a specific
    record (INC…/CHG…) use ``get_record``; for a metric/number use ``run_kpi``.
    """
    candidates = _identify_entities(question, module, max_entities)
    slugs = [c["slug"] for c in candidates]

    entities = {}
    for c in candidates:
        ctx = _entity_context(c["slug"])
        if ctx:
            entities[c["slug"]] = ctx

    rels, reachable = _relationships_among(slugs) if slugs else ([], {})

    # metric-in-disguise?
    nq = _norm(question)
    maybe_metric = None
    if any(w.strip() in nq for w in _METRIC_WORDS):
        hits = await get_catalog().search(question, limit=3)
        maybe_metric = {"looks_like_aggregate": True,
                        "suggested_kpis": [{"name": h["name"], "title": h.get("title")} for h in hits],
                        "note": "This reads like a count/aggregate — prefer run_kpi if a KPI fits."}

    # date-field guidance from the question's verb
    date_notes = [f"'{k}' -> {v}" for k, v in _DATE_HINTS.items() if k in nq]

    guidance = [
        "Build a QuerySpec for query_dataset — do NOT write raw SQL.",
        "base = the entity that carries the FILTER/DATE condition; join_with = the "
        "entity whose COLUMNS you must return. (e.g. 'change ids for releases "
        "completed' -> base=release, join_with=[change], project change columns.)",
        "Pick filter values ONLY from a column's possible_values (see "
        "dimensions_with_domain); the builder rejects values outside the domain.",
        "For a cross-entity join, use only pairs present in 'relationships' / "
        "'reachable'; there is no cross join.",
        "Run preflight(spec) before query_dataset to catch column/join/value/date "
        "issues with fixes.",
    ]
    if date_notes:
        guidance.append("Date-field hint(s): " + "; ".join(date_notes))

    result = {
        "question": question,
        "candidates": candidates,
        "entities": entities,
        "relationships": rels,
        "reachable": reachable,
        "maybe_metric": maybe_metric,
        "guidance": guidance,
    }
    log.info("plan_query q=%r -> candidates=%s metric=%s",
             question, slugs, bool(maybe_metric))
    return result


# ---------------------------------------------------------------------------
# preflight — validate a QuerySpec before build/execute
# ---------------------------------------------------------------------------
def _resolve_table(name: str) -> Optional[str]:
    """entity slug or schema.table -> table fqn (or None)."""
    loader = get_loader()
    if name and "." in name and loader.get_table(name):
        return name
    return loader.entity_primary_table(name)


def _scope_tables(spec) -> Tuple[List[str], List[str]]:
    """(scope table fqns, errors). base + each join target resolved to a table."""
    from cora_mcp.sql_builder import _norm_spec
    spec = _norm_spec(spec)
    errors: List[str] = []
    scope: List[str] = []
    base_fqn = _resolve_table(spec.base) if spec.base else None
    if not spec.base:
        errors.append("spec.base is required (an entity slug or schema.table).")
        return scope, errors
    if not base_fqn:
        loader = get_loader()
        known = sorted({s for _m, s, _e in loader.all_entities()})
        errors.append(f"unknown base {spec.base!r}. known entities: {known}")
        return scope, errors
    scope.append(base_fqn)
    for jw in spec.join_with or []:
        t = _resolve_table(jw)
        if not t:
            errors.append(f"unknown join target {jw!r}.")
            continue
        scope.append(t)
    return scope, errors


def _column_in_scope(col: str, scope: List[str]) -> Optional[str]:
    loader = get_loader()
    for fqn in scope:
        if loader.column_info(fqn, col):
            return fqn
    return None


def preflight(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a ``QuerySpec`` against the schema BEFORE building or executing it.

    Returns ``{ok, errors, warnings, notes, resolved}``. Collects ALL problems at
    once so the model can fix the spec in one pass:
      * unknown base / join target / column  -> with suggestions
      * a join with no declared relationship  -> with reachable targets
      * a filter value outside the column's domain -> with the valid list
      * a date_field that isn't a timestamp column -> warning
      * projection from one table across a 1:N join -> fan-out (DISTINCT) warning
    Does NOT run anything; ``query_dataset`` remains the executor.
    """
    from cora_mcp.sql_builder import _norm_spec, _OPS
    spec = _norm_spec(spec)
    loader = get_loader()
    errors: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []

    scope, scope_errs = _scope_tables(spec)
    errors.extend(scope_errs)

    # ---- joins: every requested target must be reachable ----
    if scope and spec.join_with:
        from cora_mcp.relationships import NoJoinPathError
        graph = get_graph()
        base_fqn = scope[0]
        for jw in spec.join_with:
            tgt = _resolve_table(jw)
            if not tgt:
                continue                       # already reported as unknown target
            if graph._bfs(base_fqn, tgt) is None:
                from cora_mcp.query_engine import _reachable_related
                reach = [r["entity"] for r in _reachable_related(spec.base)]
                errors.append(f"no declared relationship path from {spec.base!r} to "
                              f"{jw!r}. reachable from {spec.base!r}: {reach}")

    def _check_col(col: str, where: str) -> Optional[str]:
        if not col or "." in col:              # alias-qualified / raw: skip
            return None
        fqn = _column_in_scope(col, scope)
        if fqn:
            return fqn
        avail = loader.column_names_in(scope) if scope else []
        close = difflib.get_close_matches(col, avail, n=5, cutoff=0.6)
        elsewhere = loader.tables_with_column(col)
        hint = f" did you mean: {close}?" if close else ""
        if elsewhere and not close:
            hint = f" exists in {elsewhere} (not in your scope — add it to join_with)."
        errors.append(f"unknown column {col!r} ({where}).{hint}")
        return None

    # ---- columns referenced everywhere ----
    # A dimension the schema doesn't know is DROPPED at build time (the query runs
    # ungrouped), not fatal — so preflight flags it as a warning, not an error.
    for d in spec.dimensions or []:
        if d and "." not in d and not _column_in_scope(d, scope):
            avail = loader.column_names_in(scope) if scope else []
            close = difflib.get_close_matches(d, avail, n=5, cutoff=0.6)
            elsewhere = loader.tables_with_column(d)
            if close:
                hint = f" did you mean: {close}? (or it stays dropped)"
            elif elsewhere:
                hint = f" (exists in {elsewhere} — add it to join_with to keep it)"
            else:
                hint = ""
            warnings.append(f"dimension {d!r} not in scope; it will be dropped and "
                            f"the query will run without that breakdown.{hint}")
        else:
            _check_col(d, "dimensions")
    if spec.measure and spec.measure.column and spec.measure.column != "*":
        _check_col(spec.measure.column, "measure")
    if spec.date_field:
        fqn = _check_col(spec.date_field, "date_field")
        if fqn:
            ci = loader.column_info(fqn, spec.date_field) or {}
            if "timestamp" not in (ci.get("type") or "").lower() and ci.get("role") != "timestamp":
                warnings.append(f"date_field {spec.date_field!r} is not a timestamp column "
                                f"(type={ci.get('type')!r}); dates may not filter as expected.")
    for o in spec.order_by or []:
        if o.get("field"):
            _check_col(o["field"], "order_by")

    # ---- filters: columns + value domains ----
    all_filters = list(spec.filters or [])
    if spec.drilldown and spec.drilldown.entity_filter:
        all_filters.append(spec.drilldown.entity_filter)
        for c in spec.drilldown.detail_columns or []:
            _check_col(c, "drilldown.detail_columns")
    corrected: List[Dict[str, Any]] = []
    for flt in all_filters:
        if flt.op not in _OPS:
            errors.append(f"unsupported op {flt.op!r} on {flt.field!r}; allowed: {sorted(_OPS)}")
        fqn = _check_col(flt.field, "filter")
        if not fqn:
            continue
        ci = loader.column_info(fqn, flt.field) or {}
        pv = ci.get("possible_values")
        if pv and flt.op in ("=", "!=", "in", "not_in") and flt.values:
            resolved, rejects = value_resolver.resolve_values(flt.field, list(flt.values), pv)
            for rj in rejects:
                errors.append(f"value {rj.input!r} not valid for {flt.field!r}. "
                              f"valid values: {pv}")
            for raw, res in zip(flt.values, resolved):
                if _norm(raw) != _norm(res):
                    corrected.append({"field": flt.field, "from": raw, "to": res})

    # ---- date window needs a date field ----
    if spec.period or spec.grain:
        df = spec.date_field or (loader.table_time_field(scope[0]) if scope else None)
        if not df:
            errors.append("period/grain given but no date_field and the base table has "
                          "no default time column; pass date_field explicitly.")

    # ---- 1:N fan-out: projecting one table across a join can duplicate rows ----
    projected_cols = list(spec.dimensions or [])
    if spec.drilldown and spec.drilldown.detail_columns:
        projected_cols += spec.drilldown.detail_columns
    if scope and len(scope) > 1 and projected_cols and not spec.measure:
        proj_tables = {_column_in_scope(c, scope) for c in projected_cols if "." not in c}
        proj_tables.discard(None)
        if len(proj_tables) == 1:
            warnings.append("projecting columns from a single entity across a join may "
                            "duplicate rows (1:N fan-out); consider DISTINCT or a "
                            "count_distinct measure if you need unique rows.")

    # ---- metric in disguise ----
    if not spec.measure and not (spec.drilldown and spec.drilldown.detail_columns) \
            and not spec.dimensions:
        notes.append("no measure, dimensions or detail_columns -> defaults to count(*). "
                     "If you meant a metric, consider run_kpi.")

    if corrected:
        notes.append("values auto-corrected to the column domain: " +
                     "; ".join(f"{c['field']} {c['from']!r}->{c['to']!r}" for c in corrected))

    ok = not errors
    result = {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
        "resolved": {"base_table": scope[0] if scope else None,
                     "scope_tables": scope,
                     "corrected_values": corrected},
    }
    log.info("preflight base=%s -> ok=%s errors=%d warnings=%d",
             getattr(spec, "base", None), ok, len(errors), len(warnings))
    return result