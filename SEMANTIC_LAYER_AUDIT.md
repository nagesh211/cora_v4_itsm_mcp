# Semantic-layer audit: routing defects, registry streamlining, schema reconciliation

Every number below is from a log line in `logs/`, a `SELECT` against `vtx5`, or a test in
`tests/`. Nothing here is inferred. Where a claim could not be verified it says so.

Work done 2026-08-03 → 2026-08-04. Branch `feature/pep-poc-copy`.

---

## 0. Summary

Three user-reported answers were wrong. Each had a different root cause, but all three
shared one shape: **a fact was hand-transcribed where it could have been derived or
measured, and nothing reported the mistake** — so the system answered confidently from
the wrong population instead of failing.

| # | Symptom | Root cause | Status |
|---|---|---|---|
| 1 | "Incident Ids which impacted availability percentage" → major incidents | no availability predicate; availability entity unreachable by the planner | fixed, verified live |
| 2 | "changes closed by sector and type" → one ungrouped total | `sql_builder` never consulted `column_resolver` | fixed |
| 3 | semantic layer covers "only SLAs and some" | `predicates.json` covered 11 of 31 tables, with nothing reporting the gap | tooling built; curation is a standing task |

Underneath all three: `schema_v3.yaml` disagrees with the database in **110 error-level
ways**. That is now measurable on demand and gated.

Test totals: **406 passed, 29 failed**. The 29 are pre-existing (missing
`pytest-asyncio` config and live-service tests); the failure list is byte-identical to the
pre-change baseline, verified by stash-and-diff. Baseline was 326 passed, so 80 tests were
added.

---

## 1. Defect 1 — availability question answered from the incident module

### What happened

`logs/cora-2026-08-03.log:14498-14530`, question *"Give me Incident Ids which impacted
availability percentage during last month"*:

```
list_predicates(entity='incident')                    -> 6 incident predicates
compose_metric(predicates=['major_incident'], select=['incident_id'], period='last month')
compose entity=incident -> anchor=itsm_incident.tbl_major_incidents
SELECT a.incident_id FROM itsm_incident.tbl_major_incidents a WHERE ...   -> 119 rows
```

The availability percentage is computed from a different population entirely. The KPI's own
SQL (`logs/cora-2026-08-03.log:15188`) scopes to:

```sql
FROM itsm_availability.tbl_tableau_outagesv4
WHERE outage_type = 'OUTAGE'
  AND business_criticality_value = '1 - most critical'
  AND hypercare_project_system_id IS NULL
  AND support_group_system_id IN (SELECT group_system_id FROM itsm.tbl_group_hierarchy
                                  WHERE service_area != 'NON-IT')
```

### Three independent causes

1. **`predicates.json` had no availability predicate at all.** The only qualifier the agent
   could find for "incidents" was `major_incident`, so it substituted it.
2. **`plan_query` could not reach the availability entity either.** `record_prefixes.json`
   declared no alias for it, so `adhoc._identify_entities` matched only `'incident'`.
   Measured before the fix:
   ```
   'Show me the Incident IDs of incidents that impacted the availability percentage…'
     -> [{'slug': 'itsm_incident', 'score': 12, 'why': ["matched 'incidents'", "matched 'incident'"]}]
   'availability percentage last month'  -> []
   'outages last month'                 -> []
   ```
3. **The prompt told it to narrow.** Routing step 2b sends "list the qualified records" to
   `compose_metric`, and because the question said "Incident Ids" the agent called
   `list_predicates(entity='incident')` — which hides qualifiers belonging to another
   entity.

### Measured facts about the outage table

| query | result |
|---|---|
| `outage_type` domain | `NULL` (304,696) and `'OUTAGE'` (16,118) — nothing else |
| OUTAGE rows carrying `incident_id` | 16,118 of 16,118 (16,117 distinct) |
| OUTAGE + `'1 - most critical'` | 554 rows / 554 distinct incidents |
| same, July 2026 | 58 |
| OUTAGE alone, July 2026 | 1,248 |

