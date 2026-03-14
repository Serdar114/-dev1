"""
main.py — Entry point and async event loop for the Polymarket BTC paper bot.

Flow per second:
  1. Resolve any open trade if its window has closed (sec_to_expiry > 280)
  2. Record BTC open price at the start of each 5-min window (~295-300s remaining)
  3. In the entry window (5–30s to expiry): fetch market, get implied price,
     evaluate signal, run risk checks, open paper trade if warranted.
  4. Log everything.

Run:
  python main.py
  Ctrl+C to stop cleanly.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure polybot/ is on the path when invoked as a module or directly
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from binance_feed import BinanceFeed
from logger_utils import (
    log_tick,
    log_trade,
    setup_logging,
)
from paper_trader import PaperTrader
from polymarket_client import PolymarketClient
from risk_manager import RiskManager
from signal_engine import SignalEngine

logger = logging.getLogger(__name__)

_CONFIG_PATH = _HERE / "config.json"


def load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _current_window(now: float) -> int:
    """Return the start timestamp of the current 5-min window."""
    return int(now) // 300 * 300


def _sec_to_expiry(now: float) -> float:
    """Seconds until the current 5-min window closes."""
    window = _current_window(now)
    return (window + 300) - now


async def main() -> None:
    setup_logging(logging.INFO)
    logger.info("=" * 60)
    logger.info("Polymarket BTC 5-min paper trading bot starting.")
    logger.info("=" * 60)

    config = load_config()
    signal_file: str = config["logs"]["signal_file"]
    trade_file: str = config["logs"]["trade_file"]

    # Resolve relative paths against polybot/ directory
    if not os.path.isabs(signal_file):
        signal_file = str(_HERE / signal_file)
    if not os.path.isabs(trade_file):
        trade_file = str(_HERE / trade_file)

    feed = BinanceFeed(config)
    pm = PolymarketClient(config)
    signal_eng = SignalEngine(config)
    trader = PaperTrader(config)
    risk = RiskManager(config)

    await feed.connect()

    # Give the WebSocket a moment to receive first price
    logger.info("Waiting for first Binance price tick…")
    for _ in range(15):
        if feed.mid_price is not None:
            break
        await asyncio.sleep(0.5)

    if feed.mid_price is None:
        logger.error("No price from Binance after 7.5s. Exiting.")
        await feed.disconnect()
        return

    logger.info("First BTC mid price: %.2f", feed.mid_price)

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    prev_window: int = 0  # track window transitions
    market_cache: dict[int, object] = {}  # window_ts -> MarketInfo (avoid repeated fetches)

    # Startup sync: if the bot starts mid-window (ste < 295) we skip the
    # current window entirely and begin trading from the next boundary.
    startup_synced: bool = False
    _init_sync_logged: bool = False

    try:
        while True:
            trader.reset_daily_pnl_if_new_day()

            now = time.time()
            current_window = _current_window(now)
            ste = _sec_to_expiry(now)

            # ---- Startup sync guard ----
            if not startup_synced:
                if ste < 295:
                    if not _init_sync_logged:
                        logger.info(
                            "INIT_SYNC | started mid-window ste=%.0fs "
                            "-> wait next boundary, no trading",
                            ste,
                        )
                        _init_sync_logged = True
                    prev_window = current_window  # suppress spurious "new window" log
                    await asyncio.sleep(1)
                    continue
                # ste >= 295: we are at a fresh window boundary — proceed
                startup_synced = True

            # ---- Window transition logging ----
            if current_window != prev_window:
                logger.info(
                    "New 5-min window: %d (UTC %s) | bankroll=%.4f",
                    current_window,
                    time.strftime("%H:%M:%S", time.gmtime(current_window)),
                    trader.bankroll,
                )
                prev_window = current_window

            # ---- Resolve open trade when new window starts ----
            # ste > 280 means we're in the first ~20s of a new window
            if trader.has_open_trade() and ste > 280:
                if feed.is_stale:
                    logger.warning(
                        "Cannot resolve trade: Binance feed is stale. "
                        "Will retry next second."
                    )
                else:
                    result = trader.resolve(feed.mid_price)
                    if result:
                        log_trade(result, trade_file)

            # ---- Record open price for this window ----
            # Capture once, at the very start of the window (295–300s left).
            # Never overwritten — the open is fixed for the whole window.
            if ste >= 295 and not trader.has_open_price(current_window):
                if feed.mid_price is not None and not feed.is_stale:
                    trader.record_open_price(current_window, feed.mid_price)
                    logger.info(
                        "OPEN_CAPTURED | window=%d btc_open=%.2f ste=%.2f",
                        current_window,
                        feed.mid_price,
                        ste,
                    )

            # ---- Entry window: generate and act on signal ----
            entry_min = config["signal"]["entry_window_sec"][1]  # 5
            entry_max = config["signal"]["entry_window_sec"][0]  # 30

            if entry_min <= ste <= entry_max:
                # btc_open must have been captured at window boundary.
                # Never substitute current price — delta would be meaningless.
                btc_open = trader.get_open_price(current_window)
                if btc_open is None:
                    logger.warning(
                        "SKIP_ENTRY | window=%d reason=no_true_open_captured",
                        current_window,
                    )
                    await asyncio.sleep(1)
                    continue

                if feed.mid_price is None:
                    logger.warning("No BTC price available; skipping entry window tick.")
                    await asyncio.sleep(1)
                    continue

                # Fetch market off the event loop (sync requests → thread pool).
                # Cached after first successful fetch for this window.
                if current_window not in market_cache:
                    t0 = time.perf_counter()
                    market = await asyncio.to_thread(pm.get_market, current_window)
                    market_fetch_ms = int((time.perf_counter() - t0) * 1000)
                    logger.info(
                        "Market fetch | window=%d took=%dms found=%s",
                        current_window,
                        market_fetch_ms,
                        market.slug if market else "None",
                    )
                    market_cache[current_window] = market
                    # Prune old windows from cache
                    for old_w in [w for w in market_cache if w < current_window - 300]:
                        del market_cache[old_w]
                else:
                    market = market_cache[current_window]

                if market is None:
                    logger.warning(
                        "No market for window %d; skipping signal generation.",
                        current_window,
                    )
                    await asyncio.sleep(1)
                    continue

                # Snapshot BTC price and its age before the blocking Polymarket call.
                btc_before_fetch = feed.mid_price
                btc_age_before_ms = int((time.time() - feed.last_update_ts) * 1000)

                # Fetch implied price off the event loop (every tick, not cached).
                t0 = time.perf_counter()
                implied = await asyncio.to_thread(
                    pm.get_implied_price, market.yes_token_id
                )
                pm_fetch_ms = int((time.perf_counter() - t0) * 1000)

                # BTC age measured after the fetch so we can see if it updated.
                btc_age_ms = int((time.time() - feed.last_update_ts) * 1000)

                if implied is None:
                    logger.warning(
                        "No implied price for market %s (fetch=%dms); skipping.",
                        market.slug,
                        pm_fetch_ms,
                    )
                    await asyncio.sleep(1)
                    continue

                # Evaluate signal using the freshest BTC price available.
                sig = signal_eng.evaluate(
                    btc_current=feed.mid_price,
                    btc_open=btc_open,
                    implied_yes_price=implied,
                    seconds_to_expiry=ste,
                    market_slug=market.slug,
                )

                # band_ok: True/False only if delta was large enough to reach the
                # price-band check; None if we never got there (delta too small).
                band_ok: Optional[bool] = None
                if sig.model_prob > 0.0:
                    band_ok = "price_out_of_band" not in sig.reason

                # risk_ok: only evaluated when signal is actionable
                risk_ok: Optional[bool] = None
                risk_reason = ""
                if sig.action != "NO_TRADE":
                    risk_check = risk.check(sig, feed, trader)
                    risk_ok = risk_check.ok
                    risk_reason = risk_check.reason

                # Unified reason: prefer risk rejection reason when applicable
                tick_reason = (
                    f"risk:{risk_reason}"
                    if (risk_reason and risk_ok is False)
                    else sig.reason
                )

                # One INFO line per entry-window tick (NO_TRADE and trades).
                # btc_age_ms: age of price used for signal; pm_fetch_ms: Polymarket latency.
                logger.info(
                    "TICK | window=%d ste=%.0fs btc=%.2f open=%.2f delta=%.3f%% "
                    "implied=%.4f model_prob=%.4f edge=%.4f "
                    "band_ok=%s risk_ok=%s action=%s reason=%s "
                    "btc_age=%dms pm_fetch=%dms",
                    current_window,
                    ste,
                    feed.mid_price,
                    btc_open,
                    sig.delta_pct,
                    sig.implied_prob,
                    sig.model_prob,
                    sig.edge,
                    "N/A" if band_ok is None else band_ok,
                    "N/A" if risk_ok is None else risk_ok,
                    sig.action,
                    tick_reason,
                    btc_age_ms,
                    pm_fetch_ms,
                )

                log_tick(
                    sig,
                    current_window,
                    band_ok,
                    risk_ok,
                    tick_reason,
                    signal_file,
                    btc_age_ms=btc_age_ms,
                    pm_fetch_ms=pm_fetch_ms,
                )

                if sig.action != "NO_TRADE" and risk_ok:
                    trader.open_trade(sig, current_window)

            await asyncio.sleep(1)

    except asyncio.CancelledError:
        logger.info("Main loop cancelled.")
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        logger.info("Shutting down…")
        await feed.disconnect()
        logger.info(
            "Final bankroll: %.4f (started %.2f)",
            trader.bankroll,
            config["initial_bankroll"],
        )
        logger.info("Total paper trades: %d", len(trader.trade_history))
        wins = sum(1 for t in trader.trade_history if t.result == "WIN")
        if trader.trade_history:
            logger.info(
                "Win rate: %d/%d = %.1f%%",
                wins,
                len(trader.trade_history),
                wins / len(trader.trade_history) * 100,
            )
        logger.info("Bot stopped.")


if __name__ == "__main__":
    # Handle Ctrl+C on Windows and Unix cleanly
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
