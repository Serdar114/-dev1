"""
Ghost logger: write observations, signals, and ghost trades to JSONL files.

Ghost order lifecycle:
  signal_detected   → ghost order placed but not yet filled
  ghost_order_placed → limit order sitting in book (PAPER_MAKER)
  ghost_filled       → confirmed filled (taker: immediate; maker: when ask≤price)
  expired_unfilled   → market resolved without fill
  cancelled_repriced → price moved beyond our level

PAPER_TAKER  → immediately ghost_filled (conservative fill assumption)
PAPER_MAKER  → starts ghost_order_placed, fills only when book touches price

All writes are append-only JSONL. Never crash on write failure.
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

# Ghost order lifecycle states
GHOST_STATUS_SIGNAL_DETECTED = "signal_detected"
GHOST_STATUS_ORDER_PLACED = "ghost_order_placed"
GHOST_STATUS_FILLED = "ghost_filled"
GHOST_STATUS_EXPIRED = "expired_unfilled"
GHOST_STATUS_CANCELLED = "cancelled_repriced"
GHOST_STATUS_OPEN = "open"  # legacy compat


def _ensure_log_dir():
    os.makedirs(_LOG_DIR, exist_ok=True)


def _write_jsonl(filepath: str, record: dict):
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
    ghost_entry_type: str,   # "taker" or "maker"
    ghost_price: float,
    ghost_size_usdc: float,
    signal_type: str,
    edge_net: float,
    fill_assumption: str,
    status: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """
    Log a ghost (paper) order.

    Lifecycle:
      PAPER_TAKER → status = ghost_filled (immediate taker fill assumption)
      PAPER_MAKER → status = ghost_order_placed (unfilled until book touches price)
    """
    now = _now_utc()

    # Determine initial status by entry type
    if status is None:
        if ghost_entry_type == "taker":
            status = GHOST_STATUS_FILLED
        else:
            status = GHOST_STATUS_ORDER_PLACED

    filled_at = now if status == GHOST_STATUS_FILLED else None

    record = {
        "timestamp_utc": now,
        "placed_at": now,
        "filled_at": filled_at,
        "expired_at": None,
        "fill_latency_seconds": 0.0 if status == GHOST_STATUS_FILLED else None,
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


def update_ghost_fill_status(
    market_id: str,
    token_id: Optional[str],
    new_status: str,
    best_ask: Optional[float] = None,
) -> dict:
    """
    Write a status-update record for a ghost order.
    A separate record (not in-place edit) to preserve append-only log integrity.
    """
    now = _now_utc()
    record = {
        "timestamp_utc": now,
        "type": "ghost_status_update",
        "market_id": market_id,
        "token_id": token_id,
        "new_status": new_status,
        "update_trigger_ask": best_ask,
        "updated_at": now,
    }
    if new_status == GHOST_STATUS_FILLED:
        record["filled_at"] = now
    elif new_status in (GHOST_STATUS_EXPIRED, GHOST_STATUS_CANCELLED):
        record["expired_at"] = now
    _write_jsonl(GHOST_TRADES_FILE, record)
    return record


def check_maker_fills(current_books: dict[str, float]) -> list[dict]:
    """
    Scan open maker ghost orders and emit fill updates when best_ask <= ghost_price.

    current_books: {token_id: current_best_ask}
    Returns list of fill-update records emitted.

    This is a conservative touch rule: if the ask has touched or crossed our
    limit bid price, we assume a fill.
    """
    fills = []
    open_orders = get_open_ghost_trades()
    for order in open_orders:
        if order.get("ghost_entry_type") != "maker":
            continue
        if order.get("status") != GHOST_STATUS_ORDER_PLACED:
            continue

        token_id = order.get("token_id")
        ghost_price = order.get("ghost_price")
        if token_id is None or ghost_price is None:
            continue

        current_ask = current_books.get(token_id)
        if current_ask is not None and current_ask <= ghost_price:
            record = update_ghost_fill_status(
                market_id=order.get("market_id", ""),
                token_id=token_id,
                new_status=GHOST_STATUS_FILLED,
                best_ask=current_ask,
            )
            fills.append(record)
            logger.info(
                "Ghost maker FILLED: market=%s token=%s price=%.3f ask=%.3f",
                order.get("market_id"), token_id, ghost_price, current_ask,
            )
    return fills


def log_settlement_audit(record: dict):
    if "timestamp_utc" not in record:
        record["timestamp_utc"] = _now_utc()
    _write_jsonl(SETTLEMENT_AUDIT_FILE, record)


def read_jsonl(filepath: str) -> list[dict]:
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
    """Return ghost orders that are still pending fill (order_placed or open)."""
    all_records = read_jsonl(GHOST_TRADES_FILE)
    open_statuses = {GHOST_STATUS_ORDER_PLACED, GHOST_STATUS_OPEN, GHOST_STATUS_SIGNAL_DETECTED}
    # Build latest status per (market_id, token_id) from update records
    latest_status: dict[tuple, str] = {}
    base_orders: list[dict] = []

    for r in all_records:
        if r.get("type") == "ghost_status_update":
            key = (r.get("market_id"), r.get("token_id"))
            latest_status[key] = r.get("new_status", "")
        elif "ghost_price" in r:
            base_orders.append(r)

    result = []
    for order in base_orders:
        key = (order.get("market_id"), order.get("token_id"))
        effective_status = latest_status.get(key, order.get("status", ""))
        if effective_status in open_statuses:
            result.append(order)
    return result


def count_open_ghost_trades() -> int:
    return len(get_open_ghost_trades())


def get_ghost_trade_counts() -> dict[str, int]:
    """Return counts by lifecycle status."""
    all_records = read_jsonl(GHOST_TRADES_FILE)
    counts: dict[str, int] = {}
    for r in all_records:
        if r.get("type") == "ghost_status_update":
            continue
        if "ghost_price" not in r:
            continue
        status = r.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts
