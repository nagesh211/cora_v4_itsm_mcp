# Dynamic Multi-Module Service — Working Plan

**Status:** Draft for review · **Branch:** `feature/opensearch-intigration`
**Goal:** Make the service work for **any set of modules** — add / change / remove a
module (with its schema + KPI configs) and have **every** process work on it
automatically: not just `overview_module`, but KPI metrics, ad-hoc / cross-entity
queries, record detail, module routing, filters, values and dates. No code edit per
module. End state: **schemas live in OpenSearch, configs live in OpenSearch**, and the
runtime derives everything module-specific from those two sources.

> This plan is about **module dynamism**. The companion `OPENSEARCH_CONFIG_PLAN.md`
> already moved *KPI configs* into OpenSearch. This document covers everything else that
> still assumes the fixed 8 ITSM modules.

---

## 1. The core principle

There must be **one source of truth per fact**, and module-specific facts must be
**derived, never hardcoded**:

| Fact | Where it should come from |
|---|---|
| What modules exist, their code + human label | **Schema** (`modules[].name`, `.code`, `.description`) |
| A module's entities / tables / columns / roles | **Schema** |
| Relationships (join paths) | **Schema** (`modules[].relationships`) |
| Which KPIs belong to a module | **KPI config** `module` field (already in OpenSearch) |
| Module routing vocabulary (aliases/synonyms) | **Derived** from schema + config `nl.synonyms` |
| Record-id prefixes (INC→incident) | **Config-driven** registry (per module) |
| Filter / value aliases | **Config-driven** registry (per module or global) |

Today three of these are **hardcoded to the 8 ITSM modules** or **cached from disk with
no reload**. Those are the whole job.

---

## 2. Current state — what is already dynamic vs. coupled

### ✅ Already module-agnostic (adds a new module for free)
| Component | Why it's fine |
|---|---|
| `sql_builder.py` | Validates every table/column against the schema loader; no module names baked in. |
| `KpiCatalog` (`kpi_catalog.py`) | OpenSearch, **stateless** — a KPI for a new module is live on next request; `by_module`/`names`/`configs_for_tables` are term queries. |
| `record_lookup.curated_detail_columns` | Column set derived from schema **roles** — tracks new columns/entities automatically. |
| `relationships.py` (`get_graph`) | Built from each module's `relationships:` block in the schema. |
| `schema_loader` entity/column/table lookups | Generic over `modules[].entities[].tables[].columns`. |
| Dimension / filter **schema fallback** (`query_engine.resolve_dim_via_schema`, `_augment_fields`) | Any real column on a KPI's primary table is groupable/filterable even if the config didn't declare it. |

### ⚠️ Coupled — must change for true dynamism

| # | Location | Coupling | Impact when a new module is added |
|---|---|---|---|
| **C1** | `query_engine.py:549-579` — `_MODULE_LABELS`, `_MODULE_SYNONYMS`, `resolve_module_code` | **Hardcoded** map of the 8 codes + phrases | `overview_module("<new module>")` → `unknown module`. `plan_query(module=...)` can't resolve it. **This is the #1 blocker.** |
| **C2** | `tools/build_module_catalog.py:38-69` — `MODULE_DEFAULTS` | Seed aliases only for the 8 | New module gets code-as-name + **no routing vocabulary** → weak `detect_module`, KPIs harder to find. Not fatal (falls back to unfiltered search). |
| **C3** | `clients/web_ui.py` analyst/summarizer prompts | Hardcodes `am/cm/em/im/pm/rm/sd/sr`, example slugs (`itsm_incident…`), example columns (`open_date_time`) | LLM is biased toward ITSM; a new module's codes/entities are never suggested. Degrades tool selection, not correctness. |
| **C4** | `clients/autogen_client.py`, `tools.py` docstrings | Same hardcoded code list in tool docs | Same as C3 (LLM-facing hints). |
| **C5** | Cached singletons: `schema_loader.get_loader`, `module_router._phrases_by_module`, `record_lookup.registry`, `filter_aliases.get_registry` (all `@lru_cache(maxsize=1)`) | Loaded **once per process** from disk | A module/schema change is **invisible until restart** — the opposite of "dynamic". |
| **C6** | `schema_v3.yaml`, `module_catalog.json`, `record_prefixes.json`, `filter_aliases.json`, `value_aliases.json` | **On disk**, not in OpenSearch | New module needs a file edit + redeploy; contradicts "schemas will come from OpenSearch". |

