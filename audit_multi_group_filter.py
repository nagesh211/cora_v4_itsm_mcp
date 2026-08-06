#!/usr/bin/env python3
"""audit_multi_group_filter.py — for every KPI config in a directory (default:
itsm-updated), actually EXERCISE gen_query.build_sql the same way the runtime
does, with:
  - a single group-by dimension
  - two group-by dimensions at once ("multiple groups")
  - a single filter
  - two filters at once ("multiple filters")
and record which configs raise on each case. This is not static heuristics —
it runs the exact same driver_substitute()/dsl_build() code path
cora_mcp.query_engine calls, so a PASS here means the SQL is at least
generatable (a FAIL is a guaranteed runtime error; a PASS does not guarantee
the query is correct, only that it builds).

Usage:
    python audit_multi_group_filter.py [config_dir]   # default: itsm-updated
    python audit_multi_group_filter.py --json out.json
"""
import argparse
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import gen_query as gq  # noqa: E402

sys.path.insert(0, os.path.join(_HERE, "cora_mcp"))
from cora_mcp.opensearch_client import config_dimensions  # noqa: E402

WINDOW = {"from_date": "2026-01-01 00:00:00", "to_date": "2026-06-29 23:59:59",
          "as_of": "2026-06-29 23:59:59"}

_SKIP_LIKE = ("date", "time", "_at", "created", "closed", "opened", "resolved",
              "start", "end")


def _pick_dims(cfg):
    """Return (all_declared_dims, best_single, best_pair) using the SAME
    dimension surface the UI/breakdown code would offer: config_dimensions()
    (drilldown.dimensions / allowed_group_by), falling back to schema columns
    on the primary table for SQL-mode configs with none declared (mirrors
    gen_query.driver_substitute's own schema-fallback)."""
    dims = config_dimensions(cfg)
    if dims:
        return dims, (dims[0] if dims else None), (dims[:2] if len(dims) >= 2 else None)
    if (cfg.get("execution_mode") or "").upper() != "SQL":
        return [], None, None
    cols = gq._sql_primary_table_columns(cfg)
    cand = [c for c in cols if not any(k in c.lower() for k in _SKIP_LIKE)]
    cand = cand or list(cols)
    return cand, (cand[0] if cand else None), (cand[:2] if len(cand) >= 2 else None)


def _pick_filters(cfg):
    allowed = list((cfg.get("filters") or {}).get("allowed") or [])
    fields = cfg.get("fields") or {}
    if not allowed:
        allowed = [k for k in fields.keys()]
    # keep only keys actually declared in fields (compile_sql_filters requires it)
    allowed = [k for k in allowed if k in fields]
    return allowed


def _try(cfg, payload):
    try:
        gq.build_sql(cfg, payload)
        return True, None
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def audit_one(path, cfg):
    name = cfg.get("name") or os.path.basename(path)
    mode = (cfg.get("execution_mode") or "").upper()
    result = {"config": name, "path": path, "execution_mode": mode}

    dims, single_dim, pair_dims = _pick_dims(cfg)
    result["declared_dims"] = dims

    base_payload = dict(WINDOW)

    if single_dim:
        ok, err = _try(cfg, {**base_payload, "group_by_dim": single_dim})
        result["single_group_by"] = {"dim": single_dim, "ok": ok, "error": err}
    else:
        result["single_group_by"] = {"dim": None, "ok": False,
                                      "error": "no groupable dimension declared/derivable"}

    if pair_dims:
        ok, err = _try(cfg, {**base_payload, "group_by_dim": pair_dims})
        result["multi_group_by"] = {"dims": pair_dims, "ok": ok, "error": err}
    else:
        result["multi_group_by"] = {
            "dims": dims[:2] if dims else None, "ok": False,
            "error": ("fewer than 2 groupable dimensions declared "
                      "(only %d: %s)" % (len(dims), dims))}

    allowed_filters = _pick_filters(cfg)
    fields = cfg.get("fields") or {}
    f1 = allowed_filters[0] if allowed_filters else None
    f2s = allowed_filters[:2] if len(allowed_filters) >= 2 else None

    def _dummy(key):
        ft = (fields.get(key) or {}).get("filter_type") or "in"
        return ["__probe__"] if ft != "array_val" else ["__probe__"]

    if f1:
        ok, err = _try(cfg, {**base_payload, "filter_by": {f1: _dummy(f1)}})
        result["single_filter"] = {"field": f1, "ok": ok, "error": err}
    else:
        result["single_filter"] = {"field": None, "ok": False,
                                    "error": "no filterable field declared"}

    if f2s:
        fb = {k: _dummy(k) for k in f2s}
        ok, err = _try(cfg, {**base_payload, "filter_by": fb})
        result["multi_filter"] = {"fields": f2s, "ok": ok, "error": err}
    else:
        result["multi_filter"] = {
            "fields": allowed_filters[:2] if allowed_filters else None, "ok": False,
            "error": ("fewer than 2 filterable fields declared (only %d: %s)"
                       % (len(allowed_filters), allowed_filters))}

    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", default="itsm-updated")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    d_abs = os.path.join(_HERE, args.dir) if not os.path.isabs(args.dir) else args.dir
    files = sorted(glob.glob(os.path.join(d_abs, "*.json")))

    results = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception as exc:
            print("  skip %s: %s" % (f, exc))
            continue
        results.append(audit_one(os.path.relpath(f, _HERE), cfg))

    fail_multi_group = [r for r in results if not r["multi_group_by"]["ok"]]
    fail_single_group = [r for r in results if not r["single_group_by"]["ok"]]
    fail_multi_filter = [r for r in results if not r["multi_filter"]["ok"]]
    fail_single_filter = [r for r in results if not r["single_filter"]["ok"]]

    print("=" * 90)
    print("MULTI GROUP-BY / MULTI FILTER AUDIT — %s (%d configs)" % (args.dir, len(results)))
    print("=" * 90)
    print()
    print("Single group-by:  %d/%d FAIL" % (len(fail_single_group), len(results)))
    print("Multi  group-by:  %d/%d FAIL" % (len(fail_multi_group), len(results)))
    print("Single filter:    %d/%d FAIL" % (len(fail_single_filter), len(results)))
    print("Multi  filter:    %d/%d FAIL" % (len(fail_multi_filter), len(results)))
    print()

    print("-" * 90)
    print("FAIL ON MULTIPLE GROUP-BY (asking to group by 2+ dimensions at once)")
    print("-" * 90)
    for r in fail_multi_group:
        tag = "also fails single" if not r["single_group_by"]["ok"] else "single-dim OK, multi-dim FAILS"
        print("  - %-55s [%s]" % (r["config"], tag))
        print("      %s" % r["multi_group_by"]["error"])

    print()
    print("-" * 90)
    print("FAIL ON MULTIPLE FILTERS (applying 2+ filters at once)")
    print("-" * 90)
    for r in fail_multi_filter:
        tag = "also fails single" if not r["single_filter"]["ok"] else "single-filter OK, multi-filter FAILS"
        print("  - %-55s [%s]" % (r["config"], tag))
        print("      %s" % r["multi_filter"]["error"])

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print("\nwrote JSON report -> %s" % args.json_out)


if __name__ == "__main__":
    main()