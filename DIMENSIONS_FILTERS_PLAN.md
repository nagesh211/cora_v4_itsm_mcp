# Explicit Dimensions & Filters — Working Plan

**Status:** In progress · **Branch:** `feature/opensearch-intigration`
**Goal:** The **agent** extracts the dimensions and filters the user asked for and
passes them **explicitly** as tool arguments. The **MCP tools resolve them internally**
(alias → canonical, value → domain, schema validation). Every dimension/filter param
**accepts a value or `None`**, and when it is `None` the tool **does not invent or
default** one — an omitted dimension/filter means "the user named none".

> Scope is **dimensions + filters only**. `period`, `module`, `mode` and `grain` keep
> their current behaviour and are out of scope for this change.

---

## 1. The core principle

There are two distinct steps that must not be confused:

| Step | Owner | What it does |
|---|---|---|
| **Identify** which dimensions/filters the user asked for | **the agent** | Reads the question, extracts breakdown words and qualifiers, passes them as `dim` / `filters` / `dimensions`. |
| **Resolve** a passed term to a real column/value | **the MCP tools** | alias → canonical (`business` → `sector`), value → domain (`closed` → `CLOSED`), schema validation, reject unknowns with the valid list. |

The tools must **never** back-fill an *identify* decision the agent didn't make. If the
agent passes `None`, the tool applies no dimension/filter — it does not guess one from
the question, the config, or a default view.

---

## 2. Current state

The params already exist and resolution already lives in the tools:

| Tool | dimension param | filter param |
|---|---|---|
| `run_kpi` / `generate_query` | `dim: str \| list \| None` | `filters: dict \| None` |
| `overview_module` | `dim: str \| list \| None` | `filters: dict \| None` |
| `query_dataset` | `dimensions: list \| None` | `filters: list[dict] \| None` |

Resolution in `query_engine.py` is what we **keep**: `resolve_filter_key` (alias →
canonical), `_resolve_filter_values` (value → domain), `resolve_dim_word` +
`resolve_dim_via_schema` (schema-validated group-by), and the "unknown term → reject
with the valid list" errors.

### The one behaviour that violates the principle

**`query_engine._effective_dim` (`query_engine.py:355`)** — in `table` mode with
`dim=None`, it silently falls back to the KPI's default table view (`view.get("by")`).
So `None` is *not* "no dimension"; it becomes a hidden default group-by. This is the
"invention" we remove.

(`resolve_module_code`'s loose `contains` match at `query_engine.py:734` is the same
guessing pattern but concerns `module`, which is out of scope here.)

---

## 3. Work plan

### Phase 1 — Lock the parameter contract
Document, on every affected tool, that `dim`/`dimensions`/`filters`:
- are supplied by the **caller/agent**, extracted from the user's request;
- accept a concrete value **or `None`**;
- when `None`, apply **nothing** — the tool never infers or defaults a dimension/filter.

### Phase 2 — Kill the silent invention (the code change)
- `_effective_dim`: when `mode="table"` and `dim` is falsy, return `None` instead of the
  config's default `view.by`. The existing guard at `query_engine.py:516` then raises the
  clear error: *"table mode needs a dimension for `<kpi>`; pass dim=<field>."*
- `series` with no dim stays an overall trend (already correct — no invention).

### Phase 3 — Resolution stays in the tools (tighten, don't add)
- Keep filter alias + value resolution and unknown-term rejection as-is.
- Keep dimension resolution (`resolve_dim_word` + schema fallback). An **explicit**
  dimension that resolves to nothing is surfaced as `dropped_dim` + reason (already the
  behaviour), never swapped for a guessed neighbour.

### Phase 4 — Agent prompt owns extraction
In `_ANALYST_SYSTEM` (`clients/web_ui.py`): state that the agent must extract every
dimension and filter from the question and pass them explicitly; the MCP will **not**
infer them; omitting a dimension/filter means "none"; and `table` mode now **requires**
an explicit `dim`.

### Phase 5 — Tests
- `table` mode + `dim=None` → `QueryError`, no default group-by.
- explicit valid dim/filter → resolved + applied.
- explicit invalid dim → `dropped_dim` + reason; invalid filter → rejection with the
  valid list.
- `filters=None` and `dim=None` → plain stat, nothing invented.

---

## 4. Files touched

| File | Change | Phase |
|---|---|---|
| `cora_mcp/query_engine.py` | `_effective_dim` stops defaulting to `view.by` | 2 |
| `cora_mcp/tools.py` | docstring contract on `run_kpi`/`generate_query`/`overview_module`/`query_dataset` | 1, 3 |
| `clients/web_ui.py` | analyst prompt: agent extracts & passes dims/filters; `None` = none | 4 |
| `tests/test_query_engine.py` | update `table`-no-dim test; add None-semantics tests | 5 |

---

## 5. Behaviour change callout

`run_kpi(mode="table")` / `generate_query(mode="table")` with **no** `dim` previously
"worked" (silent default group-by) and now returns a clear error. This is intended: a
breakdown must name its dimension. `overview_module` is unaffected (no dim → scalar
values per KPI, which was already the non-inventing behaviour).