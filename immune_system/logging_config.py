"""
Logging Configuration - Structured logging for the AI Agent Immune System

Provides:
- Human-readable colored console output (default)
- JSON structured output (opt-in via LOG_FORMAT=json env var)
- Proper log levels mapped to system events
- Flush-safe stream handler for real-time output
"""
import logging
import json
import os
import sys
import time
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI colour codes (used only in the human-readable formatter)
# ---------------------------------------------------------------------------
class _Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    GREY = "\033[90m"


# Map log level to colour
_LEVEL_COLORS = {
    logging.DEBUG: _Colors.GREY,
    logging.INFO: _Colors.GREEN,
    logging.WARNING: _Colors.YELLOW,
    logging.ERROR: _Colors.RED,
    logging.CRITICAL: _Colors.RED + _Colors.BOLD,
}


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
class ColoredFormatter(logging.Formatter):
    """Human-readable formatter with ANSI colours and timestamps."""

    FORMAT = "%(asctime)s  %(levelname)-8s  %(name)-22s  %(message)s"

    def __init__(self, use_color: bool = True):
        super().__init__(self.FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelno, _Colors.RESET)
            record.levelname = f"{color}{record.levelname}{_Colors.RESET}"
            record.name = f"{_Colors.CYAN}{record.name}{_Colors.RESET}"
        return super().format(record)


class JSONFormatter(logging.Formatter):
    """Structured JSON formatter suitable for log aggregation systems."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%f"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge any extra structured fields attached to the record
        if hasattr(record, "structured_data"):
            log_entry["data"] = record.structured_data
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


# ---------------------------------------------------------------------------
# Flush-safe handler (ensures every log line is flushed immediately)
# ---------------------------------------------------------------------------
class FlushStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every emit for real-time output."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def setup_logging(
    level: Optional[str] = None,
    log_format: Optional[str] = None,
) -> None:
    """
    Configure the root logger for the application.

    Environment variables (overridden by explicit arguments):
        LOG_LEVEL   - DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)
        LOG_FORMAT  - "text" (default, colored) or "json"

    Args:
        level: Override log level (e.g. "DEBUG").
        log_format: Override format ("text" or "json").
    """
    resolved_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    resolved_format = (log_format or os.environ.get("LOG_FORMAT", "text")).lower()

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, resolved_level, logging.INFO))

    # Remove any existing handlers to avoid duplicate output on re-init
    root_logger.handlers.clear()

    handler = FlushStreamHandler(sys.stdout)

    if resolved_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        use_color = sys.stdout.isatty() or os.environ.get("FORCE_COLOR", "") == "1"
        handler.setFormatter(ColoredFormatter(use_color=use_color))

    root_logger.addHandler(handler)

    # Quieten noisy third-party loggers
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("flask").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger.

    Usage:
        from immune_system.logging_config import get_logger
        logger = get_logger(__name__)
        logger.info("Agent %s started", agent_id)
    """
    return logging.getLogger(name)
