#!/usr/bin/env python3
"""Structured logging and lightweight step telemetry for parchiver."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_ID = os.getenv("PARCHIVER_RUN_ID") or uuid.uuid4().hex
_CONFIGURED = False
_STREAMS_CONFIGURED = False

_LOG_RECORD_SKIP_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _now_iso(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            "run_id": getattr(record, "run_id", RUN_ID),
        }

        event_fields = getattr(record, "event_fields", None)
        if isinstance(event_fields, dict):
            payload.update(_json_safe(event_fields))

        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_SKIP_FIELDS:
                continue
            if key in payload or key == "event_fields":
                continue
            payload[key] = _json_safe(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = RUN_ID
        return True


def _env_enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


class _TeeStream:
    """Write stream content to both the original stream and a file mirror."""

    def __init__(self, primary, mirror) -> None:
        self._primary = primary
        self._mirror = mirror

    def write(self, data: str) -> int:
        written = self._primary.write(data)
        try:
            self._mirror.write(data)
        except Exception:
            pass
        return written

    def flush(self) -> None:
        try:
            self._primary.flush()
        except Exception:
            pass
        try:
            self._mirror.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return bool(getattr(self._primary, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._primary.fileno()

    @property
    def encoding(self) -> str | None:
        return getattr(self._primary, "encoding", None)

    @property
    def errors(self) -> str | None:
        return getattr(self._primary, "errors", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


def _stream_log_path(stream_name: str) -> Path:
    env_name = "PARCHIVER_STDOUT_LOG" if stream_name == "stdout" else "PARCHIVER_STDERR_LOG"
    explicit = os.getenv(env_name)
    if explicit:
        return Path(explicit)

    stream_dir = Path(os.getenv("PARCHIVER_STREAM_DIR", "logs/parchiver"))
    return stream_dir / f"{RUN_ID}.{stream_name}.log"


def configure_stream_tee() -> None:
    global _STREAMS_CONFIGURED
    if _STREAMS_CONFIGURED:
        return
    _STREAMS_CONFIGURED = True

    if not _env_enabled("PARCHIVER_STREAM_TEE", default=True):
        return

    stdout_path = _stream_log_path("stdout")
    stderr_path = _stream_log_path("stderr")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    stderr_encoding = getattr(sys.stderr, "encoding", None) or "utf-8"

    stdout_file = open(stdout_path, "a", buffering=1, encoding=stdout_encoding)
    stderr_file = open(stderr_path, "a", buffering=1, encoding=stderr_encoding)

    sys.stdout = _TeeStream(sys.stdout, stdout_file)
    sys.stderr = _TeeStream(sys.stderr, stderr_file)


def configure_logging(
    level: str | None = None,
    fmt: str | None = None,
) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    configure_stream_tee()

    level_name = (level or os.getenv("PARCHIVER_LOG_LEVEL", "INFO")).upper()
    format_name = (fmt or os.getenv("PARCHIVER_LOG_FORMAT", "json")).lower()
    level_value = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("parchiver")
    root.setLevel(level_value)
    root.handlers.clear()
    root.propagate = False

    handler = logging.StreamHandler(sys.stderr)
    if format_name == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    handler.addFilter(_RunIdFilter())
    root.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    logger = logging.getLogger(name)
    if logger.name.startswith("parchiver."):
        logger.propagate = True
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    logger.log(level, event, extra={"event_fields": _json_safe(fields)})


class StepTimer(AbstractContextManager):
    def __init__(self, logger: logging.Logger, step: str, **fields: Any) -> None:
        self.logger = logger
        self.step = step
        self.fields: dict[str, Any] = dict(fields)
        self._started = 0.0

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)

    def __enter__(self) -> "StepTimer":
        self._started = time.perf_counter()
        log_event(self.logger, "step_started", step=self.step, **self.fields)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = round((time.perf_counter() - self._started) * 1000, 3)
        if exc is None:
            log_event(
                self.logger,
                "step_completed",
                step=self.step,
                duration_ms=duration_ms,
                **self.fields,
            )
            return False

        log_event(
            self.logger,
            "step_failed",
            level=logging.ERROR,
            step=self.step,
            duration_ms=duration_ms,
            error_type=exc_type.__name__ if exc_type else None,
            error=str(exc),
            **self.fields,
        )
        return False


def step_timer(logger: logging.Logger, step: str, **fields: Any) -> StepTimer:
    return StepTimer(logger, step, **fields)
