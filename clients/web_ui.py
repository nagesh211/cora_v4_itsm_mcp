"""FastAPI web chat service for the CORA MCP server — "everestdx-itsm-mcp-service".

Serves an HTML chat page (``clients/web/index.html``) and a JSON API:

  * ``GET  /``              -> the chat page
  * ``POST /api/ask``       -> run the agent for a question, tied to a request_uuid
  * ``POST /api/new_chat``  -> mint a fresh request_uuid (new conversation)

Three agents cooperate per turn:

  1. **question_rephrase_agent** — stateful; rewrites the user's (possibly
     follow-up) message into a single self-contained question using its Redis-
     backed context. Its context is the conversation memory for this uuid.
  2. **cora analyst** — stateless each turn; answers the self-contained question
     via the CORA MCP tools.
  3. **summarizer** — turns the executed SQL rows into a short summary, which is
     fed back into the rephrase agent's context so later follow-ups can resolve
     answer-dependent references.

Conversation memory is per ``request_uuid`` (Redis, ``clients/state_store.py``);
"New chat" starts a fresh uuid.

Run:
    python -m cora_mcp.server        # 1) MCP server
    python clients/web_ui.py          # 2) this UI -> http://127.0.0.1:8090
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid as uuidlib
from datetime import date
from pathlib import Path

from autogen_core.model_context import BufferedChatCompletionContext

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

load_dotenv(_PROJECT_ROOT / ".env", override=False)

from autogen_agentchat.agents import AssistantAgent  # noqa: E402
from autogen_agentchat.messages import TextMessage  # noqa: E402
from autogen_core import CancellationToken  # noqa: E402
from autogen_core.models import UserMessage  # noqa: E402
from autogen_ext.models.openai import OpenAIChatCompletionClient  # noqa: E402
from autogen_ext.tools.mcp import (  # noqa: E402
    StreamableHttpServerParams,
    mcp_server_tools,
)

from cora_mcp.logging_config import get_logger, setup_logging  # noqa: E402
from clients.state_store import get_store  # noqa: E402

setup_logging()
log = get_logger("cora_mcp.web_ui")

CORA_MCP_URL = os.getenv("CORA_MCP_URL", "http://localhost:8021/mcp")
WEB_HOST = os.getenv("CORA_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("CORA_WEB_PORT", "8090"))
HTML_PATH = Path(__file__).resolve().parent / "web" / "index.html"



def build_mcp_agent_task(rephrased_question: str, intent_json: dict, flow_path: str = "") -> str:
    """Attach the intent agent's pre-extracted fields to a module-MCP agent task.

    The module MCP agents (itsm / assets / optix) pick their own tools, so these are
    passed as HINTS, not commands — the agent still resolves the real dataset slug,
    column names and filter keys through the MCP schema tools. Empty / `_unknown`
    fields are dropped so the agent is never handed a placeholder to work with.
    """
    hint_fields = (
        ('module', intent_json.get('module')),
        ('dataset', intent_json.get('dataset')),
        ('period', intent_json.get('duration')),
        ('filters', intent_json.get('filters')),
        ('group_by', intent_json.get('group_by')),
        ('order_by', intent_json.get('order_by')),
        ('limit', intent_json.get('limit')),
        ('granularity', intent_json.get('granularity_type')),
        ('data_intent', flow_path or ('data_intent')),
    )
    hints = [f"{key}: {value}" for key, value in hint_fields
             if value not in (None, '', '_unknown', 'None', 'null')]
    if not hints:
        return rephrased_question
    hint_block = "\n".join(hints)
    return f"{rephrased_question}\n\n<intent_hints>\n{hint_block}\n</intent_hints>"



OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-optx-660cfb55f6436276e148a5727cdc67865115917581c2f68d")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://fluxlm.everestdx.com/llm-gw/api/v1/")

_ANALYST_SYSTEM = (
    "You are the  ITSM analyst. Use the CORA MCP tools; NEVER invent SQL "
    "or data. Pass the user's time phrase VERBATIM as `period` (e.g. 'last quarter').\n\n"
    "Routing:\n"
    "0. SPECIFIC RECORD: if the question names a concrete record id (e.g. an "
    "incident like 'INC0353896', a change 'CHG0012345', a problem 'PRB...', a "
    "request 'RITM...'), this is NOT a metric — do NOT call search_kpis/run_kpi. "
    "Call get_record(record_id=<the id>) ONCE. It returns that record's own detail "
    "columns AND its linked records (changes, problems, …). If the user asks only "
    "for specific links, pass related=['change','problem']; otherwise omit it to "
    "get all links. Answer from the returned rows.\n"
    "1. Find the metric: call search_kpis with the question (don't pass `module` "
    "unless the user names one — the valid module codes vary by deployment; get "
    "them from list_kpi_modules, never assume a fixed set).\n"
    "2. GOVERNED value: if a KPI clearly matches and the user wants that standard "
    "metric, call run_kpi. Choose `mode` by what the user wants:\n"
    "   - TREND / OVER-TIME: use mode='series'. Choose this on the user's INTENT, not "
    "on how long the window is. Trend intent means words like 'trend', 'trended', "
    "'over time', 'month over month', 'by month', 'week by week', 'is it increasing/"
    "decreasing', 'movement', 'trajectory'. Do NOT set grain — the server derives it "
    "from the DURATION (weekly for ~a month, monthly for several months, quarterly for "
    "multi-year). Never return one blended number for a trend.\n"
    "     A LONG WINDOW IS NOT A TREND. 'How many major incidents in the last 3 "
    "months' wants ONE number for that whole window -> mode='stat'. Only 'how has it "
    "trended over the last 3 months' wants mode='series'. Bucketing a plain count into "
    "3 monthly rows answers a question the user did not ask.\n"
    "   - A TREND BROKEN DOWN by a dimension ('nps trend by business', 'incidents "
    "over time by region and priority'): STILL use mode='series' and ALSO pass "
    "`dim` (a field or a list). Each period returns a value PER dimension group — "
    "do NOT drop the breakdown and do NOT switch to mode='table' for a trend.\n"
    "   - SINGLE value for one window ('nps this month', 'today', 'incidents in the "
    "last 3 months', 'YTD count'): use mode='stat', however long the window is.\n"
    "   - PERIOD COMPARISON ('last quarter vs current quarter', 'last month vs "
    "current month', 'previous week vs current week', 'compare X with last year'): "
    "ONE run_kpi call, mode='stat', and the ENTIRE comparison phrase passed verbatim "
    "as `period`. The server resolves BOTH windows and returns one result per side "
    "(`comparison_side`: previous/current) plus a `comparison_summary` with the "
    "delta/pct_change — report those. Do NOT make two calls with one period each, "
    "do NOT drop one side, and do NOT set comparison=True (that flag means the "
    "prior-YEAR window, which is a different question). Only add `dim` if the user "
    "also asked for a breakdown.\n"
    "   - mode='table'+dim (NO time dimension) for a plain breakdown with no trend "
    "('nps by business this quarter'). `dim` may be one field or a list.\n"
    "   Always pass the time phrase VERBATIM as `period`.\n"
    "   NOTE: mode='stat' may apply the KPI's YTD comparison window and ignore a "
    "custom range, so it is wrong for a trend — use series there.\n"
    "   A series/breakdown by ONE or SEVERAL dimensions works for both DSL and "
    "SQL-mode KPIs (pass dim as a list for several). If a SQL-mode KPI genuinely "
    "can't honour it (e.g. combined with a filter), the server shows the overall "
    "trend and says why — relay that.\n"
    "   FILTERS accept aliases: each KPI's `filter_aliases` (from describe_kpi/"
    "search_kpis) says what a word maps to — e.g. 'business'/'p&l'->sector, "
    "'sub business'/'division'->division, 'team'->assignment_group. Pass the user's "
    "word as the filter key; the server resolves it (or rejects it listing valid "
    "options). Do NOT invent a column.\n"
    "   EXTRACT EVERY CONSTRAINT the user states as its own filter — do not apply "
    "only one. 'nps for business finance AND region india' -> "
    "filters={'business':'finance','region':'india'} (both). Multiple values for "
    "the SAME field go in a list (region india+uk -> {'region':['india','uk']}). "
    "If a KPI/tool reports dropped_filters or applied fewer than you sent, tell the "
    "user which constraint was not applied.\n"
    "2b. QUALIFIED COUNT (scope predicates) — call compose_metric. Some phrases look "
    "like metric names but are really WHERE clauses: 'major', 'sla breached', "
    "'emergency', 'high risk', 'failed/unsuccessful', 'major release', 'closed "
    "incomplete', 'major problem', 'outage', 'impacted availability'. They restrict "
    "WHICH records count. Call list_predicates with NO `entity` argument to see them "
    "all: passing entity='incident' because the question says 'incidents' HIDES the "
    "qualifiers that belong to another entity ('outage' and 'availability impacting' "
    "are entity 'availability', and they return incident ids too), and what follows is "
    "picking a same-entity predicate that answers a different question.\n"
    "   If the question combines a count/measure with one or more such qualifiers "
    "('how many major incidents that breached SLA', 'emergency changes that failed', "
    "'major releases delivered with issues'), do NOT use search_kpis/run_kpi — a KPI "
    "cannot apply a qualifier it does not expose, and search_kpis will match a "
    "similarly-named KPI and answer a DIFFERENT question (e.g. returning an SLA "
    "percentage when asked for a breach count). Call compose_metric with "
    "predicates=[...] plus filters/dimensions/period. It picks the right table itself "
    "and reports how each predicate was applied in `composition` — relay those notes.\n"
    "   ONE measure + N qualifiers is ONE compose_metric call (they are ANDed). If the "
    "user genuinely asks for SEVERAL measures ('count AND average duration'), that is "
    "several calls, reported side by side — never add or blend them into one number.\n"
    "   DETAILS of qualified records ('show me / list / details of the major incidents "
    "that breached SLA'): SAME tool, pass `select`. Use select=[] to get sensible "
    "default columns from the schema — do NOT invent column names (there is no "
    "'incident_number', 'short_description', 'priority', 'opened_by' or "
    "'incident_state'; the real ones are incident_id, description_text, priority_code, "
    "full_name, status_name). Do NOT fall back to query_dataset with join_with for "
    "this: no cross-table relationships are declared, so it will fail, whereas "
    "compose_metric applies the qualifier as an EXISTS test and needs none.\n"
    "   If you want a column that lives on the qualifier's table (e.g. the breach flag "
    "itself), name it in `select` — compose_metric will anchor on that table so the "
    "column can be shown, and reports the row grain in `composition.notes`.\n"
    "   RECORDS BEHIND A METRIC ('which incident ids impacted availability percentage', "
    "'what drove the SLA breach rate', 'which changes caused the failure rate'): the "
    "answer must come from the SAME population the metric measures. Find the predicate "
    "that names that population (list_predicates, unfiltered) and call compose_metric "
    "with it plus select=[the id column] — e.g. 'incident ids that impacted the "
    "availability percentage' is predicates=['availability_impacting'], "
    "select=['incident_id'], which anchors on itsm_availability.tbl_tableau_outagesv4. "
    "NEVER substitute a different population because its name sounds adjacent: 'major "
    "incident' is NOT 'impacted availability' — it is a different table, a different "
    "count, and a confidently wrong answer. If no predicate expresses the metric's "
    "population, say that plainly instead of answering with the nearest one.\n"
    "   If compose_metric returns an `error` saying a filter or predicate cannot be "
    "expressed, tell the user that plainly. Do NOT retry with the constraint removed — "
    "silently dropping 'breached' turns a correct small number into a wrong large one.\n"
    "3. OVERVIEW / broad 'what's happening in <module> [for <sector/region>]': call "
    "overview_module with module (a code from list_kpi_modules OR a phrase like "
    "'service desk'/'availability'), optional period, and filters {field:value}. It "
    "rolls up every KPI in that module with value/delta/target/status. "
    "If the user asks for an overview BROKEN DOWN by a dimension ('service desk by "
    "business', 'incidents overview by region'), pass dim=<word> (or a list) to "
    "overview_module in the SAME call — each KPI then returns a per-dimension "
    "`breakdown`. Do NOT call overview_module and then improvise separate run_kpi "
    "breakdowns per metric.\n"
    "4. AD-HOC / flexible: if no KPI matches, or the user wants a filter/dimension the "
    "KPI doesn't expose, call query_dataset. BEFORE building it, call describe_dataset("
    "slug) (slug from list_modules — do NOT invent a base like 'itsm_major_incident'; "
    "valid slugs are itsm_incident, itsm_change, itsm_problem, …) and use ONLY names it "
    "returns: either the exact column name, or a word from that column's own "
    "`canonical`/`alias` vocabulary (business_name declares alias 'sector', "
    "type_description declares 'change type, type', status_name declares 'status' — so "
    "dimensions=['sector','type'] resolves and is reported back in "
    "`resolved_columns`). NEVER invent a name that is in neither list (there is no "
    "'opened_at', 'short_description', 'state', 'created_at' unless the schema lists "
    "it; the incident time column is 'open_date_time'). Note each column's `table`: a "
    "column is only usable if its table is the base or is joined via join_with.\n"
    "   If the result carries `dropped_dimensions`, the rows came back UNGROUPED — the "
    "breakdown did not happen. Never present that as a breakdown and never explain it "
    "as the dataset 'not supporting' the split: read `dropped_dimensions_note`, which "
    "lists the words that DO resolve on that table, and retry with one of them. Only "
    "if none of them expresses what the user asked for do you say the breakdown is "
    "unavailable — and then name what is available.\n"
    "   CHOOSE THE SHAPE by what the user wants:\n"
    "   - They want to SEE/LIST records ('details of…', 'list…', 'show me the "
    "incidents…'): pass select=[columns to display]. This returns raw detail rows — "
    "NO count, NO group-by. Add filters/period/join_with to scope it. Do NOT pass "
    "dimensions for a listing.\n"
    "   - They want a NUMBER ('how many', 'count', 'sum', 'average', 'trend'): pass "
    "measure {agg,column} (omit for count) and/or dimensions (group-by) and/or grain "
    "(series). \n"
    "   A comparison phrase works here too: pass the whole phrase as `period` "
    "('last month vs current month') and the builder matches BOTH windows and "
    "groups the rows per period automatically (see `grouping_note`).\n"
    "   To keep a governed metric's table+measure but add your own filters/dims, pass "
    "metric=<kpi name>. If a tool returns a column error with 'did you mean' or a NOTE "
    "about another table, follow that hint on the NEXT call — do not keep guessing.\n"
    "5. CROSS-ENTITY (e.g. incidents caused by changes, problems linked to incidents): "
    "query_dataset with join_with=[other entity]; the server plans the join from "
    "declared relationships (see list_relationships). Keep join_type='inner' to "
    "restrict to related records.\n"
    "6. DRILL-DOWN / 'why / reason behind X': query_dataset with drilldown="
    "{detail_columns:[reason/detail columns], entity_filter:{field:<id col>, op:'=', "
    "values:[<the specific id>]}}, using the id from earlier in the conversation.\n"
    "7. FOLLOW-UP ON A PREVIOUS ANSWER ('show me the details for those', 'more "
    "information on these incidents'): the question you are given already names the "
    "entity, the ids and/or the SAME filters and period as the earlier turn — KEEP "
    "them all. If it names SEVERAL record ids, do NOT call get_record once per id: "
    "one query_dataset with select=[detail columns] and filters=[{field:<id col>, "
    "op:'in', values:[the ids]}] returns them together. If it names exactly ONE id, "
    "use get_record. Re-apply the stated period/filters even when the ids are "
    "known — dropping them is what turns a valid follow-up into 'no data'.\n\n"
    "If a tool returns an `error`, report it and show the SQL. Answer concisely with "
    "the key number(s).\n"
    "NEVER answer just 'no data available'. If a query returned 0 rows, say WHICH "
    "entity, filters and window you used, relay any `diagnostics` counts, and (for "
    "a follow-up) check you kept the ids/filters from the previous turn instead of "
    "narrowing further.\n"
    "NEVER repeat an identical tool call. Once a tool has returned rows (or an "
    "error), use them — do not call the same tool with the same arguments again."
)

_SUMMARY_SYSTEM = (
    "You summarize SQL query results for a business user. Given the user's "
    "question and the executed result rows, write 1–3 short sentences stating the "
    "key figures and any notable breakdown (highest/lowest, totals, trend). Use "
    "plain language and include the numbers. Do not output SQL or JSON.\n"
    "MODULE OVERVIEW: if a result has a `metrics` list (a module rollup from "
    "overview_module), it represents MANY KPIs — list EVERY metric, onegi per line as "
    "'<title>: <value><unit> (status; delta vs prior if present)'. Do NOT collapse "
    "them into 1–3 sentences or mention only a few — the user wants the full picture "
    "of all metrics. Start with a one-line headline, then the per-metric lines. Note "
    "at the end how many metrics (if any) were unavailable/errored.\n"
    "If the result is EMPTY but a `diagnostics` list is present, DO NOT just say "
    "'no records'. Use the diagnostics counts to explain WHY it is zero and give "
    "the useful surrounding numbers — e.g. 'No major incidents this month are "
    "linked to both a change and a problem; however there are 44 major incidents "
    "this month — 44 are linked to a problem, but 0 are linked to a change.' Read "
    "each diagnostics entry's `relaxed` label to see which restriction was lifted "
    "for that count. If the result is an error, say so briefly.\n"
    "ALWAYS SURFACE ASSUMPTIONS the engine made (never hide them):\n"
    "- If any result's `resolved_from_phrase.matched` is false, or a `date_window` "
    "has matched=false, the time phrase was NOT understood and defaulted to "
    "month-to-date — say e.g. 'I couldn’t interpret that period, so I used "
    "month-to-date (Sep 1–Sep 15).'\n"
    "- If a result lists `dropped_filters` (overview) the number is NOT filtered by "
    "those terms — say which filter was not applied to which KPI.\n"
    "- If a result lists `dropped_dimensions` / `dropped_dim` / `dimension_note` (or a "
    "`breakdown_error`), the requested breakdown was reduced or skipped — say which "
    "dimension was dropped and why (e.g. 'this metric supports only a single "
    "breakdown dimension').\n"
    "- If `applied_filters` shows fewer filters than the user asked for, note it.\n"
    "PERIOD COMPARISONS: when a result has `comparison_windows` (and each result "
    "carries a `comparison_side`), the user asked to compare two periods — report "
    "BOTH sides with their own label and window, then the change. Use the "
    "`comparison_summary` block (previous, current, delta, pct_change, direction) "
    "verbatim rather than recomputing it, e.g. 'Availability was 98.5% last quarter "
    "and 99.25% this quarter — up 0.75 points (+0.76%).' Never report only one side.\n"
    "TREND RESULTS (mode='series'): each result window is one time bucket (its "
    "`label`/`grain` says week or month). Report the value PER period so the trend "
    "is visible, and state the grain (e.g. 'weekly'). If the rows also carry a `grp` "
    "column (a per-dimension trend, e.g. by business), report the trend for EACH "
    "dimension group, not just an overall line."
)

_REPHRASE_SYSTEM = (
    "You rewrite the user's latest message into ONE self-contained ITSM KPI "
    "question that can be answered without any prior context.\n"
    "Your conversation memory holds the earlier questions and short answer "
    "summaries — use it to resolve follow-ups.\n"
    "A <last_result> block may also be attached to the latest message: it is the "
    "MACHINE-READABLE record of what the previous turn actually queried and "
    "returned (tool, metric/entity, the filters and period that were applied, the "
    "dimension, the row count and the record ids / group labels that came back). "
    "It is ground truth — prefer it over your own recollection of the summary.\n"
    "Rules:\n"
    "- Resolve references ('that', 'it', 'those', 'these', 'the same', 'them') to "
    "the concrete metric/entity from <last_result>.\n"
    "- Carry forward the metric, filters, dimensions and time phrase from the "
    "prior turn unless the new message overrides them (e.g. 'what about last "
    "month?' keeps the metric, changes the period to 'last month').\n"
    "- DETAIL FOLLOW-UPS ('show me details for it', 'more information on these "
    "incidents', 'why?'): the user means the records behind the previous answer. "
    "State the entity, the SAME filters and the SAME period explicitly, and when "
    "<last_result> lists record_ids, name them in the question (e.g. 'Show the "
    "detail columns for incidents INC0364440, INC0364512 (major incidents, "
    "priority P1, last month)'). Never drop the previous filters/period — without "
    "them the query matches nothing and the answer becomes 'no data'.\n"
    "- Keep any relative time phrase VERBATIM (e.g. 'last quarter', 'this year', "
    "'last quarter vs current quarter').\n"
    "- If the message is already self-contained, return it essentially unchanged.\n"
    "Output ONLY the rewritten question — no preamble, no quotes, no explanation."
)

# Agent identifiers used to namespace persisted state per request_uuid.
REPHRASE_AGENT = "question_rephrase_agent"
ANALYST_AGENT = "cora"

_tools_cache = {"tools": None}
_tools_lock = asyncio.Lock()


def _model_client() -> OpenAIChatCompletionClient:
    return OpenAIChatCompletionClient(
        model=os.getenv("CORA_LLM_MODEL", "gpt-4.1-mini"),
        api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


async def _get_tools():
    if _tools_cache["tools"] is None:
        async with _tools_lock:
            if _tools_cache["tools"] is None:
                params = StreamableHttpServerParams(url=CORA_MCP_URL)
                _tools_cache["tools"] = await mcp_server_tools(params)
                log.info("loaded %d MCP tools", len(_tools_cache["tools"]))
    return _tools_cache["tools"]


def _coerce(content) -> object:
    """Unwrap a tool result into a JSON value (see notes in the previous impl)."""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                texts.append(part["text"])
            elif hasattr(part, "text"):
                texts.append(part.text)
        if not texts:
            return content
        inner = "\n".join(texts)
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            return inner
    return content


def _extract(result) -> dict:
    """Pull final answer + tool outputs out of an autogen TaskResult."""
    answer = ""
    tool_outputs = []
    for msg in getattr(result, "messages", []) or []:
        cls = type(msg).__name__
        if cls == "ToolCallExecutionEvent":
            for r in getattr(msg, "content", []) or []:
                tool_outputs.append({
                    "name": getattr(r, "name", None),
                    "is_error": bool(getattr(r, "is_error", False)),
                    "result": _coerce(getattr(r, "content", None)),
                })
        elif cls == "TextMessage" and getattr(msg, "source", None) != "user":
            answer = getattr(msg, "content", "") or answer
    return {"answer": answer, "tool_outputs": tool_outputs}


def _last_text(result) -> str:
    text = ""
    for msg in getattr(result, "messages", []) or []:
        if type(msg).__name__ == "TextMessage" and getattr(msg, "source", None) != "user":
            text = getattr(msg, "content", "") or text
    return text


def _usage(msg) -> tuple[int, int]:
    u = getattr(msg, "models_usage", None)
    if not u:
        return 0, 0
    return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)


def _extract_response(response) -> dict:
    """Pull final answer, tool outputs and token usage out of an autogen
    ``Response`` (the return type of ``agent.on_messages``)."""
    answer = ""
    tool_outputs = []
    prompt_tokens = completion_tokens = 0
    for msg in getattr(response, "inner_messages", []) or []:
        p, c = _usage(msg)
        prompt_tokens += p
        completion_tokens += c
        if type(msg).__name__ == "ToolCallExecutionEvent":
            for r in getattr(msg, "content", []) or []:
                tool_outputs.append({
                    "name": getattr(r, "name", None),
                    "is_error": bool(getattr(r, "is_error", False)),
                    "result": _coerce(getattr(r, "content", None)),
                })
    final = getattr(response, "chat_message", None)
    if final is not None:
        p, c = _usage(final)
        prompt_tokens += p
        completion_tokens += c
        answer = getattr(final, "content", "") or ""
    return {
        "answer": answer,
        "tool_outputs": tool_outputs,
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
    }


def _executed_queries(response) -> list[dict]:
    """Flatten an autogen ``Response`` into just the executed query(ies) and
    their raw results — dropping the ToolCallExecutionEvent / FunctionExecutionResult
    wrappers.

    For ``run_kpi`` each executed window/bucket becomes one ``{query, rows}``
    entry (so a monthly series shows one query+result per month); ``query_dataset``
    and other tools yield a single query+result. Tools without SQL (e.g.
    search_kpis) return their coerced payload under ``result``.
    """
    out: list[dict] = []
    for msg in getattr(response, "inner_messages", []) or []:
        if type(msg).__name__ != "ToolCallExecutionEvent":
            continue
        for r in getattr(msg, "content", []) or []:
            name = getattr(r, "name", None)
            is_error = bool(getattr(r, "is_error", False))
            payload = _coerce(getattr(r, "content", None))
            if isinstance(payload, dict) and isinstance(payload.get("results"), list):
                for res in payload["results"]:                    # run_kpi (per window)
                    out.append({
                        "tool": name, "kpi": payload.get("kpi"),
                        "label": res.get("label"),
                        "query": res.get("preview") or res.get("sql"),
                        "rows": res.get("rows", res.get("error")),
                    })
            elif isinstance(payload, dict) and ("sql" in payload or "rows" in payload):
                out.append({                                       # query_dataset / ad-hoc
                    "tool": name,
                    "query": payload.get("preview") or payload.get("sql"),
                    "rows": payload.get("rows", payload.get("error")),
                })
            else:
                out.append({"tool": name, "is_error": is_error, "result": payload})
    return out


def _is_rejected_tool_entry(entry: dict) -> bool:
    """True if a flattened tool entry is a guardrail rejection / error the
    analyst recovered from (e.g. an unknown-column or no-join-path rejection).

    These are useful in the server logs but are noise to the end user — the
    agent retries and the final answer comes from the successful calls — so we
    hide them from the ``executed_queries`` / ``tool_outputs`` SSE payloads."""
    if entry.get("is_error"):
        return True
    # executed_queries stores an error string under "rows" when a query had no rows
    if isinstance(entry.get("rows"), str):
        return True
    res = entry.get("result")
    if isinstance(res, dict) and "error" in res and not res.get("rows") and not res.get("results"):
        return True
    return False


# ---------------------------------------------------------------------------
# Turn context — what the PREVIOUS turn actually queried and returned.
#
# The rephrase agent used to see only the questions and the prose summary, so a
# follow-up like "show me details for those incidents" had nothing concrete to
# resolve: the rewritten question dropped the filters/period (or the record ids)
# and the analyst then queried something that matched nothing -> "No data
# available". This captures the facts of the turn (tool, metric/entity, applied
# filters, resolved window, dimension, row count, the ids/labels that came back)
# and replays them into the next rephrase as ground truth.
# ---------------------------------------------------------------------------
TURN_CONTEXT_KEY = "turn_context"
_RECORD_ID_RE = re.compile(r"^[A-Z]{2,6}\d{4,}$")
_MAX_IDS = 25

_CONTEXT_TOOLS = ("run_kpi", "query_dataset", "overview_module", "get_record")


def _row_ids(rows: list) -> list[str]:
    """Record identifiers present in result rows (INC…/CHG…/RITM… style), in order."""
    out: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if not isinstance(value, str) or not _RECORD_ID_RE.match(value.strip()):
                continue
            if not (key.endswith("_id") or key.endswith("_number") or key == "id"):
                continue
            v = value.strip()
            if v not in out:
                out.append(v)
            break                      # one id per row is enough to identify it
        if len(out) >= _MAX_IDS:
            break
    return out


def _group_labels(rows: list) -> list[str]:
    """Breakdown labels from grouped rows ({grp: 'FINANCE', v: 12})."""
    out: list[str] = []
    for row in rows or []:
        if isinstance(row, dict) and "grp" in row:
            label = row["grp"]
            label = label[0] if isinstance(label, list) and label else label
            if label is not None and str(label) not in out:
                out.append(str(label))
    return out[:_MAX_IDS]


def _turn_context(question: str, tool_outputs: list, answer: str | None) -> dict | None:
    """Compact, machine-readable record of this turn's data access (or None)."""
    ctx: dict = {"question": question}
    if answer:
        ctx["answer"] = answer[:1200]
    entries = []
    for t in tool_outputs or []:
        name, res = t.get("name"), t.get("result")
        if name not in _CONTEXT_TOOLS or not isinstance(res, dict):
            continue
        results = res.get("results") if isinstance(res.get("results"), list) else []
        rows: list = list(res.get("rows") or [])
        for sub in results:
            rows += list(sub.get("rows") or [])
        entry = {
            "tool": name,
            "metric": res.get("kpi"),
            "title": res.get("title"),
            "entity": res.get("entity") or res.get("base_table"),
            "record_id": res.get("record_id"),
            "mode": res.get("mode"),
            "dimension": res.get("dimension"),
            "filters": res.get("filters") or res.get("requested_filters"),
            "period": (res.get("resolved_from_phrase") or {}).get("phrase"),
            "window": res.get("comparison_windows") or (res.get("date_window") or {
                k: (res.get("resolved_from_phrase") or {}).get(k)
                for k in ("start_date", "end_date")}),
            "rowcount": len(rows),
        }
        ids, labels = _row_ids(rows), _group_labels(rows)
        if ids:
            entry["record_ids"] = ids
        if labels:
            entry["group_labels"] = labels
        if res.get("metrics"):                        # overview_module rollup
            entry["metrics"] = [m.get("kpi") for m in res["metrics"]][:20]
        entries.append({k: v for k, v in entry.items() if v not in (None, {}, [])})
    if not entries:
        return ctx if answer else None
    ctx["data"] = entries[-3:]                        # the last few calls carry it
    return ctx


