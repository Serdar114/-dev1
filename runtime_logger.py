"""
runtime_logger.py - Record all execution-layer failures and timing anomalies.

Captures:
  - WebSocket disconnects, reconnects, heartbeat gaps
  - REST request RTTs (per endpoint)
  - Order/cancel latency placeholders (no live orders yet, but structure is set)
  - 425 / rate-limit events
  - Generic error events from any module

Persistence:
  data/runtime/runtime_<session_id>.jsonl   – every event, one per line
  data/runtime/runtime_<session_id>.csv     – flattened for spreadsheet review

All other modules call `RuntimeLogger.log_event(event_type, detail_dict)`.
This module is the single sink. Thread-safe.

Event schema (every record has these fields, optional extras in `detail`):
  session_id, event_type, ts_ms, utc_iso, source_module, detail_json
"""

import csv
import json
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

import config

log = logging.getLogger(__name__)


# ── event type constants ──────────────────────────────────────────────────────
# Use these instead of bare strings to avoid typos across modules.

EV_WS_CONNECT      = "ws_connect"
EV_WS_DISCONNECT   = "ws_disconnect"
EV_WS_RECONNECT    = "ws_reconnect"
EV_WS_ERROR        = "ws_error"
EV_WS_FATAL        = "ws_fatal"
EV_WS_HEARTBEAT_GAP = "ws_heartbeat_gap"   # no message for > threshold

EV_REST_RTT        = "rest_rtt"            # REST request latency sample
EV_REST_ERROR      = "rest_error"          # non-200 or exception
EV_REST_RETRY      = "rest_retry"          # retry attempt

EV_ORDER_SENT      = "order_sent"          # (future use)
EV_ORDER_ACK       = "order_ack"           # (future use)
EV_ORDER_REJECT    = "order_reject"        # (future use)
EV_ORDER_LATENCY   = "order_latency"       # (future use)
EV_CANCEL_SENT     = "cancel_sent"         # (future use)
EV_CANCEL_ACK      = "cancel_ack"          # (future use)

EV_RATE_LIMIT      = "rate_limit_425"
EV_BOOK_STALE      = "book_stale"          # no book update for > threshold
EV_REF_MISS        = "ref_miss"            # failed to capture a reference snapshot
EV_SESSION_START   = "session_start"
EV_SESSION_END     = "session_end"
EV_GENERIC_ERROR   = "generic_error"


# ── main logger class ─────────────────────────────────────────────────────────

