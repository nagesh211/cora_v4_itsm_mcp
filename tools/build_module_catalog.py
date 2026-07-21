"""Regenerate ``cora_mcp/module_catalog.json`` from ``config/*.json``.

The catalog drives module routing (see :mod:`cora_mcp.module_router`): a question
is mapped to a module *before* the OpenSearch search, so the BM25 query can be
restricted to that module's KPIs.

Two layers per module:
  * ``aliases``  — curated module-level vocabulary (full forms, short forms, domain
                   words). **Hand-edited and preserved across regenerations.** This
                   script never overwrites aliases already present in the JSON; it
                   only seeds DEFAULT_ALIASES for a module that isn't there yet.
  * ``metrics``  — name/title/unit/synonyms/tags, always regenerated from the configs
                   so adding or editing a KPI is a one-command refresh.

Usage:
  python tools/build_module_catalog.py            # merge-regenerate in place
  python tools/build_module_catalog.py --print    # write, then dump a summary
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
CONFIG_DIR = os.path.join(_ROOT, "config")
CATALOG_PATH = os.path.join(_ROOT, "cora_mcp", "module_catalog.json")

_COMMENT = ("Module routing catalog. Regenerate with tools/build_module_catalog.py. "
            "aliases = curated module-level vocabulary (full/short forms), preserved "
            "across regenerations; metrics come from config/*.json (nl.synonyms etc.).")

# Human names + seed aliases, used only to bootstrap a module the JSON doesn't have yet.
# Once a module exists in the JSON, its `aliases` there win and are never overwritten.
MODULE_DEFAULTS = {
    "am": {"name": "Availability Management",
           "aliases": ["availability management", "availability", "avail", "am", "uptime",
                       "downtime", "outage", "outages", "sla availability",
                       "service availability", "system uptime"]},
    "cm": {"name": "Change Management",
           "aliases": ["change management", "changes", "change", "chg", "cm", "cab",
                       "change advisory board", "emergency change", "normal change",
                       "standard change", "expedite change", "change failure", "change type"]},
    "em": {"name": "Event Management",
           "aliases": ["event management", "events", "event", "em", "alert", "alerts",
                       "alerting", "monitoring", "critical alert", "affected service"]},
    "im": {"name": "Incident Management",
           "aliases": ["incident management", "incidents", "incident", "inc", "im", "ticket",
                       "tickets", "outage", "major incident", "mim", "mttr",
                       "resolution sla", "response sla", "priority"]},
    "pm": {"name": "Problem Management",
           "aliases": ["problem management", "problems", "problem", "prb", "pm", "rca",
                       "root cause", "root cause analysis", "known error",
                       "major problem", "problem duration"]},
    "rm": {"name": "Release Management",
           "aliases": ["release management", "releases", "release", "rel", "rm",
                       "deployment", "deploy", "rollout", "release success", "release failure"]},
    "sd": {"name": "Service Desk",
           "aliases": ["service desk", "servicedesk", "help desk", "helpdesk", "sd", "call",
                       "calls", "chat", "chats", "contact", "abandonment", "abandoned", "asa",
                       "average speed of answer", "call volume", "first contact resolution", "fcr"]},
    "sr": {"name": "Service Request",
           "aliases": ["service request", "service requests", "request", "requests", "sr",
                       "req", "ritm", "fulfillment", "fulfilment", "request sla",
                       "accuracy of estimate"]},
}


def _load_existing() -> dict:
    if os.path.isfile(CATALOG_PATH):
        with open(CATALOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _metrics_by_module() -> dict:
    by_mod = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json"))):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        module = cfg.get("module")
        if not module:
            continue
        nl = cfg.get("nl") or {}
        gov = cfg.get("governance") or {}
        by_mod[module].append({
            "name": cfg.get("name"),
            "title": cfg.get("title"),
            "unit": cfg.get("unit"),
            "synonyms": nl.get("synonyms", []) or [],
            # drop the module tag (redundant) but keep the descriptive ones
            "tags": [t for t in (gov.get("tags", []) or []) if t != module],
        })
    for metrics in by_mod.values():
        metrics.sort(key=lambda m: m["name"] or "")
    return by_mod


def build() -> dict:
    existing = (_load_existing().get("modules") or {})
    metrics_by_mod = _metrics_by_module()

    modules = {}
    for code in sorted(set(MODULE_DEFAULTS) | set(existing) | set(metrics_by_mod)):
        prev = existing.get(code, {})
        default = MODULE_DEFAULTS.get(code, {})
        modules[code] = {
            "code": code,
            "name": prev.get("name") or default.get("name") or code,
            # preserve hand-edited aliases; only seed defaults for a brand-new module
            "aliases": prev.get("aliases") or default.get("aliases") or [],
            "metrics": metrics_by_mod.get(code, []),
        }
    return {"_comment": _COMMENT, "modules": modules}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--print", action="store_true", help="dump a summary after writing")
    args = ap.parse_args()

    data = build()
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("wrote %s (%d modules)" % (CATALOG_PATH, len(data["modules"])))
    if args.print:
        for code, m in data["modules"].items():
            print("  %s: %d metrics, %d aliases" % (code, len(m["metrics"]), len(m["aliases"])))


if __name__ == "__main__":
    main()