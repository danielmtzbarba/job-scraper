"""Structured stdout logging shared by the API and background workers."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

import structlog


_SENSITIVE_PARTS = (
    "password",
    "token",
    "secret",
    "api_key",
    "authorization",
    "credential",
    "cookie",
)
_LEVEL_COLORS = {
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_KEY_COLOR = "\033[96m"
_RESET = "\033[0m"


class KeyValueConsoleRenderer:
    """Render one compact, colorized key/value log line."""

    def __call__(self, _logger: Any, _method_name: str, event_dict: dict[str, Any]) -> str:
        values = dict(event_dict)
        level = str(values.pop("level", "INFO")).upper()
        timestamp = _local_timestamp(values.pop("timestamp", None))
        event = str(values.pop("event", "log_event"))
        values.pop("logger", None)

        rendered_fields = " ".join(
            f"{_KEY_COLOR}{key}{_RESET}={_format_value(value)}"
            for key, value in values.items()
        )
        level_color = _LEVEL_COLORS.get(level, _RESET)
        prefix = (
            f"{level_color}[ {level} ]{_RESET} : "
            f"{timestamp} : "
            f"{_KEY_COLOR}[ {event} ]{_RESET}"
        )
        return f"{prefix} : {rendered_fields}" if rendered_fields else f"{prefix} :"


def _local_timestamp(value: Any) -> str:
    if not value:
        timestamp = datetime.now().astimezone()
    else:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            timestamp = timestamp.astimezone()
        except ValueError:
            return str(value)
    milliseconds = timestamp.microsecond // 1000
    return f"{timestamp:%d.%m.%Y %H:%M:%S}.{milliseconds:03d}"


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        if value and not any(char.isspace() for char in value) and "=" not in value:
            return value
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, default=str)


def _scrub_sensitive_fields(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    for key in event_dict:
        if any(part in key.lower() for part in _SENSITIVE_PARTS):
            event_dict[key] = "[REDACTED]"
    return event_dict


def setup_logging(service_name: str, level: int = logging.INFO) -> Any:
    """Configure structured logs; ENV=production selects JSON output."""
    production = os.getenv("ENV", "development").lower() == "production"
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=level, force=True
    )
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        _scrub_sensitive_fields,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if production
        else KeyValueConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    for logger_name in ("httpx", "httpcore", "uvicorn.access"):
        dependency_logger = logging.getLogger(logger_name)
        dependency_logger.setLevel(logging.WARNING)
    return structlog.get_logger(service_name)
