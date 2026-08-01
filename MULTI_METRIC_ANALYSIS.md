# Multi-Metric Questions — Scenario Analysis

> ## ⚠️ SUPERSEDED IN PART — read §0 first
>
> Phase 0 was executed against the live database. **It falsified this document's central
> recommendation.** The analysis method and the scenario matrix hold; the conclusion
> ("Option 1: a single-table anchor is enough") does not. §0 records what was measured
> and what was built instead. Sections 5-8 are corrected inline; §2 and §3.3 are kept as
> written so the reasoning trail is auditable, with corrections marked.

---

## 0. Phase 0 results — measured, not inferred

Every claim below is from `information_schema` or a `SELECT` against `vtx5`.

### 0.1 The recommendation was wrong

`itsm_availability.tbl_tableau_major_incdnt` **has no `business_name`, `region_name` or
`sub_business_name` column.** `schema_v3.yaml` declared all three; the database has none
of them (`SELECT business_name FROM …` → `UndefinedColumnError`). So the "single-table
answer" in §2/§3.3 does not exist — that table cannot express a sector filter at all.

Measured capability grid (`-` = column absent):

| column | all_incidents | major_incidents | incident_sla | sla_resolution | sla_response | tableau_major |
|---|---|---|---|---|---|---|
| `business_name` | ARRAY | ARRAY | ARRAY | ARRAY | ARRAY | **-** |
| `major_incident_indicator` | **boolean** | varchar | - | - | - | - |
| `sla_breached_indicator` | - | - | **integer** | **integer** | varchar | **boolean** |

**No single table carries measure + major scope + breach state + sector.** A semi-join is
therefore *required*, not optional — which means Option 2 (predicate registry + `EXISTS`)
was the necessary build, and Option 1 was never viable. Implemented; see §9.

### 0.2 D2 resolved — in favour of the config

`tbl_sla_resolution.sla_breached_indicator` is **`integer` holding `0`/`1`** (0=135518,
1=331). `sla-resolve`'s `= '0'` is correct and the metric returns **99.76**, not `0.00`.
The schema's `['YES','NO']` domain was the wrong half of the contradiction.

### 0.3 A live KPI was returning zero

`im-major-incidents` emitted `lower(major_incident_indicator::text) = '1'` against a
**boolean** column, so it matched nothing: **the "Major Incidents Count" tile read 0.**
Declaring the real type makes it emit `major_incident_indicator = %s`, bound as `True`.
**Verified 0 → 161** for the same window. An audit of all 66 configs' static filters found
this was the only instance.

### 0.4 `possible_values` cannot be trusted anywhere

Not a few stale entries — a systemic problem:

| Column | Schema claims | Actually in DB |
|---|---|---|
| `tbl_all_incidents.active_indicator_type` | `NO, YES` | `Application Services, Security & Compliance, Infrastructure Services` |
| `tbl_all_incidents.status_name` | `ASSIGNED, IN QUEUE, NEW, PENDING…` | `CLOSED, OPENED, RESOLVED, CANCELED` |
| `tbl_change.production_system_type` | `Production, Non Production` | `REAL-TIME, SCHEDULED, BATCH` |
| `tbl_problem.source_description` | `PROACTIVE` | `REACTIVE, PROBLEM TASK, USER REPORT…` |
| `tbl_incident_sla.sla_target` | `resolution, response` | `24 HOURS, 120 HOURS, 8 HOURS…` |

**Design consequence:** the predicate layer carries its own DB-verified values and marks
them `resolved` so `sql_builder` skips `value_resolver`. Re-resolving a known-good value
against these domains would *reject* it. This is why `status_name = 'OPENED'` works
through a predicate but is rejected as a raw filter.

### 0.5 Structural drift removed

20 columns were declared but absent from the DB (across 9 tables), plus 2 `time.field`
refs pointing at nonexistent columns. Removed/corrected; re-verification reports **0
fabricated columns**. 7 declared tables don't exist in this database
(`itsm_ansible.*`, `tbl_sc_cat_item`, `tbl_sc_req_item`, `slm.*`) and were left alone —
they may be provisioned elsewhere.

Per your instruction, no further `possible_values` edits were made and the schema is
otherwise treated as fixed.

---

