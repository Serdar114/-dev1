"""
execution/maker_lane.py — Maker paper execution lane.

The maker lane simulates passive limit-order placement on the YES side.
It is the PRIMARY evaluation lane for the maker thesis.

Sizing: FIXED 5 SHARES (v1 hard constraint).

Quote Buckets
-------------
Every intended quote is classified into one of three buckets based on
the intended execution price (YES probability, 0-1):

    B1: 0.83 – 0.86
    B2: 0.87 – 0.90
    B3: 0.91 – 0.92

Quotes outside these ranges are not eligible for the maker lane.

Fill simulation and realism grade
----------------------------------
Paper fill is SIMULATED (no live order placement).

fill_realism_grade records the quality of the fill simulation:
    "OBSERVED_PATH"            — real WebSocket book path (not yet live).
    "PROVISIONAL_MULTI_POINT"  — 2+ REST-polled prices; fill IS evaluable.
    "PROVISIONAL_SINGLE_POINT" — exactly 1 price collected; fill blocked.
                                 Single-point paths are not sufficient evidence
                                 of a fill — the first post-decision price can
                                 equal the limit by coincidence (same-tick risk).
    "PROVISIONAL_NO_PATH"      — no prices at all; fill blocked.

fill_evaluable (bool):
    True  only when grade is PROVISIONAL_MULTI_POINT or OBSERVED_PATH.
    False for single-point and no-path grades.

    Session summaries distinguish maker_raw_fill_count (any filled=True)
    from maker_evaluable_fill_count (filled=True AND fill_evaluable=True).
    Do NOT use raw fills to draw maker viability conclusions.

When intra_window_prices is None or has < 2 entries, filled is always
False to avoid optimistic inflation of fill rate metrics.

Execution log fields (per window):
    quote_bucket            : str  — "B1" | "B2" | "B3" | "INELIGIBLE" | "N/A"
    intended_price          : float
    filled                  : bool
    fill_price              : float | None
    fill_realism_grade      : str  — see grades above
    fill_evaluable          : bool — True only for multi-point / observed paths
    shares                  : int  (always 5 in v1)
    fee_per_share           : float (always 0 for maker)
    total_fee               : float (always 0 for maker)
    gross_pnl               : float | None
    net_pnl                 : float | None  (== gross_pnl since fee=0)
    bankroll_fraction       : float — fraction of bankroll consumed by entry
    break_even_wr_estimate  : float
    win_if_correct          : float — net gain if outcome matches prediction
    loss_if_wrong           : float — net loss if outcome is wrong
    outcome_correct         : bool | None  — set after settlement
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .fees import maker_fee, break_even_win_rate

logger = logging.getLogger(__name__)

# Fixed sizing — v1 hard constraint.
FIXED_SHARES_V1 = 5

# Quote bucket definitions: (label, low_inclusive, high_inclusive)
# These are YES probability prices (0-1 range), not BTC/USD prices.
QUOTE_BUCKETS = [
    ("B1", 0.83, 0.86),
    ("B2", 0.87, 0.90),
    ("B3", 0.91, 0.92),
]

# Fill realism grade constants
FILL_GRADE_OBSERVED = "OBSERVED_PATH"              # Real WebSocket price path (future)
FILL_GRADE_PROVISIONAL_MULTI = "PROVISIONAL_MULTI_POINT"    # 2+ REST-polled prices — evaluable
FILL_GRADE_PROVISIONAL_SINGLE = "PROVISIONAL_SINGLE_POINT"  # 1 price — NOT evaluable
FILL_GRADE_PROVISIONAL = "PROVISIONAL_NO_PATH"     # No prices at all — NOT evaluable
FILL_GRADE_NA = "N/A"

# Legacy alias kept so existing callers that pass fill_realism_source=FILL_GRADE_PROVISIONAL_PROXY
# do not break; the value is ignored in the new count-based grade logic.
FILL_GRADE_PROVISIONAL_PROXY = "PROVISIONAL_OBSERVED_PROXY"


def classify_quote_bucket(price: float) -> str:
    """Return the quote bucket label for `price` (YES probability 0-1), or "INELIGIBLE"."""
    for label, lo, hi in QUOTE_BUCKETS:
        if lo <= price <= hi:
            return label
    return "INELIGIBLE"


@dataclass
class MakerResult:
    """
    Full record of a single maker-lane paper evaluation for one window.
    """
    window_open_ts: int
    slug: str
    signal_direction: str           # "YES" | "NO" | "NONE"
    intended_price: Optional[float]
    quote_bucket: str               # "B1" | "B2" | "B3" | "INELIGIBLE" | "N/A"
    shares: int = FIXED_SHARES_V1
    filled: bool = False
    fill_price: Optional[float] = None
    fill_realism_grade: str = FILL_GRADE_NA   # see module docstring
    fill_evaluable: bool = False              # True only for multi-point / observed paths
    fee_per_share: float = 0.0
    total_fee: float = 0.0
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    bankroll_fraction: Optional[float] = None
    break_even_wr_estimate: Optional[float] = None
    win_if_correct: Optional[float] = None
    loss_if_wrong: Optional[float] = None
    outcome_correct: Optional[bool] = None
    rejection_reason: Optional[str] = None
    extra: dict = field(default_factory=dict)


class MakerLane:
    """
    Maker paper execution lane.

    Call sequence per window:
        1. evaluate(...)  → MakerResult (pre-fill)
        2. settle(result, actual_outcome)  → MakerResult (post-fill)
    """

    def __init__(self, config: dict) -> None:
        self._sizing_cfg = config.get("sizing", {})
        self._quote_buckets_cfg = config.get("quote_buckets", {})
        self._shares = FIXED_SHARES_V1

    def evaluate(
        self,
        window_open_ts: int,
        slug: str,
        signal_direction: str,
        intended_price: Optional[float],
        bankroll: float,
        intra_window_prices: Optional[list] = None,
        fill_realism_source: Optional[str] = None,
    ) -> MakerResult:
        """
        Simulate a maker quote for one window.

        Parameters
        ----------
        window_open_ts      : Unix timestamp of window open.
        slug                : Market slug.
        signal_direction    : "YES" | "NO" | "NONE"
        intended_price      : The YES probability price at which to post the limit
                              order. None if signal is "NONE". Must be in (0, 1).
        bankroll            : Current paper bankroll (USD).
        intra_window_prices : List of YES probability prices observed during the
                              window (e.g. from CLOB snapshots or book feed).
                              Pass None if unavailable — fill will be PROVISIONAL_NO_PATH
                              and filled will be False.
        fill_realism_source : Override the fill realism grade string.
                              If None, auto-selected based on intra_window_prices:
                                  None  → PROVISIONAL_NO_PATH
                                  list  → OBSERVED_PATH (or PROVISIONAL_OBSERVED_PROXY
                                          if fill_realism_source="PROVISIONAL_OBSERVED_PROXY")
                              Pass FILL_GRADE_PROVISIONAL_PROXY when prices come from
                              the REST-polled CLOBYesPriceAdapter (not WebSocket).

        Fill realism:
            If intra_window_prices is provided, fill is True iff any price in the
            list dips at or below intended_price (limit order crossing logic).
            If intra_window_prices is None, fill is always False and the grade is
            PROVISIONAL_NO_PATH.  This is the CONSERVATIVE choice — we do NOT
            assume fills when we have no evidence of a price crossing.

        Returns
        -------
        MakerResult with all fields populated except outcome_correct.
        """
        result = MakerResult(
            window_open_ts=window_open_ts,
            slug=slug,
            signal_direction=signal_direction,
            intended_price=intended_price,
            quote_bucket="N/A",
        )

        if signal_direction == "NONE" or intended_price is None:
            result.rejection_reason = "no_signal"
            return result

        # Classify quote bucket.
        bucket = classify_quote_bucket(intended_price)
        result.quote_bucket = bucket

        if bucket == "INELIGIBLE":
            result.rejection_reason = f"price_{intended_price:.4f}_outside_buckets"
            logger.debug(
                "[maker] Ineligible price=%.4f window=%d slug=%s",
                intended_price, window_open_ts, slug
            )
            return result

        # Compute cost metrics.
        cost = intended_price * self._shares
        result.bankroll_fraction = cost / bankroll if bankroll > 0 else 0.0
        result.fee_per_share = maker_fee(self._shares) / self._shares  # = 0.0
        result.total_fee = 0.0
        result.break_even_wr_estimate = break_even_win_rate(intended_price, 0.0)
        result.win_if_correct = (1.0 - intended_price) * self._shares
        result.loss_if_wrong = intended_price * self._shares

        # Fill simulation — grade and evaluability determined by path density.
        #
        # Conservative rules:
        #   path=None or len<1 → PROVISIONAL_NO_PATH    fill=False evaluable=False
        #   len==1             → PROVISIONAL_SINGLE_POINT fill=False evaluable=False
        #                        (single-point is not sufficient evidence; the first
        #                         post-decision price can equal limit by coincidence)
        #   len>=2             → PROVISIONAL_MULTI_POINT  fill=maybe evaluable=True
        #                        (OR OBSERVED_PATH if caller explicitly signals WebSocket)
        #
        if not intra_window_prices:
            # No path at all.
            result.filled = False
            result.fill_price = None
            result.fill_realism_grade = FILL_GRADE_PROVISIONAL
            result.fill_evaluable = False
        elif len(intra_window_prices) < 2:
            # Single-point path — not evaluable.
            # A single price immediately after decision time can coincide with the
            # limit price without implying real tradeable liquidity at that level.
            result.filled = False
            result.fill_price = None
            result.fill_realism_grade = FILL_GRADE_PROVISIONAL_SINGLE
            result.fill_evaluable = False
            logger.debug(
                "[maker] single-point path: fill blocked window=%d slug=%s price=%.4f",
                window_open_ts, slug, intended_price,
            )
        else:
            # Multi-point path — evaluable.
            # Grade: OBSERVED_PATH only if caller explicitly signals real WebSocket data.
            if fill_realism_source == FILL_GRADE_OBSERVED:
                result.fill_realism_grade = FILL_GRADE_OBSERVED
            else:
                result.fill_realism_grade = FILL_GRADE_PROVISIONAL_MULTI
            result.fill_evaluable = True

            min_price = min(intra_window_prices)
            if min_price <= intended_price:
                result.filled = True
                result.fill_price = intended_price   # Assume fill at limit price.
                result.shares = self._shares
            else:
                result.filled = False
                result.fill_price = None

        logger.info(
            "[maker] window=%d slug=%s bucket=%s price=%.4f filled=%s "
            "grade=%s bankroll_frac=%.4f be_wr=%.4f win=%.4f loss=%.4f",
            window_open_ts, slug, bucket, intended_price, result.filled,
            result.fill_realism_grade,
            result.bankroll_fraction or 0.0,
            result.break_even_wr_estimate or 0.0,
            result.win_if_correct or 0.0,
            result.loss_if_wrong or 0.0,
        )
        return result

    def settle(self, result: MakerResult, actual_outcome: str) -> MakerResult:
        """
        Apply settlement to a MakerResult.

        Parameters
        ----------
        result         : Previously returned MakerResult.
        actual_outcome : "YES" (price went up) | "NO" (price went down).
        """
        if not result.filled or result.fill_price is None:
            result.outcome_correct = None
            return result

        result.outcome_correct = (result.signal_direction == actual_outcome)

        if result.outcome_correct:
            result.gross_pnl = (1.0 - result.fill_price) * result.shares
        else:
            result.gross_pnl = -result.fill_price * result.shares

        result.net_pnl = result.gross_pnl  # maker fee = 0

        logger.info(
            "[maker] settle window=%d outcome=%s correct=%s gross_pnl=%.4f grade=%s",
            result.window_open_ts, actual_outcome,
            result.outcome_correct, result.gross_pnl or 0.0,
            result.fill_realism_grade,
        )
        return result
