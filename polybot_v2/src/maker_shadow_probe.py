"""
Maker shadow probe for polybot_v2.

NO real orders. NO real placement.

This module:
  1. Computes a theoretical post-only quote price
  2. Checks if that quote would cross the best bid/ask (crossed)
  3. Keeps pending quotes alive with a TTL
  4. On each subsequent snapshot, checks whether best_ask dropped to/below
     the quote (forward simulation fill)
  5. After a fill, measures adverse move on the next snapshot
  6. Expired quotes are marked as expired if TTL elapsed without fill

fill_status states:
  pending     — alive, not yet filled or expired
  filled      — best_ask touched quote_price within TTL
  expired     — TTL elapsed without a fill
  crossed     — quote crossed the book at placement (rejected as post-only)
  adverse_fill — fill confirmed + adverse move measured

Tick size:
  - default_tick_size from config
  - extreme_tick_size used when implied_prob is in extreme zone
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import replace
from typing import Optional

from models import MarketSnapshot, ShadowQuote, SignalDecision
from settings import Settings

log = logging.getLogger(__name__)


class MakerShadowProbe:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        # Pending quotes awaiting fill check: (quote, entry_mid, expires_ts)
        self._pending: list[tuple[ShadowQuote, float, float]] = []
        # Filled quotes awaiting adverse-move measurement: (quote, entry_mid)
        self._filled: list[tuple[ShadowQuote, float]] = []

    def build_quote(
        self,
        decision: SignalDecision,
        market_snap: MarketSnapshot,
    ) -> Optional[ShadowQuote]:
        """
        Build a theoretical shadow quote from a SHADOW_QUOTE decision.
        Returns None if decision is not SHADOW_QUOTE.

        The quote starts as 'pending'. Fill simulation is done on subsequent
        snapshots via process_pending().
        """
        if decision.action != "SHADOW_QUOTE" or decision.chosen_side is None:
            return None

        side = decision.chosen_side
        tick_size = self._select_tick_size(market_snap.implied_yes_prob)

        if side == "yes":
            best_bid = market_snap.best_bid_yes
            best_ask = market_snap.best_ask_yes
        else:
            best_bid = market_snap.best_bid_no
            best_ask = market_snap.best_ask_no

        # Post as maker on bid side: quote at best bid (passive)
        quote_price = self._align_to_tick(best_bid, tick_size)

        # Cross check: a post-only bid that is >= best_ask would take liquidity
        is_crossed = quote_price >= best_ask

        if is_crossed:
            fill_status = "crossed"
        else:
            fill_status = "pending"

        now = time.time()
        quote = ShadowQuote(
            ts=decision.ts,
            window_ts=decision.window_ts,
            seconds_to_expiry=decision.seconds_to_expiry,
            side=side,
            quote_price=quote_price,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=tick_size,
            crossed=is_crossed,
            fill_would_happen=False,   # legacy field; fill_status is authoritative
            fill_status=fill_status,
            fill_ts=None,
            adverse_move_after_fill=None,
        )

        if fill_status == "pending":
            entry_mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
            expires_ts = now + self._cfg.quote_ttl_sec
            self._pending.append((quote, entry_mid, expires_ts))
            log.debug(
                "ShadowQuote PENDING: side=%s quote=%.4f bid=%.4f ask=%.4f ttl=%.0fs",
                side, quote_price, best_bid, best_ask, self._cfg.quote_ttl_sec,
            )
        else:
            log.debug(
                "ShadowQuote CROSSED: side=%s quote=%.4f bid=%.4f ask=%.4f",
                side, quote_price, best_bid, best_ask,
            )

        return quote

    def process_pending(
        self,
        current_market: MarketSnapshot,
    ) -> list[ShadowQuote]:
        """
        Advance the state machine for all pending and filled quotes.

        Call once per main loop tick after each market snapshot.

        Returns a list of ShadowQuotes that changed state this tick
        (filled, expired, or adverse_fill measured).  Caller should log these.
        """
        now = time.time()
        completed: list[ShadowQuote] = []

        # --- Process pending quotes (check for fill or expiry) ---
        still_pending: list[tuple[ShadowQuote, float, float]] = []
        for quote, entry_mid, expires_ts in self._pending:
            if quote.side == "yes":
                current_ask = current_market.best_ask_yes
                current_bid = current_market.best_bid_yes
            else:
                current_ask = current_market.best_ask_no
                current_bid = current_market.best_bid_no

            # Fill condition: best_ask dropped to/below our quote price
            if current_ask <= quote.quote_price and quote.quote_price > 0:
                fill_ts = now
                filled_quote = replace(
                    quote,
                    fill_would_happen=True,
                    fill_status="filled",
                    fill_ts=fill_ts,
                )
                log.debug(
                    "ShadowQuote FILLED: side=%s quote=%.4f current_ask=%.4f",
                    quote.side, quote.quote_price, current_ask,
                )
                # Queue for adverse move measurement on next tick
                current_mid = (
                    (current_bid + current_ask) / 2.0
                    if current_bid > 0 and current_ask > 0
                    else entry_mid
                )
                self._filled.append((filled_quote, current_mid))
                completed.append(filled_quote)
            elif now >= expires_ts:
                expired_quote = replace(quote, fill_status="expired")
                log.debug(
                    "ShadowQuote EXPIRED: side=%s quote=%.4f ttl_elapsed",
                    quote.side, quote.quote_price,
                )
                completed.append(expired_quote)
            else:
                still_pending.append((quote, entry_mid, expires_ts))

        self._pending = still_pending

        # --- Process filled quotes (measure adverse move on next snapshot) ---
        still_filled: list[tuple[ShadowQuote, float]] = []
        for quote, fill_mid in self._filled:
            if quote.side == "yes":
                new_bid = current_market.best_bid_yes
                new_ask = current_market.best_ask_yes
            else:
                new_bid = current_market.best_bid_no
                new_ask = current_market.best_ask_no

            new_mid = (new_bid + new_ask) / 2.0 if new_bid > 0 and new_ask > 0 else 0.0
            if fill_mid > 0 and new_mid > 0:
                # Adverse for a buyer: price fell after fill (new_mid < fill_mid)
                adverse_move = new_mid - fill_mid
            else:
                adverse_move = 0.0

            adverse_quote = replace(
                quote,
                fill_status="adverse_fill",
                adverse_move_after_fill=adverse_move,
            )
            log.debug(
                "ShadowQuote ADVERSE_FILL: side=%s fill_mid=%.4f new_mid=%.4f adverse=%.4f",
                quote.side, fill_mid, new_mid, adverse_move,
            )
            completed.append(adverse_quote)
            # Don't re-queue; adverse move measured once

        self._filled = still_filled  # clear filled queue

        return completed

    # Legacy single-result wrapper — kept for callers that only handle one result
    def check_adverse_move(
        self,
        current_market: MarketSnapshot,
    ) -> Optional[ShadowQuote]:
        """
        Compatibility shim: calls process_pending and returns the first completed
        quote, if any.  Callers should migrate to process_pending() for full results.
        """
        results = self.process_pending(current_market)
        return results[0] if results else None

    def reset(self) -> None:
        """Reset all pending state at window boundary."""
        self._pending.clear()
        self._filled.clear()

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