def _with_last_result(content: str, last: dict | None) -> str:
    """Append the previous turn's context to the rephraser's system message."""
    if not last:
        return content
    block = json.dumps(last, default=str)[:4000]
    return (f"{content}\n\nPREVIOUS TURN (ground truth for resolving this "
            f"follow-up):\n<last_result>\n{block}\n</last_result>")


async def _summarize(question: str, tool_outputs: list) -> str | None:
    """Second agent: summarise the executed SQL rows in plain language."""
    kpi_results = [t["result"] for t in tool_outputs
                   if t.get("name") in ("run_kpi", "query_dataset", "overview_module", "get_record")
                   and isinstance(t.get("result"), dict)
                   and ("results" in t["result"] or "rows" in t["result"]
                        or "metrics" in t["result"])]
    if not kpi_results:
        return None
    # Keep the payload compact for the summariser (larger cap so a full module
    # overview — up to ~12 KPIs with value/unit/status/delta — isn't truncated).
    compact = json.dumps(kpi_results, default=str)[:9000]
    summarizer = AssistantAgent(
        name="summarizer", model_client=_model_client(), system_message=_SUMMARY_SYSTEM)
    task = (f"Question: {question}\n\nExecuted result(s):\n{compact}\n\n"
            f"Write the summary now.")
    try:
        resp = await summarizer.on_messages(
            messages=[TextMessage(content=task, source="user")],
            cancellation_token=CancellationToken())
        return getattr(resp.chat_message, "content", None)
    except Exception as exc:  # pragma: no cover - summary is best-effort
        log.warning("summarizer failed: %s", exc)
        return None


