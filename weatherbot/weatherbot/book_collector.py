"""
Collect CLOB orderbook data from Polymarket.

Never uses frontend displayed percentages.
Tracks bid/ask depth separately so EV gating uses the correct side.
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

BOOK_STATE_NORMAL = "normal"
BOOK_STATE_WIDE_SPREAD = "wide_spread"
BOOK_STATE_NO_BOOK = "no_book"
BOOK_STATE_ONE_SIDED = "one_sided"

WIDE_SPREAD_THRESHOLD = 0.10


@dataclass
class OrderbookResult:
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size_best: Optional[float]      # size at best bid level
    ask_size_best: Optional[float]      # size at best ask level
    spread: Optional[float]
    mid_price: Optional[float]
    bid_depth_top_n: float              # total USDC liquidity on bid side (top N levels)
    ask_depth_top_n: float              # total USDC liquidity on ask side (top N levels)
    book_state: str                     # normal / wide_spread / one_sided / no_book
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def top_book_depth(self) -> float:
        """Combined depth (legacy compat). Prefer bid_depth_top_n / ask_depth_top_n."""
        return self.bid_depth_top_n + self.ask_depth_top_n

    @property
    def display_price_mode(self) -> str:
        """Legacy compat alias for book_state."""
        return self.book_state

    # Entry / exit side helpers
    def entry_side_depth(self, entry_type: str = "taker") -> float:
        """
        Depth available for our entry.
        taker buy → we hit the ask → ask_depth matters.
        maker bid → we post on bid side → bid_depth approximates queue.
        """
        if entry_type == "taker":
            return self.ask_depth_top_n
        return self.bid_depth_top_n

    def exit_side_depth(self, entry_type: str = "taker") -> float:
        """
        Depth available to exit (sell the position at market).
        We'd hit the bid side.
        """
        return self.bid_depth_top_n


def _get(
    url: str,
    params: dict,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> Optional[dict | list]:
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
    parsed = []
    for raw in levels_raw:
        level = _parse_level(raw)
        if level:
            parsed.append(level)
    if ascending:
        parsed.sort(key=lambda x: x[0])
    else:
        parsed.sort(key=lambda x: x[0], reverse=True)
    return parsed


def _depth_usdc(levels: list[tuple[float, float]], top_n: int = 5) -> float:
    """
    Sum of USDC value at top N levels.
    On bid side: value ≈ price * size (per-share).
    On ask side: buying cost ≈ price * size.
    We approximate as just size (in shares) × price; for small books use size directly.
    """
    total = 0.0
    for price, size in levels[:top_n]:
        total += price * size  # USDC value
    return total


def _determine_book_state(
    best_bid: Optional[float],
    best_ask: Optional[float],
    spread: Optional[float],
) -> str:
    if best_bid is None and best_ask is None:
        return BOOK_STATE_NO_BOOK
    if best_bid is None or best_ask is None:
        return BOOK_STATE_ONE_SIDED
    if spread is not None and spread > WIDE_SPREAD_THRESHOLD:
        return BOOK_STATE_WIDE_SPREAD
    return BOOK_STATE_NORMAL


def fetch_orderbook(token_id: str, top_n: int = 10) -> OrderbookResult:
    """
    Fetch CLOB orderbook for a single token.
    Tracks bid_depth and ask_depth separately.
    """
    params = {"token_id": token_id}
    data = _get(f"{CLOB_BASE}/book", params=params)

    _empty = OrderbookResult(
        token_id=token_id,
        best_bid=None, best_ask=None,
        bid_size_best=None, ask_size_best=None,
        spread=None, mid_price=None,
        bid_depth_top_n=0.0, ask_depth_top_n=0.0,
        book_state=BOOK_STATE_NO_BOOK,
    )

    if data is None:
        _empty.error = "fetch_failed"
        return _empty

    if not isinstance(data, dict):
        _empty.error = "unexpected_response_format"
        return _empty

    raw_bids = data.get("bids") or data.get("buys") or []
    raw_asks = data.get("asks") or data.get("sells") or []

    bids = _parse_side(raw_bids, ascending=False)
    asks = _parse_side(raw_asks, ascending=True)

    best_bid = bids[0][0] if bids else None
    bid_size_best = bids[0][1] if bids else None
    best_ask = asks[0][0] if asks else None
    ask_size_best = asks[0][1] if asks else None

    spread = None
    mid_price = None
    if best_bid is not None and best_ask is not None:
        if best_ask > best_bid:
            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2.0
        else:
            spread = 0.0
            mid_price = best_bid

    bid_depth = _depth_usdc(bids, top_n)
    ask_depth = _depth_usdc(asks, top_n)
    book_state = _determine_book_state(best_bid, best_ask, spread)

    return OrderbookResult(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size_best=bid_size_best,
        ask_size_best=ask_size_best,
        spread=spread,
        mid_price=mid_price,
        bid_depth_top_n=bid_depth,
        ask_depth_top_n=ask_depth,
        book_state=book_state,
        bids=bids[:top_n],
        asks=asks[:top_n],
    )


def fetch_orderbooks_for_market(
    token_ids: list[str], top_n: int = 10
) -> dict[str, OrderbookResult]:
    results = {}
    for token_id in token_ids:
        if not token_id:
            continue
        results[token_id] = fetch_orderbook(token_id, top_n=top_n)
    return results


def get_best_entry_price(book: OrderbookResult, side: str = "buy") -> Optional[float]:
    if side == "buy":
        return book.best_ask
    return book.best_bid


def has_minimum_depth_for_entry(
    book: OrderbookResult,
    stake_usdc: float,
    entry_type: str = "taker",
    multiplier: float = 2.0,
) -> bool:
    """
    Check depth on the relevant entry side.
    taker buy → needs ask_depth_top_n >= stake * multiplier
    maker bid → needs bid_depth_top_n (queue) >= stake * multiplier
    """
    depth = book.entry_side_depth(entry_type)
    return depth >= stake_usdc * multiplier
