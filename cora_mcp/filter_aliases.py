"""User-facing filter-alias resolution.

A user asks for "business ADMINISTRATION" or "the team X"; the KPI configs filter
by a canonical key (``sector``, ``assignment_group``, …). This module loads the
alias registry (``filter_aliases.json``) and resolves any alias to its canonical
key deterministically:

  * matching is case-insensitive and folds runs of space / underscore / hyphen,
    so ``"Service Area"`` == ``service_area`` == ``service-area``;
  * matching is on the WHOLE normalized string (never a substring), so
    ``sub_business`` never collides with ``business``;
  * the canonical key itself is always accepted (back-compat);
  * an alias that maps to no key, or to a key the specific KPI does not expose,
    is REJECTED with the list of what that KPI supports — never a silent guess.

No alias may map to two different keys; that is validated when the registry
loads (raises :class:`AliasConfigError`).
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
DEFAULT_ALIASES_PATH = os.path.join(_ROOT, "filter_aliases.json")

_SEP = re.compile(r"[\s_\-]+")


def normalize(term: str) -> str:
    """Lowercase and collapse space/underscore/hyphen runs to a single space."""
    return _SEP.sub(" ", (term or "").strip().lower())


class AliasConfigError(ValueError):
    """The alias registry is malformed (e.g. an alias maps to two keys)."""


class AliasRegistry:
    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_ALIASES_PATH
        self._index: Dict[str, str] = {}          # normalized alias/key -> canonical key
        self._aliases_of: Dict[str, List[str]] = {}   # canonical key -> display aliases
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            log.warning("no filter_aliases.json at %s; alias resolution disabled", self.path)
            return
        doc = json.load(open(self.path, encoding="utf-8")) or {}
        entries = doc.get("aliases") or {}
        for key, meta in entries.items():
            display = list((meta or {}).get("aliases") or [])
            self._aliases_of[key] = display
            for term in [key, *display]:
                n = normalize(term)
                prev = self._index.get(n)
                if prev is not None and prev != key:
                    raise AliasConfigError(
                        f"alias {term!r} maps to both {prev!r} and {key!r}")
                self._index[n] = key
        log.info("filter aliases loaded: %d keys, %d alias terms",
                 len(self._aliases_of), len(self._index))

    def canonical_for(self, term: str) -> Optional[str]:
        """Canonical key for a term (alias or the key itself), or None."""
        return self._index.get(normalize(term))

    def aliases_of(self, key: str) -> List[str]:
        return self._aliases_of.get(key, [])


@lru_cache(maxsize=1)
def get_registry() -> AliasRegistry:
    return AliasRegistry()