---

## 3. Per-process coverage — does each process pick up a new module today?

For a brand-new module `xx` with schema entities + KPI configs indexed:

| Process | Tool | Works automatically? | Blocker |
|---|---|---|---|
| KPI metric (value) | `run_kpi` / `generate_query` | ✅ Yes | — (config-driven) |
| KPI SQL preview | `generate_query` | ✅ Yes | — |
| KPI search / selection | `search_kpis` | ⚠️ Mostly | routing vocab missing (C2) → still works unfiltered |
| **Module overview** | `overview_module` | ❌ **No** | `resolve_module_code` (C1) |
| Ad-hoc / flexible | `query_dataset` | ✅ Yes | schema-validated |
| Cross-entity join | `query_dataset` + `join_with` | ✅ Yes* | needs schema `relationships:` for the new module |
| Drill-down / reason | `query_dataset` drilldown | ✅ Yes | — |
| Record detail | `get_record` | ⚠️ Partial | needs a `record_prefixes.json` entry (C6) for the new id prefix |
| Module discovery | `list_modules` / `describe_module` / `describe_dataset` | ✅ Yes | schema-driven (once schema reload is solved, C5) |
| Module routing | `detect_module` | ⚠️ Weak | vocab missing (C2) |
| Filters (aliases) | all | ⚠️ Partial | new domain words need alias entries (C6); real columns still work via schema fallback |
| Values ('closed'→CLOSED) | all | ✅ Yes | schema `possible_values` + value_resolver |
| Dates | all | ✅ Yes | fully generic |

**Bottom line:** the *engine* is already ~80% module-agnostic. The gaps are **(C1) module
code resolution, (C2) routing vocabulary, (C5) cache reload, (C6) data-in-OpenSearch, and
(C3/C4) prompt hints.** Fix those five and any module works across all processes.

---

## 4. Work plan (phased)

### Phase 1 — Kill the hardcoded module registry (fixes C1, biggest win)
Derive module code ↔ label ↔ synonyms **from the schema**, which already has
`modules[].name`, `.code`, `.description`, `.entities`.

- Add to `schema_loader`:
  - `module_codes() -> {code: label}` built from `m["code"]`/`m["name"]`.
  - `resolve_module_code(text)` — match a code, a full name, or an entity/alias phrase,
    all sourced from the schema (+ optional per-module `aliases:` block in the schema).
- Rewrite `query_engine.resolve_module_code` / `_MODULE_LABELS` / `_MODULE_SYNONYMS` to
  **delegate** to the schema loader. Delete the hardcoded dicts.
- `module_overview` then accepts any module the schema declares.
- **Acceptance:** add a module to the schema → `overview_module("<its name or code>")`
  runs its KPIs with zero code change.

### Phase 2 — Routing vocabulary from data (fixes C2)
- `detect_module` already reads `module_catalog.json`. Make the catalog **fully
  generated**: seed a new module's aliases from `schema.module.name` + entity names +
  the module's KPI `nl.synonyms` (already done for metrics) instead of `MODULE_DEFAULTS`.
- Keep `MODULE_DEFAULTS` only as an *optional* hand-tuning overlay, not a requirement.
- Longer term: fold the catalog into the schema (a `modules[].aliases:` block) or a
  small OpenSearch index, so it's regenerated on index, not by a manual script.

