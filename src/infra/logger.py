"""
Pretty console logger with optional colour support.

Format (matches the signal engine style):
    2026-03-07 15:22:41  INFO      signals.signal_consumer  SignalConsumer subscribed  symbols=['EUR/USD']

Colours are enabled automatically when stdout is a TTY (terminal).
They are suppressed when output is piped / redirected (e.g. to a file or
docker log collector) so the raw text stays clean.

Call `setup_logging(level)` once at startup.
All modules use `logging.getLogger(__name__)` as normal.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# ── ANSI colour codes ──────────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_LEVEL_COLOURS = {
    "DEBUG": "\033[36m",  # cyan
    "INFO": "\033[32m",  # green
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
    "CRITICAL": "\033[41m",  # red background
}

_LOGGER_COLOUR = "\033[34m"  # blue  — logger name
_EXTRA_COLOUR = "\033[35m"  # magenta — key=value pairs
_TS_COLOUR = "\033[90m"  # dark grey — timestamp

# Fields that belong to the LogRecord internals and should never be
# printed as extra context.
_SKIP_FIELDS = frozenset(
    {
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "name",
        "message",
        "taskName",
    }
)


class _PrettyFormatter(logging.Formatter):
    """
    Single-line human-readable formatter.

    Layout:
        TIMESTAMP  LEVEL     LOGGER_NAME  Message text   key=value key=value
    """

    def __init__(self, use_colour: bool = True, tz: ZoneInfo | None = None) -> None:
        super().__init__()
        self._colour = use_colour
        self._tz = tz or ZoneInfo("UTC")

    def format(self, record: logging.LogRecord) -> str:
        # ── Timestamp ──────────────────────────────────────────────────────
        ts = datetime.now(tz=self._tz).strftime("%Y-%m-%d %H:%M:%S")

        # ── Level ──────────────────────────────────────────────────────────
        level = record.levelname

        # ── Logger name (trim to last 2 segments for brevity) ──────────────
        parts = record.name.split(".")
        logger = ".".join(parts[-2:]) if len(parts) > 2 else record.name

        # ── Message ────────────────────────────────────────────────────────
        message = record.getMessage()

        # ── Extra context key=value pairs ──────────────────────────────────
        extras = {k: v for k, v in record.__dict__.items() if k not in _SKIP_FIELDS}
        extra_str = (
            "  " + "  ".join(f"{k}={v!r}" for k, v in extras.items()) if extras else ""
        )

        # ── Exception ──────────────────────────────────────────────────────
        exc_str = ""
        if record.exc_info:
            exc_str = "\n" + self.formatException(record.exc_info)

        # ── Assemble (plain) ───────────────────────────────────────────────
        line = (
            f"{ts:<20} "
            f"{level:<9} "
            f"{logger:<30} "
            f"{message}"
            f"{extra_str}"
            f"{exc_str}"
        )

        if not self._colour:
            return line

        # ── Colourised version ─────────────────────────────────────────────
        lc = _LEVEL_COLOURS.get(level, "")
        return (
            f"{_TS_COLOUR}{ts:<20}{_RESET} "
            f"{_BOLD}{lc}{level:<9}{_RESET} "
            f"{_LOGGER_COLOUR}{logger:<30}{_RESET} "
            f"{message}"
            f"{_EXTRA_COLOUR}{extra_str}{_RESET}"
            f"{exc_str}"
        )


def setup_logging(level: str = "INFO", tz: ZoneInfo | None = None) -> None:
    """
    Configure the root logger with the pretty formatter.

    Colour is enabled automatically when stdout is attached to a terminal.
    Set LOG_COLOUR=false in the environment to force plain output.
    """
    import os

    force_plain = os.environ.get("LOG_COLOUR", "").lower() == "false"
    use_colour = (not force_plain) and bool(sys.stdout and sys.stdout.isatty())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    # Only add a stream handler if stdout exists (no console when run via
    # Task Scheduler or other windowless launchers)
    if sys.stdout is not None:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_PrettyFormatter(use_colour=use_colour, tz=tz))
        root.addHandler(handler)

    # Silence noisy third-party loggers
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def add_file_handler(log_dir: "str | Path", tz: "ZoneInfo | None" = None) -> None:
    """
    Add a plain-text file handler writing to <log_dir>/engine.log.

    Safe to call after setup_logging().  Idempotent — if a FileHandler for
    the same path already exists it is not added again.
    """
    from pathlib import Path as _Path

    log_path = _Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    target = str((log_path / "engine.log").resolve())

    root = logging.getLogger()
    # Avoid duplicate handlers on restart / re-init
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and h.baseFilename == target:
            return

    fh = logging.FileHandler(target, encoding="utf-8")
    fh.setFormatter(_PrettyFormatter(use_colour=False, tz=tz))
    root.addHandler(fh)








