"""Structured logging for LaravelGraph.

Five log files:
  laravelgraph.log          — main operational log
  laravelgraph-mcp.log      — MCP request/response log
  laravelgraph-pipeline.log — detailed pipeline execution
  laravelgraph-performance.log — timing and memory metrics
  laravelgraph-errors.log   — errors and critical only
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

import structlog

_configured = False


def configure(log_level: str = "INFO", log_dir: Path | None = None) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
    )

    handlers: list[logging.Handler] = []

    # Console handler (INFO+, human-readable when TTY)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    if sys.stderr.isatty():
        console.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(colors=True),
            )
        )
    else:
        console.setFormatter(formatter)
    handlers.append(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)

        def _file_handler(name: str, lvl: int = level) -> logging.FileHandler:
            h = logging.FileHandler(log_dir / name, encoding="utf-8")
            h.setLevel(lvl)
            h.setFormatter(formatter)
            return h

        handlers.append(_file_handler("laravelgraph.log"))
        handlers.append(_file_handler("laravelgraph-pipeline.log"))
        handlers.append(_file_handler("laravelgraph-errors.log", logging.ERROR))
        handlers.append(_file_handler("laravelgraph-performance.log"))
        # MCP log — separate logger configured in mcp module

    root = logging.getLogger()
    root.setLevel(level)
    for h in handlers:
        root.addHandler(h)


def get_logger(name: str = "laravelgraph") -> structlog.stdlib.BoundLogger:
    configure()  # no-op if already configured
    return structlog.get_logger(name)


def get_mcp_logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("laravelgraph.mcp")


def get_pipeline_logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("laravelgraph.pipeline")


def get_perf_logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("laravelgraph.performance")


@contextmanager
def phase_timer(
    phase_name: str,
    logger: structlog.stdlib.BoundLogger | None = None,
    **extra: Any,
) -> Generator[dict[str, Any], None, None]:
    """Context manager that logs phase start/end with duration."""
    if logger is None:
        logger = get_pipeline_logger()

    ctx: dict[str, Any] = {}
    logger.info("Phase started", phase=phase_name, **extra)
    start = time.perf_counter()
    try:
        yield ctx
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        ctx["duration_ms"] = round(elapsed, 2)
        logger.info("Phase completed", phase=phase_name, **ctx, **extra)
        # Also log to performance channel
        get_perf_logger().info(
            "Phase timing",
            phase=phase_name,
            duration_ms=round(elapsed, 2),
            **extra,
        )
