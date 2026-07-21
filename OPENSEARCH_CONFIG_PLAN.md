# OpenSearch-Backed KPI Config Layer — Detailed Plan

**Status:** Implementing (stateless / no-cache design) · **Branch:** `feature/opensearch-intigration`
**Goal:** Move KPI configs out of the deployed codebase into an OpenSearch index so
adding/updating a config needs **no code deploy**, and get **better natural-language
config selection** than today's token-overlap scorer — while keeping SQL building
deterministic.

---

## 0. Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Where OpenSearch sits in the request flow | **Storage + search, stateless serving.** OpenSearch is the source of truth and does config *selection* (live per question). No in-memory cache — the selected config is fetched on demand for SQL building. |
| 2 | Matching strategy for selection | **BM25 keyword first.** Add semantic (kNN hybrid) only later if paraphrase recall is short. |
| 3 | Config surfaces in scope now | **KPI configs only** (`config/*.json`). Filter/value aliases and `schema_v3.yaml` stay on disk. |
| 4 | Serving model | **No RAM cache.** Selection is live per question; `generate_query` fetches the one selected config by id. Disk (`config/*.json`) is the per-call fallback. |

---

## 1. How configs feed query generation today

Four "config" surfaces are loaded from disk and cached in-memory:

| Config | File(s) | Loaded by | Role |
|---|---|---|---|
| **KPI configs** | `config/*.json` (68 files) | `KpiCatalog` (`cora_mcp/kpi_catalog.py`), `lru_cache` | Defines `fields`, DSL/SQL, `filters`, `comparison`, `render`, `nl.synonyms`, `nl.sample_questions` |
| **Filter aliases** | `filter_aliases.json` | `AliasRegistry` (`cora_mcp/filter_aliases.py`) | "team" → `assignment_group`, "business" → `sector` |
| **Value aliases** | `value_aliases.json` | `cora_mcp/value_resolver.py` | "closed" → `["CLOSED","COMPLETE",...]` |
| **Schema** | `schema_v3.yaml`, `record_prefixes.json` | `cora_mcp/schema_loader.py` | Column types, possible values, id prefixes |

**Only the first row (KPI configs) is in scope.**

### Request flow today (`cora_mcp/tools.py`)

```
user question
  → search_kpis(query)          → KpiCatalog.search()   [naive set-overlap scoring]
  → describe_kpi(name)          → KpiCatalog.summary()
  → generate_query/run_kpi(name)→ KpiCatalog.get(name) → build_sql()  [gen_query.py logic]
```

`KpiCatalog.search()` today is `len(set(query_tokens) & set(config_tokens))` with a
couple of boosts — the weakest link and the primary target for OpenSearch.

---

## 2. Target architecture — stateless, no cache

> OpenSearch is source-of-truth + search engine. **There is no in-memory config cache.**
> Selection is a live query per question; the selected config is fetched on demand for
> building. Every KPI edit/add is live on the very next request — zero restart, zero deploy.

```
                 ┌─────────────────────────────────────────┐
   Author adds/  │           OpenSearch cluster             │
   edits a KPI → │  index: cora-kpi-configs                 │
   (no deploy,   │  _id = KPI name                          │
    live next    │  curated search fields + full config blob│
    request)     └───────────────┬─────────────────────────┘
                                 │
      ┌──────────────────────────┴───────────────────────────┐
      │ (A) SELECTION — once per question                     │
      │ search_kpis → BM25 multi_match                        │
      │ returns ~8 SUMMARIES (small)          ← LLM sees this  │
      └──────────────────────────┬───────────────────────────┘
                                 │
      ┌──────────────────────────┴───────────────────────────┐
      │ (B) BUILD — once per generate_query/run_kpi           │
      │ get(name) → OpenSearch GET by _id → full config       │
      │            (server-side only)         ← LLM never sees │
      │ build_sql(config) → SQL + params                      │
      └───────────────────────────────────────────────────────┘
```

**Two OpenSearch calls per full answer**, both tiny:
- **Search** — one BM25 query (selection).
- **GET by `_id`** — a single-document lookup by KPI name (cheapest OpenSearch op, ~1–5 ms;
  not a search). This replaces exactly one line: today `catalog.get(name)`
  (`kpi_catalog.py:81`) becomes an OpenSearch get-by-id. `build_sql()` and everything
  downstream are unchanged.

**Async client.** The store uses `AsyncOpenSearch` (aiohttp transport, `AIOHttpConnection`,
pool `maxsize=30`). `KpiCatalog`'s data methods are therefore `async`, and the await
propagates through `query_engine.generate_query` and the catalog-touching MCP tools
(`search_kpis`, `describe_kpi`, `generate_query`, `describe_module`, `describe_dataset`,
`plan_query`). The `FileBackend` fallback stays synchronous; the facade bridges the two so
neither the SQL builder nor the disk path is blocked.

