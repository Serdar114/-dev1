"""
event_logger.py — Structured JSONL event logger.

Design:
  All events are written as JSON lines to a rotating log file.
  Every event has: event_type, ts (UTC unix), data (dict).
  Provenance is always in the data dict — caller must supply it.
  No silent None values. Caller is responsible for providing complete events.
  No compression of failure modes into generic "error" events.
"""

from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loggingx.schemas import EventType, LogEvent, NoTradeEvent

logger = logging.getLogger("polybot.eventlogger")


class EventLogger:
    """
    Thread-safe (within asyncio) JSONL event logger.
    Writes one JSON object per line for easy replay/analysis.
    """

    def __init__(self, log_dir: str, events_file: str = "events.jsonl"):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._log_dir / events_file
        self._fh = open(self._events_path, "a", buffering=1)  # line-buffered

    def _write(self, event: LogEvent) -> None:
        try:
            line = json.dumps(event.to_dict(), default=str) + "\n"
            self._fh.write(line)
        except Exception as exc:
            logger.error("EventLogger write failed: %s", exc)

    def log(self, event_type: str, data: Dict[str, Any]) -> None:
        event = LogEvent(event_type=event_type, ts=time.time(), data=data)
        self._write(event)

    def log_no_trade(self, no_trade: NoTradeEvent) -> None:
        self.log(EventType.NO_TRADE, {
            "condition_id":    no_trade.condition_id,
            "window_start_ts": no_trade.window_start_ts,
            "reason_code":     no_trade.reason_code,
            "is_canonical":    no_trade.is_canonical,
            "details":         no_trade.details,
        })

    def log_system_start(self, config: Dict[str, Any]) -> None:
        self.log(EventType.SYSTEM_START, {"config_snapshot": config})

    def log_error(self, context: str, error: str, data: Optional[Dict] = None) -> None:
        self.log(EventType.ERROR, {
            "context": context,
            "error": error,
            "data": data or {},
        })

    def log_market_discovered(self, condition_id: str, question: str,
                               up_token_id: str, down_token_id: str,
                               window_start_ts: float, window_end_ts: float) -> None:
        self.log(EventType.MARKET_DISCOVERED, {
            "condition_id":    condition_id,
            "question":        question,
            "up_token_id":     up_token_id,
            "down_token_id":   down_token_id,
            "window_start_ts": window_start_ts,
            "window_end_ts":   window_end_ts,
        })

    def log_chainlink_update(self, price: float, round_id: int,
                              updated_at: float, fetched_at: float,
                              freshness: str) -> None:
        self.log(EventType.CHAINLINK_UPDATE, {
            "price_usd":  price,
            "round_id":   round_id,
            "updated_at": updated_at,
            "fetched_at": fetched_at,
            "freshness":  freshness,
            "source":     "polygon_chainlink_btcusd",
        })

    def log_chainlink_stale(self, last_updated_at: Optional[float],
                             age_secs: Optional[float]) -> None:
        self.log(EventType.CHAINLINK_STALE, {
            "last_updated_at": last_updated_at,
            "age_secs": age_secs,
            "source": "polygon_chainlink_btcusd",
        })

    def log_window_open(self, condition_id: str, window_start_ts: float,
                         window_end_ts: float, chainlink_open: Optional[float],
                         chainlink_ok: bool, error: Optional[str]) -> None:
        self.log(EventType.WINDOW_OPEN, {
            "condition_id":    condition_id,
            "window_start_ts": window_start_ts,
            "window_end_ts":   window_end_ts,
            "chainlink_open":  chainlink_open,
            "chainlink_ok":    chainlink_ok,
            "error":           error,
        })

    def log_window_close(self, condition_id: str, window_start_ts: float,
                          chainlink_close: Optional[float], chainlink_ok: bool,
                          outcome: str, error: Optional[str]) -> None:
        self.log(EventType.WINDOW_CLOSE, {
            "condition_id":   condition_id,
            "window_start_ts": window_start_ts,
            "chainlink_close": chainlink_close,
            "chainlink_ok":   chainlink_ok,
            "outcome":        outcome,
            "error":          error,
        })

    def log_hypothetical(self, entry: dict) -> None:
        self.log(EventType.HYPOTHETICAL_ENTRY, entry)

    def log_resolution(self, condition_id: str, window_start_ts: float,
                        outcome: str, chainlink_open: Optional[float],
                        chainlink_close: Optional[float]) -> None:
        self.log(EventType.RESOLUTION, {
            "condition_id":    condition_id,
            "window_start_ts": window_start_ts,
            "outcome":         outcome,
            "chainlink_open":  chainlink_open,
            "chainlink_close": chainlink_close,
        })

    def log_paper_trade_open(self, trade: dict) -> None:
        self.log(EventType.PAPER_TRADE_OPEN, trade)

    def log_paper_trade_close(self, trade: dict) -> None:
        self.log(EventType.PAPER_TRADE_CLOSE, trade)

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