async def get_state_from_redis(request_uuid: str, agent_name: str | None = None):
    """Load a persisted agent state for this uuid/agent (None if absent)."""
    return await get_store().load(request_uuid, agent_name=agent_name)


async def get_agent(
    *,
    name: str,
    system_message: str,
    description: str | None = None,
    tools=None,
    request_uuid: str | None = None,
    agent_name: str | None = None,
    load_state: bool = True,
    reflect_on_tool_use: bool = False,
) -> AssistantAgent:
    """Build an AssistantAgent and (optionally) restore its state from Redis.

    Mirrors the production ``get_agent`` pattern: buffered context, optional
    tools, and per-``request_uuid``/``agent_name`` state hydration.

    ``reflect_on_tool_use`` defaults to False: after the tool loop the agent does
    NOT make an extra LLM call to narrate the results — the dedicated summarizer
    (step 3) owns the user-facing text, so that reflection call would be wasted.
    Tool chaining (search_kpis -> run_kpi) is governed by ``max_tool_iterations``,
    not by this flag, so it is unaffected.
    """
    agent = AssistantAgent(
        name=name,
        model_client=_model_client(),
        tools=tools or [],
        reflect_on_tool_use=reflect_on_tool_use,
        max_tool_iterations=8,
        model_context=BufferedChatCompletionContext(buffer_size=40),
        system_message=system_message,
        description=description,
    )
    if load_state and request_uuid:
        state = await get_state_from_redis(request_uuid, agent_name=agent_name)
        if state:
            try:
                await agent.load_state(state)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("could not load state for %s/%s: %s",
                            agent_name, request_uuid, exc)
    return agent


