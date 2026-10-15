"""Structured logging. Logs contain metadata (stages, timings, counts) and never document contents."""
from __future__ import annotations

import json
import logging
import logging.handlers
from typing import Any

from config.settings import SETTINGS

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure logging once: JSON lines to a rotating file, warnings to stderr."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("docupilot")
    root.setLevel(level)
    root.propagate = False
    try:
        SETTINGS.logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            SETTINGS.logs_dir / "docupilot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    except OSError:
        pass  # read-only filesystem: fall back to stderr only
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    sh.setLevel(logging.WARNING)
    root.addHandler(sh)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring logging on first use."""
    configure_logging()
    return logging.getLogger(f"docupilot.{name}")


def log_event(logger: logging.Logger, msg: str, **ctx: Any) -> None:
    """Log an INFO event with structured context fields (metadata only)."""
    logger.info(msg, extra={"ctx": ctx})
