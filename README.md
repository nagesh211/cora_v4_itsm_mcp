# CORA MCP — natural-language query generation

A [FastMCP](https://github.com/modelcontextprotocol) server and test client that
put a natural-language front end on the existing `gen_query.py` KPI query
generator. An agent asks a question; the server (1) resolves the date window
deterministically, (2) discovers the right module / dataset / KPI from the
catalog, and (3) uses `gen_query.py` to emit the exact SQL + bind params — for
both DSL and SQL configs — with dates, dimensions and filters applied.

```
NL question ─▶ resolve_dates ─▶ search_kpis ─▶ run_kpi ─▶ SQL (gen_query.py) ─▶ execute (asyncpg) ─▶ rows
               (deterministic)   (config/*.json)                                (Postgres)
```

An interactive **web chat UI** (`clients/web_ui.py`) sits on top: type a question,
and it shows the agent's answer, the resolved date window, the generated SQL, and
the result rows as a table.

## Layout

| Path | Purpose |
|------|---------|
| `gen_query.py` | **Unchanged.** The standalone SQL builder, imported as a library. |
| `config/*.json` | **Unchanged.** 66 KPI configs (ITSM: am/cm/em/im/pm/rm/sd/sr). |
| `schema_v3.yaml` | Rich catalog for the `itsm` module: entities → tables → columns with `role`/`canonical`/`time`. |
| `schema_v3.legacy.yaml` | Backup of the original flat schema (created on first restructure). |
| `tools/restructure_schema.py` | One-off transformer that produced the rich schema. |
| `cora_mcp/` | The MCP service (see below). |
| `clients/web_ui.py` | Interactive **web chat UI** (Starlette + autogen agent). |
| `clients/smoke_test.py` | No-LLM end-to-end test of the server. |
| `clients/autogen_client.py` | Autogen 0.7 LLM test client (terminal). |
| `.env` / `.env.example` | DB DSN + LLM config (copy the example). |
| `tests/` | `pytest` unit tests (date resolver, query engine, DB converter, web unwrap). |

### `cora_mcp/` package
- `logging_config.py` — central logging (`CORA_LOG_LEVEL`, default INFO).
- `date_resolver.py` — deterministic NL date-window resolution (FY=April,
  weeks start Sunday, current periods → to-date). No LLM / redis / app deps.
- `schema_loader.py` — loads the rich `schema_v3.yaml`; indexes modules/entities/columns.
- `kpi_catalog.py` — loads `config/*.json`; token-overlap KPI search.
- `db.py` — Postgres execution via **asyncpg**; resolves `source.connection` →
  DSN from `.env`; converts gen_query's `%s`/tuple/list params to asyncpg `$n`.
- `query_engine.py` — bridge to `gen_query.py`: `generate_query(...)` (SQL only)
  and `run_query(...)` (SQL **+ execute**, returns rows).
- `tools.py` — registers the MCP tools.
- `server.py` — FastMCP entrypoint (Streamable HTTP).

## Tools (12)

A small, fixed surface (parameterized, not one auto-generated tool per module /
entity — that had ballooned to 46 and hurt tool selection). Every tool call goes
through a short-TTL **dedup guard** (`CORA_DEDUP_TTL`, default 45s): an identical
`(tool, args)` repeat within the window returns the cached result tagged
`repeated_call` instead of re-running — a deterministic backstop against an agent
re-issuing the same query in a loop.

**Discovery**
- `list_modules()` — every catalog module + entities + coverage.
- `describe_module(module)` — one module's database type, coverage and entities
  (for ITSM, also its KPI configs). No/unknown name → the list of valid modules.
- `describe_dataset(dataset)` — one entity's columns grouped by role (dimensions /
  measures / timestamps / identifiers), possible values, member tables, related
  KPIs. E.g. `describe_dataset("itsm_change")`. No/unknown slug → valid slugs.

**KPI actions**
- `search_kpis(query, module=None)` — rank KPI configs by relevance.
- `describe_kpi(kpi)` — one KPI's filters, dimensions, sample questions.
- `generate_query(kpi, period=, from_date=, to_date=, as_of=, mode=, dim=, grain=, filters=, comparison=)`
  — SQL for both DSL and SQL configs. `mode` ∈ `stat` | `series` | `table`.
- `run_kpi(...)` — **generate SQL AND execute** a governed KPI, returning rows.

**Record detail**
- `get_record(record_id, entity=None, related=None, limit=50)` — fetch ONE specific
  record's own detail columns **and its linked records**, never a metric/count. Use
  it whenever the user names a concrete id (e.g. `INC0353896`, `CHG0012345`,
  `PRB…`). The id's prefix picks the entity + human id column via
  `record_prefixes.json` (extend that file — no code — for new record types; pass
  `entity` to override). `related` defaults to every entity reachable in one
  declared relationship hop (e.g. changes **and** problems for an incident); pass a
  list like `["change","problem"]` to restrict, or `["none"]` for the record only.
  Detail columns are chosen deterministically from the schema (human identifiers +
  key dimensions + timestamps of the queried table, capped), so they track the
  schema automatically.

