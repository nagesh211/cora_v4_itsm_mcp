#!/usr/bin/env python3
"""One-off: add a ``{filters}`` placeholder to the SQL-mode KPI configs that
advertise user filters, so ``gen_query.driver_substitute`` can inject them.

Each entry names an anchor substring already in the authored ``base_query``; the
placeholder is appended immediately after every occurrence of that anchor (some
queries filter the same base table in two subqueries, so the filter must land in
both). A couple of multi-table configs also need their filter columns alias-
qualified to avoid ambiguity — handled via ``field_fixes``.

Idempotent: re-running is a no-op (skips files that already contain ``{filters}``
at the anchor). Verified against live column introspection before authoring.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.normpath(os.path.join(HERE, "..", "config"))

# kpi -> (anchor, expected_occurrences)
SLOTS = {
    "availability-percentage": (
        "and a.support_group_system_id in (  select group_system_id from "
        "itsm.tbl_group_hierarchy  where service_area != 'NON-IT')", 2),
    "avg-sla-resolution": ("a.service_area!='NON-IT'", 2),
    "cm-major-incident": ("and a.open_date_time::varchar != ''", 1),
    "im-sla-risk": ("and a.taskslatable_business_percentage >= '75'", 1),
    "pm-major-problem-average-rca-task-duration": (
        "and a.major_problem_indicator = 'TRUE'", 1),
    "pm-open-problems": (
        "(a.closed_date_time is null or a.closed_date_time::varchar='' or "
        "a.closed_date_time > '{as_of}')", 1),
    "sd-fcr-percentage": ("and a.status_name='CLOSED'", 2),
    "sd-nps-percentage": ("and answered <> ''", 1),
    "sd-ssp-volume": ("and a.status_name not in ('CANCELED')", 2),
    "sr-incomplete": ("and closed_date_time::character varying != ''", 1),
    "sr-open-count": (
        "(a.closed_date_time is null or a.closed_date_time::varchar='' or "
        "a.closed_date_time > '{as_of}')", 1),
}

# kpi -> {logical_filter: qualified_column}  (disambiguate multi-table joins)
FIELD_FIXES = {
    # service_area exists on BOTH tbl_change (c) and tbl_all_incidents (a);
    # the KPI is about change attributes, so qualify with the change alias.
    "cm-major-incident": {"service_area": "c.service_area"},
    # 4-table join; every filter column lives on tbl_problem (alias a).
    "pm-major-problem-average-rca-task-duration": {
        "business_name": "a.business_name",
        "sub_business_name": "a.sub_business_name",
        "region_name": "a.region_name",
        "assignment_group_name": "a.assignment_group_name",
        "category_description": "a.category_description",
        "service_area": "a.service_area",
        "source_description": "a.source_description",
    },
}


def main():
    for kpi, (anchor, expected) in SLOTS.items():
        path = os.path.join(CONFIG_DIR, kpi + ".json")
        cfg = json.load(open(path, encoding="utf-8"))
        bq = cfg["sql"]["base_query"]

        if "{filters}" in bq:
            print(f"skip  {kpi}: already has {{filters}}")
        else:
            count = bq.count(anchor)
            if count != expected:
                raise SystemExit(
                    f"ABORT {kpi}: anchor found {count}x, expected {expected}\n"
                    f"  anchor={anchor!r}")
            cfg["sql"]["base_query"] = bq.replace(anchor, anchor + "{filters}")
            print(f"ok    {kpi}: injected {{filters}} after {count} anchor(s)")

        # alias-qualify columns where the authored query would be ambiguous
        for logical, col in FIELD_FIXES.get(kpi, {}).items():
            # find the field entry whose column matches the bare/old value
            for fname, meta in (cfg.get("fields") or {}).items():
                if fname == logical or meta.get("column") in (logical, col):
                    if meta.get("column") != col:
                        print(f"      {kpi}: field {fname!r} column "
                              f"{meta.get('column')!r} -> {col!r}")
                        meta["column"] = col
        json.dump(cfg, open(path, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
        open(path, "a", encoding="utf-8").write("\n")


if __name__ == "__main__":
    main()