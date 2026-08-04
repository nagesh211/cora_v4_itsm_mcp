"""Record-detail lookups: turn a specific record id (e.g. ``INC0353896``) into a
detail row + its linked records — never a metric / ``count(*)``.

When a user names a concrete record and asks "get me details" the right answer is
the record's own columns (and, if asked, the entities linked to it), NOT a KPI or
an aggregate. This module owns that path:

  * :func:`detect_records` — deterministically find record ids in a question and
    map each to its entity + human id column via ``record_prefixes.json``.
  * :func:`curated_detail_columns` — pick a readable, schema-driven column set for
    an entity (human identifiers + key dimensions + timestamps), capped.
  * :func:`registry` — the prefix / entity-alias registry (extend the JSON, no
    code change) so new modules/record types self-register.

The registry file is data, not code: add a prefix line for a new record type and
it works immediately. Anything not in the schema is still rejected downstream by
``sql_builder`` — this module only decides *what* to select, never emits SQL.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from cora_mcp.logging_config import get_logger
from cora_mcp.schema_loader import get_loader

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
DEFAULT_PREFIXES_PATH = os.path.join(_ROOT, "record_prefixes.json")

# A record id: 2-6 letters then >=4 digits (INC0353896, CHG0012345, RITM0009999).
_RECORD_RE = re.compile(r"\b([A-Za-z]{2,6})(\d{4,})\b")

# How many columns of each role to surface in a detail row (readable, not noisy).
_MAX_DIMS = 12
_MAX_TS = 6


def _plural(word: str) -> str:
    """Good-enough English plural for an entity noun ("incident" -> "incidents",
    "availability" -> "availabilities"). Only ever used to widen an alias set, so an
    imperfect form costs nothing — it simply never matches a question."""
    if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def _singular(word: str) -> str:
    """Inverse of :func:`_plural`, for an entity whose name is ALREADY plural — the
    entity is ``major_incidents``, and a user says "major incident"."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def _derived_entity_aliases() -> Dict[str, str]:
    """{alias -> entity slug} straight from the schema's entity names.

    This is the fix for a whole class of defect rather than one instance of it. The
    aliases used to be hand-typed, and a typo is invisible: ``_identify_entities``
    filters candidates against ``loader.all_entities()``, so an alias pointing at a slug
    that does not exist is silently DROPPED, not reported. ``itsm_servicerequest`` (the
    real slug is ``itsm_service_request``) therefore disabled every service-request
    question, and the availability entity was simply never listed — which is how
    "incidents that impacted availability percentage" was answered from the
    major-incident table.

    Deriving means the obvious names can never be wrong or missing; the JSON keeps only
    vocabulary no rule produces (``ritm``, ``uptime``, ``downtime``, ``outage``).
    """
    out: Dict[str, str] = {}
    for _mod, slug, ent in get_loader().all_entities():
        name = (ent.get("name") or "").strip().lower()
        if not name:
            continue
        base = name.replace("_", " ")
        # One step in whichever direction the name is not already in — same rule as
        # module_registry._derived_aliases ("changes" <-> "change"). An entity named in
        # the plural (``major_incidents``) needs the singular a user actually says.
        other = _singular(base) if base.endswith("s") else _plural(base)
        for alias in (base, other):
            out.setdefault(alias, slug)
    return out


