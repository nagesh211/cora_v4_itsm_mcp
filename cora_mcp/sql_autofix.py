"""Self-healing for free-text SQL (``generate_sql`` / ``run_postgres_sql``).

``sql_guard`` decides whether SQL is SAFE to run; ``db.py`` decides whether it
is VALID by asking Postgres itself. Neither one tries to FIX a query that
Postgres rejects for a bad identifier -- and the LLM authoring that SQL
usually named a real concept with the wrong physical spelling (a typo, a
plausible-but-wrong guess, an unqualified column that exists in two joined
tables). This module reuses the resolvers already trusted elsewhere in this
codebase (:mod:`cora_mcp.column_resolver`, the alias-scope logic in
:mod:`cora_mcp.sql_alias`) to turn a Postgres error into either:

  * a confident, mechanically-applied fix (exactly one candidate found), or
  * a set of hints (more than one candidate, or none) -- never a guess.

Rewrites are done on the parsed sqlglot AST, never by string substitution, so
fixing one bad identifier can't accidentally touch another part of the query
that happens to contain the same text (a string literal, an alias, ...).
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # pragma: no cover - optional dependency
    sqlglot = None
    exp = None

# Postgres's own wording for the three failures this module can act on.
_COLUMN_RE = re.compile(r'column "([^"]+)" does not exist', re.IGNORECASE)
_RELATION_RE = re.compile(r'relation "([^"]+)" does not exist', re.IGNORECASE)
_AMBIGUOUS_RE = re.compile(r'column reference "([^"]+)" is ambiguous', re.IGNORECASE)


def extract_table_fqns(sql: str, dialect: str = "postgres") -> List[str]:
    """Every ``schema.table`` referenced in ``sql`` (FROM/JOIN), lowercased."""
    if sqlglot is None:
        return []
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return []
    out: List[str] = []
    seen = set()
    for t in tree.find_all(exp.Table):
        db = t.args.get("db")
        name = (t.name or "").lower()
        if not name:
            continue
        fqn = f"{db.name.lower()}.{name}" if db else name
        if fqn not in seen:
            seen.add(fqn)
            out.append(fqn)
    return out


def parse_db_error(message: str) -> Optional[Dict[str, str]]:
    """Classify a Postgres error message into ``{"kind": ..., "identifier": ...}``,
    or ``None`` if it isn't one of the three shapes this module knows how to act
    on. ``kind`` is one of ``column`` | ``relation`` | ``ambiguous_column``."""
    msg = message or ""
    m = _AMBIGUOUS_RE.search(msg)
    if m:
        return {"kind": "ambiguous_column", "identifier": m.group(1)}
    m = _COLUMN_RE.search(msg)
    if m:
        return {"kind": "column", "identifier": m.group(1)}
    m = _RELATION_RE.search(msg)
    if m:
        return {"kind": "relation", "identifier": m.group(1)}
    return None


def _bare(identifier: str) -> str:
    """Strip a table qualifier off ``table.column`` -> ``column``."""
    return identifier.split(".")[-1]


def _suggest_column_fix(sql: str, dialect: str, bad_name: str) -> Dict[str, Any]:
    from cora_mcp.column_resolver import resolve_column
    from cora_mcp.schema_loader import get_loader

    tables = extract_table_fqns(sql, dialect)
    word = _bare(bad_name)
    candidates: List[str] = []  # "table.column"
    for fqn in tables:
        real = resolve_column(fqn, word)
        if real:
            candidates.append(f"{fqn}.{real}")

    if not candidates:
        # column_resolver only covers inflection/vocabulary variants -- a plain
        # typo (a letter dropped or swapped) falls through it by design. Catch
        # that narrower case here, only when exactly one column across all
        # in-scope tables is a close spelling match.
        for fqn in tables:
            names = list(get_loader().table_columns(fqn).keys())
            close = difflib.get_close_matches(word.lower(), [n.lower() for n in names],
                                              n=2, cutoff=0.8)
            if close:
                real = next(n for n in names if n.lower() == close[0])
                candidates.append(f"{fqn}.{real}")

    uniq_cols = sorted({c.split(".")[-1] for c in candidates})
    if len(uniq_cols) == 1:
        fixed = _rewrite_column(sql, dialect, bad_name, uniq_cols[0])
        if fixed:
            return {"fixed_sql": fixed,
                     "note": f"column {bad_name!r} does not exist -- replaced with "
                              f"{uniq_cols[0]!r} (resolved via schema vocabulary/"
                              f"near-miss match on the query's table(s))"}
    return {"hints": (
        [f"column {bad_name!r} does not exist. Candidates found on the query's "
         f"table(s): {sorted(set(candidates))}" ] if candidates else
        [f"column {bad_name!r} does not exist and no near-miss was found on the "
         f"query's table(s) ({tables}). Call describe_dataset on the relevant "
         f"entity to see real column names."]
    )}


def _suggest_relation_fix(sql: str, dialect: str, bad_name: str) -> Dict[str, Any]:
    from cora_mcp.schema_loader import get_loader

    known = get_loader().known_table_fqns()
    word = _bare(bad_name).lower()
    # Match on the bare table name (the part after the schema dot), since the
    # LLM's guess and the schema's fqn share that half far more often than the
    # schema prefix.
    by_bare = {fqn.split(".")[-1]: fqn for fqn in known}
    close = difflib.get_close_matches(word, list(by_bare.keys()), n=3, cutoff=0.75)
    if len(close) == 1:
        fixed = _rewrite_table(sql, dialect, bad_name, by_bare[close[0]])
        if fixed:
            return {"fixed_sql": fixed,
                     "note": f"relation {bad_name!r} does not exist -- replaced with "
                              f"{by_bare[close[0]]!r} (nearest known table)"}
    return {"hints": (
        [f"relation {bad_name!r} does not exist. Closest known tables: "
         f"{[by_bare[c] for c in close]}"] if close else
        [f"relation {bad_name!r} does not exist and no close match was found. "
         f"Call list_modules / describe_module to see real table names."]
    )}


def _suggest_ambiguous_fix(sql: str, dialect: str, bad_name: str) -> Dict[str, Any]:
    from cora_mcp.sql_alias import alias_bindings

    binds = alias_bindings(sql)
    tables = [t for t, _ in binds]
    owners = [(t, a) for t, a in binds if bad_name in _table_column_names(t)]
    if len(owners) == 1:
        table, alias = owners[0]
        fixed = _qualify_column(sql, dialect, bad_name, alias)
        if fixed:
            return {"fixed_sql": fixed,
                     "note": f"column reference {bad_name!r} was ambiguous -- "
                              f"qualified as {alias}.{bad_name} ({table} is the "
                              f"only in-scope table declaring it)"}
    return {"hints": [
        f"column reference {bad_name!r} is ambiguous between: {tables}. "
        f"Qualify it yourself as <alias>.{bad_name}."]}


def _table_column_names(fqn: str) -> set:
    from cora_mcp.schema_loader import get_loader
    return set(get_loader().table_columns(fqn).keys())


def suggest_fix(sql: str, dialect: str, error_message: str) -> Dict[str, Any]:
    """Given SQL and the Postgres error it raised, return either
    ``{"fixed_sql": ..., "note": ...}`` (a confident, single-candidate fix,
    already rewritten into the SQL) or ``{"hints": [...]}`` (no confident fix --
    ambiguous or genuinely unknown). Returns ``{"hints": []}`` for an error this
    module doesn't recognise."""
    if sqlglot is None:
        return {"hints": []}
    parsed = parse_db_error(error_message)
    if not parsed:
        return {"hints": []}
    kind, identifier = parsed["kind"], parsed["identifier"]
    if kind == "column":
        return _suggest_column_fix(sql, dialect, identifier)
    if kind == "relation":
        return _suggest_relation_fix(sql, dialect, identifier)
    if kind == "ambiguous_column":
        return _suggest_ambiguous_fix(sql, dialect, identifier)
    return {"hints": []}  # pragma: no cover - exhaustive over parse_db_error's kinds


