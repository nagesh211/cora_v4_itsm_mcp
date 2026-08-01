"""Relationship graph + join planner over the schema's ``relationships:`` blocks.

Each module in ``schema_v3.yaml`` may declare semantic edges between entity tables
(optionally through a ``via`` relation table, with an optional constant
discriminator). :class:`RelationshipGraph` loads them and :meth:`plan_join`
returns the ordered JOIN clauses needed to connect a base table to one or more
target tables — the deterministic backbone for cross-entity queries.

A ``JoinClause`` is builder-ready::

    {
      "table": "itsm.tbl_incident_change_relation",   # table to JOIN in
      "on":   [(left_fqn, left_col, this_fqn, this_col), ...],
      "const":[(this_fqn, col, value), ...],           # e.g. type='Caused By Change'
      "relationship": "incident_caused_by_change",
    }
"""
from __future__ import annotations

from collections import deque
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from cora_mcp.logging_config import get_logger
from cora_mcp.schema_loader import get_loader

log = get_logger(__name__)


class NoJoinPathError(ValueError):
    """No relationship path connects the requested tables."""


class RelationshipGraph:
    def __init__(self, loader=None):
        self.loader = loader or get_loader()
        # table_fqn -> list of (neighbor_fqn, relationship_dict)
        self._adj: Dict[str, List[Tuple[str, dict]]] = {}
        self._by_name: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        count = 0
        for module_name in self.loader.module_names():
            module = self.loader.get_module(module_name) or {}
            for rel in module.get("relationships") or []:
                left, right = rel.get("left"), rel.get("right")
                if not left or not right:
                    continue
                rel = {**rel, "module": module_name}
                self._by_name[rel["name"]] = rel
                self._adj.setdefault(left, []).append((right, rel))
                self._adj.setdefault(right, []).append((left, rel))
                count += 1
        log.info("relationship graph: %d edge(s), %d node(s)", count, len(self._adj))

    # ---- introspection ---------------------------------------------------
    def relationships(self, module: Optional[str] = None) -> List[dict]:
        rels = list(self._by_name.values())
        if module:
            rels = [r for r in rels if r.get("module") == module]
        # strip internal key
        return [{k: v for k, v in r.items() if k != "module"} for r in rels]

    def tables(self) -> List[str]:
        return sorted(self._adj.keys())

    # ---- planning --------------------------------------------------------
    def _bfs(self, base: str, target: str) -> Optional[List[Tuple[str, str, dict]]]:
        """Shortest path base->target as a list of (from_fqn, to_fqn, rel)."""
        if base == target:
            return []
        seen = {base}
        queue = deque([(base, [])])
        while queue:
            node, path = queue.popleft()
            for neighbor, rel in self._adj.get(node, []):
                if neighbor in seen:
                    continue
                new_path = path + [(node, neighbor, rel)]
                if neighbor == target:
                    return new_path
                seen.add(neighbor)
                queue.append((neighbor, new_path))
        return None

    def _edge_clauses(self, rel: dict, frm: str, to: str,
                      joined: set) -> List[dict]:
        """JOIN clause(s) to add `to` (a new entity table) onto `frm` (already
        joined), traversing relationship `rel`."""
        name = rel.get("name")
        via = rel.get("via")
        clauses: List[dict] = []
        if via:
            # Orient left_on / right_on: the group whose entity == frm binds frm.
            left_on, right_on = rel["left_on"], rel["right_on"]
            if frm == rel["left"]:
                frm_on, to_on = left_on, right_on
                frm_col = frm_on["left_col"]
                to_col = to_on["right_col"]
            else:
                frm_on, to_on = right_on, left_on
                frm_col = frm_on["right_col"]
                to_col = to_on["left_col"]
            via_clause = {
                "table": via,
                "on": [(frm, frm_col, via, frm_on["via_col"])],
                "const": [],
                "relationship": name,
            }
            const = rel.get("const")
            if const:
                via_clause["const"].append((via, const["col"], const["value"]))
            clauses.append(via_clause)
            joined.add(via)
            clauses.append({
                "table": to,
                "on": [(via, to_on["via_col"], to, to_col)],
                "const": [],
                "relationship": name,
            })
        else:
            on = rel["join_on"]
            if frm == rel["left"]:
                frm_col, to_col = on["left_col"], on["right_col"]
            else:
                frm_col, to_col = on["right_col"], on["left_col"]
            clauses.append({
                "table": to,
                "on": [(frm, frm_col, to, to_col)],
                "const": [],
                "relationship": name,
            })
        return clauses

    def plan_join(self, base: str, targets: List[str]) -> List[dict]:
        """Return ordered JoinClauses to connect `base` to each of `targets`.

        Assumes `base` is already the FROM table. Raises NoJoinPathError if any
        target is unreachable via declared relationships.
        """
        joined = {base}
        clauses: List[dict] = []
        for target in targets:
            if target in joined:
                continue
            path = self._bfs(base, target)
            if path is None:
                # Telemetry: counts how often a question genuinely needs a cross-entity
                # JOIN, as opposed to a scope predicate that an EXISTS test can satisfy.
                # The distinction decides whether populating `relationships:` is worth
                # it (MULTI_METRIC_ANALYSIS.md §8, phase 5).
                log.info("JOIN_PATH_MISSING base=%s target=%s declared_edges=%d "
                         "declared_tables=%d", base, target, len(self._by_name),
                         len(self._adj))
                raise NoJoinPathError(
                    f"no relationship path from {base} to {target}. "
                    f"Declared tables: {self.tables()}")
            for frm, to, rel in path:
                if to in joined:
                    continue
                for jc in self._edge_clauses(rel, frm, to, joined):
                    clauses.append(jc)
                    joined.add(jc["table"])
        log.debug("plan_join(%s, %s) -> %d clause(s)", base, targets, len(clauses))
        return clauses


@lru_cache(maxsize=1)
def get_graph() -> RelationshipGraph:
    return RelationshipGraph()