**Status:** Phase 0 executed; Option 2 implemented · **Branch:** `feature/pep-poc`
**Driving question:** *"Get me the count of major incidents for business PBNA that were
SLA breached during the last 3 months."*
**Goal:** Establish, before writing code, **which** natural-language question shapes the
current flow answers correctly, which it fails loudly, and which it answers **wrongly and
silently** — then pick the smallest change that closes the real gap.

> This document is analysis only. It deliberately does **not** propose a metric-composition
> engine up front, because the trace below shows the driving question is a *single-table*
> question that fails for a discoverability reason, not a composition reason.

---

## 1. The core principle

The driving question looks like it contains two metrics (**Major Incidents**, **SLA
Breached**). It does not. It contains **one measure and two predicates**:

| Term | What it actually is | Where it lives today |
|---|---|---|
| "count of … incidents" | **Measure** — `count(distinct incident_id)` | `config/im-major-incidents.json:112` |
| "major" | **Scope predicate** — a WHERE clause | `major_incident_indicator = '1'` (`im-major-incidents.json:115-125`) |
| "SLA breached" | **Scope predicate** — a WHERE clause | `sla_breached_indicator` (4 tables, 3 encodings — see §5) |
| "for business PBNA" | **Dimension + value** | `business_name` / `sector` alias |
| "during the last 3 months" | **Time window** | `date_resolver.resolve_dates` |

Two query semantics follow, and they are not interchangeable:

```
one measure + N predicates    ->  ONE query,  predicates AND-ed in WHERE
N measures                    ->  N queries,  results reported side by side
```

**Therefore: "detect multiple metrics" is the wrong primitive. "Classify each detected term
into {measure, predicate, dimension, value, time}" is the right one.** Detection is the easy
half. A system that detects two "metrics" and combines them will eventually emit
`count(major) + count(breached)`, or intersect two aggregates — both meaningless.

Corollary, and the reason this document exists: **predicates never combine as anything other
than AND.** There is no relationship to infer between them. What must be *decided* is which
table can satisfy them all at once.

---

## 2. The scenario matrix

The complete space is two axes: how many **measures** the question contains × where the
**qualifier columns** live. Verdicts are from tracing the code, not estimation.

| # | Scenario | Example | Path taken | Verdict |
|---|---|---|---|---|
| **S1** | 1 measure, no qualifier | "how many major incidents" | `search_kpis` → `run_kpi` | ✅ works |
| **S1b** | same, multi-period window | "count … during the last 3 months" | prompt forces `mode='series'` | ⚠️ **wrong shape** — 3 monthly rows, not one count |
| **S2** | 1 measure + **declared filter** | "major incidents for business PBNA" | alias `business`→`sector` | ✅ works |
| **S3** | 1 measure + **schema column, same table** | "major incidents with status closed" | schema fallback (`query_engine.py:109`) | ✅ works |
| **S4** | 1 measure + qualifier on **another table** | one illustration of many | filter rejected → ad-hoc → join → empty graph | ❌ was hard fail → ✅ **now `compose_metric`** |
| ~~**S5**~~ | ~~S4, but one table binds everything~~ | ~~via `tbl_tableau_major_incdnt`~~ | — | ❌ **DOES NOT EXIST — §0.1: that table has no `business_name`. Measurement killed this scenario.** |
| **S6** | N measures, same entity + grain | "MI count and MTTR for PBNA" | agent issues 2 `run_kpi` calls | ✅ works informally |
| **S7** | qualifier **welded into a measure** | "SLA breached count" | `search_kpis` → `sla-resolve` returns a **%** | 🔴 **silent wrong answer** |
| **S8** | 0 measures — detail listing | "show me the major incidents in PBNA" | `query_dataset(select=[…])` | ⚠️ same base problem as S4/S5 |
| **S9** | qualifier is **no column anywhere** | "major incidents caused by vendor negligence" | reject, listing valid filters | ✅ loud fail (correct) |
| **S10** | measure name matches **two configs** | "major incident count" | BM25 picks one silently | 🔴 **silent definitional drift** |
| **S11** | qualifier mistaken for a dim value | `filters={'priority':'major'}` | `value_resolver` rejects, lists P1–P5 | ✅ loud fail (correct) |
| **S12** | many qualifiers, all on primary table | "major incidents, PBNA, P1, APAC" | all AND-ed | ✅ works |

**Summary: 6 of 12 work. 3 fail loudly (correct behaviour). 3 are real problems — S4 (hard
fail), S7 and S10 (silent wrong answers).**

