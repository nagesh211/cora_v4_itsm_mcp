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
from typing import Any, Dict, List, Optional, Tuple

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
DEFAULT_PREDICATES_PATH = os.path.join(_ROOT, "predicates.json")
# Machine-written breadth layer; absent is normal (nothing generated yet).
DEFAULT_GENERATED_PATH = os.path.join(_ROOT, "predicates.generated.json")

# Distinguishes "caller did not mention generated_path" (use the default) from an
# explicit generated_path=None (load the curated file alone — what tests want).
_UNSET = object()

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

    ``note`` is maintainer-facing (how the values were verified). ``caveat`` is
    **user-facing**: the way this binding is knowingly not an exact expression of the
    concept. :func:`cora_mcp.composer.plan` copies it into ``composition.notes`` for
    whichever binding actually ran, so the answer says so instead of presenting a
    superset/subset as exact.

    ``date_field`` is which timestamp column the period should apply to WHEN this
    predicate is what scoped the query — e.g. "closed" implies the closed clock, not
    the table's generic default (which is usually the created/opened clock). Only used
    when the caller did not pass an explicit ``date_field`` to `compose_metric`.

    ``semi_joins`` is a list of EXISTS specs (same shape as
    :class:`cora_mcp.sql_builder.SemiJoin`: ``table``, ``key_left``, ``key_right``,
    ``filters``, ``negate``) that belong to THIS binding specifically — for a scoping
    condition that lives on a *different* table than the one this binding is on (e.g.
    "support group belongs to an IT service area", which requires an EXISTS against
    ``itsm.tbl_group_hierarchy`` even though the binding itself is on the outage
    table). This is distinct from :func:`cora_mcp.composer._semi_join_for`, which
    builds an EXISTS for a predicate that binds to NO table other than one reached via
    semi-join at all — here the binding already applies directly, and the semi-join is
    just one more AND-ed condition it carries.
    """

    __slots__ = ("table", "conditions", "note", "primary", "caveat", "semi_joins",
                 "date_field")

    def __init__(self, table: str, conditions: List[dict], note: Optional[str] = None,
                 primary: bool = False, caveat: Optional[str] = None,
                 semi_joins: Optional[List[dict]] = None,
                 date_field: Optional[str] = None):
        self.table = table
        self.conditions = conditions or []
        self.note = note
        self.primary = bool(primary)
        self.caveat = caveat
        self.semi_joins = semi_joins or []
        self.date_field = date_field

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
                                          primary=b.get("primary", False),
                                          caveat=b.get("caveat"),
                                          semi_joins=b.get("semi_joins"),
                                          date_field=b.get("date_field"))

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
    """Two layers, loaded generated-first then curated-on-top.

    ``predicates.generated.json`` (optional, written by ``tools/gen_predicates.py``)
    supplies BREADTH: one predicate per measured (column, value) across every table that
    carries the pair. It exists because the hand-written file covered 11 of 31 tables —
    two whole entities had no qualifier vocabulary, which is how a question resolves to
    the nearest predicate that happens to exist instead of the right one.

    ``predicates.json`` supplies JUDGMENT and always wins: business synonyms a generator
    cannot invent (``mi``, ``sev1``, ``out of sla``, ``risky change``), FREE bindings
    ("every row of this table already is a major incident"), composite scopes, and
    cross-table unification where the stored type differs per table.

    The two layers have deliberately different strictness. A duplicate synonym inside the
    curated file is a **config error** and raises — two hand-written predicates claiming
    one phrase means a question silently gets one of two populations. A generated term
    that collides with anything already claimed is **dropped and logged**: the generated
    file is machine output over hundreds of values, and a collision there must never stop
    the service from starting. Every drop is retrievable via :meth:`shadowed_terms` so
    the coverage report can show what the overlay is masking.
    """

    def __init__(self, path: Optional[str] = None,
                 generated_path: Optional[str] = _UNSET):
        self.path = path or DEFAULT_PREDICATES_PATH
        self.generated_path = (DEFAULT_GENERATED_PATH if generated_path is _UNSET
                               else generated_path)
        self._by_name: Dict[str, Predicate] = {}
        self._index: Dict[str, str] = {}      # normalized synonym/name -> predicate name
        self._generated: set = set()          # names that came from the generated layer
        self._shadowed: Dict[str, Tuple[str, str]] = {}   # term -> (dropped_from, kept)
        self._load()

    def _load(self) -> None:
        # 1) generated layer — breadth, never fatal
        if self.generated_path and os.path.isfile(self.generated_path):
            gen = json.load(open(self.generated_path, encoding="utf-8")) or {}
            for name, spec in (gen.get("predicates") or {}).items():
                try:
                    pred = Predicate(name, spec or {})
                except PredicateConfigError as exc:
                    log.warning("skipping generated predicate %r: %s", name, exc)
                    continue
                self._by_name[name] = pred
                self._generated.add(name)
                self._claim(name, [name, *pred.synonyms], strict=False)
            log.info("generated predicates loaded from %s: %d",
                     self.generated_path, len(self._generated))

        # 2) curated layer — judgment, wins every conflict
        if not os.path.isfile(self.path):
            log.warning("no predicates.json at %s; curated overlay is absent", self.path)
            return
        doc = json.load(open(self.path, encoding="utf-8")) or {}
        curated = (doc.get("predicates") or {})
        for name, spec in curated.items():
            pred = Predicate(name, spec or {})
            self._by_name[name] = pred
            self._generated.discard(name)     # a curated entry of the same name replaces
            # Take every term for the curated predicate, evicting a generated claim.
            self._claim(name, [name, *pred.synonyms], strict=True,
                        curated_names=set(curated))
        log.info("predicates loaded: %d predicate(s) (%d generated, %d curated), "
                 "%d term(s), %d binding(s), %d shadowed term(s)",
                 len(self._by_name), len(self._generated), len(curated),
                 len(self._index), sum(len(p.tables()) for p in self._by_name.values()),
                 len(self._shadowed))

    def _claim(self, name: str, terms: List[str], strict: bool,
               curated_names: Optional[set] = None) -> None:
        """Point every term at ``name``.

        ``strict`` (the curated layer) raises when the term is already owned by another
        CURATED predicate — that is an authoring mistake worth failing loudly. A term
        held by a generated predicate is simply taken over, because the curated phrasing
        is the one a user actually says."""
        for term in terms:
            n = normalize(term)
            if not n:
                continue
            prev = self._index.get(n)
            if prev is None or prev == name:
                self._index[n] = name
                continue
            prev_is_curated = curated_names is not None and prev in curated_names
            if strict and prev_is_curated:
                raise PredicateConfigError(
                    f"synonym {term!r} maps to both {prev!r} and {name!r}")
            if strict:                     # curated evicts a generated claim
                self._shadowed[n] = (prev, name)
                self._index[n] = name
                log.debug("curated %r takes term %r from generated %r", name, term, prev)
            else:                          # generated yields to whatever holds it
                self._shadowed[n] = (name, prev)
                log.debug("generated %r yields term %r to %r", name, term, prev)

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

    def is_generated(self, name: str) -> bool:
        """True when this predicate came from the machine-written breadth layer and no
        curated entry replaced it. Callers use it to present the curated vocabulary
        first — a generated name like ``type_description_emergency`` is correct but is
        not what a person says."""
        return name in self._generated

    def curated_names(self) -> List[str]:
        return sorted(n for n in self._by_name if n not in self._generated)

    def shadowed_terms(self) -> Dict[str, Tuple[str, str]]:
        """{term -> (predicate that lost it, predicate that holds it)} — every phrase one
        layer gave up. Reported by ``--coverage`` so masking is visible rather than a
        silent behaviour difference between two deployments."""
        return dict(self._shadowed)

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


def coverage() -> Dict[str, Any]:
    """Which tables and scope columns have no predicate — offline, schema-only.

    Exists because the gap was invisible. An audit found the hand-written file covering
    11 of 31 declared tables, and nothing reported that: a question about an uncovered
    table simply resolved to the nearest predicate that did exist, which is how
    "incident ids which impacted availability percentage" came back as major incidents.

    Reports rather than judges — a table legitimately has no predicate (a join table, a
    company lookup) — so this is for review, not a build failure. ``--verify`` is the
    gate; this is the map.
    """
    from cora_mcp.schema_loader import get_loader
    try:
        from tools.gen_predicates import _is_scope_column
    except Exception:                        # tools/ not importable (installed package)
        _is_scope_column = None

    loader = get_loader()
    reg = get_registry()
    bound_tables = {b.table for n in reg.names() for b in reg.get(n).bindings()}
    bound_cols = {(b.table, c["field"]) for n in reg.names()
                  for b in reg.get(n).bindings() for c in b.conditions}

    tables: List[Dict[str, Any]] = []
    for _mod, slug, ent in loader.all_entities():
        for t in ent.get("tables") or []:
            fqn = t.get("name")
            cols = [c.get("name") for c in t.get("columns") or []
                    if (c.get("role") or "dimension") == "dimension"]
            scope_cols = ([c for c in cols if _is_scope_column(c)]
                          if _is_scope_column else [])
            tables.append({
                "table": fqn,
                "entity": ent.get("name"),
                "covered": fqn in bound_tables,
                "scope_columns": scope_cols,
                "uncovered_scope_columns": [c for c in scope_cols
                                            if (fqn, c) not in bound_cols],
            })
    uncovered = [t for t in tables if not t["covered"]]
    return {
        "tables": tables,
        "table_count": len(tables),
        "covered_tables": len(tables) - len(uncovered),
        "uncovered_tables": [t["table"] for t in uncovered],
        "curated": len(reg.curated_names()),
        "generated": len(reg.names()) - len(reg.curated_names()),
        "shadowed_terms": reg.shadowed_terms(),
    }


def _main() -> int:  # pragma: no cover - CLI helper
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true",
                    help="re-measure every binding against the live database "
                         "(exits non-zero on a mismatch — use this as the CI gate)")
    ap.add_argument("--coverage", action="store_true",
                    help="report tables and scope columns with no predicate (offline)")
    ap.add_argument("--connection", default="vtx5")
    args = ap.parse_args()

    reg = get_registry()

    if args.coverage:
        rep = coverage()
        print("%d predicate(s): %d curated, %d generated"
              % (len(reg.names()), rep["curated"], rep["generated"]))
        print("tables: %d declared, %d with at least one predicate\n"
              % (rep["table_count"], rep["covered_tables"]))
        print("%-46s %-18s %s" % ("TABLE", "ENTITY", "STATUS"))
        for t in rep["tables"]:
            if t["covered"] and not t["uncovered_scope_columns"]:
                status = "covered"
            elif t["covered"]:
                status = "partial — no predicate on: %s" % ", ".join(
                    t["uncovered_scope_columns"][:6])
            elif t["scope_columns"]:
                status = "NO PREDICATE — scope columns present: %s" % ", ".join(
                    t["scope_columns"][:6])
            else:
                status = "no predicate (no scope column — expected for a join/lookup)"
            print("%-46s %-18s %s" % (t["table"], t["entity"], status))
        if rep["shadowed_terms"]:
            print("\nterms one layer gave up (curated wins):")
            for term, (lost, kept) in sorted(rep["shadowed_terms"].items()):
                print("   %-34s %s -> %s" % (term, lost, kept))
        return 0

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