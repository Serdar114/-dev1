"""
logger_utils.py — Structured logging to JSONL and CSV files.

Manages two log files:
  logs/signals.jsonl — every entry-window tick (NO_TRADE and trades)
  logs/trades.csv    — every resolved paper trade with PnL

Creates the logs/ directory and CSV header automatically on first write.
"""

import csv
import json
import logging
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from signal_engine import SignalResult
    from paper_trader import TradeResult

logger = logging.getLogger(__name__)

_CSV_HEADER = [
    "timestamp",
    "market",
    "side",
    "fill_price",
    "stake",
    "fee",
    "btc_open",
    "btc_close",
    "delta_pct",
    "edge",
    "result",
    "pnl",
    "bankroll",
]


def _ensure_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
        logger.info("Created log directory: %s", directory)


def log_tick(
    signal: "SignalResult",
    window_ts: int,
    band_ok: Optional[bool],
    risk_ok: Optional[bool],
    reason: str,
    signal_file: str,
    btc_age_ms: int = 0,
    pm_fetch_ms: int = 0,
) -> None:
    """
    Append one entry-window tick to the JSONL signals file.
    Written for every tick — NO_TRADE and actionable signals alike.

    Fields
    ------
    window_ts   : start of the 5-min window (unix seconds)
    ste         : seconds to expiry at evaluation time
    btc         : BTC mid price at evaluation time
    open        : BTC open price recorded at window start
    delta_pct   : (btc - open) / open * 100
    implied_price: YES token implied price from Polymarket
    model_prob  : model's estimated YES probability
    edge        : model_prob - trade_price - fee  (0 when no trade signal)
    band_ok     : True/False if band check was reached; null if delta too small
    risk_ok     : True/False result of RiskManager; null if signal was NO_TRADE
    action      : "UP" | "DOWN" | "NO_TRADE"
    reason      : why we traded or didn't (or risk rejection reason)
    btc_age_ms  : milliseconds since last Binance price tick
    pm_fetch_ms : milliseconds taken to fetch Polymarket implied price
    """
    _ensure_dir(signal_file)
    record = {
        "window_ts": window_ts,
        "ste": round(signal.seconds_to_expiry, 1),
        "btc": round(signal.btc_spot, 2),
        "open": round(signal.btc_open, 2),
        "delta_pct": round(signal.delta_pct, 6),
        "implied_price": round(signal.implied_prob, 6),
        "model_prob": round(signal.model_prob, 6),
        "edge": round(signal.edge, 6),
        "band_ok": band_ok,
        "risk_ok": risk_ok,
        "action": signal.action,
        "reason": reason,
        "btc_age_ms": btc_age_ms,
        "pm_fetch_ms": pm_fetch_ms,
    }
    try:
        with open(signal_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.error("Failed to write tick log: %s", exc)


def log_trade(trade_result: "TradeResult", trade_file: str) -> None:
    """
    Append a resolved trade to the CSV trade log.
    Writes CSV header if the file is new/empty.
    """
    _ensure_dir(trade_file)
    write_header = not os.path.exists(trade_file) or os.path.getsize(trade_file) == 0
    t = trade_result.trade
    row = {
        "timestamp": trade_result.resolved_at,
        "market": t.market_slug,
        "side": t.action,
        "fill_price": round(t.fill_price, 6),
        "stake": t.stake,
        "fee": round(t.fee, 6),
        "btc_open": round(t.btc_open, 2),
        "btc_close": round(trade_result.btc_close, 2),
        "delta_pct": round(t.delta_pct, 6),
        "edge": round(t.edge, 6),
        "result": trade_result.result,
        "pnl": round(trade_result.pnl, 6),
        "bankroll": round(trade_result.bankroll_after, 4),
    }
    try:
        with open(trade_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADER)
            if write_header:
                writer.writeheader()
                logger.info("CSV header written to %s", trade_file)
            writer.writerow(row)
    except OSError as exc:
        logger.error("Failed to write trade log: %s", exc)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with timestamp + level formatting."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