**Insights**
- `overview_module(module, period=None, filters=None, limit_kpis=None)` — run every
  KPI in a module for one period/filter and return a rollup (value, delta vs the
  comparison window, target, RAG status). `module` accepts a code
  (am/cm/em/im/pm/rm/sd/sr) or a phrase ("service desk", "availability"). Each
  filter is applied only to the KPIs that allow it (the rest are recorded under
  `dropped_filters`, never silently mis-applied). E.g. "what's happening in
  availability for the CGF sector".

**Ad-hoc / relationships / dates**
- `query_dataset(base, metric=, select=, measure=, dimensions=, filters=, period=, join_with=, join_type=, grain=, drilldown=, limit=)`
  — **dynamic schema-driven query** (ad-hoc / cross-entity / drill-down): the LLM
  supplies a structured spec, a deterministic builder validates every identifier
  against the schema and executes read-only SQL. See "Dynamic queries" below.
  Pass `select=[cols]` for a **detail listing** (raw rows, no aggregation); pass
  `measure`/`dimensions`/`grain` for an **aggregate** (count/sum/avg/series). Text
  filters (scalars, `in`, `like`, and text arrays) match **case-insensitively**
  (`lower(col)=lower(value)`) so `closed` matches stored `CLOSED`; numbers, dates
  and booleans compare as-is. Empty-but-valid results attach a `diagnostics` block
  (each restriction relaxed + its count) so a zero can be explained, not just
  reported. Every executed statement is syntax-checked with **sqlglot** first
  (`CORA_SQL_VALIDATE=off` to disable).
- `list_relationships(module=None)` — declared join paths for cross-entity queries.
- `resolve_dates(period)` — NL phrase → `{start_date, end_date, matched}`.

### Filters, dimensions & dates — how they apply

- **Filters** apply in **both** execution modes. DSL configs bind them as params;
  SQL configs inject them as inline (escaped) predicates at a `{filters}`
  placeholder in the authored `base_query`, using each field's `fields`/`filter_type`
  mapping. A requested filter is validated against the config's `filters.allowed`
  and mapped column — an unknown/disallowed filter is **rejected** (clear error),
  never silently dropped or mis-applied. If a SQL config genuinely can't take a
  filter (no `{filters}` slot), the request errors rather than returning an
  unfiltered number.
- **Filter aliases.** A filter key may be a canonical name (`sector`, `division`,
  `assignment_group`, …) **or** a user-facing alias defined in `filter_aliases.json`
  (`business`/`p&l` → `sector`, `sub_business`/`division` → `division`, `team` →
  `assignment_group`, `capability` → `service_area`, …). Resolution is
  case-insensitive, folds spaces/underscores/hyphens, matches the whole term (so
  `sub_business` never collides with `business`), and is KPI-aware: an alias for a
  filter the KPI doesn't expose is **rejected with the list of valid filters**,
  never guessed. `describe_kpi`/`search_kpis` advertise each KPI's accepted aliases
  under `filter_aliases` so the model uses the real vocabulary. See
  `cora_mcp/filter_aliases.py`.
- **Dimensions** (`mode="table"`) group DSL configs by the chosen column. SQL-mode
  configs can't express a generic `GROUP BY` over an authored scalar query, so
  table mode on a SQL KPI is **rejected** (use its drilldown breakdown or
  `query_dataset`) instead of silently returning an ungrouped scalar.
- **Dates** apply in both modes (DSL `between` bind; SQL `{from_date}`/`{to_date}`/
  `{as_of}` substitution).

## Dynamic queries (schema-driven)

Beyond the 66 governed KPIs, `query_dataset` answers questions **directly from the
schema**. The LLM never writes SQL — it emits a structured spec and
`cora_mcp/sql_builder.py` builds it deterministically, rejecting any table/column
not in `schema_v3.yaml` (SELECT-only, auto-`LIMIT`). Four paths:

1. **Metric-anchored** — `metric=<kpi>` reuses that KPI's table + measure + date
   field, but dimensions/filters may be **any** schema column of that table.
   (Only DSL-mode KPIs can be anchored — a SQL-mode KPI's formula lives in raw
   `base_query` and isn't decomposable, so anchoring one errors unless you pass an
   explicit `measure`. For SQL KPIs, use their now-supported `filters.allowed` via
   `run_kpi`/`generate_query` instead.)
