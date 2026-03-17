"""
Logging setup for polybot_v2.

Produces:
  - Human-readable console output via stdlib logging
  - Structured JSONL files per channel: signals, paper_trades, shadow_quotes, bankroll
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any


def setup_console_logger(level: str = "INFO") -> None:
    """Configure root logger for human-readable console output."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)-5s] %(name)s - %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(numeric)
    root.handlers.clear()
    root.addHandler(handler)


class JsonlLogger:
    """Writes structured JSON lines to a log file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = open(path, "a", buffering=1)  # line-buffered

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("_ts", time.time())
        self._fh.write(json.dumps(record) + "\n")

    def close(self) -> None:
        self._fh.close()


class StructuredLogger:
    """
    Holds one JsonlLogger per channel.
    Channels: signals, paper_trades, shadow_quotes, bankroll
    """

    CHANNELS = ("signals", "paper_trades", "shadow_quotes", "bankroll")

    def __init__(self, log_dir: Path, enabled: bool = True) -> None:
        self._enabled = enabled
        self._loggers: dict[str, JsonlLogger] = {}
        if enabled:
            for ch in self.CHANNELS:
                self._loggers[ch] = JsonlLogger(log_dir / f"{ch}.jsonl")

    def log(self, channel: str, record: dict[str, Any]) -> None:
        if not self._enabled:
            return
        if channel not in self._loggers:
            raise ValueError(f"Unknown log channel: {channel!r}")
        self._loggers[channel].write(record)

    def close(self) -> None:
        for lg in self._loggers.values():
            lg.close()
