# Question Catalog — what users can actually ask

**Status:** Derived from `config/*.json` (66 KPIs) + `schema_v3.yaml` · **Branch:** `feature/pep-poc`
**Purpose:** The grounded list of question shapes this deployment can answer, tiered by
whether they work **today**. Doubles as a regression corpus — see `MULTI_METRIC_ANALYSIS.md`
for the scenario IDs (S1–S12) each tier maps to.

> Every metric name and dimension below is copied from the real catalog or schema.
>
> ## ⚠️ Correction — values were re-measured against the database
>
> This catalog originally took its filter values from `schema_v3.yaml`'s
> `possible_values`. Those were then measured against the live DB and found **broadly
> wrong** (`MULTI_METRIC_ANALYSIS.md` §0.4). Values below are now marked:
>
> * **✅ verified** — confirmed present in the database
> * **❌ fictional** — declared in the schema, absent from the data; a question using it
>   returns zero rows or is rejected
>
> The Tier B tables have been corrected accordingly. Where a real domain replaced a
> fictional one, the real values are shown.

---

## 0. The shared vocabulary

**Sector (`sector` / "business" / "p&l")** — from `pepops_bkp/changes-total-volume-by-sector.json`:
`AMESA, APAC, CGF, EUROPE, FLNA, LATAM, PBNA, PGCS, QFNA, SODASTREAM`

> ⚠️ `schema_v3.yaml` declares `tbl_incident_company.combined_business_name` as
> `['AMESA','APAC','CGF','EUROPE','LATAM','NORTH AMERICA','PGCS','SODASTREAM']` — **missing
> PBNA, FLNA, QFNA**, and carrying `NORTH AMERICA` instead. `business_name` on the incident
> tables declares *no* domain, so `PBNA` passes through unvalidated and works. But if anyone
> ever copies that stale list onto `business_name`, **every PBNA question starts being
> rejected** by `value_resolver`. Fix the domain before adding one.

**Universal filters** (accepted by nearly every KPI — count = configs allowing it):

| Filter key | Accepted aliases | Configs |
|---|---|---|
| `assignment_group` | team, owner group, responsible group | 58 |
| `service_area` | capability | 57 |
| `sector` | business, p&l | 52 |
| `division` | sub_sector, sub_business, sub_p&l | 49 |
| `region` | region | 49 |
| `category` | classification | 47 |
| `priority` | — | 19 |
| `risk` | risk type | 11 |
| `type` | category type | 9 |
| `vendor`, `source`, `service`, `country`, `request_type`, `methodology`, `contact_type` | (canonical only) | 2–6 |

**Universal breakdown dimensions:** `business_name`, `sub_business_name`, `region_name`,
`service_area`, `assignment_group_name`, `category_description`.

**Time phrases** (all deterministic, `date_resolver`): `today`, `yesterday`, `last 7 days`,
`this month`, `last month`, `MTD`, `QTD`, `YTD`, `PYTD`, `last quarter`, `Q3 2025`, `H1 2025`,
`FY2024`, `Aug 2025`, `between 2025-06-10 and 2025-08-15`, `last 3 months`,
plus two-sided comparisons: `last quarter vs current quarter`, `this month compared to last month`.

---

## 1. Tier A — works today: one metric, declared filters/dimensions (S1, S2, S12)

The core traffic. `search_kpis` → `run_kpi`. 66 metrics × these shapes.

### A1 · Point value
- What is our SLA Resolve % this month?
- How many major incidents were opened last quarter?
- What is the Change Failure Rate % for PBNA year-to-date?
- What is Mean Time to Recover (MTTR) in hours for the CGF sector this quarter?
- What is our Net Promoter Score this month?
- What is the Call Abandonment Rate for the service desk last month?
- How many problems are currently open?
- What is the SLA At-Risk Count right now?
- What is Overall availability percentage for EUROPE this quarter?
- What is the Release Success Rate % year-to-date?
- What is Mean Time to Fulfil (days) for service requests last month?
- How many RITMs were opened last month?

### A2 · Trend over time (`mode='series'`, grain auto-derived)
- Show the trend in major incident count over the last 6 months.
- How has SLA Resolve % trended this year?
- Trend of Change Failure Rate % over the last 4 quarters.
- Show Call Volume week by week for the last month.
- How has Avg Resolution Time (hrs) moved over the last 3 quarters?
- Trend of Emergency Change % for LATAM this year.
- Show the Critical Alerts MTTR trend over the last 6 months.
- How has Net Promoter Score trended since the start of the fiscal year?

### A3 · Breakdown by one dimension (`mode='table'` + `dim`)
- Major incident count by sector last quarter.
- SLA Resolve % by assignment group this month.
- Change Failure Rate % by service area year-to-date.
- Total Incidents Opened by region last month.
- Problem count by category this quarter.
- Release Success Rate % by division this year.
- Call Volume by contact type last month.
- Avg Business-Hours Duration by assignment group this quarter.

