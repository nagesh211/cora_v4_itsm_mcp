# Relationship Review — module `itsm`

Every join key below was **measured against the live `vtx5` database**, not inferred from
column names. Match rates and cardinalities are actual counts. No columns are added,
removed or modified — this document only declares edges between existing tables.

---

## 1. Scope reality check

The schema declares **one module** (`itsm`, code `INONE`) holding **9 entities / 30 tables**.
"Module by module" therefore reduces to entity-group by entity-group.

Before proposing edges, the tables were probed for existence and volume:

| State | Count | Tables |
|---|---|---|
| Present **with data** | **19** | see §3 |
| Present but **empty** (0 rows) | 4 | `automation.tbl_automation_change`, `automation.tbl_automation_end_to_end`, `automation.tbl_automation_servicerequest_task`, `automation.automation_target` |
| **Absent from the database** | 7 | `itsm_ansible.tbl_ansible_job`, `itsm_ansible.tbl_ansible_template`, `itsm_ansible.tbl_ansible_scheduled_jobs`, `itsm_servicerequest.tbl_sc_cat_item`, `itsm_servicerequest.tbl_sc_req_item`, `slm.exception_details`, `slm.exception_info` |

So **relationships are only declarable and verifiable for 19 of the 30 declared tables.**
Two whole entities (`ansible_automation`, `automation_index`) yield no verifiable edge:
the first does not exist in the DB, the second exists but is empty.

Separately, six tables used by **production KPI SQL** are missing from the schema
entirely, so no edge can be declared for them: `itsm_servicedesk.tbl_fcr`,
`itsm_servicedesk.tbl_ssp_volume`, `itsm.tbl_group_hierarchy`, `itsm.tbl_sn_grp_typ`,
`itsm.tbl_sn_grp_typ_xref`, `itsm_servicerequest.tbl_request_task`.
Two more are referenced by the schema's own `inner_join` hints but never defined:
`slm.exception_status`, `slm.exception_type`.

---

## 2. Three traps that a name-based approach would walk into

These are the reason every key was measured. All three would produce edges that join
cleanly in SQL and silently return wrong answers.

**2.1 — `incident_sla_system_id` is table-local, not a shared key.**
It appears in four tables (`tbl_incident_sla`, `tbl_sla_response`, `tbl_sla_resolution`,
`tbl_tableau_major_incdnt`) and looks like the obvious SLA join key. It is not:

```
tbl_sla_resolution  ⋈ tbl_incident_sla on incident_sla_system_id  ->        0 rows
tbl_sla_response    ⋈ tbl_incident_sla on incident_sla_system_id  ->        0 rows
tbl_tableau_major_incdnt ⋈ tbl_incident_sla on incident_sla_system_id ->    0 rows
```

Each table mints its own surrogate. The SLA family joins to incidents on
**`incident_system_id`**, which matches 100% in all three cases.

**2.2 — Two "obvious" FK columns resolve to nothing.**

```
tbl_major_incidents.problem_system_id   -> tbl_problem   :    0 / 1653 matched
tbl_sn_prblm_derivative.change_system_id -> tbl_change   :    0 / 86104 matched
```

The values are well-formed 32-char sys_ids (distinct, not placeholders), so they are
real references — into a record universe that is **not loaded here**. Declaring either
edge yields a join that always returns empty.

**2.3 — `tbl_major_incidents` is not a subset of `tbl_all_incidents`.**

```
tbl_major_incidents                              : 1653 rows
  ... matched into tbl_all_incidents             : 1182
  ... unmatched (on incident_system_id)          :  471
  ... those 471 also fail to match on incident_id:    0 matched
tbl_all_incidents where major_incident_indicator : 1182
```

**28.5% of major incidents exist only in `tbl_major_incidents`.** This validates
`predicates.json` marking `tbl_major_incidents` as the `primary` binding — but the
`tbl_all_incidents` + `major_incident_indicator = true` binding **undercounts major
incidents by 471**. Any inner join between these two tables drops those rows.

Consequence for edge design: the incident↔change bridge must be declared from
**both** `tbl_all_incidents` and `tbl_major_incidents`, so a query anchored on the
major-incident table (which the composer prefers, since `major_incident` binds FREE
there) gets a one-hop path instead of routing through a 71.5%-lossy edge.

---

## 3. Recommended relationships

15 edges, grouped by entity — 7 one-to-one, 6 one-to-many, 2 many-to-many.
`match` = referential match rate measured on live data.

### 3.1 Entity `incident` — anchor `itsm_incident.tbl_all_incidents` (155,610 rows, unique on both keys)

