"""Structured JSON logging configuration for parent-side components.

Provides a JSONFormatter and setup_logging() callable from both the
FastAPI API and the Celery worker.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Structured extra fields
        for key in ("correlation_id", "tenant_id", "stage", "duration_ms", "service", "status_code"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value

        # Exception info
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(entry, default=str)


def setup_logging(service_name: str = "api") -> logging.Logger:
    """Configure the root logger with JSONFormatter on stdout.

    Args:
        service_name: Identifier added to every log entry (e.g. "api", "worker").

    Returns:
        A named logger for the given service.
    """
    root = logging.getLogger()
    # Avoid adding duplicate handlers on repeated calls
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JSONFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    logger = logging.getLogger(service_name)
    # Attach default service name via a filter
    _filter = _ServiceFilter(service_name)
    if not any(isinstance(f, _ServiceFilter) for f in logger.filters):
        logger.addFilter(_filter)

    return logger


class _ServiceFilter(logging.Filter):
    """Injects 'service' into every log record for a given logger."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service_name  # type: ignore[attr-defined]
        return True
