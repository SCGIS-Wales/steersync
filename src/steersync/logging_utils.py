"""Logging configuration with secret redaction and optional JSON output."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Literal

_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<=://)[^/@\s]+@"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
)


def redact(text: str) -> str:
    """Mask credential like substrings before they reach any log sink."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("***", text)
    return text


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(
    level: str = "INFO", fmt: Literal["text", "json"] = "text", *, quiet: bool = False
) -> None:
    """Configure the root logger for CLI use. Logs go to stderr so stdout stays machine readable."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    formatter: logging.Formatter = (
        _JsonFormatter() if fmt == "json" else _TextFormatter("%(levelname)s %(name)s: %(message)s")
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel("ERROR" if quiet else level)
