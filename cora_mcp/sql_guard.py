"""AST-based read-only SQL guard for Postgres.

Used by two callers with different trust levels:

  * ``sql_builder._guard_readonly`` — a belt-and-suspenders re-check on SQL
    this module already built deterministically from a validated
    :class:`~cora_mcp.sql_builder.QuerySpec`. That SQL is trusted; this is
    just a final backstop.
  * ``generate_sql`` / ``run_postgres_sql`` (``cora_mcp/tools.py``) — the
    ONLY guard standing between free-text, LLM-authored SQL and the
    database. That SQL is NOT trusted, so every check here inspects the
    real parsed structure, never just the first keyword or the presence of
    a literal ``;``.

Why not keep using the old regex-only check everywhere? It only looks at the
first keyword and rejects a literal ``;`` — it does not walk the parsed AST,
so a mutating statement hidden inside a CTE (for example
``WITH x AS (DELETE FROM t RETURNING id) SELECT * FROM x``) sails through: it
starts with ``WITH`` and contains no ``;``. This module parses with sqlglot
and walks every node in the tree, so a mutating statement nested anywhere is
still caught.

Deliberately NOT supported: ``EXPLAIN``. sqlglot's Postgres dialect falls
back to a generic, unparsed ``Command`` node for it in this version, which
this guard can't safely distinguish from other unparsed DDL/utility
statements — so ``EXPLAIN`` is rejected rather than half-validated. Use
``generate_sql`` (dry-run, no execution) to sanity-check a query instead.
"""
from __future__ import annotations

from typing import Tuple

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # pragma: no cover - optional dependency
    sqlglot = None
    exp = None


class SQLGuardError(ValueError):
    """SQL rejected by the read-only guard."""


def _node_types(*names: str) -> Tuple[type, ...]:
    if exp is None:
        return ()
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


# Node types that mutate data or schema, or change session/server state —
# rejected ANYWHERE in the parsed tree, not just at the top level, so one
# hidden inside a CTE or subquery is still caught.
_MUTATING_NODES = _node_types(
    "Insert", "Update", "Delete", "Drop", "Create", "Alter",
    "TruncateTable", "Merge", "Grant", "Command", "Set", "Copy",
)

# What the outermost statement is allowed to be. Deliberately excludes
# ``Command`` (sqlglot's catch-all for anything it can't parse into a real
# node, including EXPLAIN under the Postgres dialect in this sqlglot
# version) — an unparsed statement gets no real safety guarantee, so it's
# refused rather than assumed safe.
_ALLOWED_TOP = _node_types("Select", "Union", "With")

# Schemas that expose the database's own metadata. Off limits: not needed to
# answer an ITSM analytics question, and keeps ``describe_dataset`` (the
# cached, sanctioned path) as the one source of schema info for the LLM.
_BLOCKED_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}
_BLOCKED_TABLE_PREFIXES = ("pg_",)

# Functions with side effects, resource-exhaustion potential, or file/network/
# session access — none of these are ever needed for a read-only analytics
# query.
_BLOCKED_FUNCS = {
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "lo_import", "lo_export", "dblink", "dblink_exec", "dblink_connect",
    "set_config", "pg_advisory_lock", "pg_advisory_xact_lock", "pg_sleep_for",
}

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000


def _schema_of(table_node) -> str:
    db = table_node.args.get("db")
    return (db.name if db else "").lower()


def _func_name(node) -> str:
    # ``Anonymous`` (a function sqlglot doesn't model as a dedicated node,
    # e.g. pg_sleep/set_config) carries its real name in ``.name`` —
    # ``sql_name()`` on it returns the generic literal "anonymous", not the
    # function actually being called, so ``.name`` must win when present.
    name = getattr(node, "name", "") or ""
    if name:
        return name.lower()
    if hasattr(node, "sql_name"):
        try:
            return (node.sql_name() or "").lower()
        except Exception:
            pass
    return type(node).__name__.lower()


