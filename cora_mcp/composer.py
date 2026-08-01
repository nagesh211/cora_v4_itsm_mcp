"""Compose ONE measure with N scope predicates into a single correct query.

This is the layer that was missing. A question like "how many <entity> that are <X> and
<Y>, for <dimension>, over <period>" contains **one measure and several predicates** — it
is one query with AND-ed conditions, never an arithmetic combination of metrics (see
``MULTI_METRIC_ANALYSIS.md`` §1). The hard part is not the SQL, which
:mod:`cora_mcp.sql_builder` already emits safely; it is deciding **which table to anchor
on**, because the predicates and dimensions a question mentions are rarely all stored on
the same table.

The pipeline, all deterministic:

  1. **Resolve** each predicate phrase via :mod:`cora_mcp.predicate_registry`.
  2. **Enumerate** candidate anchors — every table of the entity, plus every table any
     requested predicate can bind to.
  3. **Score** each candidate on what it can satisfy *itself*:
       * hard requirements: the measure's column, and a time column if a period is given
       * how many dimensions/filters it can bind  (a filter it cannot bind is fatal —
         dropping one changes the answer)
       * FREE predicates (the table is inherently scoped that way, so the predicate
         costs no WHERE clause at all)  > DIRECT (a WHERE on this table) > SEMI (an
         EXISTS against another table)
  4. **Residual** predicates become EXISTS semi-joins on the predicate's ``grain_key``.
  5. **Refuse** with a named reason if the best candidate still cannot bind something.

Step 3's ordering is what makes the result cheap as well as correct: choosing a table
that is already scoped to the subset turns a predicate into zero SQL. Step 5 preserves
the invariant the rest of this codebase holds everywhere (``dropped_dim``,
``dropped_filters``, ``dimension_note``) — a constraint that cannot be honoured is
reported, never silently discarded, because dropping "breached" from a breach question
turns a correct small number into a confident large one.

Nothing here writes SQL or guesses at a name: the anchor decision is combinatorics over
declared bindings, and the spec it emits goes through the same validated builder as
every other path.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from cora_mcp.column_resolver import resolve_column, resolvable_words
from cora_mcp.logging_config import get_logger
from cora_mcp.predicate_registry import Predicate, get_registry
from cora_mcp.schema_loader import get_loader

log = get_logger(__name__)


class ComposeError(ValueError):
    """The request cannot be composed into one correct query (with the reason)."""


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------
class Candidate:
    """One possible anchor table, with what it can and cannot satisfy."""

    __slots__ = ("table", "free", "direct", "semi", "unbound_predicates",
                 "bound_filters", "unbound_filters", "bound_dims", "unbound_dims",
                 "bound_select", "unbound_select",
                 "date_field", "measure_ok", "primary_hits", "no_date")

    def __init__(self, table: str):
        self.table = table
        self.free: List[Predicate] = []
        self.direct: List[Predicate] = []
        self.semi: List[Predicate] = []
        self.unbound_predicates: List[str] = []
        self.bound_filters: Dict[str, str] = {}      # user word -> column
        self.unbound_filters: List[str] = []
        self.bound_dims: Dict[str, str] = {}
        self.unbound_dims: List[str] = []
        self.bound_select: Dict[str, str] = {}       # requested detail column -> column
        self.unbound_select: List[str] = []
        self.date_field: Optional[str] = None
        self.measure_ok = True
        self.no_date = False
        self.primary_hits = 0        # predicates for which THIS is the primary table

    @property
    def viable(self) -> bool:
        """A candidate is viable only if it can express the measure, the time window
        and EVERY requested filter. Filters are non-negotiable: silently omitting one
        returns a number that answers a different question."""
        return (self.measure_ok and not self.unbound_filters
                and not self.unbound_predicates)

    @property
    def score(self) -> Tuple:
        """Higher is better, compared left to right:

          1. fewer requested DETAIL columns it cannot project. First because a listing
             exists to show those columns: a table that cannot project the breach flag
             is a poor anchor for "show me the breached ones", even if it satisfies the
             predicate more cheaply.
          2. fewer dimensions it cannot break down by
          3. more predicates for which this is the registry's PRIMARY table — an
             authoritative source beats an equally-bindable but narrower one
          4. more FREE predicates (the table is already scoped, so zero SQL)
          5. more DIRECT predicates (a WHERE here beats an EXISTS elsewhere)
          6. fewer semi-joins
          7. table name, purely so planning is deterministic and reproducible
        """
        return (-len(self.unbound_select), -len(self.unbound_dims),
                self.primary_hits, len(self.free), len(self.direct),
                -len(self.semi), self.table)

    def explain(self) -> str:
        bits = []
        if self.free:
            bits.append("free: " + ", ".join(p.name for p in self.free))
        if self.direct:
            bits.append("direct: " + ", ".join(p.name for p in self.direct))
        if self.semi:
            bits.append("semi-join: " + ", ".join(p.name for p in self.semi))
        return "%s (%s)" % (self.table, "; ".join(bits) or "no predicates")


def _entity_tables(entity: Optional[str]) -> List[str]:
    """Every table belonging to ``entity`` (by slug or bare entity name)."""
    loader = get_loader()
    out: List[str] = []
    for _mod, slug, ent in loader.all_entities():
        if entity and entity not in (slug, ent.get("name")):
            continue
        for t in ent.get("tables") or []:
            if t.get("name") and t["name"] not in out:
                out.append(t["name"])
    return out


def _resolve_predicates(terms: List[str]) -> Tuple[List[Predicate], List[str]]:
    reg = get_registry()
    found: List[Predicate] = []
    unknown: List[str] = []
    for term in terms or []:
        p = reg.resolve(term)
        if p is None:
            unknown.append(term)
        elif p.name not in {f.name for f in found}:
            found.append(p)
    return found, unknown


_DETAIL_ROLES = ("identifier", "dimension", "timestamp")
_DEFAULT_DETAIL_MAX = 12


def default_detail_columns(table: str, grain_key: Optional[str] = None) -> List[str]:
    """A sensible column set for "show me the records" when the caller names none.

    Exists because a model asked to invent detail columns invents *plausible* ones — a
    real request asked for ``incident_number``, ``short_description``, ``priority``,
    ``opened_by`` and ``incident_state``, none of which exist on the table (the real
    names are ``incident_id``, ``description_text``, ``priority_code``, ``full_name``,
    ``status_name``). Deriving the list from schema roles removes the guesswork.
    """
    cols = get_loader().table_columns(table)
    picked: List[str] = []
    if grain_key and grain_key in cols:
        picked.append(grain_key)
    # identifiers first, then business dimensions, then the timestamps
    for role in _DETAIL_ROLES:
        for name, ci in cols.items():
            if len(picked) >= _DEFAULT_DETAIL_MAX:
                break
            if name in picked:
                continue
            if (ci.get("role") or "dimension") != role:
                continue
            if "system_id" in name or name.startswith("dw_"):
                continue           # internal surrogate keys are noise in a listing
            picked.append(name)
    return picked[:_DEFAULT_DETAIL_MAX]


def _score_candidate(table: str, preds: List[Predicate], filters: Dict[str, Any],
                     dimensions: List[str], measure_column: Optional[str],
                     need_date: bool, date_field: Optional[str] = None,
                     select: Optional[List[str]] = None) -> Candidate:
    loader = get_loader()
    cand = Candidate(table)
    cols = loader.table_columns(table)

    if measure_column and measure_column not in ("*", None):
        cand.measure_ok = measure_column in cols

    for word in select or []:
        # roles=() — a listing may legitimately show an id or a timestamp
        col = resolve_column(table, word, roles=())
        if col:
            cand.bound_select[word] = col
        else:
            cand.unbound_select.append(word)
    if need_date:
        # An explicit date_field is a requirement, not a hint: "opened last month" and
        # "closed last month" are different questions, so a table that lacks the named
        # column must not silently answer with a different clock.
        if date_field:
            cand.date_field = date_field if date_field in cols else None
        else:
            cand.date_field = loader.table_time_field(table)
            if not cand.date_field or cand.date_field not in cols:
                cand.date_field = next(
                    (n for n, ci in cols.items() if ci.get("role") == "timestamp"), None)
        if not cand.date_field:
            cand.no_date = True
            cand.measure_ok = False        # cannot honour the period here

    for word in filters or {}:
        col = resolve_column(table, word)
        if col:
            cand.bound_filters[word] = col
        else:
            cand.unbound_filters.append(word)

    for word in dimensions or []:
        col = resolve_column(table, word, roles=("dimension",))
        if col:
            cand.bound_dims[word] = col
        else:
            cand.unbound_dims.append(word)

    for p in preds:
        b = p.binding_for(table)
        if b is not None:
            if b.primary:
                cand.primary_hits += 1
            (cand.free if b.is_free else cand.direct).append(p)
            continue
        # Not on this table — can it be tested on another one at the same grain?
        if p.grain_key and p.grain_key in cols and p.tables():
            cand.semi.append(p)
        else:
            cand.unbound_predicates.append(p.name)
    return cand


def _refusal(scored: List[Candidate], measure_column: Optional[str],
             date_field: Optional[str]) -> str:
    """Explain why nothing worked, reporting only the reason that actually blocked the
    closest candidate — a message that lists every possible cause is as unhelpful as
    no message at all.

    Diagnoses in order of how fundamental the problem is: no table carries the measure
    at all, then no table has the requested clock, then the specific filter/predicate
    that could not bind on the otherwise-best table.
    """
    if not scored:
        return "cannot compose this question: no candidate tables at all"
    tried = [c.table for c in scored]

    with_measure = [c for c in scored if measure_column in (None, "*")
                    or measure_column in get_loader().table_columns(c.table)]
    if not with_measure:
        return (f"cannot compose this question: no candidate table has the measure "
                f"column {measure_column!r}. Tried {tried}.")

    with_date = [c for c in with_measure if not c.no_date]
    if not with_date:
        named = f" named {date_field!r}" if date_field else ""
        return (f"cannot compose this question: no candidate table carrying "
                f"{measure_column!r} also has a time column{named}, so the period "
                f"cannot be applied. Tried {[c.table for c in with_measure]}.")

    best = max(with_date, key=lambda c: c.score)
    if best.unbound_filters:
        offered = resolvable_words(best.table)[:25]
        return (f"cannot compose this question: filter(s) {best.unbound_filters} exist "
                f"on none of the candidate tables {[c.table for c in with_date]}. "
                f"Applying the rest would answer a different question, so nothing was "
                f"run. {best.table} accepts: {offered}")
    if best.unbound_predicates:
        return (f"cannot compose this question: predicate(s) "
                f"{best.unbound_predicates} share no grain key with the candidate "
                f"tables {[c.table for c in with_date]}, so they cannot be tested.")
    return f"cannot compose this question: no viable anchor among {tried}"


def _semi_join_for(pred: Predicate, anchor: str) -> Dict[str, Any]:
    """Build the EXISTS spec that tests ``pred`` on another table.

    Prefers a binding with real conditions over a "free" table: a free table means
    *membership implies the predicate*, so ``EXISTS(that table)`` is also valid, but a
    conditional binding is usually the narrower, better-indexed test."""
    loader = get_loader()
    # The joined table must actually carry the grain key, or the EXISTS cannot be tied
    # back to the base row. Filtering here (rather than letting the builder fail) keeps
    # the refusal explainable.
    bindings = [b for b in pred.bindings()
                if b.table != anchor and loader.column_info(b.table, pred.grain_key)]
    if not bindings:
        raise ComposeError(
            f"predicate {pred.name!r} cannot be evaluated relative to {anchor!r}: no "
            f"other table binding it carries the grain key {pred.grain_key!r}")
    bindings.sort(key=lambda b: (not b.primary, b.is_free, b.table))
    b = bindings[0]
    return {"table": b.table, "key_right": pred.grain_key, "key_left": pred.grain_key,
            "filters": b.as_filters(), "label": pred.name}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def plan(
    entity: Optional[str] = None,
    measure: Optional[Dict[str, Any]] = None,
    predicates: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    dimensions: Optional[List[str]] = None,
    period: Optional[str] = None,
    grain: Optional[str] = None,
    base: Optional[str] = None,
    date_field: Optional[str] = None,
    select: Optional[List[str]] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Choose an anchor table and return a builder-ready QuerySpec plus the reasoning.

    Raises :class:`ComposeError` when no table can satisfy the request — with the
    specific thing that could not be bound, so the caller can say why rather than
    returning a number that quietly ignores part of the question.
    """
    preds, unknown = _resolve_predicates(predicates or [])
    if unknown:
        known = get_registry().names()
        raise ComposeError(
            f"unknown scope predicate(s) {unknown}. known predicates: {known}")

    if entity is None:
        entities = {p.entity for p in preds if p.entity}
        if len(entities) > 1:
            raise ComposeError(
                f"predicates span multiple entities {sorted(entities)}; these are "
                f"different populations and cannot be intersected in one query. "
                f"Ask them as separate questions.")
        entity = next(iter(entities), None) if entities else None

    # A `select` list means the user wants ROWS, not a number: no measure, no group-by.
    # `is not None` rather than truthiness, so an EMPTY list still means "a listing,
    # with default columns" instead of falling back to an aggregate.
    listing = select is not None
    grain_key = next((p.grain_key for p in preds if p.grain_key), None)
    if listing:
        measure, measure_column = None, None
    else:
        if measure is None:
            # Default to count(distinct <entity key>) rather than count(*). The anchor
            # may hold several rows per entity (an SLA table has one row per clock), and
            # count(*) would then silently report clocks as if they were incidents.
            measure = ({"agg": "count_distinct", "column": grain_key, "alias": "v"}
                       if grain_key else {"agg": "count", "column": "*", "alias": "v"})
        measure = dict(measure)
        measure.setdefault("alias", "v")
        measure_column = measure.get("column")

    # Candidate anchors: the entity's tables plus every table a predicate can bind to.
    candidates: List[str] = []
    if base:
        candidates = [base]
    else:
        for t in _entity_tables(entity):
            candidates.append(t)
        for p in preds:
            for t in p.tables():
                if t not in candidates:
                    candidates.append(t)
    if not candidates:
        raise ComposeError(
            f"no candidate tables for entity {entity!r}; pass `base` explicitly")

    scored = [_score_candidate(t, preds, filters or {}, dimensions or [],
                               measure_column, bool(period or grain), date_field,
                               select=select)
              for t in candidates]
    viable = [c for c in scored if c.viable]
    if not viable:
        raise ComposeError(_refusal(scored, measure_column, date_field))

    winner = max(viable, key=lambda c: c.score)

    spec_filters: List[Dict[str, Any]] = []
    for word, value in (filters or {}).items():
        vals = list(value) if isinstance(value, (list, tuple)) else [value]
        spec_filters.append({"field": winner.bound_filters[word],
                             "op": "in" if len(vals) > 1 else "=", "values": vals})
    # Predicates that bind here become plain AND-ed conditions with DB-verified values.
    for p in winner.direct:
        b = p.binding_for(winner.table)
        spec_filters.extend(b.as_filters())

    semi_joins = [_semi_join_for(p, winner.table) for p in winner.semi]

    spec: Dict[str, Any] = {
        "base": winner.table,
        "measure": measure,
        "dimensions": [winner.bound_dims[d] for d in (dimensions or [])
                       if d in winner.bound_dims],
        "filters": spec_filters,
        "semi_joins": semi_joins,
        "period": period,
        "date_field": winner.date_field,
        "grain": grain,
        "limit": limit,
    }
    if listing:
        # Detail listing: project columns, no aggregate and no GROUP BY. The semi-joins
        # still apply, which is the point — a qualified listing needs no relationship
        # graph, only the EXISTS test the count already uses.
        detail = [winner.bound_select[w] for w in (select or [])
                  if w in winner.bound_select]
        if not detail:
            detail = default_detail_columns(winner.table, grain_key)
        spec["measure"] = None
        spec["dimensions"] = []
        spec["drilldown"] = {"detail_columns": detail}

    notes: List[str] = []
    if winner.free:
        notes.append(
            "%s already contains only %s, so %s needed no filter."
            % (winner.table, " / ".join(p.name for p in winner.free),
               "they" if len(winner.free) > 1 else "it"))
    if winner.semi:
        notes.append(
            "%s evaluated with an EXISTS test against %s (a row filter, not a join, so "
            "one-to-many rows cannot inflate the measure)."
            % (", ".join(p.name for p in winner.semi),
               ", ".join(sj["table"] for sj in semi_joins)))
    if winner.unbound_dims:
        notes.append(
            "breakdown by %s is not available on %s, so it was not applied."
            % (winner.unbound_dims, winner.table))
    if listing:
        if winner.unbound_select:
            notes.append(
                "column(s) %s do not exist on %s and were left out of the listing; "
                "available: %s"
                % (winner.unbound_select, winner.table,
                   resolvable_words(winner.table)[:25]))
        if winner.semi:
            notes.append(
                "columns from %s cannot be shown: %s was applied as an EXISTS test, "
                "which filters rows without joining them. Ask for those columns "
                "explicitly to anchor on that table instead."
                % (", ".join(sj["table"] for sj in semi_joins),
                   ", ".join(p.name for p in winner.semi)))
        # Row-grain caveat, but only where it can actually bite. A table on which the
        # entity predicate binds for FREE is a dedicated entity table (one row per
        # entity), so warning there would be a false alarm. A table where the predicate
        # needed a WHERE clause is typically a child table — an SLA table holds one row
        # per clock — and there a single entity really can repeat.
        pk = next((n for n, ci in get_loader().table_columns(winner.table).items()
                   if ci.get("primary_key")), None)
        if grain_key and pk and pk != grain_key and not winner.free:
            notes.append(
                "rows are at %s grain (keyed on %s), not one row per %s — a single %s "
                "may therefore appear more than once."
                % (winner.table, pk, grain_key, grain_key))

    result = {
        "entity": entity,
        "anchor_table": winner.table,
        "shape": "listing" if listing else "aggregate",
        "spec": spec,
        "predicates": {
            "free": [p.name for p in winner.free],
            "direct": [p.name for p in winner.direct],
            "semi_join": [p.name for p in winner.semi],
        },
        "dropped_dimensions": winner.unbound_dims or None,
        "notes": notes,
        "considered": [c.explain() for c in sorted(viable, key=lambda c: c.score,
                                                   reverse=True)[:6]],
    }
    log.info("compose entity=%s -> anchor=%s free=%s direct=%s semi=%s dropped_dims=%s",
             entity, winner.table, result["predicates"]["free"],
             result["predicates"]["direct"], result["predicates"]["semi_join"],
             winner.unbound_dims)
    return result


async def compose_and_run(connection: str = "vtx5", dialect: str = "postgres",
                          **kwargs) -> Dict[str, Any]:
    """:func:`plan` then execute through the normal validated path."""
    from cora_mcp.query_engine import run_dataset_query

    planned = plan(**kwargs)
    out = await run_dataset_query(planned["spec"],
                                  connection=connection, dialect=dialect,
                                  limit=kwargs.get("limit", 200))
    out["composition"] = {k: planned[k] for k in
                          ("entity", "anchor_table", "shape", "predicates", "notes",
                           "dropped_dimensions", "considered")}
    return out