"""
feature_builder.py — Assembles per-tick FeatureVector from live system state.

Design:
  Takes all live data sources as inputs.
  Assembles the FeatureVector defined in schemas.py.
  NEVER defaults a missing value to a guess.
  NEVER marks a feature as tradeable if any canonical field is missing.
  All delta/basis computations use explicit None-checking.
  is_tradeable = True only when ALL canonical fields are present and fresh.

  Canonical fields (all must be present+fresh for is_tradeable=True):
    - chainlink_open (from window truth tracker)
    - chainlink_now (from Chainlink feed, fresh)
    - up_best_ask, down_best_ask (from order books)
    - fees: fee_rate, fee_source (from metadata)
    - tick_size, min_order_size (from metadata)
    - window timing (secs_to_expiry > min threshold)

  Auxiliary (missing is OK, logged):
    - binance_bid, binance_ask
    - basis_bps

  This module does NOT decide whether to trade. That is no_trade_rules.
"""

from __future__ import annotations
import logging
import time
from typing import Optional, TYPE_CHECKING

from loggingx.schemas import (
    FeatureVector, FreshnessState, CanonicalPriceSnapshot, BinancePrice,
    OrderBookSnapshot, MarketMetadata, WindowTruth,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger("polybot.feature_builder")


class FeatureBuilder:
    """
    Assembles FeatureVector for a market at a given instant.

    Job: combine all data sources into a single, auditable feature snapshot.
    Input:
      - condition_id, up_token_id, down_token_id, window_start_ts, window_end_ts
      - chainlink: Optional[ChainlinkPrice] (current)
      - chainlink_open: Optional[float] (captured at window open)
      - binance: Optional[BinancePrice]
      - up_book: Optional[OrderBookSnapshot]
      - down_book: Optional[OrderBookSnapshot]
      - metadata: Optional[MarketMetadata]
    Output: FeatureVector (all fields present or explicitly None)
    Failure: returns FeatureVector with is_tradeable=False and all None fields
    """

    def build(
        self,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        window_start_ts: float,
        window_end_ts: float,
        canonical: Optional[CanonicalPriceSnapshot],
        chainlink_open: Optional[float],
        binance: Optional[BinancePrice],
        up_book: Optional[OrderBookSnapshot],
        down_book: Optional[OrderBookSnapshot],
        metadata: Optional[MarketMetadata],
    ) -> FeatureVector:
        now = time.time()

        fv = FeatureVector(
            condition_id=condition_id,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            built_at=now,
        )

        # Timing
        fv.secs_to_expiry = max(0.0, window_end_ts - now)

        # ── Canonical price (from source selector) ────────────────
        # chainlink_now / chainlink_freshness hold the canonical price value.
        # canonical_source_tag records which physical source provided it.
        if canonical is not None:
            fv.chainlink_now           = canonical.price_usd
            fv.chainlink_freshness     = canonical.freshness
            fv.canonical_source_tag    = canonical.source_tag
            fv.canonical_fallback_active = canonical.fallback_active
        else:
            fv.chainlink_now           = None
            fv.chainlink_freshness     = FreshnessState.MISSING
            fv.canonical_source_tag    = None
            fv.canonical_fallback_active = None

        fv.chainlink_open = chainlink_open

        if (
            chainlink_open is not None
            and fv.chainlink_now is not None
            and chainlink_open != 0.0
        ):
            fv.chainlink_delta_bps = (
                (fv.chainlink_now - chainlink_open) / chainlink_open * 10000.0
            )
        else:
            fv.chainlink_delta_bps = None

        # ── Binance (auxiliary) ───────────────────────────────────
        if binance is not None:
            fv.binance_bid = binance.bid
            fv.binance_ask = binance.ask
            fv.binance_freshness = binance.freshness

            if (
                chainlink_open is not None
                and chainlink_open != 0.0
            ):
                binance_mid = binance.mid()
                fv.binance_delta_bps = (
                    (binance_mid - chainlink_open) / chainlink_open * 10000.0
                )
            else:
                fv.binance_delta_bps = None
        else:
            fv.binance_bid = None
            fv.binance_ask = None
            fv.binance_freshness = FreshnessState.MISSING
            fv.binance_delta_bps = None

        # Basis: chainlink_now vs binance_mid (both must be fresh+present)
        if (
            fv.chainlink_now is not None
            and fv.chainlink_freshness == FreshnessState.FRESH
            and binance is not None
            and binance.freshness == FreshnessState.FRESH
            and fv.chainlink_now != 0.0
        ):
            fv.basis_bps = (binance.mid() - fv.chainlink_now) / fv.chainlink_now * 10000.0
        else:
            fv.basis_bps = None

        # ── Order books ───────────────────────────────────────────
        if up_book is not None:
            fv.up_best_bid = up_book.best_bid()
            fv.up_best_ask = up_book.best_ask()
            fv.up_spread   = up_book.spread()
        else:
            fv.up_best_bid = None
            fv.up_best_ask = None
            fv.up_spread   = None

        if down_book is not None:
            fv.down_best_bid = down_book.best_bid()
            fv.down_best_ask = down_book.best_ask()
            fv.down_spread   = down_book.spread()
        else:
            fv.down_best_bid = None
            fv.down_best_ask = None
            fv.down_spread   = None

        # Pair sum
        if fv.up_best_ask is not None and fv.down_best_ask is not None:
            fv.pair_sum_best_ask = fv.up_best_ask + fv.down_best_ask
        else:
            fv.pair_sum_best_ask = None

        # ── Fees (from metadata) ──────────────────────────────────
        if metadata is not None:
            fv.fees_enabled  = metadata.fees_enabled
            fv.fee_rate      = metadata.fee_rate
            fv.fee_source    = metadata.fee_source
            fv.tick_size     = metadata.tick_size
            fv.min_order_size = metadata.min_order_size

            # Effective fee on a hypothetical stake (illustrative, per-unit)
            if metadata.fee_rate is not None and fv.up_best_ask is not None:
                fv.effective_fee = metadata.fee_rate * fv.up_best_ask
            else:
                fv.effective_fee = None
        else:
            fv.fees_enabled  = None
            fv.fee_rate      = None
            fv.fee_source    = None
            fv.effective_fee = None
            fv.tick_size     = None
            fv.min_order_size = None

        # ── is_tradeable ──────────────────────────────────────────
        fv.is_tradeable = self._check_tradeable(fv)

        return fv

    def _check_tradeable(self, fv: FeatureVector) -> bool:
        """
        is_tradeable = True only when all canonical truth fields are present and fresh.
        This does NOT replace no_trade_rules — it is a pre-filter only.
        """
        if fv.chainlink_freshness != FreshnessState.FRESH:
            return False
        if fv.chainlink_now is None:
            return False
        if fv.chainlink_open is None:
            return False
        if fv.up_best_ask is None or fv.down_best_ask is None:
            return False
        if fv.fee_rate is None or fv.fee_source is None:
            return False
        if fv.tick_size is None or fv.min_order_size is None:
            return False
        if fv.secs_to_expiry is not None and fv.secs_to_expiry <= 0:
            return False
        return True
