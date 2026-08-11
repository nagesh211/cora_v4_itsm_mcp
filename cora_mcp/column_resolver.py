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
  5. a **near miss**: same meaning-carrying words, different inflection or date shape

Step 2 is the part that was previously authored but unused. ``schema_v3.yaml`` already
carries ``canonical: sla breached`` / ``alias: sector`` on many columns and
``schema_loader`` passes it through to ``describe_dataset``, but no resolution path
consulted it — so a question using the business vocabulary the schema itself declares
was rejected. Honouring it here means every column that documents its own vocabulary
becomes addressable, with no new registry to maintain.

Step 5 exists because refusing a *typo-grade* difference is worse than resolving it.
``close_date_time`` vs ``closed_date_time``, ``open_date`` vs ``opened_date`` —
these name the same clock, and rejecting one produced a clarifying question the user
could only answer by reading the schema back to us. The match is deliberately narrow:
the meaning-carrying words must be the same modulo an English inflection (``close`` ~
``closed``), and the date-shape words (``date`` / ``time`` / ``datetime`` /
``timestamp``) are compared separately so ``closed_date`` can answer for
``close_date_time`` while ``created_date`` never can. When two columns are equally
near, it stays ambiguous and returns ``None`` — guessing between ``closed_date`` and
``resolved_date`` is exactly the mistake this module exists to prevent.

Returns ``None`` rather than guessing when nothing matches; callers surface that as an
explicit "not available on this metric" instead of substituting a neighbour.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from cora_mcp.logging_config import get_logger
from cora_mcp.schema_loader import get_loader

log = get_logger(__name__)

_SEP = re.compile(r"[\s_\-]+")
_SUFFIXES = ("_name", "_description")

# Words that describe the *shape* of a timestamp rather than what it records. They are
# compared separately from the meaning-carrying words, so `close_date_time` can reach
# `closed_date` (same subject, different precision) but never `created_date`.
_SHAPE_WORDS: Dict[str, Set[str]] = {
    "date": {"date"}, "time": {"time"},
    "datetime": {"date", "time"}, "timestamp": {"date", "time"},
    "dt": {"date", "time"}, "ts": {"date", "time"},
}
# English inflections a column name and a spoken word differ by: close/closed/closes,
# open/opened, resolve/resolved, assign/assigned. Anything longer than this is a
# different word, not a different tense.
_INFLECTIONS = ("d", "ed", "s", "es", "ing", "n")
_MIN_STEM = 4          # below this, a shared prefix is coincidence, not a stem

# The trailing word a representation suffix (`_name` / `_description`) becomes once
# split on separators. Step 4 below only widens a bare word ("category") into these
# forms; it never narrows the other way. A caller that already says "category_name"
# for a column actually named `category_description` (same concept, different
# representation) fell through every step and was refused. Stripping this trailing
# word from EITHER side before the near-miss comparison fixes both directions at once.
_REPR_WORDS: Set[str] = {"name", "description"}


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


def _split_shape(term: str) -> Tuple[List[str], Set[str]]:
    """Split a term into (meaning-carrying words, date-shape words)."""
    core: List[str] = []
    shape: Set[str] = set()
    for tok in normalize(term).split():
        if tok in _SHAPE_WORDS:
            shape |= _SHAPE_WORDS[tok]
        else:
            core.append(tok)
    return core, shape


def _same_word(a: str, b: str) -> bool:
    """True when two words differ only by an English inflection."""
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return (len(short) >= _MIN_STEM and long.startswith(short)
            and long[len(short):] in _INFLECTIONS)