def assert_safe_ast(tree) -> None:
    """Walk an already-parsed sqlglot tree and raise :class:`SQLGuardError` on
    the first unsafe thing found: a mutating statement anywhere in the tree
    (including nested in a CTE), a reference to a blocked system schema/table,
    a blocked function call, or a ``SELECT ... INTO`` (creates a table as a
    side effect)."""
    if exp is None:
        raise SQLGuardError("sqlglot is not installed; cannot validate SQL safety")
    for node in tree.walk():
        if _MUTATING_NODES and isinstance(node, _MUTATING_NODES):
            raise SQLGuardError(
                f"refusing SQL containing a {type(node).__name__} node — only "
                f"read-only SELECT/WITH/UNION statements are allowed")
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise SQLGuardError(
                "refusing 'SELECT ... INTO ...': it creates a table as a side "
                "effect, which is not read-only")
        if isinstance(node, exp.Table):
            if _schema_of(node) in _BLOCKED_SCHEMAS:
                raise SQLGuardError(
                    f"refusing to query {_schema_of(node)}.{(node.name or '').lower()}: "
                    f"schema introspection is blocked here — call describe_dataset "
                    f"instead")
            if (node.name or "").lower().startswith(_BLOCKED_TABLE_PREFIXES):
                raise SQLGuardError(
                    f"refusing to query {node.name!r}: looks like a Postgres system "
                    f"catalog — call describe_dataset instead")
        if isinstance(node, (exp.Anonymous, exp.Func)):
            fname = _func_name(node)
            if fname in _BLOCKED_FUNCS:
                raise SQLGuardError(f"refusing to call {fname!r}: blocked function")


def check_readonly_sql(sql: str, dialect: str = "postgres",
                       default_limit: int = DEFAULT_LIMIT,
                       max_limit: int = MAX_LIMIT) -> str:
    """Validate free-text SQL is a single, read-only, non-probing SELECT/WITH/
    UNION statement, then return it with a ``LIMIT`` enforced — added if
    missing, clamped down if it exceeds ``max_limit``. Raises
    :class:`SQLGuardError` for anything else (syntax error, multiple
    statements, a mutating node anywhere in the tree, a blocked schema/table/
    function, or a statement type sqlglot couldn't parse into a real node).
    """
    if sqlglot is None:
        raise SQLGuardError("sqlglot is not installed; cannot safely validate free-text SQL")
    stripped = (sql or "").strip()
    if not stripped:
        raise SQLGuardError("empty SQL")
    try:
        statements = [s for s in sqlglot.parse(stripped, dialect=dialect) if s is not None]
    except Exception as exc:
        raise SQLGuardError(f"SQL failed to parse: {str(exc).splitlines()[0]}") from exc
    if not statements:
        raise SQLGuardError("no statement found")
    if len(statements) > 1:
        raise SQLGuardError(
            "refusing multiple statements in one call — issue one SELECT per "
            "tool call")
    tree = statements[0]
    if not isinstance(tree, _ALLOWED_TOP):
        raise SQLGuardError(
            f"refusing non-SELECT SQL: statement parsed as {type(tree).__name__}, "
            f"only SELECT/WITH/UNION are allowed (EXPLAIN is not supported here — "
            f"use generate_sql to check a query without executing it)")

    assert_safe_ast(tree)

    # Enforce a LIMIT so a query nobody bounded can't scan an entire fact
    # table: add the default if missing, clamp it down if the caller asked
    # for more than max_limit.
    existing = tree.args.get("limit")
    if existing is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(default_limit)))
        log.info("sql_guard: no LIMIT in caller SQL; added LIMIT %d", default_limit)
    else:
        try:
            n = int(existing.expression.this)
            if n > max_limit:
                tree.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
                log.info("sql_guard: caller LIMIT %d exceeds max %d; clamped", n, max_limit)
        except (AttributeError, TypeError, ValueError):
            pass  # non-literal LIMIT expression (rare) — leave as authored

    return tree.sql(dialect=dialect)
