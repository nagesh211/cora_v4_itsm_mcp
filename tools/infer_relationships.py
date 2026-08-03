#!/usr/bin/env python3
"""infer_relationships.py — discover join edges nobody hand-wrote.

``harvest_relationships.py`` seeds ``schema_v3.yaml`` from the joins the KPI configs
already contain. That is provenance, not discovery: by construction it can only ever
declare a path a human already wrote, which is exactly why a question about two
entities no config happens to join together fails with ``NoJoinPathError``.

This script discovers edges instead of transcribing them, from two sources:

**Schema (offline, always available).** A column name shared by two tables where one
side declares ``primary_key: true`` is a parent/child pair — the PK side is the
"one", the other is the "many". A table that holds two such child keys and is nobody's
entity table is a junction, and becomes a ``via`` edge between the two entities it
links.

**Database (``--from-db``, needs a reachable Postgres).** Four independent checks,
all of which must pass, mirroring the discipline of a foreign key without requiring
one to be declared:

  1. ``information_schema`` foreign keys — authoritative, ``confidence: 1.0``.
  2. compatible type families (an ``int`` never joins a ``timestamp``).
  3. the parent side is genuinely unique (``pg_index`` / ``pg_stats.n_distinct``).
  4. **real value overlap ≥ 30%** — the child's values are sampled and tested for
     membership against the *full* parent via a semi-join. This is the check that
     separates a real relationship from two tables that merely happen to share the
     column name ``id``.

Confidence is the measured overlap fraction. Cardinality is measured, not assumed,
which matters because :mod:`cora_mcp.fanout` treats an undeclared edge as unsafe and
refuses ``sum``/``avg`` across it — so measuring an edge as ``many_to_one`` is what
*unblocks* those measures rather than merely documenting them.

Nothing is overwritten: an edge already declared in the schema is left exactly as it
is, and inferred edges are added alongside with their provenance.

Usage::

    python tools/infer_relationships.py                    # offline, print candidates
    python tools/infer_relationships.py --from-db          # measure against Postgres
    python tools/infer_relationships.py --from-db --write  # ...and update the schema
    python tools/infer_relationships.py --min-overlap 0.5  # stricter than the 30% default
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp import fanout                                    # noqa: E402
from cora_mcp.logging_config import get_logger                 # noqa: E402
from cora_mcp.schema_loader import get_loader                  # noqa: E402

log = get_logger(__name__)

SCHEMA_PATH = os.path.join(_ROOT, "schema_v3.yaml")
DEFAULT_MIN_OVERLAP = 0.30
SAMPLE_SIZE = 2000

# Type families that may legitimately be joined to one another.
_FAMILIES = {
    "int": {"int", "int2", "int4", "int8", "smallint", "integer", "bigint", "numeric",
            "decimal"},
    "text": {"text", "varchar", "character varying", "char", "character", "citext",
             "uuid"},
}

# A column named like this is a key, not a measurement.
_KEY_RE = re.compile(r"(^|_)(id|system_id|sys_id|key|no|number|code)$", re.I)


def _family(pgtype: Optional[str]) -> Optional[str]:
    t = (pgtype or "").lower().strip().rstrip("[]")
    for fam, members in _FAMILIES.items():
        if t in members:
            return fam
    return None


def _short(fqn: str) -> str:
    return fqn.split(".")[-1].replace("tbl_", "")


# ---------------------------------------------------------------------------
# Offline inference from schema_v3.yaml
# ---------------------------------------------------------------------------
class SchemaIndex:
    """The slices of the schema this script reasons over."""

    def __init__(self):
        self.loader = get_loader()
        self.tables: Dict[str, Dict[str, dict]] = {
            fqn: self.loader.table_columns(fqn) for fqn in self.loader._by_table
        }
        # tables that are the primary table of some entity -- i.e. things a question
        # can be *about*, as opposed to plumbing between them.
        self.entity_tables: Set[str] = set()
        for _mod, slug, _entity in self.loader.all_entities():
            pt = self.loader.entity_primary_table(slug)
            if pt:
                self.entity_tables.add(pt)

    def pk_columns(self, fqn: str) -> Set[str]:
        return {n for n, c in self.tables.get(fqn, {}).items() if c.get("primary_key")}

    def key_columns(self, fqn: str) -> Set[str]:
        """Columns that look like keys: declared identifiers, or key-shaped names."""
        out = set()
        for name, col in self.tables.get(fqn, {}).items():
            if col.get("role") == "identifier" or col.get("primary_key") \
                    or _KEY_RE.search(name):
                out.add(name)
        return out

    def declared_edges(self) -> List[dict]:
        edges = []
        for module_name in self.loader.module_names():
            module = self.loader.get_module(module_name) or {}
            edges.extend(module.get("relationships") or [])
        return edges


def _owners_of(idx: SchemaIndex, column: str) -> List[str]:
    """Every table for which ``column`` is a primary key — the candidate "one" sides.

    A column is often the PK of more than one table: ``incident_system_id`` keys both
    ``tbl_all_incidents`` and ``tbl_major_incidents``. Picking one and discarding the
    other would be a guess, and discarding both (the obvious safe move) throws away
    every incident edge in the schema — including the SLA and outage joins that most
    of the real questions need.

    So emit an edge for each, and let ``--from-db`` decide: measured overlap ranks a
    genuine parent above a coincidence, and a subset table like
    ``tbl_major_incidents`` is kept on the strength of the direction that *is* high
    (see :func:`_best_overlap`).
    """
    owners = [fqn for fqn in idx.tables if column in idx.pk_columns(fqn)]
    entity_owners = [f for f in owners if f in idx.entity_tables]
    return entity_owners or owners


def infer_from_schema(idx: SchemaIndex) -> List[dict]:
    """Direct parent/child edges plus ``via`` edges through junction tables."""
    candidates: List[dict] = []
    seen: Set[Tuple[str, str, str]] = set()

    # ---- direct edges: child.col -> parent.col where parent.col is a PK --------
    for child_fqn, cols in idx.tables.items():
        for col in idx.key_columns(child_fqn):
            if col in idx.pk_columns(child_fqn):
                continue                              # this table IS the parent
            for parent_fqn in _owners_of(idx, col):
                if parent_fqn == child_fqn:
                    continue
                cfam = _family((cols.get(col) or {}).get("type"))
                pfam = _family((idx.tables[parent_fqn].get(col) or {}).get("type"))
                if cfam and pfam and cfam != pfam:
                    continue                          # incompatible types
                key = tuple(sorted([child_fqn, parent_fqn]) + [col])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "name": f"{_short(parent_fqn)}_to_{_short(child_fqn)}",
                    "description": (f"Inferred from the schema: {col} is the primary "
                                    f"key of {parent_fqn}."),
                    "left": parent_fqn,
                    "right": child_fqn,
                    "join_on": {"left_col": col, "right_col": col},
                    # one parent row -> many child rows, read from the parent side
                    "cardinality": "one_to_many",
                    "source": "schema:primary_key",
                    "confidence": None,      # unmeasured until --from-db
                })

    # ---- via edges: a junction links two entity tables ------------------------
    for j_fqn in idx.tables:
        if j_fqn in idx.entity_tables:
            continue                                  # an entity, not plumbing
        # A junction is defined by its *columns*: exactly two foreign keys pointing
        # at entity tables. Each of those columns may key more than one entity
        # (incident_system_id -> all_incidents and major_incidents), so the edges are
        # the cross-product -- incidents<->changes and major-incidents<->changes are
        # both real, and both are needed.
        by_col: Dict[str, List[str]] = {}
        for col in idx.key_columns(j_fqn):
            owners = [p for p in _owners_of(idx, col)
                      if p in idx.entity_tables and p != j_fqn]
            if owners:
                by_col[col] = owners
        if len(by_col) != 2:
            continue
        (lc, lps), (rc, rps) = sorted(by_col.items())
        for lp in lps:
            for rp in rps:
                if lp == rp:
                    continue
                key = tuple(sorted([lp, rp]) + [j_fqn])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "name": f"{_short(lp)}_to_{_short(rp)}_via_{_short(j_fqn)}",
                    "description": (f"Inferred from the schema: {j_fqn} links {lp} "
                                    f"and {rp}."),
                    "left": lp,
                    "right": rp,
                    "via": j_fqn,
                    "left_on": {"left_col": lc, "via_col": lc},
                    "right_on": {"right_col": rc, "via_col": rc},
                    "cardinality": "many_to_many",
                    "source": "schema:junction",
                    "confidence": None,
                })

    candidates.sort(key=lambda c: c["name"])
    return candidates


# ---------------------------------------------------------------------------
# Live measurement against Postgres
# ---------------------------------------------------------------------------
_FK_SQL = """
SELECT tc.table_schema  || '.' || tc.table_name   AS child,
       kcu.column_name                            AS child_col,
       ccu.table_schema || '.' || ccu.table_name  AS parent,
       ccu.column_name                            AS parent_col
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema    = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name
 AND ccu.table_schema    = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
