"""CORA MCP server — FastMCP over gen_query.py + the schema catalog.

Run:
    python -m cora_mcp.server

Serves Streamable HTTP at ``http://<host>:<port>/mcp`` (defaults
``0.0.0.0:8081``), matching the test client's StreamableHttpServerParams.

Env:
    CORA_MCP_HOST   (default 0.0.0.0)
    CORA_MCP_PORT   (default 8081)
    CORA_LOG_LEVEL  (default INFO)
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from cora_mcp.logging_config import get_logger, setup_logging
from cora_mcp.tools import register_tools

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)

log = get_logger("cora_mcp.server")


def build_server(host: str | None = None, port: int | None = None) -> tuple[FastMCP, int]:
    host = host or os.getenv("CORA_MCP_HOST", "0.0.0.0")
    port = int(port or os.getenv("CORA_MCP_PORT", "8081"))
    mcp = FastMCP("cora", host=host, port=port)
    n = register_tools(mcp)
    return mcp, n


def main(host: str | None = None, port: int | None = None) -> None:
    setup_logging()
    mcp, n = build_server(host, port)
    log.info("CORA MCP starting: %d tools on http://%s:%s%s",
             n, mcp.settings.host, mcp.settings.port, mcp.settings.streamable_http_path)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
