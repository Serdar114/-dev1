"""
reference_recorder.py - Record Binance BTC/USDT price at key time offsets
relative to each 5-minute market window's close.

Snapshot schedule per window:
  T-60, T-30, T-15, T-10, T-5, T-1  (seconds before close)
  T+0  (settlement / at-close)

What we store per snapshot:
  session_id, market_id, condition_id, yes_token_id, no_token_id,
  snapshot_label,       e.g. "T-60"
  target_utc,           when we aimed to capture
  actual_utc,           when we actually called Binance
  latency_ms,           actual_utc - target_utc
  btc_price_usdt,       Binance spot
  poly_mid_yes,         Polymarket mid-price of YES token (if available)
  poly_mid_no,          Polymarket mid-price of NO token (if available)
  poly_last_yes,        last trade price YES
  poly_last_no,         last trade price NO
  delta_open,           btc_price_usdt - price_at_window_open
  delta_pct_open,       delta_open / price_at_window_open * 100

Persistence:
  data/references/ref_<session_id>_<market_id_short>.jsonl
  data/references/ref_<session_id>.csv  (aggregate all markets)

Threading model:
  One thread per market window (markets are ~5 minutes, few at a time).
  Each thread sleeps until the next snapshot time, wakes, fetches, logs.
  A shared event signals shutdown.

No strategy. Pure measurement.
"""

import csv
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

import config

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_ts() -> float:
    return time.time()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat(timespec="milliseconds")