def _same_core(a: List[str], b: List[str]) -> bool:
    """True when both terms carry the same words, order-independent, one-to-one."""
    if len(a) != len(b) or not a:
        return False
    pool = list(b)
    for tok in a:
        hit = next((x for x in pool if _same_word(tok, x)), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


def _core_variants(core: List[str]) -> Tuple[List[str], ...]:
    """``core``, plus (when its last word is a representation suffix like "name" or
    "description") the same list with that trailing word dropped. Two representation
    forms of the same concept — "category_description" vs a caller's "category_name"
    — should compare equal on the bare subject, not on which suffix each happens to
    use, so both the with- and without-suffix reading are offered to the caller."""
    if len(core) > 1 and core[-1] in _REPR_WORDS:
        return (core, core[:-1])
    return (core,)


def _near_miss(cols: dict, word: str, eligible) -> Optional[str]:
    """The one column whose name (or declared vocabulary) is an inflection/date-shape/
    representation-suffix variant of ``word``. ``None`` when nothing is near, or when
    two columns are equally near — an ambiguous clock must be asked about, not
    guessed at."""
    w_core, w_shape = _split_shape(word)
    if not w_core:
        return None                      # "date time" alone names no subject
    w_variants = _core_variants(w_core)
    ranked: List[Tuple[int, str]] = []
    for name, ci in cols.items():
        if not eligible(name, ci):
            continue
        best: Optional[int] = None
        for term in [name] + _meta_terms(ci):
            c_core, c_shape = _split_shape(term)
            # A date-shaped word must match a date-shaped column: `closed` on its own
            # is not evidence enough to pick a timestamp.
            if bool(w_shape) != bool(c_shape):
                continue
            matched = any(_same_core(w_variant, c_variant)
                          for w_variant in w_variants
                          for c_variant in _core_variants(c_core))
            if not matched:
                continue
            dist = len(w_shape ^ c_shape)
            best = dist if best is None else min(best, dist)
        if best is not None:
            ranked.append((best, name))
    if not ranked:
        return None
    ranked.sort()
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        log.info("column_resolver: %r is equally near %s on %s — ambiguous, refusing",
                 word, [n for _d, n in ranked if _d == ranked[0][0]], "this table")
        return None
    return ranked[0][1]


def resolve_column_detail(fqn: str, word: str,
                          roles: tuple = ()) -> Tuple[Optional[str], Optional[str]]:
    """:func:`resolve_column`, plus HOW the word was resolved.

    The ``how`` is ``exact`` | ``vocabulary`` | ``alias`` | ``suffix`` | ``near_miss``
    (``None`` when unresolved). Callers use it to report an approximate match in the
    answer — resolving ``close_date_time`` to ``closed_date`` is the right call, but
    only if the user is told which clock actually ran.
    """
    if not word:
        return None, None
    cols = get_loader().table_columns(fqn)
    if not cols:
        return None, None

    def _eligible(name: str, ci: dict) -> bool:
        return not roles or (ci.get("role") or "dimension") in roles

    w = normalize(word)

    # 1) exact column name
    for name, ci in cols.items():
        if normalize(name) == w and _eligible(name, ci):
            return name, "exact"
    # 2) the column's own declared vocabulary
    for name, ci in cols.items():
        if w in _meta_terms(ci) and _eligible(name, ci):
            return name, "vocabulary"
    # 3) filter alias -> canonical key, then retry 1-2 with the key
    from cora_mcp.filter_aliases import get_registry
    canonical = get_registry().canonical_for(word)
    if canonical and normalize(canonical) != w:
        hit, how = resolve_column_detail(fqn, canonical, roles=roles)
        if hit:
            return hit, "alias" if how in ("exact", "vocabulary") else how
    # 4) suffix forms: business -> business_name
    wanted = {w + normalize(s) for s in _SUFFIXES} | {w}
    for name, ci in cols.items():
        n = normalize(name)
        if not _eligible(name, ci):
            continue
        if n in wanted:
            return name, "suffix"
        for suf in _SUFFIXES:
            if name.lower().endswith(suf) and normalize(name[: -len(suf)]) == w:
                return name, "suffix"
    # 5) near miss: same subject, different inflection or timestamp precision
    hit = _near_miss(cols, word, _eligible)
    if hit:
        log.info("column_resolver: %r resolved to near-miss column %r on %s",
                 word, hit, fqn)
        return hit, "near_miss"
    return None, None


def resolve_column(fqn: str, word: str, roles: tuple = ()) -> Optional[str]:
    """The column on ``fqn`` that ``word`` refers to, or None.

    ``roles`` optionally restricts to schema roles (e.g. ``("dimension",)`` for a
    group-by, so a breakdown can never land on a measure or a timestamp). An empty
    tuple accepts any role, which is what filtering wants.
    """
    return resolve_column_detail(fqn, word, roles=roles)[0]


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