class RuntimeLogger:
    """
    Thread-safe event sink. Instantiate once per session, pass to all modules.

    Usage:
        rl = RuntimeLogger(session_id)
        rl.log(EV_WS_DISCONNECT, {"market_id": "...", "code": 1006})
        rl.log(EV_REST_RTT, {"endpoint": "/book", "latency_ms": 42.3})
    """

    CSV_FIELDS = [
        "session_id", "event_type", "ts_ms", "utc_iso",
        "source_module", "detail_json",
    ]

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._lock = threading.Lock()

        self._jsonl_path = config.RUNTIME_DIR / f"runtime_{session_id}.jsonl"
        self._csv_path   = config.RUNTIME_DIR / f"runtime_{session_id}.csv"

        # In-memory ring buffer for fast access by other modules
        self._events: Deque[Dict] = deque(maxlen=10_000)

        # Per-event-type counters for summary
        self._counts: Dict[str, int] = defaultdict(int)

        # RTT tracking per endpoint
        self._rtt_samples: Dict[str, List[float]] = defaultdict(list)

        # WS state tracking
        self._ws_state: Dict[str, str] = {}   # market_id -> "connected"|"disconnected"
        self._last_ws_msg_ts: Dict[str, float] = {}  # market_id -> timestamp
        self._heartbeat_monitor_active = False
        self._shutdown = threading.Event()

        with open(self._csv_path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=self.CSV_FIELDS).writeheader()

        log.info("[runtime] RuntimeLogger initialized: session=%s", session_id)

    def log(
        self,
        event_type: str,
        detail: Optional[Dict] = None,
        source: str = "unknown",
    ) -> Dict:
        """Log an event. Returns the persisted record."""
        ts_ms   = int(time.time() * 1000)
        utc_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        detail  = detail or {}

        record = {
            "session_id":   self.session_id,
            "event_type":   event_type,
            "ts_ms":        ts_ms,
            "utc_iso":      utc_iso,
            "source_module": source,
            "detail_json":  json.dumps(detail),
        }

        with self._lock:
            self._events.append(record)
            self._counts[event_type] += 1

            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(record) + "\n")

            with open(self._csv_path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=self.CSV_FIELDS).writerow(record)

        # Side effects for specific event types
        if event_type == EV_REST_RTT:
            endpoint = detail.get("endpoint", "unknown")
            latency  = detail.get("latency_ms")
            if isinstance(latency, (int, float)):
                self._rtt_samples[endpoint].append(float(latency))

        if event_type in (EV_WS_CONNECT, EV_WS_RECONNECT):
            mid = detail.get("market_id", "")
            self._ws_state[mid] = "connected"
            self._last_ws_msg_ts[mid] = time.time()

        if event_type == EV_WS_DISCONNECT:
            mid = detail.get("market_id", "")
            self._ws_state[mid] = "disconnected"

        # Warn on notable events
        if event_type in (EV_WS_DISCONNECT, EV_WS_ERROR, EV_WS_FATAL, EV_RATE_LIMIT,
                           EV_ORDER_REJECT, EV_REST_ERROR):
            log.warning("[runtime] %s: %s", event_type, json.dumps(detail)[:120])
        elif event_type == EV_WS_HEARTBEAT_GAP:
            log.error("[runtime] HEARTBEAT GAP: %s", json.dumps(detail)[:120])

        return record

    # ── convenience wrapper for REST timing ───────────────────────────────────

    def timed_rest(self, endpoint_label: str, fn: Callable, source: str = "unknown"):
        """
        Call fn(), measure its RTT, and log EV_REST_RTT.
        Re-raises exceptions after logging EV_REST_ERROR.
        Returns the function's return value.
        """
        t0 = time.monotonic()
        try:
            result = fn()
            latency_ms = (time.monotonic() - t0) * 1000
            self.log(EV_REST_RTT, {
                "endpoint": endpoint_label,
                "latency_ms": round(latency_ms, 2),
                "ok": True,
            }, source=source)
            return result
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self.log(EV_REST_ERROR, {
                "endpoint": endpoint_label,
                "latency_ms": round(latency_ms, 2),
                "error": str(exc),
            }, source=source)
            raise

    # ── heartbeat monitor ─────────────────────────────────────────────────────

    def start_heartbeat_monitor(self, markets: List[str]):
        """
        Background thread: check each market's WS last-message time.
        Log EV_WS_HEARTBEAT_GAP if no message for > WS_HEARTBEAT_TIMEOUT_S.
        """
        if self._heartbeat_monitor_active:
            return
        self._heartbeat_monitor_active = True

        def _monitor():
            while not self._shutdown.is_set():
                now = time.time()
                for mid in markets:
                    last = self._last_ws_msg_ts.get(mid, now)
                    gap  = now - last
                    if gap > config.WS_HEARTBEAT_TIMEOUT_S:
                        self.log(EV_WS_HEARTBEAT_GAP, {
                            "market_id":  mid,
                            "gap_seconds": round(gap, 1),
                            "ws_state":   self._ws_state.get(mid, "unknown"),
                        }, source="heartbeat_monitor")
                self._shutdown.wait(timeout=5.0)

        t = threading.Thread(target=_monitor, name="heartbeat-monitor", daemon=True)
        t.start()
        log.info("[runtime] Heartbeat monitor started for %d markets", len(markets))

    def notify_ws_msg(self, market_id: str):
        """Call this each time a WS message is received for a market."""
        self._last_ws_msg_ts[market_id] = time.time()

    # ── summary / query ───────────────────────────────────────────────────────

    def summary(self) -> Dict:
        """Return a summary dict for use in verdict_report."""
        with self._lock:
            counts = dict(self._counts)
            total  = len(self._events)

        rtt_stats: Dict[str, Dict] = {}
        for endpoint, samples in self._rtt_samples.items():
            if samples:
                rtt_stats[endpoint] = {
                    "count":  len(samples),
                    "min_ms": round(min(samples), 2),
                    "max_ms": round(max(samples), 2),
                    "mean_ms": round(sum(samples) / len(samples), 2),
                    "p95_ms": round(sorted(samples)[int(len(samples) * 0.95)], 2),
                }

        ws_disconnects = counts.get(EV_WS_DISCONNECT, 0)
        ws_errors      = counts.get(EV_WS_ERROR, 0) + counts.get(EV_WS_FATAL, 0)
        hb_gaps        = counts.get(EV_WS_HEARTBEAT_GAP, 0)
        rate_limits    = counts.get(EV_RATE_LIMIT, 0)
        rest_errors    = counts.get(EV_REST_ERROR, 0)
        order_rejects  = counts.get(EV_ORDER_REJECT, 0)

        return {
            "total_events":     total,
            "event_counts":     counts,
            "rtt_stats":        rtt_stats,
            "ws_disconnects":   ws_disconnects,
            "ws_errors":        ws_errors,
            "heartbeat_gaps":   hb_gaps,
            "rate_limits":      rate_limits,
            "rest_errors":      rest_errors,
            "order_rejects":    order_rejects,
            "worst_rtt_ms":     max(
                (v["max_ms"] for v in rtt_stats.values()), default=None
            ),
        }

    def get_events(self, event_type: Optional[str] = None) -> List[Dict]:
        """Return events from the ring buffer, optionally filtered by type."""
        with self._lock:
            events = list(self._events)
        if event_type:
            events = [e for e in events if e["event_type"] == event_type]
        return events

    def shutdown(self):
        self._shutdown.set()
        log.info("[runtime] RuntimeLogger shut down: session=%s", self.session_id)

    # ── callable interface for book_recorder WS callbacks ────────────────────

    def as_log_fn(self, source: str = "unknown") -> Callable:
        """Return a callable (event_type, detail) -> None compatible with WS callbacks."""
        def _log_fn(event_type: str, detail: Dict):
            self.log(event_type, detail, source=source)
        return _log_fn