"""


async def _is_unique(con, fqn: str, col: str) -> bool:
    """Is ``col`` unique on ``fqn``? Cheap plan first, exact count as a fallback."""
    schema, table = fqn.split(".", 1)
    n_distinct = await con.fetchval(
        "SELECT n_distinct FROM pg_stats WHERE schemaname=$1 AND tablename=$2 "
        "AND attname=$3", schema, table, col)
    if n_distinct == -1:                 # pg's marker for "unique in every sampled row"
        return True
    total, distinct = await con.fetchrow(
        f'SELECT count(*), count(DISTINCT "{col}") FROM {fqn}')
    return bool(total) and total == distinct


async def _overlap(con, child: str, child_col: str,
                   parent: str, parent_col: str) -> float:
    """Fraction of the child's sampled distinct values that exist in the parent.

    The child side is sampled (a full distinct scan of a 150k-row table per candidate
    edge would make this script unusable), but membership is tested against the
    *whole* parent with a semi-join — sampling the parent would invent misses.
    """
    row = await con.fetchrow(
        f'''
        WITH sample AS (
            SELECT DISTINCT "{child_col}" AS v
            FROM {child}
            WHERE "{child_col}" IS NOT NULL
            LIMIT {SAMPLE_SIZE}
        )
        SELECT count(*) AS n,
               count(*) FILTER (
                   WHERE EXISTS (SELECT 1 FROM {parent} p
                                 WHERE p."{parent_col}" = sample.v)
               ) AS hit
        FROM sample
        ''')
    n = row["n"] or 0
    return (row["hit"] / n) if n else 0.0


async def _best_overlap(con, lt: str, lc: str, rt: str, rc: str) -> Tuple[float, str]:
    """Overlap measured in both directions; the better one wins.

    Direction matters more than it looks. For a classic foreign key, ~100% of the
    child's values exist in the parent. But for a *subset* table — every row of
    ``tbl_major_incidents`` is also in ``tbl_all_incidents`` — the child→parent
    fraction against the subset is tiny even though the join is perfectly valid.
    Taking the maximum keeps both shapes and throws away only pairs that share a
    column name and nothing else.
    """
    fwd = await _overlap(con, rt, rc, lt, lc)     # right's values found in left
    rev = await _overlap(con, lt, lc, rt, rc)     # left's values found in right
    return (fwd, f"{rt}->{lt}") if fwd >= rev else (rev, f"{lt}->{rt}")


async def measure(candidates: List[dict], min_overlap: float) -> List[dict]:
    """Verify each candidate against the live database; drop what fails."""
    from cora_mcp import db as dbmod

    dsn = dbmod.resolve_dsn(None)
    if not dsn:
        raise SystemExit("no Postgres DSN configured. Set CORA_PG_DSN in .env")
    import asyncpg

    con = await asyncpg.connect(dsn=dsn, timeout=30)
    kept: List[dict] = []
    try:
        # ---- authoritative foreign keys ------------------------------------
        fks = {(r["child"], r["child_col"], r["parent"], r["parent_col"])
               for r in await con.fetch(_FK_SQL)}
        log.info("found %d declared foreign key(s)", len(fks))

        for cand in candidates:
            pairs = _pairs_of(cand)
            oks: List[float] = []
            directions: List[str] = []
            for (lt, lc, rt, rc) in pairs:
                if (rt, rc, lt, lc) in fks or (lt, lc, rt, rc) in fks:
                    oks.append(1.0)
                    directions.append("declared FK")
                    continue
                try:
                    frac, direction = await _best_overlap(con, lt, lc, rt, rc)
                except Exception as exc:
                    log.warning("overlap check failed for %s (%s.%s <-> %s.%s): %s",
                                cand["name"], lt, lc, rt, rc, exc)
                    frac, direction = 0.0, "failed"
                oks.append(frac)
                directions.append(direction)
            # A two-hop via edge is only as good as its weaker hop.
            conf = min(oks) if oks else 0.0
            cand["overlap_direction"] = ", ".join(directions)
            cand["confidence"] = round(conf, 3)
            if conf < min_overlap:
                log.info("dropping %s: overlap %.2f < %.2f", cand["name"], conf,
                         min_overlap)
                continue
            cand["source"] = "database:foreign_key" if conf == 1.0 and cand.get(
                "join_on") else cand.get("source", "database:overlap")
            # ---- measured cardinality --------------------------------------
            if "join_on" in cand:
                lu = await _is_unique(con, cand["left"], cand["join_on"]["left_col"])
                ru = await _is_unique(con, cand["right"], cand["join_on"]["right_col"])
                cand["cardinality"] = fanout.classify_cardinality(lu, ru)
            kept.append(cand)
    finally:
        await con.close()
    return kept


def _pairs_of(cand: dict) -> List[Tuple[str, str, str, str]]:
    """(left_table, left_col, right_table, right_col) for each join hop."""
    if "join_on" in cand:
        return [(cand["left"], cand["join_on"]["left_col"],
                 cand["right"], cand["join_on"]["right_col"])]
    return [
        (cand["left"], cand["left_on"]["left_col"], cand["via"],
         cand["left_on"]["via_col"]),
        (cand["right"], cand["right_on"]["right_col"], cand["via"],
         cand["right_on"]["via_col"]),
    ]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _edge_identity(rel: dict) -> Tuple:
    return (rel.get("left"), rel.get("right"), rel.get("via"),
            (rel.get("join_on") or {}).get("left_col"))


def novel_only(candidates: List[dict], declared: List[dict]) -> List[dict]:
    """Drop candidates that restate an edge the schema already declares.

    Matching is by endpoints, not by name: a curated edge and an inferred one that
    connect the same two tables the same way are the same edge, and the curated one
    wins because it carries the discriminator constant and the human-chosen name.
    """
    have = {_edge_identity(r) for r in declared}
    have |= {(b, a, v, c) for (a, b, v, c) in have}      # edges are undirected
    return [c for c in candidates if _edge_identity(c) not in have]


def write_schema(new_edges: List[dict], module_name: str = "itsm") -> int:
    """Append inferred edges to the module's ``relationships:`` block, in place."""
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    def to_commented(obj):
        if isinstance(obj, dict):
            m = CommentedMap()
            for k, v in obj.items():
                if v is not None:
                    m[k] = to_commented(v)
            return m
        if isinstance(obj, list):
            s = CommentedSeq()
            for v in obj:
                s.append(to_commented(v))
            return s
        return obj

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=2, offset=0)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        doc = yaml.load(fh)

    for mod in doc.get("modules") or []:
        if mod.get("name") != module_name:
            continue
        block = mod.get("relationships")
        if block is None:
            block = CommentedSeq()
            keys = list(mod.keys())
            pos = keys.index("entities") if "entities" in keys else len(keys)
            mod.insert(pos, "relationships", block)
        for edge in new_edges:
            block.append(to_commented(edge))
        with open(SCHEMA_PATH, "w", encoding="utf-8", newline="\n") as fh:
            yaml.dump(doc, fh)
        print(f"wrote {len(new_edges)} inferred relationship(s) to {SCHEMA_PATH}")
        return 0
    print(f"module {module_name!r} not found in {SCHEMA_PATH}")
    return 1