**No hydration, no refresh, no poll, no staleness.** An added *or edited* config is
reflected on the next request because nothing is cached.

**Token cost is unchanged from today.** The full config is fetched **server-side inside
`generate_query`** and never routed through the LLM. `search_kpis` returns only ~8
summaries. The 68 configs never enter the model context. (See §8.)

---

## 3. Index mapping

`cora-kpi-configs` — one doc per KPI, `_id = name`:

```jsonc
PUT cora-kpi-configs
{
  "mappings": {
    "properties": {
      "name":            { "type": "keyword", "fields": { "text": { "type": "text" } } },
      "title":           { "type": "text", "analyzer": "english" },
      "module":          { "type": "keyword" },              // am/cm/em/im/pm/rm/sd/sr
      "unit":            { "type": "keyword" },
      "execution_mode":  { "type": "keyword" },              // DSL | SQL
      "status":          { "type": "keyword" },              // live | draft
      "synonyms":        { "type": "text", "analyzer": "english" },
      "sample_questions":{ "type": "text", "analyzer": "english" },
      "tags":            { "type": "keyword", "fields": { "text": { "type": "text" } } },
      "allowed_filters": { "type": "keyword" },
      "drilldown_dims":  { "type": "keyword" },
      "primary_table":   { "type": "keyword" },              // "itsm_change.tbl_change"
      "search_blob":     { "type": "text", "analyzer": "english" },  // name+title+syn+samples+tags
      "updated_at":      { "type": "date" },

      // The FULL config JSON — stored, NOT indexed. Returned via _source, fed to build_sql().
      "config":          { "type": "object", "enabled": false }
    }
  }
}
```

`"config": {"enabled": false}` stores the entire KPI JSON verbatim and returns it on
retrieval, but does not index its internals (also dodges the 1000-field limit). Only
curated search fields are analyzed. `embedding`/kNN is intentionally omitted (deferred).

---

## 4. Every way users ask → OpenSearch mapping → response

| # | Scenario (example) | OpenSearch? | Match / field | Response |
|---|---|---|---|---|
| **S1** | Named KPI — *"emergency change lead time"* | **Yes (selection)** | `name.text^5`, `title^3` BM25 | Exact config ranks top |
| **S2** | Paraphrase — *"urgent change turnaround time"* | **Yes** ← biggest win | BM25 over `synonyms`/`search_blob` | Correct config where token-overlap misses |
| **S3** | Module-level — *"service desk for APAC"* | **Yes** | `filter: {term:{module:"sd"}}` then rank | Member configs → `overview_module` |
| **S4** | Filter aliases — *"...for Retail sector"* | **No** (aliases on disk) | Existing `AliasRegistry` | `filters={"sector":["Retail"]}` |
| **S5** | Value synonyms — *"closed incidents"* | **No** | Existing `value_resolver` + schema | Real stored values |
| **S6** | Time period — *"last quarter"* | **No** | Deterministic `resolve_dates` | Date window |
| **S7** | Trend/series — *"month over month"* | **No** (post-selection) | `mode="series"`, `grain` | Time buckets |
| **S8** | Breakdown — *"by region"* | **No** (post-selection) | `mode="table"`, `dim` validated vs `drilldown_dims` | Grouped rows |
| **S9** | Ambiguous — *"change metrics"* | **Yes** | BM25 + score-gap heuristic | Ranked list to disambiguate |
| **S10** | No governed KPI — *"incidents caused by changes"* | **Yes (gate)** | Top BM25 score < threshold ⇒ "no confident KPI" | Route to `plan_query`/`query_dataset` |
| **S11** | Named record — *"INC0353896"* | **No** | `record_prefixes.json` | `get_record` |
| **S12** | Comparison (CYTD/PYTD) | **No** | `resolve_comparison` reads `config.comparison` | Two windows |

**Boundary:** OpenSearch changes **selection (S1–S3, S9–S10)** and **storage of every KPI
JSON**. It does not touch date math (S6), SQL building (S7/S8/S12), alias/value resolution
(S4/S5), or record lookup (S11).

### Example selection query (S1/S2)

```jsonc
GET cora-kpi-configs/_search
{
  "size": 8,
  "query": {
    "multi_match": {
      "query": "urgent change turnaround time",
      "fields": ["name.text^5","title^3","synonyms^2","sample_questions","search_blob","tags.text^2"],
      "type": "best_fields"
    }
  },
  "_source": ["config"]                      // build summary from the returned config
}
```
Add `"filter": [{"term": {"module": "cm"}}]` inside a `bool` for S3. Returns the **same
summary shape** `search_kpis` returns today — `tools.py` and the tool surface are unchanged.

---

## 5. Worked end-to-end example

User: *"how has urgent change turnaround trended monthly for the Retail business"*

