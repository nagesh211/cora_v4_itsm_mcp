"""Central logging configuration for the CORA MCP service.

Every module does ``from cora_mcp.logging_config import get_logger`` and then
``log = get_logger(__name__)``. Call :func:`setup_logging` once at process
start (the server and the CLI entrypoints do this); importing a module and
grabbing a logger is always safe even if setup has not run yet.

Level is controlled by the ``CORA_LOG_LEVEL`` env var (default ``INFO``).

Logs are written to stderr *and* to a daily file under a root-level ``logs/``
folder (``logs/cora-YYYY-MM-DD.log``). The folder is created on first use and a
new file is opened each day. Override the directory with ``CORA_LOG_DIR``.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Project root = parent of the ``cora_mcp`` package directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _log_dir() -> Path:
    """Resolve (and create) the directory that holds the daily log files."""
    env = os.getenv("CORA_LOG_DIR")
    directory = Path(env) if env else _PROJECT_ROOT / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _file_handler() -> logging.Handler:
    """A file handler whose name is the current day, rolling over at midnight.

    The active file is ``logs/cora-YYYY-MM-DD.log`` for today. At midnight the
    handler rotates and opens a fresh dated file for the new day.
    """
    today = _dt.date.today().isoformat()
    path = _log_dir() / f"cora-{today}.log"
    handler = TimedRotatingFileHandler(
        path, when="midnight", backupCount=0, encoding="utf-8"
    )
    # Rotated files keep the ``cora-<date>.log`` shape rather than the default
    # ``cora-<date>.log.<suffix>``.
    handler.namer = lambda default: str(
        _log_dir() / f"cora-{Path(default).name.rsplit('.', 1)[-1]}.log"
    )
    return handler


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger exactly once (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = (level or os.getenv("CORA_LOG_LEVEL", "INFO")).upper()
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    root = logging.getLogger()
    root.setLevel(getattr(logging, lvl, logging.INFO))

    # Console handler (stderr) — avoid duplicating if already configured.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    else:
        for h in root.handlers:
            h.setFormatter(fmt)

    # Daily file handler — best effort; never break the app on a fs error.
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        try:
            fh = _file_handler()
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:  # pragma: no cover - defensive
            root.warning("file logging disabled: %s", exc)

    # Quiet opensearch-py's per-request INFO lines (one per HTTP call, e.g.
    # ``POST .../_search [status:200 request:0.915s]``); keep warnings/errors.
    logging.getLogger("opensearch").setLevel(logging.WARNING)

    _CONFIGURED = True
    root.debug("logging configured at level %s", lvl)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, ensuring logging is configured first."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