| Edge | Join | Match | Cardinality |
|---|---|---|---|
| `incident_has_sla_clocks` | → `tbl_incident_sla` on `incident_system_id` | 311,220 / 311,220 (100%) | **1:N — exactly 2 rows per incident** |
| `incident_sla_response` | → `tbl_sla_response` on `incident_system_id` | 155,610 / 155,610 (100%) | 1:1 |
| `incident_sla_resolution` | → `tbl_sla_resolution` on `incident_system_id` | 135,923 / 135,923 (100%) | 1:1 over resolved incidents only |
| `incident_company` | → `tbl_incident_company` on `incident_system_id` | 155,610 / 155,610 (100%) | 1:1 satellite |

`tbl_incident_sla` is the union of both SLA clocks (`sla_type_value` is uniformly `'SLA'`
and does **not** discriminate them). Joining it doubles rows — measures over it must be
`count(distinct incident_id)`, which is already the composer's default.

### 3.2 Entity `change` — the edge that unblocks the original question

| Edge | Join | Match | Cardinality |
|---|---|---|---|
| `incident_caused_by_change` | `tbl_all_incidents` → `tbl_change` **via** `tbl_incident_change_relation`, `const type='Caused By Change'` | 21,446 / 21,446 on **both** sides (100%) | 1:N — up to 4 changes per incident (avg 1.081) |
| `major_incident_caused_by_change` | `tbl_major_incidents` → `tbl_change` via the same bridge + const | same bridge | 1:N |
| `change_company` | `tbl_change` → `tbl_change_company` on `change_system_id` | 110,750 / 110,750 (100%) | 1:1 satellite |

The `const` is **required, not decorative**: the bridge's `type` column holds
`Caused By Change` (17,249) and `Unknown` (4,197). Omitting it overstates
change-caused incidents by ~24%.

