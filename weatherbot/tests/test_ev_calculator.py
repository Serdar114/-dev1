"""Tests for ev_calculator.py — updated for side-specific depth + ensemble gating."""
import pytest
from weatherbot.ev_calculator import (
    ACTION_PAPER_MAKER,
    ACTION_PAPER_TAKER,
    ACTION_SKIP,
    ACTION_WATCH,
    ACTION_EXIT_WATCH,
    calculate_ev,
)


def _ev(
    model_prob=0.70,
    nowcast_prob=None,
    nowcast_conf="none",
    best_bid=0.55,
    best_ask=0.60,
    bid_size=50.0,
    ask_size=50.0,
    spread=0.05,
    # Side-specific depths — default large enough to not block
    ask_depth_top_n=20.0,
    bid_depth_top_n=20.0,
    book_state="normal",
    ensemble_agreement=0.65,
    model_spread=2.0,
    safety=0.75,
    hours_to_close=24.0,
    n_members=50,               # non-zero = valid ensemble
    deterministic_fallback=False,
    forecast_blocked_reason=None,
    **kwargs,
):
    return calculate_ev(
        model_probability=model_prob,
        nowcast_probability=nowcast_prob,
        nowcast_confidence=nowcast_conf,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
        spread=spread,
        ask_depth_top_n=ask_depth_top_n,
        bid_depth_top_n=bid_depth_top_n,
        book_state=book_state,
        ensemble_agreement=ensemble_agreement,
        model_spread=model_spread,
        settlement_safety=safety,
        hours_to_close=hours_to_close,
        n_members=n_members,
        deterministic_fallback_used=deterministic_fallback,
        forecast_blocked_reason=forecast_blocked_reason,
        **kwargs,
    )


# ── Basic action outcomes ─────────────────────────────────────────────────────

def test_strong_maker_edge():
    result = _ev(model_prob=0.80, best_bid=0.55, best_ask=0.60, spread=0.05)
    assert result.action == ACTION_PAPER_MAKER
    assert result.edge_net_maker >= 0.08


def test_strong_taker_edge_with_stale():
    result = _ev(
        model_prob=0.85,
        best_ask=0.60,
        spread=0.08,
        hours_to_close=6.0,
        taker_requires_stale=True,
    )
    assert result.stale_flag is True
    assert result.action in (ACTION_PAPER_TAKER, ACTION_PAPER_MAKER)


def test_skip_spread_too_wide():
    result = _ev(model_prob=0.80, spread=0.15, best_bid=0.50, best_ask=0.65)
    assert result.action == ACTION_SKIP
    assert "spread_too_wide" in result.reason


def test_skip_no_book():
    result = _ev(
        best_bid=None, best_ask=None, spread=None,
        book_state="no_book",
        ask_depth_top_n=0.0, bid_depth_top_n=0.0,
    )
    assert result.action == ACTION_SKIP
    assert "no_book" in result.reason


def test_skip_insufficient_depth():
    # Both sides very shallow
    result = _ev(ask_depth_top_n=0.3, bid_depth_top_n=0.3, stake_usdc=2.0, min_depth_multiplier=2.0)
    assert result.action == ACTION_SKIP
    assert "insufficient_depth" in result.reason


def test_taker_blocked_when_only_ask_insufficient():
    """Taker should be blocked when ask side is thin, even if bid side is deep."""
    result = _ev(
        model_prob=0.85,
        best_ask=0.60,
        spread=0.05,
        ask_depth_top_n=1.0,    # thin ask → taker blocked
        bid_depth_top_n=50.0,   # deep bid irrelevant for taker
        stake_usdc=2.0,
        min_depth_multiplier=2.0,
        taker_requires_stale=False,
    )
    # Taker blocked but maker might still work if bid depth OK
    if result.action == ACTION_PAPER_TAKER:
        pytest.fail("Taker should be blocked when ask_depth is insufficient")


def test_skip_safety_too_low():
    result = _ev(safety=0.40)
    assert result.action == ACTION_SKIP
    assert "safety" in result.reason.lower()


def test_skip_near_resolution():
    result = _ev(hours_to_close=2.0, close_hours_reject=4.0)
    assert result.near_resolution is True
    assert result.action in (ACTION_SKIP, ACTION_EXIT_WATCH)