```
1. search_kpis(...)             → OpenSearch BM25 → hit: name="emergency" (+summary)   [LLM sees summary]
2. "business" → filter-alias (disk AliasRegistry)  → key "sector"
3. "Retail"   → value-alias/schema check (disk)    → "Retail"
4. "monthly"/"trended"                             → mode=series, grain=month
5. generate_query(kpi="emergency", mode="series", grain="month", filters={"sector":["Retail"]})
     → KpiCatalog.get("emergency") → OpenSearch GET by _id → full config   [server-side; LLM never sees]
     → build_sql(config) → SQL + params
```

Only step 1 (selection) and the `get` in step 5 touch OpenSearch. Steps 2–4 and
`build_sql` are the existing pipeline, unchanged.

---

## 6. Zero-deploy config loop (the payoff)

Files remain the **git authoring format**; an idempotent indexer pushes them to OpenSearch
(the deployed runtime store).

```
# add / edit a KPI
python tools/index_configs.py <name-or-glob>      # upsert one/many docs
# → live on the NEXT request. No restart, no refresh ping, no cache — nothing is held in RAM.
```

Because there is no cache, **edits and additions behave identically** — the next `search`
/ `get` sees the new bytes immediately.

---

## 7. Build phases

1. **Index mapping + idempotent indexer** — `tools/index_configs.py` creates the mapping
   and upserts `config/*.json` (curated fields + full `config` blob). Re-runnable;
   `--recreate` drops and rebuilds.
2. **Backend swap** — `KpiCatalog` gains two backends behind its unchanged public methods:
   * `FileBackend` — today's disk load + token-overlap search (the fallback).
   * `OpenSearchBackend` — live BM25 `search`, get-by-`_id`, `by_module`/`names`/
     `configs_for_tables` via term queries; **per-call disk fallback** on any OpenSearch error.
   Selected by `CORA_CONFIG_BACKEND=opensearch|files|auto` (default `auto`: OpenSearch if
   configured + reachable, else files).
3. **Accuracy regression** — build a labeled set from every config's
   `nl.sample_questions` (~200 pairs); measure top-1 / recall@3 for BM25 vs the old scorer.
4. **(Deferred) Semantic hybrid** — add `embedding` + kNN only if BM25 under-recalls.

---

## 8. Cost / token model — server work vs LLM tokens

| | What it is | Who sees it | Cost |
|---|---|---|---|
| Config fetch (get-by-id) | Server-side OpenSearch call inside `generate_query` | **Only server code** (`build_sql`) | ~1–5 ms; **0 LLM tokens** |
| `search_kpis` result | ~8 trimmed summaries | The LLM | small (unchanged from today) |
| Full config JSON | The 300-line KPI doc | **Never the LLM** | n/a |

The 68 configs never enter the prompt. LLM token usage is driven only by tool return
values, which are identical in shape/size to today. If anything, better BM25 ranking
lowers tokens (fewer retries over mediocre matches).

---

## 9. Resilience & fallback

Two distinct failure modes, two fallbacks:

| Failure | What broke | Fallback |
|---|---|---|
| **A. OpenSearch down / library absent / index missing** | Can't search or get | **Per-call disk fallback** to `config/*.json` — governed KPIs keep working exactly as today. |
| **B. OpenSearch up, no confident match (S10)** | Not a governed KPI | **Schema-driven ad-hoc** — `plan_query` → `query_dataset` (uses `schema_v3.yaml`). |

The implementation is **fully optional**: if `opensearch-py` is not installed or no
`OPENSEARCH_URL` is set, the catalog runs the `FileBackend` and behaves byte-for-byte like
today.

---

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| New runtime dependency / OpenSearch outage | Per-call disk fallback; optional library (graceful import) |
| Two sources of truth (files vs index) drift | Files stay authoritative; indexer is the only writer |
| BM25 scores shift as corpus grows | Pin analyzer settings; keep `sample_questions` regression set |
| Nested `config` blows up mapping / 1000-field limit | `"config": {"enabled": false}` — stored, not indexed |
| Extra get-by-id per build | Negligible latency; `tools.py` `_CALL_CACHE` (45s) already dedups repeats |
| `"no confident KPI"` threshold mis-tuned | Calibrate on labeled sample questions; log near-miss scores |

---

## 11. Config / env

```
# .env
CORA_CONFIG_BACKEND=auto          # auto | opensearch | files
OPENSEARCH_URL=https://host:9200  # unset ⇒ files backend
OPENSEARCH_USER=...
OPENSEARCH_PASSWORD=...
OPENSEARCH_INDEX=cora-kpi-configs
OPENSEARCH_VERIFY_CERTS=true
```

---

## 12. Explicitly unchanged

`gen_query.py` build logic · `resolve_dates` · `resolve_comparison` · filter/value aliases ·
`schema_v3.yaml` / `record_prefixes.json` · `query_dataset` / `plan_query` ad-hoc fallback ·
`get_record` · semantic/kNN (deferred) · the MCP tool surface (`tools.py`).