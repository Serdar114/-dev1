"""
loggingx/event_logger.py — Async JSONL event logger.

All events are queued and written by a background thread.
Callers never block on disk I/O.
Output: one JSON object per line in the configured log file.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from typing import Any, Dict

from loggingx.schemas import BaseEvent

_SENTINEL = None


class EventLogger:
    """
    Thread-safe async logger.
    Usage:
        logger = EventLogger("logs/measurement.jsonl")
        logger.log(MarketFoundEvent(slug="btc-up-down-5m-..."))
    """

    def __init__(self, log_path: str) -> None:
        self._log_path = log_path
        self._queue: queue.Queue[Any] = queue.Queue()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="event-logger"
        )
        self._thread.start()

    def log(self, event: BaseEvent) -> None:
        """Enqueue an event for writing. Non-blocking."""
        self._queue.put(event.to_dict())

    def log_dict(self, d: Dict[str, Any]) -> None:
        """Enqueue a raw dict. Non-blocking."""
        self._queue.put(d)

    def flush_and_stop(self) -> None:
        """Drain queue and stop writer thread. Call on clean shutdown."""
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=5.0)

    def _writer_loop(self) -> None:
        with open(self._log_path, "a", buffering=1) as fh:
            while True:
                item = self._queue.get()
                if item is _SENTINEL:
                    break
                try:
                    fh.write(json.dumps(item) + "\n")
                except Exception:
                    # Never crash the writer thread
                    pass
