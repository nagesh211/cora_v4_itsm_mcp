"""Resolve a user's word to a real column ON A GIVEN TABLE.

A breakdown or filter word is rarely the literal column name. "business" is
``business_name``, "capability" is ``service_area``, "team" is
``assignment_group_name`` — and which physical column a word means **differs per
table**, so resolution has to be table-scoped rather than global.

Resolution order (first hit wins, whole-string matching only — never a substring, so
``sub business`` can never collide with ``business``):

  1. the exact column name
  2. the column's schema ``canonical`` / ``alias`` metadata
  3. ``filter_aliases.json``: alias -> canonical key, then retry 1-2 with that key
  4. the suffix forms ``<word>_name`` / ``<word>_description``

Step 2 is the part that was previously authored but unused. ``schema_v3.yaml`` already
carries ``canonical: sla breached`` / ``alias: sector`` on many columns and
``schema_loader`` passes it through to ``describe_dataset``, but no resolution path
consulted it — so a question using the business vocabulary the schema itself declares
was rejected. Honouring it here means every column that documents its own vocabulary
becomes addressable, with no new registry to maintain.

Returns ``None`` rather than guessing when nothing matches; callers surface that as an
explicit "not available on this metric" instead of substituting a neighbour.
"""
from __future__ import annotations

import re
from typing import List, Optional

from cora_mcp.logging_config import get_logger
from cora_mcp.schema_loader import get_loader

log = get_logger(__name__)

_SEP = re.compile(r"[\s_\-]+")
_SUFFIXES = ("_name", "_description")


def normalize(term: str) -> str:
    return _SEP.sub(" ", (term or "").strip().lower())


def _meta_terms(ci: dict) -> List[str]:
    """The vocabulary a column declares for itself (``canonical`` / ``alias``). Both
    may hold a comma-separated list, e.g. ``canonical: user id, requestor``."""
    out: List[str] = []
    for key in ("canonical", "alias"):
        raw = ci.get(key)
        if not raw:
            continue
        for part in str(raw).split(","):
            part = normalize(part)
            if part:
                out.append(part)
    return out


def resolve_column(fqn: str, word: str, roles: tuple = ()) -> Optional[str]:
    """The column on ``fqn`` that ``word`` refers to, or None.

    ``roles`` optionally restricts to schema roles (e.g. ``("dimension",)`` for a
    group-by, so a breakdown can never land on a measure or a timestamp). An empty
    tuple accepts any role, which is what filtering wants.
    """
    if not word:
        return None
    cols = get_loader().table_columns(fqn)
    if not cols:
        return None

    def _eligible(name: str, ci: dict) -> bool:
        return not roles or (ci.get("role") or "dimension") in roles

    w = normalize(word)

    # 1) exact column name
    for name, ci in cols.items():
        if normalize(name) == w and _eligible(name, ci):
            return name
    # 2) the column's own declared vocabulary
    for name, ci in cols.items():
        if w in _meta_terms(ci) and _eligible(name, ci):
            return name
    # 3) filter alias -> canonical key, then retry 1-2 with the key
    from cora_mcp.filter_aliases import get_registry
    canonical = get_registry().canonical_for(word)
    if canonical and normalize(canonical) != w:
        hit = resolve_column(fqn, canonical, roles=roles)
        if hit:
            return hit
    # 4) suffix forms: business -> business_name
    wanted = {w + normalize(s) for s in _SUFFIXES} | {w}
    for name, ci in cols.items():
        n = normalize(name)
        if not _eligible(name, ci):
            continue
        if n in wanted:
            return name
        for suf in _SUFFIXES:
            if name.lower().endswith(suf) and normalize(name[: -len(suf)]) == w:
                return name
    return None


def resolvable_words(fqn: str) -> List[str]:
    """Every word that resolves on ``fqn`` — column names plus declared vocabulary.
    Used to build actionable "did you mean" errors instead of a bare rejection."""
    cols = get_loader().table_columns(fqn)
    out: List[str] = []
    for name, ci in cols.items():
        out.append(name)
        out.extend(_meta_terms(ci))
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq