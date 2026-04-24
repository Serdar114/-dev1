"""
EV calculator: compute gross and net edge for each market/bucket.

Inputs: model probability, nowcast, orderbook, buffers.
Outputs: action recommendation, price, size.

V1 signal thresholds (configurable):
  - maker candidate: net_edge_maker >= 0.08
  - taker candidate: net_edge_taker >= 0.12 AND stale flag
  - strong signal: net_edge >= 0.15
  - reject: spread > 0.10, depth < 2x stake, safety < 0.65
  - reject: entry within 4h of resolution (configurable)
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Action codes
ACTION_SKIP = "SKIP"
ACTION_WATCH = "WATCH"
ACTION_PAPER_MAKER = "PAPER_MAKER"
ACTION_PAPER_TAKER = "PAPER_TAKER"
ACTION_EXIT_WATCH = "EXIT_WATCH"

# Default thresholds
DEFAULT_MAKER_EDGE_THRESHOLD = 0.08
DEFAULT_MAKER_STRONG_THRESHOLD = 0.15
DEFAULT_TAKER_EDGE_THRESHOLD = 0.12
DEFAULT_TAKER_STRONG_THRESHOLD = 0.18
DEFAULT_MAX_SPREAD = 0.10
DEFAULT_MIN_DEPTH_MULTIPLIER = 2.0
DEFAULT_MIN_SAFETY = 0.65
DEFAULT_CLOSE_HOURS_REJECT = 4
DEFAULT_FEE_BUFFER = 0.02
DEFAULT_SLIPPAGE_BUFFER = 0.01
DEFAULT_CONFIDENCE_BUFFER = 0.02
DEFAULT_STAKE_USDC = 2.0
DEFAULT_MAX_STAKE_USDC = 5.0
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_BANKROLL = 30.0


@dataclass
class EVResult:
    action: str
    reason: str
    signal_type: Optional[str]
    # Probabilities
    model_probability: float
    nowcast_probability: Optional[float]
    blended_probability: float
    # Prices
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    recommended_price: Optional[float]
    recommended_size_usdc: float
    # Edge
    edge_gross: float
    edge_net_maker: float
    edge_net_taker: float
    # Flags
    stale_flag: bool
    near_resolution: bool
    safety_score: float
    # Details
    nowcast_confidence: str
    ensemble_agreement: float
    model_spread: float
    top_book_depth: float


def _blend_probabilities(
    model_prob: float,
    nowcast_prob: Optional[float],
    nowcast_confidence: str,
) -> float:
    """
    Blend model and nowcast probabilities.
    Nowcast weight depends on confidence:
      high   → 0.35 weight
      medium → 0.20 weight
      low    → 0.08 weight
      none   → 0.00 weight
    """
    weights = {"high": 0.35, "medium": 0.20, "low": 0.08, "none": 0.0}
    w = weights.get(nowcast_confidence, 0.0)

    if nowcast_prob is None or w == 0.0:
        return model_prob

    return model_prob * (1 - w) + nowcast_prob * w


def _kelly_size(
    probability: float,
    price: float,
    bankroll: float,
    kelly_fraction: float,
    max_stake: float,
) -> float:
    """Fractional Kelly sizing. Returns USDC stake."""
    if price <= 0 or price >= 1:
        return 0.0
    # Kelly for binary bet: f = (p*(1/price) - 1) / ((1/price) - 1)
    # Simplified: f = (p - price) / (1 - price)
    b = (1.0 - price) / price  # odds
    kelly_f = (probability * (b + 1) - 1) / b
    if kelly_f <= 0:
        return 0.0
    frac_kelly = kelly_f * kelly_fraction
    stake = frac_kelly * bankroll
    return min(stake, max_stake)


def _detect_stale_flag(
    spread: Optional[float],
    hours_to_close: float,
    display_price_mode: str,
    ensemble_agreement: float,
) -> bool:
    """
    Stale quote flag: market hasn't repriced despite model conviction.
    Heuristics:
    - Wide spread AND < 8 hours to resolution
    - One-sided book near resolution
    - High ensemble agreement (>= 0.75) but market price is stale
    """
    if spread is not None and spread >= 0.08 and hours_to_close <= 8:
        return True
    if display_price_mode in ("one_sided", "no_book") and hours_to_close <= 6:
        return True
    return False


def calculate_ev(
    model_probability: float,
    nowcast_probability: Optional[float],
    nowcast_confidence: str,
    best_bid: Optional[float],
    best_ask: Optional[float],
    bid_size: Optional[float],
    ask_size: Optional[float],
    spread: Optional[float],
    top_book_depth: float,
    display_price_mode: str,
    ensemble_agreement: float,
    model_spread: float,
    settlement_safety: float,
    hours_to_close: float,
    # Config
    stake_usdc: float = DEFAULT_STAKE_USDC,
    max_stake_usdc: float = DEFAULT_MAX_STAKE_USDC,
    bankroll: float = DEFAULT_BANKROLL,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    fee_buffer: float = DEFAULT_FEE_BUFFER,
    slippage_buffer: float = DEFAULT_SLIPPAGE_BUFFER,
    confidence_buffer: float = DEFAULT_CONFIDENCE_BUFFER,
    maker_edge_threshold: float = DEFAULT_MAKER_EDGE_THRESHOLD,
    taker_edge_threshold: float = DEFAULT_TAKER_EDGE_THRESHOLD,
    maker_strong_threshold: float = DEFAULT_MAKER_STRONG_THRESHOLD,
    taker_strong_threshold: float = DEFAULT_TAKER_STRONG_THRESHOLD,
    max_spread: float = DEFAULT_MAX_SPREAD,
    min_depth_multiplier: float = DEFAULT_MIN_DEPTH_MULTIPLIER,
    min_safety: float = DEFAULT_MIN_SAFETY,
    close_hours_reject: float = DEFAULT_CLOSE_HOURS_REJECT,
    taker_requires_stale: bool = True,
) -> EVResult:
    """
    Main EV calculation. Returns EVResult with action, edge, recommended price/size.
    """
    blended = _blend_probabilities(model_probability, nowcast_probability, nowcast_confidence)
    stale_flag = _detect_stale_flag(spread, hours_to_close, display_price_mode, ensemble_agreement)
    near_resolution = hours_to_close <= close_hours_reject

    # Compute gross and net edges
    total_buffer = fee_buffer + slippage_buffer + confidence_buffer

    if best_ask is not None and best_ask > 0:
        edge_gross = blended - best_ask
        edge_net_taker = edge_gross - total_buffer
    else:
        edge_gross = 0.0
        edge_net_taker = -total_buffer

    # Maker: we place a limit bid below ask
    if best_bid is not None and best_ask is not None and spread is not None:
        # Maker entry = mid minus a small improvement, no taker fee
        maker_entry = (best_bid + best_ask) / 2.0 - 0.01  # try to set near mid
        maker_entry = max(maker_entry, best_bid)
        edge_net_maker = blended - maker_entry - confidence_buffer - slippage_buffer / 2
    elif best_bid is not None:
        maker_entry = best_bid + 0.01  # improve on best bid
        edge_net_maker = blended - maker_entry - confidence_buffer
    else:
        maker_entry = None
        edge_net_maker = -total_buffer

    # --- Filter gates ---
    reject_reasons = []

    if near_resolution:
        reject_reasons.append(f"near_resolution_{hours_to_close:.1f}h")

    if spread is not None and spread > max_spread:
        reject_reasons.append(f"spread_too_wide_{spread:.3f}")

    if top_book_depth < stake_usdc * min_depth_multiplier:
        reject_reasons.append(f"insufficient_depth_{top_book_depth:.2f}_need_{stake_usdc*min_depth_multiplier:.2f}")

    if settlement_safety < min_safety:
        reject_reasons.append(f"safety_score_low_{settlement_safety:.2f}")

    if display_price_mode == "no_book":
        reject_reasons.append("no_book")

    # --- Determine action ---
    if reject_reasons:
        # May still be a watch candidate for exit or logging
        if near_resolution and edge_net_taker >= taker_edge_threshold * 0.8:
            action = ACTION_EXIT_WATCH
            reason = "near_resolution_exit_opportunity"
            signal_type = "near_resolution_reprice"
        else:
            action = ACTION_SKIP
            reason = "; ".join(reject_reasons)
            signal_type = None
    else:
        signal_type = None
        action = ACTION_SKIP
        reason = "edge_below_threshold"

        # Taker check (requires stale flag in V1)
        if not taker_requires_stale or stale_flag:
            if edge_net_taker >= taker_strong_threshold:
                action = ACTION_PAPER_TAKER
                signal_type = "taker_strong" if stale_flag else "taker_standard"
                reason = f"taker_edge_{edge_net_taker:.3f}_strong"
            elif edge_net_taker >= taker_edge_threshold:
                action = ACTION_PAPER_TAKER
                signal_type = "taker_stale" if stale_flag else "taker_standard"
                reason = f"taker_edge_{edge_net_taker:.3f}"

        # Maker check
        if action == ACTION_SKIP or action == ACTION_WATCH:
            if edge_net_maker >= maker_strong_threshold:
                action = ACTION_PAPER_MAKER
                signal_type = "maker_strong"
                reason = f"maker_edge_{edge_net_maker:.3f}_strong"
            elif edge_net_maker >= maker_edge_threshold:
                action = ACTION_PAPER_MAKER
                signal_type = "maker_standard"
                reason = f"maker_edge_{edge_net_maker:.3f}"

        # Watch: some edge but below thresholds, or moderate confidence
        if action == ACTION_SKIP:
            if edge_net_maker > 0.03 or edge_net_taker > 0.05:
                action = ACTION_WATCH
                signal_type = "watch_low_edge"
                reason = f"watch_maker={edge_net_maker:.3f}_taker={edge_net_taker:.3f}"

    # Size calculation
    if action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
        if best_ask is not None and best_ask > 0:
            size = _kelly_size(blended, best_ask, bankroll, kelly_fraction, max_stake_usdc)
            size = max(size, 0.5)  # minimum ghost size
        else:
            size = stake_usdc
    else:
        size = 0.0

    # Recommended price
    if action == ACTION_PAPER_TAKER:
        rec_price = best_ask
    elif action == ACTION_PAPER_MAKER:
        rec_price = maker_entry
    else:
        rec_price = None

    return EVResult(
        action=action,
        reason=reason,
        signal_type=signal_type,
        model_probability=model_probability,
        nowcast_probability=nowcast_probability,
        blended_probability=blended,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        recommended_price=rec_price,
        recommended_size_usdc=round(size, 2),
        edge_gross=round(edge_gross, 4),
        edge_net_maker=round(edge_net_maker, 4),
        edge_net_taker=round(edge_net_taker, 4),
        stale_flag=stale_flag,
        near_resolution=near_resolution,
        safety_score=settlement_safety,
        nowcast_confidence=nowcast_confidence,
        ensemble_agreement=ensemble_agreement,
        model_spread=model_spread,
        top_book_depth=top_book_depth,
    )
