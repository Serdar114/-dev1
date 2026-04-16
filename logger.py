"""
Append-only JSONL logger for the Polymarket BTC observation system.

Six streams, all under the log_dir:
  system_events.jsonl       — startup / shutdown / reconnect / errors
  market_discovery.jsonl    — family lock acquisition and changes
  book_updates.jsonl        — raw BookSnapshot writes
  joined_observations.jsonl — enriched JoinedObservation records
  rtds_snapshots.jsonl      — raw DualReferenceSnapshot writes
  heartbeat.jsonl           — periodic liveness records

All writes go through a single threading.Lock.
Every write is followed by flush() for durability.
"""
import json
import pathlib
import threading
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from schemas import BookSnapshot, DualReferenceSnapshot, JoinedObservation

STREAM_NAMES = (
    "system_events",
    "market_discovery",
    "book_updates",
    "joined_observations",
    "rtds_snapshots",
    "heartbeat",
)


class Logger:
    def __init__(self, log_dir: str = "logs") -> None:
        self._log_dir = pathlib.Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._files: Dict[str, Any] = {
            name: self._open(name + ".jsonl") for name in STREAM_NAMES
        }

    def _open(self, filename: str):
        path = self._log_dir / filename
        return open(str(path), "a", encoding="utf-8")

    def _write(self, stream: str, record: dict) -> None:
        with self._lock:
            fh = self._files.get(stream)
            if fh is None:
                return
            fh.write(json.dumps(record) + "\n")
            fh.flush()

    # ------------------------------------------------------------------ #
    # Public logging methods                                               #
    # ------------------------------------------------------------------ #

    def log_system_event(
        self, event_type: str, data: Optional[dict] = None
    ) -> None:
        self._write(
            "system_events",
            {"ts": int(time.time() * 1000), "event": event_type, "data": data or {}},
        )

    def log_market_discovery(self, family: str, pair_data: dict) -> None:
        self._write(
            "market_discovery",
            {"ts": int(time.time() * 1000), "family": family, **pair_data},
        )

    def log_book_update(self, snapshot: BookSnapshot) -> None:
        self._write(
            "book_updates",
            {
                "ts_local": snapshot.ts_local,
                "token_id": snapshot.token_id,
                "best_bid": snapshot.best_bid,
                "best_ask": snapshot.best_ask,
                "mid": snapshot.mid,
                "spread": snapshot.spread,
                "source": snapshot.source,
            },
        )

    def log_rtds_snapshot(self, snapshot: DualReferenceSnapshot) -> None:
        self._write(
            "rtds_snapshots",
            {
                "ts_local": snapshot.ts_local,
                "binance_price": snapshot.binance_price,
                "binance_source_ts": snapshot.binance_source_ts,
                "binance_local_ts": snapshot.binance_local_ts,
                "binance_stale": snapshot.binance_stale,
                "chainlink_price": snapshot.chainlink_price,
                "chainlink_source_ts": snapshot.chainlink_source_ts,
                "chainlink_local_ts": snapshot.chainlink_local_ts,
                "chainlink_stale": snapshot.chainlink_stale,
                "basis_bps": snapshot.basis_bps,
                "lag_ms": snapshot.lag_ms,
            },
        )

    def log_joined_observation(self, obs: JoinedObservation) -> None:
        self._write("joined_observations", asdict(obs))

    def log_heartbeat(self, data: dict) -> None:
        self._write(
            "heartbeat",
            {"ts": int(time.time() * 1000), **data},
        )

    def close(self) -> None:
        with self._lock:
            for fh in self._files.values():
                try:
                    fh.close()
                except Exception:
                    pass
