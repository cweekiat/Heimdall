"""Structured JSON logging (structlog) and Prometheus metrics."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog
from prometheus_client import Counter, start_http_server

RECORDS_IN = Counter("heimdall_records_in_total", "Records ingested", ["source"])
DROPPED = Counter("heimdall_dropped_total", "Implausible records dropped", ["source"])
HTTP_ERRORS = Counter("heimdall_http_errors_total", "HTTP errors", ["source"])
CIRCUIT_OPEN = Counter("heimdall_circuit_open_total", "Circuit breaker openings", ["source"])


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "heimdall") -> Any:
    return structlog.get_logger(name)


def start_metrics_server(port: int = 9108, addr: str | None = None) -> None:
    """Serve Prometheus metrics. Binds loopback unless HEIMDALL_METRICS_HOST/addr overrides."""
    start_http_server(port, addr=addr or os.environ.get("HEIMDALL_METRICS_HOST", "127.0.0.1"))
