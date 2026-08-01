"""Scope-predicate resolution — the reusable WHERE-clause half of the semantic layer.

A KPI config says *what to aggregate*; a **predicate** says *over which subset*. The
distinction is the whole point: a user phrase like "major" or "sla breached" reads like
a metric name but is really a filter, and resolving it to a KPI produces a confidently
wrong number (see ``MULTI_METRIC_ANALYSIS.md`` §1).

This module loads ``predicates.json`` and answers two questions deterministically:

  * ``resolve(term)``           -> which predicate does this user phrase mean?
  * ``pred.binding_for(table)`` -> how does it compile against THIS table?

The per-table binding is what makes the layer correct rather than merely plausible. The
same concept is physically different per table — ``sla_breached`` is integer ``1`` on
``tbl_incident_sla``, varchar ``'1'`` on ``tbl_sla_response``, boolean ``true`` on
``tbl_tableau_major_incdnt`` and varchar ``'TRUE'`` on the service-request table — so a
predicate can never be a single global string. A binding whose ``conditions`` list is
**empty** means the table is already scoped that way (every row of
``tbl_major_incidents`` is a major incident), so the predicate costs no WHERE clause and
is satisfied purely by *choosing* that table.

Binding values are **DB-verified and used verbatim**: callers mark the emitted filters
``resolved`` so :mod:`cora_mcp.sql_builder` skips ``value_resolver``. That is deliberate.
``schema_v3.yaml``'s ``possible_values`` were measured against the database and disagree
on many columns (``active_indicator_type`` is *not* ``['NO','YES']``; ``status_name`` on
``tbl_all_incidents`` really holds ``OPENED``, which the schema omits entirely), so
re-resolving a known-good predicate value against that domain would reject it. Run
``python -m cora_mcp.predicate_registry --verify`` after any data-model change to
re-measure every binding against the live DB.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
DEFAULT_PREDICATES_PATH = os.path.join(_ROOT, "predicates.json")

_SEP = re.compile(r"[\s_\-]+")


def normalize(term: str) -> str:
    """Lowercase and collapse space/underscore/hyphen runs — same folding as
    :mod:`cora_mcp.filter_aliases`, so ``"SLA_Breached"`` == ``sla breached``."""
    return _SEP.sub(" ", (term or "").strip().lower())


class PredicateConfigError(ValueError):
    """``predicates.json`` is malformed (e.g. one synonym meaning two predicates)."""


class Binding:
    """How one predicate compiles against one table.

    ``conditions`` is a list of ``{field, op, values}`` dicts. An **empty** list means
    the table is inherently scoped this way — the predicate is free.

    ``primary`` marks the authoritative table for the concept. It matters when several
    tables can express the same predicate but are not equivalent: a generic "SLA
    breached" must resolve to the table covering *both* the response and resolution
    clocks, not to whichever single-clock table happens to sort first. Without this the
    tie-break is arbitrary and the answer silently narrows.
    """

    __slots__ = ("table", "conditions", "note", "primary")

    def __init__(self, table: str, conditions: List[dict], note: Optional[str] = None,
                 primary: bool = False):
        self.table = table
        self.conditions = conditions or []
        self.note = note
        self.primary = bool(primary)

    @property
    def is_free(self) -> bool:
        """True when the table already satisfies the predicate with no WHERE clause."""
        return not self.conditions

    def as_filters(self) -> List[dict]:
        """Builder-ready filter dicts. ``resolved=True`` tells sql_builder these values
        are already the real stored values and must NOT be re-resolved against the
        column's (unreliable) declared domain."""
        return [{"field": c["field"], "op": c.get("op", "="),
                 "values": list(c.get("values") or []), "resolved": True}
                for c in self.conditions]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "Binding(%s, %d cond%s)" % (self.table, len(self.conditions),
                                           " FREE" if self.is_free else "")


class Predicate:
    __slots__ = ("name", "synonyms", "entity", "grain_key", "negated_by", "_bindings")

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.synonyms = list(spec.get("synonyms") or [])
        self.entity = spec.get("entity")
        self.grain_key = spec.get("grain_key")
        self.negated_by = spec.get("negated_by")
        self._bindings: Dict[str, Binding] = {}
        for b in spec.get("bindings") or []:
            tbl = b.get("table")
            if not tbl:
                raise PredicateConfigError(
                    f"predicate {name!r} has a binding with no table")
            self._bindings[tbl] = Binding(tbl, b.get("conditions") or [], b.get("note"),
                                          primary=b.get("primary", False))

    def binding_for(self, table: str) -> Optional[Binding]:
        """The binding for ``table``, or None if this predicate cannot bind there."""
        return self._bindings.get(table)

    def tables(self) -> List[str]:
        """Every table this predicate can bind to."""
        return list(self._bindings)

    def bindings(self) -> List[Binding]:
        return list(self._bindings.values())

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "Predicate(%s, entity=%s, %d table(s))" % (
            self.name, self.entity, len(self._bindings))