Answer to *"will the current flow work?"* — **yes for everything except cross-table
qualifiers, and the driving question is exactly that case.** Not a partial failure: a
guaranteed one, reached after 4–6 tool calls.

Answer to *"what happens when there are no multiple metrics?"* — **S1/S2/S3/S12, which all
already work**, including qualifiers no config ever declared as filters (see §3.1). This is
the bulk of real traffic and it needs no new machinery.

---

## 3. Traces of the decisive scenarios

### 3.1 S2 / S3 — why single-metric questions already work

`resolve_filter_key` (`query_engine.py:89`) tries four things in order:

```
1. canonical key present in filters.allowed        -> 'sector'
2. filter_aliases.json alias -> canonical          -> 'business' -> 'sector'
3. resolve_dim_via_schema(config, term, roles=())   -> 'status' -> 'status_name'
4. raise QueryError, listing the available filters
```

Step 3 is the load-bearing one: **any dimension column on a KPI's primary table is already
filterable**, declared or not. `_augment_fields` (`query_engine.py:337`) synthesizes the
field entry, `_validate_filters` (`:150`) accepts it because it is a real schema column, and
`_resolve_filter_values` (`:211`) resolves the value against the column's declared domain.

**Implication for the work plan:** single-table predicate composition is a solved problem in
this codebase. What is missing is narrower than "multi-metric support".

### 3.2 S4 — the driving question, call by call

```
1. search_kpis("count of major incidents ... PBNA ... SLA breached ... last 3 months")
     -> im-major-incidents  (or incident-count — see S10)

2. run_kpi(im-major-incidents, filters={'business':'PBNA','sla_breached':'yes'})
     resolve_filter_key('sla_breached'):
       not in filters.allowed                                    x
       not in filter_aliases.json                                x
       resolve_dim_via_schema -> tbl_all_incidents has no such column   x
     -> QueryError: "unknown filter 'sla_breached' for KPI 'im-major-incidents'.
                     available filters (with aliases): {sector: [business, p&l], ...}"

3. agent falls to prompt step 4 -> describe_dataset('itsm_incident')
     -> sees sla_breached_indicator, tagged table = itsm_incident.tbl_sla_resolution

4. query_dataset(base='itsm_incident', filters=[{field:'sla_breached_indicator',...}])
     base -> entity_primary_table() -> tables[0] = tbl_all_incidents
     sql_builder._resolve_col -> BuilderError (sql_builder.py:134):
       "NOTE: a column named 'sla_breached_indicator' exists in
        ['itsm_incident.tbl_incident_sla', 'itsm_incident.tbl_sla_response',
         'itsm_incident.tbl_sla_resolution'], which is/are not part of this query.
         Add it via join_with"

5. query_dataset(..., join_with=['...'])
     RelationshipGraph has ZERO edges (relationships.py:42 logs 0)
     -> NoJoinPathError (relationships.py:149):
        "no relationship path from ... Declared tables: []"
```

Dead end at step 5, unrecoverable. Note step 4's message is genuinely excellent — it names
the exact tables and the exact fix. **The system diagnoses itself correctly and then cannot
act on its own advice, because the relationship graph is empty.**

### 3.3 S5 — the near-miss that reframes the whole problem

`schema_v3.yaml:2475` declares `itsm_availability.tbl_tableau_major_incdnt`: a
**major-incident-grain table that carries `sla_breached_indicator`** alongside
`business_name`, `region_name`, `service_area`, `priority_description`, `open_date_time`,
`incident_id`. Every predicate in the driving question binds to this one table.

`sql_builder._resolve_base` (`sql_builder.py:107`) accepts a fully-qualified table, not only
an entity slug:

```python
if "." in base and self.loader.get_table(base):
    return base
```

So this works **today, with no code change**:

```
query_dataset(base    = "itsm_availability.tbl_tableau_major_incdnt",
              measure = {"agg":"count_distinct","column":"incident_id"},
              filters = [{"field":"sla_breached_indicator","op":"=","values":["true"]},
                         {"field":"business_name","op":"=","values":["PBNA"]}],
              period  = "last 3 months",
              date_field = "open_date_time")
```

producing:

```sql
SELECT count(DISTINCT a.incident_id) AS value
FROM itsm_availability.tbl_tableau_major_incdnt a
WHERE a.sla_breached_indicator = %s
  AND lower(a.business_name) = %s
  AND cast(a.open_date_time AS timestamp) BETWEEN %s AND %s
LIMIT 200
```

