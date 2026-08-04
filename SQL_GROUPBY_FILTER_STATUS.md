# SQL-mode dimension breakdown & filter status (`itsm/`, 196 configs)

Snapshot of what's fixed and what's left, after adding generic GROUP BY support
to SQL-mode KPIs (previously only DSL-mode KPIs could be broken down by a
dimension at all).

## Scoreboard

| | Before this work | Now |
|---|---|---|
| GROUP BY works (of 196 configs) | 138 | **164** (58 blocked -> 31 blocked) |
| GROUP BY blocked | 58 | **31** |
| Originally-blocked KPIs (14, `type: kpi`) fixed | 0 | **13 / 14** |
| Filters work (single or multi-filter) | 195 / 196 | 195 / 196 (unchanged, already solid) |

## What was fixed

| Fix | Where | Mechanism |
|---|---|---|
| `module_overview`/health only rolls up `type: "kpi"` | `cora_mcp/query_engine.py` | Widgets (dashboard chart configs) are excluded from the automatic per-module rollup; still queryable directly via `run_kpi`. |
| Schema-fallback groupability | `gen_query.py` | If a KPI declares no `allowed_group_by`, a dimension is still allowed when its column is a real column on the primary table (`schema_v3.yaml`). |
| Structural safety guard | `gen_query.py` | Refuses (clean error) to inject a dimension when the primary table is nested inside a derived subquery at the outer scope — prevents shipping SQL that would error with "missing FROM-clause entry". |
| **Class-1 pushdown** (sqlglot AST) | `gen_query.py`: `_sql_dimension_chain`, `_apply_dimension_chain` | Pushes the dimension down through a single chain of wrapping CTEs/subqueries to the real table, adding `GROUP BY` at every aggregating level along the way (there can be more than one, e.g. a per-record CTE re-aggregated by an outer `SUM`). +22 configs fixed. |
| **Class-2a pushdown** (sqlglot AST) | `gen_query.py`: `_sql_two_branch_ratio`, `_sql_pushdown_ratio_group_by` | For a ratio of two bare, ungrouped totals combined with no existing join key (e.g. `NUMERATOR N CROSS JOIN DENOMINATOR D`): groups each branch independently, converts the join to `FULL OUTER JOIN ON grp` + `COALESCE(grp)` so a dimension value present on only one side isn't silently dropped. +4 configs fixed. |
| `module_overview` breakdown routing fixed | `cora_mcp/query_engine.py`: `_attach_breakdown` | Was unconditionally routing every SQL-mode KPI through a dead `drilldown.breakdown.query` mechanism (config key never populated by any shipped config) instead of the generic GROUP BY path above. Now tries the generic path first for both DSL and SQL. |
| `tbl_automation_incident` added to schema catalog | `schema_v3.yaml` | Was missing entirely (88 real live-DB columns); added full entry matching sibling `automation.*` tables' conventions (`business_name` -> `canonical: sector`). Unblocks dimension resolution for `automation-incident-automation-percentage`. |

Every fix was validated against the **live Postgres DB**, not just parse-checked. Full test suite (53 pre-existing failures, unrelated to this work — confirmed identical before/after every change) showed **zero regressions** at each step.

## Still blocked — GROUP BY (31 configs)

Only **one** real KPI remains blocked; the rest are dashboard widgets.

| Module | KPIs (`type: kpi`) | Widgets (`type: widget`) |
|---|---|---|
| automation | — | `automation-bundle-wise-automation-percentage`, `automation-change-automation-percentage-widget`, `automation-end-to-end-automation-percentage`, `automation-incident-automation-percentage-widget`, `automation-overall-automation`, `automation-sc-task-automation-percentage` |
| availability | **`availability-avg-sla-resolution`** | `availability-availability-trend-line-v2`, `availability-availability-trend`, `availability-avg-sla-resolution-trend` |
| catalog | — | `catalog-automation-percentage`, `catalog-catalog-by-hierarchy-one-automation-metrics`, `catalog-general-vs-standard-request-volume`, `catalog-itil-vs-non-itil`, `catalog-nps-catalog` |
| changes | — | `changes-failure-trend-line-v2`, `changes-failure-trend`, `changes-sector-trend`, `changes-user-reported-change-opened-vs-closed` |
| incidents | — | `incidents-incident-summary`, `incidents-incidents-by-sector`, `incidents-incidents-sla-summary`, `incidents-user-reported-incident-created-vs-closed` |
| incops | — | `incops-incident-summary`, `incops-incidents-by-sector`, `incops-incidents-sla-summary` |
| problems | — | `problems-major-problem-trend`, `problems-non-major-problem-trend`, `problems-problem-trend` |
| servicereq | — | `servicereq-general-aging-graph`, `servicereq-top-fifteen-users-by-assigned-to` |

### Why these remain blocked

1. **Trend/summary widgets already joined on a key** — a time bucket, or already grouped by one hardcoded business dimension. E.g. `incidents-incidents-by-sector` already internally groups by `business_name` and joins two counts `ON sector_name = sector_name`. Regrouping by a *different* dimension means reworking which column each branch selects, not just adding a join key — the class-2a rewrite deliberately excludes anything already keyed/grouped, since guessing there risks a silently wrong number.
2. A few (`catalog-*`, `problems-*-trend`) are ratio-of-branches shapes similar to what class-2a fixed, but already combined via an `ON`/`USING` predicate rather than a bare cross-join — same reasoning, out of the safe automated scope.
3. **`availability-avg-sla-resolution`** — its ratio is built from *two different real tables* (`tbl_major_incidents` vs `tbl_sla_resolution`); correctly refuses rather than assume their `business_name` columns mean the same "sector" without a human decision.

None of these are candidates for more generic engineering the way class-1/class-2a were — each needs either a DSL rewrite or per-KPI SQL surgery with a human reviewing the join semantics.

## Still blocked — filters (1 config)

`servicereq-top-fifteen-users-by-assigned-to` (widget) — a fixed top-15 ranking query with no `{filters}` placeholder authored at all.

## Known execution bugs (unrelated to GROUP BY/filters, found along the way)

- `automation-incident-automation-percentage` and `automation-overall-automation-percentage` both reference `a.assigned_to` in their WHERE clause — a column that doesn't exist on their live tables (`tbl_automation_incident`, `tbl_automation_end_to_end`). Confirmed pre-existing: fails identically on the *original, unmodified* query, so unrelated to any of the work above.
  - Neither live table has a column for an individual assignee/resolver — only `assignment_group`/`assignment_group_system_id` (a team name), which doesn't match the literal values being checked (`'Ansible'`, `'ITOPSBOT.INTEGRATION'` — specific bot/service-account names).
  - **Left untouched per decision on 2026-08-05** — needs the real column name from whoever owns the ITSM data model before it's safe to fix (guessing wrong would silently change which tickets are excluded from the automation-percentage calculation).