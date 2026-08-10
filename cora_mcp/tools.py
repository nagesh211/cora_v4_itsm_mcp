"""Registers CORA tools on a FastMCP instance — one small, fixed surface.

  * **Discovery** — ``list_modules``, ``describe_module(name)``,
    ``describe_dataset(slug)`` walk the catalog: modules -> entities -> columns
    (grouped by role) / possible values / member tables / related KPIs.
  * **KPI actions** — ``search_kpis``, ``describe_kpi``, ``generate_query`` (SQL
    only), ``run_kpi`` (SQL + execute).
  * **Insights** — ``overview_module`` rolls up every KPI in a module for one
    period/filter (value, delta, target, RAG status).
  * **Ad-hoc / schema-driven** — ``query_dataset`` (+ ``list_relationships``) and
    the deterministic date helper ``resolve_dates``.

``describe_module`` / ``describe_dataset`` are parameterized rather than one
auto-generated tool per module / entity — that kept the surface from ballooning
to 46 tools (which hurt tool selection).

Every invocation is logged (tool name, args, elapsed ms) by the shared wrapper.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import time
from typing import Any, Dict, List, Optional, Union

from cora_mcp.adhoc import plan_query as _plan_query, preflight as _preflight
from cora_mcp.date_resolver import resolve_dates as _resolve_dates
from cora_mcp.kpi_catalog import get_catalog
from cora_mcp.logging_config import get_logger
from cora_mcp.module_registry import (detect_module, module_choices, refresh_modules,
                                      routing_mode)
from cora_mcp.query_engine import (
    QueryError,
    generate_query as _generate_query,
    get_record_detail as _get_record_detail,
    module_overview as _module_overview,
    run_dataset_query as _run_dataset_query,
    run_query as _run_query,
)
from cora_mcp.relationships import get_graph
from cora_mcp.schema_loader import get_loader
from cora_mcp.sql_builder import BuilderError

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Free-text SQL support (generate_sql / run_postgres_sql / resolve_filter_value)
#
# Every other tool in this module builds SQL deterministically from a
# validated spec (sql_builder.QuerySpec) or an authored KPI config
# (gen_query) — the LLM never writes SQL text itself. That's the safest
# design, but it can't express every shape: a single query that computes TWO
# independent metrics per dimension value in one row (e.g. "incidents created
# vs closed last month by vendor") needs conditional aggregation
# (`count(...) FILTER (WHERE ...)` / `count(case when ... end)`), which
# neither an authored KPI's fixed comparison-widget SQL nor
# sql_builder.QuerySpec (one measure per call) can express today. These three
# functions are the escape hatch for that shape — gated by cora_mcp.sql_guard
# (an AST-based read-only check, not the old string-prefix one) since this SQL
# is free text the LLM wrote, not something this module already validated.
# ---------------------------------------------------------------------------

# Business-rule row exclusions baked into some governed KPIs' authored SQL
# (see e.g. itsm/incidents-user-reported-incident-created-vs-closed.json)
# that a hand-written query does NOT get for free. Seeded from what's been
# found so far — NOT exhaustive; cross-check describe_kpi on a similarly
# named governed KPI for the entity before assuming none apply.
STANDARD_EXCLUSIONS: Dict[str, List[str]] = {
    "itsm_incident": [
        "status_name != 'CANCELED' -- exclude cancelled incidents unless the "
        "question is specifically about cancellations",
        "coalesce(contact_type, 'Channel is Empty') <> 'SYSTEM GENERATED' -- "
        "exclude system-generated tickets from 'user reported' style counts",
        "open_by_full_name != 'DNAC.INTEGRATION' -- exclude automation-opened "
        "tickets from 'user reported' style counts",
    ],
}


def _sql_presence_report(sql: str, dimensions: List[str], filters: List[str],
                         dialect: str = "postgres") -> Dict[str, Any]:
    """Check whether the dimension/filter WORDS the caller intended actually
    show up in the parsed SQL's GROUP BY / WHERE (+ JOIN ON) clauses.

    This is a text-containment heuristic over the parsed-and-reprinted SQL,
    not a full semantic proof: a dimension/filter expressed through a very
    differently-named physical column may show as "missing" even though it's
    genuinely applied (read the returned `sql` to confirm either way). It
    exists to catch the common, exact failure this whole investigation was
    about: a dimension or filter the caller MEANT to apply that never actually
    made it into the query text at all.
    """
    import sqlglot
    from sqlglot import exp
    tree = sqlglot.parse_one(sql, dialect=dialect)

    group_texts = [e.sql(dialect=dialect).lower()
                   for grp in tree.find_all(exp.Group) for e in grp.expressions]
    select_texts = [proj.sql(dialect=dialect).lower()
                    for sel in tree.find_all(exp.Select) for proj in sel.expressions]
    where_texts = [w.this.sql(dialect=dialect).lower() for w in tree.find_all(exp.Where)]
    join_texts = [j.sql(dialect=dialect).lower() for j in tree.find_all(exp.Join)]

    group_blob = " | ".join(group_texts)
    select_blob = " | ".join(select_texts)
    where_blob = " | ".join(where_texts + join_texts)

    def _norm(word: str) -> str:
        return word.strip().lower().replace(" ", "_")

    applied_dims, missing_dims, select_only_dims = [], [], []
    for d in dimensions:
        w = _norm(d)
        if w and w in group_blob:
            applied_dims.append(d)
        elif w and w in select_blob:
            select_only_dims.append(d)
        else:
            missing_dims.append(d)

    applied_filters, missing_filters = [], []
    for f in filters:
        w = _norm(f)
        (applied_filters if (w and w in where_blob) else missing_filters).append(f)

    return {
        "applied_dimensions": applied_dims,
        "dimensions_in_select_but_not_grouped": select_only_dims,
        "missing_dimensions": missing_dims,
        "applied_filters": applied_filters,
        "missing_filters": missing_filters,
        "has_group_by": bool(group_texts),
        "presence_check_note": (
            "text-containment heuristic over the parsed SQL, not a full semantic "
            "proof -- a 'missing' dimension/filter may still be applied under a "
            "very differently-named column; read `sql` to confirm."),
    }


def _log_call(name: str, **kv: Any):
    log.info("TOOL %s | %s", name, ", ".join(f"{k}={v!r}" for k, v in kv.items()))
    return time.perf_counter()


def _log_done(name: str, t0: float, summary: str = ""):
    log.info("TOOL %s done in %.1fms %s", name, (time.perf_counter() - t0) * 1000, summary)


# ---------------------------------------------------------------------------
# Per-turn dedup guard — collapse identical back-to-back tool calls.
#
# The analyst can loop, re-issuing the SAME (tool, args) call several times in one
# turn (esp. when a result was uninformative). This short-TTL memo returns the
# cached result for an exact repeat within CORA_DEDUP_TTL seconds and tags it
# `repeated_call`, so a thrash can't run the same SQL 6× — a deterministic backstop
# independent of the model's behaviour.
# ---------------------------------------------------------------------------
_CALL_CACHE: Dict[str, tuple] = {}
_CALL_TTL = float(os.getenv("CORA_DEDUP_TTL", "45"))


def _cache_key(name: str, args: tuple, kwargs: dict) -> Optional[str]:
    try:
        return name + "|" + json.dumps([args, kwargs], sort_keys=True, default=str)
    except Exception:
        return None


def _mark_repeat(result: Any) -> Any:
    if isinstance(result, dict):
        return {**result, "repeated_call": True}
    return result


def _dedup(fn, name: str):
    """Wrap a tool fn with the short-TTL dedup memo, preserving its signature so
    FastMCP still introspects the real parameters."""
    is_async = inspect.iscoroutinefunction(fn)

    if is_async:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = _cache_key(name, args, kwargs)
            now = time.monotonic()
            if key and key in _CALL_CACHE and now - _CALL_CACHE[key][0] < _CALL_TTL:
                log.info("TOOL %s | deduped repeat within %.0fs", name, _CALL_TTL)
                return _mark_repeat(_CALL_CACHE[key][1])
            res = await fn(*args, **kwargs)
            if key:
                _CALL_CACHE[key] = (now, res)
            return res
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = _cache_key(name, args, kwargs)
            now = time.monotonic()
            if key and key in _CALL_CACHE and now - _CALL_CACHE[key][0] < _CALL_TTL:
                log.info("TOOL %s | deduped repeat within %.0fs", name, _CALL_TTL)
                return _mark_repeat(_CALL_CACHE[key][1])
            res = fn(*args, **kwargs)
            if key:
                _CALL_CACHE[key] = (now, res)
            return res

    # Cache the real signature with annotations already EVALUATED (this module uses
    # `from __future__ import annotations`, so they're strings otherwise). FastMCP
    # builds its arg schema from this, and a string annotation would leave an
    # unresolved forward ref (e.g. Optional) that fails to build.
    try:
        wrapper.__signature__ = inspect.signature(fn, eval_str=True)
    except (ValueError, TypeError, NameError):   # pragma: no cover - defensive
        pass
    return wrapper


# ---------------------------------------------------------------------------
# Core action tools
# ---------------------------------------------------------------------------
def _register_core(mcp) -> List[str]:
    catalog = get_catalog()

    def resolve_dates(period: str) -> Dict[str, Any]:
        """Resolve a natural-language time phrase into an inclusive date window.

        Deterministic. Understands phrases like "last 3 months", "this month",
        "MTD/QTD/YTD/WTD" and their prior-period forms "PYTD/PMTD/PQTD" (= the
        same elapsed span in the previous period, e.g. prior-year-to-date),
        "today", "yesterday", "last/past N days", "N days ago", "last quarter",
        "next 6 months", "between 2025-06-10 and 2025-08-15", "Aug 2025",
        "Q3 2025", "H1 2025", "FY2024", "rest of this year". Fiscal year starts
        in April; weeks start Sunday.

        Also understands "current"/"previous" as synonyms of "this"/"last"
        ("current quarter", "previous week") and the common abbreviations
        ("last qtr", "3 mos").

        A two-sided COMPARISON phrase ("last quarter vs current quarter",
        "this month compared to last month") returns both windows under
        `comparison` = {previous: {...}, current: {...}}; start_date/end_date then
        describe the current (later) side. Pass such a phrase straight to
        run_kpi/generate_query as `period` — they run the metric for BOTH windows.

        Returns start_date / end_date (YYYY-MM-DD), a `matched` flag (False =>
        the phrase was NOT recognised and this fell back to month-to-date — tell
        the user the window was assumed), and the original phrase.
        """
        t0 = _log_call("resolve_dates", period=period)
        out = _resolve_dates(period)
        _log_done("resolve_dates", t0, f"-> {out['start_date']}..{out['end_date']}")
        return out

    def list_modules() -> List[Dict[str, Any]]:
        """List all catalog modules with their database type, coverage and
        entity summaries. Use this first to discover what data is available."""
        t0 = _log_call("list_modules")
        out = get_loader().all_module_summaries()
        _log_done("list_modules", t0, f"-> {len(out)} modules")
        return out

    async def list_kpi_modules() -> List[Dict[str, Any]]:
        """List the KPI module codes available in THIS deployment, with their
        human label, KPI count and accepted aliases. The codes differ per
        deployment — call this instead of assuming any fixed set. Pass a `code`
        to search_kpis(module=...) or overview_module(module=...)."""
        t0 = _log_call("list_kpi_modules")
        out = await module_choices()
        _log_done("list_kpi_modules", t0, f"-> {[m['code'] for m in out]}")
        return out

    async def refresh_kpi_modules() -> Dict[str, Any]:
        """Reload the module vocabulary from OpenSearch immediately, instead of
        waiting for the cache TTL. Use after indexing configs for a new module."""
        t0 = _log_call("refresh_kpi_modules")
        mods = await refresh_modules()
        out = {"modules": sorted(mods), "count": len(mods)}
        _log_done("refresh_kpi_modules", t0, f"-> {out['count']} module(s)")
        return out

    async def search_kpis(query: str, module: Optional[str] = None) -> List[Dict[str, Any]]:
        """Find KPI configs matching a natural-language question, ranked by
        relevance. Optionally restrict to a module code — the valid codes depend
        on the deployment, so get them from list_kpi_modules rather than
        guessing. Returns each KPI's name, title, unit, execution mode, allowed
        filters and drilldown dimensions. Feed the chosen `name` to
        generate_query."""
        t0 = _log_call("search_kpis", query=query, module=module)
        # When the caller didn't pin a module, infer one from the question to
        # sharpen ranking. Under the default CORA_MODULE_ROUTING=boost the guess
        # only re-ranks — it can never exclude a KPI, which matters while configs
        mode = routing_mode()
        detected = (await detect_module(query)
                    if (module is None and mode != "off") else None)
        boost_only = detected is not None and mode == "boost"
        if detected:
            log.info("search_kpis: routed %r -> module=%s (%s)", query, detected, mode)
        out = await catalog.search(query, module=module or detected,
                                   boost_only=boost_only)
        if detected and not boost_only and not out:
            # Hard-filter mode only: a misdetection must not hide every hit.
            log.info("search_kpis: module=%s filter empty; retrying unfiltered", detected)
            out = await catalog.search(query)
        _log_done("search_kpis", t0, f"-> {[r['name'] for r in out]}")
        return out

    async def describe_kpi(kpi: str) -> Dict[str, Any]:
        """Return the full summary of one KPI config: title, unit, execution
        mode, allowed filters, drilldown dimensions and sample questions."""
        t0 = _log_call("describe_kpi", kpi=kpi)
        out = await catalog.summary(kpi)
        if out is None:
            near = [r["name"] for r in await catalog.search(kpi, limit=5)]
            out = {"error": f"unknown KPI {kpi!r}", "closest": near}
        _log_done("describe_kpi", t0)
        return out

    async def generate_query(
        kpi: str,
        period: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        as_of: Optional[str] = None,
        mode: str = "stat",
        dim: Union[str, List[str], None] = None,
        grain: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        comparison: bool = False,
    ) -> Dict[str, Any]:
        """Generate the SQL (+ bind params + inlined preview) for a KPI.

        Works for both DSL and SQL configs. Provide the time window as either a
        natural-language `period` (e.g. "last quarter" — resolved via
        resolve_dates) OR explicit `from_date`/`to_date` (YYYY-MM-DD); if neither
        is given a default window is used.

        mode: "stat" (scalar value; honours the config's CYTD/PYTD comparison
        windows), "series" (time buckets — set `grain` day/week/month/quarter),
        or "table" (grouped by a dimension — set `dim`). `dim` accepts a single
        field OR a list to break down by several dimensions at once, e.g.
        dim=["region_name", "priority"].
        `filters` is a mapping of field -> value or list of values.
        `comparison=True` also emits the previous (PYTD) window in stat mode.
        `as_of` (YYYY-MM-DD) forces a snapshot read.

        PERIOD COMPARISONS ("last quarter vs current quarter", "last month vs
        current month", "previous week vs current week"): pass the WHOLE phrase as
        `period` in ONE call — the server resolves both windows and returns one
        result per side, each tagged `comparison_side` (previous/current) and
        labelled with the user's own phrase, plus a `comparison_windows` block. Do
        NOT split it into two calls with one period each, and do NOT set
        `comparison=True` for it (that flag means the prior-YEAR window).

        TWO-METRIC COMPARISON GROUPED BY A DIMENSION ("incidents created vs
        closed last month by vendor") is a DIFFERENT shape from the above —
        that's ONE metric compared across two time windows; this is TWO
        metrics compared per dimension value in ONE window. If the result
        comes back with `dropped_dim`/`dimension_note` set (the KPI couldn't
        honour the requested `dim`), do NOT silently answer with the
        dimension missing, and do NOT paper over it by calling run_kpi twice
        (once per metric) and merging the two tables yourself — that merge
        has no outer-join guarantee here and can drop a dimension value that
        only has activity on one side (e.g. a vendor with 0 closes). Use
        run_postgres_sql instead: write ONE query with conditional
        aggregation (`count(case when ... end)` per metric), which guarantees
        every dimension value appears for both metrics. See run_postgres_sql's
        docstring for the exact pattern.
        """
        t0 = _log_call("generate_query", kpi=kpi, period=period, mode=mode,
                       dim=dim, grain=grain, filters=filters, comparison=comparison)
        try:
            out = await _generate_query(kpi, period=period, from_date=from_date,
                                        to_date=to_date, as_of=as_of, mode=mode, dim=dim,
                                        grain=grain, filters=filters, comparison=comparison)
        except QueryError as exc:
            log.warning("generate_query rejected: %s", exc)
            return {"error": str(exc)}
        for res in out.get("results", []):     # drop raw (non-JSON) exec params
            res.pop("_exec_params", None)
        _log_done("generate_query", t0, f"-> {len(out['results'])} result(s)")
        return out

    async def run_kpi(
        kpi: str,
        period: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        as_of: Optional[str] = None,
        mode: str = "stat",
        dim: Union[str, List[str], None] = None,
        grain: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        comparison: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Generate the SQL for a KPI AND execute it against its database,
        returning the actual result rows (not just SQL).

        Same arguments as generate_query (see it for `mode`, `period`, `dim`,
        `grain`, `filters`, `comparison`, `as_of`). Each result carries
        `columns` and `rows` (plus `rowcount`), or an `error` string if the
        query could not be executed (e.g. the database connection is not
        configured). The generated SQL is still included for transparency.
        Prefer this tool when the user wants an answer/number, not SQL.

        PERIOD COMPARISONS: pass the whole phrase ("last quarter vs current
        quarter") as `period` in ONE call with mode='stat'. The result carries one
        window per side (`comparison_side`: previous/current) and, for stat mode, a
        ready-made `comparison_summary` {previous, current, delta, pct_change,
        direction} — report those numbers rather than recomputing them.

        TWO-METRIC COMPARISON GROUPED BY A DIMENSION ("incidents created vs
        closed last month by vendor") — see generate_query's docstring for
        why this differs from a period comparison. If this call's result
        carries `dropped_dim`/`dimension_note`, switch to run_postgres_sql
        rather than accepting the answer with the dimension missing, or
        calling run_kpi again for the second metric and merging the two
        tables yourself (no outer-join guarantee exists for that merge here —
        it can drop a dimension value that only has activity on one side).
        """
        t0 = _log_call("run_kpi", kpi=kpi, period=period, mode=mode, dim=dim,
                       grain=grain, filters=filters, comparison=comparison, limit=limit)
        try:
            out = await _run_query(kpi, period=period, from_date=from_date,
                                   to_date=to_date, as_of=as_of, mode=mode, dim=dim,
                                   grain=grain, filters=filters, comparison=comparison,
                                   limit=limit)
        except QueryError as exc:
            log.warning("run_kpi rejected: %s", exc)
            return {"error": str(exc)}
        _log_done("run_kpi", t0, f"-> {len(out['results'])} window(s)")
        return out

    async def query_dataset(
        base: Optional[str] = None,
        metric: Optional[str] = None,
        select: Optional[List[str]] = None,
        measure: Optional[Dict[str, Any]] = None,
        dimensions: Optional[List[str]] = None,
        filters: Optional[List[Dict[str, Any]]] = None,
        period: Optional[str] = None,
        date_field: Optional[str] = None,
        join_with: Optional[List[str]] = None,
        join_type: str = "inner",
        grain: Optional[str] = None,
        drilldown: Optional[Dict[str, Any]] = None,
        order_by: Optional[List[Dict[str, str]]] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Build and execute a query dynamically from the schema — for questions
        NOT covered by a governed KPI, for cross-entity joins, and for drill-down.

        The query is built deterministically and every table/column is validated
        against the schema (unknown names are rejected, never executed). Discover
        valid names with list_modules -> dataset_<module>_<entity>.

        TWO SHAPES — pick ONE:
          * DETAIL LISTING (user wants to SEE records: "details/list/show me the
            incidents …"): pass `select=[columns to return]`. Returns those columns
            as raw rows, one per record — NO aggregation, NO count. Filters, period
            and joins still apply.
          * AGGREGATE (user wants a NUMBER: "how many / count / sum / avg / trend"):
            pass `measure` and/or `dimensions` (grouped) and/or `grain` (time series).
            Do NOT pass `dimensions` for a detail listing — dimensions GROUP BY and
            add count(*), which is wrong when the user asked to see records.

        Args:
          base: the entity slug (e.g. 'itsm_incident') or schema.table to query from.
          metric: optional KPI name to anchor the base table + measure + date field
            (use when the user wants a governed metric but with extra filters/dims).
          select: columns to return as detail rows (no aggregation). Use this for
            "show me / list / details of" questions. Takes precedence over
            measure/dimensions.
          measure: {"agg": count|count_distinct|sum|avg|min|max, "column": "<col>"}
            (omit for count(*)). Only for AGGREGATE questions.
          dimensions: columns to GROUP BY for an aggregate breakdown (e.g.
            ["region_name"]). Only for AGGREGATE questions — never for a listing.
          filters: [{"field": "<col>", "op": "="|"!="|"<"|">"|"<="|">="|"in"|"not_in"|"like"|"not_null",
            "values": [...]}].
          period: natural-language time phrase (e.g. "last month"); applied to date_field
            or the table's time column.
          join_with: entities/tables to join for cross-entity questions (e.g.
            ["itsm_change"] to reach changes from incidents). Joins are planned from the
            schema's declared relationships (see list_relationships).
          join_type: "inner" (default; restrict to records that HAVE the relationship,
            e.g. "incidents caused by changes") or "left" (enrich; keep all base rows).
          grain: day|week|month|quarter for a time series.
          drilldown: {"detail_columns": [...], "entity_filter": {"field","op","values"}}
            to return detail/reason rows for a specific record.
          limit: max rows (default 200, hard cap 5000).
        """
        t0 = _log_call("query_dataset", base=base, metric=metric, select=select,
                       dimensions=dimensions, filters=filters, period=period,
                       join_with=join_with, grain=grain, drilldown=bool(drilldown),
                       limit=limit)
        # A `select` list is a detail listing (no aggregation): route it through the
        # builder's no-aggregate detail path. It wins over measure/dimensions so a
        # "show me the records" question never collapses into a count(*)+GROUP BY.
        if select and not drilldown:
            drilldown = {"detail_columns": select}
            dimensions = None
            measure = None
        spec = {
            "base": base, "metric": metric, "measure": measure,
            "dimensions": dimensions or [], "filters": filters or [],
            "period": period, "date_field": date_field, "join_with": join_with or [],
            "join_type": join_type, "grain": grain, "drilldown": drilldown,
            "order_by": order_by, "limit": limit,
        }
        try:
            out = await _run_dataset_query(spec, limit=limit)
        except (QueryError, BuilderError) as exc:
            log.warning("query_dataset rejected: %s", exc)
            return {"error": str(exc)}
        _log_done("query_dataset", t0)
        return out

    async def get_record(
        record_id: str,
        entity: Optional[str] = None,
        related: Optional[List[str]] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Fetch the DETAILS of ONE specific record by its id — and the records
        linked to it. Use this whenever the user names a concrete record id (e.g.
        an incident 'INC0353896', a change 'CHG0012345', a problem 'PRB...'), NOT a
        KPI/metric tool: a metric would only count it.

        The id's prefix picks the entity and its human id column automatically (via
        record_prefixes.json); pass `entity` to override for an id without a known
        prefix. Returns the record's own detail columns plus, for each declared
        relationship, the linked records' details.

        Args:
          record_id: the record identifier, e.g. 'INC0353896'.
          entity: optional entity slug/name to force (e.g. 'itsm_incident',
            'incident') when the prefix is unknown.
          related: which linked entities to include — omit/None for ALL entities
            reachable in one relationship hop (e.g. changes AND problems for an
            incident), a list like ['change','problem'] to restrict, or ['none']
            for the record only.
          limit: max rows per sub-query (default 50).
        """
        t0 = _log_call("get_record", record_id=record_id, entity=entity,
                       related=related, limit=limit)
        rel_arg: Any = "all" if related is None else (
            "none" if related == ["none"] else related)
        try:
            out = await _get_record_detail(record_id, entity=entity,
                                           related=rel_arg, limit=limit)
        except (QueryError, BuilderError, ValueError) as exc:
            log.warning("get_record rejected: %s", exc)
            return {"error": str(exc)}
        _log_done("get_record", t0, f"-> found={out.get('found')} {len(out['results'])} block(s)")
        return out

    async def plan_query(question: str, module: Optional[str] = None) -> Dict[str, Any]:
        """PLAN an ad-hoc / cross-entity question BEFORE building a query — the
        discovery step for anything no governed KPI answers and that isn't a single
        named record. Identifies the entity(ies) the question is about and returns a
        compact schema context: each entity's columns by role, the VALUE domains for
        enum columns (dimensions_with_domain — pick filter values from here), and the
        declared relationships/one-hop reachability between them (for cross-entity
        joins). Also flags when a question is really a metric ("how many …" ->
        run_kpi). It does NOT write or run SQL; use its output to assemble a
        query_dataset spec, then call preflight on that spec.

        Args:
          question: the user's natural-language question.
          module: optional module hint (code or phrase, e.g. 'change', 'release') to
            bias entity identification.
        """
        t0 = _log_call("plan_query", question=question, module=module)
        out = await _plan_query(question, module=module)
        _log_done("plan_query", t0,
                  f"-> {len(out.get('candidates') or [])} candidate(s)")
        return out

    def preflight(
        base: Optional[str] = None,
        metric: Optional[str] = None,
        measure: Optional[Dict[str, Any]] = None,
        dimensions: Optional[List[str]] = None,
        filters: Optional[List[Dict[str, Any]]] = None,
        period: Optional[str] = None,
        date_field: Optional[str] = None,
        join_with: Optional[List[str]] = None,
        join_type: str = "inner",
        grain: Optional[str] = None,
        drilldown: Optional[Dict[str, Any]] = None,
        order_by: Optional[List[Dict[str, str]]] = None,
        select: Optional[List[str]] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """VALIDATE a query_dataset spec against the schema BEFORE executing it —
        same argument shape as query_dataset. Returns {ok, errors, warnings, notes,
        resolved} collecting ALL problems at once so you can fix the spec in one
        pass: unknown base/join/column (with suggestions), a join with no declared
        relationship (with reachable targets), a filter value outside a column's
        domain (with the valid list), a non-timestamp date_field, and 1:N fan-out
        (DISTINCT) risk. Nothing is executed. Call this after plan_query and before
        query_dataset; if ok is true the same spec will build and run."""
        t0 = _log_call("preflight", base=base, join_with=join_with,
                       filters=filters, period=period)
        if select and not drilldown:
            drilldown = {"detail_columns": select}
            dimensions = None
            measure = None
        spec = {
            "base": base, "metric": metric, "measure": measure,
            "dimensions": dimensions or [], "filters": filters or [],
            "period": period, "date_field": date_field, "join_with": join_with or [],
            "join_type": join_type, "grain": grain, "drilldown": drilldown,
            "order_by": order_by, "limit": limit,
        }
        out = _preflight(spec)
        _log_done("preflight", t0, f"-> ok={out.get('ok')}")
        return out

    def list_predicates(entity: Optional[str] = None) -> List[Dict[str, Any]]:
        """List the SCOPE PREDICATES available — the reusable qualifiers that restrict
        WHICH records a metric counts (e.g. 'major', 'sla breached', 'emergency change',
        'high risk', 'failed change', 'major release').

        These are NOT metrics and NOT dimensions. A phrase like "sla breached" reads
        like a KPI name but is really a WHERE clause, so passing it to search_kpis/
        run_kpi gives a wrong or missing answer. Pass them to `compose_metric` instead.

        Each entry gives the canonical `name`, the `synonyms` that resolve to it, the
        `entity` it applies to, and the tables it can be evaluated on. Optionally filter
        by entity ('incident', 'problem', 'change', 'release', 'service_request')."""
        from cora_mcp.predicate_registry import get_registry as _preds
        t0 = _log_call("list_predicates", entity=entity)
        reg = _preds()
        out = []
        for name in reg.names():
            p = reg.get(name)
            if entity and p.entity != entity:
                continue
            out.append({
                "name": p.name, "entity": p.entity, "synonyms": p.synonyms,
                "grain_key": p.grain_key,
                "tables": p.tables(),
                "free_on": [b.table for b in p.bindings() if b.is_free],
            })
        _log_done("list_predicates", t0, f"-> {len(out)}")
        return out

    async def compose_metric(
        measure: Optional[Dict[str, Any]] = None,
        predicates: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        dimensions: Optional[List[str]] = None,
        select: Optional[List[str]] = None,
        period: Optional[str] = None,
        grain: Optional[str] = None,
        entity: Optional[str] = None,
        base: Optional[str] = None,
        date_field: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Answer a question qualified by ONE OR MORE scope predicates — either as a
        NUMBER or as the underlying RECORDS.

        TWO SHAPES:
          * AGGREGATE (default) — "how many <entity> that are BOTH <X> AND <Y>".
          * DETAIL LISTING — pass `select`. Use this for "show me / list / details of
            the <X> that are <Y>". Same predicate logic, but it returns raw rows with
            no aggregation. Pass `select=[]` (an empty list) to get a sensible default
            column set derived from the schema — do NOT invent column names.

        Use `select` here rather than query_dataset for any qualified listing:
        query_dataset would need a declared JOIN to reach the qualifier's table, and
        this composes it as an EXISTS test instead, which needs no relationship.

        USE THIS when the question carries a qualifier that is not one of a KPI's
        dimensions: 'major', 'sla breached', 'emergency', 'high risk', 'failed',
        'major release', 'closed incomplete' (see `list_predicates`). Those restrict
        which records count; they are not metrics, so run_kpi cannot apply them and
        search_kpis will mis-resolve them to a similarly-named KPI.

        It picks the anchor table automatically: the table that satisfies the most
        predicates itself (a table already scoped to the subset costs no filter at all)
        while still carrying every requested filter and dimension. Any predicate that
        lives elsewhere becomes an EXISTS test on the shared entity key — a row filter,
        so one-to-many rows can never inflate the measure.

        If a requested filter or predicate cannot be expressed anywhere, it REFUSES with
        the reason instead of dropping it — a partially-applied question returns a
        confidently wrong number.

        Args:
          measure: {"agg": count|count_distinct|sum|avg|min|max, "column": "<col>"};
            defaults to count_distinct on the entity key, so an anchor holding several
            rows per entity cannot inflate the count. Ignored when `select` is given.
          predicates: scope predicate names/synonyms, ANDed together (from list_predicates).
          filters: {dimension word -> value or [values]} e.g. {"business": "PBNA"}.
          dimensions: breakdown columns/words, e.g. ["region"].
          select: detail columns for a LISTING (mutually exclusive with measure/
            dimensions). `[]` = pick a sensible default set from the schema.
          period: natural-language window passed verbatim, e.g. "last 3 months".
          grain: day|week|month|quarter for a time series.
          entity: optional entity hint; inferred from the predicates when omitted.
          base: force a specific anchor table (skips selection).
          date_field: force which timestamp the period applies to (opened vs closed).

        Returns the rows plus a `composition` block naming the anchor table, how each
        predicate was satisfied (free / direct / semi_join) and any dropped breakdown —
        report those notes so the user knows how the number was scoped.
        """
        from cora_mcp.composer import ComposeError, compose_and_run
        t0 = _log_call("compose_metric", predicates=predicates, filters=filters,
                       dimensions=dimensions, select=select, period=period,
                       entity=entity, measure=measure, grain=grain)
        try:
            out = await compose_and_run(
                measure=measure, predicates=predicates, filters=filters,
                dimensions=dimensions, select=select, period=period, grain=grain,
                entity=entity, base=base, date_field=date_field, limit=limit)
        except (ComposeError, QueryError, BuilderError) as exc:
            log.warning("compose_metric refused: %s", exc)
            return {"error": str(exc)}
        _log_done("compose_metric", t0,
                  f"-> anchor={out.get('composition', {}).get('anchor_table')}")
        return out

    def list_relationships(module: Optional[str] = None) -> List[Dict[str, Any]]:
        """List the declared table relationships (join paths) available for
        cross-entity queries — e.g. incident_caused_by_change, incident_has_problem.
        Pass a `module` code to filter. Use the `name`s / tables to decide what to
        pass as query_dataset `join_with`."""
        t0 = _log_call("list_relationships", module=module)
        out = get_graph().relationships(module)
        _log_done("list_relationships", t0, f"-> {len(out)}")
        return out

    async def overview_module(
        module: str,
        period: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        dim: Union[str, List[str], None] = None,
        limit_kpis: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Module-level insights: run every KPI in a module for one period and
        filter, and return a rollup (value, delta vs the comparison window,
        target and RAG status per KPI).

        The comparison window is NOT always "last month": a named period like
        "current month"/"this quarter" compares against the SAME calendar
        dates one year ago (e.g. Aug 1-9 2026 vs Aug 1-9 2025), while a
        CYTD-basis KPI with no period given compares CYTD vs PYTD instead.
        Each KPI's `previous_window` (from/to) and `previous_label` say which
        one was actually used -- when reporting a delta, say what it's versus
        (e.g. "vs the same 9 days last year"), don't just say "improved from
        X" and let the basis go unstated.

        Use this for broad questions like "what's happening in availability for
        the CGF sector" (module="availability", filters={"sector":"CGF"}) or
        "insights on the service desk for APAC" (module="service desk",
        filters={"region":"APAC"}). `module` accepts a module code from
        list_kpi_modules OR a phrase ("service desk", "availability") — the
        valid codes depend on the deployment, so don't assume a fixed set.
        `period` is a natural-language window (e.g. "last quarter"); `filters` is
        a mapping of field -> value(s). Each filter is applied only to the KPIs
        that allow it — the rest record it under `dropped_filters`, never
        silently mis-applied.

        For "overview of <module> BY <dimension>" (e.g. "service desk by
        business", "incidents by region and priority") pass `dim` — a single word
        or a list. Each KPI then also returns a `breakdown` grouped by that
        dimension (on top of its overall value); KPIs that can't be grouped by it
        record `dropped_dim`. Do NOT call this then improvise per-KPI run_kpi
        breakdowns — one overview_module call with `dim` covers the whole module.
        For a single metric use run_kpi instead."""
        t0 = _log_call("overview_module", module=module, period=period,
                       filters=filters, dim=dim, limit_kpis=limit_kpis)
        try:
            out = await _module_overview(module, period=period, filters=filters,
                                         dim=dim, limit_kpis=limit_kpis)
        except QueryError as exc:
            log.warning("overview_module rejected: %s", exc)
            return {"error": str(exc)}
        _log_done("overview_module", t0, f"-> {out['kpi_count']} KPI(s)")
        return out

    async def describe_module(module: Optional[str] = None) -> Dict[str, Any]:
        """Catalog overview of ONE schema module: database type, coverage and its
        entities (query each via describe_dataset). For ITSM this also lists the
        governed KPI configs. Pass the module `name` (from list_modules); with no
        name (or an unknown one) it returns the list of valid module names."""
        loader = get_loader()
        catalog = get_catalog()
        t0 = _log_call("describe_module", module=module)
        valid = loader.module_names()
        if not module or module not in valid:
            return {"error": f"unknown module {module!r}" if module else "module required",
                    "available_modules": valid}
        summary = loader.module_summary(module) or {}
        if module == "itsm":                       # itsm spans the KPI module codes
            names = await catalog.names()
            summary["kpis"] = list(await asyncio.gather(*(catalog.summary(n) for n in names)))
        _log_done("describe_module", t0)
        return summary

    async def describe_dataset(dataset: Optional[str] = None) -> Dict[str, Any]:
        """Describe ONE dataset/entity: columns grouped by role (dimensions,
        measures, timestamps, identifiers), possible values, member tables and any
        related KPIs. Pass the entity `dataset` slug (e.g. 'itsm_change', from
        list_modules / describe_module). With no slug (or an unknown one) it
        returns the list of valid dataset slugs."""
        loader = get_loader()
        catalog = get_catalog()
        t0 = _log_call("describe_dataset", dataset=dataset)
        slugs = [slug for _m, slug, _e in loader.all_entities()]
        if not dataset or dataset not in slugs:
            return {"error": f"unknown dataset {dataset!r}" if dataset else "dataset required",
                    "available_datasets": slugs}
        detail = loader.entity_detail(dataset)
        if detail is None:
            return {"error": f"unknown dataset {dataset!r}", "available_datasets": slugs}
        related = await catalog.configs_for_tables(detail_table_fqns(detail))
        if related:
            detail["related_kpis"] = related
        _log_done("describe_dataset", t0,
                  f"-> {len(detail['dimensions'])} dims, {len(detail['measures'])} measures")
        return detail

    def resolve_filter_value(dataset: str, column: str, value: str) -> Dict[str, Any]:
        """Resolve a user-typed filter VALUE to what's actually stored for a
        column — the same deterministic resolver run_kpi/compose_metric/
        query_dataset already use internally (exact match -> curated synonym
        -> fuzzy near-match -> reject).

        Call this BEFORE inlining a literal into hand-written SQL
        (run_postgres_sql) whenever the value names a real-world thing (a
        vendor, sector, status, priority, ...) rather than a number or date.
        A user typing "Wipro" when the column stores "WIPRO LTD" produces a
        query that runs fine and returns zero rows — indistinguishable from
        "there is no data" unless the value was resolved first.

        Args:
          dataset: entity slug from describe_dataset (e.g. 'itsm_incident').
          column: the column/dimension word (e.g. 'vendor', or its physical
            name if you already know it).
          value: what the user said.

        Returns {column, input, resolved, matched, method, valid_values}.
        `method` is exact|synonym|fuzzy|passthrough|reject. If `matched` is
        False, `valid_values` lists the column's real domain — surface it or
        ask the user which one they meant rather than guessing or silently
        filtering on the unresolved literal.
        """
        from cora_mcp import value_resolver
        from cora_mcp.column_resolver import resolve_column_detail
        t0 = _log_call("resolve_filter_value", dataset=dataset, column=column, value=value)
        loader = get_loader()
        detail = loader.entity_detail(dataset)
        if detail is None:
            _log_done("resolve_filter_value", t0, "-> unknown dataset")
            return {"error": f"unknown dataset {dataset!r}",
                    "available_datasets": [slug for _m, slug, _e in loader.all_entities()]}
        possible = (detail.get("possible_values") or {}).get(column)
        if possible is None:
            # `column` may be a business word (e.g. "vendor"), not the physical
            # column name (e.g. "it_vendor_name") possible_values is keyed by.
            for fqn in detail_table_fqns(detail):
                real, _how = resolve_column_detail(fqn, column, roles=("dimension", "filter"))
                if real:
                    possible = (detail.get("possible_values") or {}).get(real)
                    if possible is not None:
                        column = real
                        break
        r = value_resolver.resolve_value(column, value, possible)
        out = {"column": column, "input": value, "resolved": r.resolved,
               "matched": r.matched, "method": r.method, "valid_values": r.valid_values}
        _log_done("resolve_filter_value", t0, f"-> {r.method}")
        return out

    async def generate_sql(
        sql: str,
        dimensions: Optional[List[str]] = None,
        filters: Optional[List[str]] = None,
        dialect: str = "postgres",
    ) -> Dict[str, Any]:
        """DRY-RUN a hand-written SQL SELECT: check it's safe and well-formed,
        and whether the dimensions/filters you INTENDED actually made it into
        the query — all WITHOUT executing it or reading a single row.

        Call this before run_postgres_sql whenever you hand-wrote SQL
        yourself, especially for a "compare two metrics grouped by a
        dimension" question (e.g. "incidents created vs closed last month by
        vendor") — exactly the class of question where a dimension or one
        side of the comparison has been found to silently go missing. Pass
        the dimension/filter words you MEANT to apply and get back whether
        they actually show up in GROUP BY / WHERE, instead of trusting your
        own SQL by eye.

        Args:
          sql: the SELECT/WITH/UNION statement to check.
          dimensions: dimension words you intended to GROUP BY (e.g.
            ["vendor"]) — checked for presence in the query's GROUP BY.
          filters: filter words you intended to apply (e.g. ["status"]) —
            checked for presence in the query's WHERE/JOIN ON.
          dialect: SQL dialect (default 'postgres').

        Returns:
          sql: your SQL with a LIMIT enforced (added if you omitted one).
          applied_dimensions / missing_dimensions: which requested dimension
            words were found (or not) in the GROUP BY.
          dimensions_in_select_but_not_grouped: a requested dimension appears
            in SELECT but with no GROUP BY on it -- it will NOT break the
            numbers down per value, just repeat one value on every row.
          applied_filters / missing_filters: which requested filter words
            were found (or not) in WHERE/JOIN ON.
          schema_check: {ok: true} if the live database's own parser accepted
            the statement (Parse+Describe -- zero rows read), or its error if
            a column/table doesn't actually exist. `null` if no database is
            configured (not treated as a failure).
          error: set INSTEAD of the above if the SQL was rejected outright --
            a syntax error, or the read-only guard blocked it (see
            run_postgres_sql's docstring for exactly what's blocked and why).

        Note: this does NOT check for missing standard business-rule row
        exclusions (e.g. excluding cancelled/system-generated records) -- see
        run_postgres_sql's docstring for the known per-entity list. A query
        can pass this dry-run cleanly (safe, grouped, filtered exactly as
        intended) and still silently include rows a governed KPI would have
        excluded.
        """
        t0 = _log_call("generate_sql", sql=sql, dimensions=dimensions, filters=filters)
        from cora_mcp import sql_guard
        try:
            checked_sql = sql_guard.check_readonly_sql(sql, dialect=dialect)
        except sql_guard.SQLGuardError as exc:
            _log_done("generate_sql", t0, "-> guard rejected")
            return {"error": str(exc), "sql": sql}

        try:
            report = _sql_presence_report(checked_sql, dimensions or [], filters or [], dialect)
        except Exception as exc:
            report = {"presence_check_error": f"could not analyse SQL structure: {exc}"}

        out: Dict[str, Any] = {"sql": checked_sql, **report}

        from cora_mcp import db as _db
        try:
            v = await _db.validate(dialect, None, checked_sql, [], plan=False)
            out["schema_check"] = {"ok": True, "param_types": v.get("param_types")}
        except _db.DBNotConfigured:
            out["schema_check"] = None
        except _db.DBError as exc:
            schema_check: Dict[str, Any] = {"ok": False, "error": str(exc)}
            from cora_mcp import sql_autofix
            fix = sql_autofix.suggest_fix(checked_sql, dialect, str(exc))
            if fix.get("fixed_sql"):
                schema_check["suggested_fix"] = {"sql": fix["fixed_sql"], "note": fix["note"]}
            elif fix.get("hints"):
                schema_check["hints"] = fix["hints"]
            out["schema_check"] = schema_check
        _log_done("generate_sql", t0,
                  f"-> missing_dims={out.get('missing_dimensions')} "
                  f"missing_filters={out.get('missing_filters')}")
        return out

    async def run_postgres_sql(
        sql: str,
        limit: int = 200,
        dimensions: Optional[List[str]] = None,
        filters: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Execute a hand-written, read-only Postgres SQL SELECT and return
        the rows. Only SELECT / WITH / UNION are allowed -- no INSERT/UPDATE/
        DELETE/DDL anywhere in the statement (including hidden inside a CTE),
        no querying information_schema/pg_catalog/pg_*, no session/file/
        network functions (pg_sleep, dblink*, set_config, pg_terminate_backend,
        ...). See cora_mcp.sql_guard for the exact rules -- anything it
        rejects comes back as `error`, never a raw driver exception.

        USE THIS as the escape hatch for questions no governed KPI (run_kpi)
        or the structured builder (query_dataset / compose_metric) can
        express in ONE call -- most notably a "metric A vs metric B, grouped
        by dimension X" comparison (e.g. "incidents created vs closed last
        month by vendor"). Write ONE query with conditional aggregation
        instead of two separate KPI calls you would have to merge yourself:

            SELECT it_vendor_name AS vendor,
                   count(distinct case when created_date_time
                         between '2026-07-01 00:00:00' and '2026-07-31 23:59:59'
                         then incident_id end) AS created_count,
                   count(distinct case when closed_date_time
                         between '2026-07-01 00:00:00' and '2026-07-31 23:59:59'
                         then incident_id end) AS closed_count
            FROM itsm_incident.tbl_all_incidents
            GROUP BY it_vendor_name

        This is deliberately preferred over two separate run_kpi calls for
        this shape: one table scan, one GROUP BY, both metrics guaranteed
        present for every vendor -- including a vendor with zero of one side
        -- with no client-side merge that can silently drop a metric or a
        dimension value.

        BEFORE WRITING SQL:
          1. Call describe_dataset(<entity slug>) for real column names and
             each column's `possible_values` -- never invent a column name or
             guess a stored value's spelling.
          2. Call resolve_dates(period) for any date/time phrase ("last
             month", "MTD", ...) and splice start_date/end_date into your
             WHERE -- do not compute date math yourself.
          3. Call resolve_filter_value(dataset, column, value) for every
             filter value that names a real-world thing (a vendor, sector,
             status, ...) -- do not inline the user's literal spelling
             unresolved.
          4. Optionally call generate_sql first to dry-run your query -- but
             this tool now runs the SAME presence check and, on a Postgres
             error, the SAME self-correction pass itself, so skipping that
             call no longer skips the protection.

        STANDARD BUSINESS-RULE EXCLUSIONS -- a governed KPI's authored SQL
        often excludes rows a naive query would include (cancelled records,
        automation-generated tickets, etc.). Free-text SQL does NOT get these
        automatically. Known exclusions (NOT exhaustive -- cross-check
        describe_kpi on a similarly named governed KPI for the entity before
        assuming none apply):
          itsm_incident (user-reported style counts):
            status_name != 'CANCELED'
            AND coalesce(contact_type, 'Channel is Empty') <> 'SYSTEM GENERATED'
            AND open_by_full_name != 'DNAC.INTEGRATION'

        Args:
          sql: the SELECT/WITH/UNION statement to run. No separate params
            list -- inline literals (resolve any user-facing value with
            resolve_filter_value first, then inline the resolved literal).
          limit: max rows returned (default 200, hard cap 5000) -- enforced
            whether or not your SQL already has a LIMIT.
          dimensions: optional dimension words you intended to GROUP BY --
            checked for presence in the GROUP BY, same as generate_sql.
          filters: optional filter words you intended to apply -- checked for
            presence in WHERE/JOIN ON, same as generate_sql.
        """
        t0 = _log_call("run_postgres_sql", sql=sql, limit=limit)
        from cora_mcp import sql_guard, db as _db
        cap = max(1, min(int(limit or 200), sql_guard.MAX_LIMIT))
        try:
            checked_sql = sql_guard.check_readonly_sql(
                sql, dialect="postgres", default_limit=cap, max_limit=cap)
        except sql_guard.SQLGuardError as exc:
            log.warning("run_postgres_sql rejected: %s", exc)
            return {"error": str(exc), "sql": sql}
        try:
            presence = _sql_presence_report(checked_sql, dimensions or [], filters or [],
                                            "postgres")
        except Exception as exc:
            presence = {"presence_check_error": f"could not analyse SQL structure: {exc}"}
        try:
            out = await _db.execute("postgres", None, checked_sql, [], limit=cap)
        except _db.DBError as exc:
            log.warning("run_postgres_sql failed: %s", exc)
            from cora_mcp import sql_autofix
            fix = sql_autofix.suggest_fix(checked_sql, "postgres", str(exc))
            fixed_sql = fix.get("fixed_sql")
            if fixed_sql:
                try:
                    retried_sql = sql_guard.check_readonly_sql(
                        fixed_sql, dialect="postgres", default_limit=cap, max_limit=cap)
                    retry_out = await _db.execute("postgres", None, retried_sql, [], limit=cap)
                except (sql_guard.SQLGuardError, _db.DBError) as retry_exc:
                    log.warning("run_postgres_sql auto-fix retry also failed: %s", retry_exc)
                    return {"error": str(exc), "sql": checked_sql, **presence,
                             "attempted_fix": {"sql": fixed_sql, "note": fix["note"],
                                                "retry_error": str(retry_exc)}}
                retry_out["sql"] = retried_sql
                retry_out["auto_corrected"] = {"from_sql": checked_sql, "to_sql": retried_sql,
                                               "reason": fix["note"]}
                try:
                    retry_out.update(_sql_presence_report(retried_sql, dimensions or [],
                                                          filters or [], "postgres"))
                except Exception:
                    retry_out.update(presence)
                _log_done("run_postgres_sql", t0,
                          f"-> auto-corrected, {retry_out.get('rowcount')} row(s)")
                return retry_out
            return {"error": str(exc), "sql": checked_sql, **presence,
                     "hints": fix.get("hints", [])}
        out["sql"] = checked_sql
        out.update(presence)
        _log_done("run_postgres_sql", t0, f"-> {out.get('rowcount')} row(s)")
        return out

    names = []
    for fn, name in [
        (resolve_dates, "resolve_dates"),
        (list_modules, "list_modules"),
        (list_kpi_modules, "list_kpi_modules"),
        (refresh_kpi_modules, "refresh_kpi_modules"),
        (search_kpis, "search_kpis"),
        (describe_kpi, "describe_kpi"),
        (generate_query, "generate_query"),
        (run_kpi, "run_kpi"),
        (get_record, "get_record"),
        (query_dataset, "query_dataset"),
        (list_predicates, "list_predicates"),
        (compose_metric, "compose_metric"),
        (plan_query, "plan_query"),
        (preflight, "preflight"),
        (list_relationships, "list_relationships"),
        (overview_module, "overview_module"),
        (describe_module, "describe_module"),
        (describe_dataset, "describe_dataset"),
        (resolve_filter_value, "resolve_filter_value"),
        (generate_sql, "generate_sql"),
        (run_postgres_sql, "run_postgres_sql"),
    ]:
        mcp.add_tool(_dedup(fn, name), name=name)
        names.append(name)
    return names


def detail_table_fqns(detail: Dict[str, Any]) -> List[str]:
    return [t["name"] for t in detail.get("tables", [])]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def register_tools(mcp) -> int:
    """Register every CORA tool on ``mcp``. Returns the tool count.

    The catalog is exposed through a small, fixed tool surface — the per-module
    and per-entity lookups are the parameterized ``describe_module`` /
    ``describe_dataset`` tools rather than one auto-generated tool per module /
    entity (which bloated the surface to 46 tools and hurt tool selection)."""
    core = _register_core(mcp)
    log.info("registered tools: %d core", len(core))
    log.debug("core=%s", core)
    return len(core)