def save_agent_state(request_uuid: str, agent: AssistantAgent, agent_name: str) -> None:
    """Fire-and-forget persistence of an agent's state (background task)."""

    async def _do_save() -> None:
        state = await agent.save_state()
        await get_store().save(request_uuid, state, agent_name=agent_name)

    task = asyncio.create_task(_do_save())

    def _log_task_result(ta: asyncio.Task) -> None:
        try:
            ta.result()
        except Exception as exc:
            log.error("error saving state for %s in background: %s", agent_name, exc)

    task.add_done_callback(_log_task_result)


async def _rephrase(message: TextMessage, request_uuid: str) -> tuple[str, AssistantAgent]:
    """Rewrite a follow-up into a self-contained question using prior context.

    The previous turn's :func:`_turn_context` (what was queried, with which filters
    and window, and which records came back) is attached to the message as a
    ``<last_result>`` block, so a follow-up like "details for those" is rewritten
    against facts rather than against the prose summary alone.

    Returns the rephrased question and the (state-loaded) rephrase agent so the
    caller can append the answer summary before persisting its state.
    """
    raw = message.content
    last = await get_state_from_redis(request_uuid, agent_name=TURN_CONTEXT_KEY)
    if last:
        log.info("rephrase: carrying last-turn context (%s)",
                 [d.get("tool") for d in (last.get("data") or [])] or "answer only")
    # The block rides on the SYSTEM message, not the conversation: the agent is
    # rebuilt every turn, so only the freshest turn context is ever in play (a
    # user-message block would accumulate one stale copy per turn in the history).
    agent = await get_agent(
        name=REPHRASE_AGENT,
        system_message=_with_last_result(_REPHRASE_SYSTEM, last),
        description="Rewrites follow-up messages into self-contained KPI questions.",
        request_uuid=request_uuid,
        agent_name=REPHRASE_AGENT,
        load_state=True,
    )
    try:
        resp = await agent.on_messages(
            messages=[message], cancellation_token=CancellationToken())
        rewritten = (getattr(resp.chat_message, "content", "") or "").strip()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("rephrase failed, using raw question: %s", exc)
        return raw, agent
    return (rewritten or raw), agent


