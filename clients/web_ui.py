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

CORA_MCP_URL = os.getenv("CORA_MCP_URL", "http://localhost:8029/mcp")
WEB_HOST = os.getenv("CORA_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("CORA_WEB_PORT", "8090"))
HTML_PATH = Path(__file__).resolve().parent / "web" / "index.html"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-optx-660cfb55f6436276e148a5727cdc67865115917581c2f68d")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://fluxlm.everestdx.com/llm-gw/api/v1/")

_ANALYST_SYSTEM = (
    "You are the everestdx ITSM analyst. Use the CORA MCP tools; NEVER invent SQL "
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
    "unless the user names a code am/cm/em/im/pm/rm/sd/sr).\n"
    "2. GOVERNED value: if a KPI clearly matches and the user wants that standard "
    "metric, call run_kpi. Choose `mode` by what the user wants:\n"
    "   - TREND / OVER-TIME / 'trended' / 'month over month' / any multi-period "
    "window ('last 3 months', 'last quarter', 'this year'): use mode='series'. Do "
    "NOT set grain — the server derives it from the DURATION (weekly for ~a month, "
    "monthly for several months, quarterly for multi-year), so 'trend for last "
    "month' comes back WEEKLY automatically. Never return one blended number for a "
    "trend.\n"
    "   - A TREND BROKEN DOWN by a dimension ('nps trend by business', 'incidents "
    "over time by region and priority'): STILL use mode='series' and ALSO pass "
    "`dim` (a field or a list). Each period returns a value PER dimension group — "
    "do NOT drop the breakdown and do NOT switch to mode='table' for a trend.\n"
    "   - SINGLE snapshot value (no trend, one window — 'nps this month', 'today'): "
    "use mode='stat'.\n"
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
    "3. OVERVIEW / broad 'what's happening in <module> [for <sector/region>]': call "
    "overview_module with module (a code am/cm/em/im/pm/rm/sd/sr OR a phrase like "
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
    "valid slugs are itsm_incident, itsm_change, itsm_problem, …) and use ONLY the "
    "exact column names it returns — NEVER guess or invent a column (there is no "
    "'opened_at', 'short_description', 'state', 'created_at' unless the schema lists "
    "it; the incident time column is 'open_date_time'). Note each column's `table`: a "
    "column is only usable if its table is the base or is joined via join_with.\n"
    "   CHOOSE THE SHAPE by what the user wants:\n"
    "   - They want to SEE/LIST records ('details of…', 'list…', 'show me the "
    "incidents…'): pass select=[columns to display]. This returns raw detail rows — "
    "NO count, NO group-by. Add filters/period/join_with to scope it. Do NOT pass "
    "dimensions for a listing.\n"
    "   - They want a NUMBER ('how many', 'count', 'sum', 'average', 'trend'): pass "
    "measure {agg,column} (omit for count) and/or dimensions (group-by) and/or grain "
    "(series). \n"
    "   To keep a governed metric's table+measure but add your own filters/dims, pass "
    "metric=<kpi name>. If a tool returns a column error with 'did you mean' or a NOTE "
    "about another table, follow that hint on the NEXT call — do not keep guessing.\n"
    "5. CROSS-ENTITY (e.g. incidents caused by changes, problems linked to incidents): "
    "query_dataset with join_with=[other entity]; the server plans the join from "
    "declared relationships (see list_relationships). Keep join_type='inner' to "
    "restrict to related records.\n"
    "6. DRILL-DOWN / 'why / reason behind X': query_dataset with drilldown="
    "{detail_columns:[reason/detail columns], entity_filter:{field:<id col>, op:'=', "
    "values:[<the specific id>]}}, using the id from earlier in the conversation.\n\n"
    "If a tool returns an `error`, report it and show the SQL. Answer concisely with "
    "the key number(s).\n"
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
    "Rules:\n"
    "- Resolve references ('that', 'it', 'those', 'the same') to the concrete "
    "metric/entity from earlier.\n"
    "- Carry forward the metric, filters, dimensions and time phrase from the "
    "prior turn unless the new message overrides them (e.g. 'what about last "
    "month?' keeps the metric, changes the period to 'last month').\n"
    "- Keep any relative time phrase VERBATIM (e.g. 'last quarter', 'this year').\n"
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

    Returns the rephrased question and the (state-loaded) rephrase agent so the
    caller can append the answer summary before persisting its state.
    """
    agent = await get_agent(
        name=REPHRASE_AGENT,
        system_message=_REPHRASE_SYSTEM,
        description="Rewrites follow-up messages into self-contained KPI questions.",
        request_uuid=request_uuid,
        agent_name=REPHRASE_AGENT,
        load_state=True,
    )
    raw = message.content
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