def _fetch_binance_price() -> tuple:
    """
    Return (price_usdt, latency_ms, actual_dt).
    price_usdt is float or None on failure.
    """
    t0 = time.monotonic()
    actual_dt = _utcnow()
    try:
        resp = requests.get(
            config.BINANCE_REST_TICKER,
            timeout=config.HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        price = float(data["price"])
        latency_ms = (time.monotonic() - t0) * 1000
        return price, round(latency_ms, 2), actual_dt
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        log.warning("Binance price fetch failed: %s (latency=%.0fms)", exc, latency_ms)
        return None, round(latency_ms, 2), actual_dt


def _fetch_poly_mid(token_id: Optional[str]) -> Optional[float]:
    """Fetch Polymarket mid-price for a token. Returns float or None."""
    if not token_id:
        return None
    try:
        resp = requests.get(
            config.CLOB_MID_PRICE,
            params={"token_id": token_id},
            timeout=config.HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        mid = data.get("mid") or data.get("price") or data.get("midpoint")
        return float(mid) if mid is not None else None
    except Exception as exc:
        log.debug("Poly mid fetch failed for token %s: %s", token_id, exc)
        return None


def _fetch_poly_last(token_id: Optional[str]) -> Optional[float]:
    """Fetch Polymarket last trade price for a token. Returns float or None."""
    if not token_id:
        return None
    try:
        resp = requests.get(
            config.CLOB_LAST_PRICE,
            params={"token_id": token_id},
            timeout=config.HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price") or data.get("last_trade_price")
        return float(price) if price is not None else None
    except Exception as exc:
        log.debug("Poly last price fetch failed for token %s: %s", token_id, exc)
        return None


# ── CSV writer (thread-safe, shared across markets in a session) ──────────────

class SessionCsvWriter:
    """Append rows to a shared session CSV, thread-safe."""

    FIELDS = [
        "session_id", "market_id", "condition_id",
        "yes_token_id", "no_token_id",
        "snapshot_label", "target_utc", "actual_utc",
        "capture_latency_ms",
        "btc_price_usdt", "poly_mid_yes", "poly_mid_no",
        "poly_last_yes", "poly_last_no",
        "price_at_open", "delta_open", "delta_pct_open",
        "fetch_ok",
    ]

    def __init__(self, session_id: str):
        self._path = config.REFS_DIR / f"ref_{session_id}.csv"
        self._lock = threading.Lock()
        with open(self._path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def write(self, row: Dict):
        with self._lock:
            with open(self._path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.FIELDS)
                writer.writerow({k: row.get(k, "") for k in self.FIELDS})


# ── Per-market recorder ───────────────────────────────────────────────────────

class MarketReferenceRecorder:
    """
    Records reference prices for a single market window.
    Designed to run in its own thread.
    """

    def __init__(
        self,
        session_id: str,
        market: Dict,
        csv_writer: SessionCsvWriter,
        shutdown_event: threading.Event,
    ):
        self.session_id = session_id
        self.market = market
        self.market_id       = market.get("market_id") or market.get("condition_id", "unknown")
        self.condition_id    = market.get("condition_id", "")
        self.yes_token_id    = market.get("yes_token_id")
        self.no_token_id     = market.get("no_token_id")
        self.csv_writer      = csv_writer
        self.shutdown        = shutdown_event

        end_str = market.get("end_time_utc")
        self.close_time: Optional[datetime] = None
        if end_str:
            try:
                self.close_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        self.price_at_open: Optional[float] = None
        self.snapshots: List[Dict] = []

        # Per-market JSONL
        short_id = self.market_id[:12].replace("/", "_")
        self._jsonl_path = config.REFS_DIR / f"ref_{session_id}_{short_id}.jsonl"

    def _seconds_to_close(self) -> Optional[float]:
        if self.close_time is None:
            return None
        return (self.close_time - _utcnow()).total_seconds()

    def _capture_snapshot(self, label: str, target_dt: datetime) -> Dict:
        """Fetch all data for one snapshot and return the record."""
        btc_price, latency_ms, actual_dt = _fetch_binance_price()
        poly_mid_yes  = _fetch_poly_mid(self.yes_token_id)
        poly_mid_no   = _fetch_poly_mid(self.no_token_id)
        poly_last_yes = _fetch_poly_last(self.yes_token_id)
        poly_last_no  = _fetch_poly_last(self.no_token_id)

        delta_open     = None
        delta_pct_open = None
        if btc_price is not None and self.price_at_open is not None:
            delta_open     = round(btc_price - self.price_at_open, 4)
            delta_pct_open = round(delta_open / self.price_at_open * 100, 6)

        rec = {
            "session_id":        self.session_id,
            "market_id":         self.market_id,
            "condition_id":      self.condition_id,
            "yes_token_id":      self.yes_token_id or "",
            "no_token_id":       self.no_token_id or "",
            "snapshot_label":    label,
            "target_utc":        _iso(target_dt),
            "actual_utc":        _iso(actual_dt),
            "capture_latency_ms": latency_ms,
            "btc_price_usdt":    btc_price,
            "poly_mid_yes":      poly_mid_yes,
            "poly_mid_no":       poly_mid_no,
            "poly_last_yes":     poly_last_yes,
            "poly_last_no":      poly_last_no,
            "price_at_open":     self.price_at_open,
            "delta_open":        delta_open,
            "delta_pct_open":    delta_pct_open,
            "fetch_ok":          btc_price is not None,
        }

        # Persist JSONL immediately
        with open(self._jsonl_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

        self.csv_writer.write(rec)

        log.info(
            "[ref] market=%s label=%-8s BTC=%-10s poly_mid_yes=%-6s "
            "delta_open=%-8s latency=%.0fms",
            self.market_id[:12],
            label,
            f"{btc_price:.2f}" if btc_price else "ERR",
            f"{poly_mid_yes:.4f}" if poly_mid_yes else "N/A",
            f"{delta_open:+.2f}" if delta_open is not None else "N/A",
            latency_ms,
        )

        return rec

    def _sleep_until(self, target_ts: float) -> bool:
        """Sleep until target unix timestamp. Return False if shutdown signalled."""
        while True:
            remaining = target_ts - time.time()
            if remaining <= 0:
                return True
            wait = min(remaining, 0.5)
            if self.shutdown.wait(timeout=wait):
                return False
        return True

    def run(self):
        """Main loop: take snapshots at each configured offset before close."""
        if self.close_time is None:
            log.error("market_id=%s has no close_time – cannot schedule snapshots", self.market_id)
            return

        close_ts = self.close_time.timestamp()

        # ── T-open snapshot (capture baseline price as soon as we start) ──────
        open_snap = self._capture_snapshot("T-open", _utcnow())
        if open_snap["btc_price_usdt"] is not None:
            self.price_at_open = open_snap["btc_price_usdt"]
        self.snapshots.append(open_snap)

        # ── Scheduled offset snapshots ─────────────────────────────────────────
        for offset_s in sorted(config.REF_OFFSETS_SECONDS, reverse=True):
            label     = f"T-{offset_s}"
            target_ts = close_ts - offset_s
            target_dt = datetime.fromtimestamp(target_ts, tz=timezone.utc)

            if not self._sleep_until(target_ts):
                log.info("Shutdown during ref recorder for market %s at %s", self.market_id, label)
                return

            snap = self._capture_snapshot(label, target_dt)
            self.snapshots.append(snap)

        # ── T-0 settlement snapshot ───────────────────────────────────────────
        if not self._sleep_until(close_ts):
            return

        snap = self._capture_snapshot("T-0", self.close_time)
        self.snapshots.append(snap)

        log.info("[ref] market=%s all snapshots complete (%d total)",
                 self.market_id, len(self.snapshots))


# ── Session-level entry point ─────────────────────────────────────────────────

def run_reference_recorders(
    session_id: str,
    markets: List[Dict],
    shutdown_event: threading.Event,
) -> Dict[str, MarketReferenceRecorder]:
    """
    Start one reference recorder thread per market.
    Returns dict of market_id -> recorder (threads are daemon threads).
    """
    csv_writer = SessionCsvWriter(session_id)
    recorders: Dict[str, MarketReferenceRecorder] = {}

    for market in markets:
        rec = MarketReferenceRecorder(
            session_id=session_id,
            market=market,
            csv_writer=csv_writer,
            shutdown_event=shutdown_event,
        )
        t = threading.Thread(target=rec.run, name=f"ref-{rec.market_id[:8]}", daemon=True)
        t.start()
        recorders[rec.market_id] = rec
        log.info("Reference recorder started for market %s", rec.market_id[:12])

    return recorders


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    """Quick test: record BTC price every offset for a synthetic 5-minute window."""
    import sys
    from datetime import timedelta

    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # Synthetic market closing in 90 seconds
    close_dt = _utcnow() + timedelta(seconds=90)

    synthetic_market = {
        "market_id":    "TEST_MARKET_001",
        "condition_id": "TEST_MARKET_001",
        "question":     "Will BTC be higher in 5 minutes? (SYNTHETIC TEST)",
        "yes_token_id": None,
        "no_token_id":  None,
        "end_time_utc": close_dt.isoformat(),
    }

    shutdown = threading.Event()
    print(f"Running synthetic reference recorder until {close_dt.isoformat()}")
    print("Press Ctrl-C to stop early.\n")

    recorders = run_reference_recorders(session_id, [synthetic_market], shutdown)

    try:
        # Wait for close_time + buffer
        time.sleep((close_dt - _utcnow()).total_seconds() + 5)
    except KeyboardInterrupt:
        shutdown.set()
        print("\nShutdown signalled.")

    for mid, rec in recorders.items():
        print(f"\nSnapshots for {mid}:")
        for s in rec.snapshots:
            print(f"  {s['snapshot_label']:8s}  BTC={s['btc_price_usdt']}  "
                  f"delta={s['delta_open']}")