async def _remember_answer(agent: AssistantAgent, summary: str | None) -> None:
    """Feed the answer summary into the rephrase agent's context so future
    follow-ups can resolve answer-dependent references ('why was it high')."""
    if not summary:
        return
    try:
        await agent.model_context.add_message(
            UserMessage(content=f"(answer summary: {summary})", source="user")
        )
    except Exception as exc:  # pragma: no cover - best-effort
        log.warning("could not append answer summary to rephrase context: %s", exc)


async def _remember_turn(request_uuid: str, question: str, tool_outputs: list,
                         answer: str | None) -> None:
    """Persist this turn's data-access facts for the NEXT turn's rephrase.

    Kept separate from the agent state so it survives a rephrase-state failure,
    and stored even when the summarizer produced nothing (a turn with rows but no
    summary is exactly the one a follow-up needs to lean on)."""
    ctx = _turn_context(question, tool_outputs, answer)
    if not ctx:
        return
    try:
        await get_store().save(request_uuid, ctx, agent_name=TURN_CONTEXT_KEY)
    except Exception as exc:  # pragma: no cover - best-effort
        log.warning("could not save turn context for %s: %s", request_uuid, exc)


app = FastAPI(title="everestdx-itsm-mcp-service")


class ChatMessageIn(BaseModel):
    """A serialized autogen ``TextMessage`` as sent by the client."""
    content: str
    metadata: dict = Field(default_factory=dict)
    models_usage: dict | None = None
    source: str = "user"
    type: str = "TextMessage"


