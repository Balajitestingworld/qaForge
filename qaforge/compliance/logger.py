"""Structured, masked, audit-friendly logging.

* Machine log: one JSON object per line (UTC ISO timestamps) in <run_dir>/run.log.jsonl
  plus a per-test JSON log inside each evidence pack.
* Console log: short human-readable lines.
* Every record carries run_id, env, test_id, jira keys and the current step automatically
  (context variables), and every message passes through the masker.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qaforge.compliance.masker import masker

_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("qaf_log_ctx", default={})

LOGGER_ROOT = "qaforge"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bind(**fields: Any) -> None:
    """Attach fields (test_id, jira, step, app, role ...) to every subsequent record."""
    current = dict(_ctx.get())
    current.update({k: v for k, v in fields.items() if v is not None})
    _ctx.set(current)


def unbind(*keys: str) -> None:
    current = dict(_ctx.get())
    for k in keys:
        current.pop(k, None)
    _ctx.set(current)


def current_context() -> dict[str, Any]:
    return dict(_ctx.get())


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.qaf = current_context()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": masker.mask(record.getMessage()),
        }
        payload.update(getattr(record, "qaf", {}))
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = masker.mask_obj(extra)
        if record.exc_info:
            payload["exc"] = masker.mask(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "qaf", {})
        test = ctx.get("test_short", "")
        prefix = f"[{test}] " if test else ""
        return f"{record.levelname:<7} {prefix}{masker.mask(record.getMessage())}"


class _ListHandler(logging.Handler):
    """Collects formatted JSON lines for the active test's evidence pack."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never break a test
            self.handleError(record)


_configured = False


def configure_logging(run_dir: Path, level: str = "INFO", console: bool = True) -> None:
    global _configured
    root = logging.getLogger(LOGGER_ROOT)
    root.setLevel(level.upper())
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
    root.filters.clear()

    run_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(run_dir / "run.log.jsonl", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(_ContextFilter())
    root.addHandler(file_handler)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(ConsoleFormatter())
        ch.addFilter(_ContextFilter())
        root.addHandler(ch)
    _configured = True


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_ROOT}.{name}" if name else LOGGER_ROOT)


class capture_test_log:
    """Context manager: collect every JSON log line emitted while a test runs."""

    def __init__(self) -> None:
        self.handler = _ListHandler()
        self.handler.addFilter(_ContextFilter())

    def __enter__(self) -> _ListHandler:
        logging.getLogger(LOGGER_ROOT).addHandler(self.handler)
        return self.handler

    def __exit__(self, *exc: object) -> None:
        logging.getLogger(LOGGER_ROOT).removeHandler(self.handler)

    def write(self, path: Path) -> None:
        path.write_text("\n".join(self.handler.lines) + ("\n" if self.handler.lines else ""), encoding="utf-8")
