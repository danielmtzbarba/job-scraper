"""Structured stdout logging shared by the API and background workers."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any, TextIO

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
_DATE_COLOR = "\033[38;5;245m"
_TIME_COLOR = "\033[38;5;117m"
_KEY_COLOR = _TIME_COLOR
_RESET = "\033[0m"
_MCP_SESSION_MESSAGES = (
    (re.compile(r"^Rejected request with unknown or expired session ID: (.+)$"), "mcp_session_rejected"),
    (re.compile(r"^Created new transport with session ID: (.+)$"), "mcp_session_created"),
    (re.compile(r"^Terminating session: (.+)$"), "mcp_session_terminated"),
)
_MCP_TRANSPORT_LOGGERS = (
    "mcp.server.streamable_http_manager",
    "mcp.server.streamable_http",
)


class KeyValueConsoleRenderer:
    """Render one compact, colorized key/value log line."""

    def __call__(self, _logger: Any, _method_name: str, event_dict: dict[str, Any]) -> str:
        values = dict(event_dict)
        level = str(values.pop("level", "INFO")).upper()
        timestamp = _local_timestamp(values.pop("timestamp", None))
        date, separator, time = timestamp.partition(" ")
        colored_timestamp = (
            f"{_DATE_COLOR}{date}{_RESET} "
            f"{_TIME_COLOR}{time}{_RESET}"
            if separator
            else timestamp
        )
        event = str(values.pop("event", "log_event"))
        values.pop("logger", None)

        rendered_fields = " ".join(
            f"{_KEY_COLOR}{key}{_RESET}={_format_value(value)}"
            for key, value in values.items()
        )
        level_color = _LEVEL_COLORS.get(level, _RESET)
        prefix = (
            f"{level_color}[ {level} ]{_RESET} : "
            f"{colored_timestamp} : "
            f"{level_color}[ {event} ]{_RESET}"
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


class MCPTransportFormatter(logging.Formatter):
    """Render MCP SDK transport messages with the application's log format."""

    def __init__(self, *, production: bool) -> None:
        super().__init__()
        self.production = production
        self.console_renderer = KeyValueConsoleRenderer()

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        event_dict: dict[str, Any] = {
            "level": record.levelname.lower(),
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "event": "mcp_transport_log",
            "message": message,
        }
        for pattern, event_name in _MCP_SESSION_MESSAGES:
            match = pattern.fullmatch(message)
            if match:
                event_dict["event"] = event_name
                event_dict.pop("message")
                event_dict["session_id"] = match.group(1)
                break
        _scrub_sensitive_fields(None, "", event_dict)
        if self.production:
            return json.dumps(event_dict, ensure_ascii=False)
        return self.console_renderer(None, "", event_dict)


class MCPTransportLevelFilter(logging.Filter):
    """Treat stale MCP session requests as debug diagnostics."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _MCP_SESSION_MESSAGES[0][0].fullmatch(record.getMessage()):
            record.levelno = logging.DEBUG
            record.levelname = logging.getLevelName(logging.DEBUG)
        return True


def setup_logging(
    service_name: str,
    level: int = logging.INFO,
    *,
    stream: TextIO = sys.stdout,
) -> Any:
    """Configure structured logs; ENV=production selects JSON output."""
    production = os.getenv("ENV", "development").lower() == "production"
    logging.basicConfig(
        format="%(message)s", stream=stream, level=level, force=True
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

    for logger_name in _MCP_TRANSPORT_LOGGERS:
        transport_logger = logging.getLogger(logger_name)
        for existing_filter in transport_logger.filters[:]:
            if isinstance(existing_filter, MCPTransportLevelFilter):
                transport_logger.removeFilter(existing_filter)
        transport_logger.addFilter(MCPTransportLevelFilter())
        for handler in transport_logger.handlers[:]:
            if getattr(handler, "_job_scraper_mcp_handler", False):
                transport_logger.removeHandler(handler)
                handler.close()
        handler = logging.StreamHandler(stream)
        handler._job_scraper_mcp_handler = True
        handler.setLevel(level)
        handler.setFormatter(MCPTransportFormatter(production=production))
        transport_logger.addHandler(handler)
        transport_logger.propagate = False
    return structlog.get_logger(service_name)
