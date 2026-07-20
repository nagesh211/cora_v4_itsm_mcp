"""No-LLM smoke test for the CORA MCP server.

Connects over Streamable HTTP, lists the tools, then exercises the core action
tools directly (no model, no API key required). Use this to verify the server
end-to-end before pointing an agent at it.

Run:
    python -m cora_mcp.server          # in one terminal
    python clients/smoke_test.py       # in another
"""
from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

CORA_MCP_URL = os.getenv("CORA_MCP_URL", "http://localhost:8081/mcp")


def _unwrap(result) -> object:
    """Pull a JSON-friendly value out of a CallToolResult."""
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        # FastMCP wraps non-dict returns under {"result": ...}.
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    parts = []
    for c in result.content or []:
        text = getattr(c, "text", None)
        if text is not None:
            try:
                parts.append(json.loads(text))
            except json.JSONDecodeError:
                parts.append(text)
    return parts[0] if len(parts) == 1 else parts


def _show(title: str, value: object, limit: int = 900) -> None:
    print(f"\n=== {title} ===")
    text = json.dumps(value, indent=2, default=str)
    print(text if len(text) <= limit else text[:limit] + "\n... (truncated)")


async def main() -> None:
    print(f"[smoke] connecting to {CORA_MCP_URL}")
    async with streamablehttp_client(CORA_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            print(f"[smoke] {len(tools)} tools available")
            print("  tools  :", [t.name for t in tools])

            async def call(name, args=None):
                return _unwrap(await session.call_tool(name, args or {}))

            _show("resolve_dates('last 3 months')", await call("resolve_dates", {"period": "last 3 months"}))
            _show("resolve_dates('between 2025-06-10 and 2025-08-15')",
                  await call("resolve_dates", {"period": "between 2025-06-10 and 2025-08-15"}))

            mods = await call("list_modules")
            print("\n=== list_modules ===")
            for m in mods:
                print(f"  {m['name']:<18} {m['database_type']:<10} "
                      f"{len(m['entities'])} entities")

            _show("search_kpis('emergency change lead time')",
                  await call("search_kpis", {"query": "emergency change lead time in days"}))

            _show("describe_dataset('itsm_change') (roles summary)",
                  {k: v for k, v in (await call("describe_dataset",
                                                {"dataset": "itsm_change"})).items()
                   if k in ("module", "database_type", "dimensions", "measures",
                            "timestamps", "related_kpis")})

            _show("generate_query emergency / last quarter / table by region",
                  await call("generate_query", {
                      "kpi": "emergency", "period": "last quarter",
                      "mode": "table", "dim": "region"}))

            _show("generate_query cm-major-incident (SQL mode) / sector=CGF",
                  await call("generate_query", {
                      "kpi": "cm-major-incident", "period": "this year",
                      "mode": "stat", "filters": {"sector": "CGF"}}))

            _show("overview_module('availability') filtered by sector=CGF",
                  await call("overview_module", {
                      "module": "availability", "filters": {"sector": "CGF"}}))

    print("\n[smoke] OK")


if __name__ == "__main__":
    asyncio.run(main())
