"""
tests/test_trade_zones.py

Trade eligibility zone tests for Patch 3.

Separates three visibility levels:
  1. "Outside research zone"     — signal engine extreme_zone gate fails
  2. "Inside research zone, outside trade zone" — signal ok, lane rejects fill
  3. "Trade-eligible"            — signal ok AND price within lane trade zone

Maker trade zone: [0.83, 0.92] (enforced via quote bucket INELIGIBLE)
Taker trade zone: [0.80, 0.92] (enforced via TakerLane._zone_low/_zone_high)
"""
import pytest
from execution.maker_lane import MakerLane, FILL_GRADE_NA
from execution.taker_lane import TakerLane
from logger.summary import WindowLog


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _maker_config(zone_low=0.83, zone_high=0.92):
    return {
        "sizing": {"min_shares": 5, "fixed_shares_v1": 5, "initial_bankroll": 30.0},
        "quote_buckets": {"B1": [0.83, 0.86], "B2": [0.87, 0.90], "B3": [0.91, 0.92]},
        "trade_zones": {
            "maker_trade_zone_low": zone_low,
            "maker_trade_zone_high": zone_high,
        },
    }


def _taker_config(zone_low=0.80, zone_high=0.92):
    return {
        "fees": {"taker_fee_C": 0.02, "assumed_slippage_bps": 0},
        "trade_zones": {
            "taker_trade_zone_low": zone_low,
            "taker_trade_zone_high": zone_high,
        },
    }


# ---------------------------------------------------------------------------
# Maker trade zone (0.83–0.92 via quote buckets)
# ---------------------------------------------------------------------------

class TestMakerTradeZone:
    def test_maker_rejects_0_82_below_low_bound(self):
        """0.82 < maker_trade_zone_low=0.83 → INELIGIBLE bucket → no fill."""
        maker = MakerLane(_maker_config())
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.82, bankroll=30.0,
        )
        assert result.quote_bucket == "INELIGIBLE"
        assert result.filled is False
        assert result.fill_realism_grade == FILL_GRADE_NA

    def test_maker_rejects_0_93_above_high_bound(self):
        """0.93 > maker_trade_zone_high=0.92 → INELIGIBLE."""
        maker = MakerLane(_maker_config())
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.93, bankroll=30.0,
        )
        assert result.quote_bucket == "INELIGIBLE"

    def test_maker_accepts_0_83_at_low_boundary(self):
        """0.83 == maker_trade_zone_low → B1 bucket → eligible."""
        maker = MakerLane(_maker_config())
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.83, bankroll=30.0,
        )
        assert result.quote_bucket == "B1"
        assert result.quote_bucket != "INELIGIBLE"

    def test_maker_accepts_0_85_in_b1(self):
        """0.85 is in B1 [0.83, 0.86] → eligible."""
        maker = MakerLane(_maker_config())
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.85, bankroll=30.0,
        )
        assert result.quote_bucket == "B1"

    def test_maker_accepts_0_87_in_b2(self):
        maker = MakerLane(_maker_config())
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
        )
        assert result.quote_bucket == "B2"

    def test_maker_accepts_0_92_at_high_boundary(self):
        """0.92 == B3 high → eligible."""
        maker = MakerLane(_maker_config())
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.92, bankroll=30.0,
        )
        assert result.quote_bucket == "B3"

    def test_maker_zone_low_stored_from_config(self):
        maker = MakerLane(_maker_config(zone_low=0.83))
        assert maker._zone_low == 0.83

    def test_maker_zone_high_stored_from_config(self):
        maker = MakerLane(_maker_config(zone_high=0.92))
        assert maker._zone_high == 0.92

    def test_maker_zone_defaults_without_trade_zones_config(self):
        """Maker without trade_zones config defaults to 0.83/0.92."""
        maker = MakerLane({"sizing": {"min_shares": 5, "fixed_shares_v1": 5, "initial_bankroll": 30.0}})
        assert maker._zone_low == pytest.approx(0.83)
        assert maker._zone_high == pytest.approx(0.92)


# ---------------------------------------------------------------------------
# Taker trade zone (0.80–0.92 via explicit zone check)
# ---------------------------------------------------------------------------

