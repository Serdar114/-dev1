"""
EV calculator: compute gross and net edge for each market/bucket.

Inputs: model probability, nowcast, orderbook, buffers, ensemble validation.
Outputs: action recommendation, price, size.

Key safety rules (V1):
  - Uses entry-side depth (ask for taker, bid for maker) not combined depth
  - n_members == 0 or deterministic_fallback → downgrade to WATCH/SKIP
  - forecast_blocked_reason set → downgrade to WATCH/SKIP
  - live_eligible False → paper candidates only (ghost trades fine in V1)
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

ACTION_SKIP = "SKIP"
ACTION_WATCH = "WATCH"
ACTION_PAPER_MAKER = "PAPER_MAKER"
ACTION_PAPER_TAKER = "PAPER_TAKER"
ACTION_EXIT_WATCH = "EXIT_WATCH"

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
    # Quality
    nowcast_confidence: str
    ensemble_agreement: float
    model_spread: float
    # Depth (side-specific)
    ask_depth_top_n: float
    bid_depth_top_n: float
    entry_side_depth: float
    exit_side_depth: float
    # Ensemble validation
    n_members: int
    deterministic_fallback_used: bool
    forecast_blocked_reason: Optional[str]


def _blend_probabilities(
    model_prob: float,
    nowcast_prob: Optional[float],
    nowcast_confidence: str,
) -> float:
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
    if price <= 0 or price >= 1:
        return 0.0
    b = (1.0 - price) / price
    kelly_f = (probability * (b + 1) - 1) / b
    if kelly_f <= 0:
        return 0.0
    stake = kelly_f * kelly_fraction * bankroll
    return min(stake, max_stake)


def _detect_stale_flag(
    spread: Optional[float],
    hours_to_close: float,
    book_state: str,
    ensemble_agreement: float,
) -> bool:
    if spread is not None and spread >= 0.08 and hours_to_close <= 8:
        return True
    if book_state in ("one_sided", "no_book") and hours_to_close <= 6:
        return True
    return False


def calculate_ev(
    model_probability: float,
    nowcast_probability: Optional[float],
    nowcast_confidence: str,
    best_bid: Optional[float],
    best_ask: Optional[float],
    bid_size: Optional[float],     # kept for compat; prefer depth fields
    ask_size: Optional[float],
    spread: Optional[float],
    # Side-specific depths (required for correct gating)
    ask_depth_top_n: float = 0.0,
    bid_depth_top_n: float = 0.0,
    book_state: str = "unknown",
    ensemble_agreement: float = 0.0,
    model_spread: float = 0.0,
    settlement_safety: float = 0.5,
    hours_to_close: float = 999.0,
    # Ensemble validation
    n_members: int = 0,
    deterministic_fallback_used: bool = False,
    forecast_blocked_reason: Optional[str] = None,
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
    # Legacy compat
    top_book_depth: float = 0.0,
    display_price_mode: str = "unknown",
) -> EVResult:
    """
    Main EV calculation. Returns EVResult with action, edge, recommended price/size.

    Uses entry-side depth for gating:
      taker buy → needs ask_depth_top_n
      maker bid → needs bid_depth_top_n
    """
    # Handle legacy callers that pass top_book_depth but not side-specific
    if ask_depth_top_n == 0.0 and bid_depth_top_n == 0.0 and top_book_depth > 0.0:
        ask_depth_top_n = top_book_depth / 2.0
        bid_depth_top_n = top_book_depth / 2.0
    # book_state fallback for legacy display_price_mode
    if book_state == "unknown" and display_price_mode != "unknown":
        book_state = display_price_mode

    blended = _blend_probabilities(model_probability, nowcast_probability, nowcast_confidence)
    stale_flag = _detect_stale_flag(spread, hours_to_close, book_state, ensemble_agreement)
    near_resolution = hours_to_close <= close_hours_reject

    total_buffer = fee_buffer + slippage_buffer + confidence_buffer

    if best_ask is not None and best_ask > 0:
        edge_gross = blended - best_ask
        edge_net_taker = edge_gross - total_buffer
    else:
        edge_gross = 0.0
        edge_net_taker = -total_buffer

    if best_bid is not None and best_ask is not None and spread is not None:
        maker_entry = (best_bid + best_ask) / 2.0 - 0.01
        maker_entry = max(maker_entry, best_bid)
        edge_net_maker = blended - maker_entry - confidence_buffer - slippage_buffer / 2
    elif best_bid is not None:
        maker_entry = best_bid + 0.01
        edge_net_maker = blended - maker_entry - confidence_buffer
    else:
        maker_entry = None
        edge_net_maker = -total_buffer

    # ── Ensemble quality gate ─────────────────────────────────────────────────
    ensemble_blocked = False
    ensemble_block_reason = None

    if forecast_blocked_reason:
        ensemble_blocked = True
        ensemble_block_reason = forecast_blocked_reason
    elif n_members == 0:
        ensemble_blocked = True
        ensemble_block_reason = "n_members_zero"
    elif deterministic_fallback_used:
        ensemble_blocked = True
        ensemble_block_reason = "deterministic_fallback_only"

    # ── Reject filters ────────────────────────────────────────────────────────
    reject_reasons: list[str] = []

    if near_resolution:
        reject_reasons.append(f"near_resolution_{hours_to_close:.1f}h")

    if spread is not None and spread > max_spread:
        reject_reasons.append(f"spread_too_wide_{spread:.3f}")

    if book_state in ("no_book",):
        reject_reasons.append("no_book")

    if settlement_safety < min_safety:
        reject_reasons.append(f"safety_score_low_{settlement_safety:.2f}")

    # Side-specific depth check
    taker_depth_ok = ask_depth_top_n >= stake_usdc * min_depth_multiplier
    maker_depth_ok = bid_depth_top_n >= stake_usdc * min_depth_multiplier

    if not taker_depth_ok and not maker_depth_ok:
        reject_reasons.append(
            f"insufficient_depth_ask={ask_depth_top_n:.2f}_bid={bid_depth_top_n:.2f}"
            f"_need={stake_usdc*min_depth_multiplier:.2f}"
        )

    # ── Action determination ──────────────────────────────────────────────────
    if reject_reasons:
        if near_resolution and edge_net_taker >= taker_edge_threshold * 0.8:
            action = ACTION_EXIT_WATCH
            reason = "near_resolution_exit_opportunity"
            signal_type = "near_resolution_reprice"
        else:
            action = ACTION_SKIP
            reason = "; ".join(reject_reasons)
            signal_type = None
    elif ensemble_blocked:
        # Ensemble quality insufficient → downgrade to WATCH at best
        if edge_net_maker > 0.03 or edge_net_taker > 0.05:
            action = ACTION_WATCH
            reason = f"ensemble_blocked:{ensemble_block_reason}"
            signal_type = "watch_ensemble_weak"
        else:
            action = ACTION_SKIP
            reason = f"ensemble_blocked:{ensemble_block_reason}"
            signal_type = None
    else:
        signal_type = None
        action = ACTION_SKIP
        reason = "edge_below_threshold"

        # Taker check
        if taker_depth_ok:
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
        if action == ACTION_SKIP and maker_depth_ok:
            if edge_net_maker >= maker_strong_threshold:
                action = ACTION_PAPER_MAKER
                signal_type = "maker_strong"
                reason = f"maker_edge_{edge_net_maker:.3f}_strong"
            elif edge_net_maker >= maker_edge_threshold:
                action = ACTION_PAPER_MAKER
                signal_type = "maker_standard"
                reason = f"maker_edge_{edge_net_maker:.3f}"

        if action == ACTION_SKIP:
            if edge_net_maker > 0.03 or edge_net_taker > 0.05:
                action = ACTION_WATCH
                signal_type = "watch_low_edge"
                reason = f"watch_maker={edge_net_maker:.3f}_taker={edge_net_taker:.3f}"

    # ── Sizing ────────────────────────────────────────────────────────────────
    if action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
        if best_ask is not None and best_ask > 0:
            size = _kelly_size(blended, best_ask, bankroll, kelly_fraction, max_stake_usdc)
            size = max(size, 0.5)
        else:
            size = stake_usdc
    else:
        size = 0.0

    if action == ACTION_PAPER_TAKER:
        rec_price = best_ask
    elif action == ACTION_PAPER_MAKER:
        rec_price = maker_entry
    else:
        rec_price = None

    entry_side = ask_depth_top_n if action == ACTION_PAPER_TAKER else bid_depth_top_n
    exit_side = bid_depth_top_n

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
        ask_depth_top_n=ask_depth_top_n,
        bid_depth_top_n=bid_depth_top_n,
        entry_side_depth=entry_side,
        exit_side_depth=exit_side,
        n_members=n_members,
        deterministic_fallback_used=deterministic_fallback_used,
        forecast_blocked_reason=ensemble_block_reason if ensemble_blocked else forecast_blocked_reason,
    )
