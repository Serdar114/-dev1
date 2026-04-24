"""
Ghost logger: write observations, signals, and ghost trades to JSONL files.

All writes are append-only JSONL.
One JSON object per line.
Never crash on write failure.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")

OBSERVATIONS_FILE = os.path.join(_LOG_DIR, "observations.jsonl")
SIGNALS_FILE = os.path.join(_LOG_DIR, "signals.jsonl")
GHOST_TRADES_FILE = os.path.join(_LOG_DIR, "ghost_trades.jsonl")
SETTLEMENT_AUDIT_FILE = os.path.join(_LOG_DIR, "settlement_audit.jsonl")


def _ensure_log_dir():
    os.makedirs(_LOG_DIR, exist_ok=True)


def _write_jsonl(filepath: str, record: dict):
    """Append a single JSON record to a JSONL file."""
    try:
        _ensure_log_dir()
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.error("Failed to write to %s: %s", filepath, exc)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_observation(
    market_id: str,
    event_id: Optional[str],
    slug: Optional[str],
    question: str,
    city: Optional[str],
    station_code: Optional[str],
    unit: Optional[str],
    market_type: str,
    bucket_label: Optional[str],
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    close_time_utc: Optional[str],
    hours_to_close: Optional[float],
    resolution_source: Optional[str],
    settlement_safety_score: float,
    blacklist_flag: bool,
    best_bid: Optional[float],
    best_ask: Optional[float],
    bid_size: Optional[float],
    ask_size: Optional[float],
    spread: Optional[float],
    top_book_depth: float,
    display_price_mode: str,
    model_probability: float,
    ensemble_agreement: float,
    model_spread: float,
    nowcast_probability: Optional[float],
    current_temp: Optional[float],
    edge_gross: float,
    edge_net_maker: float,
    edge_net_taker: float,
    recommended_action: str,
    recommended_price: Optional[float],
    recommended_size_usdc: float,
    reject_reason: Optional[str],
    raw_refs: Optional[dict] = None,
    extra: Optional[dict] = None,
):
    record = {
        "timestamp_utc": _now_utc(),
        "market_id": market_id,
        "event_id": event_id,
        "slug": slug,
        "question": question,
        "city": city,
        "station_code": station_code,
        "unit": unit,
        "market_type": market_type,
        "bucket_label": bucket_label,
        "bucket_low": bucket_low,
        "bucket_high": bucket_high,
        "close_time_utc": close_time_utc,
        "hours_to_close": hours_to_close,
        "resolution_source": resolution_source,
        "settlement_safety_score": settlement_safety_score,
        "blacklist_flag": blacklist_flag,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread": spread,
        "top_book_depth": top_book_depth,
        "display_price_mode": display_price_mode,
        "model_probability": model_probability,
        "ensemble_agreement": ensemble_agreement,
        "model_spread": model_spread,
        "nowcast_probability": nowcast_probability,
        "current_temp": current_temp,
        "edge_gross": edge_gross,
        "edge_net_maker": edge_net_maker,
        "edge_net_taker": edge_net_taker,
        "recommended_action": recommended_action,
        "recommended_price": recommended_price,
        "recommended_size_usdc": recommended_size_usdc,
        "reject_reason": reject_reason,
        "raw_refs": raw_refs or {},
    }
    if extra:
        record.update(extra)
    _write_jsonl(OBSERVATIONS_FILE, record)


def log_signal(
    market_id: str,
    token_id: Optional[str],
    city: Optional[str],
    bucket_label: Optional[str],
    signal_type: str,
    action: str,
    model_probability: float,
    blended_probability: float,
    nowcast_probability: Optional[float],
    nowcast_confidence: str,
    best_ask: Optional[float],
    best_bid: Optional[float],
    spread: Optional[float],
    edge_net_maker: float,
    edge_net_taker: float,
    recommended_price: Optional[float],
    recommended_size_usdc: float,
    settlement_safety_score: float,
    stale_flag: bool,
    hours_to_close: Optional[float],
    reason: str,
    extra: Optional[dict] = None,
):
    record = {
        "timestamp_utc": _now_utc(),
        "market_id": market_id,
        "token_id": token_id,
        "city": city,
        "bucket_label": bucket_label,
        "signal_type": signal_type,
        "action": action,
        "model_probability": model_probability,
        "blended_probability": blended_probability,
        "nowcast_probability": nowcast_probability,
        "nowcast_confidence": nowcast_confidence,
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread": spread,
        "edge_net_maker": edge_net_maker,
        "edge_net_taker": edge_net_taker,
        "recommended_price": recommended_price,
        "recommended_size_usdc": recommended_size_usdc,
        "settlement_safety_score": settlement_safety_score,
        "stale_flag": stale_flag,
        "hours_to_close": hours_to_close,
        "reason": reason,
    }
    if extra:
        record.update(extra)
    _write_jsonl(SIGNALS_FILE, record)


def log_ghost_trade(
    market_id: str,
    token_id: Optional[str],
    city: Optional[str],
    bucket_label: Optional[str],
    side: str,
    ghost_entry_type: str,
    ghost_price: float,
    ghost_size_usdc: float,
    signal_type: str,
    edge_net: float,
    fill_assumption: str,
    status: str = "open",
    extra: Optional[dict] = None,
) -> dict:
    """
    Log a ghost (paper) trade and return the record for tracking.
    """
    record = {
        "timestamp_utc": _now_utc(),
        "market_id": market_id,
        "token_id": token_id,
        "city": city,
        "bucket_label": bucket_label,
        "side": side,
        "ghost_entry_type": ghost_entry_type,
        "ghost_price": ghost_price,
        "ghost_size_usdc": ghost_size_usdc,
        "signal_type": signal_type,
        "edge_net": edge_net,
        "fill_assumption": fill_assumption,
        "status": status,
    }
    if extra:
        record.update(extra)
    _write_jsonl(GHOST_TRADES_FILE, record)
    return record


def log_settlement_audit(record: dict):
    """Write a settlement audit record."""
    if "timestamp_utc" not in record:
        record["timestamp_utc"] = _now_utc()
    _write_jsonl(SETTLEMENT_AUDIT_FILE, record)


def read_jsonl(filepath: str) -> list[dict]:
    """Read all records from a JSONL file. Returns empty list on error."""
    records = []
    if not os.path.exists(filepath):
        return records
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping malformed JSONL line: %s", exc)
    except Exception as exc:
        logger.error("Failed to read %s: %s", filepath, exc)
    return records


def get_open_ghost_trades() -> list[dict]:
    """Return ghost trades with status='open'."""
    all_trades = read_jsonl(GHOST_TRADES_FILE)
    return [t for t in all_trades if t.get("status") == "open"]


def count_open_ghost_trades() -> int:
    return len(get_open_ghost_trades())