### A4 · Breakdown by several dimensions (`dim` as a list)
- Major incidents by region and priority last quarter.
- SLA Resolve % by sector and assignment group this month.
- Change volume by division and service area year-to-date.
- Incidents opened by region and category last month.

### A5 · Trend broken down by a dimension (`mode='series'` + `dim`)
- Major incident trend by sector over the last 6 months.
- SLA Resolve % trend by region this year.
- NPS trend by business over the last 4 quarters.
- Change Failure Rate % by type over the last 6 months.

### A6 · Period-over-period comparison (one call, whole phrase as `period`)
- Major incidents last quarter vs current quarter.
- SLA Resolve % this month compared to last month.
- Change Failure Rate % previous quarter vs current quarter.
- Total Incidents Opened this year vs last year.
- Call Volume last month vs current month for APAC.
- How does MTTR this quarter compare with the same quarter last year?

### A7 · Filtered combinations (several declared filters at once)
- Major incidents for PBNA in the APAC region last quarter.
- SLA Resolve % for the CGF sector, service area X, this month.
- Change Failure Rate % for EUROPE with HIGH risk year-to-date.
- Incidents opened for PBNA division Y, priority P1, last 30 days.
- NPS for business FLNA and region LATAM this quarter.

### A8 · Module overview / rollup (`overview_module`)
- What's happening in incident management this month?
- Give me a service desk overview for APAC last quarter.
- Availability module overview for the CGF sector year-to-date.
- Change management summary for PBNA this quarter.
- Show me the release management overview by sector this year.
- Service request overview broken down by region last month.

### A9 · Target / RAG status (from `status.bands` + `target`)
- Which teams are missing the SLA Resolve target and by how much?
- Are we green on SLA Resolve % this month?
- Which KPIs in the availability module are red this quarter?
- Which sectors are below the 95% SLA resolve target?

---

## 2. Tier B — works today via the schema fallback: qualifiers no config declared (S3)

`resolve_filter_key` falls back to **any dimension column on the metric's primary table**
(`query_engine.py:109`), and `value_resolver` maps the user's word to the declared domain.
**These are the highest-value questions nobody knows they can ask.**

### B1 · Incidents (`tbl_all_incidents`, `tbl_incident_sla`)
| Qualifier column | Real values (measured) | Example question |
|---|---|---|
| `status_name` | ✅ `CLOSED, OPENED, RESOLVED, CANCELED` — ❌ *not* `ASSIGNED/IN QUEUE/NEW/PENDING/IN PROGRESS` | How many incidents are **OPENED** for PBNA? |
| `major_incident_indicator` | ✅ `true / false` (boolean) | How many **major** incidents this quarter? |
| `priority_code` | ✅ `P1…P5` | Major incidents at **P1** by sector last quarter. |
| `active_indicator_type` | ❌ **not a flag at all** — really holds `Application Services, Security & Compliance, Infrastructure Services` | (do not use as "active"; it is a service-line dimension) |
| `sla_target` | ❌ *not* `resolution/response` — really `24 HOURS, 120 HOURS, 8 HOURS, 72 HOURS, 4 HOURS, 2 HOURS, 1 HOUR` | SLA rows with a **4 HOURS** target. |
| `sla_stage` | ✅ `CANCELLED, COMPLETED, IN PROGRESS, PAUSED` | Resolution SLA % where the stage is **COMPLETED**. |

- How many incidents are still OPENED for PBNA this month?
- Total incidents opened at priority P1 and P2, by region, last quarter.
- Avg Resolution Time (hrs) for RESOLVED incidents only, by assignment group.

> ⚠️ "open incidents" must resolve to `OPENED`. `value_aliases.json` maps "open" to
> `NEW/OPEN/ASSESS/IN PROGRESS` — **none of which exist** in this column, so the raw
> filter returns zero. Use the `open_incident` predicate via `compose_metric`, which
> carries the verified value.

### B2 · Changes (`tbl_change`)
| Qualifier column | Real values (measured) | Example question |
|---|---|---|
| `type_description` | ✅ `NORMAL, STANDARD, EMERGENCY, EXPEDITE` (no MODEL/NOT FOUND in data) | Change volume for **EMERGENCY** changes by sector. |
| `risk_description` | ✅ `MODERATE, LOW, HIGH` | Change Failure Rate % for **HIGH** risk changes. |
| `successful_indicator` | ✅ `1` (104788) / `0` (5719) | How many **unsuccessful** changes last month? |
| `status_name` | ✅ `CLOSED, IMPLEMENT, REVIEW, SCHEDULED, AUTHORIZE, ASSESS, NEW, CANCELLED` | Changes still in **IMPLEMENT** by team. |
| `production_system_type` | ❌ *not* `Production/Non Production` — really `REAL-TIME, SCHEDULED, BATCH` | Changes on **BATCH** systems by division. |
| `urgency_description`, `impact_description`, `closure_code_description`, `sap_impact_indicator`, `security_approval_required_indicator` | ⚠️ **unmeasured** — verify before relying on the declared domain | — |