### Fix

* **`predicates.json`** — two predicates, both DB-verified:
  * `outage` — `outage_type = 'OUTAGE'`. Generic; without this condition a count of
    "outages" returns the table's service-day rows.
  * `availability_impacting` — adds `business_criticality_value = '1 - most critical'`,
    the population the KPI measures.
* **`record_prefixes.json`** — availability/outage/uptime/downtime aliases.
* **`clients/web_ui.py`** — `list_predicates` must be called unfiltered, plus an explicit
  "records behind a metric" rule: never substitute an adjacent-sounding population.
* **`predicate_registry.Binding.caveat`** (new) surfaced into `composition.notes` by
  `composer.plan`. Needed because the predicate *cannot* reproduce the KPI: it also
  excludes hypercare projects (`IS NULL`) and non-IT support groups (a subquery), and a
  binding condition supports neither op. The listing is a small superset and now says so.

### Verified live after the fix

`logs/cora-2026-08-03.log` at 22:08:48:

```
compose_metric | predicates=['availability_impacting'], select=['incident_id'], period='last month'
compose entity=incident -> anchor=itsm_availability.tbl_tableau_outagesv4
query ok: 58 row(s)      # INC0376682, INC0377132, INC0378948, …
```

58 matches the independent `count(distinct incident_id)` for the same scope.

---

## 2. Defect 2 — dimensions silently dropped in ad-hoc queries

### What happened

*"show me changes closed by sector and type for last month"* returned a single ungrouped
number and the summary *"dataset lacks sector/type disaggregation"*. That claim is false —
`itsm_change.tbl_change` declares both breakdowns explicitly:

```yaml
- name: business_name      canonical: sector             alias: sector
- name: type_description   canonical: change type, type  alias: change type, type
```

### Root cause

`column_resolver` exists to translate business words into physical columns. It was wired
into two of the three query paths:

| path | resolves `sector` / `type`? |
|---|---|
| `run_kpi` — via `resolve_dim_word`, `query_engine.py:305` | yes |
| `compose_metric` — via `composer.py:231` | yes |
| `query_dataset` — via `sql_builder._resolve_col` | **no** |

`_resolve_col` did only `loader.column_info(fqn, colname)` — a literal name lookup.
`sector` is not a physical column, so it raised `BuilderError`, and the dimension loop
(`sql_builder.py:431-437`) catches that and **drops** the dimension.

Reproduced before the fix:

```
base=itsm_change, dimensions=['sector','type'], grain=month, period=last month
  -> columns ['bucket','value'] | rowcount 1
  -> rows [{'bucket': '2026-07-01T00:00:00', 'value': 9734}]
  -> dropped_dimensions = ['sector','type']
  -> SELECT date_trunc('month', a.open_date) AS bucket, count(*) ... GROUP BY date_trunc(...)
```

The governed path handled the same breakdown correctly at the same time
(`run_kpi('total-volume', mode='table', dim=['sector','type'])` → 20 rows,
`GROUP BY a.business_name, a.type_description`), which is why the bug looked
dataset-specific when it was route-specific.

### Fix

* `sql_builder._resolve_col` falls back to `column_resolver.resolve_column_detail` per
  in-scope table. Applies to dimensions, filters and detail listings alike. The
  literal-name path stays role-agnostic so nothing that resolved before behaves
  differently; only the new vocabulary search is role-narrowed, so a `GROUP BY` cannot
  land on a timestamp.
* `BuildResult.resolved_columns` (new) — `{word: {table, column, how}}`, so the answer can
  name the column that actually ran.
* `dropped_dimensions_note` (new) — lists the words that *do* resolve on that table.
* Prompt: the alias/canonical vocabulary is valid input, and a `dropped_dimensions`
  result must never be described as a breakdown.

After:

```
dimensions=['sector','type'], date_field=closed_date, period=last month
  -> columns ['sector','type','value'] | rowcount 20
  -> [{'sector':'ENGINEERING','type':'NORMAL','value':230}, …]
  -> resolved_columns: sector->business_name, type->type_description (how: vocabulary)
  -> SELECT u0.sector, a."type_description", count(*) …
     CROSS JOIN LATERAL unnest(a."business_name") … GROUP BY u0.sector, a."type_description"
```

### Not reproduced

The reported figure **8386** matches no query I could reconstruct (`closed_date` July =
3,835; `open_date`/`work_start_date_time` = 9,673–9,734), and that turn does not appear in
any file under `logs/`. The *shape* reproduces exactly, so the diagnosis is sound, but the
specific route that turn took is unconfirmed.

---

## 3. Semantic-layer coverage and streamlining

### The audit that prompted it

`python -m cora_mcp.predicate_registry --coverage`

* **11 of 31 declared tables had any predicate. 20 had none.**
* Two entire entities — `ansible_automation` (3 tables) and `automation_index` (4) — had
  no qualifier vocabulary at all.
* **31 of the 34 bindings are a single column equal to a single value.** Only 3 carry
  judgment: two free bindings and one composite.
* `schema_v3.yaml` already declares **141 dimension-role columns with `possible_values`,
  holding 373 values** (146 columns across all roles).

### What was built

1. **`tools/gen_predicates.py`** — measures each candidate column's real domain in
   Postgres and emits per-table bindings preserving the stored type. Gated on
   scope-shaped column names, measured cardinality, and grain-key presence.
2. **Two-layer `PredicateRegistry`** — loads `predicates.generated.json` if present, then
   applies `predicates.json` on top. Curated always wins. Deliberately asymmetric
   strictness: a duplicate synonym in the curated file still raises
   (`PredicateConfigError`), while a generated collision is dropped and logged, because
   machine output over hundreds of values must not stop startup. `shadowed_terms()`
   reports every masked phrase.
3. **`--coverage`** — the visibility artifact: per table, covered / partial / no-predicate,
   naming the scope columns that lack one.
4. **Derived entity aliases** — `record_lookup._derived_entity_aliases()` produces the
   obvious names from the schema's entity names (with a singular/plural step in whichever
   direction the name isn't already in, so `major_incidents` yields "major incident").
   `record_prefixes.json` went from 21 hand-typed aliases to 13 genuinely non-derivable
   ones. A dangling overlay slug now logs a warning instead of vanishing.

`--verify` already exited non-zero on mismatch, so it works as the CI gate unchanged:
`23 predicate(s), 33 probe(s), 0 mismatch(es), 0 unreadable → VERDICT: ok`.

### Why the generated file is NOT committed or autoloaded

A full run produces **184 predicates over 11 tables**, covering every populated entity —
real breadth. But reviewing the output kept surfacing entries that are correct SQL and
wrong vocabulary:

* `contact_type_phone`, `active_indicator_type_security_compliance` — scope-shaped *names*
  holding breakdown *values*
* `closure_code_duplicate_problem` vs `closure_code_duplicate_availability` — machine names
  competing for the bare phrase "duplicate"
* `active_indicator_0` → synonym `"0 major incidents"`
* `closure_code_` — from an empty-string value

Each tightening pass found another borderline column. **That is the finding, not a snag:** a
name-shape rule cannot decide what counts as a business scope, and 184 machine-named
entries would also land in every `list_predicates` response feeding the agent's prompt.

An earlier version of the gate used `deeper_insights: true` and produced `country_usa`,
`sub_business_name_support`, `it_vendor_name_everest_dx`. That flag marks good *breakdown*
columns — close to the opposite of a scope. Making `support` and `analytics` predicate
phrases would break routing, since `filter_aliases.json` + `column_resolver` already handle
those correctly as **filters**. The gate is now an explicit allowlist of scope-shaped names
(`status*`, `state`, `*_type`, `priority*`, `risk*`, `closure_code*`, `*_indicator`,
`methodology`, `stage*`, …) with a small false-positive denylist.