class AskRequest(BaseModel):
    message: ChatMessageIn
    request_uuid: str | None = None


def _sse(obj) -> str:
    """Encode one Server-Sent Event frame."""
    return f"data: {json.dumps(obj, default=str)}\n\n"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HTML_PATH)


@app.post("/api/new_chat")
async def new_chat() -> JSONResponse:
    ruid = str(uuidlib.uuid4())
    log.info("new chat: uuid=%s", ruid)
    return JSONResponse({"request_uuid": ruid})


@app.post("/api/ask")
async def ask(req: AskRequest) -> StreamingResponse:
    """Run the rephrase -> analyst -> summarize pipeline, streaming each stage
    as Server-Sent Events. Accepts an autogen ChatMessage as ``message``."""
    ruid = req.request_uuid or str(uuidlib.uuid4())
    user_msg = TextMessage(content=req.message.content, source=req.message.source or "user")

    async def gen():
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        yield _sse({"content": {"test_sse": "Session initialized..."}, "branch_id": "orchestrator"})
        yield _sse({"content": {"request_uuid": ruid}})

        if not user_msg.content.strip():
            yield _sse({"content": {"error": "empty question"}})
            yield _sse({"content": "complete"})
            return

        log.info("ask: uuid=%s q=%r", ruid, user_msg.content)

        # MCP tools
        try:
            ts = time.perf_counter()
            tools = await _get_tools()
            timings["get_tools"] = round(time.perf_counter() - ts, 4)
        except Exception as exc:
            yield _sse({"content": {"error":
                        f"cannot reach MCP server at {CORA_MCP_URL}: {exc}. "
                        f"Start it with `python -m cora_mcp.server`."}})
            yield _sse({"content": "complete"})
            return

        today = date.today().isoformat()

        # 1) Rephrase (stateful): follow-up -> self-contained question.
        ts = time.perf_counter()
        rephrased, rephrase_agent = await _rephrase(user_msg, ruid)
        timings["rephrase_agent"] = round(time.perf_counter() - ts, 4)
        if rephrased != user_msg.content:
            log.info("ask: uuid=%s rephrased -> %r", ruid, rephrased)
        yield _sse({"content": {"rephrased_question": rephrased}})

        # 2) Analyst (stateless): answer via MCP tools using on_messages.
        analyst = await get_agent(
            name=ANALYST_AGENT,
            system_message=f"Today's date is {today}.\n{_ANALYST_SYSTEM}",
            description="everestdx ITSM analyst over the CORA MCP catalog.",
            tools=tools,
            load_state=False,
        )
        try:
            ts = time.perf_counter()
            response = await analyst.on_messages(
                messages=[TextMessage(content=rephrased, source="user")],
                cancellation_token=CancellationToken())
            timings["analyst_agent"] = round(time.perf_counter() - ts, 4)


            log.debug(f" response: \n {response} \n",)

            executed = _executed_queries(response)
            for q in executed:
                if "query" in q:
                    log.info("executed %s %s\n  query: %s\n  rows : %s",
                             q.get("tool"), q.get("label") or "", q.get("query"), q.get("rows"))
                else:
                    log.info("tool %s -> %s", q.get("tool"), q.get("result"))
        except Exception as exc:
            yield _sse({"content": {"error": f"agent error: {exc}"}})
            yield _sse({"content": "complete"})
            return

        parsed = _extract_response(response)

        # 3) Summarize the executed rows.
        ts = time.perf_counter()
        summary = await _summarize(rephrased, parsed["tool_outputs"])
        timings["summarizer"] = round(time.perf_counter() - ts, 4)

        chat_message = summary or parsed["answer"] or "(no response)"
        yield _sse({"content": {"chat_message": chat_message}})
        # Clean view: executed query(ies) + raw rows only (no FunctionExecutionResult
        # wrappers). `tool_outputs` is kept for the existing UI table rendering.
        # Drop guardrail-rejected intermediate calls (unknown-column / no-join-path
        # errors the analyst recovered from) so they don't surface as UI warnings;
        # they remain in the server logs above.
        ui_executed = [q for q in executed if not _is_rejected_tool_entry(q)]
        ui_tool_outputs = [t for t in parsed["tool_outputs"] if not _is_rejected_tool_entry(t)]
        if ui_executed:
            yield _sse({"content": {"executed_queries": ui_executed}})
        if ui_tool_outputs:
            yield _sse({"content": {"tool_outputs": ui_tool_outputs}})
        yield _sse({"content": {"input_tokens": parsed["input_tokens"],
                                "output_tokens": parsed["output_tokens"]}})

        # 4) Feed the summary back into the rephrase context; persist in bg.
        await _remember_answer(rephrase_agent, summary)
        save_agent_state(ruid, rephrase_agent, REPHRASE_AGENT)
        # 5) Record WHAT was queried/returned so the next turn's rephrase can
        #    resolve "details for those" against facts, not prose.
        await _remember_turn(ruid, rephrased, parsed["tool_outputs"],
                             summary or parsed["answer"])

        total = round(time.perf_counter() - t0, 4)
        yield _sse({"content": {"debug_query": {"execution_timings": {
            "total_execution_time": total, "individual_timings": timings}}}})
        yield _sse({"content": "complete"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def main() -> None:
    print(f"[web] everestdx-itsm-mcp-service on http://{WEB_HOST}:{WEB_PORT}  (MCP: {CORA_MCP_URL})")
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="info")


if __name__ == "__main__":
    main()
