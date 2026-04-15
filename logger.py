"""
logger.py — 6-stream JSONL logger.

Streams (all under logs/):
  system_events.jsonl     — startup / shutdown / reconnect / errors
  market_discovery.jsonl  — family lock acquisition and changes
  book_updates.jsonl      — raw BookSnapshot records
  joined_observations.jsonl — enriched JoinedObservation records (both families)
  rtds_snapshots.jsonl    — DualReferenceSnapshot records
  heartbeat.jsonl         — periodic liveness records

Thread-safe: all writes go through a single lock.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)

_STREAMS = (
    "system_events",
    "market_discovery",
    "book_updates",
    "joined_observations",
    "rtds_snapshots",
    "heartbeat",
)


class Logger:
    def __init__(self, log_dir: str = "logs") -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handles: Dict[str, Any] = {}
        for stream in _STREAMS:
            path = self._dir / f"{stream}.jsonl"
            self._handles[stream] = open(path, "a", encoding="utf-8")
        log.info("logger: opened %d streams in %s", len(_STREAMS), log_dir)

    # ── internal ──────────────────────────────────────────────────────────────

    def _write(self, stream: str, record: dict) -> None:
        with self._lock:
            fh = self._handles[stream]
            fh.write(json.dumps(record, default=str) + "\n")
            fh.flush()

    def close(self) -> None:
        with self._lock:
            for name, fh in self._handles.items():
                try:
                    fh.close()
                except Exception as exc:  # noqa: BLE001
                    log.debug("logger: close error stream=%s: %s", name, exc)

    # ── per-stream helpers ────────────────────────────────────────────────────

    def log_system_event(self, event_type: str, **kwargs) -> None:
        self._write("system_events", {
            "ts_local": int(time.time() * 1000),
            "event_type": event_type,
            **kwargs,
        })

    def log_market_discovery(self, family: str, slugs: list) -> None:
        self._write("market_discovery", {
            "ts_local": int(time.time() * 1000),
            "family": family,
            "slugs": slugs,
        })

    def log_book_update(self, snapshot) -> None:
        self._write("book_updates", {
            "ts_local": snapshot.ts_local,
            "token_id": snapshot.token_id,
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "mid": snapshot.mid,
            "spread": snapshot.spread,
            "source": snapshot.source,
        })

    def log_joined_observation(self, obs) -> None:
        self._write("joined_observations", obs.to_dict())

    def log_rtds_snapshot(self, snap) -> None:
        self._write("rtds_snapshots", {
            "ts_local": snap.ts_local,
            "binance_price": snap.binance_price,
            "binance_source_ts": snap.binance_source_ts,
            "binance_local_ts": snap.binance_local_ts,
            "binance_stale": snap.binance_stale,
            "chainlink_price": snap.chainlink_price,
            "chainlink_source_ts": snap.chainlink_source_ts,
            "chainlink_local_ts": snap.chainlink_local_ts,
            "chainlink_stale": snap.chainlink_stale,
            "basis_bps": snap.basis_bps,
            "lag_ms": snap.lag_ms,
        })

    def log_heartbeat(self, **kwargs) -> None:
        self._write("heartbeat", {
            "ts_local": int(time.time() * 1000),
            **kwargs,
        })