def test_exit_watch_near_resolution_with_edge():
    result = _ev(
        model_prob=0.82,
        best_ask=0.60,
        spread=0.05,
        hours_to_close=2.0,
        close_hours_reject=4.0,
        safety=0.75,
    )
    assert result.near_resolution is True


def test_watch_low_edge():
    result = _ev(model_prob=0.62, best_ask=0.55, spread=0.05)
    assert result.action in (ACTION_WATCH, ACTION_SKIP)


def test_no_model_edge_skip():
    result = _ev(model_prob=0.52, best_ask=0.55)
    assert result.action == ACTION_SKIP


def test_edge_gross_correct():
    result = _ev(model_prob=0.75, best_ask=0.60)
    expected_gross = 0.75 - 0.60
    assert abs(result.edge_gross - expected_gross) < 0.001


def test_nowcast_blending():
    r_no_nowcast = _ev(model_prob=0.70, nowcast_prob=None, nowcast_conf="none")
    r_with_nowcast = _ev(model_prob=0.70, nowcast_prob=0.90, nowcast_conf="high")
    assert r_with_nowcast.blended_probability > r_no_nowcast.blended_probability


def test_recommended_size_nonzero_for_candidate():
    result = _ev(model_prob=0.82, best_ask=0.60, spread=0.05)
    if result.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
        assert result.recommended_size_usdc > 0


def test_taker_requires_stale_respected():
    result = _ev(
        model_prob=0.85,
        best_ask=0.62,
        spread=0.04,
        hours_to_close=48.0,
        taker_requires_stale=True,
    )
    assert result.stale_flag is False
    if result.action == ACTION_PAPER_TAKER:
        pytest.fail("Should not take without stale flag when taker_requires_stale=True")


def test_kelly_size_capped():
    result = _ev(model_prob=0.95, best_ask=0.40, ask_depth_top_n=100.0, bid_depth_top_n=100.0, max_stake_usdc=5.0)
    if result.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
        assert result.recommended_size_usdc <= 5.0


def test_safety_hard_reject_override():
    result = _ev(model_prob=0.90, best_ask=0.50, safety=0.0)
    assert result.action == ACTION_SKIP


# ── Ensemble gating ───────────────────────────────────────────────────────────

def test_n_members_zero_blocks_candidate():
    """n_members=0 must prevent PAPER_MAKER/PAPER_TAKER."""
    result = _ev(model_prob=0.85, best_ask=0.55, n_members=0)
    assert result.action in (ACTION_WATCH, ACTION_SKIP)
    assert result.n_members == 0
    assert result.forecast_blocked_reason == "n_members_zero"


def test_deterministic_fallback_blocks_candidate():
    """deterministic_fallback=True must prevent PAPER_MAKER/PAPER_TAKER."""
    result = _ev(model_prob=0.85, best_ask=0.55, n_members=1, deterministic_fallback=True)
    assert result.action in (ACTION_WATCH, ACTION_SKIP)
    assert result.deterministic_fallback_used is True


def test_forecast_blocked_reason_blocks_candidate():
    result = _ev(model_prob=0.85, best_ask=0.55, forecast_blocked_reason="missing_target_date")
    assert result.action in (ACTION_WATCH, ACTION_SKIP)
    assert result.forecast_blocked_reason == "missing_target_date"


def test_valid_ensemble_allows_candidate():
    """n_members=50, no fallback → candidate actions possible."""
    result = _ev(
        model_prob=0.82,
        best_ask=0.58,
        spread=0.05,
        n_members=50,
        deterministic_fallback=False,
        forecast_blocked_reason=None,
    )
    assert result.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER, ACTION_WATCH)


# ── Side-specific depth fields ────────────────────────────────────────────────

def test_depth_fields_populated():
    result = _ev(ask_depth_top_n=15.0, bid_depth_top_n=25.0)
    assert result.ask_depth_top_n == 15.0
    assert result.bid_depth_top_n == 25.0


def test_entry_side_depth_is_ask_for_taker():
    result = _ev(
        model_prob=0.85,
        ask_depth_top_n=20.0,
        bid_depth_top_n=5.0,
        spread=0.05,
        taker_requires_stale=False,
    )
    if result.action == ACTION_PAPER_TAKER:
        assert result.entry_side_depth == result.ask_depth_top_n
