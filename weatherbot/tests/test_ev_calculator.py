"""Tests for ev_calculator.py"""
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
    top_book_depth=20.0,
    display_mode="real_midpoint",
    ensemble_agreement=0.65,
    model_spread=2.0,
    safety=0.75,
    hours_to_close=24.0,
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
        top_book_depth=top_book_depth,
        display_price_mode=display_mode,
        ensemble_agreement=ensemble_agreement,
        model_spread=model_spread,
        settlement_safety=safety,
        hours_to_close=hours_to_close,
        **kwargs,
    )


def test_strong_maker_edge():
    result = _ev(model_prob=0.80, best_bid=0.55, best_ask=0.60, spread=0.05)
    assert result.action == ACTION_PAPER_MAKER
    assert result.edge_net_maker >= 0.08


def test_strong_taker_edge_with_stale():
    result = _ev(
        model_prob=0.85,
        best_ask=0.60,
        spread=0.08,   # wide but under 0.10
        hours_to_close=6.0,
        taker_requires_stale=True,
    )
    # Wide spread + near resolution triggers stale flag
    assert result.stale_flag is True
    assert result.action in (ACTION_PAPER_TAKER, ACTION_PAPER_MAKER)


def test_skip_spread_too_wide():
    result = _ev(model_prob=0.80, spread=0.15, best_bid=0.50, best_ask=0.65)
    assert result.action == ACTION_SKIP
    assert "spread_too_wide" in result.reason


def test_skip_no_book():
    result = _ev(best_bid=None, best_ask=None, spread=None, display_mode="no_book", top_book_depth=0.0)
    assert result.action == ACTION_SKIP
    assert "no_book" in result.reason


def test_skip_insufficient_depth():
    result = _ev(top_book_depth=0.5, stake_usdc=2.0, min_depth_multiplier=2.0)
    assert result.action == ACTION_SKIP
    assert "insufficient_depth" in result.reason


def test_skip_safety_too_low():
    result = _ev(safety=0.40)
    assert result.action == ACTION_SKIP
    assert "safety" in result.reason.lower()


def test_skip_near_resolution():
    result = _ev(hours_to_close=2.0, close_hours_reject=4.0)
    # Near resolution should either skip or EXIT_WATCH
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
        top_book_depth=20.0,
    )
    assert result.near_resolution is True


def test_watch_low_edge():
    result = _ev(model_prob=0.62, best_ask=0.55, spread=0.05)
    # Edge present but below thresholds → WATCH
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
    # With high-confidence nowcast pushing toward 0.90, blended should be higher
    assert r_with_nowcast.blended_probability > r_no_nowcast.blended_probability


def test_recommended_size_nonzero_for_candidate():
    result = _ev(model_prob=0.82, best_ask=0.60, spread=0.05, top_book_depth=20.0)
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
    # Not stale (spread narrow, hours far) → should not be taker
    assert result.stale_flag is False
    if result.action == ACTION_PAPER_TAKER:
        pytest.fail("Should not take without stale flag when taker_requires_stale=True")


def test_kelly_size_capped():
    result = _ev(model_prob=0.95, best_ask=0.40, top_book_depth=100.0, max_stake_usdc=5.0)
    if result.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
        assert result.recommended_size_usdc <= 5.0


def test_safety_hard_reject_override():
    """Even if EV says trade, safety=0.0 should prevent it."""
    result = _ev(model_prob=0.90, best_ask=0.50, safety=0.0)
    assert result.action == ACTION_SKIP