# ---------------------------------------------------------------------------
# AST rewrites -- never string substitution, so a fix can't touch text that
# merely happens to match elsewhere in the query (a literal, an alias, ...).
# ---------------------------------------------------------------------------
def _rewrite_column(sql: str, dialect: str, bad_name: str, good_name: str) -> Optional[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    bad = _bare(bad_name).lower()
    hit = False
    for col in tree.find_all(exp.Column):
        if (col.name or "").lower() == bad:
            col.set("this", exp.to_identifier(good_name))
            hit = True
    return tree.sql(dialect=dialect) if hit else None


def _rewrite_table(sql: str, dialect: str, bad_fqn: str, good_fqn: str) -> Optional[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    bad = _bare(bad_fqn).lower()
    good_schema, _, good_table = good_fqn.rpartition(".")
    hit = False
    for t in tree.find_all(exp.Table):
        if (t.name or "").lower() == bad:
            t.set("this", exp.to_identifier(good_table))
            if good_schema:
                t.set("db", exp.to_identifier(good_schema))
            hit = True
    return tree.sql(dialect=dialect) if hit else None


def _qualify_column(sql: str, dialect: str, bad_name: str, alias: str) -> Optional[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    bad = bad_name.lower()
    hit = False
    for col in tree.find_all(exp.Column):
        if (col.name or "").lower() == bad and not col.table:
            col.set("table", exp.to_identifier(alias))
            hit = True
    return tree.sql(dialect=dialect) if hit else None
