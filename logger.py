"""
logger.py — Thread-safe JSONL logger for the 48-hour BTC feasibility observation run.

Streams:
  system.jsonl            — startup/shutdown/reconnect/exceptions/anomalies
  markets.jsonl           — market metadata snapshots and selection events
  books.jsonl             — periodic top-of-book snapshots
  external_price.jsonl    — BTC reference price updates
  joined_observation.jsonl — 2-second joined market+price observations (primary output)
  future_execution_schema.jsonl — schema stub, no orders in Day 1-2

All writes are thread-safe (one lock per file).
Console output is a human-readable summary alongside structured file writes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from schemas import to_jsonl


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_DIR = os.environ.get("LOG_DIR", "logs")
CONSOLE_LEVEL = logging.DEBUG


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _ensure_log_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Console handler (human-readable summaries)
# ---------------------------------------------------------------------------

def _build_console_logger() -> logging.Logger:
    logger = logging.getLogger("poly_obs")
    logger.setLevel(CONSOLE_LEVEL)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(CONSOLE_LEVEL)
        fmt = logging.Formatter(
            "[%(asctime)s UTC] %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        h.setFormatter(fmt)
        logging.Formatter.converter = time.gmtime  # UTC
        logger.addHandler(h)
    return logger


_console = _build_console_logger()


# ---------------------------------------------------------------------------
# JSONL file writer (one instance per stream)
# ---------------------------------------------------------------------------

class JsonlWriter:
    def __init__(self, filename: str, log_dir: str = LOG_DIR) -> None:
        _ensure_log_dir(log_dir)
        self._path = os.path.join(log_dir, filename)
        self._lock = threading.Lock()
        self._fh = open(self._path, "a", buffering=1)  # line-buffered

    def write(self, record: Any) -> None:
        """Write one record as a JSONL line. record may be a dataclass, dict, or str."""
        try:
            if isinstance(record, str):
                line = record
            else:
                line = to_jsonl(record)
            with self._lock:
                self._fh.write(line + "\n")
        except Exception as exc:  # never crash the caller
            _console.error("JsonlWriter(%s) write error: %s", self._path, exc)

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Stream registry — one global set of open writers
# ---------------------------------------------------------------------------

class _Streams:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writers: Dict[str, JsonlWriter] = {}

    def _get(self, name: str) -> JsonlWriter:
        with self._lock:
            if name not in self._writers:
                self._writers[name] = JsonlWriter(name)
            return self._writers[name]

    @property
    def system(self) -> JsonlWriter:
        return self._get("system.jsonl")

    @property
    def markets(self) -> JsonlWriter:
        return self._get("markets.jsonl")

    @property
    def market_discovery(self) -> JsonlWriter:
        """
        market_discovery.jsonl — one line per candidate decision + summary per cycle.
        Always created on first access; written even when 0 candidates are found.
        """
        return self._get("market_discovery.jsonl")

    @property
    def books(self) -> JsonlWriter:
        return self._get("books.jsonl")

    @property
    def external_price(self) -> JsonlWriter:
        return self._get("external_price.jsonl")

    @property
    def joined_observation(self) -> JsonlWriter:
        return self._get("joined_observation.jsonl")

    @property
    def future_execution_schema(self) -> JsonlWriter:
        return self._get("future_execution_schema.jsonl")

    def flush_all(self) -> None:
        with self._lock:
            for w in self._writers.values():
                try:
                    w._fh.flush()
                except Exception:
                    pass

    def close_all(self) -> None:
        with self._lock:
            for w in self._writers.values():
                w.close()


_streams = _Streams()


# ---------------------------------------------------------------------------
# Public logging functions
# ---------------------------------------------------------------------------

def log_startup(config: Dict[str, Any]) -> None:
    rec = {
        "ts_local": _now_ms(),
        "event": "startup",
        "utc": _utc_iso(_now_ms()),
        "config": config,
    }
    _streams.system.write(rec)
    _console.info("STARTUP | log_dir=%s", LOG_DIR)


def log_shutdown(reason: str = "normal") -> None:
    rec = {
        "ts_local": _now_ms(),
        "event": "shutdown",
        "reason": reason,
    }
    _streams.system.write(rec)
    _streams.flush_all()
    _console.info("SHUTDOWN | reason=%s", reason)
    _streams.close_all()


def log_system_event(
    event: str,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    level: str = "info",
) -> None:
    rec: Dict[str, Any] = {
        "ts_local": _now_ms(),
        "event": event,
    }
    if detail:
        rec["detail"] = detail
    if extra:
        rec.update(extra)
    _streams.system.write(rec)
    log_fn = getattr(_console, level, _console.info)
    log_fn("SYS | %s | %s", event, detail or "")


def log_exception(context: str, exc: Exception, extra: Optional[Dict[str, Any]] = None) -> None:
    rec: Dict[str, Any] = {
        "ts_local": _now_ms(),
        "event": "exception",
        "context": context,
        "exc_type": type(exc).__name__,
        "exc_msg": str(exc),
    }
    if extra:
        rec.update(extra)
    _streams.system.write(rec)
    _console.error("EXCEPTION | %s | %s: %s", context, type(exc).__name__, exc)


def log_parse_anomaly(
    context: str,
    field: Optional[str],
    raw_value: Any,
    reason: str,
) -> None:
    rec = {
        "ts_local": _now_ms(),
        "event": "parse_anomaly",
        "context": context,
        "field": field,
        "raw_value": str(raw_value)[:200],  # truncate large blobs
        "reason": reason,
    }
    _streams.system.write(rec)
    _console.warning("PARSE_ANOMALY | %s | field=%s | %s", context, field, reason)


def log_reconnect(component: str, attempt: int, reason: Optional[str] = None) -> None:
    rec = {
        "ts_local": _now_ms(),
        "event": "reconnect",
        "component": component,
        "attempt": attempt,
        "reason": reason,
    }
    _streams.system.write(rec)
    _console.warning("RECONNECT | %s | attempt=%d | %s", component, attempt, reason or "")


def log_heartbeat_miss(component: str, last_seen_ms: Optional[int], threshold_ms: int) -> None:
    now = _now_ms()
    gap = (now - last_seen_ms) if last_seen_ms else None
    rec = {
        "ts_local": now,
        "event": "heartbeat_miss",
        "component": component,
        "last_seen_ms": last_seen_ms,
        "gap_ms": gap,
        "threshold_ms": threshold_ms,
    }
    _streams.system.write(rec)
    _console.warning("HEARTBEAT_MISS | %s | gap_ms=%s", component, gap)


# ---------------------------------------------------------------------------
# Market logging
# ---------------------------------------------------------------------------

def log_market_selection(
    selected_market: Any,
    candidates: list,
    excluded: list,
    fee_context: Optional[Any] = None,
) -> None:
    from schemas import MarketRecord, FeeContext

    def _market_summary(m: MarketRecord) -> Dict[str, Any]:
        return {
            "market_id": m.market_id,
            "market_slug": m.market_slug,
            "question": m.question,
            "end_time": m.end_time,
            "accepting_orders": m.accepting_orders,
            "active": m.active,
            "minimum_tick_size": m.minimum_tick_size,
            "neg_risk": m.neg_risk,
            "fees_enabled": m.fees_enabled,
            "fee_rate_bps": m.fee_rate_bps,
            "min_order_size": m.min_order_size,
            "tokens": [{"token_id": t.token_id, "outcome": t.outcome} for t in m.tokens],
            "selected": m.selected,
            "excluded": m.excluded,
            "exclusion_reason": m.exclusion_reason,
            "parse_warnings": m.parse_warnings,
        }

    rec: Dict[str, Any] = {
        "ts_local": _now_ms(),
        "event": "market_selection",
        "selected": _market_summary(selected_market) if selected_market else None,
        "candidate_count": len(candidates),
        "excluded_count": len(excluded),
        "candidates": [_market_summary(m) for m in candidates],
        "excluded": [_market_summary(m) for m in excluded],
    }
    if fee_context is not None:
        from dataclasses import asdict as _asdict
        rec["fee_context"] = _asdict(fee_context)

    _streams.markets.write(rec)
    slug = selected_market.market_slug if selected_market else "none"
    _console.info(
        "MARKET_SEL | selected=%s | candidates=%d | excluded=%d",
        slug, len(candidates), len(excluded),
    )


def log_market_snapshot(market: Any, fee_context: Optional[Any] = None) -> None:
    """Periodic re-snapshot of selected market metadata."""
    from dataclasses import asdict as _asdict
    rec: Dict[str, Any] = {
        "ts_local": _now_ms(),
        "event": "market_snapshot",
        **_asdict(market),
    }
    if fee_context is not None:
        rec["fee_context"] = _asdict(fee_context)
    _streams.markets.write(rec)


# ---------------------------------------------------------------------------
# Book logging
# ---------------------------------------------------------------------------

def log_book_snapshot(snapshot: Any) -> None:
    _streams.books.write(snapshot)
    # Console: only log anomalies to avoid flooding stdout
    flags = getattr(snapshot, "book_state_flags", [])
    if flags:
        _console.debug(
            "BOOK | %s | %s | flags=%s",
            getattr(snapshot, "market_slug", "?"),
            getattr(snapshot, "side_label", "?"),
            flags,
        )


# ---------------------------------------------------------------------------
# External price logging
# ---------------------------------------------------------------------------

def log_external_price(snapshot: Any) -> None:
    _streams.external_price.write(snapshot)
    # Only log to console when stale
    age = getattr(snapshot, "data_age_ms", None)
    if age is not None and age > 5000:
        _console.warning("EXT_PRICE_STALE | age_ms=%d", age)


# ---------------------------------------------------------------------------
# Joined observation logging (primary Day 1-2 output)
# ---------------------------------------------------------------------------

def log_joined_observation(obs: Any) -> None:
    _streams.joined_observation.write(obs)

    # Human-readable console summary every ~30 seconds (reduce noise)
    # Callers are responsible for cadence; we just log a compact line
    price = getattr(obs, "external_btc_price", None)
    ask_sum = getattr(obs, "pair_best_ask_sum", None)
    up_ask = getattr(obs, "up_best_ask", None)
    dn_ask = getattr(obs, "down_best_ask", None)
    flags = [
        f for f in [
            "stale_external" if getattr(obs, "stale_external", False) else None,
            "stale_up" if getattr(obs, "stale_book_up", False) else None,
            "stale_dn" if getattr(obs, "stale_book_down", False) else None,
            "empty_up_ask" if getattr(obs, "empty_up_ask", False) else None,
            "empty_dn_ask" if getattr(obs, "empty_down_ask", False) else None,
            "crossed_up" if getattr(obs, "crossed_up", False) else None,
            "crossed_dn" if getattr(obs, "crossed_down", False) else None,
            "fee_unk" if getattr(obs, "fee_unknown", False) else None,
            "tick_unk" if getattr(obs, "tick_unknown", False) else None,
        ]
        if f is not None
    ]
    _console.debug(
        "OBS | btc=%.2f | up_ask=%s | dn_ask=%s | ask_sum=%s | flags=%s",
        price or 0.0,
        f"{up_ask:.4f}" if up_ask else "None",
        f"{dn_ask:.4f}" if dn_ask else "None",
        f"{ask_sum:.4f}" if ask_sum else "None",
        flags or "ok",
    )


# ---------------------------------------------------------------------------
# Future execution schema stub writer
# ---------------------------------------------------------------------------

def log_future_execution_schema_stub(record: Any) -> None:
    """Write schema stub to future_execution_schema.jsonl. No actual order data in Day 1-2."""
    _streams.future_execution_schema.write(record)


# ---------------------------------------------------------------------------
# Convenience re-export of streams for direct access
# ---------------------------------------------------------------------------

streams = _streams
