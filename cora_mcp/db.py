"""Database execution for generated KPI queries.

The KPI configs describe their data source as::

    "source": {"connection": "vtx5", "dialect": "postgres", "schema": "..."}

  against the same database, so the label does not select a DSN.
- ``dialect``    is the DB type -> selects the driver (postgres -> asyncpg).
- ``schema``     is already baked into the generated SQL.

``gen_query`` emits **psycopg-style** SQL: ``%s`` placeholders, tuples for
``IN`` lists, and Python lists for ``= ANY(%s)`` / ``&& %s`` array filters.
asyncpg instead uses numbered ``$1, $2`` placeholders, so :func:`to_asyncpg`
rewrites the SQL + params faithfully before execution.

There is exactly one Postgres DSN for the whole server::

    CORA_PG_DSN=postgresql://user:pass@host:5432/dbname

Legacy per-connection vars (``CORA_DB_VTX5``, ``CORA_DB_PEPOPS``, ...) are still
honoured as a fallback, but ``CORA_PG_DSN`` wins whenever it is set.

Until a DSN is configured, execution raises :class:`DBNotConfigured` (surfaced
to the caller as a clear ``error`` field rather than a crash).
"""
from __future__ import annotations

import datetime
import decimal
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

# Load the project-root .env once so CORA_DB_* / CORA_PG_DSN are available.
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=False)

try:
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover - driver optional at import time
    asyncpg = None


class DBError(RuntimeError):
    """A query failed to execute."""


class DBNotConfigured(DBError):
    """No DSN configured for the requested connection."""


class UnsupportedDialect(DBError):
    """No driver wired for the config's dialect."""


# ---------------------------------------------------------------------------
# DSN resolution
# ---------------------------------------------------------------------------
def resolve_dsn(connection: Optional[str] = None) -> Optional[str]:
    """Return the one Postgres DSN every KPI executes against.

    routing key — there is a single database behind all of them, ``CORA_PG_DSN``.
    ``connection`` is still accepted so legacy per-connection vars keep working
    for anyone who has them set, but ``CORA_PG_DSN`` takes precedence.
    """
    dsn = os.getenv("CORA_PG_DSN")
    if dsn:
        return dsn
    name = (connection or "").strip()
    if name:
        env = f"CORA_DB_{name.upper()}"
        dsn = os.getenv(env)
        if dsn:
            log.debug("resolved connection %r via legacy %s", name, env)
            return dsn
    return None


# ---------------------------------------------------------------------------
# Parameter coercion (psycopg2 text semantics -> asyncpg typed binding)
# ---------------------------------------------------------------------------
# gen_query was written for psycopg2, which sends every bind param as text and
# lets Postgres cast it (so '1' works for a boolean, '2026-06-01 00:00:00' for a
# timestamp, etc.). asyncpg uses the binary protocol and binds by the parameter's
# *actual* Postgres type, rejecting a mismatched string. Rather than guess types
# from values, we prepare the statement and ask Postgres for each parameter's
# type, then coerce the string value to match (:func:`_coerce_for_pgtype`).
import re as _re  # noqa: E402

_TS_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2})?$")
_TRUE = {"1", "t", "true", "y", "yes", "on"}
_FALSE = {"0", "f", "false", "n", "no", "off"}


def _parse_dt(value: str) -> Optional[datetime.datetime]:
    if not _TS_RE.match(value):
        return None
    v = value.replace("T", " ")
    fmt = "%Y-%m-%d %H:%M:%S" if " " in v else "%Y-%m-%d"
    try:
        return datetime.datetime.strptime(v, fmt)
    except ValueError:
        return None


def _coerce_for_pgtype(value: Any, typename: Optional[str]) -> Any:
    """Coerce a (usually string) param to the Python type asyncpg needs for the
    given Postgres type name (from a prepared statement). Non-strings and
    unknown types pass through unchanged."""
    if typename and typename.startswith("_") and isinstance(value, (list, tuple)):
        elem = typename[1:]  # array element type, e.g. _text -> text
        return [_coerce_for_pgtype(v, elem) for v in value]
    if not isinstance(value, str) or not typename:
        return value
    t = typename.lower()
    if t in ("bool", "boolean"):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        return value
    if t in ("int2", "int4", "int8", "smallint", "integer", "bigint"):
        try:
            return int(value)
        except ValueError:
            return value
    if t in ("float4", "float8", "numeric", "real", "double precision", "decimal"):
        try:
            return float(value)
        except ValueError:
            return value
    if t in ("timestamp", "timestamptz", "timestamp without time zone",
             "timestamp with time zone"):
        return _parse_dt(value) or value
    if t == "date":
        dt = _parse_dt(value)
        return dt.date() if dt else value
    return value