class PredicateRegistry:
    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_PREDICATES_PATH
        self._by_name: Dict[str, Predicate] = {}
        self._index: Dict[str, str] = {}      # normalized synonym/name -> predicate name
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            log.warning("no predicates.json at %s; predicate resolution disabled", self.path)
            return
        doc = json.load(open(self.path, encoding="utf-8")) or {}
        for name, spec in (doc.get("predicates") or {}).items():
            pred = Predicate(name, spec or {})
            self._by_name[name] = pred
            for term in [name, *pred.synonyms]:
                n = normalize(term)
                prev = self._index.get(n)
                if prev is not None and prev != name:
                    raise PredicateConfigError(
                        f"synonym {term!r} maps to both {prev!r} and {name!r}")
                self._index[n] = name
        log.info("predicates loaded: %d predicate(s), %d term(s), %d binding(s)",
                 len(self._by_name), len(self._index),
                 sum(len(p.tables()) for p in self._by_name.values()))

    # ---- lookup ----------------------------------------------------------
    def get(self, name: str) -> Optional[Predicate]:
        return self._by_name.get(name)

    def resolve(self, term: str) -> Optional[Predicate]:
        """Predicate for a user phrase (name or synonym), or None. Whole-string match
        only — never a substring — so "sub business" can't collide with "business"."""
        name = self._index.get(normalize(term))
        return self._by_name.get(name) if name else None

    def names(self) -> List[str]:
        return sorted(self._by_name)

    def for_entity(self, entity: str) -> List[Predicate]:
        return [p for p in self._by_name.values() if p.entity == entity]

    def predicates_bindable_on(self, table: str) -> List[Predicate]:
        """Every predicate that can bind to ``table`` — used by discovery tools to
        advertise the extra qualifiers a dataset supports."""
        return [p for p in self._by_name.values() if p.binding_for(table)]

    def scan(self, text: str, max_terms: int = 8) -> List[Predicate]:
        """Find predicate phrases occurring in free text, longest phrase first so
        "major incident" wins over "major". Returns each predicate at most once.

        This is a *candidate* scan for discovery/telemetry. The authoritative path is
        the caller passing predicate names explicitly (the agent identifies, the tools
        resolve — the contract in DIMENSIONS_FILTERS_PLAN.md)."""
        padded = " %s " % normalize(text)
        hits: List[Predicate] = []
        seen = set()
        for term in sorted(self._index, key=len, reverse=True):
            if len(hits) >= max_terms:
                break
            name = self._index[term]
            if name in seen:
                continue
            if " %s " % term in padded:
                seen.add(name)
                hits.append(self._by_name[name])
        return hits


@lru_cache(maxsize=1)
def get_registry() -> PredicateRegistry:
    return PredicateRegistry()


# ---------------------------------------------------------------------------
# Verification — re-measure every binding against the live database.
# ---------------------------------------------------------------------------
async def verify(connection: str = "vtx5", dialect: str = "postgres") -> Dict[str, Any]:
    """Check every binding against the real database and report what does not hold.

    This exists because ``schema_v3.yaml`` was measured to disagree with the database on
    both column existence and value domains. A predicate whose value matches no row is
    exactly the silent-zero failure this layer prevents, so it must be caught here and
    not in a user's answer.

    Each condition is an **existence probe** (``SELECT 1 ... WHERE col = v LIMIT 1``)
    rather than a ``SELECT DISTINCT`` scan: it answers precisely the question asked, can
    use an index, and short-circuits on the first hit instead of reading the table.

    ``mismatches`` (the value genuinely never occurs) is a data-model defect and sets
    ``ok=False``. ``unreadable`` (connection dropped, table absent) is infrastructure and
    is reported separately — a flaky network must not be mistaken for a bad predicate.
    """
    from cora_mcp import db

    reg = get_registry()
    mismatches: List[str] = []
    unreadable: List[str] = []
    checked = 0
    for pred in reg._by_name.values():
        for b in pred.bindings():
            for cond in b.conditions:
                col = cond.get("field")
                for v in cond.get("values") or []:
                    checked += 1
                    sql = ('SELECT 1 FROM %s WHERE "%s" = %%s LIMIT 1' % (b.table, col))
                    out, err = None, None
                    for _attempt in range(3):
                        # The probe is read-only and idempotent, so a dropped
                        # connection is worth retrying: this link has been observed
                        # to time out mid-run, and a network blip must not be
                        # reported as a data-model defect.
                        try:
                            out = await db.execute(dialect, connection, sql, [v], limit=1)
                            err = None
                            break
                        except Exception as exc:      # DBError or a raw OSError
                            err = exc
                    if out is None:
                        unreadable.append("%s/%s.%s: %s"
                                          % (pred.name, b.table, col, str(err)[:90]))
                        continue
                    if not out.get("rows"):
                        mismatches.append(
                            "%s: %s.%s = %r matches NO rows — predicate would always "
                            "return empty" % (pred.name, b.table, col, v))
    report = {"predicates": len(reg._by_name), "conditions_checked": checked,
              "mismatches": mismatches, "unreadable": unreadable,
              "ok": not mismatches}
    log.info("predicate verify: %d probe(s), %d mismatch(es), %d unreadable",
             checked, len(mismatches), len(unreadable))
    return report


def _main() -> int:  # pragma: no cover - CLI helper
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true",
                    help="re-measure every binding against the live database")
    ap.add_argument("--connection", default="vtx5")
    args = ap.parse_args()

    reg = get_registry()
    if not args.verify:
        for name in reg.names():
            p = reg.get(name)
            free = [b.table for b in p.bindings() if b.is_free]
            print("%-22s entity=%-16s key=%-18s tables=%d%s"
                  % (name, p.entity, p.grain_key, len(p.tables()),
                     "  free on: %s" % ", ".join(free) if free else ""))
        return 0

    async def _run():
        from cora_mcp import db
        rep = await verify(args.connection)
        for m in rep["mismatches"]:
            print("MISMATCH    " + m)
        for u in rep["unreadable"]:
            print("UNREADABLE  " + u)
        print("\n%d predicate(s), %d probe(s), %d mismatch(es), %d unreadable"
              % (rep["predicates"], rep["conditions_checked"],
                 len(rep["mismatches"]), len(rep["unreadable"])))
        print("VERDICT:", "ok" if rep["ok"] else "DATA-MODEL DEFECT")
        await db.close_pools()
        return 0 if rep["ok"] else 1

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())