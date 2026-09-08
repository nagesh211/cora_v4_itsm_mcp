"""Loads the rich ``schema_v3.yaml`` and indexes modules -> entities -> tables.

Read-only. Uses PyYAML (comments are irrelevant at read time). Everything is
returned as plain JSON-serialisable dicts so the values can be handed straight
back through MCP tools.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import yaml

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
DEFAULT_SCHEMA_PATH = os.path.join(_ROOT, "schema_v3.yaml")

_ROLES = ("timestamp", "identifier", "measure", "dimension", "metadata")


def _slug(*parts: str) -> str:
    raw = "_".join(str(p) for p in parts).lower()
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")


class SchemaLoader:
    """Index over the rich schema catalog."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_SCHEMA_PATH
        log.info("loading schema catalog: %s", self.path)
        with open(self.path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        self._modules: List[dict] = doc.get("modules") or []
        # name -> module ; slug -> (module_name, entity_dict)
        self._by_module: Dict[str, dict] = {m["name"]: m for m in self._modules}
        self._by_entity_slug: Dict[str, Tuple[str, dict]] = {}
        # schema.table -> (module_name, entity_dict, table_dict); first occurrence wins.
        self._by_table: Dict[str, Tuple[str, dict, dict]] = {}
        for m in self._modules:
            for e in m.get("entities") or []:
                self._by_entity_slug[_slug(m["name"], e["name"])] = (m["name"], e)
                for t in e.get("tables") or []:
                    self._by_table.setdefault(t["name"], (m["name"], e, t))
        log.info("schema loaded: %d modules, %d entities, %d tables",
                 len(self._modules), len(self._by_entity_slug), len(self._by_table))

    # ---- modules ---------------------------------------------------------
    def module_names(self) -> List[str]:
        return [m["name"] for m in self._modules]

    def get_module(self, name: str) -> Optional[dict]:
        return self._by_module.get(name)

    def module_summary(self, name: str) -> Optional[dict]:
        m = self.get_module(name)
        if not m:
            return None
        entities = m.get("entities") or []
        return {
            "name": m["name"],
            "database_type": m.get("database_type"),
            "code": m.get("code"),
            "is_date_applicable": m.get("is_date_applicable"),
            "description": m.get("description"),
            "coverage": m.get("coverage"),
            "entities": [
                {"name": e["name"], "slug": _slug(m["name"], e["name"]),
                 "table_count": len(e.get("tables") or [])}
                for e in entities
            ],
        }

    def all_module_summaries(self) -> List[dict]:
        return [self.module_summary(n) for n in self.module_names()]

    # ---- entities --------------------------------------------------------
    def entity_slugs(self) -> List[str]:
        return list(self._by_entity_slug.keys())

    def all_entities(self) -> List[Tuple[str, str, dict]]:
        """Return (module_name, slug, entity_dict) for every entity."""
        return [(mod, _slug(mod, e["name"]), e)
                for slug, (mod, e) in self._by_entity_slug.items()]

    def get_entity(self, slug: str) -> Optional[Tuple[str, dict]]:
        return self._by_entity_slug.get(slug)

    @staticmethod
    def entity_slug(module_name: str, entity_name: str) -> str:
        return _slug(module_name, entity_name)

    # ---- table-level lookups (for the dynamic SQL builder) ---------------
    def get_table(self, fqn: str) -> Optional[dict]:
        rec = self._by_table.get(fqn)
        return rec[2] if rec else None

    def table_module(self, fqn: str) -> Optional[str]:
        """Owning module for a ``schema.table`` in the catalog's own module
        vocabulary -- the fallback attribution for a table that no KPI config
        anchors on (so ad-hoc / raw SQL can still be attributed)."""
        rec = self._by_table.get(fqn) or self._by_table.get((fqn or "").lower())
        return rec[0] if rec else None

    def known_table_fqns(self) -> List[str]:
        """Every ``schema.table`` this catalog knows about -- used to suggest a
        near-match for a table name that doesn't exist (e.g. free-text SQL that
        typo'd a relation)."""
        return list(self._by_table.keys())

    def table_columns(self, fqn: str) -> Dict[str, dict]:
        rec = self._by_table.get(fqn)
        if not rec:
            return {}
        return {c["name"]: c for c in rec[2].get("columns") or []}

    def column_info(self, fqn: str, col: str) -> Optional[dict]:
        return self.table_columns(fqn).get(col)

    def tables_with_column(self, col: str) -> List[str]:
        """Every table fqn that declares a column named ``col`` (exact match).
        Used to tell a caller a column exists but in a table not in their query."""
        out = []
        for fqn, rec in self._by_table.items():
            if any(c.get("name") == col for c in rec[2].get("columns") or []):
                out.append(fqn)
        return out

    def column_names_in(self, fqns: List[str]) -> List[str]:
        """All distinct column names available across the given tables."""
        names: List[str] = []
        seen = set()
        for fqn in fqns:
            for name in self.table_columns(fqn):
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def table_time_field(self, fqn: str) -> Optional[str]:
        rec = self._by_table.get(fqn)
        if not rec:
            return None
        return (rec[2].get("time") or {}).get("field")

    def entity_primary_table(self, slug: str) -> Optional[str]:
        found = self.get_entity(slug)
        if not found:
            return None
        _, entity = found
        tables = entity.get("tables") or []
        return tables[0]["name"] if tables else None

    # ---- columns / tables ------------------------------------------------
    @staticmethod
    def table_fqns(entity: dict) -> List[str]:
        return [t.get("name") for t in (entity.get("tables") or [])]

    @staticmethod
    def _columns(entity: dict) -> List[Tuple[str, dict]]:
        """(table_name, column_dict) across all tables of the entity."""
        out: List[Tuple[str, dict]] = []
        for t in entity.get("tables") or []:
            for c in t.get("columns") or []:
                out.append((t.get("name"), c))
        return out

    def entity_detail(self, slug: str) -> Optional[dict]:
        """A tool-ready description of one entity: columns grouped by role,
        possible values, and per-table time blocks."""
        found = self.get_entity(slug)
        if not found:
            return None
        module_name, entity = found
        module = self.get_module(module_name) or {}

        by_role: Dict[str, List[dict]] = {r: [] for r in _ROLES}
        possible_values: Dict[str, list] = {}
        seen = set()
        for table_name, col in self._columns(entity):
            name = col.get("name")
            role = col.get("role", "dimension")
            key = (name, role)
            if key in seen:
                continue
            seen.add(key)
            entry = {"name": name, "type": col.get("type"), "table": table_name}
            for opt in ("canonical", "alias", "primary_key", "deeper_insights", "cross_join"):
                if col.get(opt) is not None:
                    entry[opt] = col[opt]
            by_role.setdefault(role, []).append(entry)
            if col.get("possible_values"):
                possible_values[name] = list(col["possible_values"])

        tables = []
        for t in entity.get("tables") or []:
            tables.append({
                "name": t.get("name"),
                "description": t.get("description"),
                "time": t.get("time"),
                "column_count": len(t.get("columns") or []),
            })

        return {
            "slug": slug,
            "module": module_name,
            "database_type": module.get("database_type"),
            "entity": entity.get("name"),
            "description": entity.get("description"),
            "tables": tables,
            "dimensions": [c["name"] for c in by_role.get("dimension", [])],
            "measures": [c["name"] for c in by_role.get("measure", [])],
            "timestamps": [c["name"] for c in by_role.get("timestamp", [])],
            "identifiers": [c["name"] for c in by_role.get("identifier", [])],
            "metadata_cols": [c["name"] for c in by_role.get("metadata", [])],
            "columns_by_role": by_role,
            "possible_values": possible_values,
        }


@lru_cache(maxsize=1)
def get_loader() -> SchemaLoader:
    """Process-wide singleton loader."""
    return SchemaLoader()
