#!/usr/bin/env python3
"""harvest_relationships.py — declare table relationships in schema_v3.yaml.

The schema lists tables/columns but not how tables join. The existing KPI configs
already encode every join path in their ``from_raw`` / ``base_query``; this script
(a) can print those discovered joins for provenance (``--verify``), and (b) writes
a curated ``relationships:`` block onto the relevant module in ``schema_v3.yaml``.

A relationship is a semantic edge between two *entity* tables, optionally through a
``via`` relation table, with an optional constant discriminator (e.g. the
incident↔change link is only meaningful where ``type = 'Caused By Change'``):

    - name: incident_caused_by_change
      left: itsm_incident.tbl_all_incidents
      right: itsm_change.tbl_change
      via: itsm.tbl_incident_change_relation
      left_on:  {left_col: incident_system_id, via_col: incident_system_id}
      right_on: {right_col: change_system_id,   via_col: change_system_id}
      const:    {col: type, value: Caused By Change}

    - name: release_causes_change            # direct edge (no via)
      left: itsm_release.tbl_pepops_release_mgmt
      right: itsm_change.tbl_change
      join_on: {left_col: change_id, right_col: change_id}   # 'join_on' not 'on' (YAML bool trap)

Usage:
    python tools/harvest_relationships.py            # write relationships into schema
    python tools/harvest_relationships.py --verify    # print joins discovered in configs
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
SCHEMA_PATH = os.path.join(_ROOT, "schema_v3.yaml")
CONFIG_DIR = os.path.join(_ROOT, "config")

# ---------------------------------------------------------------------------
# Curated relationships, seeded from the joins the configs already use.
# Keyed by module name; each entry becomes one item under module.relationships.
# ---------------------------------------------------------------------------
CURATED = {
    "itsm": [
        {
            "name": "incident_caused_by_change",
            "description": "Incidents linked to the change that caused them.",
            "left": "itsm_incident.tbl_all_incidents",
            "right": "itsm_change.tbl_change",
            "via": "itsm.tbl_incident_change_relation",
            "left_on": {"left_col": "incident_system_id", "via_col": "incident_system_id"},
            "right_on": {"right_col": "change_system_id", "via_col": "change_system_id"},
            "const": {"col": "type", "value": "Caused By Change"},
        },
        {
            "name": "incident_has_problem",
            "description": "Incidents linked to a related problem record.",
            "left": "itsm_incident.tbl_all_incidents",
            "right": "itsm_problem.tbl_problem",
            "via": "itsm.tbl_incident_problem_relation",
            "left_on": {"left_col": "incident_system_id", "via_col": "incident_system_id"},
            "right_on": {"right_col": "problem_system_id", "via_col": "problem_system_id"},
        },
        {
            "name": "release_causes_change",
            "description": "Releases joined to their changes by change_id.",
            "left": "itsm_release.tbl_pepops_release_mgmt",
            "right": "itsm_change.tbl_change",
            "join_on": {"left_col": "change_id", "right_col": "change_id"},
        },
        {
            "name": "incident_sla_resolution",
            "description": "Incidents joined to their SLA resolution rows.",
            "left": "itsm_incident.tbl_all_incidents",
            "right": "itsm_incident.tbl_sla_resolution",
            "join_on": {"left_col": "incident_system_id", "right_col": "incident_system_id"},
        },
    ],
}


def verify() -> None:
    """Print the join paths encoded in the configs (provenance for CURATED)."""
    print("=== joins discovered in config from_raw / base_query ===")
    seen = set()
    for f in sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json"))):
        c = json.load(open(f, encoding="utf-8"))
        d = c.get("dsl") or {}
        text = d.get("from_raw") or ((c.get("sql") or {}) or {}).get("base_query", "")
        if " join " not in text.lower():
            continue
        tables = tuple(sorted(set(re.findall(r"(itsm[a-z_]*\.[a-z_0-9]+)", text, re.I))))
        if tables in seen:
            continue
        seen.add(tables)
        conds = re.findall(r"on\s+([a-z0-9_.]+)\s*=\s*([a-z0-9_.]+)", text, re.I)
        types = re.findall(r"\.type\s*=\s*'([^']+)'", text)
        print(f"* {c['name']}: {list(tables)}")
        for a, b in conds:
            print(f"    {a} = {b}")
        if types:
            print(f"    type: {types}")


def _to_commented(obj):
    """Recursively convert plain dict/list into ruamel CommentedMap/Seq for
    clean block-style output."""
    if isinstance(obj, dict):
        m = CommentedMap()
        for k, v in obj.items():
            m[k] = _to_commented(v)
        return m
    if isinstance(obj, list):
        s = CommentedSeq()
        for v in obj:
            s.append(_to_commented(v))
        return s
    return obj


def inject() -> int:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=2, offset=0)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        doc = yaml.load(fh)

    changed = 0
    for mod in doc.get("modules") or []:
        rels = CURATED.get(mod.get("name"))
        if not rels:
            continue
        block = _to_commented(rels)
        # Insert 'relationships' right before 'entities' for readability.
        if "relationships" in mod:
            mod["relationships"] = block
        else:
            keys = list(mod.keys())
            pos = keys.index("entities") if "entities" in keys else len(keys)
            mod.insert(pos, "relationships", block)
        changed += 1
        print(f"module {mod['name']}: wrote {len(rels)} relationship(s)")

    if not changed:
        print("no modules matched CURATED; nothing written")
        return 1
    with open(SCHEMA_PATH, "w", encoding="utf-8", newline="\n") as fh:
        yaml.dump(doc, fh)
    print(f"updated {SCHEMA_PATH}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true",
                    help="print joins discovered in configs; do not write")
    args = ap.parse_args()
    if args.verify:
        verify()
        return 0
    return inject()


if __name__ == "__main__":
    sys.exit(main())
