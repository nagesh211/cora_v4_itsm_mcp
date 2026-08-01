"""Deterministic SQL builder from a validated structured intent (QuerySpec).

The LLM never writes SQL. It emits a QuerySpec (which table, what to measure,
dimensions, filters, joins, date window, drill-down); this module validates every
identifier against ``schema_loader``, plans joins via ``relationships`` and emits a
read-only ``SELECT`` with psycopg-style ``%s`` params — so the existing
``db.execute`` path (``to_asyncpg`` + prepared-type coercion) runs it unchanged.

Anything not present in the schema raises :class:`BuilderError` *before* any SQL
is produced, so hallucinated tables/columns can never execute.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from cora_mcp.date_resolver import resolve_dates
from cora_mcp.logging_config import get_logger
from cora_mcp.relationships import NoJoinPathError, get_graph
from cora_mcp.schema_loader import get_loader
from cora_mcp import value_resolver

log = get_logger(__name__)

_AGGS = {"count", "count_distinct", "sum", "avg", "min", "max"}
_SCALAR_OPS = {"=", "!=", "<", ">", "<=", ">="}
_OPS = _SCALAR_OPS | {"in", "not_in", "like", "not_null"}
_GRAIN = {"day": "day", "week": "week", "month": "month", "quarter": "quarter"}
_ALIASES = "abcdefghijklmnopqrstuvwxyz"
_MAX_LIMIT = 5000
P = "\x01"  # param placeholder sentinel (rendered to %s at the end)


class BuilderError(ValueError):
    """The spec references something not in the schema, or is malformed."""


class Measure(BaseModel):
    agg: Optional[str] = None            # count|count_distinct|sum|avg|min|max
    column: Optional[str] = None         # column name, or "*"/None for count
    expression: Optional[str] = None     # raw SQL measure (from a metric anchor; trusted)
    alias: str = "value"


class Filter(BaseModel):
    field: str
    op: str = "="
    values: List[Any] = Field(default_factory=list)
    # Set when ``values`` are ALREADY the real stored values (e.g. emitted from a
    # predicate binding, which carries DB-verified constants). Skips the
    # ``value_resolver`` pass so a known-good value is never re-checked against the
    # column's declared ``possible_values`` — those were measured to be wrong on
    # several columns, and re-resolving would reject a correct value.
    resolved: bool = False


class SemiJoin(BaseModel):
    """An EXISTS test against a related table — a row *filter*, not a row multiplier.

    Used when a predicate cannot bind to the anchor table and must be evaluated on
    another table at the same entity grain. EXISTS rather than INNER JOIN is
    deliberate: the SLA tables hold one row per SLA clock, so a join would fan out
    and silently corrupt ``avg``/``sum``/``count(*)`` measures. An EXISTS keeps the
    base row count intact no matter how many matching rows exist.
    """
    table: str                                   # entity slug or schema.table
    key_right: str                               # join column ON THE JOINED table
    key_left: Optional[str] = None               # base-side column (defaults to key_right)
    filters: List[Filter] = Field(default_factory=list)
    negate: bool = False                         # NOT EXISTS
    label: Optional[str] = None                  # why this exists, for explainability


class Drilldown(BaseModel):
    detail_columns: List[str] = Field(default_factory=list)
    entity_filter: Optional[Filter] = None
    # When set, detail_columns are resolved against THIS table first (falling back
    # to the rest of the scope). Needed for linked-record detail so a column that
    # exists in both the base and a joined table (e.g. title_text) resolves to the
    # intended joined entity, not the base.
    detail_table: Optional[str] = None


class QuerySpec(BaseModel):
    base: Optional[str] = None           # entity slug or schema.table
    metric: Optional[str] = None         # optional KPI anchor (table + measure + date field)
    measure: Optional[Measure] = None
    dimensions: List[str] = Field(default_factory=list)
    filters: List[Filter] = Field(default_factory=list)
    semi_joins: List[SemiJoin] = Field(default_factory=list)
    period: Optional[str] = None         # NL phrase -> resolve_dates
    date_field: Optional[str] = None
    join_with: List[str] = Field(default_factory=list)
    join_type: str = "inner"             # inner (restrict to related) | left (enrich)
    grain: Optional[str] = None
    drilldown: Optional[Drilldown] = None
    order_by: Optional[List[Dict[str, str]]] = None
    limit: int = 200


class BuildResult(BaseModel):
    sql: str
    params: List[Any]
    base_table: str
    joined_tables: List[str]
    # Tables reached by an EXISTS test rather than a JOIN — they restrict the base
    # rows but contribute no columns, so callers can explain the scoping.
    semi_joined_tables: List[str] = Field(default_factory=list)
    date_window: Optional[Dict[str, Any]] = None
    dropped_dimensions: List[str] = Field(default_factory=list)
    # Set when a comparison period forced a time bucket the caller didn't ask for,
    # so the answer can say the rows are per-period.
    implicit_grain: Optional[str] = None


def _norm_spec(spec) -> QuerySpec:
    return spec if isinstance(spec, QuerySpec) else QuerySpec(**(spec or {}))


class _Builder:
    def __init__(self, spec: QuerySpec):
        self.spec = spec
        self.loader = get_loader()
        self.graph = get_graph()
        self.scope: List[Tuple[str, str]] = []   # (table_fqn, alias) in join order
        self.params: List[Any] = []

    # ---- identifier helpers ---------------------------------------------
    def _resolve_base(self) -> str:
        base = self.spec.base
        if not base:
            raise BuilderError("spec.base is required (an entity slug or schema.table)")
        if "." in base and self.loader.get_table(base):
            return base
        primary = self.loader.entity_primary_table(base)
        if primary:
            return primary
        raise BuilderError(
            f"unknown base {base!r}: not a known table or entity. "
            f"Use list_modules / dataset_<entity> to discover valid names.")

    def _resolve_col(self, colname: str, prefer: Optional[str] = None) -> Tuple[str, dict]:
        """Find (alias, column_info) for a bare column across in-scope tables.

        If ``prefer`` (a table fqn) is given and it holds the column, that table
        wins over scope order — so a name present in several joined tables (e.g.
        ``title_text``) resolves to the intended one."""
        if prefer:
            for fqn, alias in self.scope:
                if fqn == prefer:
                    ci = self.loader.column_info(fqn, colname)
                    if ci:
                        return alias, ci
        for fqn, alias in self.scope:
            ci = self.loader.column_info(fqn, colname)
            if ci:
                return alias, ci
        raise BuilderError(self._unknown_column_msg(colname))

    def _unknown_column_msg(self, colname: str) -> str:
        """A column isn't in scope — build an actionable message: close matches in
        the queried tables, and whether the exact name lives in a table that just
        isn't joined (so the model corrects in one step instead of guessing)."""
        import difflib
        in_scope = [t for t, _ in self.scope]
        available = self.loader.column_names_in(in_scope)
        parts = [f"column {colname!r} not found in the queried tables {in_scope}"]
        # exact name exists elsewhere in the schema?
        elsewhere = [t for t in self.loader.tables_with_column(colname) if t not in in_scope]
        if elsewhere:
            parts.append(
                f"NOTE: a column named {colname!r} exists in {elsewhere}, which "
                f"is/are not part of this query. Add it via join_with, or query that "
                f"entity directly.")
        close = difflib.get_close_matches(colname, available, n=5, cutoff=0.6)
        if close:
            parts.append(f"did you mean one of: {close}?")
        else:
            parts.append(f"available columns include: {available[:40]}")
        return " ".join(parts)

    @staticmethod
    def _ref_from(alias: str, ci: dict, colname: str) -> str:
        phys = ci.get("physical_name")
        if phys and phys != colname:
            return f'{alias}."{phys}"'
        return f"{alias}.{colname}"

    @staticmethod
    def _is_array(ci: dict) -> bool:
        return "[]" in (ci.get("type") or "")

    def _col_ref(self, colname: str) -> str:
        """Resolve a bare column name to alias.\"physical\" across in-scope tables."""
        alias, ci = self._resolve_col(colname)
        return self._ref_from(alias, ci, colname)

    def _next_alias(self) -> str:
        return _ALIASES[len(self.scope)]

    # ---- clauses ---------------------------------------------------------
    def _plan_joins(self, base_fqn: str) -> List[str]:
        if not self.spec.join_with:
            return []
        targets = []
        for jw in self.spec.join_with:
            if "." in jw and self.loader.get_table(jw):
                targets.append(jw)
            else:
                pt = self.loader.entity_primary_table(jw)
                if not pt:
                    raise BuilderError(f"unknown join target {jw!r}")
                targets.append(pt)
        try:
            clauses = self.graph.plan_join(base_fqn, targets)
        except NoJoinPathError as exc:
            raise BuilderError(str(exc)) from exc

        jt = (self.spec.join_type or "inner").lower()
        if jt not in ("inner", "left"):
            raise BuilderError("join_type must be 'inner' or 'left'")
        keyword = "INNER JOIN" if jt == "inner" else "LEFT JOIN"
        alias_of = {base_fqn: "a"}
        sql_parts: List[str] = []
        for jc in clauses:
            tbl = jc["table"]
            if tbl not in alias_of:
                alias_of[tbl] = self._next_alias_for(alias_of)
                self.scope.append((tbl, alias_of[tbl]))
            on_bits = []
            for (lt, lc, rt, rc) in jc["on"]:
                on_bits.append(f"{alias_of[lt]}.{lc} = {alias_of[rt]}.{rc}")
            for (t, c, val) in jc["const"]:
                on_bits.append(f"{alias_of[t]}.{c} = {P}")
                self.params.append(val)
            sql_parts.append(f"{keyword} {tbl} {alias_of[tbl]} ON " + " AND ".join(on_bits))
        return sql_parts

    def _next_alias_for(self, alias_of: dict) -> str:
        return _ALIASES[len(alias_of)]

    def _between(self, col: str, win: dict) -> str:
        self.params.append(win["start_date"] + " 00:00:00")
        self.params.append(win["end_date"] + " 23:59:59")
        return f"cast({col} as timestamp) BETWEEN {P} AND {P}"

    def _where(self) -> List[str]:
        where: List[str] = []
        for flt in self.spec.filters:
            where.append(self._condition(flt))
        # date window
        if self.spec.period:
            date_field = self.spec.date_field or self.loader.table_time_field(self.scope[0][0])
            if not date_field:
                raise BuilderError(
                    f"period given but no date_field and table {self.scope[0][0]} has no "
                    f"time column; pass date_field explicitly")
            win = self._period_win or resolve_dates(self.spec.period)
            self._date_window = win
            col = self._col_ref(date_field)
            cmp_windows = win.get("comparison")
            if cmp_windows:
                # A comparison phrase ("last month vs current month") covers TWO
                # windows. Both are matched with an OR rather than one span across
                # them, so disjoint sides ("Q1 2026 vs Q3 2026") never drag in the
                # periods between. `_select_and_group` adds the period bucket so each
                # side lands on its own row.
                sides = " OR ".join(self._between(col, cmp_windows[s])
                                    for s in ("previous", "current"))
                where.append(f"({sides})")
            else:
                where.append(self._between(col, win))
        # drill-down entity pin
        if self.spec.drilldown and self.spec.drilldown.entity_filter:
            where.append(self._condition(self.spec.drilldown.entity_filter))
        # semi-joins last, so their bind params land after every other WHERE param
        for sj in self.spec.semi_joins:
            where.append(self._semi_join_sql(sj))
        return where

    def _semi_join_sql(self, sj: SemiJoin) -> str:
        """``EXISTS (SELECT 1 FROM other o WHERE o.key = a.key AND <conds>)``.

        A test, not a join: the base row count is unchanged however many rows match,
        so a 1:N relationship (one incident, many SLA clocks) cannot inflate an
        ``avg``/``sum``/``count(*)`` measure the way an INNER JOIN would."""
        fqn = sj.table
        if not ("." in fqn and self.loader.get_table(fqn)):
            resolved = self.loader.entity_primary_table(sj.table)
            if not resolved:
                raise BuilderError(
                    f"unknown semi-join table {sj.table!r}: not a known table or entity")
            fqn = resolved
        alias = "sj%d" % self._sj_n
        self._sj_n += 1

        right = sj.key_right
        if not self.loader.column_info(fqn, right):
            raise BuilderError(
                f"semi-join key {right!r} not found in {fqn}; "
                f"available: {sorted(self.loader.table_columns(fqn))[:30]}")
        left = sj.key_left or sj.key_right
        # The left key must exist in the OUTER scope — this is what ties the subquery
        # to the base row, so a missing one is a hard error rather than a dropped test.
        lalias, lci = self._resolve_col(left)
        left_ref = self._ref_from(lalias, lci, left)

        conds = ["%s.%s = %s" % (alias, right, left_ref)]
        for flt in sj.filters:
            conds.append(self._condition(flt, in_table=(fqn, alias)))
        expr = "EXISTS (SELECT 1 FROM %s %s WHERE %s)" % (fqn, alias, " AND ".join(conds))
        self.semi_joined_tables.append(fqn)
        return "NOT %s" % expr if sj.negate else expr

    @staticmethod
    def _is_text_type(type_l: str) -> bool:
        return any(k in type_l for k in ("char", "text", "keyword", "varchar"))

    def _condition(self, flt: Filter,
                   in_table: Optional[Tuple[str, str]] = None) -> str:
        """SQL for one filter. ``in_table`` = (table_fqn, alias) resolves the column
        against THAT table only — used inside an EXISTS subquery, where the column
        belongs to the joined table rather than anything in the outer scope."""
        if flt.op not in _OPS:
            raise BuilderError(f"unsupported op {flt.op!r}; allowed: {sorted(_OPS)}")
        if in_table:
            fqn, alias = in_table
            ci = self.loader.column_info(fqn, flt.field)
            if not ci:
                avail = sorted(self.loader.table_columns(fqn))
                raise BuilderError(
                    f"column {flt.field!r} not found in {fqn}; available: {avail[:30]}")
        else:
            alias, ci = self._resolve_col(flt.field)
        col = self._ref_from(alias, ci, flt.field)
        type_l = (ci.get("type") or "").lower()
        is_array = self._is_array(ci)
        is_text = (not is_array) and self._is_text_type(type_l)
        is_text_array = is_array and self._is_text_type(type_l)
        if flt.op == "not_null":
            return f"{col} IS NOT NULL"

        # Resolve the user's VALUE(S) against the column's declared domain
        # (schema `possible_values`) BEFORE they hit the query: 'completed' ->
        # 'CLOSED', reject an unknown value with the valid list. Only for
        # equality/membership on TEXT columns — ranges/LIKE/numbers are left as-is
        # (a range or substring isn't a member of the enum). Columns with no
        # declared domain pass through unchanged.
        values = list(flt.values)
        if (is_text or is_text_array) and flt.op in ("=", "!=", "in", "not_in") \
                and ci.get("possible_values") and not flt.resolved:
            values = value_resolver.resolve_or_raise(
                flt.field, values, ci.get("possible_values"), BuilderError)

        # Case-insensitivity: for TEXT columns/values we compare lower(col) to
        # lower(value) so a user typing 'apac'/'Emergency' matches stored 'APAC'/
        # 'EMERGENCY'. Numbers, booleans and dates are compared as-is (no lower()).
        # Array columns (e.g. region_name text[]) overlap element-wise.
        if is_array:
            if not values:
                raise BuilderError(f"filter on array column {flt.field!r} needs values")
            if is_text_array:
                # case-insensitive membership: any element (lowered) in the (lowered) set
                self.params.append([str(v).lower() for v in values])
                expr = (f"EXISTS (SELECT 1 FROM unnest({col}) AS _e "
                        f"WHERE lower(_e::text) = ANY({P}))")
            else:
                self.params.append(list(values))           # non-text array: raw overlap
                expr = f"{col} && {P}"
            return f"NOT ({expr})" if flt.op in ("!=", "not_in") else expr

        if is_text and flt.op in ("=", "!="):
            if not values:
                raise BuilderError(f"filter on {flt.field!r} needs a value")
            self.params.append(str(values[0]).lower())
            return f"lower({col}) {flt.op} {P}"
        if is_text and flt.op in ("in", "not_in"):
            if not values:
                raise BuilderError(f"filter on {flt.field!r} with op {flt.op} needs values")
            self.params.append([str(v).lower() for v in values])   # array param
            expr = f"lower({col}) = ANY({P})"
            return f"NOT ({expr})" if flt.op == "not_in" else expr
        if is_text and flt.op == "like":
            self.params.append(str(values[0]).lower() if values else "%")
            return f"lower({col}) LIKE {P}"

        if flt.op in ("in", "not_in"):
            if not values:
                raise BuilderError(f"filter on {flt.field!r} with op {flt.op} needs values")
            kw = "IN" if flt.op == "in" else "NOT IN"
            self.params.append(tuple(values))
            return f"{col} {kw} {P}"
        if flt.op == "like":
            self.params.append(values[0] if values else "%")
            return f"{col} LIKE {P}"
        # scalar comparison (numbers / dates / booleans) — no case folding
        if not values:
            raise BuilderError(f"filter on {flt.field!r} with op {flt.op} needs a value")
        self.params.append(values[0])
        return f"{col} {flt.op} {P}"

    def _select_and_group(self) -> Tuple[List[str], List[str]]:
        sel: List[str] = []
        group: List[str] = []

        # Drill-down: return detail columns, no aggregation.
        if self.spec.drilldown and self.spec.drilldown.detail_columns:
            prefer = self.spec.drilldown.detail_table
            for c in self.spec.drilldown.detail_columns:
                alias, ci = self._resolve_col(c, prefer=prefer)
                sel.append(f"{self._ref_from(alias, ci, c)} AS {c}")
            return sel, []

        # dimensions — a dimension the schema doesn't know is dropped (query runs
        # ungrouped) rather than failing the whole request; recorded for the caller.
        for dim in self.spec.dimensions:
            try:
                alias, ci = self._resolve_col(dim)
            except BuilderError:
                self.dropped_dimensions.append(dim)
                log.info("dropping unknown dimension %r (not in schema scope)", dim)
                continue
            if self._is_array(ci):
                # array dim (e.g. region_name text[]) -> LATERAL unnest to a scalar
                raw = self._ref_from(alias, ci, dim)
                u = f"u{self._lat_n}"
                self._lat_n += 1
                self._laterals.append(f"CROSS JOIN LATERAL unnest({raw}) AS {u}({dim})")
                ref = f"{u}.{dim}"
            else:
                ref = self._ref_from(alias, ci, dim)
            sel.append(f"{ref} AS {dim}")
            group.append(ref)
        # time bucket. A period comparison with no explicit grain buckets by the
        # compared period itself: without it both windows would collapse into one
        # aggregate row and the comparison the user asked for would be invisible.
        grain = self.spec.grain
        if not grain and (self._period_win or {}).get("comparison"):
            grain = self._comparison_grain()
            self.implicit_grain = grain
            log.info("comparison period with no grain -> grouping by %s", grain)
        if grain:
            if grain not in _GRAIN:
                raise BuilderError(f"grain must be one of {sorted(_GRAIN)}")
            date_field = self.spec.date_field or self.loader.table_time_field(self.scope[0][0])
            if not date_field:
                raise BuilderError("grain given but no date_field/time column on base table")
            bucket = f"date_trunc('{grain}', cast({self._col_ref(date_field)} as timestamp))"
            sel.append(f"{bucket} AS bucket")
            group.append(bucket)
        # measure
        sel.append(self._measure_expr())
        return sel, group

    def _measure_expr(self) -> str:
        m = self.spec.measure or Measure(agg="count", column="*")
        alias = m.alias or "value"
        if m.expression:  # trusted raw expression from a metric anchor
            return f"{m.expression} AS {alias}"
        agg = (m.agg or "count").lower()
        if agg not in _AGGS:
            raise BuilderError(f"agg must be one of {sorted(_AGGS)}")
        if agg == "count" and (not m.column or m.column == "*"):
            return f"count(*) AS {alias}"
        if not m.column:
            raise BuilderError(f"measure agg {agg} needs a column")
        col = self._col_ref(m.column)
        if agg == "count_distinct":
            return f"count(distinct {col}) AS {alias}"
        return f"{agg}({col}) AS {alias}"

    def _comparison_grain(self) -> str:
        """The grain that puts each compared side on its own row — derived from how
        long a side is (a month-vs-month comparison groups by month, quarter-vs-
        quarter by quarter), so the bucket boundaries line up with the periods."""
        from datetime import date as _date
        prev = (self._period_win or {}).get("comparison", {}).get("previous", {})
        try:
            days = (_date.fromisoformat(prev["end_date"])
                    - _date.fromisoformat(prev["start_date"])).days + 1
        except (KeyError, TypeError, ValueError):
            return "month"
        if days <= 10:
            return "week"
        if days <= 45:
            return "month"
        return "quarter" if days <= 130 else "year"

    # ---- assembly --------------------------------------------------------
    def build(self) -> BuildResult:
        self._date_window = None
        self._laterals: List[str] = []   # LATERAL unnest clauses for array dimensions
        self._lat_n = 0
        self._sj_n = 0                   # EXISTS subquery alias counter (sj0, sj1, ...)
        self.semi_joined_tables: List[str] = []
        self.dropped_dimensions: List[str] = []
        self.implicit_grain: Optional[str] = None
        # Resolved once up front: both _select_and_group (implicit comparison grain)
        # and _where (one or two windows) need it, and _select_and_group runs first.
        self._period_win = resolve_dates(self.spec.period) if self.spec.period else None
        base_fqn = self._resolve_base()
        self.scope = [(base_fqn, "a")]
        joins_sql = self._plan_joins(base_fqn)
        sel, group = self._select_and_group()
        where = self._where()

        sql = f"SELECT {', '.join(sel)} FROM {base_fqn} a"
        if joins_sql:
            sql += " " + " ".join(joins_sql)
        if self._laterals:
            sql += " " + " ".join(self._laterals)
        if where:
            sql += " WHERE " + " AND ".join(where)
        if group:
            sql += " GROUP BY " + ", ".join(group)

        order_by = self.spec.order_by
        if not order_by and group and not (self.spec.drilldown and self.spec.drilldown.detail_columns):
            if self.implicit_grain:
                order_by = [{"field": "bucket", "direction": "ASC"}]   # oldest period first
            else:
                malias = (self.spec.measure.alias if self.spec.measure else None) or "value"
                order_by = [{"field": malias, "direction": "DESC"}]
        if order_by:
            parts = [f"{o['field']} {o.get('direction', 'ASC')}" for o in order_by]
            sql += " ORDER BY " + ", ".join(parts)

        limit = min(int(self.spec.limit or 200), _MAX_LIMIT)
        sql += f" LIMIT {limit}"

        sql = sql.replace(P, "%s")
        _guard_readonly(sql)
        joined = [t for t, _ in self.scope if t != base_fqn]
        log.debug("built SQL over %s (joins=%s): %s", base_fqn, joined, sql)
        return BuildResult(sql=sql, params=self.params, base_table=base_fqn,
                           joined_tables=joined,
                           semi_joined_tables=self.semi_joined_tables,
                           date_window=self._date_window,
                           dropped_dimensions=self.dropped_dimensions,
                           implicit_grain=self.implicit_grain)


def _guard_readonly(sql: str) -> None:
    stripped = sql.lstrip().lower()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise BuilderError("refusing non-SELECT SQL")
    if ";" in sql:
        raise BuilderError("refusing SQL containing ';'")


def build(spec) -> BuildResult:
    """Validate + build SQL for a QuerySpec (dict or QuerySpec). See module docs."""
    return _Builder(_norm_spec(spec)).build()