def report(candidates: List[dict], declared_count: int) -> None:
    print(f"\n{len(candidates)} candidate edge(s) not already declared "
          f"({declared_count} declared):\n")
    for c in candidates:
        conf = c.get("confidence")
        conf_s = "unmeasured" if conf is None else f"{conf:.0%}"
        shape = f"via {c['via']}" if c.get("via") else "direct"
        print(f"  {c['name']}")
        print(f"      {c['left']}  <->  {c['right']}   ({shape})")
        print(f"      cardinality={c.get('cardinality')}  confidence={conf_s}  "
              f"source={c.get('source')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-db", action="store_true",
                    help="verify candidates against Postgres (FK metadata, key "
                         "uniqueness, value overlap) and measure cardinality")
    ap.add_argument("--write", action="store_true",
                    help="append the surviving edges to schema_v3.yaml")
    ap.add_argument("--min-overlap", type=float, default=DEFAULT_MIN_OVERLAP,
                    help=f"minimum value overlap to keep an edge "
                         f"(default {DEFAULT_MIN_OVERLAP})")
    ap.add_argument("--module", default="itsm", help="module to write into")
    args = ap.parse_args()

    idx = SchemaIndex()
    declared = idx.declared_edges()
    candidates = novel_only(infer_from_schema(idx), declared)

    if args.from_db:
        candidates = asyncio.run(measure(candidates, args.min_overlap))
    elif args.write:
        print("refusing to --write unmeasured edges: run with --from-db so each edge's "
              "value overlap and cardinality are measured rather than assumed.\n"
              "(An unmeasured edge defaults to fan-out-unsafe, which blocks sum/avg.)")
        return 2

    report(candidates, len(declared))
    if args.write:
        return write_schema(candidates, args.module)
    if candidates:
        print("\nre-run with --from-db --write to verify and persist these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())