class RecordRegistry:
    """Prefix -> entity/id-column and entity-alias index, loaded from JSON."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_PREFIXES_PATH
        self._prefixes: Dict[str, Dict[str, str]] = {}   # PREFIX -> {entity, id_column}
        self._entity_aliases: Dict[str, str] = {}        # alias -> entity slug
        self._load()

    def _load(self) -> None:
        # Derived FIRST, and outside the file check. The schema-derived aliases are not an
        # enhancement to the file — they are the base layer, and the file is an overlay for
        # vocabulary no rule can produce. Deriving inside the file-exists branch made the
        # base layer depend on the very file it is meant to make optional: with the file
        # absent, alias resolution went to zero instead of degrading to the obvious names.
        self._entity_aliases.update(_derived_entity_aliases())
        derived = len(self._entity_aliases)
        valid = set(get_loader().entity_slugs())

        if not os.path.isfile(self.path):
            # Prefixes have NO derivable equivalent — nothing in the schema says an
            # incident id starts with "INC" — so record lookup by id really is disabled
            # here, while entity aliases keep working.
            log.warning("no record_prefixes.json at %s; record lookup by id is disabled "
                        "(%d schema-derived entity alias(es) still available)",
                        self.path, derived)
            return
        doc = json.load(open(self.path, encoding="utf-8")) or {}
        for pfx, meta in (doc.get("prefixes") or {}).items():
            if isinstance(meta, dict) and meta.get("entity"):
                self._prefixes[pfx.upper()] = {
                    "entity": meta["entity"],
                    "id_column": meta.get("id_column"),
                }
        dangling = []
        for alias, slug in (doc.get("entity_aliases") or {}).items():
            if slug not in valid:
                # Loud rather than silent: downstream this alias just vanishes, and the
                # question quietly routes to whatever else it mentions.
                dangling.append("%s -> %s" % (alias, slug))
            self._entity_aliases[alias.strip().lower()] = slug
        if dangling:
            log.warning("record_prefixes.json entity_aliases point at slugs absent from "
                        "the schema (they will never match): %s. valid slugs: %s",
                        dangling, sorted(valid))
        bad_prefixes = [f"{p} -> {m['entity']}" for p, m in self._prefixes.items()
                        if m.get("entity") not in valid]
        if bad_prefixes:
            log.warning("record_prefixes.json prefixes point at slugs absent from the "
                        "schema (get_record on such an id will fail): %s", bad_prefixes)
        log.info("record registry: %d prefix(es), %d entity alias(es) "
                 "(%d derived from schema, %d from file)",
                 len(self._prefixes), len(self._entity_aliases), derived,
                 len(doc.get("entity_aliases") or {}))

    def prefix(self, pfx: str) -> Optional[Dict[str, str]]:
        return self._prefixes.get((pfx or "").upper())

    def entity_for_alias(self, term: str) -> Optional[str]:
        return self._entity_aliases.get((term or "").strip().lower())

    def known_prefixes(self) -> List[str]:
        return sorted(self._prefixes)


@lru_cache(maxsize=1)
def registry() -> RecordRegistry:
    return RecordRegistry()


def _human_id_column(slug: str, fallback: Optional[str] = None) -> Optional[str]:
    """The id column a human types (e.g. incident_id) — prefer a *_id identifier
    that is NOT a *_system_id; fall back to the registry-provided one."""
    detail = get_loader().entity_detail(slug) or {}
    idents = detail.get("identifiers") or []
    if fallback and fallback in idents:
        return fallback
    human = [c for c in idents if c.endswith("_id") and not c.endswith("_system_id")]
    if human:
        return human[0]
    non_system = [c for c in idents if not c.endswith("_system_id")]
    if non_system:
        return non_system[0]
    return fallback or (idents[0] if idents else None)


@lru_cache(maxsize=1)
def _id_column_by_entity() -> Dict[str, str]:
    """{entity slug -> the human id column}, taken from the ``prefixes`` section.

    ``prefixes`` already records this (``REL`` -> ``itsm_release`` / ``release_number``),
    and it is AUTHORITATIVE rather than redundant: the schema-only heuristic in
    :func:`_human_id_column` picks the first non-system ``*_id`` it sees, which is
    ``change_id`` for ``itsm_release`` and ``first_task_id`` for
    ``itsm_service_request`` — both wrong. Anything keyed on a derived id (a linked-record
    lookup, a predicate's grain key) silently used the wrong column for those entities.
    """
    out: Dict[str, str] = {}
    for meta in registry()._prefixes.values():
        slug, col = meta.get("entity"), meta.get("id_column")
        if slug and col:
            out.setdefault(slug, col)
    return out


def _entity_id_column(slug: str) -> Optional[str]:
    """The human id column for an entity — declared value first, heuristic second.

    Prefer this over :func:`_human_id_column` wherever there is no prefix in hand; the
    bare heuristic is only correct for entities whose id column happens to sort first.

    The declared column is accepted whatever ROLE the schema gave it, which
    :func:`_human_id_column` cannot do because it only looks at the identifier list.
    ``release_number`` is declared ``role: dimension`` on
    ``itsm_release.tbl_pepops_release_mgmt`` even though it is plainly the human id, so
    checking the identifier list rejected it and the heuristic fell through to
    ``change_id`` — a release listing keyed on the id of a different entity.
    """
    declared = _id_column_by_entity().get(slug)
    if declared:
        detail = get_loader().entity_detail(slug) or {}
        present = {e["name"] for entries in (detail.get("columns_by_role") or {}).values()
                   for e in entries}
        if declared in present:
            return declared
        log.warning("record_prefixes.json declares id_column %r for %s but no such "
                    "column exists; falling back to the schema heuristic",
                    declared, slug)
    return _human_id_column(slug, declared)


def resolve_entity(*, entity: Optional[str], prefix: Optional[str]) -> Tuple[str, str]:
    """Resolve to (entity_slug, id_column) from an explicit entity name/slug or a
    detected id prefix. Raises ValueError with guidance if it can't."""
    reg = registry()
    loader = get_loader()
    valid = {slug for _m, slug, _e in loader.all_entities()}

    if entity:
        slug = entity if entity in valid else reg.entity_for_alias(entity)
        if not slug or slug not in valid:
            raise ValueError(
                f"unknown entity {entity!r}. valid: {sorted(valid)}")
        return slug, _entity_id_column(slug)

    if prefix:
        meta = reg.prefix(prefix)
        if not meta:
            raise ValueError(
                f"unknown record prefix {prefix!r}. known: {reg.known_prefixes()}. "
                f"Add it to record_prefixes.json or pass `entity` explicitly.")
        slug = meta["entity"]
        return slug, _human_id_column(slug, meta.get("id_column"))

    raise ValueError("need an `entity` or a record id with a known prefix")


def detect_records(text: str) -> List[Dict[str, str]]:
    """Find record ids in free text and map each to its entity + id column.

    Returns [{record_id, prefix, entity, id_column}]; ids whose prefix is not in
    the registry are skipped (so arbitrary CI names don't become record lookups)."""
    reg = registry()
    out: List[Dict[str, str]] = []
    seen = set()
    for m in _RECORD_RE.finditer(text or ""):
        rid = m.group(0).upper()
        if rid in seen:
            continue
        meta = reg.prefix(m.group(1))
        if not meta:
            continue
        seen.add(rid)
        slug = meta["entity"]
        out.append({
            "record_id": rid,
            "prefix": m.group(1).upper(),
            "entity": slug,
            "id_column": _human_id_column(slug, meta.get("id_column")),
        })
    return out


def curated_detail_columns(slug: str, id_column: Optional[str] = None,
                           table: Optional[str] = None) -> List[str]:
    """A readable, schema-driven detail column set for an entity: the human id,
    key dimensions and timestamps (capped). Deterministic and role-driven, so it
    tracks the schema automatically as columns are added.

    ``table`` restricts the result to columns that actually exist in that table
    (an entity can span several tables, but a detail query hits only one), so the
    builder never asks for a column the queried table lacks."""
    detail = get_loader().entity_detail(slug)
    if not detail:
        return []
    loader = get_loader()

    def _in_table(name: str) -> bool:
        return table is None or loader.column_info(table, name) is not None

    cols: List[str] = []

    def _add(name: Optional[str]) -> None:
        if name and name not in cols and _in_table(name):
            cols.append(name)

    _add(id_column)
    # other human-facing identifiers (skip opaque *_system_id)
    for c in detail.get("identifiers") or []:
        if not c.endswith("_system_id"):
            _add(c)
    n_dims = 0
    for c in detail.get("dimensions") or []:
        if n_dims >= _MAX_DIMS:
            break
        if _in_table(c) and c not in cols:
            cols.append(c)
            n_dims += 1
    n_ts = 0
    for c in detail.get("timestamps") or []:
        if n_ts >= _MAX_TS:
            break
        if _in_table(c) and c not in cols:
            cols.append(c)
            n_ts += 1
    return cols