"""CORA MCP — a FastMCP service and catalog layer over gen_query.py.

Modules
-------
- logging_config : central logging setup used by every module.
- date_resolver  : deterministic natural-language date-window resolution.
- schema_loader  : loads the rich schema_v3.yaml and indexes modules/entities.
- kpi_catalog    : loads config/*.json and provides KPI search.
- query_engine   : bridges to gen_query.py to emit final SQL for a KPI + window.
- tools          : registers module/entity/action tools on a FastMCP instance.
- server         : FastMCP entrypoint (streamable-http).
"""

__version__ = "0.1.0"