class TestTakerTradeZone:
    def test_taker_rejects_0_79_below_low_bound(self):
        """0.79 < taker_trade_zone_low=0.80 → fill blocked, zone_eligible=False."""
        taker = TakerLane(_taker_config())
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.79, bankroll=30.0,
        )
        assert result.filled is False
        assert result.zone_eligible is False
        assert "taker_zone" in (result.rejection_reason or "")

    def test_taker_accepts_0_80_at_low_boundary(self):
        """0.80 == taker_trade_zone_low → within zone → fill proceeds."""
        taker = TakerLane(_taker_config())
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.80, bankroll=30.0,
        )
        assert result.filled is True
        assert result.zone_eligible is True

    def test_taker_accepts_0_81_inside_zone(self):
        """
        Key test: taker zone low is 0.80, so 0.81 must be accepted.
        This is NARROWER than the extreme_zone (0.10–0.90) but 0.81 is
        inside the taker trade zone.
        """
        taker = TakerLane(_taker_config(zone_low=0.80))
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.81, bankroll=30.0,
        )
        assert result.filled is True
        assert result.zone_eligible is True

    def test_taker_rejects_0_93_above_high_bound(self):
        taker = TakerLane(_taker_config())
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.93, bankroll=30.0,
        )
        assert result.filled is False
        assert result.zone_eligible is False
        assert "taker_zone" in (result.rejection_reason or "")

    def test_taker_accepts_0_92_at_high_boundary(self):
        taker = TakerLane(_taker_config())
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.92, bankroll=30.0,
        )
        assert result.filled is True
        assert result.zone_eligible is True

    def test_taker_zone_rejection_reason_format(self):
        """Rejection reason must include price and zone bounds for log clarity."""
        taker = TakerLane(_taker_config(zone_low=0.80, zone_high=0.92))
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.79, bankroll=30.0,
        )
        reason = result.rejection_reason or ""
        assert "taker_zone" in reason
        assert "0.79" in reason or "0.7" in reason   # price should appear

    def test_taker_zone_eligible_defaults_true(self):
        from execution.taker_lane import TakerResult
        r = TakerResult(window_open_ts=1, slug="s", signal_direction="YES", intended_price=0.87)
        assert r.zone_eligible is True

    def test_taker_result_zone_eligible_default_is_true(self):
        from execution.taker_lane import TakerResult
        r = TakerResult(window_open_ts=1, slug="s", signal_direction="YES", intended_price=0.87)
        assert r.zone_eligible is True

    def test_taker_zone_defaults_without_trade_zones_config(self):
        """Taker without trade_zones config defaults to 0.80/0.92."""
        taker = TakerLane({"fees": {"taker_fee_C": 0.02, "assumed_slippage_bps": 0}})
        assert taker._zone_low == pytest.approx(0.80)
        assert taker._zone_high == pytest.approx(0.92)

    def test_taker_zone_configurable(self):
        """Zone bounds can be set via config."""
        taker = TakerLane(_taker_config(zone_low=0.75, zone_high=0.95))
        assert taker._zone_low == pytest.approx(0.75)
        assert taker._zone_high == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Taker zone vs maker zone differ (maker starts higher)
# ---------------------------------------------------------------------------

class TestZoneDifferenceBetweenLanes:
    def test_taker_accepts_price_that_maker_rejects_at_0_80(self):
        """
        0.80 is inside the taker zone but below the maker zone (0.83).
        - Taker: zone_eligible=True, fills
        - Maker: INELIGIBLE bucket (quote_bucket enforcement)
        """
        maker = MakerLane(_maker_config())
        taker = TakerLane(_taker_config())

        maker_result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.80, bankroll=30.0,
        )
        taker_result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.80, bankroll=30.0,
        )

        assert maker_result.quote_bucket == "INELIGIBLE"
        assert taker_result.zone_eligible is True
        assert taker_result.filled is True

    def test_taker_also_accepts_0_82(self):
        """0.82 is inside taker zone [0.80, 0.92] but outside maker buckets."""
        taker = TakerLane(_taker_config())
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.82, bankroll=30.0,
        )
        assert result.filled is True
        assert result.zone_eligible is True


# ---------------------------------------------------------------------------
# WindowLog trade zone fields
# ---------------------------------------------------------------------------

class TestWindowLogTradeZoneFields:
    def test_windowlog_has_maker_trade_zone_eligible(self):
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        assert hasattr(wl, "maker_trade_zone_eligible")
        assert wl.maker_trade_zone_eligible is True  # default

    def test_windowlog_has_taker_trade_zone_eligible(self):
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        assert hasattr(wl, "taker_trade_zone_eligible")
        assert wl.taker_trade_zone_eligible is True  # default

    def test_windowlog_zone_fields_can_be_false(self):
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        wl.maker_trade_zone_eligible = False
        wl.taker_trade_zone_eligible = False
        assert wl.maker_trade_zone_eligible is False
        assert wl.taker_trade_zone_eligible is False

    def test_research_zone_vs_trade_zone_separation(self):
        """
        Demonstrate three-level visibility:
          - Research zone (extreme_zone): 0.10–0.90 (signal engine)
          - Maker trade zone: 0.83–0.92
          - Taker trade zone: 0.80–0.92
        A price of 0.79 is inside the research zone but outside BOTH trade zones.
        """
        maker = MakerLane(_maker_config())
        taker = TakerLane(_taker_config())

        # p=0.79: inside extreme_zone (0.10, 0.90), outside both trade zones
        maker_result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.79, bankroll=30.0,
        )
        taker_result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.79, bankroll=30.0,
        )

        # Inside research zone (signal would be evaluated)
        # p=0.79 is between extreme_zone_low=0.10 and extreme_zone_high=0.90
        # but below maker (0.83) and taker (0.80) trade zones

        assert maker_result.quote_bucket == "INELIGIBLE"    # outside maker zone
        assert taker_result.zone_eligible is False           # outside taker zone
        assert taker_result.filled is False
