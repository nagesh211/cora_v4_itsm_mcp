"""Pre-execution SQL syntax validation via sqlglot.

Every query — governed KPI SQL and dynamically-built queries alike — is parsed
with sqlglot (Postgres dialect) before it is sent to the database, so a malformed
statement fails fast with a clear message instead of a raw driver error (or, worse,
silently wrong results). This complements ``sql_builder._guard_readonly`` (which
enforces SELECT-only / no ``;``): that guards *intent*, this guards *syntax*.

The check is best-effort and fail-open on the parser itself:
  * if sqlglot isn't installed, validation is skipped (logged once);
  * set ``CORA_SQL_VALIDATE=off`` to disable;
The placeholders gen_query emits (``%s``) and the asyncpg form (``$1``) both parse
under the Postgres dialect, so validation runs on the SQL as-is.
"""
from __future__ import annotations

import os
from typing import Optional

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

try:
    import sqlglot
    from sqlglot.errors import ParseError
except Exception:  # pragma: no cover - optional dependency
    sqlglot = None
    ParseError = Exception


class SQLSyntaxError(ValueError):
    """The generated SQL is not syntactically valid."""


def _enabled() -> bool:
    return os.getenv("CORA_SQL_VALIDATE", "on").strip().lower() not in ("off", "0", "false", "no")


def validate_sql(sql: str, dialect: str = "postgres") -> None:
    """Raise :class:`SQLSyntaxError` if ``sql`` doesn't parse. No-op when sqlglot
    is unavailable or validation is disabled."""
    if not _enabled() or sqlglot is None:
        return
    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except ParseError as exc:
        # keep the message compact — sqlglot's is multi-line with a caret
        msg = str(exc).splitlines()[0]
        raise SQLSyntaxError(f"SQL failed syntax validation: {msg}") from exc
    if not statements or statements[0] is None:
        raise SQLSyntaxError("SQL failed syntax validation: empty statement")
    if len(statements) > 1:
        raise SQLSyntaxError("SQL failed syntax validation: multiple statements not allowed")