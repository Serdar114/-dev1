"""
Maker shadow probe for polybot_v2.

Phase 2: real evaluation lane. NO real orders. NO real placement.

This module:
  1. Computes a theoretical post-only quote price
  2. Guards on passive maker edge (fair_for_side - quote_price >= min_passive_edge)
  3. Guards on degenerate book states and spread quality for maker placement
  4. Keeps pending quotes alive with a TTL
  5. On each subsequent snapshot, checks whether best_ask dropped to/below
     the quote (forward simulation fill)
  6. After a fill, measures next-tick mid to classify filled_adverse vs filled_favorable
  7. At window boundary, resolves all remaining quotes with boundary outcome and
     computes maker_pnl_if_held for any filled quote

fill_status lifecycle:
  pending           - alive, waiting for fill or expiry
  crossed_rejected  - quote crossed the book at placement (post-only rejected)
  expired_unfilled  - TTL elapsed without fill
  filled            - best_ask touched quote_price within TTL (interim, pending next-tick measure)
  filled_adverse    - fill confirmed + next-tick mid moved against us
  filled_favorable  - fill confirmed + next-tick mid moved in our favor
  boundary_resolved - window ended while quote was pending or in flight

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
        # Filled quotes awaiting next-tick adverse/favorable measurement: (quote, fill_mid)
        self._filled_awaiting: list[tuple[ShadowQuote, float]] = []
        # All settled quotes this window (adverse/favorable measured), kept until boundary
        # so boundary_outcome and maker_pnl_if_held can be added
        self._settled: list[ShadowQuote] = []

    def build_quote(
        self,
        decision: SignalDecision,
        market_snap: MarketSnapshot,
    ) -> Optional[ShadowQuote]:
        """
        Build a theoretical shadow quote from a SHADOW_QUOTE decision.
        Returns None if decision is not SHADOW_QUOTE or build is rejected.

        Phase 2: copies full decision context onto the quote record.
        Computes passive edge and rejects if below min_passive_edge.
        """
        if decision.action != "SHADOW_QUOTE" or decision.chosen_side is None:
            return None

        side = decision.chosen_side
        tick_size = self._select_tick_size(market_snap.implied_yes_prob)

        if side == "yes":
            best_bid = market_snap.best_bid_yes
            best_ask = market_snap.best_ask_yes
            fair_for_side = decision.fair_yes_prob
        else:
            best_bid = market_snap.best_bid_no
            best_ask = market_snap.best_ask_no
            # fair_no = 1 - fair_yes (implied by complementarity)
            fair_for_side = 1.0 - decision.fair_yes_prob

        # Guard: degenerate book for this side
        if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            log.debug(
                "MakerShadow REJECT degenerate_book: side=%s bid=%.4f ask=%.4f",
                side, best_bid, best_ask,
            )
            return self._build_rejected_quote(
                decision, market_snap, best_bid, best_ask, tick_size,
                reject_reason=f"degenerate_book(bid={best_bid:.4f} ask={best_ask:.4f})",
            )

        # Guard: spread too wide for maker placement
        spread = best_ask - best_bid
        if spread > self._cfg.max_spread_maker:
            log.debug(
                "MakerShadow REJECT spread_too_wide: side=%s spread=%.4f > max=%.4f",
                side, spread, self._cfg.max_spread_maker,
            )
            return self._build_rejected_quote(
                decision, market_snap, best_bid, best_ask, tick_size,
                reject_reason=f"spread_too_wide({spread:.4f}>{self._cfg.max_spread_maker:.4f})",
            )

        # Post as maker on bid side: quote at best bid (passive), aligned to tick
        quote_price = self._align_to_tick(best_bid, tick_size)

        # Compute passive maker edge: what we earn if filled at quote_price vs fair value
        if quote_price > 0:
            passive_edge = fair_for_side - quote_price
        else:
            passive_edge = None

        # Guard: minimum passive edge
        if passive_edge is None or passive_edge < self._cfg.min_passive_edge:
            edge_str = f"{passive_edge:.4f}" if passive_edge is not None else "None"
            log.debug(
                "MakerShadow REJECT min_passive_edge: side=%s edge=%s thr=%.4f",
                side, edge_str, self._cfg.min_passive_edge,
            )
            return self._build_rejected_quote(
                decision, market_snap, best_bid, best_ask, tick_size,
                reject_reason=(
                    f"min_passive_edge(edge={edge_str}"
                    f"<thr={self._cfg.min_passive_edge:.4f})"
                ),
                quote_price=quote_price,
                passive_edge=passive_edge,
            )

        # Cross check: a post-only bid that is >= best_ask would take liquidity
        is_crossed = quote_price >= best_ask
        fill_status = "crossed_rejected" if is_crossed else "pending"

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
            fill_status=fill_status,
            fill_ts=None,
            adverse_move_after_fill=None,
            # Phase 2: decision context
            market_slug=market_snap.slug,
            regime=decision.regime,
            pattern=decision.pattern,
            elapsed_from_window_start=decision.elapsed_from_window_start,
            implied_yes_prob=decision.implied_yes_prob,
            fair_yes_prob=decision.fair_yes_prob,
            delta_raw_fraction=decision.delta_raw_fraction,
            delta_pct_display=decision.delta_pct_display,
            confidence_score=decision.confidence_score,
            confidence_components=decision.confidence_components,
            intended_passive_edge=passive_edge,
            intended_notional=self._cfg.maker_intended_notional,
            reject_reason=None,
        )

        if fill_status == "pending":
            entry_mid = (best_bid + best_ask) / 2.0
            expires_ts = now + self._cfg.quote_ttl_sec
            self._pending.append((quote, entry_mid, expires_ts))
            log.debug(
                "MakerShadow PENDING: side=%s quote=%.4f bid=%.4f ask=%.4f "
                "passive_edge=%.4f ttl=%.0fs",
                side, quote_price, best_bid, best_ask,
                passive_edge, self._cfg.quote_ttl_sec,
            )
        else:
            log.debug(
                "MakerShadow CROSSED_REJECTED: side=%s quote=%.4f bid=%.4f ask=%.4f",
                side, quote_price, best_bid, best_ask,
            )

        return quote

    def _build_rejected_quote(
        self,
        decision: SignalDecision,
        market_snap: MarketSnapshot,
        best_bid: float,
        best_ask: float,
        tick_size: float,
        reject_reason: str,
        quote_price: float = 0.0,
        passive_edge: Optional[float] = None,
    ) -> ShadowQuote:
        """Build a quote record for a placement that was rejected before entering the book."""
        side = decision.chosen_side or "unknown"
        if side == "yes":
            fair_for_side = decision.fair_yes_prob
        else:
            fair_for_side = 1.0 - decision.fair_yes_prob
        return ShadowQuote(
            ts=decision.ts,
            window_ts=decision.window_ts,
            seconds_to_expiry=decision.seconds_to_expiry,
            side=side,
            quote_price=quote_price,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=tick_size,
            crossed=False,
            fill_status="crossed_rejected",
            fill_ts=None,
            adverse_move_after_fill=None,
            market_slug=market_snap.slug,
            regime=decision.regime,
            pattern=decision.pattern,
            elapsed_from_window_start=decision.elapsed_from_window_start,
            implied_yes_prob=decision.implied_yes_prob,
            fair_yes_prob=decision.fair_yes_prob,
            delta_raw_fraction=decision.delta_raw_fraction,
            delta_pct_display=decision.delta_pct_display,
            confidence_score=decision.confidence_score,
            confidence_components=decision.confidence_components,
            intended_passive_edge=passive_edge,
            intended_notional=self._cfg.maker_intended_notional,
            reject_reason=reject_reason,
        )

    def process_pending(
        self,
        current_market: MarketSnapshot,
    ) -> list[ShadowQuote]:
        """
        Advance the state machine for all pending and filled quotes.

        Call once per main loop tick after each market snapshot.

        Returns a list of ShadowQuotes that changed state this tick
        (filled, expired_unfilled, or fill quality measured).
        Caller should log these and pass to metrics.
        """
        now = time.time()
        completed: list[ShadowQuote] = []

        # Snapshot filled_awaiting at start of tick (before new fills are queued).
        # This ensures a quote that fills on tick N gets its adverse/favorable
        # measurement on tick N+1, not N (fill_mid vs next_mid would be identical on N).
        fills_to_measure = list(self._filled_awaiting)
        self._filled_awaiting = []

        # --- Process pending quotes (check for fill or expiry) ---
        still_pending: list[tuple[ShadowQuote, float, float]] = []
        for quote, entry_mid, expires_ts in self._pending:
            if quote.side == "yes":
                current_ask = current_market.best_ask_yes
                current_bid = current_market.best_bid_yes
            else:
                current_ask = current_market.best_ask_no
                current_bid = current_market.best_bid_no

            # Fill condition: best_ask dropped to/below our passive bid quote
            if current_ask <= quote.quote_price and quote.quote_price > 0:
                fill_ts = now
                current_mid = (
                    (current_bid + current_ask) / 2.0
                    if current_bid > 0 and current_ask > 0
                    else entry_mid
                )
                filled_quote = replace(
                    quote,
                    fill_status="filled",
                    fill_ts=fill_ts,
                    fill_mid=current_mid,
                )
                log.debug(
                    "MakerShadow FILLED: side=%s quote=%.4f current_ask=%.4f fill_mid=%.4f",
                    quote.side, quote.quote_price, current_ask, current_mid,
                )
                # Queue for next-tick quality measurement
                self._filled_awaiting.append((filled_quote, current_mid))
                completed.append(filled_quote)

            elif now >= expires_ts:
                expired_quote = replace(quote, fill_status="expired_unfilled")
                log.debug(
                    "MakerShadow EXPIRED_UNFILLED: side=%s quote=%.4f",
                    quote.side, quote.quote_price,
                )
                completed.append(expired_quote)
                # No need to track for boundary — no pnl to compute (no fill happened)

            else:
                still_pending.append((quote, entry_mid, expires_ts))

        self._pending = still_pending

        # --- Measure adverse/favorable for fills from PREVIOUS ticks ---
        for quote, fill_mid in fills_to_measure:
            if quote.side == "yes":
                new_bid = current_market.best_bid_yes
                new_ask = current_market.best_ask_yes
            else:
                new_bid = current_market.best_bid_no
                new_ask = current_market.best_ask_no

            if new_bid > 0 and new_ask > 0:
                new_mid = (new_bid + new_ask) / 2.0
            else:
                new_mid = None

            if fill_mid > 0 and new_mid is not None and new_mid > 0:
                # Move from our perspective: positive = price went up after we bought
                price_move = new_mid - fill_mid
                if price_move >= 0:
                    # Price rose after we bought YES (or price rose for our side)
                    status = "filled_favorable"
                    favorable = price_move
                    adverse = None
                else:
                    status = "filled_adverse"
                    adverse = price_move  # negative number = adverse
                    favorable = None
            else:
                # Can't measure — treat as adverse_fill with None values
                status = "filled_adverse"
                adverse = None
                favorable = None
                new_mid = None

            measured_quote = replace(
                quote,
                fill_status=status,
                next_mid_after_fill=new_mid,
                adverse_move_after_fill=adverse,
                favorable_move_after_fill=favorable,
            )
            log.debug(
                "MakerShadow %s: side=%s fill_mid=%.4f new_mid=%s",
                status.upper(),
                quote.side,
                fill_mid,
                f"{new_mid:.4f}" if new_mid is not None else "None",
            )
            completed.append(measured_quote)
            # Keep in settled list for boundary resolution
            self._settled.append(measured_quote)

        return completed

    def resolve_boundary(self, outcome_yes: float) -> list[ShadowQuote]:
        """
        Called at window boundary. Resolves all remaining and settled quotes with
        the boundary outcome (1.0 = BTC price up = YES wins, 0.0 = NO wins).

        Returns list of ShadowQuote records with boundary data populated.
        Call reset() after this to clear all state.

        For filled quotes (settled): adds boundary_outcome and maker_pnl_if_held.
        For pending quotes: resolves as boundary_resolved (never filled, TTL irrelevant).
        """
        resolved: list[ShadowQuote] = []

        def _compute_boundary(q: ShadowQuote, *, was_pending: bool) -> ShadowQuote:
            boundary_outcome_for_side = (
                outcome_yes if q.side == "yes" else (1.0 - outcome_yes)
            )
            # maker_pnl_if_held: per-share PnL from holding until resolution
            # If filled: we bought the token at quote_price; at resolution it pays 0 or 1
            # pnl_per_share = boundary_outcome_for_side - quote_price
            if not was_pending and q.quote_price > 0:
                pnl = boundary_outcome_for_side - q.quote_price
                realized_edge = (
                    (pnl - q.intended_passive_edge)
                    if q.intended_passive_edge is not None
                    else None
                )
            else:
                pnl = None
                realized_edge = None

            return replace(
                q,
                boundary_outcome_yes=outcome_yes,
                boundary_outcome_for_side=boundary_outcome_for_side,
                maker_pnl_if_held=pnl,
                maker_edge_realized_vs_expected=realized_edge,
                fill_status=(
                    q.fill_status if not was_pending else "boundary_resolved"
                ),
            )

        # Resolve settled fills (adverse/favorable measured) with boundary outcome
        for q in self._settled:
            resolved.append(_compute_boundary(q, was_pending=False))

        # Resolve remaining pending quotes (never filled)
        for q, _entry_mid, _expires_ts in self._pending:
            resolved.append(_compute_boundary(q, was_pending=True))

        # Resolve quotes in fill_awaiting (filled but not yet quality-measured)
        for q, _fill_mid in self._filled_awaiting:
            resolved.append(_compute_boundary(q, was_pending=False))

        log.debug(
            "MakerShadow boundary resolved %d quotes: outcome_yes=%.1f",
            len(resolved), outcome_yes,
        )
        return resolved

    def reset(self) -> None:
        """Reset all pending state at window boundary. Call AFTER resolve_boundary()."""
        self._pending.clear()
        self._filled_awaiting.clear()
        self._settled.clear()

    # Legacy single-result wrapper kept for any callers using old API
    def check_adverse_move(
        self,
        current_market: MarketSnapshot,
    ) -> Optional[ShadowQuote]:
        """
        Compatibility shim: calls process_pending and returns the first completed
        quote, if any. Callers should migrate to process_pending() for full results.
        """
        results = self.process_pending(current_market)
        return results[0] if results else None

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
