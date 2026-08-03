"""Join fan-out: detect it, correct what can be corrected, refuse the rest.

A one-to-many join multiplies base rows. If one incident has two SLA clocks, then
``incidents JOIN sla`` holds two rows for that incident, and every measure computed
over the joined result is wrong by the fan-out factor::

    SELECT count(*) FROM tbl_all_incidents a JOIN tbl_incident_sla b ON ...
    -> 302,100      (SLA clocks)
       151,050      (incidents — what the question actually asked for)

Nothing about that result looks broken. It is a plausible number, larger than the
truth, returned with full confidence. That is the failure mode this module exists
to prevent, and it is why a warning is not enough: the existing
``adhoc.preflight`` note ("consider DISTINCT") is advice attached to a field that
nothing downstream reads.

Three kinds of measure, three different treatments:

=========================  ==========================================================
``count(*)``               **Corrected.** ``count(DISTINCT a.ctid)`` counts base rows,
                           not joined rows. ``ctid`` is Postgres' physical row
                           identity — not stable across ``UPDATE``/``VACUUM``, but
                           constant within a single query's snapshot, which is all a
                           row-identity needs to be. Exact, and needs no key
                           declaration in the schema.
``count_distinct``,        **Unchanged.** Naturally fan-out invariant: duplicating a
``min``, ``max``           row cannot change a distinct count, a minimum or a maximum.
``sum``, ``avg``,          **Refused.** These have no exact single-pass correction over
``count(<column>)``        a fanned result — the honest fix is to not fan out at all
                           (an EXISTS semi-join, which :mod:`cora_mcp.composer`
                           already prefers) or to pre-aggregate the many-side.
                           Refusing keeps the invariant the rest of this codebase
                           holds: a constraint that cannot be honoured is reported,
                           never silently applied wrong.
=========================  ==========================================================

**When is an edge assumed to fan out?** Cardinality is a property of the data, and
``schema_v3.yaml`` does not declare it today. So the default is the safe one: an
edge fans out unless it says otherwise. Declare the exception on the relationship::

    - name: release_causes_change
      left: itsm_release.tbl_pepops_release_mgmt
      right: itsm_change.tbl_change
      join_on: {left_col: change_id, right_col: change_id}
      cardinality: many_to_one        # many releases -> one change; safe from the left

``tools/infer_relationships.py`` measures real cardinality against the database and
writes this key, so the assumption gets replaced by a fact rather than an opinion.

``CORA_FANOUT_GUARD`` tunes the behaviour:

  * ``strict`` (default) — correct ``count(*)``, refuse ``sum``/``avg``.
  * ``warn``             — correct ``count(*)``, allow ``sum``/``avg`` with a note.
  * ``off``              — legacy behaviour, no correction and no refusal.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)


class FanoutError(ValueError):
    """A measure cannot be computed correctly across a one-to-many join."""


# Aggregates that a duplicated row cannot change.
SAFE_AGGS = frozenset({"count_distinct", "min", "max"})
# Aggregates that a duplicated row corrupts and that have no exact inline fix.
UNCORRECTABLE_AGGS = frozenset({"sum", "avg"})

# Cardinality values that mean "joining this edge cannot multiply the left rows".
# Read from the perspective of the side already in the FROM clause.
_SAFE_FROM_LEFT = {"one_to_one", "many_to_one"}
_SAFE_FROM_RIGHT = {"one_to_one", "one_to_many"}


def guard_mode() -> str:
    """``strict`` | ``warn`` | ``off`` — see the module docstring."""
    mode = (os.getenv("CORA_FANOUT_GUARD") or "strict").strip().lower()
    if mode not in ("strict", "warn", "off"):
        log.warning("CORA_FANOUT_GUARD=%r is not strict/warn/off; using strict", mode)
        return "strict"
    return mode


# ---------------------------------------------------------------------------
# Which edges can multiply rows
# ---------------------------------------------------------------------------
def edge_fans_out(rel: dict, frm: str) -> bool:
    """Can traversing ``rel`` away from table ``frm`` multiply ``frm``'s rows?

    ``frm`` matters: an edge that is many-to-one in one direction is one-to-many in
    the other, and only the latter fans out.
    """
    # A junction table is many-to-many by construction: one incident can link to
    # several changes and vice versa. No declaration can make that safe.
    if rel.get("via"):
        return True
    declared = (rel.get("cardinality") or "").strip().lower()
    if not declared:
        return True                       # unknown -> assume the unsafe case
    if frm == rel.get("left"):
        return declared not in _SAFE_FROM_LEFT
    if frm == rel.get("right"):
        return declared not in _SAFE_FROM_RIGHT
    return True                           # can't orient it -> assume unsafe


def fanning_relationships(clauses: List[dict], graph, base: str) -> List[str]:
    """Names of the relationships in a join plan that can multiply base rows.

    ``clauses`` is what :meth:`RelationshipGraph.plan_join` returns. The traversal
    direction is recovered from the ``on`` tuples: each clause names the table it
    joins *from*, which is already in scope.
    """
    names: List[str] = []
    for jc in clauses:
        rel_name = jc.get("relationship")
        rel = graph.relationship(rel_name) if rel_name else None
        if not rel:
            names.append(rel_name or "<unnamed>")     # unknown edge -> unsafe
            continue
        # the left-hand side of the first ON tuple is the table we joined from
        on = jc.get("on") or []
        frm = on[0][0] if on else base
        if edge_fans_out(rel, frm) and rel_name not in names:
            names.append(rel_name)
    return names


# ---------------------------------------------------------------------------
# Measure correction
# ---------------------------------------------------------------------------
def correct_measure(
    agg: str,
    column_ref: Optional[str],
    alias: str,
    base_alias: str,
    fanning: List[str],
    home_is_base: bool = True,
) -> Tuple[str, Optional[str]]:
    """Return ``(sql_fragment, note)`` for one measure over a possibly-fanned join.

    ``fanning`` is the list of relationship names that can multiply base rows; an
    empty list means the join is safe and every measure passes through unchanged.

    ``home_is_base`` says whether the measured column lives on the base table. It
    matters, and getting it wrong in either direction is a bug:

      * ``sum(a.outage_minutes)`` over ``incidents JOIN sla`` **is** inflated — the
        base row repeats once per SLA clock, and so does its outage value.
      * ``sum(b.elapsed_seconds)`` over the same join is **not** inflated — the SLA
        clock rows are the natural grain of that column, and summing all of them is
        precisely what the question means.

    So a measure on a joined table passes through untouched, *unless* a second
    fanning edge is present — then that table's own rows are being duplicated too,
    and the value is no longer trustworthy either.

    Raises :class:`FanoutError` for an aggregate that cannot be corrected.
    """
    agg = (agg or "count").lower()
    plain = _plain(agg, column_ref, alias)
    if not fanning:
        return plain, None

    mode = guard_mode()
    if mode == "off":
        return plain, None

    why = ", ".join(fanning)

    if agg in SAFE_AGGS:
        return plain, None

    if agg == "count" and (not column_ref or column_ref == "*"):
        note = (f"counted distinct base rows because the join via {why} can return "
                f"more than one row per base record")
        log.info("fan-out: count(*) -> count(distinct %s.ctid) [%s]", base_alias, why)
        return f"count(DISTINCT {base_alias}.ctid) AS {alias}", note

    # A measure on the many-side is already at its own grain -- provided nothing
    # else in the plan is duplicating that side as well.
    if not home_is_base and len(fanning) == 1:
        return plain, (f"{agg} is measured at the grain of the table joined via {why}, "
                       f"not per base record")

    # count(<column>), sum, avg on the duplicated side -- no exact inline correction.
    if mode == "warn":
        note = (f"WARNING: {agg} is computed over a join via {why} that can duplicate "
                f"base rows, so this value may be inflated")
        log.warning("fan-out: allowing %s across fanning join [%s] (CORA_FANOUT_GUARD=warn)",
                    agg, why)
        return plain, note

    raise FanoutError(
        f"cannot compute {agg} correctly: the join via {why} can return more than one "
        f"row per base record, which would inflate the result. "
        f"Options: (a) use an EXISTS semi-join if the joined table is only being used "
        f"to filter (semi_joins in the spec), (b) measure count instead, which is "
        f"corrected automatically, or (c) declare the edge's real cardinality in "
        f"schema_v3.yaml (run tools/infer_relationships.py to measure it)."
    )


def _plain(agg: str, column_ref: Optional[str], alias: str) -> str:
    if agg == "count" and (not column_ref or column_ref == "*"):
        return f"count(*) AS {alias}"
    if agg == "count_distinct":
        return f"count(distinct {column_ref}) AS {alias}"
    return f"{agg}({column_ref}) AS {alias}"


# ---------------------------------------------------------------------------
# Measured cardinality (used by tools/infer_relationships.py)
# ---------------------------------------------------------------------------
def classify_cardinality(left_unique: bool, right_unique: bool) -> str:
    """Map measured key uniqueness on both sides to a ``cardinality:`` value."""
    if left_unique and right_unique:
        return "one_to_one"
    if right_unique:
        return "many_to_one"        # many left rows -> one right row
    if left_unique:
        return "one_to_many"
    return "many_to_many"