### Phase 3 — Reloadable / OpenSearch-backed schema & sidecars (fixes C5, C6)
This is the "schemas will be there" step.

- **Schema source abstraction.** Give `SchemaLoader` a backend seam like `KpiCatalog`
  has: `FileSchemaBackend` (today's `schema_v3.yaml`) and `OpenSearchSchemaBackend`
  (a `cora-schema` index, one doc per module). Selected by env
  (`CORA_SCHEMA_BACKEND=files|opensearch`).
- **Cache invalidation.** Replace the bare `@lru_cache(maxsize=1)` singletons with a
  versioned/TTL cache (or an explicit `reload()` + a lightweight "schema version" doc
  polled every N seconds / bumped on write). Options, cheapest→richest:
  1. Short TTL (e.g. 60s) re-fetch of a tiny "modules manifest" doc; reload only on
     version change. *(Recommended — bounded staleness, near-zero cost.)*
  2. Explicit admin endpoint / MCP tool `reload_schema` after an index write.
  3. Fully stateless per-request schema reads (like `KpiCatalog`) — simplest mental
     model, more OpenSearch calls; fine given schema is small.
- **Move sidecars** (`record_prefixes`, `filter_aliases`, `value_aliases`,
  `module_catalog`) into OpenSearch docs or, better, **into the schema itself** as
  per-module blocks (`aliases:`, `record_prefixes:`, `value_aliases:`). One write path,
  one source of truth, one reload.

### Phase 4 — Make the prompts module-agnostic (fixes C3, C4)
- Replace the hardcoded `am/cm/em/…` and example slugs in `web_ui.py` /
  `autogen_client.py` / `tools.py` docstrings with a **runtime-injected module list**:
  call `list_modules` and interpolate the codes/labels/example entities into the system
  prompt at session start. The model then always sees the *current* module set.
- Keep the *behavioral* guidance (when to use which tool) static; only the module
  enumeration becomes dynamic.

### Phase 5 — Record-id prefixes per module (finishes `get_record`)
- Today a new id prefix (e.g. `RLSE…`) needs a `record_prefixes.json` line.
- Make the registry reloadable (Phase 3) and ideally **derive** prefixes from the
  schema: a per-entity `record_prefix:` + `human_id_column:` hint, so a new module's
  records resolve without a separate file.

---

## 5. Lifecycle — "what happens when I change / remove / add a module"

| Scenario | What breaks today | Behaviour after the plan |
|---|---|---|
| **Add a module** (schema + configs) | `overview_module` rejects it; routing weak; records need a prefix line | Fully live after schema reindex + config index — all processes work. |
| **Rename a module code** (`sd`→`svd`) | `_MODULE_LABELS` wrong; every KPI `module:"sd"` orphaned; routing stale | Schema is the source; **but** KPI `module` fields must be re-indexed to the new code. Add a **consistency check** (Phase 6). |
| **Remove a module** | Cached schema still serves it until restart; KPIs pointing at removed tables error at build; relationships referencing removed tables dangle | Reload drops it; `configs_for_tables`/`by_module` return empty; add a **reindex sweep** that flags orphaned KPIs. |
| **Split / merge modules** | Entities move between modules; slugs (`_slug(module,entity)`) change → breaks record `entity` slugs, `by_module`, relationships keyed by table fqn | Table fqns are stable, so `sql_builder`/relationships survive; **entity slugs change** → any stored slug references (record aliases) must be re-derived. Document slug = `module_entity`; avoid persisting slugs. |
| **Two modules share a physical table** | `_by_table.setdefault` keeps first module; `_primary_table_to_slug` collision handling exists but prefers schema-prefix match | Already handled defensively; add a test. |
| **Module with no date column** | KPIs with `period`/`grain` error ("no time column") | Already surfaced as a clear error; schema `is_date_applicable` should gate date-dependent prompts. |

### Cross-cutting invariants to enforce (new **Phase 6 — consistency guard**)
A `validate_catalog` tool / CI check that, on every reindex, asserts:
1. Every KPI `module` code exists in the schema.
2. Every KPI `primary_dataset` table exists in the schema.
3. Every KPI `fields[].column` exists on its table.
4. Every schema `relationships[].left/right/via` table exists.
5. Every `record_prefixes` entity slug exists.
6. Every filter-alias canonical key is a real column somewhere.
Fail the index (or warn loudly) on violation — this is what keeps a *dynamic* catalog
from silently drifting into broken queries when modules change.

---

## 6. Concrete change list (files)

| File | Change | Phase |
|---|---|---|
| `cora_mcp/schema_loader.py` | Add `module_codes()`, schema-driven `resolve_module_code()`; add backend seam + reload | 1, 3 |
| `cora_mcp/query_engine.py` | Delete `_MODULE_LABELS`/`_MODULE_SYNONYMS`; delegate `resolve_module_code` to schema | 1 |
| `cora_mcp/module_router.py` | Seed vocab from schema, not `MODULE_DEFAULTS`; reloadable | 2, 3 |
| `tools/build_module_catalog.py` | Generate aliases from schema entities + KPI synonyms | 2 |
| `cora_mcp/record_lookup.py` | Reloadable registry; optional schema-derived prefixes | 3, 5 |
| `cora_mcp/filter_aliases.py` | Reloadable; optional OpenSearch/schema source | 3 |
| `cora_mcp/opensearch_client.py` | Add `cora-schema` index mapping + client reuse | 3 |
| `tools/index_schema.py` *(new)* | Indexer for schema docs (mirrors `index_configs.py`) | 3 |
| `tools/validate_catalog.py` *(new)* | Consistency guard (Phase 6) | 6 |
| `clients/web_ui.py`, `clients/autogen_client.py` | Inject live module list into prompts | 4 |

---

## 7. Recommended sequencing & effort

1. **Phase 1** (schema-driven module codes) — *small, high value, no infra.* Unblocks
   `overview_module` for new modules immediately. **Do first.**
2. **Phase 6** (consistency guard) — *small.* Cheap insurance before things get dynamic.
3. **Phase 2** (routing vocab) — *small.* Better search for new modules.
4. **Phase 4** (module-agnostic prompts) — *small.*
5. **Phase 3** (OpenSearch schema + reload) — *largest.* The real "schemas come from
   OpenSearch" step; do once Phases 1–2 prove the schema-as-truth model.
6. **Phase 5** (schema-derived record prefixes) — *small, optional.*

Phases 1, 2, 4, 6 are all achievable without moving schema off disk — they make the
service module-agnostic **now**. Phase 3 is the infra investment for the OpenSearch
end-state.

---

## 8. Open questions for you

1. **Schema in OpenSearch**: one doc per module, or one big schema doc? (Per-module is
   friendlier for add/remove and reindex.)
2. **Reload model** (Phase 3): bounded-TTL manifest poll *(my recommendation)*, explicit
   `reload` tool, or fully stateless per-request reads?
3. **Sidecars** (aliases, prefixes, value maps): fold into the schema per-module, or keep
   as separate small OpenSearch indices?
4. **Module code stability**: can we guarantee codes never change once assigned? (If yes,
   the rename scenario mostly disappears.)
5. **Non-ITSM modules**: will future modules keep the same shape (`modules[].entities[].
   tables[].columns[]` with the same `role` vocabulary)? The whole plan assumes that
   contract holds — if a new domain has a different schema shape, `schema_loader` needs a
   normalization layer.
6. **Cross-module relationships**: will joins ever span two *different* modules? Today
   relationships are declared per-module; a cross-module join needs the edge declared in
   one of them (table fqns are global, so it works — just confirm the authoring convention).

---

*Nothing in this plan changes SQL building, date math, value resolution, or the MCP tool
surface. It only removes the assumption that the module set is the fixed ITSM eight.*