def _coerce_temporal(value: Any) -> Any:
    """Legacy heuristic (full timestamp strings only, not bare dates); retained
    for direct callers. The main execute path uses prepared-statement types via
    _coerce_for_pgtype."""
    if isinstance(value, str) and _TS_RE.match(value) and " " in value.replace("T", " "):
        return _parse_dt(value) or value
    if isinstance(value, list):
        return [_coerce_temporal(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# %s (psycopg) -> $n (asyncpg) conversion
# ---------------------------------------------------------------------------
def to_asyncpg(sql: str, params: List[Any]) -> Tuple[str, List[Any]]:
    """Rewrite gen_query's ``%s`` SQL + params into asyncpg ``$n`` form.

    - scalar param            -> single ``$n``
    - list param (ANY / &&)   -> single ``$n`` carrying the whole array
    - tuple param (IN / NOT IN) -> expanded to ``($n, $n+1, ...)``
    - ``%%`` (escaped literal) -> ``%``
    """
    out: List[str] = []
    new_params: List[Any] = []
    idx = 1
    pit = iter(params or [])
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "%" and i + 1 < n:
            nxt = sql[i + 1]
            if nxt == "%":
                out.append("%")
                i += 2
                continue
            if nxt == "s":
                try:
                    val = next(pit)
                except StopIteration as exc:  # pragma: no cover - defensive
                    raise DBError("more %s placeholders than params") from exc
                if isinstance(val, tuple):
                    if not val:
                        out.append("(NULL)")
                    else:
                        slots = []
                        for el in val:
                            slots.append(f"${idx}")
                            new_params.append(el)
                            idx += 1
                        out.append("(" + ", ".join(slots) + ")")
                else:
                    out.append(f"${idx}")
                    new_params.append(val)
                    idx += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out), new_params


# ---------------------------------------------------------------------------
# JSON-friendly row values
# ---------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Execution (asyncpg pool cache)
# ---------------------------------------------------------------------------
_pools: Dict[str, Any] = {}


async def _get_pool(dsn: str):
    # asyncpg pools are bound to the event loop that created them. Key the cache
    # by (loop id, dsn) so a different loop (e.g. each pytest-asyncio test) gets
    # its own pool instead of reusing one from a dead loop. The server runs a
    # single loop, so it just gets one pool per DSN.
    import asyncio
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, dsn)
    pool = _pools.get(key)
    if pool is None:
        log.info("creating asyncpg pool for %s", _redact(dsn))
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4,
                                         command_timeout=30)
        _pools[key] = pool
    return pool


def _redact(dsn: str) -> str:
    # Hide credentials in logs: keep scheme + host tail.
    if "@" in dsn:
        return dsn.split("://", 1)[0] + "://***@" + dsn.rsplit("@", 1)[1]
    return dsn


async def execute(
    dialect: str,
    connection: Optional[str],
    sql: str,
    params: List[Any],
    limit: int = 200,
) -> Dict[str, Any]:
    """Execute SQL and return ``{columns, rows, rowcount, truncated}``.

    Raises DBNotConfigured / UnsupportedDialect / DBError on failure.
    """
    if dialect != "postgres":
        raise UnsupportedDialect(
            f"dialect {dialect!r} not supported (only 'postgres' is wired via asyncpg)")
    if asyncpg is None:
        raise DBError("asyncpg is not installed; run `pip install asyncpg`")

    dsn = resolve_dsn(connection)
    if not dsn:
        raise DBNotConfigured("no Postgres DSN configured. Set CORA_PG_DSN in .env")

    # Syntax gate: parse the SQL before we touch the database, so a malformed query
    # fails fast with a clear message instead of a raw driver error.
    from cora_mcp.sql_validate import SQLSyntaxError, validate_sql
    try:
        validate_sql(sql, dialect)
    except SQLSyntaxError as exc:
        log.warning("SQL rejected by validator: %s", exc)
        raise DBError(str(exc)) from exc

    # With no bind params the SQL has no %s placeholders — pass it through verbatim.
    # (Running to_asyncpg would misread a literal '%' in e.g. LIKE '%desk%' as '%s'.)
    if params:
        aq, ap = to_asyncpg(sql, params)
    else:
        aq, ap = sql, []
    log.info("executing on %s/%s: %s", connection, dialect, aq.replace("\n", " "))
    pool = await _get_pool(dsn)
    try:
        async with pool.acquire() as con:
            # Prepare so Postgres tells us each parameter's real type, then coerce
            # gen_query's string literals ('1', '2026-06-01 00:00:00', ...) to the
            # Python types asyncpg binds for that type (bool/int/float/datetime).
            stmt = await con.prepare(aq)
            ptypes = [t.name for t in stmt.get_parameters()]
            coerced = [_coerce_for_pgtype(v, ptypes[i] if i < len(ptypes) else None)
                       for i, v in enumerate(ap)]
            records = await stmt.fetch(*coerced)
    except Exception as exc:
        log.exception("query failed: %s", exc)
        raise DBError(str(exc)) from exc

    columns = list(records[0].keys()) if records else []
    rows = [{k: _jsonable(v) for k, v in r.items()} for r in records[:limit]]
    truncated = len(records) > limit
    log.info("query ok: %d row(s)%s", len(records), " (truncated)" if truncated else "")
    return {"columns": columns, "rows": rows, "rowcount": len(records),
            "truncated": truncated}


async def close_pools() -> None:  # pragma: no cover - lifecycle helper
    for key, pool in list(_pools.items()):
        await pool.close()
        _pools.pop(key, None)