# ── Probe: measure RTT to key endpoints ──────────────────────────────────────

def probe_endpoints(runtime_logger: RuntimeLogger) -> Dict[str, float]:
    """
    Make one request to each key endpoint and measure RTT.
    Returns {endpoint_label: latency_ms}. For use at session start.
    """
    import requests

    endpoints = [
        ("binance_ticker",  config.BINANCE_REST_TICKER),
        ("clob_markets",    config.CLOB_MARKETS),
    ]
    results: Dict[str, float] = {}
    for label, url in endpoints:
        t0 = time.monotonic()
        ok = False
        try:
            resp = requests.get(url, timeout=config.HTTP_TIMEOUT_S)
            resp.raise_for_status()
            ok = True
        except Exception as exc:
            log.warning("[runtime] Probe %s failed: %s", label, exc)

        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        results[label] = latency_ms
        runtime_logger.log(EV_REST_RTT, {
            "endpoint": label, "latency_ms": latency_ms, "ok": ok, "probe": True,
        }, source="probe_endpoints")
        log.info("[runtime] Probe %s: %.0fms ok=%s", label, latency_ms, ok)

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rl = RuntimeLogger(session_id)
    rl.log(EV_SESSION_START, {"message": "test session"}, source="cli")
    print("Probing endpoints...")
    rtts = probe_endpoints(rl)
    for ep, ms in rtts.items():
        print(f"  {ep}: {ms:.0f}ms")
    rl.log(EV_SESSION_END, {"message": "test session end"}, source="cli")
    print("\nSummary:")
    import pprint
    pprint.pprint(rl.summary())
    rl.shutdown()
