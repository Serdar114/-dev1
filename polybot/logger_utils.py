"""
logger_utils.py — Structured logging to JSONL and CSV files.

Manages two log files:
  logs/signals.jsonl — every signal evaluation (NO_TRADE and trades)
  logs/trades.csv    — every resolved paper trade with PnL

Creates the logs/ directory and CSV header automatically on first write.
"""

import csv
import json
import logging
import os
from typing import TYPE_CHECKING

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


def log_signal(signal: "SignalResult", signal_file: str) -> None:
    """
    Append a signal evaluation record to the JSONL signals file.
    All signals are logged, including NO_TRADE decisions.
    """
    _ensure_dir(signal_file)
    record = {
        "ts": signal.timestamp,
        "market": signal.market_slug,
        "action": signal.action,
        "delta_pct": round(signal.delta_pct, 6),
        "edge": round(signal.edge, 6),
        "reason": signal.reason,
        "implied": round(signal.implied_prob, 6),
        "model": round(signal.model_prob, 6),
        "trade_price": round(signal.trade_price, 6),
        "fee_pct": round(signal.fee_pct, 6),
        "btc_spot": round(signal.btc_spot, 2),
        "btc_open": round(signal.btc_open, 2),
        "tte_sec": round(signal.seconds_to_expiry, 1),
    }
    try:
        with open(signal_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.error("Failed to write signal log: %s", exc)


def log_signal_rejected(signal: "SignalResult", risk_reason: str, signal_file: str) -> None:
    """Log a signal that was blocked by risk manager."""
    _ensure_dir(signal_file)
    record = {
        "ts": signal.timestamp,
        "market": signal.market_slug,
        "action": f"REJECTED:{signal.action}",
        "delta_pct": round(signal.delta_pct, 6),
        "edge": round(signal.edge, 6),
        "reason": f"risk:{risk_reason}",
        "implied": round(signal.implied_prob, 6),
        "model": round(signal.model_prob, 6),
        "trade_price": round(signal.trade_price, 6),
        "fee_pct": round(signal.fee_pct, 6),
        "btc_spot": round(signal.btc_spot, 2),
        "btc_open": round(signal.btc_open, 2),
        "tte_sec": round(signal.seconds_to_expiry, 1),
    }
    try:
        with open(signal_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.error("Failed to write rejected signal log: %s", exc)


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