**Intended loop:** `--coverage` (what's missing) → generator (what the real values are) →
hand-curate the ones worth vocabulary into `predicates.json` → `--verify`.

Note also that nothing was deleted from `predicates.json`. An early plan said it would
shrink to ~5 entries; that was wrong — all 23 carry curated synonyms a generator cannot
invent (`mi`, `sev1`, `out of sla`, `risky change`, `traditional release`,
`unfulfilled request`, `outstanding`).

---

## 4. Bug found on the way — wrong entity id column

`_human_id_column` derives the human id by taking the first non-system `*_id`:

| entity | derived (was in use) | declared in `record_prefixes.json` |
|---|---|---|
| `itsm_release` | `change_id` — wrong | `release_number` |
| `itsm_service_request` | `first_task_id` — wrong | `request_item_id` |

Linked-record listings (`query_engine.py:1352`) and entity-keyed lookups used the derived
value, so a release listing was keyed on a **change** id.

The declared value was rejected for a subtle reason: `release_number` is declared
`role: dimension` on `itsm_release.tbl_pepops_release_mgmt`, and `_human_id_column` only
consults the *identifier* list. `_entity_id_column()` (new) prefers the declared value and
accepts it whatever role the schema gave it.

**This reverses an earlier claim in this work.** `id_column` in `record_prefixes.json` was
described as redundant because the heuristic derives it. It is not — it is the authority.
`tests/test_semantic_layer_wiring.py::test_the_heuristic_alone_is_wrong_for_release_and_service_request`
pins why, so it does not get deleted as dead config.

---

## 5. Schema-vs-database reconciliation

`python tools/reconcile_schema.py --all` →
**110 error(s), 192 warning(s), 0 unreadable — VERDICT: SCHEMA DRIFT**

`schema_v3.yaml` is the planner's whole world: `sql_builder` resolves every column through
it, `value_resolver` validates filter values against its `possible_values`, and
`compose_metric` scores candidate tables from it. Every disagreement below is silent at
plan time and only surfaces as a wrong answer or a runtime error.

| sev | meaning | count |
|---|---|---|
| E6 | declared `time.field` missing or not temporal | 3 |
| E1 | declared table absent from the database | 7 |
| E2 | declared column absent | 19 |
| E5 | declared name differs only in case | 3 |
| E3 | declared type family disagrees | 6 |
| E4 | declared `possible_value` matches no row | **72** |
| W1 | in the database, not declared | 180 |
| W2 | domain incomplete or unenumerable | 12 |

### E6 — fix first

A time field that does not exist breaks **every** dated question on that table, not one
column's worth.

```
itsm.tbl_incident_change_relation.dw_last_updt_dtm       is varchar, not a date/timestamp
itsm.tbl_incident_problem_relation.dw_last_updt_dtm      does not exist
itsm_servicerequest.tbl_request_item_sector.dw_last_updt_dtm   does not exist
```

`tbl_request_item_sector` is the table `QUESTION_CATALOG.md:223` names as the *workaround*
for SR sector questions — so that workaround cannot carry a period.

### E1 — 7 declared tables are not in this database

All 3 `itsm_ansible.*`, `itsm_servicerequest.tbl_sc_cat_item`, `tbl_sc_req_item`, both
`slm.exception_*`. So `ansible_automation` cannot be given predicate coverage here at all —
its tables are absent, not merely uncurated.

### E2 — two documented fixes were never applied

* `QUESTION_CATALOG.md:224` states the SR sector columns were *"wrongly declared there;
  removed"*. They are **still declared**: `tbl_request_item.business_name`,
  `region_name`, `sub_business_name` and all three `_text` variants.
* `MULTI_METRIC_ANALYSIS.md` §0.1 states the same for
  `itsm_availability.tbl_tableau_major_incdnt.{business_name, region_name,
  sub_business_name}`. Still declared.

Both still generate SQL naming columns Postgres does not have.

### E4 — the domains are wholesale disjoint, not incomplete

72 columns. Spot-checked pairs show near-total disagreement rather than a missing entry, so
`value_resolver` **rejects every real value** on those columns:

```
priority_description     declared [3, 2, 1]                    actual ['P2', 'P1']
target                   declared ['RESPONSE','RESOLUTION']    actual ['4 HOURS', '8 HOURS']
sla_type                 declared ['OLA','SLA']                actual ['RESOLUTION']
service_class            declared ['Technology Management…']   actual ['APPLICATION','INFRASTRUCTURE','NETWORK','SECURITY']
major_problem_indicator  declared ['NO','YES']                 actual ['TRUE','FALSE']
sla_stage                declared ['CANCELLED','COMPLETED','IN PROGRESS','PAUSED']  actual ['COMPLETED','IN_PROGRESS']
hierarchy_5              declared 7 values                     actual 24 entirely different ones
hypercare_project        declared ['PR303072 - PEPSICO…']      actual ['NO', 'YES']
```

This is the concrete reason `predicates.json` marks its values `resolved` to **bypass** the
schema domain. Fixing E4 is what lets that bypass eventually go away.

### E3 — type family, which changes emitted SQL

`sql_builder._is_array` chooses between `a.col` and `CROSS JOIN LATERAL unnest(a.col)` from
the declared type alone.

```
itsm_incident.tbl_incident_sla.sla_breached_indicator     declared varchar   actual int4
itsm_incident.tbl_sla_resolution.sla_breached_indicator   declared varchar   actual int4
itsm_incident.tbl_all_incidents.reopen_count              declared numeric   actual varchar
itsm_servicerequest.tbl_request_item.sla_start_date_time  declared date      actual timestamp
itsm_servicerequest.tbl_request_item.sla_end_date_time    declared date      actual timestamp
itsm.tbl_incident_change_relation.dw_last_updt_dtm        declared timestamp actual varchar
```

The `sla_breached_indicator` rows corroborate the predicate layer: `predicates.json` binds
integer `1` there, matching the database. The schema is the thing that is wrong.

### W1 — 180 undeclared columns

Includes the 7 on `itsm_availability.tbl_tableau_outagesv4` that the
`availability-percentage` KPI scopes on (`support_group_system_id`,
`hypercare_project_system_id`, `business_system_id`, `region_system_id`,
`service_system_id`, `sub_business_system_id`, `service_control`). Their absence from the
schema is exactly why `availability_impacting` ships with a caveat instead of reproducing
the KPI.

### Two tool defects the live run exposed

1. **Case-insensitive matching.** The first version reported
   `itsm_release.tbl_pepops_release_mgmt.BUSINESS_NAME` as *absent* (E2), and `apply_fixes`
   would have **deleted a column that exists** — Postgres holds `business_name`. Now a
   separate `E5` that renames. Caught because W1 listed the lowercase name on the same
   table.
2. **Blank values excluded from measured domains.** `closure_code` was about to get `''`
   written in as a filterable value.

### The candidate file

**`schema_v3.reconciled.yaml`** — 108 mechanical corrections; `schema_v3.yaml` untouched.
Validated through `SchemaLoader`:

* loads clean — 9 entities / 30 tables, slug list identical
* 978 → 959 columns, exactly the 19 E2 drops
* curated metadata survives: `alias='sector'`, `canonical='change type, type'`, and the
  `cross_join` LATERAL clause
* `BUSINESS_NAME` **renamed** to `business_name`, not deleted
* `time` blocks preserved (`open_date`, `the_date`)

It deliberately leaves the judgment calls: no undeclared columns added (they need a role
and often `canonical`/`alias` vocabulary), no tables dropped (absent here may mean present
in another deployment).

Checked before recommending the drops: none of the 10 `sr-*` KPI configs reference
`business_name`/`region_name`/`sub_business_name` in their `fields`, so removing them does
not break the governed path.

**Not swapped in.** Replacing a 124 KB schema is a call for the owner, and a YAML
round-trip reformats the whole file, so the diff is large even though the semantic delta is
those 108 changes.

---

## 6. Files

### Changed

| file | what |
|---|---|
| `predicates.json` | + `outage`, `availability_impacting` (DB-verified); `caveat` on a binding |
| `record_prefixes.json` | aliases reduced to non-derivable only; `RITM`/`REQ` slug typo fixed |
| `cora_mcp/predicate_registry.py` | two-layer load, `Binding.caveat`, `coverage()`, `--coverage`, `is_generated`/`curated_names`/`shadowed_terms` |
| `cora_mcp/composer.py` | binding caveats surfaced into `composition.notes` |
| `cora_mcp/sql_builder.py` | `_resolve_col` vocabulary fallback; `resolved_columns` on `BuildResult` |
| `cora_mcp/query_engine.py` | `dropped_dimensions_note`, `resolved_columns`; `_entity_id_column` for linked records |
| `cora_mcp/record_lookup.py` | `_derived_entity_aliases`, `_plural`/`_singular`, `_entity_id_column`, dangling-slug warnings |
| `clients/web_ui.py` | routing rules: unfiltered `list_predicates`, records-behind-a-metric, vocabulary columns, dropped-dimension handling |

### Added

| file | what |
|---|---|
| `tools/gen_predicates.py` | DB-measured predicate candidate generator |
| `tools/reconcile_schema.py` | schema-vs-database drift gate (`--structure` / `--all` / `--out`) |
| `schema_v3.reconciled.yaml` | candidate corrected schema, for review |
| `tests/test_availability_routing.py` | 9 tests — 8 fail without the fix |
| `tests/test_adhoc_dim_vocabulary.py` | 8 tests — 6 fail without the fix |
| `tests/test_semantic_layer_wiring.py` | 33 tests — alias derivation, id-column precedence, layer precedence, coverage |
| `tests/test_reconcile_schema.py` | 30 tests, no DB — type families, `E5` rename-not-drop, mechanical-only fixes |

### Commands

```bash
python -m cora_mcp.predicate_registry --coverage        # what has no predicate (offline)
python -m cora_mcp.predicate_registry --verify          # CI gate: non-zero on mismatch
python tools/gen_predicates.py --dry-run                # candidate predicates, writes nothing
python tools/gen_predicates.py --entity change          # one module, for curating it
python tools/reconcile_schema.py                        # structural drift, seconds, CI gate
python tools/reconcile_schema.py --all                  # + measured domains, minutes
python tools/reconcile_schema.py --all --out schema_v3.reconciled.yaml
```

---

## 7. Outstanding

1. **Decide on `schema_v3.reconciled.yaml`.** 108 mechanical corrections ready; needs a
   review of the large diff, then a full test run against it.
2. **E6 first, then E2/E5.** These produce runtime errors and dropped dimensions today.
3. **Declare the 7 `outagesv4` columns** the availability KPI scopes on. That is what would
   let `availability_impacting` reproduce the KPI exactly and retire its caveat — it needs
   an `IS NULL` op and a subquery op in the binding grammar too.
4. **Curate predicates for the 20 uncovered tables** using `--coverage` + the generator.
   `ansible_automation` cannot be done here (E1: tables absent).
5. **Decide who owns overlapping phrases.** `emergency_change` (predicate,
   `type_description='EMERGENCY'`) and the `emergency` KPI in module `cm` both answer to
   "emergency changes", in two different stores. Today the prompt arbitrates at runtime.
6. **Reconcile KPI configs against the database too.** `total-volume`'s static filter uses
   `upper(a.type_code)`, and `type_code` is not declared in `schema_v3.yaml`. The same
   check applied to each KPI's `fields[].column` and `time.column` would catch that class;
   `reconcile_schema.py` does not cover it yet.
7. **Consider moving `predicates.json` into OpenSearch** alongside the KPI configs and
   module meta, which are hot-reloadable by design. Adding a KPI is a reindex; adding a
   predicate is a redeploy.