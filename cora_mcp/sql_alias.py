"""Table-alias scope of an authored ``base_query`` — so injected filters are
fully qualified.

A SQL-mode KPI's filters are inlined at the config's ``{filters}`` placeholder
using each field's ``fields[*].column``. Legacy configs write that column
already alias-qualified (``c.business_name``); the newer generated configs write
it bare (``business_name``). When the authored query joins two tables that BOTH
carry that column, the bare form reaches Postgres as an unqualified reference and
the whole query fails with::

    column reference "business_name" is ambiguous

This module reads the authored query's ``FROM`` / ``JOIN`` clauses, maps each
table to its alias, and rewrites a config's filter columns to
``<alias>.<column>`` — exactly the form the legacy configs hand-write, so
``gen_query.resolve_field`` / ``compile_sql_filters`` use it verbatim. The
declared column type is carried onto the field meta at the same time, so
qualification never costs the type-aware ``lower()`` handling in ``gen_query``.

Only applied when the authored query has **more than one table in scope** (the
sole case where an unqualified column can be ambiguous), so single-table
configs keep emitting byte-identical SQL.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

# `FROM schema.table alias` / `JOIN schema.table AS alias`. Only schema-qualified
# tables are matched — a bare `FROM (subquery) alias` is not a table binding.
_BINDING_RE = re.compile(
    r"\b(?:from|join)\s+([a-z_][\w]*\.[a-z_][\w]*)\s+(?:as\s+)?([a-z_][\w]*)",
    re.IGNORECASE)

# Words that can follow a table name without being its alias.
_NOT_AN_ALIAS = {
    "on", "where", "group", "order", "having", "limit", "offset", "union",
    "inner", "left", "right", "full", "cross", "outer", "join", "select",
    "and", "or", "as", "using", "window", "fetch", "except", "intersect",
}


def alias_bindings(sql_text: str) -> List[Tuple[str, str]]:
    """``[(table_fqn_lower, alias), ...]`` for every aliased table reference, in
    textual order (so a later binding of the same table wins for a later slot)."""
    out: List[Tuple[str, str]] = []
    for m in _BINDING_RE.finditer(sql_text or ""):
        table, alias = m.group(1), m.group(2)
        if alias.lower() in _NOT_AN_ALIAS:
            continue
        out.append((table.lower(), alias))
    return out


_TABLE_RE = re.compile(r"\b(?:from|join)\s+([a-z_][\w]*\.[a-z_][\w]*)", re.IGNORECASE)


def referenced_tables(sql_text: str) -> List[str]:
    """Distinct ``schema.table`` names in every ``FROM``/``JOIN`` clause, in
    first-seen order — best-effort attribution for hand-written SQL that
    carries no KPI config to cite (e.g. ``run_postgres_sql``). Unlike
    :func:`alias_bindings` this doesn't require (or capture) an alias, so it
    also catches unaliased single-table queries."""
    seen: Dict[str, None] = {}
    for m in _TABLE_RE.finditer(sql_text or ""):
        seen.setdefault(m.group(1).lower(), None)
    return list(seen)


def _primary_fqn(config: dict) -> Optional[str]:
    pd = config.get("primary_dataset") or {}
    schema = pd.get("schema") or (config.get("source") or {}).get("schema")
    table = pd.get("table") or pd.get("name")
    if schema and table:
        return f"{schema}.{table}".lower()
    return None


def slot_scope(base_query: str, primary_fqn: Optional[str],
               slot: str = "{filters}") -> Optional[Tuple[str, Dict[str, str]]]:
    """Resolve the alias an injected filter must use, for EVERY ``slot`` in
    ``base_query``.

    Returns ``(alias, {table_fqn: alias})`` where ``alias`` qualifies the primary
    table in the scope preceding each slot, or ``None`` when the query has no
    slot, no aliased tables, only one table in scope (nothing to disambiguate),
    or when different slots would need different aliases (one ``fields`` mapping
    cannot serve two scopes — left untouched rather than mis-qualified).
    """
    if not base_query or slot not in base_query:
        return None
    all_binds = alias_bindings(base_query)
    if len({t for t, _ in all_binds}) < 2:
        return None                      # single table in scope: never ambiguous

    aliases: List[str] = []
    for prefix in base_query.split(slot)[:-1]:
        binds = alias_bindings(prefix)
        if not binds:
            return None
        chosen = None
        for table, alias in binds:       # last binding of the primary table wins
            if primary_fqn and table == primary_fqn:
                chosen = alias
        aliases.append(chosen or binds[-1][1])
    if len(set(aliases)) != 1:
        log.debug("slot_scope: %d {filters} slots need different aliases (%s); "
                  "leaving filter columns unqualified", len(aliases), aliases)
        return None

    scope: Dict[str, str] = {}
    for table, alias in all_binds:
        scope.setdefault(table, alias)
    return aliases[0], scope


def _schema_types() -> Dict[str, Dict[str, Any]]:
    """``{table_fqn_lower: {column: sql_type}}`` from ``schema_v3.yaml``.

    Reuses ``gen_query``'s already-cached index (the same file the builder reads
    for its ``lower()`` type decisions) so there is one source of truth.
    """
    import gen_query as gq
    return {(fqn or "").lower(): cols for fqn, cols in gq._load_schema_types().items()}


def _owner_alias(column: str, primary_fqn: Optional[str], primary_alias: str,
                 scope: Dict[str, str]) -> Optional[str]:
    """Which alias should qualify ``column`` — or ``None`` if the schema can't say.

    The primary table wins when it declares the column (the author's intent for a
    KPI's own filter). Otherwise, if exactly one other in-scope table declares it,
    that table owns it. If ``schema_v3.yaml`` proves neither (an undeclared column,
    or a table missing from the schema) we do **not** guess: an unqualified column
    that is genuinely unique still resolves fine in Postgres, whereas a guessed
    alias would break it. Left bare, and logged.
    """
    types = _schema_types()
    if primary_fqn and column in (types.get(primary_fqn) or {}):
        return primary_alias
    owners = [alias for table, alias in scope.items()
              if column in (types.get(table) or {})]
    if len(owners) == 1:
        return owners[0]
    log.debug("cannot prove which table owns %r in scope %s; leaving it unqualified",
              column, sorted(scope))
    return None


def qualify_filter_columns(config: dict, keys) -> dict:
    """Return ``config`` (shallow copy) with the ``fields`` entries for ``keys``
    rewritten to ``<alias>.<column>`` for a multi-table SQL-mode ``base_query``.

    A no-op for DSL configs, for queries with a single table in scope, for keys
    whose column is already qualified, and whenever the alias can't be resolved
    unambiguously — in every one of those cases the config is returned as-is.
    """
    keys = [k for k in (keys or []) if k and k != "granularity"]
    if not keys or config.get("execution_mode") == "DSL":
        return config
    base_query = ((config.get("sql") or {}).get("base_query")) or ""
    primary = _primary_fqn(config)
    resolved = slot_scope(base_query, primary)
    if not resolved:
        return config
    primary_alias, scope = resolved

    fields = config.get("fields") or {}
    types = _schema_types()
    changed: Dict[str, dict] = {}
    for key in keys:
        meta = fields.get(key)
        if not meta:
            continue
        column = meta.get("column")
        if not column or "." in column:          # unknown, or already qualified
            continue
        alias = _owner_alias(column, primary, primary_alias, scope)
        if not alias:
            continue
        new_meta = dict(meta)
        new_meta["column"] = f"{alias}.{column}"
        if not new_meta.get("type"):
            # Carry the declared type across: ``gen_query._column_type`` gives up
            # on a qualified column, and without a type a text column would fall
            # back to ``lower(col::text)`` (and a boolean/numeric one would be
            # compared as text). Preserving it keeps the emitted predicate identical.
            owner = next((t for t, a in scope.items()
                          if a == alias and column in (types.get(t) or {})), None)
            declared = (types.get(owner) or {}).get(column) if owner else None
            if declared:
                new_meta["type"] = declared
        changed[key] = new_meta

    if not changed:
        return config
    log.info("qualified filter column(s) for SQL-mode KPI %r: %s",
             config.get("name"),
             {k: v["column"] for k, v in changed.items()})
    out = dict(config)
    out["fields"] = {**fields, **changed}
    return out