"""Filter-VALUE resolution against a column's known domain (``possible_values``).

The KEY side — which column a user term filters — is handled by
``filter_aliases`` / ``resolve_filter_key``. This is the VALUE side.

A user asks for changes that are "completed", but the column
``itsm_change.status_name`` only ever holds ``{SCHEDULED, REVIEW, CLOSED,
ASSESS, AUTHORIZE, NEW, IMPLEMENT}``. Left alone the builder emits
``lower(status_name) = 'completed'`` — syntactically valid, but it matches zero
rows: a silently-wrong answer. This module maps a user value to the real stored
value using, in order:

  1. **exact** match against ``possible_values`` (case-insensitive / space-folded)
  2. a curated **synonym** map (``value_aliases.json``), keyed by column name with
     a ``global`` fallback; the synonym's target must itself be a real
     ``possible_value`` (so the schema stays the source of truth)
  3. a **fuzzy** near-match against ``possible_values`` (difflib), above a cutoff

If none hits AND the column declares a domain, the value is a **reject** — the
caller raises with the valid list (a clear error beats a clean-but-empty result).
If the column has no declared ``possible_values``, the value passes through
unchanged (we cannot validate a domain we do not know).
"""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ALIASES_PATH = os.path.normpath(os.path.join(_HERE, "..", "value_aliases.json"))

# Fuzzy cutoff: 0..1 (difflib ratio). Deliberately high — we would rather reject
# and show the valid list than silently coerce to a wrong-but-similar value.
_FUZZY_CUTOFF = 0.84


def normalize(s: Any) -> str:
    """Case-fold and fold spaces/underscores/hyphens so 'In_Progress' matches
    'in progress'. Runs of separators collapse to a single space."""
    text = str(s).strip().lower()
    out = []
    prev_sep = False
    for ch in text:
        if ch in (" ", "_", "-", "\t"):
            if not prev_sep:
                out.append(" ")
            prev_sep = True
        else:
            out.append(ch)
            prev_sep = False
    return "".join(out).strip()


@lru_cache(maxsize=1)
def _load() -> Dict[str, Any]:
    try:
        with open(_ALIASES_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        log.info("value_aliases.json not found at %s; value synonyms disabled", _ALIASES_PATH)
        return {"global": {}, "by_column": {}}
    # normalize all synonym keys once
    def _norm_section(sec: Dict[str, Any]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for k, v in (sec or {}).items():
            out[normalize(k)] = v if isinstance(v, list) else [v]
        return out
    return {
        "global": _norm_section(raw.get("global")),
        "by_column": {normalize(col): _norm_section(m)
                      for col, m in (raw.get("by_column") or {}).items()},
    }


def _synonym_targets(column: str, norm_value: str) -> List[str]:
    """Candidate stored values a synonym could map to (column-specific first)."""
    reg = _load()
    col_map = reg["by_column"].get(normalize(column), {})
    if norm_value in col_map:
        return col_map[norm_value]
    return reg["global"].get(norm_value, [])


@dataclass
class ValueResolution:
    """Outcome of resolving one user value against a column's domain."""
    column: str
    input: Any
    resolved: Any            # the value to actually use in the query
    matched: bool            # False -> a reject (no confident mapping to the domain)
    method: str              # exact | synonym | fuzzy | passthrough | reject
    valid_values: Optional[List[Any]] = None   # the domain, for a helpful message


def resolve_value(column: str, raw: Any,
                  possible_values: Optional[List[Any]]) -> ValueResolution:
    """Resolve ONE value against ``possible_values``. See module docstring."""
    if not possible_values:                       # unknown domain -> can't validate
        return ValueResolution(column, raw, raw, True, "passthrough")

    pv = list(possible_values)
    by_norm = {normalize(v): v for v in pv}       # normalized -> canonical stored form
    nraw = normalize(raw)

    # 1) exact (case-insensitive)
    if nraw in by_norm:
        canon = by_norm[nraw]
        return ValueResolution(column, raw, canon, True, "exact", pv)

    # 2) synonym map -> first candidate that is a real possible_value
    for cand in _synonym_targets(column, nraw):
        ncand = normalize(cand)
        if ncand in by_norm:
            return ValueResolution(column, raw, by_norm[ncand], True, "synonym", pv)

    # 3) fuzzy near-match against the domain
    close = difflib.get_close_matches(nraw, list(by_norm), n=1, cutoff=_FUZZY_CUTOFF)
    if close:
        return ValueResolution(column, raw, by_norm[close[0]], True, "fuzzy", pv)

    # 4) reject — the domain is known and nothing matched
    return ValueResolution(column, raw, raw, False, "reject", pv)


def resolve_values(column: str, raws: List[Any],
                   possible_values: Optional[List[Any]]
                   ) -> Tuple[List[Any], List[ValueResolution]]:
    """Resolve a list of values. Returns (resolved_values, rejects). ``resolved``
    contains a usable value for every input (rejects keep their original so the
    caller can choose to warn rather than fail); ``rejects`` lists the ones that
    did not map to the domain."""
    resolved: List[Any] = []
    rejects: List[ValueResolution] = []
    for raw in raws:
        r = resolve_value(column, raw, possible_values)
        resolved.append(r.resolved)
        if not r.matched:
            rejects.append(r)
        elif r.method in ("synonym", "fuzzy"):
            log.info("value_resolver: %s %r -> %r (%s)", column, raw, r.resolved, r.method)
    return resolved, rejects


def resolve_or_raise(column: str, raws: List[Any],
                     possible_values: Optional[List[Any]],
                     exc_type=ValueError) -> List[Any]:
    """Resolve values; raise ``exc_type`` with the valid list if any is unknown."""
    resolved, rejects = resolve_values(column, raws, possible_values)
    if rejects:
        bad = ", ".join(repr(r.input) for r in rejects)
        raise exc_type(
            f"value(s) {bad} not valid for {column!r}. "
            f"valid values: {list(possible_values)}")
    return resolved