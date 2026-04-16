"""
Volatility engine: computes 60-minute realised annualised volatility from
Chainlink BTC/USD price ticks.

One bar = the last price seen within a given UTC minute.
A bar is committed when a tick arrives whose minute_ts exceeds the current
accumulated minute_ts (i.e. on the first tick of the next minute).

Annualised vol = std(log_returns) × sqrt(525_600)  [minutes/year]
Outlier filter: bars whose |return − mean| > 5σ are excluded before
computing the final std.

State is persisted atomically to state/chainlink_price_history.jsonl every
60 seconds and on explicit force_persist() calls.  On startup the last
hour of bars is restored, skipping the 60-bar warmup when enough history
exists.
"""
import json
import math
import os
import pathlib
import threading
import time
from collections import deque
from typing import Optional

STATE_FILE = pathlib.Path("state") / "chainlink_price_history.jsonl"
MAX_BARS = 60
PERSIST_INTERVAL_S = 60
ANNUALIZE_FACTOR = math.sqrt(525_600)   # sqrt(minutes per year)
OUTLIER_SIGMA_THRESHOLD = 5.0


class VolEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Committed bars: deque of (minute_ts_seconds, close_price)
        self._bars: deque = deque(maxlen=MAX_BARS)

        # In-progress accumulator for the current UTC minute
        self._current_minute_ts: Optional[int] = None
        self._current_minute_last_price: Optional[float] = None

        self._last_persist_ts: float = 0.0

        self._load_state()

    # ------------------------------------------------------------------ #
    # State persistence                                                    #
    # ------------------------------------------------------------------ #

    def _load_state(self) -> None:
        """Load bars from the state file, keeping only the last hour."""
        if not STATE_FILE.exists():
            return

        now_s = int(time.time())
        cutoff_s = now_s - 3600

        try:
            with open(str(STATE_FILE), "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        minute_ts = rec.get("minute_ts")
                        price = rec.get("chainlink_price")
                        if (
                            minute_ts is not None
                            and price is not None
                            and int(minute_ts) >= cutoff_s
                        ):
                            self._bars.append((int(minute_ts), float(price)))
                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue
        except OSError:
            pass

    def _write_bars_to_disk(self, bars: list) -> None:
        """Atomic rewrite of the state file (.tmp + os.replace)."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            with open(str(tmp), "w", encoding="utf-8") as fh:
                for minute_ts, price in bars:
                    rec = {"minute_ts": minute_ts, "chainlink_price": price}
                    fh.write(json.dumps(rec) + "\n")
            os.replace(str(tmp), str(STATE_FILE))
        except OSError:
            pass

    def force_persist(self) -> None:
        """Flush current bar history to disk immediately (call on shutdown)."""
        with self._lock:
            bars = list(self._bars)
        self._write_bars_to_disk(bars)

    # ------------------------------------------------------------------ #
    # Price ingestion                                                      #
    # ------------------------------------------------------------------ #

    def push_price(self, price: float, source_ts_ms: int) -> None:
        """
        Record a new Chainlink price observation.

        source_ts_ms is the exchange-side timestamp in milliseconds.
        On the first tick of a new UTC minute the previous minute's last
        price is committed as a completed bar.
        """
        minute_ts = (source_ts_ms // 1000 // 60) * 60  # floor to minute boundary
        should_persist = False
        bars_snapshot: Optional[list] = None

        with self._lock:
            if self._current_minute_ts is None:
                self._current_minute_ts = minute_ts
                self._current_minute_last_price = price
            elif minute_ts > self._current_minute_ts:
                # Commit the just-completed minute
                self._bars.append(
                    (self._current_minute_ts, self._current_minute_last_price)
                )
                self._current_minute_ts = minute_ts
                self._current_minute_last_price = price
            else:
                # Still within the same minute — update the running last price
                self._current_minute_last_price = price

            now_s = time.monotonic()
            if now_s - self._last_persist_ts >= PERSIST_INTERVAL_S:
                self._last_persist_ts = now_s
                bars_snapshot = list(self._bars)
                should_persist = True

        if should_persist and bars_snapshot is not None:
            self._write_bars_to_disk(bars_snapshot)

    # ------------------------------------------------------------------ #
    # Volatility computation                                               #
    # ------------------------------------------------------------------ #

    def get_annualized_vol(self) -> Optional[float]:
        """
        Return annualised realised volatility based on committed minute bars.
        Returns None if fewer than MAX_BARS bars have been committed.
        """
        with self._lock:
            bars = list(self._bars)

        if len(bars) < MAX_BARS:
            return None

        prices = [p for _, p in bars]

        log_returns = []
        for i in range(1, len(prices)):
            prev, curr = prices[i - 1], prices[i]
            if prev > 0 and curr > 0:
                log_returns.append(math.log(curr / prev))

        if len(log_returns) < 2:
            return None

        # Outlier filter: remove returns where |r − mean| > 5σ
        mean_r = sum(log_returns) / len(log_returns)
        var_raw = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        std_raw = math.sqrt(var_raw) if var_raw > 0 else 0.0

        if std_raw > 0:
            filtered = [
                r for r in log_returns
                if abs(r - mean_r) <= OUTLIER_SIGMA_THRESHOLD * std_raw
            ]
        else:
            filtered = log_returns

        if len(filtered) < 2:
            return None

        mean_f = sum(filtered) / len(filtered)
        var_f = sum((r - mean_f) ** 2 for r in filtered) / (len(filtered) - 1)
        std_f = math.sqrt(var_f) if var_f > 0 else 0.0

        return std_f * ANNUALIZE_FACTOR

    def bar_count(self) -> int:
        with self._lock:
            return len(self._bars)