- Change Failure Rate % for HIGH risk changes this quarter.
- How many emergency changes were unsuccessful last month, by assignment group?
- Total change volume for STANDARD changes in EUROPE year-to-date.

Available as predicates via `compose_metric`: `emergency_change`, `expedite_change`,
`normal_change`, `standard_change`, `failed_change`, `high_risk_change`.

### B3 · Problems (`tbl_problem`)
| Qualifier column | Real values (measured) | Example |
|---|---|---|
| `major_problem_indicator` | ✅ `TRUE` (1713) / `FALSE` (7870) — ❌ *not* `YES/NO` | **Major** problem count by sector. |
| `status_name` | ✅ `CLOSED, RESOLVED, ROOT CAUSE ANALYSIS, ASSESS, NEW, FIX IN PROGRESS` | Problems in **ROOT CAUSE ANALYSIS** right now. |
| `source_description` | ❌ *not* only `PROACTIVE` — really `REACTIVE, PROBLEM TASK, USER REPORT, INCIDENT REVIEW, AUTOMATED ALERT` | How many **REACTIVE** problems this quarter? |
| `caused_by_preventive_maintenance`, `user_resolution_codes` | ⚠️ **unmeasured** — verify first | — |

- How many problems are in ROOT CAUSE ANALYSIS for PBNA right now?
- Major problem count by region this year (predicate: `major_problem`).
- Avg Problem Duration (days) for REACTIVE problems only.

### B4 · Releases (`tbl_pepops_release_mgmt`)
| Qualifier column | Declared values | Example |
|---|---|---|
| `release_status` | `GREEN, YELLOW, RED, NOT PROVIDED` | How many releases are **RED** this quarter? |
| `closure_code` | `SUCCESSFUL, UNSUCCESSFUL, SUCCESSFUL WITH ISSUES` | Releases closed **successful with issues**. |
| `methodology` | `AGILE, WATERFALL/TRADITIONAL, NOT PROVIDED` | Release Defect Rate for **AGILE** releases. |
| `type` | `MAJOR, MINOR, NOT PROVIDED` | **Major** release count by sector. |
| `state` | `DRAFT, DEFINE, PLANNING, AWAITING APPROVAL, BUILD & TEST, DEPLOYMENT…` | Releases **awaiting approval** right now. |
| `impact_description` | `GLOBAL / SECTOR, SITE, SITE/DEPARTMENT, USER` | Releases with **GLOBAL / SECTOR** impact. |

- Release Success Rate % for AGILE major releases this year.
- How many releases are RED or YELLOW right now, by delivery group?
- Cancelled Release Rate for waterfall releases last quarter.

### B5 · Service Requests (`tbl_request_item`)
| Qualifier column | Real values (measured) | Example |
|---|---|---|
| `status_name` | ✅ `CLOSED COMPLETE, IN PROGRESS, PENDING, OPEN, CLOSED INCOMPLETE, CANCELLED, PENDING APPROVAL, REJECTED` | How many SRs are **PENDING APPROVAL**? |
| `sla_breached_indicator` | ✅ `TRUE` (9350) / `FALSE` (42850) — ❌ *not* `0/1` | **SLA-breached** service requests by sector |
| `request_type` | ⚠️ unmeasured (`GENERAL, STANDARD, NOT PROVIDED` declared) | RITM count for **STANDARD** requests. |
| `approval`, `fulfillment_automation_level` | ⚠️ on `tbl_sc_req_item` / `tbl_sc_cat_item`, **which do not exist in this DB** | not answerable here |

- How many service requests breached SLA last month, by sector? (predicate: `sr_sla_breached`)
- SR closed-incomplete rate this quarter (predicate: `sr_closed_incomplete`).
- How many RITMs are PENDING APPROVAL right now, by assignment group?

> ⚠️ SR sector/region/division live on `tbl_request_item_sector`, **not** on
> `tbl_request_item` (the schema wrongly declared them there; removed). Breaking SR
> metrics down by sector needs that join, which blocker B1 still prevents.

### B6 · Availability / Major Incidents (`tbl_tableau_major_incdnt`)
| Qualifier column | Real values (measured) |
|---|---|
| `sla_breached_indicator` | ✅ `true` (617) / `false` (1034) — boolean |
| `sla_type` | ✅ `SLA, OLA` |
| `target` | ❌ *not* `RESPONSE/RESOLUTION` — really `8 HOURS, 4 HOURS` |
| `sla_stage_description` | ✅ `IN PROGRESS, PAUSED, COMPLETED` |
| `priority_description` | ✅ `1, 2, 3` (not `P1..P5`) |

