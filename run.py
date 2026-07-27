"""Entry point for the CORA MCP service.

Run:
    python run.py

Env (from .env):
    CORA_MCP_HOST   (default 0.0.0.0)
    CORA_MCP_PORT   (default 8081)
"""
import os

from dotenv import load_dotenv

try:
    from cora_mcp.server import main
except Exception as e:
    print(f"SOME MODULES ARE MISSING, {e} at {e.__traceback__.tb_lineno}")
    exit(1)

if __name__ == "__main__":
    load_dotenv()
    host = os.getenv("CORA_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("CORA_MCP_PORT", "8081"))
    main(host, port)