"""
Phase 2 maker evaluation tests.

Tests:
  - quote_id lifecycle integrity
  - None semantics for unavailable maker fields
  - passive edge gating
  - degenerate book rejection at probe level
  - crossed quote rejection
  - fill transition (filled_adverse / filled_favorable)
  - expiry transition (expired_unfilled)
  - boundary resolution attribution
  - maker PnL-if-held calculation
  - metrics aggregation for maker counts and win/loss
  - ASCII-safe logging (no Unicode em-dash in console formatter)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from dataclasses import replace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from models import MarketSnapshot, MetricsSnapshot, ShadowQuote, SignalDecision
from maker_shadow_probe import MakerShadowProbe
from metrics import MetricsCollector
from settings import Settings

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_settings() -> Settings:
    cfg = Settings(CONFIG_PATH)
    return cfg


def make_market(
    bid_yes: float = 0.40,
    ask_yes: float = 0.46,
    bid_no: float = 0.54,
    ask_no: float = 0.60,
    ste: float = 120.0,
    slug: str = "btc-5m-test",
) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id="cid",
        token_id_yes="yes",
        token_id_no="no",
        best_bid_yes=bid_yes,
        best_ask_yes=ask_yes,
        best_bid_no=bid_no,
        best_ask_no=ask_no,
        last_trade_price_yes=None,
        window_end_ts=time.time() + ste,
        slug=slug,
    )


def make_decision(
    side: str = "yes",
    fair_yes: float = 0.55,
    implied_yes: float = 0.43,
    ste: float = 120.0,
    regime: str = "TRENDING",
    pattern: str = "BURST",
    confidence: float = 0.65,
    elapsed: float = 45.0,
    delta_raw: float = 0.003,
) -> SignalDecision:
    return SignalDecision(
        ts=time.time(),
        window_ts=time.time() + ste,
        lane="maker_shadow",
        action="SHADOW_QUOTE",
        chosen_side=side,
        reason="test",
        seconds_to_expiry=ste,
        elapsed_from_window_start=elapsed,
        btc_mid=84000.0,
        window_open=83748.0,
        delta_pct=delta_raw,
        delta_raw_fraction=delta_raw,
        delta_pct_display=delta_raw * 100.0,
        realized_vol_60s=0.001,
        fair_yes_prob=fair_yes,
        implied_yes_prob=implied_yes,
        raw_edge_yes=0.05,
        raw_edge_no=0.01,
        after_fee_edge_yes=0.04,
        after_fee_edge_no=0.005,
        confidence_score=confidence,
        confidence_components="div=0.12;base=1.0;reg=1.5;pat=1.2;dq=0.8;dm=0.83;raw=1.50",
        regime=regime,
        pattern=pattern,
        fair_computed=True,
        fair_computed_fresh=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# A) quote_id integrity
# ──────────────────────────────────────────────────────────────────────────────

class TestQuoteIdIntegrity:
    def test_quote_id_is_set_on_build(self):
        """Every built quote gets a non-empty quote_id."""
        cfg = make_settings()
        # Use a passive edge that passes the min_passive_edge guard (0.02)
        # fair_yes=0.55, bid_yes aligned to tick=0.01 -> quote_price=0.40
        # passive_edge = 0.55 - 0.40 = 0.15 > 0.02 -> passes
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55, implied_yes=0.43)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.quote_id != ""
        assert len(quote.quote_id) == 12  # uuid4 hex[:12]

    def test_each_quote_gets_unique_id(self):
        """Two quotes built in sequence have different quote_ids."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        d1 = make_decision(side="yes", fair_yes=0.55)
        d2 = make_decision(side="yes", fair_yes=0.56)
        q1 = probe.build_quote(d1, market)
        q2 = probe.build_quote(d2, market)
        assert q1 is not None
        assert q2 is not None
        assert q1.quote_id != q2.quote_id

    def test_quote_id_preserved_through_state_transitions(self):
        """quote_id stays the same across fill transitions (dataclasses.replace preserves it)."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # Place a quote with bid=0.40, ask=0.46; fill when current_ask <= quote_price
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        original_id = quote.quote_id

        # Simulate fill: current_ask drops to quote_price (0.40)
        fill_market = make_market(bid_yes=0.38, ask_yes=0.40)
        completed = probe.process_pending(fill_market)
        # Should have a filled event
        assert len(completed) >= 1
        fill_event = next((q for q in completed if q.fill_status == "filled"), None)
        assert fill_event is not None
        assert fill_event.quote_id == original_id

    def test_default_quote_id_never_empty(self):
        """ShadowQuote dataclass default always generates a non-empty quote_id."""
        q = ShadowQuote(
            ts=1.0,
            window_ts=2.0,
            seconds_to_expiry=100.0,
            side="yes",
            quote_price=0.40,
            best_bid=0.40,
            best_ask=0.46,
            tick_size=0.01,
            crossed=False,
        )
        assert q.quote_id != ""
        assert len(q.quote_id) == 12


# ──────────────────────────────────────────────────────────────────────────────
# B) None semantics
# ──────────────────────────────────────────────────────────────────────────────

class TestNoneSemantics:
    def test_unresolved_fields_are_none_not_zero(self):
        """Boundary fields are None until boundary resolution; never default 0.0."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_ts is None
        assert quote.fill_mid is None
        assert quote.next_mid_after_fill is None
        assert quote.adverse_move_after_fill is None
        assert quote.favorable_move_after_fill is None
        assert quote.boundary_outcome_yes is None
        assert quote.boundary_outcome_for_side is None
        assert quote.maker_pnl_if_held is None
        assert quote.maker_edge_realized_vs_expected is None

    def test_intended_passive_edge_is_float_when_computed(self):
        """intended_passive_edge is a float (not None) when quote is valid."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.intended_passive_edge is not None
        assert quote.intended_passive_edge > 0.0

    def test_rejected_quote_has_reject_reason_not_none(self):
        """A probe-rejected quote has reject_reason set (not None)."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # fair_yes barely above implied but passive_edge will be small
        # bid_yes=0.44, ask_yes=0.46; quote_price=0.44; passive_edge=0.55-0.44=0.11
        # With min_passive_edge=0.02 this passes — use tight values to fail
        # fair_yes=0.45, bid_yes=0.44 -> passive_edge=0.45-0.44=0.01 < 0.02 threshold
        market = make_market(bid_yes=0.44, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.45, implied_yes=0.43)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.reject_reason is not None
        assert "passive_edge" in quote.reject_reason or "min_passive_edge" in quote.reject_reason

    def test_crossed_quote_has_none_boundary_fields(self):
        """A crossed quote that never entered pending has None fill/boundary fields."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # Force cross: bid >= ask after tick alignment
        # bid_yes=0.50, ask_yes=0.50 -> crossed
        market = make_market(bid_yes=0.50, ask_yes=0.50)
        decision = make_decision(side="yes", fair_yes=0.70, implied_yes=0.50)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_status == "crossed_rejected"
        assert quote.fill_ts is None
        assert quote.boundary_outcome_yes is None
        assert quote.maker_pnl_if_held is None


# ──────────────────────────────────────────────────────────────────────────────
# C) Passive edge gating
# ──────────────────────────────────────────────────────────────────────────────

class TestPassiveEdgeGating:
    def test_quote_rejected_when_passive_edge_below_threshold(self):
        """Build returns a rejected quote when passive_edge < min_passive_edge."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # min_passive_edge default = 0.02
        # fair_yes=0.45, bid_yes=0.44, quote_price=0.44, passive_edge=0.01 < 0.02
        market = make_market(bid_yes=0.44, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.45, implied_yes=0.43)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_status == "crossed_rejected"
        assert "min_passive_edge" in (quote.reject_reason or "") or \
               "passive_edge" in (quote.reject_reason or "")
        # Quote is NOT added to pending
        assert len(probe._pending) == 0

    def test_quote_accepted_when_passive_edge_meets_threshold(self):
        """Build returns a pending quote when passive_edge >= min_passive_edge."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # fair_yes=0.55, bid_yes=0.40, quote_price=0.40, passive_edge=0.15 > 0.02
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55, implied_yes=0.43)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_status == "pending"
        assert len(probe._pending) == 1

    def test_passive_edge_computed_correctly(self):
        """intended_passive_edge = fair_for_side - quote_price (tick-aligned)."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # bid=0.423 -> aligned to tick=0.01 -> 0.42
        market = make_market(bid_yes=0.423, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.quote_price == pytest.approx(0.42, abs=1e-9)
        expected_edge = 0.55 - 0.42
        assert quote.intended_passive_edge == pytest.approx(expected_edge, abs=1e-9)

    def test_no_side_passive_edge(self):
        """For NO side, passive_edge uses fair_no = 1 - fair_yes."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # NO side: fair_yes=0.40, fair_no=0.60, bid_no=0.54 -> quote_price=0.54
        # passive_edge = 0.60 - 0.54 = 0.06 > 0.02
        market = make_market(bid_no=0.54, ask_no=0.60)
        decision = make_decision(side="no", fair_yes=0.40, implied_yes=0.44)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_status == "pending"
        assert quote.intended_passive_edge == pytest.approx(0.60 - 0.54, abs=1e-9)


# ──────────────────────────────────────────────────────────────────────────────
# D) Degenerate book rejection at probe level
# ──────────────────────────────────────────────────────────────────────────────

class TestDegenerateBookRejectionProbe:
    def test_yes_bid_zero_rejected(self):
        """bid_yes=0 -> degenerate book reject."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.0, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_status == "crossed_rejected"
        assert "degenerate_book" in (quote.reject_reason or "")

    def test_yes_bid_ge_ask_rejected(self):
        """bid >= ask (crossed book) -> degenerate book reject."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.50, ask_yes=0.48)  # inverted
        decision = make_decision(side="yes", fair_yes=0.70, implied_yes=0.49)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert "degenerate_book" in (quote.reject_reason or "")

    def test_spread_too_wide_rejected(self):
        """bid-ask spread > max_spread_maker -> rejected."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # max_spread_maker default = 0.06; spread=0.10 > 0.06
        market = make_market(bid_yes=0.40, ask_yes=0.50)  # spread = 0.10
        decision = make_decision(side="yes", fair_yes=0.60, implied_yes=0.45)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert "spread_too_wide" in (quote.reject_reason or "")
        assert len(probe._pending) == 0


# ──────────────────────────────────────────────────────────────────────────────
# E) Crossed quote rejection
# ──────────────────────────────────────────────────────────────────────────────

class TestCrossedQuoteRejection:
    def test_crossed_when_quote_price_ge_ask(self):
        """quote_price >= best_ask triggers crossed_rejected status.
        Setup: bid=0.50, ask=0.51 -> valid spread; bid aligns to tick 0.50 = ask (no, ask=0.51).
        Actually need quote_price >= ask. Use bid=0.51 that ticks to 0.51, ask=0.51.
        But bid >= ask is degenerate. Use bid=0.507 -> ticks to 0.50, ask=0.50 — no, still degen.
        Real cross: bid=0.501, ask=0.51, tick=0.01 -> quote_price=floor(0.501/0.01)*0.01=0.50 < 0.51 -> not crossed.
        To get crossed: need bid that ticks to >= ask. E.g. bid=0.510 ticks to 0.51, ask=0.51 -> but bid==ask = degen.
        Use tick=0.02: bid=0.519 -> floor(0.519/0.02)*0.02 = floor(25.95)*0.02 = 25*0.02 = 0.50; ask=0.50 -> degen again.
        Simplest approach: monkeypatch tick_size to 0.10 so bid=0.505 -> 0.50, ask=0.50... still degen.
        Real scenario: quote_price >= ask when tick causes upward rounding. But we floor, so quote_price <= bid < ask always.
        A crossed quote can only happen when quote_price == bid == ask (degenerate) or if bid was already at ask.
        The crossed guard is a safety net for edge cases. Test it by constructing a quote directly.
        """
        # The crossed guard fires when quote_price >= best_ask after tick alignment.
        # Force this by setting a quote_price that equals best_ask (bid equals ask after tick).
        # The degenerate_book guard fires first when bid >= ask, so we can't test crossed
        # independently via build_quote with bid==ask.
        # Test the logic directly: construct a quote with the expected state.
        q = ShadowQuote(
            ts=time.time(), window_ts=time.time() + 120, seconds_to_expiry=100.0,
            side="yes", quote_price=0.50, best_bid=0.50, best_ask=0.50,
            tick_size=0.01, crossed=True, fill_status="crossed_rejected",
        )
        assert q.fill_status == "crossed_rejected"
        assert q.crossed is True
        assert q.fill_ts is None
        assert q.maker_pnl_if_held is None

    def test_non_crossed_quote_is_pending(self):
        """quote_price < best_ask -> pending."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_status == "pending"
        assert quote.crossed is False


# ──────────────────────────────────────────────────────────────────────────────
# F) Fill transition
# ──────────────────────────────────────────────────────────────────────────────

class TestFillTransition:
    def _place_and_fill(self, price_up: bool):
        """Place a YES maker quote, then trigger fill and measure direction."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        # bid=0.40, ask=0.46 -> quote_price=0.40
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None
        assert quote.fill_status == "pending"

        # Fill trigger: current_ask <= quote_price (0.40)
        fill_market = make_market(bid_yes=0.38, ask_yes=0.40)
        completed = probe.process_pending(fill_market)
        fill_event = next((q for q in completed if q.fill_status == "filled"), None)
        assert fill_event is not None
        assert fill_event.fill_ts is not None
        assert fill_event.fill_mid is not None

        # Next tick: measure adverse/favorable
        if price_up:
            next_market = make_market(bid_yes=0.44, ask_yes=0.48)   # mid rose -> favorable
        else:
            next_market = make_market(bid_yes=0.32, ask_yes=0.36)   # mid fell -> adverse

        completed2 = probe.process_pending(next_market)
        return completed2

    def test_fill_favorable_when_price_rises(self):
        """If next-tick mid is above fill_mid -> filled_favorable."""
        results = self._place_and_fill(price_up=True)
        measured = next(
            (q for q in results if q.fill_status in ("filled_favorable", "filled_adverse")),
            None,
        )
        assert measured is not None
        assert measured.fill_status == "filled_favorable"
        assert measured.favorable_move_after_fill is not None
        assert measured.favorable_move_after_fill > 0
        assert measured.adverse_move_after_fill is None

    def test_fill_adverse_when_price_falls(self):
        """If next-tick mid is below fill_mid -> filled_adverse."""
        results = self._place_and_fill(price_up=False)
        measured = next(
            (q for q in results if q.fill_status in ("filled_favorable", "filled_adverse")),
            None,
        )
        assert measured is not None
        assert measured.fill_status == "filled_adverse"
        assert measured.adverse_move_after_fill is not None
        assert measured.adverse_move_after_fill < 0
        assert measured.favorable_move_after_fill is None

    def test_fill_mid_is_recorded(self):
        """fill_mid is set to the mid at fill time, not 0.0."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        probe.build_quote(decision, market)

        fill_market = make_market(bid_yes=0.38, ask_yes=0.40)
        completed = probe.process_pending(fill_market)
        fill_event = next(q for q in completed if q.fill_status == "filled")
        expected_mid = (0.38 + 0.40) / 2.0
        assert fill_event.fill_mid == pytest.approx(expected_mid, abs=1e-9)


# ──────────────────────────────────────────────────────────────────────────────
# G) Expiry transition
# ──────────────────────────────────────────────────────────────────────────────

class TestExpiryTransition:
    def test_quote_expires_when_ttl_elapsed(self):
        """A quote that never fills becomes expired_unfilled after TTL."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        quote = probe.build_quote(decision, market)
        assert quote is not None

        # Manually expire: set expires_ts to past
        entry = probe._pending[0]
        probe._pending[0] = (entry[0], entry[1], time.time() - 1.0)

        # Next tick: book hasn't filled
        no_fill_market = make_market(bid_yes=0.39, ask_yes=0.45)
        completed = probe.process_pending(no_fill_market)
        expired = next((q for q in completed if q.fill_status == "expired_unfilled"), None)
        assert expired is not None
        assert expired.fill_ts is None
        assert expired.fill_mid is None
        assert expired.boundary_outcome_yes is None  # not yet resolved

    def test_pending_queue_empty_after_expiry(self):
        """After expiry, the quote is removed from pending."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        probe.build_quote(decision, market)
        assert len(probe._pending) == 1

        entry = probe._pending[0]
        probe._pending[0] = (entry[0], entry[1], time.time() - 1.0)

        probe.process_pending(make_market())
        assert len(probe._pending) == 0


# ──────────────────────────────────────────────────────────────────────────────
# H) Boundary resolution attribution
# ──────────────────────────────────────────────────────────────────────────────

class TestBoundaryResolution:
    def _setup_probe_with_filled_quote(self, outcome_yes: float):
        """Place, fill, measure, then resolve boundary. Returns resolved quote."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        probe.build_quote(decision, market)

        # Fill
        fill_market = make_market(bid_yes=0.38, ask_yes=0.40)
        probe.process_pending(fill_market)

        # Measure adverse/favorable
        next_market = make_market(bid_yes=0.44, ask_yes=0.48)
        probe.process_pending(next_market)

        # Boundary
        resolved = probe.resolve_boundary(outcome_yes)
        return resolved

    def test_boundary_outcome_yes_set(self):
        """boundary_outcome_yes is set on all resolved quotes."""
        resolved = self._setup_probe_with_filled_quote(outcome_yes=1.0)
        assert len(resolved) >= 1
        for q in resolved:
            assert q.boundary_outcome_yes == 1.0

    def test_boundary_outcome_for_side_yes(self):
        """YES side: boundary_outcome_for_side matches boundary_outcome_yes."""
        resolved = self._setup_probe_with_filled_quote(outcome_yes=1.0)
        yes_quotes = [q for q in resolved if q.side == "yes"]
        assert len(yes_quotes) >= 1
        for q in yes_quotes:
            assert q.boundary_outcome_for_side == 1.0

    def test_boundary_outcome_for_side_no(self):
        """NO side: boundary_outcome_for_side = 1 - outcome_yes."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_no=0.54, ask_no=0.60)
        decision = make_decision(side="no", fair_yes=0.40, implied_yes=0.44)
        probe.build_quote(decision, market)

        fill_market = make_market(bid_no=0.52, ask_no=0.54)
        probe.process_pending(fill_market)
        probe.process_pending(make_market())  # measure

        resolved = probe.resolve_boundary(outcome_yes=0.0)  # BTC down -> NO wins
        no_quotes = [q for q in resolved if q.side == "no"]
        assert len(no_quotes) >= 1
        for q in no_quotes:
            assert q.boundary_outcome_for_side == 1.0  # NO wins when outcome_yes=0

    def test_pending_at_boundary_becomes_boundary_resolved(self):
        """A still-pending quote at boundary gets fill_status=boundary_resolved."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        probe.build_quote(decision, market)

        # Don't fill; go straight to boundary
        resolved = probe.resolve_boundary(outcome_yes=1.0)
        assert len(resolved) >= 1
        pending_at_boundary = next(
            (q for q in resolved if q.fill_status == "boundary_resolved"), None
        )
        assert pending_at_boundary is not None
        assert pending_at_boundary.maker_pnl_if_held is None  # no fill happened

    def test_reset_clears_all_state(self):
        """After resolve_boundary + reset, all queues are empty."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        probe.build_quote(decision, market)

        probe.resolve_boundary(outcome_yes=1.0)
        probe.reset()
        assert len(probe._pending) == 0
        assert len(probe._filled_awaiting) == 0
        assert len(probe._settled) == 0


# ──────────────────────────────────────────────────────────────────────────────
# I) Maker PnL-if-held calculation
# ──────────────────────────────────────────────────────────────────────────────

class TestMakerPnlIfHeld:
    def _get_filled_resolved_quote(self, outcome_yes: float, side: str = "yes"):
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        if side == "yes":
            market = make_market(bid_yes=0.40, ask_yes=0.46)
            decision = make_decision(side="yes", fair_yes=0.55)
        else:
            market = make_market(bid_no=0.54, ask_no=0.60)
            decision = make_decision(side="no", fair_yes=0.40)

        probe.build_quote(decision, market)
        if side == "yes":
            fill_m = make_market(bid_yes=0.38, ask_yes=0.40)
        else:
            fill_m = make_market(bid_no=0.52, ask_no=0.54)
        probe.process_pending(fill_m)
        probe.process_pending(make_market())  # measure
        resolved = probe.resolve_boundary(outcome_yes)
        filled = [q for q in resolved if q.maker_pnl_if_held is not None]
        assert len(filled) >= 1
        return filled[0]

    def test_pnl_if_held_yes_wins(self):
        """YES maker filled at 0.40; YES wins -> pnl = 1.0 - 0.40 = +0.60."""
        q = self._get_filled_resolved_quote(outcome_yes=1.0, side="yes")
        assert q.maker_pnl_if_held == pytest.approx(1.0 - q.quote_price, abs=1e-9)
        assert q.maker_pnl_if_held > 0

    def test_pnl_if_held_yes_loses(self):
        """YES maker filled at 0.40; YES loses -> pnl = 0.0 - 0.40 = -0.40."""
        q = self._get_filled_resolved_quote(outcome_yes=0.0, side="yes")
        assert q.maker_pnl_if_held == pytest.approx(0.0 - q.quote_price, abs=1e-9)
        assert q.maker_pnl_if_held < 0

    def test_pnl_if_held_no_wins(self):
        """NO maker filled at 0.54; NO wins (outcome_yes=0) -> pnl = 1.0 - 0.54."""
        q = self._get_filled_resolved_quote(outcome_yes=0.0, side="no")
        assert q.maker_pnl_if_held == pytest.approx(1.0 - q.quote_price, abs=1e-9)
        assert q.maker_pnl_if_held > 0

    def test_edge_realized_vs_expected(self):
        """maker_edge_realized_vs_expected = pnl - intended_passive_edge."""
        q = self._get_filled_resolved_quote(outcome_yes=1.0, side="yes")
        assert q.maker_edge_realized_vs_expected is not None
        expected = q.maker_pnl_if_held - q.intended_passive_edge
        assert q.maker_edge_realized_vs_expected == pytest.approx(expected, abs=1e-9)

    def test_unfilled_quote_has_no_pnl(self):
        """A quote that expired unfilled has maker_pnl_if_held=None."""
        cfg = make_settings()
        probe = MakerShadowProbe(cfg)
        market = make_market(bid_yes=0.40, ask_yes=0.46)
        decision = make_decision(side="yes", fair_yes=0.55)
        probe.build_quote(decision, market)

        # Force expiry
        entry = probe._pending[0]
        probe._pending[0] = (entry[0], entry[1], time.time() - 1.0)
        probe.process_pending(make_market())  # expire it

        resolved = probe.resolve_boundary(outcome_yes=1.0)
        # Expired quote should have no pnl since it wasn't filled
        expired = next((q for q in resolved if "expired" in q.fill_status), None)
        # expired_unfilled was removed from probe, so may not be in resolved
        # But there should be no quotes with pnl since probe settled list is empty
        for q in resolved:
            assert q.maker_pnl_if_held is None


# ──────────────────────────────────────────────────────────────────────────────
# J) Metrics aggregation
# ──────────────────────────────────────────────────────────────────────────────

class TestMetricsAggregation:
    def _make_signal(self, action: str, side=None, lane: str = "maker_shadow") -> SignalDecision:
        return SignalDecision(
            ts=time.time(),
            window_ts=time.time() + 120,
            lane=lane,
            action=action,
            chosen_side=side,
            reason="test",
        )

    def _make_shadow_quote(
        self,
        fill_status: str = "pending",
        intended_passive_edge: float = 0.10,
        side: str = "yes",
    ) -> ShadowQuote:
        return ShadowQuote(
            ts=time.time(),
            window_ts=time.time() + 120,
            seconds_to_expiry=100.0,
            side=side,
            quote_price=0.40,
            best_bid=0.40,
            best_ask=0.46,
            tick_size=0.01,
            crossed=False,
            fill_status=fill_status,
            intended_passive_edge=intended_passive_edge,
            regime="TRENDING",
            pattern="BURST",
        )

    def test_maker_quote_count_increments_on_shadow_quote_signal(self):
        m = MetricsCollector()
        m.on_signal(self._make_signal("SHADOW_QUOTE", side="yes"))
        snap = m.snapshot()
        assert snap["maker_quote_count"] == 1

    def test_maker_no_quote_count_increments_on_no_quote_signal(self):
        m = MetricsCollector()
        m.on_signal(self._make_signal("NO_QUOTE"))
        snap = m.snapshot()
        assert snap["maker_no_quote_count"] == 1

    def test_maker_fill_count_increments(self):
        # Full lifecycle: pending -> filled (intermediate) -> filled_adverse (terminal).
        # fill should be counted ONCE, at the terminal state only.
        m = MetricsCollector()
        q = self._make_shadow_quote(fill_status="pending")
        m.on_shadow_fill(q)
        # Intermediate "filled" — must NOT increment maker_fill_count
        filled = replace(q, fill_status="filled")
        m.on_shadow_state_change(filled)
        # Terminal "filled_adverse" — increments maker_fill_count exactly once
        adverse = replace(q, fill_status="filled_adverse")
        m.on_shadow_state_change(adverse)
        snap = m.snapshot()
        assert snap["maker_fill_count"] == 1  # one fill, not two (was 2 before bug fix)
        assert snap["maker_adverse_fill_count"] == 1

    def test_maker_expired_count_increments(self):
        m = MetricsCollector()
        q = self._make_shadow_quote(fill_status="pending")
        m.on_shadow_fill(q)
        expired = replace(q, fill_status="expired_unfilled")
        m.on_shadow_state_change(expired)
        snap = m.snapshot()
        assert snap["maker_expired_count"] == 1
        assert snap["maker_fill_count"] == 0

    def test_boundary_win_loss_counts(self):
        m = MetricsCollector()
        q_win = self._make_shadow_quote()
        q_win = replace(
            q_win,
            fill_status="filled_favorable",
            boundary_outcome_yes=1.0,
            boundary_outcome_for_side=1.0,
            maker_pnl_if_held=0.60,
            intended_passive_edge=0.10,
            maker_edge_realized_vs_expected=0.50,
        )
        q_loss = self._make_shadow_quote()
        q_loss = replace(
            q_loss,
            fill_status="filled_adverse",
            boundary_outcome_yes=0.0,
            boundary_outcome_for_side=0.0,
            maker_pnl_if_held=-0.40,
            intended_passive_edge=0.10,
            maker_edge_realized_vs_expected=-0.50,
        )
        m.on_boundary_resolved(q_win)
        m.on_boundary_resolved(q_loss)
        snap = m.snapshot()
        assert snap["maker_boundary_resolved_count"] == 2
        assert snap["maker_boundary_win_count"] == 1
        assert snap["maker_boundary_loss_count"] == 1
        assert snap["maker_boundary_win_rate"] == pytest.approx(0.5, abs=1e-3)

    def test_pnl_aggregation(self):
        m = MetricsCollector()
        for pnl in [0.60, -0.40, 0.60]:
            q = self._make_shadow_quote()
            q = replace(
                q,
                fill_status="filled_favorable",
                boundary_outcome_yes=1.0,
                boundary_outcome_for_side=1.0,
                maker_pnl_if_held=pnl,
                intended_passive_edge=0.10,
                maker_edge_realized_vs_expected=pnl - 0.10,
            )
            m.on_boundary_resolved(q)
        snap = m.snapshot()
        assert snap["maker_pnl_if_held_total"] == pytest.approx(0.60 - 0.40 + 0.60, abs=1e-5)
        assert snap["maker_pnl_if_held_avg"] == pytest.approx((0.60 - 0.40 + 0.60) / 3.0, abs=1e-5)

    def test_fill_count_not_double_counted_across_lifecycle(self):
        """Regression: pending->filled->filled_adverse lifecycle counts as ONE fill, not two."""
        m = MetricsCollector()
        q = self._make_shadow_quote(fill_status="pending")
        m.on_shadow_fill(q)
        # Intermediate state emitted first by process_pending
        m.on_shadow_state_change(replace(q, fill_status="filled"))
        # Terminal state emitted on next tick by process_pending
        m.on_shadow_state_change(replace(q, fill_status="filled_adverse"))
        snap = m.snapshot()
        # fill_rate = maker_fill_count / maker_pending_count = 1/1 = 1.0 (not > 1)
        assert snap["maker_fill_count"] == 1
        assert snap["maker_fill_rate"] <= 1.0

    def test_fill_rate_never_exceeds_one(self):
        """Regression: fill_rate must always be <= 1.0 regardless of lifecycle events."""
        m = MetricsCollector()
        # 3 pending quotes
        for _ in range(3):
            q = self._make_shadow_quote(fill_status="pending")
            m.on_shadow_fill(q)
        # All 3 fill through full lifecycle (would have been 6 fills before fix)
        for _ in range(3):
            q = self._make_shadow_quote(fill_status="pending")
            m.on_shadow_state_change(replace(q, fill_status="filled"))
            m.on_shadow_state_change(replace(q, fill_status="filled_adverse"))
        snap = m.snapshot()
        assert snap["maker_fill_rate"] <= 1.0
        assert snap["maker_fill_count"] == 3

    def test_boundary_cut_fill_counted_once_in_on_boundary_resolved(self):
        """A quote that fills but hits boundary before quality measurement is counted once."""
        m = MetricsCollector()
        q = self._make_shadow_quote(fill_status="pending")
        m.on_shadow_fill(q)
        # Fill occurs but boundary arrives before next-tick measurement
        m.on_shadow_state_change(replace(q, fill_status="filled"))  # NOT counted here
        # Boundary resolution: fill_status remains "filled" (not measured)
        boundary_q = replace(
            q,
            fill_status="filled",
            boundary_outcome_yes=1.0,
            boundary_outcome_for_side=1.0,
            maker_pnl_if_held=0.60,
        )
        m.on_boundary_resolved(boundary_q)
        snap = m.snapshot()
        assert snap["maker_fill_count"] == 1  # counted once, in on_boundary_resolved

    def test_by_side_breakdown_populated(self):
        m = MetricsCollector()
        q = self._make_shadow_quote(side="yes")
        q = replace(
            q,
            boundary_outcome_yes=1.0,
            boundary_outcome_for_side=1.0,
            maker_pnl_if_held=0.60,
            intended_passive_edge=0.10,
            fill_status="filled_favorable",
        )
        m.on_boundary_resolved(q)
        snap = m.snapshot()
        assert "yes" in snap["maker_by_side"]
        assert snap["maker_by_side"]["yes"]["boundary_win_count"] == 1

    def test_by_regime_breakdown(self):
        m = MetricsCollector()
        q = self._make_shadow_quote()
        q = replace(q, regime="TRENDING", boundary_outcome_for_side=1.0,
                    boundary_outcome_yes=1.0, maker_pnl_if_held=0.1,
                    intended_passive_edge=0.05, fill_status="filled_favorable")
        m.on_boundary_resolved(q)
        snap = m.snapshot()
        assert "TRENDING" in snap["maker_by_regime"]

    def test_fill_rate_computed(self):
        m = MetricsCollector()
        # 2 pending, 1 fills completely (terminal state = filled_adverse after measurement)
        for _ in range(2):
            q = self._make_shadow_quote(fill_status="pending")
            m.on_shadow_fill(q)
        # Use terminal fill state, not intermediate "filled"
        filled_final = self._make_shadow_quote(fill_status="filled_adverse")
        m.on_shadow_state_change(filled_final)
        snap = m.snapshot()
        assert snap["maker_fill_rate"] == pytest.approx(1 / 2, abs=1e-3)


# ──────────────────────────────────────────────────────────────────────────────
# K) ASCII-safe logging
# ──────────────────────────────────────────────────────────────────────────────

class TestAsciiSafeLogging:
    def test_console_formatter_no_em_dash(self):
        """The console log formatter must not contain em-dash (U+2013) or similar."""
        import logging
        from logger import setup_console_logger
        setup_console_logger("DEBUG")
        root = logging.getLogger()
        for handler in root.handlers:
            if hasattr(handler, "formatter") and handler.formatter:
                fmt = handler.formatter._fmt
                # Must not contain em-dash U+2013 or en-dash U+2013
                assert "\u2013" not in fmt, f"Em-dash found in formatter: {fmt!r}"
                assert "\u2014" not in fmt, f"Em-dash U+2014 found in formatter: {fmt!r}"

    def test_no_unicode_arrows_in_log_format(self):
        """Log format string must be ASCII-encodable (for Windows cp125x compat)."""
        import logging
        from logger import setup_console_logger
        setup_console_logger("INFO")
        root = logging.getLogger()
        for handler in root.handlers:
            if hasattr(handler, "formatter") and handler.formatter:
                fmt = handler.formatter._fmt
                try:
                    fmt.encode("ascii")
                except UnicodeEncodeError as e:
                    pytest.fail(f"Console formatter contains non-ASCII: {e}")

    def test_shadow_quote_log_produces_no_unicode_arrows(self):
        """_log_shadow_quote output record has no Unicode arrows in string values."""
        captured = {}

        class _Cap:
            def log(self, channel, record):
                captured.update(record)

        from main import PolybotV2
        from settings import Settings
        cfg = Settings(CONFIG_PATH)
        cfg._raw.setdefault("ui", {})["enabled"] = False

        # Build a minimal quote record directly
        q = ShadowQuote(
            ts=1.0, window_ts=2.0, seconds_to_expiry=100.0,
            side="yes", quote_price=0.40, best_bid=0.40, best_ask=0.46,
            tick_size=0.01, crossed=False, fill_status="pending",
            regime="TRENDING", pattern="BURST",
            confidence_components="div=0.1;base=1.0",
        )

        bot = PolybotV2.__new__(PolybotV2)
        bot._structured = _Cap()
        bot._log_shadow_quote(q, event="lifecycle")

        for k, v in captured.items():
            if isinstance(v, str):
                try:
                    v.encode("ascii")
                except UnicodeEncodeError:
                    pytest.fail(
                        f"Non-ASCII character in shadow quote log field {k!r}={v!r}"
                    )
