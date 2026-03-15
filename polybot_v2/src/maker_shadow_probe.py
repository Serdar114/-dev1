"""
Maker shadow probe for polybot_v2.

NO real orders. NO real placement.

This module:
  1. Computes a theoretical post-only quote price
  2. Checks if that quote would cross the best bid/ask
  3. Checks if a theoretical fill would have happened
  4. Logs adverse move after fill (using next price snapshot)

Tick size:
  - default_tick_size from config
  - extreme_tick_size used when implied_prob near extreme zones
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

from models import MarketSnapshot, ShadowQuote, SignalDecision
from settings import Settings

log = logging.getLogger(__name__)


class MakerShadowProbe:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        # pending: track last quote for adverse move check
        self._pending: Optional[tuple[ShadowQuote, float]] = None  # (quote, entry_book_mid)

    def build_quote(
        self,
        decision: SignalDecision,
        market_snap: MarketSnapshot,
    ) -> Optional[ShadowQuote]:
        """
        Build a theoretical shadow quote from a SHADOW_QUOTE decision.
        Returns None if decision is not SHADOW_QUOTE.
        """
        if decision.action != "SHADOW_QUOTE" or decision.chosen_side is None:
            return None

        side = decision.chosen_side
        tick_size = self._select_tick_size(market_snap.implied_yes_prob)

        if side == "yes":
            best_bid = market_snap.best_bid_yes
            best_ask = market_snap.best_ask_yes
            # Post as maker on bid side: we want to buy below fair value
            raw_quote = best_bid  # quote at best bid (passive)
        else:
            best_bid = market_snap.best_bid_no
            best_ask = market_snap.best_ask_no
            raw_quote = best_bid

        # Align to tick
        quote_price = self._align_to_tick(raw_quote, tick_size)

        # Cross check: a post-only quote that crosses the opposite side is a taker
        # For a BID quote: crossed if quote_price >= best_ask
        crossed = quote_price >= best_ask

        # Fill check: a bid quote fills if the best ask drops to/below our quote
        # In a shadow (no real order), we check if current best_ask <= quote_price
        fill_would_happen = (best_ask <= quote_price) and (quote_price > 0)

        quote = ShadowQuote(
            ts=decision.ts,
            window_ts=decision.window_ts,
            seconds_to_expiry=decision.seconds_to_expiry,
            side=side,
            quote_price=quote_price,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=tick_size,
            crossed=crossed,
            fill_would_happen=fill_would_happen,
            adverse_move_after_fill=None,
        )

        # Store for adverse move tracking
        if fill_would_happen:
            book_mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
            self._pending = (quote, book_mid)

        log.debug(
            "ShadowQuote: side=%s quote=%.4f bid=%.4f ask=%.4f cross=%s fill=%s",
            side, quote_price, best_bid, best_ask, crossed, fill_would_happen,
        )

        return quote

    def check_adverse_move(
        self,
        current_market: MarketSnapshot,
    ) -> Optional[ShadowQuote]:
        """
        If we have a pending theoretical fill, compute adverse move
        based on new book mid vs entry book mid.
        Returns updated ShadowQuote if pending fill exists, else None.
        """
        if self._pending is None:
            return None

        quote, entry_mid = self._pending
        side = quote.side

        if side == "yes":
            new_bid = current_market.best_bid_yes
            new_ask = current_market.best_ask_yes
        else:
            new_bid = current_market.best_bid_no
            new_ask = current_market.best_ask_no

        new_mid = (new_bid + new_ask) / 2.0 if new_bid > 0 and new_ask > 0 else 0.0
        if entry_mid > 0 and new_mid > 0:
            # adverse move: price moved away from us (as a buyer, price dropped)
            adverse_move = new_mid - entry_mid
            # for a buyer, adverse = negative move (price fell after our fill)
        else:
            adverse_move = 0.0

        updated = ShadowQuote(
            ts=quote.ts,
            window_ts=quote.window_ts,
            seconds_to_expiry=quote.seconds_to_expiry,
            side=quote.side,
            quote_price=quote.quote_price,
            best_bid=quote.best_bid,
            best_ask=quote.best_ask,
            tick_size=quote.tick_size,
            crossed=quote.crossed,
            fill_would_happen=quote.fill_would_happen,
            adverse_move_after_fill=adverse_move,
        )

        self._pending = None
        return updated

    def reset(self) -> None:
        """Reset pending state at window boundary."""
        self._pending = None

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _select_tick_size(self, implied_prob: float) -> float:
        """Use finer tick in extreme zones."""
        if (
            implied_prob >= self._cfg.extreme_upper
            or implied_prob <= self._cfg.extreme_lower
        ):
            return self._cfg.extreme_tick_size
        return self._cfg.default_tick_size

    @staticmethod
    def _align_to_tick(price: float, tick_size: float) -> float:
        """Round price DOWN to nearest tick (post-only bid wants to be non-crossing)."""
        if tick_size <= 0:
            return price
        return math.floor(price / tick_size) * tick_size
