"""Autogen 0.7.4 sample client against the CORA MCP server.

Run:
    pip install "autogen-agentchat>=0.7.4,<0.8" "autogen-ext[openai,mcp]>=0.7.4,<0.8"
    # set OPENAI_API_KEY / OPENAI_BASE_URL / CORA_LLM_MODEL (or use the defaults below)
    python -m cora_mcp.server           # start the server first
    python clients/autogen_client.py

Connects via Streamable HTTP to the CORA MCP server, fetches the live tool
list, hands it to a single AssistantAgent, and asks the sample questions.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load ../.env (project root) so OPENAI_* / Azure vars flow through automatically.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from autogen_agentchat.agents import AssistantAgent  # noqa: E402
from autogen_agentchat.ui import Console  # noqa: E402
from autogen_ext.models.openai import OpenAIChatCompletionClient  # noqa: E402
from autogen_ext.tools.mcp import (  # noqa: E402
    StreamableHttpServerParams,
    mcp_server_tools,
)

CORA_MCP_URL = os.getenv("CORA_MCP_URL", "http://localhost:8081/mcp")

SAMPLE_QUESTIONS = [
    "What is our average emergency change lead time in days for the last quarter?",
    "Show the emergency change lead time this month broken down by region.",
    "How have major incidents caused by changes trended this year?",
    "Which modules and datasets are available in the catalog?",
    "What is the incident MTTR year to date?",
]

# Defaults mirror the project's LLM gateway; override via env.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-optx-660cfb55f6436276e148a5727cdc67865115917581c2f68d")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://fluxlm.everestdx.com/llm-gw/api/v1/")


def _model_client() -> OpenAIChatCompletionClient:
    model = os.getenv("CORA_LLM_MODEL", "gpt-4.1-mini")
    return OpenAIChatCompletionClient(
        model=model, api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


async def main() -> None:
    server_params = StreamableHttpServerParams(url=CORA_MCP_URL)
    tools = await mcp_server_tools(server_params)
    print(f"[autogen] Loaded {len(tools)} tools from CORA MCP")

    today_str = date.today().isoformat()
    agent = AssistantAgent(
        name="cora_kpi_analyst",
        model_client=_model_client(),
        tools=tools,
        reflect_on_tool_use=True,
        max_tool_iterations=8,  # allow search_kpis -> run_kpi -> answer chaining
        system_message=(
            f"Today's date is {today_str}. Use it to interpret relative time phrases "
            f"('this year', 'last quarter', 'this month', 'next quarter', etc.).\n\n"
            "You are a CORA ITSM/FinOps analyst. The CORA MCP server gives you a live "
            "catalog of modules/datasets and a query generator over KPI configs. USE THE "
            "TOOLS — do not invent SQL or data.\n\n"
            "Workflow:\n"
            "  1. Call search_kpis with the question to find the right KPI `name`.\n"
            "  2. GOVERNED value: call run_kpi with that name to EXECUTE and get real "
            "numbers. Pass the time phrase VERBATIM as `period`, and `filters` "
            "{field:value} for constraints (sector, region, …). mode='series'+grain for "
            "trends. (mode='table' group-by is DSL-only; use query_dataset to break "
            "down a SQL-mode KPI.)\n"
            "     FILTERS accept aliases (see each KPI's `filter_aliases`): "
            "'business'/'p&l'->sector, 'sub business'/'division'->division, "
            "'team'->assignment_group, 'capability'->service_area. Pass the user's "
            "word as the filter key; the server resolves or rejects it. Don't guess a column.\n"
            "  2b. OVERVIEW ('what's happening in <module>' / 'insights on service desk "
            "for APAC'): call overview_module with module (a code from list_kpi_modules "
            "or a phrase) + optional period/filters — a rollup of the module's KPIs. For "
            "'<module> overview by <dimension>' pass dim=<word> (or a list) so each KPI "
            "returns a per-dimension breakdown; don't improvise per-KPI run_kpi calls.\n"
            "  3. AD-HOC (no matching KPI, or a filter/dimension the KPI lacks): call "
            "query_dataset with base (entity slug/table), measure {agg,column}, dimensions, "
            "filters [{field,op,values}], period. Discover columns via list_modules + "
            "describe_dataset(slug).\n"
            "  4. CROSS-ENTITY (incidents caused by changes, problems linked to incidents): "
            "query_dataset with join_with=[other entity] (join_type='inner'); see "
            "list_relationships.\n"
            "  5. DRILL-DOWN ('reason behind X'): query_dataset with drilldown="
            "{detail_columns:[...], entity_filter:{field,op,values}}.\n"
            "  Report the key number(s). On error, state it and show the SQL."
        ),
    )
    for q in SAMPLE_QUESTIONS:
        print(f"\n=== Q: {q}")
        await Console(agent.run_stream(task=q))


if __name__ == "__main__":
    asyncio.run(main())