**But the agent will essentially never find it:**

| Obstacle | Detail |
|---|---|
| The entity slug points at the wrong table | `entity_primary_table('itsm_availability')` (`schema_loader.py:139`) returns `tables[0]` = `tbl_tableau_outagesv4` — the **outages** table |
| No KPI config anchors there | `search_kpis` cannot surface it; the `am` configs (`incident-count`, `mttr`, `resolve-hours`) all anchor on `itsm_incident.tbl_major_incidents` |
| Wrong module neighbourhood | "major incident" routes to `im`/`am`; this table sits under `availability` |
| The prompt discourages the required move | `web_ui.py:107` says *"use ONLY the exact column names it returns"* and *"do NOT invent a base"* — the fully-qualified-table base is exactly what's needed |

**The correct query is reachable by construction and unreachable in practice. Closing that
gap is the cheapest fix in this entire analysis.**

### 3.4 S7 — the confident wrong number (independent of multi-metric work)

*"How many incidents breached SLA last month?"* → `search_kpis` hits `sla-resolve` with high
confidence → `run_kpi` returns e.g. `96.5`. The measure (`config/sla-resolve.json:117`) is:

```sql
round(100.0 * count(distinct incident_id) filter (where sla_breached_indicator = '0')
      / nullif(count(distinct incident_id),0), 2)
```

That is the **percentage that did *not* breach**. The user asked for a **count that did**.
The predicate is welded inside the aggregate, so it cannot be inverted, extracted or
intersected. **There is no breach-count metric anywhere in the catalog.** Nothing in the
pipeline detects the mismatch — no error, no dropped-filter note.

**This is the highest-severity scenario and it is live today, with or without any
multi-metric work.**

### 3.5 S10 — two configs, one name, different numbers

| Config | Module | Table | Time column | Static filters |
|---|---|---|---|---|
| `im-major-incidents` | `im` | `tbl_all_incidents` | `open_date_time` | `major_incident_indicator='1'` |
| `incident-count` — "MI Count(Closed)" | `am` | `tbl_major_incidents` | `closed_date_time` | `status_name='CLOSED'`, `service_area!='NON-IT'` |

Both legitimately answer "major incident count", on different tables, with different date
semantics (opened vs closed) and different scopes. BM25 relevance picks one; the user is
never told which definition was used. Any composition layer built on top inherits and
amplifies this.

---

## 4. Blockers

| # | Blocker | Evidence | Blocks |
|---|---|---|---|
| **B1** | **Relationship graph is empty** — `schema_v3.yaml` has zero `relationships:` entries; the join facts exist only as unparsed `inner_join:` strings on individual columns (`schema_v3.yaml:1387`, `:1712`, `:2669`). `tools/harvest_relationships.py` exists to promote them but its output has never been committed. | `RelationshipGraph._load` logs 0 edges | **all** cross-table composition (S4) |
| **B2** | **Configs and schema have drifted** — `im-major-incidents` filters on `tbl_all_incidents.major_incident_indicator` and times off `open_date_time`; **neither column exists** in that table's schema block (it has `open_date`, `created_date`, `closed_date`, `resolved_date`, no major flag). The DSL path works because `gen_query.dsl_build` reads `config["fields"]` and never consults the schema; `sql_builder` validates against the schema and would reject them. | `config/im-major-incidents.json` vs `schema_v3.yaml:10-172` | any `sql_builder`-based work on `tbl_all_incidents` |
| **B3** | **No semi-join primitive** — `sql_builder` offers only `INNER`/`LEFT JOIN` (`sql_builder.py:196`). Cross-table predicates need `EXISTS`, because `tbl_sla_resolution` is 1:N per incident (one row per SLA clock). An INNER JOIN is safe for `count(distinct)` but silently wrong for `avg`/`sum`/`count(*)`. `preflight` warns about fan-out (`adhoc.py:363`) but nothing prevents it. | — | correct cross-table composition |
| **B4** | **Predicate vocabulary is authored but unused** — all four `sla_breached_indicator` columns carry `canonical: sla breached` / `alias: sla breached`. `schema_loader.py:181` passes this through to `describe_dataset` output, but **no resolution path consults it**: `resolve_dim_via_schema` matches column *names* only (`{word, word_name, word_description}`). | `schema_loader.py:181` | cheap win — "sla breached" could resolve without any new registry |
| **B5** | **Trend-vs-count routing keys off window length, not intent** — the prompt selects `mode='series'` for *"any multi-period window ('last 3 months', 'last quarter', 'this year')"*. `_auto_grain` then buckets a 90-day span monthly, so a plain count-over-window returns 3 rows. | `clients/web_ui.py:107` | S1b, every count-over-window question |