2. **Ad-hoc (no metric)** — set `base` (entity/table), `measure` {agg, column},
   `dimensions`, `filters`, `period`.
3. **Cross-entity** — `join_with=[entity]`; joins are planned from the schema's
   `relationships:` block (`cora_mcp/relationships.py`), which is seeded from the
   join paths the existing configs already use (incident↔change with
   `type='Caused By Change'`, incident↔problem, release→change, SLA↔incident).
   `join_type='inner'` (default) restricts to related records.
4. **Drill-down** — `drilldown={detail_columns, entity_filter}` returns the
   detail/reason rows for a specific record (e.g. why a given change happened).

Relationships are declared by `tools/harvest_relationships.py` (`--verify` prints
the joins discovered in the configs; the default run writes the curated
`relationships:` blocks into `schema_v3.yaml`).

## Setup

```bash
python -m pip install -r requirements.txt      # into your venv
```

## Configure

```bash
cp .env.example .env      # then fill in:
#   CORA_DB_VTX5=postgresql://user:pass@host:5432/dbname   # to execute queries
#   OPENAI_API_KEY / OPENAI_BASE_URL / CORA_LLM_MODEL      # for the agent
```

Without a DB DSN the system still works — `run_kpi` returns the generated SQL
plus a clear "connection not configured" note instead of rows.

## Run

```bash
python -m cora_mcp.server            # 1) MCP server -> http://0.0.0.0:8081/mcp
python clients/web_ui.py             # 2) web chat UI -> http://127.0.0.1:8090
```

Env: `CORA_MCP_HOST` (0.0.0.0), `CORA_MCP_PORT` (8081), `CORA_LOG_LEVEL` (INFO),
`CORA_WEB_HOST` (127.0.0.1), `CORA_WEB_PORT` (8090), `CORA_MCP_URL`
(http://localhost:8081/mcp).

## Verify

```bash
# 1. rebuild the rich schema (idempotent; backup kept)
python tools/restructure_schema.py

# 2. unit tests
python -m pytest tests/ -q

# 3. no-LLM end-to-end (server must be running)
python clients/smoke_test.py

# 4. web chat UI (server must be running; needs OPENAI_* for the agent)
python clients/web_ui.py            # open http://127.0.0.1:8090

# 5. terminal LLM agent
python clients/autogen_client.py
```

Live DB tests in `tests/test_db_exec.py` run automatically once `CORA_DB_VTX5`
(or `CORA_PG_DSN`) is set; otherwise they are skipped.

## Notes on semantics

- **Date windows.** `stat` mode honours each config's comparison basis (CYTD /
  PYTD anchored to the resolved *to* date) — the same behaviour as the product's
  `serve_kpi`. `series` and `table` use the literal resolved window. Pass a
  natural-language `period` (resolved by `resolve_dates`) *or* explicit
  `from_date`/`to_date`.
- **Both execution modes.** DSL configs compile through the structured builder;
  SQL configs get the window substituted into their authored `base_query`.
  `generate_query` / `run_kpi` handle both transparently.
- **Execution.** `run_kpi` executes the generated SQL via asyncpg against the
  DSN resolved from the config's `source.connection` (all KPIs use `vtx5` →
  `CORA_DB_VTX5`). gen_query's psycopg-style `%s` params (tuples for `IN`, lists
  for `ANY`/`&&`) are converted to asyncpg `$n` bind params in `db.to_asyncpg`.
  Because gen_query (built for psycopg2) passes literals as strings while asyncpg
  binds by exact type, `db.execute` prepares the statement, reads each
  parameter's Postgres type, and coerces the string to the right Python type
  (bool / int / float / timestamp / date) via `db._coerce_for_pgtype`.
- **Web chat UI** (`clients/web_ui.py`, FastAPI). Endpoints: `GET /` (the page,
  `clients/web/index.html`), `POST /api/ask`, `POST /api/new_chat`. Each chat has
  a `request_uuid`; the autogen agent's state is saved to / loaded from **Redis**
  (`clients/state_store.py`, `REDIS_URL`; in-memory fallback if Redis is down) so
  follow-up questions keep context, and **New chat** starts a fresh uuid. A second
  **summarizer agent** turns the executed rows into a plain-language summary shown
  above the SQL and result table.
- **Date resolver** is deterministic only. If a phrase isn't recognised it falls
  back to month-to-date and sets `matched=false`; an LLM fallback can be added
  at the marked hook in `date_resolver.resolve_dates`.
- **Schema restructure** adds `role`/`canonical`/`time`/`coverage` and preserves
  all existing metadata (`alias`, `possible_values`, `cross_join`, `primary_key`).
  Places needing domain input (e.g. primary/secondary currency mapping) are
  flagged with `OPEN_QUESTION` comments rather than guessed.