`slm.exception_details` / `slm.exception_info` are absent from the DB — the schema's
five `inner_join` hints on them cannot be declared. (Those hints also contain a typo,
`status_ide`, and point at two tables that don't exist in the schema either.)

### 3.3 Entity `problem`

| Edge | Join | Match | Cardinality |
|---|---|---|---|
| `incident_has_problem` | `tbl_all_incidents` → `tbl_problem` via `tbl_incident_problem_relation`, `const type='CAUSED BY'` | 9,592 / 9,592 both sides (100%) | 1:1 — max 1 problem per incident |
| `problem_has_tasks` | `tbl_problem` → `tbl_problem_tsk_dim` on `problem_system_id` | 19,261 / 19,261 (100%) | 1:N — avg 2.008, max 3 |
| `problem_task_derivative` | `tbl_problem_tsk_dim` → `tbl_sn_prblm_derivative` on `problem_task_system_id` | 19,011 matched | 1:N — **partial, see note** |

The bridge's `type` holds only `CAUSED BY`, so its `const` is documentation rather than a
filter — but declaring it keeps the edge honest if a second type ever appears.

`tbl_sn_prblm_derivative` is a wide xref (86,104 rows) whose only usable keys are
`problem_system_id` (19,011 / 86,104 = 22%) and `problem_task_system_id` (19,011). Its
`change_system_id` resolves to nothing (§2.2). Declared for the task link only, and
flagged partial — 78% of its rows point outside the loaded data.

### 3.4 Entity `availability`

| Edge | Join | Match | Cardinality |
|---|---|---|---|
| `major_incident_sla_detail` | `tbl_major_incidents` → `tbl_tableau_major_incdnt` on `incident_id` | 1,653 / 1,653 (100%) | **1:1 — clean** |
| `incident_has_outage` | `tbl_all_incidents` → `tbl_tableau_outagesv4` on `incident_system_id` | 311,220 / 311,220 (100%) | **1:N — exactly 2 per incident** |

Anchor `major_incident_sla_detail` on `tbl_major_incidents`, **not** `tbl_all_incidents`:
against the latter it matches only 1,182 / 1,653 (§2.3).

`tbl_tableau_outagesv4` holds 311,220 rows / 311,220 distinct `outage_system_id` /
155,610 distinct incidents — two genuine outage rows per incident, not duplicates.

### 3.5 Entity `service_request`

| Edge | Join | Match | Cardinality |
|---|---|---|---|
| `request_item_sector` | `tbl_request_item` → `tbl_request_item_sector` on `request_item_system_id` | 52,250 / 52,250 (100%) | 1:1 satellite |

The catalog tables (`tbl_sc_cat_item`, `tbl_sc_req_item`) are absent from the DB, so the
`cat_item` → catalog-item edge cannot be declared or verified.
`tbl_request_item.first_task_id` holds `SCTASK…` ids pointing at
`itsm_servicerequest.tbl_request_task`, which is **missing from the schema** — worth
adding, since production KPI SQL already joins it.

### 3.6 Entity `release` — declare, but both edges fan out

| Edge | Join | Match | Cardinality |
|---|---|---|---|
| `release_causes_change` | `tbl_pepops_release_mgmt` → `tbl_change` on `change_id` | 10,816 / 62,107 (**17%**) | N:M |
| `release_company` | → `tbl_pepops_release_mgmt_company` on `release_system_id` | 62,000 / 62,000 (100%) | **N:M — not a satellite** |

Two structural problems, both worth knowing before relying on these:

- `tbl_pepops_release_mgmt.release_system_id` is declared `primary_key` but is **not
  unique**: 62,000 rows / 51,800 distinct values, up to 52 rows per value.
  `tbl_pepops_release_mgmt_company` has the identical shape, so joining them on
  `release_system_id` fans out to 592,400 rows.
- `tbl_change.change_id` is **also not unique** (110,750 rows / 110,079 distinct).
  Joining releases to changes on `change_id` can multiply rows on both sides.

The direct `release.incident_id → tbl_all_incidents` edge is **rejected**: 698 / 62,000
= 1.1% match. The `rm-release-causing-major` KPI already takes the correct route
(release → change → bridge → incident); that path falls out of the edges above for free.

---

## 4. Rejected candidates, with evidence

| Candidate | Why rejected |
|---|---|
| SLA family joined on `incident_sla_system_id` | 0 rows matched in all 3 pairings (§2.1) |
| `tbl_major_incidents.problem_system_id → tbl_problem` | 0 / 1,653 matched |
| `tbl_sn_prblm_derivative.change_system_id → tbl_change` | 0 / 86,104 matched |
| `tbl_pepops_release_mgmt.incident_id → tbl_all_incidents` | 698 / 62,000 (1.1%) — use the bridge path |
| `automation.tbl_automation_change.change_system_id → tbl_change` | table is empty (0 rows); structurally plausible, unverifiable |
| `automation.tbl_automation_end_to_end.ticket_id → incident/change/request` | polymorphic on `ticket_type`; table empty **and** format cannot express it (§5) |
| `automation.automation_target` → anything | no row-level key; grain is `bundle_name` × `month` |
| all `itsm_ansible.*` edges | all three tables absent from the DB; no ITSM ticket key on any of them |
| all `slm.*` edges | both tables absent from the DB |

---

## 5. Two limits of the current relationship format

Both are worth deciding on before this block is applied.

**5.1 — `const` is ignored on direct (non-`via`) edges.**
`relationships._edge_clauses` attaches `const` only to the `via` clause; the `else`
branch that handles `join_on` never reads it. So a polymorphic FK whose discriminator
sits on the base table — exactly `tbl_automation_end_to_end.ticket_type` — cannot be
declared. None of the 14 recommended edges needs this today, but the automation entity
will when it has data.

**5.2 — Cardinality is not modelled.**
`plan_join` emits the same JOIN for a 1:1 satellite and for a 1:N child. Eight of the 15
edges are not 1:1 (six 1:N, two N:M), and three of those (`incident_has_sla_clocks`,
`incident_has_outage`, `release_company`) multiply rows by 2, 2 and up to 52.
`count(distinct <grain_key>)` is
already the composer's default measure, which contains the damage — but a `SUM` over a
1:N join would silently over-report.

The graph loader preserves unknown keys and `list_relationships` returns them, so the
`cardinality` and `notes` fields in the block below reach the agent as guidance even
though `plan_join` does not act on them. Making the planner *enforce* cardinality
(prefer `EXISTS` over `JOIN` for 1:N) is a separate change.

**5.3 — Minor:** two edges sharing one `via` table could emit a duplicate JOIN for that
table if a single query traverses both (`plan_join` guards `to`, not `via`). Only
reachable if one query anchors on `tbl_all_incidents` *and* `tbl_major_incidents`, which
the composer will not do — but `incident_caused_by_change` and
`major_incident_caused_by_change` share `tbl_incident_change_relation`, so it is worth a
guard.

---

## 6. The block to apply

Goes under `modules[0]` (`itsm`), immediately before `entities:`. Note `join_on` rather
than `on` — bare `on:` is parsed as boolean `true` under YAML 1.1.

```yaml
  relationships:
  # ---- incident core -----------------------------------------------------
  - name: incident_has_sla_clocks
    description: Incidents to their SLA clock rows (both response and resolution).
    left: itsm_incident.tbl_all_incidents
    right: itsm_incident.tbl_incident_sla
    join_on: {left_col: incident_system_id, right_col: incident_system_id}
    cardinality: one_to_many
    notes: Exactly 2 rows per incident (one per clock). Verified 311220/311220 match.
      Do NOT join on incident_sla_system_id - it is table-local and matches 0 rows.
  - name: incident_sla_response
    description: Incidents to their SLA response clock.
    left: itsm_incident.tbl_all_incidents
    right: itsm_incident.tbl_sla_response
    join_on: {left_col: incident_system_id, right_col: incident_system_id}
    cardinality: one_to_one
    notes: Verified 155610/155610 match.
  - name: incident_sla_resolution
    description: Incidents to their SLA resolution clock.
    left: itsm_incident.tbl_all_incidents
    right: itsm_incident.tbl_sla_resolution
    join_on: {left_col: incident_system_id, right_col: incident_system_id}
    cardinality: one_to_one
    notes: Verified 135923/135923 match; present only for resolved incidents.
  - name: incident_company
    description: Incidents to their sector/business mapping row.
    left: itsm_incident.tbl_all_incidents
    right: itsm.tbl_incident_company
    join_on: {left_col: incident_system_id, right_col: incident_system_id}
    cardinality: one_to_one
    notes: Verified 155610/155610 match.

  # ---- incident <-> change ----------------------------------------------
  - name: incident_caused_by_change
    description: Incidents linked to the change that caused them.
    left: itsm_incident.tbl_all_incidents
    right: itsm_change.tbl_change
    via: itsm.tbl_incident_change_relation
    left_on: {left_col: incident_system_id, via_col: incident_system_id}
    right_on: {right_col: change_system_id, via_col: change_system_id}
    const: {col: type, value: Caused By Change}
    cardinality: one_to_many
    notes: Bridge verified 21446/21446 on both sides; up to 4 changes per incident.
      The const is REQUIRED - type holds 'Caused By Change' (17249) and 'Unknown' (4197).
  - name: major_incident_caused_by_change
    description: Major incidents linked to the change that caused them.
    left: itsm_incident.tbl_major_incidents
    right: itsm_change.tbl_change
    via: itsm.tbl_incident_change_relation
    left_on: {left_col: incident_system_id, via_col: incident_system_id}
    right_on: {right_col: change_system_id, via_col: change_system_id}
    const: {col: type, value: Caused By Change}
    cardinality: one_to_many
    notes: Declared separately from incident_caused_by_change so a query anchored on
      tbl_major_incidents gets a one-hop path. Routing via tbl_all_incidents would
      lose the 471 major incidents absent from that table.
  - name: change_company
    description: Changes to their sector/business mapping row.
    left: itsm_change.tbl_change
    right: itsm.tbl_change_company
    join_on: {left_col: change_system_id, right_col: change_system_id}
    cardinality: one_to_one
    notes: Verified 110750/110750 match.

  # ---- incident <-> problem ---------------------------------------------
  - name: incident_has_problem
    description: Incidents linked to the problem that caused them.
    left: itsm_incident.tbl_all_incidents
    right: itsm_problem.tbl_problem
    via: itsm.tbl_incident_problem_relation
    left_on: {left_col: incident_system_id, via_col: incident_system_id}
    right_on: {right_col: problem_system_id, via_col: problem_system_id}
    const: {col: type, value: CAUSED BY}
    cardinality: one_to_one
    notes: Bridge verified 9592/9592 on both sides; max 1 problem per incident.
      type currently holds only 'CAUSED BY'.
  - name: problem_has_tasks
    description: Problems to their problem-task rows.
    left: itsm_problem.tbl_problem
    right: itsm_problem.tbl_problem_tsk_dim
    join_on: {left_col: problem_system_id, right_col: problem_system_id}
    cardinality: one_to_many
    notes: Verified 19261/19261 match; avg 2.008 tasks per problem, max 3.
  - name: problem_task_derivative
    description: Problem tasks to their derivative xref rows.
    left: itsm_problem.tbl_problem_tsk_dim
    right: itsm_problem.tbl_sn_prblm_derivative
    join_on: {left_col: problem_task_system_id, right_col: problem_task_system_id}
    cardinality: one_to_many
    notes: PARTIAL - 19011 of 86104 derivative rows resolve; the rest reference records
      not loaded here. Do NOT use this table's change_system_id (0/86104 match).

  # ---- availability ------------------------------------------------------
  - name: major_incident_sla_detail
    description: Major incidents to their availability/SLA detail row.
    left: itsm_incident.tbl_major_incidents
    right: itsm_availability.tbl_tableau_major_incdnt
    join_on: {left_col: incident_id, right_col: incident_id}
    cardinality: one_to_one
    notes: Verified 1653/1653 match. Anchored on tbl_major_incidents deliberately -
      against tbl_all_incidents it matches only 1182/1653.
  - name: incident_has_outage
    description: Incidents to their outage records.
    left: itsm_incident.tbl_all_incidents
    right: itsm_availability.tbl_tableau_outagesv4
    join_on: {left_col: incident_system_id, right_col: incident_system_id}
    cardinality: one_to_many
    notes: Verified 311220/311220 match; exactly 2 distinct outage rows per incident.

  # ---- service request ---------------------------------------------------
  - name: request_item_sector
    description: Request items to their sector/business mapping row.
    left: itsm_servicerequest.tbl_request_item
    right: itsm_servicerequest.tbl_request_item_sector
    join_on: {left_col: request_item_system_id, right_col: request_item_system_id}
    cardinality: one_to_one
    notes: Verified 52250/52250 match.

  # ---- release -----------------------------------------------------------
  - name: release_causes_change
    description: Releases joined to their changes by change_id.
    left: itsm_release.tbl_pepops_release_mgmt
    right: itsm_change.tbl_change
    join_on: {left_col: change_id, right_col: change_id}
    cardinality: many_to_many
    notes: SPARSE - only 10816/62107 release rows resolve to a change. Neither side is
      unique on the join key (release_system_id 62000 rows/51800 distinct;
      tbl_change.change_id 110750/110079), so this join fans out. Reach incidents from
      a release via this edge then incident_caused_by_change - the direct
      release.incident_id link matches only 698/62000 and is deliberately not declared.
  - name: release_company
    description: Releases to their sector/business mapping rows.
    left: itsm_release.tbl_pepops_release_mgmt
    right: itsm.tbl_pepops_release_mgmt_company
    join_on: {left_col: release_system_id, right_col: release_system_id}
    cardinality: many_to_many
    notes: 100% match but NOT a 1:1 satellite - release_system_id is non-unique on both
      sides, so this join expands 62000 rows to 592400. Aggregate with count(distinct).
```

---

## 7. Effect on the original question

*"How many major incidents were created due to emergency changes during the last 6 months"*

With `major_incident_caused_by_change` declared, this becomes one planned join —
`tbl_major_incidents` → bridge (`type='Caused By Change'`) → `tbl_change`
(`type_description='EMERGENCY'`).

**Validated end to end.** The block was injected into a temp copy of the schema and the
question run through the real `run_dataset_query` path. Generated SQL:

```sql
SELECT count(distinct a.incident_id) AS v
FROM itsm_incident.tbl_major_incidents a
INNER JOIN itsm.tbl_incident_change_relation b
        ON a.incident_system_id = b.incident_system_id AND b.type = 'Caused By Change'
INNER JOIN itsm_change.tbl_change c
        ON b.change_system_id = c.change_system_id
WHERE lower(c.type_description) = 'emergency'
  AND cast(a.open_date_time as timestamp) BETWEEN '2026-01-01' AND '2026-06-30'
```

Results, with `last 6 months` resolving to the six complete calendar months
2026-01-01 … 2026-06-30:

```
major incidents caused by an EMERGENCY change : 11
  ... by EXPEDITE                             :  7
  ... by NORMAL                               :  1
major incidents with any Caused-By-Change link (all time) : 110
```

The all-time figure matches a hand-written query exactly (110 = 110), confirming the
planned join is identical to the one `cm-major-incident` already hand-codes. A rolling
`now() - interval '6 months'` window returns 10 rather than 11 — that difference is the
period definition, not the join.

Both anchors agree, so the 471-row gap in §2.3 does not distort *this* question — every
change-linked major incident happens to sit in both tables. That is incidental, not
invariant, which is why the second anchor is still declared.

---

## 8. Suggested order of work

1. Apply the §6 block (`tools/harvest_relationships.py` is the right home — its
   `CURATED` dict already holds an earlier 4-edge version of this, and re-running it is
   how the block survives the next schema re-source).
2. Fix the two malformed hints found during this review: `schema_v3.yaml:1701`
   (`tbl_incident_change_relation` hint equates `change_system_id` to
   `incident_system_id`) and the `tbl_problem_tsk_dim` hint referencing a non-existent
   `problem.problem`. Both are wrong in ways that would propagate if anyone generated
   edges from them.
3. Decide on §5.1 / §5.2 before the automation entity gets data.
4. Separately from relationships: the `major_incident` predicate's `tbl_all_incidents`
   binding undercounts by 471 (§2.3). Worth a note in `predicates.json` or dropping
   that binding in favour of the primary table.