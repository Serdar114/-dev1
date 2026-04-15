"""
vol_engine.py — Realized volatility engine (PATCH: new module).

Tracks Chainlink price in minute buckets (deque, maxlen=60).
Computes annualized realized vol once 60 bars are available.
Persists state to state/chainlink_price_history.jsonl so sessions can resume
without re-warming.

Minute-bar logic:
  - The last Chainlink price received during minute M is committed as the bar
    for minute M when minute M+1 begins.
  - push_price() is called by the main thread on every observation tick (~2s).

Realized vol formula:
  returns_i = log(price_i / price_{i-1})   for i = 1..59
  Outliers removed: |r - mean| > 5σ
  annualized_vol = std(filtered_returns) × sqrt(525_600)
  where 525_600 = minutes per year (365 × 24 × 60)

State persistence:
  Write: atomic rewrite (→ .tmp, then os.replace) every 60 seconds.
  Read:  on startup, load bars where minute_ts > now - 3600.
         If < 60 bars loaded, warmup continues normally.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

log = logging.getLogger(__name__)

_STATE_PATH = "state/chainlink_price_history.jsonl"
_MAX_BARS = 60
_MINUTES_PER_YEAR = 525_600          # 365 × 24 × 60
_SIGMA_OUTLIER_THRESHOLD = 5.0
_SAVE_INTERVAL_S = 60.0
_STALE_HISTORY_S = 3_600             # discard bars older than 1 h on load


class VolEngine:
    """
    Thread-safe. All public methods may be called from any thread.
    In practice push_price and get_annualized_vol are called from the main
    observation thread; no contention expected.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Each element: (minute_ts_seconds: int, chainlink_price: float)
        self._bars: Deque[Tuple[int, float]] = deque(maxlen=_MAX_BARS)
        self._current_minute: Optional[int] = None    # floor(ts_ms / 60_000) × 60
        self._last_price: Optional[float] = None
        self._last_save_ts: float = 0.0

        self._load_state()

    # ── public ────────────────────────────────────────────────────────────────

    def push_price(
        self,
        price: float,
        source_ts_ms: Optional[int] = None,
    ) -> None:
        """
        Record a new Chainlink price.  Commits previous minute's last price
        when a minute boundary is crossed.
        source_ts_ms: Chainlink-provided timestamp in ms; falls back to wall
        clock if None.
        """
        ts_ms = source_ts_ms if source_ts_ms is not None else int(time.time() * 1000)
        minute_ts = (ts_ms // 60_000) * 60  # floor to minute, in seconds

        with self._lock:
            if self._current_minute is None:
                self._current_minute = minute_ts

            if minute_ts > self._current_minute:
                # Minute boundary crossed — commit previous minute's last price
                if self._last_price is not None:
                    self._bars.append((self._current_minute, self._last_price))
                    log.debug(
                        "vol_engine: bar committed minute=%d price=%.4f bars=%d",
                        self._current_minute, self._last_price, len(self._bars),
                    )
                self._current_minute = minute_ts

            self._last_price = price

            # Periodic state persistence
            now = time.time()
            if now - self._last_save_ts >= _SAVE_INTERVAL_S:
                self._save_state_locked()
                self._last_save_ts = now

    def get_annualized_vol(self) -> Optional[float]:
        """
        Returns annualized realized vol (0–∞ range, e.g. 0.65 = 65%).
        Returns None when fewer than 60 bars are available (warmup phase).
        """
        with self._lock:
            return self._compute_vol_locked()

    def bar_count(self) -> int:
        with self._lock:
            return len(self._bars)

    # ── vol computation ───────────────────────────────────────────────────────

    def _compute_vol_locked(self) -> Optional[float]:
        if len(self._bars) < _MAX_BARS:
            return None

        prices = [p for _, p in self._bars]

        # Build log returns
        returns = []
        for i in range(1, len(prices)):
            p_prev, p_curr = prices[i - 1], prices[i]
            if p_prev > 0 and p_curr > 0:
                returns.append(math.log(p_curr / p_prev))

        if len(returns) < 2:
            return None

        # First-pass stats for outlier filter
        n = len(returns)
        mean_r = sum(returns) / n
        var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 0.0

        # Remove 5-sigma outliers (flash-crash artifact protection)
        if std_r > 0:
            filtered = [r for r in returns if abs(r - mean_r) <= _SIGMA_OUTLIER_THRESHOLD * std_r]
            if len(filtered) < 2:
                filtered = returns  # fallback: keep all if filter is too aggressive
        else:
            filtered = returns

        # Final vol on filtered returns
        n_f = len(filtered)
        mean_f = sum(filtered) / n_f
        var_f = sum((r - mean_f) ** 2 for r in filtered) / (n_f - 1)
        std_f = math.sqrt(var_f) if var_f > 0 else 0.0

        return std_f * math.sqrt(_MINUTES_PER_YEAR)

    # ── state persistence ─────────────────────────────────────────────────────

    def _save_state_locked(self) -> None:
        """Atomic rewrite of state file. Must be called with _lock held."""
        state_dir = os.path.dirname(_STATE_PATH)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        tmp_path = _STATE_PATH + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                for minute_ts, price in self._bars:
                    fh.write(
                        json.dumps({"minute_ts": minute_ts, "chainlink_price": price}) + "\n"
                    )
            os.replace(tmp_path, _STATE_PATH)
            log.debug("vol_engine: state saved (%d bars)", len(self._bars))
        except Exception as exc:
            log.warning("vol_engine: state save failed: %s", exc)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _load_state(self) -> None:
        if not os.path.exists(_STATE_PATH):
            log.info("vol_engine: no state file found — starting warmup from scratch")
            return

        cutoff_s = time.time() - _STALE_HISTORY_S
        loaded = 0
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    minute_ts = int(record["minute_ts"])
                    price = float(record["chainlink_price"])
                    if minute_ts >= cutoff_s:
                        self._bars.append((minute_ts, price))
                        loaded += 1
        except Exception as exc:
            log.warning("vol_engine: state load failed (%s) — starting warmup from scratch", exc)
            self._bars.clear()
            return

        self._last_save_ts = time.time()
        warmup = len(self._bars) < _MAX_BARS
        log.info(
            "vol_engine: loaded %d bars from state (warmup=%s, bars_needed=%d)",
            loaded, warmup, max(0, _MAX_BARS - len(self._bars)),
        )
