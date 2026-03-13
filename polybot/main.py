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

# Ensure polybot/ is on the path when invoked as a module or directly
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from binance_feed import BinanceFeed
from logger_utils import (
    log_signal,
    log_signal_rejected,
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

    try:
        while True:
            trader.reset_daily_pnl_if_new_day()

            now = time.time()
            current_window = _current_window(now)
            ste = _sec_to_expiry(now)

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
            # Capture when we're very early in the window (295–300s left)
            if ste >= 295 and not trader.has_open_price(current_window):
                if feed.mid_price is not None and not feed.is_stale:
                    trader.record_open_price(current_window, feed.mid_price)

            # ---- Entry window: generate and act on signal ----
            entry_min = config["signal"]["entry_window_sec"][1]  # 5
            entry_max = config["signal"]["entry_window_sec"][0]  # 30

            if entry_min <= ste <= entry_max:
                btc_open = trader.get_open_price(current_window)
                if btc_open is None:
                    # Fallback: use current price as open (last resort)
                    btc_open = feed.mid_price
                    if btc_open is not None:
                        trader.record_open_price(current_window, btc_open)
                        logger.warning(
                            "Open price not pre-recorded for window %d; "
                            "using current BTC=%.2f as proxy.",
                            current_window,
                            btc_open,
                        )

                if feed.mid_price is None or btc_open is None:
                    logger.warning("No BTC price available; skipping entry window tick.")
                    await asyncio.sleep(1)
                    continue

                # Fetch market (cache per window to avoid repeated API calls)
                if current_window not in market_cache:
                    market = pm.get_market(current_window)
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

                # Fetch implied price
                implied = pm.get_implied_price(market.yes_token_id)
                if implied is None:
                    logger.warning(
                        "No implied price for market %s; skipping.", market.slug
                    )
                    await asyncio.sleep(1)
                    continue

                # Evaluate signal
                sig = signal_eng.evaluate(
                    btc_current=feed.mid_price,
                    btc_open=btc_open,
                    implied_yes_price=implied,
                    seconds_to_expiry=ste,
                    market_slug=market.slug,
                )
                log_signal(sig, signal_file)

                if sig.action != "NO_TRADE":
                    risk_check = risk.check(sig, feed, trader)
                    if risk_check.ok:
                        trader.open_trade(sig, current_window)
                    else:
                        log_signal_rejected(sig, risk_check.reason, signal_file)

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
