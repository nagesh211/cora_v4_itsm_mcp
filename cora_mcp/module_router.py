"""Map a natural-language question to an ITSM module code *before* the search.

``search_kpis`` runs a BM25 query over all 66 KPI configs. Without a module filter,
"percentage of changes by type" competes against availability, incident, etc. metrics
and unrelated KPIs leak into the top results. This module infers the most likely
module from the question so the search can be restricted to it (``module`` filter on
the OpenSearch query), sharpening relevance.

Vocabulary comes from ``cora_mcp/module_catalog.json`` (see
``tools/build_module_catalog.py``): curated ``aliases`` per module plus each metric's
``synonyms``. Matching is:

  * **normalized** — case/underscore/hyphen folded via :func:`filter_aliases.normalize`;
  * **word-boundary** — "cm" matches the standalone token, not inside "outcome";
  * **longer-phrase-wins** — a phrase scores its word count, so "change management"
    (2) outweighs a stray "change" (1);
  * **guarded** — a module is returned only when it *strictly* beats the runner-up;
    ties or no matches yield ``None`` (caller then searches unfiltered).

:func:`detect_module` returns ``None`` whenever it is not confident, so routing can
only help — a miss degrades to today's unfiltered behaviour.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from cora_mcp.filter_aliases import normalize
from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(_HERE, "module_catalog.json")


@lru_cache(maxsize=1)
def _phrases_by_module() -> Dict[str, List[str]]:
    """{module code -> sorted unique normalized phrases}. Aliases + metric synonyms +
    metric names (hyphens folded). Built once; cached for the process."""
    try:
        with open(CATALOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("module_router: cannot load %s (%s); routing disabled", CATALOG_PATH, exc)
        return {}

    out: Dict[str, set] = {}
    for code, mod in (data.get("modules") or {}).items():
        phrases: set = set()
        for alias in mod.get("aliases", []) or []:
            if alias:
                phrases.add(normalize(alias))
        for metric in mod.get("metrics", []) or []:
            for syn in metric.get("synonyms", []) or []:
                if syn:
                    phrases.add(normalize(syn))
            name = metric.get("name")
            if name:
                phrases.add(normalize(name))  # "sd-abn-percentage" -> "sd abn percentage"
        phrases.discard("")
        out[code] = phrases
    return {code: sorted(p) for code, p in out.items()}


def _score_modules(question: str) -> Dict[str, int]:
    """Per-module match score: sum of word-counts of every catalog phrase that appears
    as a whole-word run in the question. Empty dict when nothing matches."""
    q = " %s " % normalize(question)   # pad so word-boundary checks work at the edges
    if q.strip() == "":
        return {}
    scores: Dict[str, int] = {}
    for code, phrases in _phrases_by_module().items():
        total = 0
        for phrase in phrases:
            if (" %s " % phrase) in q:
                total += len(phrase.split())  # longer phrase = stronger signal
        if total:
            scores[code] = total
    return scores


def detect_module(question: str) -> Optional[str]:
    """Best-guess module code for a question, or ``None`` if not confident.

    ``None`` is returned when nothing matches or the top two modules tie, so the
    caller should search unfiltered in that case."""
    scores = _score_modules(question)
    if not scores:
        return None
    ranked: List[Tuple[str, int]] = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
        return ranked[0][0]
    return None  # ambiguous (cross-module question) -> don't filter


def reset_cache() -> None:
    """Drop the cached phrase index (tests / after editing module_catalog.json)."""
    _phrases_by_module.cache_clear()