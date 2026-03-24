"""
book_recorder.py - Record Polymarket order book state for BTC 5-minute markets.

Captures:
  - Best bid, best ask, mid-price, spread (absolute and %)
  - Top-of-book USDC depth (bid_depth_usdc, ask_depth_usdc)
  - Full ladder snapshots (top BOOK_TOP_N_LEVELS)
  - Sub-sampling: dense in last 60s of window

Two capture modes:
  1. REST polling  – primary, always active, every BOOK_SNAPSHOT_INTERVAL_MS
  2. WebSocket     – supplementary, subscribes to Polymarket WS for real-time updates
                     logged separately, not used to replace REST

Persistence per market:
  data/books/book_<session_id>_<short_market_id>.jsonl   – every snapshot
  data/books/book_<session_id>_<short_market_id>_ladder.jsonl – full ladder (larger)
  data/books/book_summary_<session_id>.csv              – flattened top-of-book

No strategy. Measurement only.
"""

import csv
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests
import websocket  # websocket-client

import config

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts_ms() -> int:
    """Current unix timestamp in milliseconds."""
    return int(time.time() * 1000)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_level(level: Any) -> Optional[Tuple[float, float]]:
    """
    Parse a book level which may be:
      [price, size]  or  {"price": ..., "size": ...}
    Returns (price, size) floats or None.
    """
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        try:
            return float(level[0]), float(level[1])
        except (ValueError, TypeError):
            return None
    if isinstance(level, dict):
        try:
            p = level.get("price") or level.get("p")
            s = level.get("size") or level.get("s")
            if p is not None and s is not None:
                return float(p), float(s)
        except (ValueError, TypeError):
            return None
    return None


def _parse_book(raw: Dict) -> Tuple[List[Tuple], List[Tuple]]:
    """
    Parse raw CLOB book response into sorted bids and asks.
    Returns (bids, asks) where each is a list of (price, size) tuples.
    Bids: descending by price. Asks: ascending by price.
    """
    bids_raw = raw.get("bids", []) or []
    asks_raw = raw.get("asks", []) or []

    bids = sorted(
        [p for p in (_parse_level(l) for l in bids_raw) if p is not None],
        key=lambda x: -x[0],
    )
    asks = sorted(
        [p for p in (_parse_level(l) for l in asks_raw) if p is not None],
        key=lambda x: x[0],
    )
    return bids, asks


def _compute_metrics(
    bids: List[Tuple], asks: List[Tuple]
) -> Dict[str, Optional[float]]:
    """Compute top-of-book metrics from parsed bids/asks lists."""
    best_bid = bids[0][0]  if bids else None
    bid_sz   = bids[0][1]  if bids else None
    best_ask = asks[0][0]  if asks else None
    ask_sz   = asks[0][1]  if asks else None

    mid = None
    spread_abs = None
    spread_pct = None

    if best_bid is not None and best_ask is not None:
        mid        = round((best_bid + best_ask) / 2, 6)
        spread_abs = round(best_ask - best_bid, 6)
        if mid > 0:
            spread_pct = round(spread_abs / mid * 100, 4)

    # Top-of-book USDC depth
    bid_depth_usdc = round(best_bid * bid_sz, 4) if (best_bid and bid_sz) else None
    ask_depth_usdc = round(best_ask * ask_sz, 4) if (best_ask and ask_sz) else None

    # Aggregated depth across top N levels
    bid_depth_n = None
    ask_depth_n = None
    if bids:
        bid_depth_n = round(sum(p * s for p, s in bids[:config.BOOK_TOP_N_LEVELS]), 4)
    if asks:
        ask_depth_n = round(sum(p * s for p, s in asks[:config.BOOK_TOP_N_LEVELS]), 4)

    return {
        "best_bid":       best_bid,
        "best_ask":       best_ask,
        "bid_size":       bid_sz,
        "ask_size":       ask_sz,
        "mid":            mid,
        "spread_abs":     spread_abs,
        "spread_pct":     spread_pct,
        "bid_depth_usdc": bid_depth_usdc,
        "ask_depth_usdc": ask_depth_usdc,
        "bid_depth_top_n_usdc": bid_depth_n,
        "ask_depth_top_n_usdc": ask_depth_n,
        "bid_levels":     len(bids),
        "ask_levels":     len(asks),
    }