> ⚠️ **This table has NO `business_name`, `region_name` or `sub_business_name`** — the
> schema declared them and the database does not have them (`MULTI_METRIC_ANALYSIS.md`
> §0.1). So it cannot answer any sector/region/division question, and the "single-table
> answer" this catalog previously claimed does not exist. Use `compose_metric`, which
> anchors on `tbl_major_incidents` (which does carry sector) and evaluates breach with an
> `EXISTS` test against `tbl_incident_sla`.

---

## 3. Tier C — works today: records and detail listings (S8 partial)

### C1 · One record by id (`get_record`) — always works
- Give me the details of INC0353896.
- Show me CHG0012345 and everything linked to it.
- What happened with PRB0004567?
- Details of RITM0098765 and its parent request.
- Which changes and problems are linked to incident INC0353896?

### C2 · Detail listings (`query_dataset` + `select=[…]`)
- List the incidents opened for PBNA last week with id, priority and assignment group.
- Show me the emergency changes closed last month with risk and closure code.
- List the problems currently in root cause analysis with owner and open date.
- Show me the P1 incidents in the APAC region last month.

---

## 4. Tier D — qualified counts: now answered by `compose_metric`

These combine a measure with one or more **scope predicates**. They used to fail
(`resolve_filter_key` rejected the qualifier, and the join path was unavailable —
`MULTI_METRIC_ANALYSIS.md` §3.2). They are now composed automatically: the anchor table is
chosen by scoring, and any predicate that lives elsewhere becomes an `EXISTS` test.

Call `list_predicates` for the full vocabulary. Currently 21 predicates across incidents,
problems, changes, releases and service requests.

**Incidents**
- How many major incidents for business PBNA breached SLA in the last 3 months?
- How many major incidents breached SLA last quarter, by sector?
- Which assignment groups have the most SLA-breached major incidents this month?
- How many incidents met SLA this month, by region? *(predicate: `sla_met`)*
- How many major incidents are still OPENED for PBNA?

**Changes**
- How many emergency changes failed last quarter, by team? *(`emergency_change` + `failed_change`)*
- High-risk emergency changes this month, by division.
- Standard changes that were unsuccessful, by sector.

**Problems / Releases / SRs**
- Major problem count by region this year.
- Major agile releases that closed successfully, by delivery group.
- Cancelled releases this quarter by methodology.
- Service requests that breached SLA and closed incomplete, last month.

**Still not answerable** (and refused with a reason rather than mis-answered):
- Major incidents breaching **OLA** — `tbl_ola_response` is not declared in the schema.
- Response-SLA vs resolution-SLA split — `sla_target` holds durations, not clock names.
- Any question mixing entities ("emergency changes that caused major incidents") — needs
  blocker B1 (`relationships:`), and is correctly refused as two populations.

---

## 5. Tier E — cannot answer today (and why)

| Question shape | Blocker | Ref |
|---|---|---|
| "Incidents **caused by changes** last quarter" / any cross-entity link | `relationships:` block is empty → `NoJoinPathError` | B1 |
| "Problems linked to major incidents by sector" | same | B1 |
| "How many incidents **breached SLA**" (plain incidents, not major) | breach lives on `tbl_sla_*`, not `tbl_all_incidents`; needs join or a new anchor | B1/B3 |
| "**Count** of SLA breaches" (any module) | only a **%** metric exists, predicate welded into the measure | S7 |
| "Major incident count" (which definition?) | two configs answer it differently, chosen silently by BM25 | S10 |
| "Count of X during the last 3 months" as a single number | prompt routes multi-period windows to `series` → returns buckets | S1b/B5 |
| Anything on `major_incident_indicator` via `query_dataset` | column absent from `schema_v3.yaml` → builder rejects | B2 |

---

## 6. How to use this as a test corpus

Recommended mapping to `tests/`:

| Tier | Assert |
|---|---|
| A1–A9 | `run_kpi` / `overview_module` returns rows, no `error`, no `dropped_filters` |
| B1–B5 | filter resolves via schema fallback; value resolves to the declared domain; **no** `dropped_filters` |
| C1–C2 | `get_record` `found=True`; `select=` returns detail rows with no `GROUP BY` |
| D | **currently asserts the failure** (`QueryError` naming the unknown filter). Flip to success assertions when Phase 2 lands — that flip is the acceptance test. |
| E | asserts a *clear* error, never a silent number |

The Tier D flip is the cleanest definition of "done" for the Phase 2 work in
`MULTI_METRIC_ANALYSIS.md`.