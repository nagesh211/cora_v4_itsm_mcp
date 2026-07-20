"""Loads and searches the KPI configs in ``config/*.json``.

Each config is a KPI definition consumed by ``gen_query.py``. This module
indexes them by name and module and provides a lightweight token-overlap search
over each KPI's name / title / natural-language synonyms / sample questions /
tags, so a natural-language question can be routed to a KPI.
"""
from __future__ import annotations

import glob
import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
DEFAULT_CONFIG_DIR = os.path.join(_ROOT, "config")

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "for", "in", "on", "by", "to", "and", "or",
         "is", "are", "what", "how", "our", "me", "show", "give", "get",
         "this", "that", "with", "per", "at", "which"}


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP]


def _filter_alias_map(allowed: List[str]) -> Dict[str, List[str]]:
    """{allowed filter key -> accepted aliases}, so the summary advertises the
    exact vocabulary the resolver understands. Empty list = only the key name."""
    from cora_mcp.filter_aliases import get_registry
    reg = get_registry()
    return {k: reg.aliases_of(k) for k in (allowed or [])}


class KpiCatalog:
    def __init__(self, config_dir: Optional[str] = None):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        log.info("loading KPI configs from: %s", self.config_dir)
        self._by_name: Dict[str, dict] = {}
        self._corpus: Dict[str, List[str]] = {}   # name -> token list
        self._by_table: Dict[str, List[str]] = {}  # "schema.table" -> [kpi names]
        self._load()
        log.info("KPI catalog loaded: %d configs", len(self._by_name))

    def _load(self) -> None:
        for path in sorted(glob.glob(os.path.join(self.config_dir, "*.json"))):
            try:
                cfg = json.load(open(path, encoding="utf-8"))
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("skipping unreadable config %s: %s", path, exc)
                continue
            name = cfg.get("name")
            if not name:
                continue
            self._by_name[name] = cfg

            nl = cfg.get("nl") or {}
            gov = cfg.get("governance") or {}
            parts = [name, cfg.get("title", ""), cfg.get("module", "")]
            parts += nl.get("synonyms", []) or []
            parts += nl.get("sample_questions", []) or []
            parts += gov.get("tags", []) or []
            self._corpus[name] = _tokens(" ".join(parts))

            pd = cfg.get("primary_dataset") or {}
            fqn = f"{pd.get('schema')}.{pd.get('table')}"
            self._by_table.setdefault(fqn, []).append(name)

    # ---- access ----------------------------------------------------------
    def names(self) -> List[str]:
        return list(self._by_name.keys())

    def get(self, name: str) -> Optional[dict]:
        return self._by_name.get(name)

    def exists(self, name: str) -> bool:
        return name in self._by_name

    def summary(self, name: str) -> Optional[dict]:
        cfg = self.get(name)
        if not cfg:
            return None
        nl = cfg.get("nl") or {}
        allowed = (cfg.get("filters") or {}).get("allowed", [])
        return {
            "name": name,
            "module": cfg.get("module"),
            "title": cfg.get("title"),
            "unit": cfg.get("unit"),
            "execution_mode": cfg.get("execution_mode"),
            "allowed_filters": allowed,
            # Accepted user-facing aliases per filter, so a question can say
            # "business"/"p&l" (-> sector) or "team" (-> assignment_group) and be
            # resolved deterministically. Pass any of these as a `filters` key.
            "filter_aliases": _filter_alias_map(allowed),
            "drilldown_dimensions": (cfg.get("drilldown") or {}).get("dimensions", []),
            "sample_questions": nl.get("sample_questions", []),
        }

    def by_module(self, module: str) -> List[str]:
        return [n for n, c in self._by_name.items() if c.get("module") == module]

    def configs_for_tables(self, fqns: List[str]) -> List[str]:
        """KPI names whose primary dataset is one of the given schema.table names."""
        out: List[str] = []
        for fqn in fqns:
            out.extend(self._by_table.get(fqn, []))
        return sorted(set(out))

    # ---- search ----------------------------------------------------------
    def search(self, query: str, module: Optional[str] = None, limit: int = 8) -> List[dict]:
        q = _tokens(query)
        if not q:
            return []
        qset = set(q)
        ql = (query or "").lower()
        scored = []
        for name, corpus in self._corpus.items():
            cfg = self._by_name[name]
            if module and cfg.get("module") != module:
                continue
            cset = set(corpus)
            overlap = len(qset & cset)
            if overlap == 0 and name.replace("-", " ") not in ql:
                continue
            score = overlap
            # Boosts for stronger signals.
            if name.replace("-", " ") in ql or name in ql:
                score += 5
            title_tokens = set(_tokens(cfg.get("title", "")))
            score += len(qset & title_tokens)  # title match counts double
            scored.append((score, name))
        scored.sort(key=lambda s: (-s[0], s[1]))
        results = [self.summary(n) | {"score": sc} for sc, n in scored[:limit]]
        log.debug("search %r (module=%s) -> %s", query, module, [r["name"] for r in results])
        return results


@lru_cache(maxsize=1)
def get_catalog() -> KpiCatalog:
    return KpiCatalog()
