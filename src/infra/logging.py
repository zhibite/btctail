"""Tiny structured logger.

Every call site uses ``log.info("event.name", key=value, ...)``. The event
name stays stable (easy to grep / aggregate) and the context is appended as
``key=value`` pairs. A rotating file handler is installed next to the console
handler so production incidents can be reconstructed after the fact.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any, Callable

_CONFIGURED = False
_HOOK: Callable[[str, str, dict], None] | None = None


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "ctx", None) or {}
        base = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<5s} [{record.name}] {record.getMessage()}"
        )
        if not ctx:
            return base
        parts = []
        for key, value in ctx.items():
            text = _render(value)
            parts.append(f"{key}={text}")
        return base + " " + " ".join(parts)


def _render(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (dict, list, tuple)):
        text = str(value)
        return text if len(text) <= 400 else text[:397] + "..."
    return str(value)


class Logger:
    """Adapter matching the structlog call style used in the reference bot."""

    __slots__ = ("_log", "_name")

    def __init__(self, name: str, log: logging.Logger) -> None:
        self._name = name
        self._log = log

    def _emit(self, level: int, event: str, exc_info: bool = False, **ctx: Any) -> None:
        self._log.log(level, event, extra={"ctx": ctx}, exc_info=exc_info)
        hook = _HOOK
        if hook is not None and level >= logging.INFO:
            try:
                hook(logging.getLevelName(level), event, ctx)
            except Exception:  # noqa: BLE001 - a journal failure must never break trading
                pass

    def debug(self, event: str, **ctx: Any) -> None:
        if self._log.isEnabledFor(logging.DEBUG):
            self._emit(logging.DEBUG, event, **ctx)

    def info(self, event: str, **ctx: Any) -> None:
        self._emit(logging.INFO, event, **ctx)

    def warning(self, event: str, **ctx: Any) -> None:
        self._emit(logging.WARNING, event, **ctx)

    def error(self, event: str, **ctx: Any) -> None:
        self._emit(logging.ERROR, event, **ctx)

    def exception(self, event: str, **ctx: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=True, **ctx)


def configure_logging(level: str = "INFO", data_dir: Path | None = None,
                      log_file: str = "polytail.log") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = _Formatter()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if data_dir is not None:
        try:
            log_dir = Path(data_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            # Keep a bounded file set; the dashboard reads the SQLite event
            # table for recent history anyway.
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / log_file, maxBytes=20 * 1024 * 1024, backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"log file disabled: {exc}\n")

    # uvicorn/http client noise
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def set_event_hook(hook: Callable[[str, str, dict], None] | None) -> None:
    """Install a sink (usually the SQLite event table) for INFO+ events."""
    global _HOOK
    _HOOK = hook


def get_logger(name: str) -> Logger:
    short = name.replace("src.", "")
    return Logger(short, logging.getLogger(short))


def current_level() -> str:
    return logging.getLevelName(logging.getLogger().level)