def _fetch_book(token_id: str) -> Tuple[Optional[Dict], float]:
    """
    Fetch raw book from CLOB REST.
    Returns (raw_response_dict, latency_ms).
    """
    t0 = time.monotonic()
    try:
        resp = requests.get(
            config.CLOB_BOOK,
            params={"token_id": token_id},
            timeout=config.HTTP_TIMEOUT_S,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()
        return resp.json(), round(latency_ms, 2)
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        log.warning("Book fetch failed for token %s: %s (%.0fms)", token_id, exc, latency_ms)
        return None, round(latency_ms, 2)


# ── CSV writer ────────────────────────────────────────────────────────────────

class BookSummaryCsvWriter:
    FIELDS = [
        "session_id", "market_id", "token_id", "side_label",
        "snapshot_ts_ms", "snapshot_utc",
        "seconds_to_close",
        "best_bid", "best_ask", "bid_size", "ask_size",
        "mid", "spread_abs", "spread_pct",
        "bid_depth_usdc", "ask_depth_usdc",
        "bid_depth_top_n_usdc", "ask_depth_top_n_usdc",
        "bid_levels", "ask_levels",
        "rest_latency_ms", "fetch_ok",
    ]

    def __init__(self, session_id: str):
        self._path = config.BOOKS_DIR / f"book_summary_{session_id}.csv"
        self._lock = threading.Lock()
        with open(self._path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def write(self, row: Dict):
        with self._lock:
            with open(self._path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writerow(
                    {k: row.get(k, "") for k in self.FIELDS}
                )


# ── Per-market book recorder ──────────────────────────────────────────────────

class MarketBookRecorder:
    """
    REST-polling book recorder for a single token (YES or NO side).
    WebSocket is separate (see WSBookRecorder below).
    """

    def __init__(
        self,
        session_id: str,
        market: Dict,
        token_id: str,
        side_label: str,          # "YES" or "NO"
        close_ts: float,          # unix timestamp of market close
        csv_writer: BookSummaryCsvWriter,
        shutdown_event: threading.Event,
    ):
        self.session_id  = session_id
        self.market_id   = market.get("market_id") or market.get("condition_id", "unknown")
        self.token_id    = token_id
        self.side_label  = side_label
        self.close_ts    = close_ts
        self.csv_writer  = csv_writer
        self.shutdown    = shutdown_event

        short_id = self.market_id[:12].replace("/", "_")
        self._jsonl_path  = config.BOOKS_DIR / f"book_{session_id}_{short_id}_{side_label.lower()}.jsonl"
        self._ladder_path = config.BOOKS_DIR / f"book_{session_id}_{short_id}_{side_label.lower()}_ladder.jsonl"

        # Ring buffer: last N snapshots (used by shadow evaluators)
        self.recent: Deque[Dict] = deque(maxlen=120)

    def _seconds_to_close(self) -> float:
        return self.close_ts - time.time()

    def _interval_ms(self) -> int:
        stc = self._seconds_to_close()
        if stc <= 60:
            return config.BOOK_LAST60_INTERVAL_MS
        return config.BOOK_SNAPSHOT_INTERVAL_MS

    def _snapshot(self) -> Dict:
        stc = self._seconds_to_close()
        ts_ms = _ts_ms()
        utc = _utcnow_iso()

        raw, latency_ms = _fetch_book(self.token_id)

        if raw is None:
            snap = {
                "session_id":     self.session_id,
                "market_id":      self.market_id,
                "token_id":       self.token_id,
                "side_label":     self.side_label,
                "snapshot_ts_ms": ts_ms,
                "snapshot_utc":   utc,
                "seconds_to_close": round(stc, 3),
                "fetch_ok":       False,
                "rest_latency_ms": latency_ms,
            }
        else:
            bids, asks = _parse_book(raw)
            metrics = _compute_metrics(bids, asks)

            # Full ladder (top N levels)
            ladder_entry = {
                "ts_ms":     ts_ms,
                "token_id":  self.token_id,
                "bids":      bids[:config.BOOK_TOP_N_LEVELS],
                "asks":      asks[:config.BOOK_TOP_N_LEVELS],
            }

            with open(self._ladder_path, "a") as lf:
                lf.write(json.dumps(ladder_entry) + "\n")

            snap = {
                "session_id":     self.session_id,
                "market_id":      self.market_id,
                "token_id":       self.token_id,
                "side_label":     self.side_label,
                "snapshot_ts_ms": ts_ms,
                "snapshot_utc":   utc,
                "seconds_to_close": round(stc, 3),
                "rest_latency_ms": latency_ms,
                "fetch_ok":       True,
                **metrics,
            }

        with open(self._jsonl_path, "a") as f:
            f.write(json.dumps(snap) + "\n")
        self.csv_writer.write(snap)
        self.recent.append(snap)

        if raw is not None:
            log.debug(
                "[book] %s %s stc=%.1fs bid=%-7s ask=%-7s spread=%-6s depth_b=%-7s latency=%.0fms",
                self.market_id[:10], self.side_label,
                stc,
                snap.get("best_bid", "N/A"),
                snap.get("best_ask", "N/A"),
                f"{snap.get('spread_pct', 0):.2f}%",
                snap.get("bid_depth_usdc", "N/A"),
                latency_ms,
            )

        return snap

    def run(self):
        log.info("[book] Starting REST recorder: market=%s token=%s %s",
                 self.market_id[:12], self.token_id[:12] if self.token_id else "?", self.side_label)

        while not self.shutdown.is_set():
            if self._seconds_to_close() < -10:
                log.info("[book] market=%s %s: market closed, stopping", self.market_id[:12], self.side_label)
                break

            self._snapshot()

            interval_s = self._interval_ms() / 1000
            if self.shutdown.wait(timeout=interval_s):
                break

        log.info("[book] REST recorder stopped: market=%s %s", self.market_id[:12], self.side_label)


# ── WebSocket supplementary recorder ─────────────────────────────────────────

class WSBookRecorder:
    """
    Subscribe to Polymarket's WebSocket for a market's order book updates.
    Logs raw messages with millisecond timestamps. Does NOT replace REST polling.
    Feeds runtime_logger with disconnect/reconnect events.

    Polymarket WS message format (subscriptions):
    {
      "type": "subscribe",
      "channel": "market",
      "markets": ["<token_id>"]
    }

    Updates arrive as:
    {
      "event_type": "book",
      "asset_id": "<token_id>",
      "bids": [...],
      "asks": [...]
    }
    """

    def __init__(
        self,
        session_id: str,
        market: Dict,
        token_ids: List[str],
        close_ts: float,
        runtime_log_fn,           # callable(event_type, detail_dict)
        shutdown_event: threading.Event,
    ):
        self.session_id  = session_id
        self.market_id   = market.get("market_id") or market.get("condition_id", "unknown")
        self.token_ids   = [t for t in token_ids if t]
        self.close_ts    = close_ts
        self.runtime_log = runtime_log_fn
        self.shutdown    = shutdown_event
        self._ws: Optional[websocket.WebSocketApp] = None

        short_id = self.market_id[:12].replace("/", "_")
        self._ws_jsonl = config.BOOKS_DIR / f"book_ws_{session_id}_{short_id}.jsonl"
        self._last_msg_ts: float = 0.0
        self._connect_count: int = 0

    def _on_open(self, ws):
        self._connect_count += 1
        self._last_msg_ts = time.time()
        log.info("[ws-book] Connected (#%d) for market=%s", self._connect_count, self.market_id[:12])
        self.runtime_log("ws_connect", {
            "market_id": self.market_id, "connect_count": self._connect_count,
            "ts_ms": _ts_ms(),
        })
        sub_msg = json.dumps({
            "type": "subscribe",
            "channel": "market",
            "markets": self.token_ids,
        })
        ws.send(sub_msg)

    def _on_message(self, ws, message):
        recv_ts_ms = _ts_ms()
        self._last_msg_ts = time.time()
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            data = {"raw": message}

        entry = {"recv_ts_ms": recv_ts_ms, "recv_utc": _utcnow_iso(), **data}
        with open(self._ws_jsonl, "a") as f:
            f.write(json.dumps(entry) + "\n")

        event_type = data.get("event_type", "")
        if event_type == "book":
            log.debug("[ws-book] book update for %s at %d", self.market_id[:10], recv_ts_ms)

    def _on_error(self, ws, error):
        log.warning("[ws-book] error market=%s: %s", self.market_id[:12], error)
        self.runtime_log("ws_error", {
            "market_id": self.market_id, "error": str(error), "ts_ms": _ts_ms(),
        })

    def _on_close(self, ws, code, reason):
        log.info("[ws-book] Closed market=%s code=%s reason=%s", self.market_id[:12], code, reason)
        self.runtime_log("ws_disconnect", {
            "market_id": self.market_id, "code": code, "reason": reason, "ts_ms": _ts_ms(),
        })

    def run(self):
        if not self.token_ids:
            log.warning("[ws-book] No token IDs for market=%s – skipping WS", self.market_id[:12])
            return

        while not self.shutdown.is_set() and time.time() < self.close_ts + 5:
            try:
                self._ws = websocket.WebSocketApp(
                    config.CLOB_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                log.error("[ws-book] Fatal WS error market=%s: %s – reconnecting in 2s", self.market_id[:12], exc)
                self.runtime_log("ws_fatal", {"market_id": self.market_id, "error": str(exc), "ts_ms": _ts_ms()})

            if not self.shutdown.is_set():
                time.sleep(2)   # brief backoff before reconnect

        log.info("[ws-book] WS recorder stopped for market=%s", self.market_id[:12])

    def stop(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


# ── Session-level entry point ─────────────────────────────────────────────────

def run_book_recorders(
    session_id: str,
    markets: List[Dict],
    shutdown_event: threading.Event,
    runtime_log_fn=None,
) -> Dict[str, Dict]:
    """
    Launch REST polling + WS threads for each market.
    Returns dict of market_id -> {"yes": MarketBookRecorder, "no": MarketBookRecorder, "ws": WSBookRecorder}
    """
    if runtime_log_fn is None:
        def runtime_log_fn(etype, detail): pass

    csv_writer = BookSummaryCsvWriter(session_id)
    result: Dict[str, Dict] = {}

    for market in markets:
        mid       = market.get("market_id") or market.get("condition_id", "?")
        yes_tok   = market.get("yes_token_id")
        no_tok    = market.get("no_token_id")
        end_str   = market.get("end_time_utc")
        close_ts  = 0.0

        if end_str:
            try:
                close_ts = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass

        if close_ts == 0.0:
            log.error("[book] market=%s has no end_time – cannot schedule", mid[:12])
            continue

        recorders: Dict = {}

        for token_id, side_label in [(yes_tok, "YES"), (no_tok, "NO")]:
            if not token_id:
                log.warning("[book] market=%s missing %s token_id", mid[:12], side_label)
                continue
            rec = MarketBookRecorder(
                session_id=session_id, market=market,
                token_id=token_id, side_label=side_label,
                close_ts=close_ts, csv_writer=csv_writer,
                shutdown_event=shutdown_event,
            )
            t = threading.Thread(
                target=rec.run,
                name=f"book-rest-{side_label.lower()}-{mid[:8]}",
                daemon=True,
            )
            t.start()
            recorders[side_label.lower()] = rec

        token_ids = [t for t in [yes_tok, no_tok] if t]
        ws_rec = WSBookRecorder(
            session_id=session_id, market=market,
            token_ids=token_ids, close_ts=close_ts,
            runtime_log_fn=runtime_log_fn,
            shutdown_event=shutdown_event,
        )
        ws_thread = threading.Thread(
            target=ws_rec.run,
            name=f"book-ws-{mid[:8]}",
            daemon=True,
        )
        ws_thread.start()
        recorders["ws"] = ws_rec

        result[mid] = recorders
        log.info("[book] Launched recorders for market=%s (YES+NO REST + WS)", mid[:12])

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    """Quick test against a known token ID. Usage: python book_recorder.py <token_id>"""
    import sys
    token_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not token_id:
        print("Usage: python book_recorder.py <polymarket_token_id>")
        print("Fetching a sample book from CLOB to test parsing...")
        # Just test the REST fetch with a placeholder token
        raw, lat = _fetch_book("0x1234")
        print(f"Fetch result: {raw} latency={lat}ms")
        raise SystemExit(0)

    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    shutdown = threading.Event()
    from datetime import timedelta
    close_dt = datetime.now(timezone.utc) + timedelta(seconds=60)
    close_ts = close_dt.timestamp()
    csv_writer = BookSummaryCsvWriter(session_id)
    market = {"market_id": "CLI_TEST", "condition_id": "CLI_TEST",
               "yes_token_id": token_id, "no_token_id": None,
               "end_time_utc": close_dt.isoformat()}
    rec = MarketBookRecorder(
        session_id=session_id, market=market,
        token_id=token_id, side_label="YES",
        close_ts=close_ts, csv_writer=csv_writer,
        shutdown_event=shutdown,
    )
    print(f"Recording book for token {token_id} for 60 seconds. Press Ctrl-C to stop.")
    t = threading.Thread(target=rec.run, daemon=True)
    t.start()
    try:
        time.sleep(70)
    except KeyboardInterrupt:
        shutdown.set()
    print(f"Done. Snapshots: {len(rec.recent)}")
