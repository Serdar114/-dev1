"""
Settlement audit: fetch resolved market outcomes and compute paper PnL.

For resolved markets:
  - Determine winning outcome/bucket
  - Match against ghost trades
  - Compute fill-adjusted paper PnL
  - Write settlement_audit.jsonl

If outcome cannot be determined: mark unresolved_unknown.
Never fake outcomes.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .ghost_logger import (
    GHOST_TRADES_FILE,
    SETTLEMENT_AUDIT_FILE,
    log_settlement_audit,
    read_jsonl,
)

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0

# Taker fill cost approximation for PnL
TAKER_FEE = 0.02
MAKER_FEE = 0.00


def _get(url: str, params: dict, timeout: int = DEFAULT_TIMEOUT,
         retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF) -> Optional[Any]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("GET %s attempt %d/%d: %s", url, attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    return None


def fetch_market_resolution(market_id: str) -> Optional[dict]:
    """
    Fetch market resolution data from Gamma API.

    Returns dict with:
      - resolved: bool
      - winning_outcome: str or None
      - winning_token_id: str or None
      - resolution_time: str or None
      - raw: dict
    """
    data = _get(f"{GAMMA_BASE}/markets/{market_id}", params={})
    if not data:
        return None

    if isinstance(data, dict):
        return _parse_resolution(data)

    return None


def _parse_resolution(market: dict) -> dict:
    """Parse Gamma market JSON for resolution status."""
    resolved = bool(market.get("resolved") or market.get("resolutionTime") or market.get("resolvedBy"))
    winning_outcome = market.get("winnerOutcome") or market.get("outcome") or market.get("resolution")
    winning_token_id = None

    # Try to find winning token id from outcome prices
    outcome_prices = market.get("outcomePrices") or {}
    if isinstance(outcome_prices, dict):
        for token_id, price in outcome_prices.items():
            try:
                if float(price) == 1.0:
                    winning_token_id = str(token_id)
                    break
            except (TypeError, ValueError):
                pass

    # Also check tokens array
    tokens = market.get("tokens") or []
    for token in tokens:
        if isinstance(token, dict):
            winner = token.get("winner") or token.get("winning")
            if winner:
                winning_token_id = str(token.get("token_id") or token.get("tokenId") or "")
                winning_outcome = winning_outcome or token.get("outcome")
                break

    return {
        "resolved": resolved,
        "winning_outcome": winning_outcome,
        "winning_token_id": winning_token_id,
        "resolution_time": market.get("resolutionTime") or market.get("resolvedAt"),
        "raw": market,
    }


def _compute_pnl(
    ghost_price: float,
    ghost_size_usdc: float,
    entry_type: str,
    won: bool,
) -> dict:
    """
    Compute paper PnL for a ghost trade.

    Won = the outcome we bet on resolved YES (price → 1.0).
    Lost = price → 0.0.

    Gross PnL: if won, profit = size * (1 - ghost_price) / ghost_price per unit
    Actually simpler: we bought at ghost_price per share.
    shares = ghost_size_usdc / ghost_price
    if won: pnl = shares * (1.0 - ghost_price) - fee
    if lost: pnl = -ghost_size_usdc - fee

    Fill-adjusted: subtract slippage estimate.
    """
    fee = TAKER_FEE if entry_type == "taker" else MAKER_FEE

    if ghost_price <= 0 or ghost_price >= 1:
        return {"gross_pnl": 0.0, "net_pnl": 0.0, "fill_adjusted_pnl": 0.0}

    shares = ghost_size_usdc / ghost_price

    if won:
        gross_pnl = shares * 1.0 - ghost_size_usdc  # profit
        fee_cost = ghost_size_usdc * fee
        slippage = ghost_size_usdc * 0.005
        net_pnl = gross_pnl - fee_cost
        fill_adjusted_pnl = gross_pnl - fee_cost - slippage
    else:
        gross_pnl = -ghost_size_usdc
        fee_cost = ghost_size_usdc * fee
        net_pnl = gross_pnl - fee_cost
        fill_adjusted_pnl = net_pnl

    return {
        "gross_pnl": round(gross_pnl, 4),
        "net_pnl": round(net_pnl, 4),
        "fill_adjusted_pnl": round(fill_adjusted_pnl, 4),
    }


def audit_single_market(market_id: str, ghost_trades: list[dict]) -> list[dict]:
    """
    Audit a single market: fetch resolution, match ghost trades, compute PnL.

    Returns list of audit records (one per ghost trade, plus one summary).
    """
    audit_records = []
    resolution = fetch_market_resolution(market_id)

    if resolution is None:
        for gt in ghost_trades:
            record = {
                "market_id": market_id,
                "ghost_trade_ts": gt.get("timestamp_utc"),
                "status": "unresolved_unknown",
                "resolution_fetch_error": True,
                "ghost_price": gt.get("ghost_price"),
                "ghost_size_usdc": gt.get("ghost_size_usdc"),
                "pnl": None,
            }
            audit_records.append(record)
        return audit_records

    if not resolution["resolved"]:
        for gt in ghost_trades:
            record = {
                "market_id": market_id,
                "ghost_trade_ts": gt.get("timestamp_utc"),
                "status": "not_yet_resolved",
                "winning_outcome": None,
                "ghost_price": gt.get("ghost_price"),
                "ghost_size_usdc": gt.get("ghost_size_usdc"),
                "pnl": None,
            }
            audit_records.append(record)
        return audit_records

    winning_token_id = resolution.get("winning_token_id")
    winning_outcome = resolution.get("winning_outcome")

    for gt in ghost_trades:
        token_id = gt.get("token_id")
        ghost_price = gt.get("ghost_price") or 0.0
        ghost_size = gt.get("ghost_size_usdc") or 0.0
        entry_type = gt.get("ghost_entry_type") or "taker"

        # Determine if this ghost trade won
        won = False
        if winning_token_id and token_id:
            won = (str(winning_token_id) == str(token_id))
        elif winning_outcome and gt.get("bucket_label"):
            # Fallback: try to match outcome string to bucket
            won = (str(winning_outcome).lower() == str(gt.get("bucket_label", "")).lower())

        pnl = _compute_pnl(ghost_price, ghost_size, entry_type, won)

        record = {
            "market_id": market_id,
            "ghost_trade_ts": gt.get("timestamp_utc"),
            "city": gt.get("city"),
            "bucket_label": gt.get("bucket_label"),
            "token_id": token_id,
            "status": "resolved",
            "winning_outcome": winning_outcome,
            "winning_token_id": winning_token_id,
            "ghost_won": won,
            "ghost_price": ghost_price,
            "ghost_size_usdc": ghost_size,
            "ghost_entry_type": entry_type,
            "signal_type": gt.get("signal_type"),
            "edge_net": gt.get("edge_net"),
            "resolution_time": resolution.get("resolution_time"),
            **pnl,
        }
        audit_records.append(record)
        log_settlement_audit(record)

    return audit_records


def run_settlement_audit(market_ids: Optional[list[str]] = None) -> dict:
    """
    Run settlement audit for all open ghost trades (or specified market_ids).

    Returns summary stats.
    """
    ghost_trades = read_jsonl(GHOST_TRADES_FILE)
    open_trades = [t for t in ghost_trades if t.get("status") == "open"]

    if not open_trades:
        logger.info("No open ghost trades to audit")
        return {"audited": 0, "resolved": 0, "total_pnl": 0.0}

    # Group by market_id
    by_market: dict[str, list[dict]] = {}
    for t in open_trades:
        mid = t.get("market_id", "")
        if market_ids and mid not in market_ids:
            continue
        by_market.setdefault(mid, []).append(t)

    total_resolved = 0
    total_pnl = 0.0
    audited = 0

    for mid, trades in by_market.items():
        records = audit_single_market(mid, trades)
        audited += len(records)
        for r in records:
            if r.get("status") == "resolved":
                total_resolved += 1
                pnl = r.get("fill_adjusted_pnl") or 0.0
                total_pnl += pnl

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total_open_trades": len(open_trades),
        "markets_checked": len(by_market),
        "records_audited": audited,
        "trades_resolved": total_resolved,
        "total_fill_adjusted_pnl": round(total_pnl, 4),
    }
    logger.info("Settlement audit: %s", summary)
    log_settlement_audit({"type": "summary", **summary})
    return summary