---

## 5. Data contradictions (must be settled first)

### D1 — one predicate, three encodings

| Table | Type | Declared `possible_values` | Ref |
|---|---|---|---|
| `itsm_incident.tbl_incident_sla` | varchar | `['0','1','NO','YES']` | `schema_v3.yaml:308-313` |
| `itsm_incident.tbl_sla_response` | varchar | `['0','1','NO','YES']` | `schema_v3.yaml:568-573` |
| `itsm_incident.tbl_sla_resolution` | varchar | **`['YES','NO']`** | `schema_v3.yaml:790-795` |
| `itsm_availability.tbl_tableau_major_incdnt` | **bool** | `['true','false']` | `schema_v3.yaml:2542-2546` |

**A predicate cannot be a global string constant — it must compile per-table.** The same is
true of dimensions: `business_name` is `text []` on `tbl_all_incidents` and
`tbl_major_incidents` (needs `unnest`) but `varchar` on `tbl_tableau_major_incdnt`; priority
is `priority_code` `['P1'..'P5']` on one and `priority_description` `[3,2,1]` on the other.

### D2 — config and schema directly contradict each other

On `tbl_sla_resolution` the schema declares the domain `['YES','NO']`, while
`config/sla-resolve.json:117` compares that exact column to `'0'`. **Both cannot be right,
and either way something is broken now:**

- **If stored values are `YES`/`NO`** → `filter (where sla_breached_indicator = '0')` matches
  zero rows, so **SLA Resolve % returns `0.00` for every window** — a live shipping metric
  returning a wrong number.
- **If stored values are `0`/`1`** → the schema's domain is wrong, and
  `value_resolver.resolve_or_raise` (`value_resolver.py:152`) **rejects** a user asking for
  breached=`'1'` (not in `['YES','NO']`, no synonym in `value_aliases.json`, difflib ratio
  below the `0.84` cutoff at `value_resolver.py:42`). Worse: asking for `'yes'` passes
  validation and then **returns 0 rows** — a clean, silent zero.

**D2 must be resolved before any predicate work.** A predicate registry authored on top of an
unverified constant would encode the wrong value in four places instead of one.

---


## 6. What was built (supersedes the original §6-§8 options analysis)

Option 1 is not viable (§0.1), so the generic Option 2 design was implemented. Per your
direction it is **not** shaped around the PBNA/SLA example — that question is one
illustration; the layer is seeded across incidents, problems, changes, releases and
service requests, and anchor choice is scoring, never a hardcoded route.

### 6.1 Predicate registry — `predicates.json` + `cora_mcp/predicate_registry.py`

21 predicates, 34 bindings, all values measured against the DB. A predicate declares
`synonyms`, `entity`, `grain_key` and **per-table bindings**, because the same concept is
stored differently per table:

| `sla_breached` on | compiles to |
|---|---|
| `tbl_incident_sla` (**primary** — both clocks) | `sla_breached_indicator = 1` (integer) |
| `tbl_sla_resolution` | `sla_breached_indicator = 1` (integer) |
| `tbl_sla_response` | `sla_breached_indicator = '1'` (varchar) |
| `tbl_tableau_major_incdnt` | `sla_breached_indicator = true` (boolean) |

A binding with **no conditions** means the table is already scoped that way — every row
of `tbl_major_incidents` is major, so the predicate costs *zero SQL* and is satisfied by
choosing that table. `primary` marks the authoritative table so a generic "breached"
cannot silently narrow to one SLA clock (a real defect the first test run caught).

`python -m cora_mcp.predicate_registry --verify` re-probes every binding and separates
genuine mismatches from connection failures.

### 6.2 `EXISTS` semi-join — `cora_mcp/sql_builder.py`

New `SemiJoin` spec emitting
`EXISTS (SELECT 1 FROM other sj0 WHERE sj0.<key> = a.<key> AND <cond>)`.

**`EXISTS`, not `JOIN`, is the correctness point:** the SLA tables hold one row per SLA
clock per incident, so a join fans out. Harmless for `count(distinct id)`, silently wrong
for `avg`/`sum`/`count(*)` — it inflates by the number of child rows and returns a
plausible number. A semi-join leaves the base row count untouched.

Also added `Filter.resolved`, which bypasses `value_resolver` for registry values —
required by §0.4, since re-resolving a correct value against a wrong domain rejects it.

### 6.3 Composer — `cora_mcp/composer.py`

`plan()` scores every candidate anchor and assembles a validated `QuerySpec`:

```
1 fewer dimensions it cannot break down by
2 more predicates for which it is the PRIMARY table
3 more FREE predicates        (already scoped -> no SQL)
4 more DIRECT predicates      (a WHERE here beats an EXISTS elsewhere)
5 fewer semi-joins
6 table name                  (determinism, so planning is reproducible)
```

Hard requirements: the measure's column, a usable time column, and **every** requested
filter. A filter that cannot bind anywhere is a **refusal**, not a drop — omitting one
answers a different question. Cross-entity predicate sets are refused outright
(incidents and changes are different populations).

### 6.4 Two new MCP tools

`list_predicates` (discovery) and `compose_metric` (execution). The analyst prompt gained
rule **2b** telling it that phrases like "major"/"breached"/"failed" are WHERE clauses and
must go to `compose_metric`, never to `search_kpis` — which would match a similarly-named
KPI and answer a different question (S7).

### 6.5 Shared column resolver — `cora_mcp/column_resolver.py`

Table-scoped word → column, now honouring each column's declared `canonical`/`alias`
vocabulary. That metadata existed in the schema and reached `describe_dataset` but no
resolution path consulted it, so questions phrased in the schema's own vocabulary were
rejected. `query_engine.resolve_dim_via_schema` delegates here, so the existing KPI path
benefits too.

### 6.6 Trend routing (S1b) and instrumentation

The prompt now selects `mode='series'` on **trend intent**, not window length — "how many
in the last 3 months" is one number, not three monthly buckets. Structured
`FILTER_REJECTED` and `JOIN_PATH_MISSING` log lines make the payoff measurable rather than
assumed; `FILTER_REJECTED` also reports whether the rejected term was a known predicate.

---

## 7. Verification

| Check | Result |
|---|---|
| New tests (`composer`, `predicate_registry`, `column_resolver`, `semi_join`) | **77 passed** |
| Existing suite, like-for-like vs a clean worktree at HEAD | `test_series_dim` **8 failed → 9 passed**; all others unchanged |
| Schema re-verified against `information_schema` | **0 fabricated columns** |
| `im-major-incidents` | **0 → 161** |
| Composer plans (9 shapes, offline) | correct anchor, correct refusals |
| Live execution of composed SQL | ⚠️ **not yet run** — the DB link dropped mid-session (`WinError 121/1231`) |

Pre-existing failures NOT caused by these changes, confirmed identical at HEAD: 3
`test_sql_builder` cross-entity joins (empty `relationships:`, blocker B1), `test_adhoc`
(calls an async function synchronously), `test_query_engine` (needs OpenSearch).
`test_adhoc_executes_live` fails rather than skips only because a DSN is now configured —
a broken async test that a missing `.env` used to hide.

---

## 8. Remaining work

1. **Run the composed SQL live** once the DB is reachable — the one unverified step. Start
   with `compose_metric(predicates=["major","sla breached"], filters={"business":"PBNA"},
   period="last 3 months")` and sanity-check the count against a hand-written query.
2. **S7 — breach *count* metric.** Still outstanding: `sla-resolve` welds the predicate
   into its measure, so "how many breached" resolves to a percentage. `compose_metric`
   now answers it, but `search_kpis` will still match the % KPI first.
3. **S10 — duplicate definitions.** `im-major-incidents` (opened, `tbl_all_incidents`) vs
   `incident-count` (closed, `tbl_major_incidents`) both answer "major incident count"
   with different numbers; BM25 picks one silently.
4. **Blocker B1** — `relationships:` is still empty, so true cross-entity *joins*
   (projecting columns from a related entity) remain unavailable. Predicate composition
   does not need it (semi-joins key off the registry's `grain_key`), which is why this is
   no longer urgent. `JOIN_PATH_MISSING` telemetry will show whether it is worth doing.
5. **`tbl_ola_response`** exists in the DB but is not declared in `schema_v3.yaml`, so its
   breach binding had to be dropped — OLA breach questions cannot be composed until the
   table is declared.
