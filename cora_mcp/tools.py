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
        # carry two module vocabularies (legacy 'cm' vs pepops 'changes').
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
        (plan_query, "plan_query"),
        (preflight, "preflight"),
        (list_relationships, "list_relationships"),
        (overview_module, "overview_module"),
        (describe_module, "describe_module"),
        (describe_dataset, "describe_dataset"),
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
