#!/usr/bin/env python3
"""Generate ``predicates.generated.json`` by MEASURING column domains in the database.

Why this exists
---------------
``predicates.json`` was hand-transcribed, and an audit found it covering **11 of 31
tables**: two entire entities (``ansible_automation``, ``automation_index`` — 7 tables)
had no qualifier vocabulary at all, so any question about them resolved to the nearest
predicate that *did* exist. That is the same failure mode that answered "incident ids
which impacted availability percentage" out of the major-incident table.

31 of the 34 hand-written bindings are a single column equal to a single value — pure
mechanical shape — while ``schema_v3.yaml`` already declares 141 enum dimension columns
holding 373 values. So the *coverage* half is generatable and the transcription is what
does not scale.

What is NOT generatable, and is why ``predicates.json`` survives as an overlay:

  * business synonyms — ``mi``, ``sev1``, ``out of sla``, ``risky change``,
    ``traditional release``, ``unfulfilled request``, ``outstanding``
  * FREE bindings — "every row of ``tbl_major_incidents`` already is a major incident",
    a table-selection fact no column value expresses
  * composite scopes — ``availability_impacting`` is two conditions chosen to match a
    KPI's population
  * cross-table unification where the stored *type* differs per table
    (``sla_breached`` is int ``1`` / varchar ``'1'`` / bool ``true``)

Why it measures instead of reading the schema
---------------------------------------------
``schema_v3.yaml``'s ``possible_values`` is known-wrong in ways that would mass-produce
broken predicates: ``outage_type`` declares 1 value where the database holds NULL
(304,696 rows) and ``'OUTAGE'`` (16,118); ``target`` declares ``RESPONSE/RESOLUTION``
but really holds ``8 HOURS, 4 HOURS``; ``priority_description`` declares P-codes and
holds ``1, 2, 3``; ``sla_breached_indicator`` declares 4 values on one table and 2 on
another. So the schema decides *which columns are candidates*; the database decides
*what the values are*.

The gate: a SCOPE column, not any low-cardinality column
--------------------------------------------------------
A predicate answers "over which subset", so it wants a small closed set of *states and
outcomes*. That is not the same thing as a good breakdown. All 23 curated predicates use
exactly one shape of column — ``status_name``, ``state``, ``type_description``, ``type``,
``methodology``, ``closure_code``, ``risk_description``, ``outage_type``,
``business_criticality_value``, ``*_indicator`` — and **none** uses a sector, region,
country, vendor, assignment group or configuration item.

An earlier version of this script gated on ``deeper_insights: true`` and produced
``country_usa``, ``sub_business_name_support``, ``it_vendor_name_everest_dx``. That flag
marks good *breakdown* columns, which is close to the opposite of a scope: it would have
made ``support``, ``analytics``, ``operations`` and ``security`` resolvable predicate
phrases, when ``filter_aliases.json`` plus ``column_resolver`` already handle those
correctly as FILTERS. A user asking "incidents in support" wants a filter, and binding a
scope predicate there changes the population instead of narrowing it.

So the candidate gate is an explicit allowlist of scope-shaped column names
(``_SCOPE_SHAPES``), and then:

  1. role is ``dimension``
  2. the column name matches a scope shape
  3. its measured distinct non-null count is <= ``--max-values``
  4. the owning entity's grain key exists on the table, so the predicate can be counted
     at entity grain and can act as an EXISTS semi-join elsewhere

``deeper_insights`` / ``filters.allowed`` are deliberately NOT used: they are the
breakdown signal, and this is the scope layer.

Output shape
------------
One predicate per (column, value), with a binding for every table where that pair was
measured — which is what preserves the per-table stored type. Predicates are named
``<column>_<value_slug>`` so they can never collide with the curated names, and the
registry applies ``predicates.json`` on top (see ``PredicateRegistry._load``).

CANDIDATES, NOT VOCABULARY — read before writing the output file
---------------------------------------------------------------
``predicates.generated.json`` is NOT committed, and ``PredicateRegistry`` loads it only
if it happens to exist. That is deliberate, and measured rather than assumed: a full run
produces ~184 predicates, and review of the output kept finding entries that are correct
SQL but wrong vocabulary —

  * ``contact_type_phone``, ``active_indicator_type_security_compliance`` — channels and
    service classes matching a scope-shaped NAME while holding breakdown values
  * ``closure_code_duplicate_problem`` vs ``closure_code_duplicate_availability`` —
    machine names competing for the bare phrase "duplicate"
  * ``active_indicator_0`` — a flag whose values carry no sayable phrasing

Each fix to the gate surfaced another borderline column, which is the finding: a
name-shape rule cannot decide what counts as a business scope, and 184 machine-named
entries would also land in every ``list_predicates`` response that feeds the agent's
prompt. So the intended loop is:

    --coverage (what is missing)  ->  this tool (what the real values are)
        ->  hand-curate the ones worth vocabulary into predicates.json
        ->  --verify (prove every binding still matches rows)

Writing the file and letting the registry pick it up is supported for a deployment that
wants breadth over precision; the curated overlay always wins, so it can only add
vocabulary, never change an existing answer.

USAGE
  python tools/gen_predicates.py --dry-run        # report + sample, writes nothing
  python tools/gen_predicates.py --entity change  # one entity, for curating that module
  python tools/gen_predicates.py --max-values 8
  python tools/gen_predicates.py --out /tmp/candidates.json    # review, then curate
  python -m cora_mcp.predicate_registry --coverage             # what has no predicate
  python -m cora_mcp.predicate_registry --verify               # the CI gate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_ROOT, ".env"), override=False)

from cora_mcp import db  # noqa: E402
from cora_mcp.logging_config import get_logger, setup_logging  # noqa: E402
from cora_mcp.record_lookup import (_entity_id_column, _plural,  # noqa: E402
                                    _singular)
from cora_mcp.schema_loader import get_loader  # noqa: E402

log = get_logger("gen_predicates")

OUT_PATH = os.path.join(_ROOT, "predicates.generated.json")
DEFAULT_MAX_VALUES = 12

# Never candidates whatever the schema says: warehouse bookkeeping and surrogate keys
# carry no business meaning, and a hierarchy level is a breakdown rather than a scope.
_SKIP_PREFIXES = ("dw_",)
_SKIP_SUFFIXES = ("_system_id", "_uuid")
_SKIP_EXACT = {"hierarchy_1", "hierarchy_2", "hierarchy_3", "hierarchy_4", "hierarchy_5",
               "delivery_hierarchy_4", "delivery_hierarchy_5"}

# A SCOPE column names a state, a kind or an outcome. Derived from the shape of every
# column the 23 curated predicates actually bind to, then widened to the obvious
# siblings. Anything not matching here is a breakdown (sector / region / vendor / group /
# CI / category) and belongs to filter_aliases.json + column_resolver, not to a
# predicate — see the module docstring.
_SCOPE_SHAPES = tuple(re.compile(p) for p in (
    r".*_indicator$",              # major_incident_indicator, sla_breached_indicator
    r"^active$",
    r"^status$", r"^status_.*$", r".*_status$",
    r"^state$", r".*_state$",
    r"^type$", r"^type_.*$", r".*_type$", r".*_type_value$",
    r"^priority$", r"^priority_.*$",
    r"^risk$", r"^risk_.*$",
    r"^closure_code.*$",
    r"^methodology$",
    r"^stage$", r"^stage_.*$", r".*_stage$", r".*_stage_description$",
    r"^impact_description$", r"^urgency_description$",
    r"^target$", r"^sla_target$", r"^approval$",
    r"^business_criticality_value$", r"^service_classification_value$",
    r"^outbound_it_control_indicator$", r"^under_outside_it_control$",
))


# Named like a scope, holds something else. ``active_indicator_type`` reads as a flag and
# actually stores service classes ('Application Services', 'Security & Compliance') —
# already recorded in MULTI_METRIC_ANALYSIS.md as disagreeing with its declared domain.
# A name-shape rule cannot see that, so known traps are listed explicitly.
#
# Each pass over the output found another of these — which is the finding, not a snag:
# a name-shape rule cannot decide what is a business scope, so the generated file is a
# REVIEW artifact to curate from, not vocabulary to load unread. See the CANDIDATES,
# NOT VOCABULARY section below.
_SHAPE_FALSE_POSITIVES = {
    "active_indicator_type",   # holds service classes, not a flag
    "contact_type",            # the channel a ticket arrived by — a breakdown
    "ticket_type",
}


def _is_scope_column(name: str) -> bool:
    if name in _SHAPE_FALSE_POSITIVES:
        return False
    return any(p.match(name) for p in _SCOPE_SHAPES)

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: Any) -> str:
    return _SLUG.sub("_", str(value).strip().lower()).strip("_")


def _phrase(value: Any) -> str:
    return _SLUG.sub(" ", str(value).strip().lower()).strip()


def _noun(entity: str) -> str:
    return (entity or "").replace("_", " ").strip()


def _synonyms(column: str, value: Any, entity: str) -> List[str]:
    """Vocabulary a generated predicate answers to.

    Deliberately conservative, because the curated overlay owns the good phrasings and a
    generated term that collides with one is dropped at load:

      * ``<value> <entity>`` and its plural are always emitted — "cancelled change",
        "cancelled changes". These are unambiguous and are what a person actually says.
      * ``<column> <value>`` is always emitted, so every predicate stays addressable
        even for a boolean or an integer ("sla breached indicator true").
      * the BARE value is emitted only when it is distinctive: at least two words, or at
        least five characters. That keeps "cancelled" and "closed complete" while
        refusing "p1", "high", "low", "1" — single short tokens that occur in questions
        for reasons unrelated to scope.
    """
    phrase = _phrase(value)
    noun = _noun(entity)
    out: List[str] = []
    # A code carries no words, so no phrasing built from it reads as English: an
    # ``active_indicator`` of 0 produced the synonym "0 major incidents". Codes get the
    # column-qualified form only.
    wordy = bool(phrase) and not phrase.replace(" ", "").isdigit()
    if wordy:
        if " " in phrase or len(phrase) >= 5:
            out.append(phrase)
        if noun:
            other = (_singular(noun) if noun.endswith("s") else _plural(noun))
            out.append(f"{phrase} {noun}")
            out.append(f"{phrase} {other}")
    out.append(f"{_phrase(column)} {phrase}".strip())
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _candidate_columns(entity_filter: Optional[str] = None,
                       ) -> List[Tuple[str, str, str, str, dict]]:
    """[(entity, table, grain_key, column, column_info)] passing the schema-side gate."""
    loader = get_loader()
    out = []
    for _mod, slug, ent in loader.all_entities():
        entity = ent.get("name")
        if entity_filter and entity_filter not in (entity, slug):
            continue
        grain = _entity_id_column(slug)
        for t in ent.get("tables") or []:
            table = t.get("name")
            cols = {c.get("name"): c for c in t.get("columns") or []}
            if not grain or grain not in cols:
                log.debug("skip %s: grain key %r absent", table, grain)
                continue
            for name, ci in cols.items():
                if (ci.get("role") or "dimension") != "dimension":
                    continue
                if (name.startswith(_SKIP_PREFIXES) or name.endswith(_SKIP_SUFFIXES)
                        or name in _SKIP_EXACT):
                    continue
                if not _is_scope_column(name):
                    continue
                out.append((entity, table, grain, name, ci))
    return out


async def _measure(table: str, column: str, max_values: int,
                   connection: str) -> Optional[List[Any]]:
    """The column's real domain, or None when it is too wide / unreadable.

    NULLs are excluded: a filter condition supports no IS NULL op, so a NULL "value"
    could not be compiled even if it were the majority of the table (it is, on
    ``outage_type``)."""
    sql = ('SELECT %s AS v, count(*) AS c FROM %s WHERE %s IS NOT NULL '
           'GROUP BY 1 ORDER BY 2 DESC LIMIT %d' % (column, table, column, max_values + 1))
    try:
        out = await db.execute("postgres", connection, sql, [], limit=max_values + 1)
    except Exception as exc:
        log.warning("unreadable %s.%s: %s", table, column, str(exc)[:120])
        return None
    rows = out.get("rows") or []
    if not rows:
        return None
    if len(rows) > max_values:
        log.debug("skip %s.%s: >%d distinct values", table, column, max_values)
        return None
    # An empty / whitespace-only value is stored data, not a business state: it produced
    # a predicate literally named ``closure_code_`` whose only synonym was the column
    # name. Filtering it here rather than downstream keeps the slug non-empty by
    # construction.
    return [r["v"] for r in rows if str(r["v"]).strip() != ""]


async def generate(max_values: int = DEFAULT_MAX_VALUES,
                   entity_filter: Optional[str] = None,
                   connection: str = "vtx5") -> Dict[str, Any]:
    candidates = _candidate_columns(entity_filter)
    log.info("candidate scope columns passing the schema gate: %d", len(candidates))

    # (column, value_slug, entity) -> predicate under construction
    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    measured = skipped = 0
    for entity, table, grain, column, _ci in candidates:
        values = await _measure(table, column, max_values, connection)
        if values is None:
            skipped += 1
            continue
        measured += 1
        for v in values:
            key = (column, _slug(v), entity)
            entry = acc.setdefault(key, {
                "name": "%s_%s" % (column, _slug(v)),
                "synonyms": _synonyms(column, v, entity),
                "entity": entity,
                "grain_key": grain,
                "bindings": [],
            })
            entry["bindings"].append({
                "table": table,
                "conditions": [{"field": column, "op": "=", "values": [v]}],
                "note": "generated: measured in %s" % table,
            })

    # A (column, value) that occurs on several tables becomes ONE predicate with a
    # binding per table — the per-table stored value is kept verbatim, which is the
    # whole point of a per-table binding.
    #
    # The same (column, value) can also occur for several ENTITIES: status_name='CLOSED'
    # exists on incidents, changes and requests, and those are different populations that
    # must stay separate predicates. Name them with the entity suffix in that case — for
    # ALL of them, not just the second one, so a name never depends on iteration order
    # (an earlier version gave the first entity the bare name, which silently changed
    # which population `status_name_closed` meant when the schema was reordered).
    per_pair = Counter((col, slug) for col, slug, _ent in acc)
    predicates: Dict[str, Any] = {}
    for (col, slug, entity), entry in sorted(acc.items()):
        name = entry.pop("name")
        if per_pair[(col, slug)] > 1:
            name = "%s_%s" % (name, entity)
        if name in predicates:                     # cannot happen; refuse to lose one
            raise RuntimeError("generated name collision on %r" % name)
        predicates[name] = entry

    doc = {
        "description": (
            "GENERATED — do not hand-edit. Written by tools/gen_predicates.py, which "
            "measures each candidate column's real domain in the database rather than "
            "trusting schema_v3.yaml's possible_values (documented to disagree with "
            "the database on outage_type, target, priority_description and "
            "sla_breached_indicator among others). Curated names, synonyms, free "
            "bindings and composite scopes live in predicates.json, which the registry "
            "applies ON TOP of this file; on any name or synonym conflict the curated "
            "entry wins. Regenerate after a data-model change, then run "
            "`python -m cora_mcp.predicate_registry --verify`."),
        "generated_by": "tools/gen_predicates.py",
        "gate": {"max_values": max_values,
                 "requires": "role=dimension AND (deeper_insights OR kpi filters.allowed)"
                             " AND measured cardinality<=max_values AND grain key on table"},
        "predicates": predicates,
    }
    log.info("generated %d predicate(s) from %d measured column(s) (%d skipped)",
             len(predicates), measured, skipped)
    return doc


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-values", type=int, default=DEFAULT_MAX_VALUES,
                    help="skip a column with more distinct values than this "
                         "(default %d)" % DEFAULT_MAX_VALUES)
    ap.add_argument("--entity", default=None, help="restrict to one entity name/slug")
    ap.add_argument("--connection", default="vtx5")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()
    setup_logging()

    async def _run():
        doc = await generate(args.max_values, args.entity, args.connection)
        preds = doc["predicates"]
        by_entity: Dict[str, int] = {}
        tables: set = set()
        for p in preds.values():
            by_entity[p["entity"]] = by_entity.get(p["entity"], 0) + 1
            tables.update(b["table"] for b in p["bindings"])
        print("\n%d generated predicate(s) over %d table(s)" % (len(preds), len(tables)))
        for ent, n in sorted(by_entity.items()):
            print("   %-20s %d" % (ent, n))
        if args.dry_run:
            print("\n--dry-run: nothing written. Sample:")
            for name in list(preds)[:12]:
                p = preds[name]
                print("   %-42s %s" % (name, p["synonyms"][:3]))
        else:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, default=str)
                fh.write("\n")
            print("\nwrote %s" % args.out)
        await db.close_pools()
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(_main())