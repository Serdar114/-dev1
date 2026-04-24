"""
Collect CLOB orderbook data from Polymarket.

Never uses frontend displayed percentages.
Always fetches real orderbook bid/ask.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0

DISPLAY_MODE_REAL_MIDPOINT = "real_midpoint"
DISPLAY_MODE_WIDE_SPREAD = "wide_spread"
DISPLAY_MODE_NO_BOOK = "no_book"
DISPLAY_MODE_ONE_SIDED = "one_sided"
DISPLAY_MODE_UNKNOWN = "unknown"

WIDE_SPREAD_THRESHOLD = 0.10


@dataclass
class OrderbookResult:
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    spread: Optional[float]
    mid_price: Optional[float]
    top_book_depth: float  # total liquidity at top N levels
    display_price_mode: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # [(price, size), ...]
    asks: list[tuple[float, float]] = field(default_factory=list)
    error: Optional[str] = None


def _get(url: str, params: dict, timeout: int = DEFAULT_TIMEOUT,
         retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF) -> Optional[dict | list]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("CLOB GET %s attempt %d/%d: %s", url, attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    return None


def _parse_level(raw: dict | list) -> Optional[tuple[float, float]]:
    """Parse a single orderbook level into (price, size)."""
    try:
        if isinstance(raw, dict):
            price = float(raw.get("price") or raw.get("p") or 0)
            size = float(raw.get("size") or raw.get("s") or raw.get("quantity") or 0)
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price, size = float(raw[0]), float(raw[1])
        else:
            return None
        return price, size
    except (TypeError, ValueError):
        return None


def _parse_side(levels_raw: list, ascending: bool = True) -> list[tuple[float, float]]:
    """Parse list of raw levels. ascending=True for asks, False for bids."""
    parsed = []
    for raw in levels_raw:
        level = _parse_level(raw)
        if level:
            parsed.append(level)
    # Sort: bids descending (best bid = highest), asks ascending (best ask = lowest)
    if ascending:
        parsed.sort(key=lambda x: x[0])
    else:
        parsed.sort(key=lambda x: x[0], reverse=True)
    return parsed


def _determine_display_mode(
    best_bid: Optional[float],
    best_ask: Optional[float],
    spread: Optional[float],
) -> str:
    if best_bid is None and best_ask is None:
        return DISPLAY_MODE_NO_BOOK
    if best_bid is None or best_ask is None:
        return DISPLAY_MODE_ONE_SIDED
    if spread is not None and spread > WIDE_SPREAD_THRESHOLD:
        return DISPLAY_MODE_WIDE_SPREAD
    return DISPLAY_MODE_REAL_MIDPOINT


def _depth_at_top(levels: list[tuple[float, float]], top_n: int = 5) -> float:
    """Sum of size at top N levels."""
    return sum(size for _, size in levels[:top_n])


def fetch_orderbook(token_id: str, top_n: int = 10) -> OrderbookResult:
    """
    Fetch CLOB orderbook for a single token (outcome).

    Polymarket CLOB /book endpoint:
      GET /book?token_id=<token_id>
    """
    params = {"token_id": token_id}
    data = _get(f"{CLOB_BASE}/book", params=params)

    if data is None:
        return OrderbookResult(
            token_id=token_id,
            best_bid=None,
            best_ask=None,
            bid_size=None,
            ask_size=None,
            spread=None,
            mid_price=None,
            top_book_depth=0.0,
            display_price_mode=DISPLAY_MODE_NO_BOOK,
            error="fetch_failed",
        )

    if not isinstance(data, dict):
        return OrderbookResult(
            token_id=token_id,
            best_bid=None,
            best_ask=None,
            bid_size=None,
            ask_size=None,
            spread=None,
            mid_price=None,
            top_book_depth=0.0,
            display_price_mode=DISPLAY_MODE_NO_BOOK,
            error="unexpected_response_format",
        )

    raw_bids = data.get("bids") or data.get("buys") or []
    raw_asks = data.get("asks") or data.get("sells") or []

    bids = _parse_side(raw_bids, ascending=False)
    asks = _parse_side(raw_asks, ascending=True)

    best_bid = bids[0][0] if bids else None
    bid_size = bids[0][1] if bids else None
    best_ask = asks[0][0] if asks else None
    ask_size = asks[0][1] if asks else None

    spread = None
    mid_price = None
    if best_bid is not None and best_ask is not None:
        if best_ask > best_bid:
            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2.0
        else:
            # Crossed book — unusual, treat as wide
            spread = 0.0
            mid_price = best_bid

    bid_depth = _depth_at_top(bids, top_n)
    ask_depth = _depth_at_top(asks, top_n)
    top_book_depth = bid_depth + ask_depth

    display_mode = _determine_display_mode(best_bid, best_ask, spread)

    return OrderbookResult(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
        spread=spread,
        mid_price=mid_price,
        top_book_depth=top_book_depth,
        display_price_mode=display_mode,
        bids=bids[:top_n],
        asks=asks[:top_n],
    )


def fetch_orderbooks_for_market(token_ids: list[str], top_n: int = 10) -> dict[str, OrderbookResult]:
    """Fetch orderbooks for all token IDs of a market."""
    results = {}
    for token_id in token_ids:
        if not token_id:
            continue
        results[token_id] = fetch_orderbook(token_id, top_n=top_n)
    return results


def get_best_entry_price(book: OrderbookResult, side: str = "buy") -> Optional[float]:
    """
    Return the best available entry price for a given side.
    side="buy" → we pay the ask (taker)
    side="sell" → we receive the bid (maker providing liquidity)
    """
    if side == "buy":
        return book.best_ask
    return book.best_bid


def has_minimum_depth(book: OrderbookResult, stake_usdc: float, multiplier: float = 2.0) -> bool:
    """Check if top-book depth is at least multiplier * stake_usdc."""
    return book.top_book_depth >= stake_usdc * multiplier
