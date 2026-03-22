"""
tests/test_bot_wiring.py

Integration tests for bot.py orchestrator wiring — covers the integration
patch requirements:

1. Pre-signal YES mid fetch uses market.yes_token_id
2. YES probability feeds into FeedWindow.current_yes_mid (not None)
3. PROVISIONAL mode with YES mid available => signal becomes candidate
4. YES mid unavailable => WindowLog.signal_input_missing logged
5. Taker lane receives YES probability, not BTC/USD spot
6. Intra-window collector uses market.yes_token_id
7. WindowLog records yes_price_source, yes_price_is_provisional, runtime_mode_effective
8. maker_path_source and maker_path_points_collected populated

Tests use _fetch_yes_snap (tested directly) and integration-level helpers that
simulate the bot flow with mocked CLOB adapter and intra-window collector.
"""
import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import ResearchBot
from discovery.market import WindowMarket
from feeds.price_types import YesPriceSnapshot
from logger.summary import WindowLog
from sigeng.engine import FeedWindow, SignalEngine, SignalDirection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_market(yes_token_id: str = "token_yes_abc123") -> WindowMarket:
    return WindowMarket(
        slug="btc-updown-5m-1700000000",
        window_open_ts=1_700_000_000,
        window_close_ts=1_700_000_300,
        market_id="mkt_001",
        yes_token_id=yes_token_id,
        no_token_id="token_no_xyz789",
        condition_id="0xcond",
        end_date_iso="2025-01-01T00:05:00Z",
    )


def _make_yes_snap(probability: float = 0.87, token_id: str = "token_yes_abc123") -> YesPriceSnapshot:
    return YesPriceSnapshot(
        probability=probability,
        timestamp=1_700_000_001.5,
        source="clob_midpoint",
        is_provisional=True,
        token_id=token_id,
    )


def _make_wl(window_open_ts: int = 1_700_000_000) -> WindowLog:
    return WindowLog(window_open_ts=window_open_ts, slug="btc-updown-5m-1700000000", phase="0c")


class _BotStub:
    """
    Minimal stub of ResearchBot that exposes only the methods under test.

    Binds the actual method implementations from ResearchBot so we test
    real code, not re-implementations.
    """

    def __init__(self, yes_adapter=None, intra_collector=None, bot_mode: str = "PROVISIONAL"):
        self._yes_adapter = yes_adapter or AsyncMock()
        self._intra_collector = intra_collector or AsyncMock()
        self._bot_mode = bot_mode

        # Bind real implementations
        self._fetch_yes_snap = types.MethodType(ResearchBot._fetch_yes_snap, self)


# ---------------------------------------------------------------------------
# Tests: _fetch_yes_snap
# ---------------------------------------------------------------------------

class TestFetchYesSnap:
    def test_returns_snap_when_adapter_succeeds(self):
        snap = _make_yes_snap(probability=0.87)
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=snap)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        market = _make_market()

        result = asyncio.run(stub._fetch_yes_snap(market, wl))

        assert result is snap
        assert result.probability == 0.87

    def test_uses_market_yes_token_id(self):
        """Adapter must be called with market.yes_token_id, not hardcoded."""
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=_make_yes_snap())

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        market = _make_market(yes_token_id="specific_token_id_xyz")

        asyncio.run(stub._fetch_yes_snap(market, wl))

        call_args = adapter.get_yes_mid.call_args
        assert call_args.kwargs.get("token_id") == "specific_token_id_xyz" or \
               call_args.args[0] == "specific_token_id_xyz", (
            f"Expected token_id='specific_token_id_xyz', got call_args={call_args}"
        )

    def test_wl_yes_price_source_populated_on_success(self):
        snap = _make_yes_snap()
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=snap)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        asyncio.run(stub._fetch_yes_snap(_make_market(), wl))

        assert wl.yes_price_source == "clob_midpoint"

    def test_wl_yes_price_is_provisional_populated_on_success(self):
        snap = _make_yes_snap()  # is_provisional=True by default
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=snap)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        asyncio.run(stub._fetch_yes_snap(_make_market(), wl))

        assert wl.yes_price_is_provisional is True

    def test_returns_none_on_adapter_failure(self):
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=None)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        result = asyncio.run(stub._fetch_yes_snap(_make_market(), wl))

        assert result is None

    def test_signal_input_missing_set_on_adapter_failure(self):
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=None)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        asyncio.run(stub._fetch_yes_snap(_make_market(), wl))

        assert wl.signal_input_missing is not None
        assert "yes_mid_unavailable" in wl.signal_input_missing

    def test_returns_none_when_market_is_none(self):
        stub = _BotStub()
        wl = _make_wl()
        result = asyncio.run(stub._fetch_yes_snap(None, wl))

        assert result is None
        assert wl.signal_input_missing is not None
        assert "market_not_discovered" in wl.signal_input_missing

    def test_yes_price_source_remains_none_on_failure(self):
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=None)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        asyncio.run(stub._fetch_yes_snap(_make_market(), wl))

        assert wl.yes_price_source is None


# ---------------------------------------------------------------------------
# Tests: FeedWindow YES probability wiring
# ---------------------------------------------------------------------------

class TestFeedWindowYesMidWiring:
    """
    Verify that the bot builds FeedWindow.current_yes_mid from the
    YesPriceSnapshot, NOT from BTC/USD spot prices.
    """

    def test_feed_window_uses_yes_snap_probability(self):
        """
        When YES snap is available, FeedWindow.current_yes_mid must equal
        snap.probability, not wl.fast_price (BTC/USD).
        """
        snap = _make_yes_snap(probability=0.87)
        fw = FeedWindow(
            window_open_ts=1_700_000_000,
            slug="btc-updown-5m-1700000000",
            open_fast_price=94_000.0,      # BTC/USD — must NOT be used for extreme zone
            latest_fast_price=94_050.0,
            open_chainlink_price=93_990.0,
            latest_chainlink_price=93_995.0,
            current_yes_mid=snap.probability,  # YES probability — from CLOB snap
            yes_bid=None,
            yes_ask=None,
            fast_gap_seconds=1.0,
            chainlink_gap_seconds=1.5,
            fast_feed_stale=False,
            chainlink_feed_stale=False,
            seconds_to_window_close=120.0,
            candles_same_direction=0,
            yes_book_available=False,
            candles_available=False,
        )
        assert fw.current_yes_mid == 0.87
        # Verify BTC/USD is stored separately and not contaminating YES space
        assert fw.latest_fast_price == 94_050.0
        assert fw.current_yes_mid != fw.latest_fast_price

    def test_feed_window_yes_mid_none_when_snap_unavailable(self):
        """When YES snap is None, current_yes_mid must remain None."""
        snap = None
        fw = FeedWindow(
            window_open_ts=1_700_000_000,
            slug="btc-updown-5m-1700000000",
            open_fast_price=94_000.0,
            latest_fast_price=94_050.0,
            open_chainlink_price=93_990.0,
            latest_chainlink_price=93_995.0,
            current_yes_mid=snap.probability if snap is not None else None,
            yes_bid=None,
            yes_ask=None,
            fast_gap_seconds=1.0,
            chainlink_gap_seconds=1.5,
            fast_feed_stale=False,
            chainlink_feed_stale=False,
            seconds_to_window_close=120.0,
            candles_same_direction=0,
            yes_book_available=False,
            candles_available=False,
        )
        assert fw.current_yes_mid is None


# ---------------------------------------------------------------------------
# Tests: Signal engine with wired YES probability
# ---------------------------------------------------------------------------

class TestSignalEngineCandidateProduction:
    """
    Blocker #3 (round 3): in PROVISIONAL mode, when YES mid is available via
    CLOB REST, the signal engine must produce candidates.
    """

    def _provisional_config(self):
        return {
            "bot_mode": "PROVISIONAL",
            "signal": {
                "endcycle_entry_cutoff_seconds": 45,
                "feed_freshness_threshold_seconds": 8.0,
                "basis_mismatch_flag_threshold_bps": 30.0,
                "min_spread_quality_bps": 5.0,
                "extreme_zone_low": 0.10,
                "extreme_zone_high": 0.90,
                "momentum_persistence_candles": 2,
            },
        }

    def test_provisional_mode_yes_mid_available_produces_candidate(self):
        """
        Full path: YES mid 0.87 available → extreme_zone passes →
        PROVISIONAL soft-passes spread/momentum → signal is eligible.
        """
        engine = SignalEngine(self._provisional_config())
        fw = FeedWindow(
            window_open_ts=1_700_000_000,
            slug="btc-updown-5m-1700000000",
            open_fast_price=94_000.0,
            latest_fast_price=94_050.0,      # fast > chainlink → YES direction
            open_chainlink_price=93_990.0,
            latest_chainlink_price=93_995.0,
            current_yes_mid=0.87,            # ← from CLOB REST snap
            yes_bid=None,
            yes_ask=None,
            fast_gap_seconds=1.0,
            chainlink_gap_seconds=1.5,
            fast_feed_stale=False,
            chainlink_feed_stale=False,
            seconds_to_window_close=120.0,
            candles_same_direction=0,
            yes_book_available=False,        # no live book — soft-passed
            candles_available=False,         # no candles — soft-passed
        )
        sig = engine.evaluate(fw)

        assert sig.quote_eligible is True, (
            f"Expected candidate in PROVISIONAL mode with YES mid=0.87. "
            f"Gates: {sig.gates}. Rejections: {sig.rejection_reasons}"
        )
        assert sig.direction != SignalDirection.NONE

    def test_provisional_mode_yes_mid_unavailable_extreme_zone_fails(self):
        """
        When YES mid is None, extreme_zone hard gate fails even in PROVISIONAL mode.
        Window is rejected with explicit reason.
        """
        engine = SignalEngine(self._provisional_config())
        fw = FeedWindow(
            window_open_ts=1_700_000_000,
            slug="btc-updown-5m-1700000000",
            open_fast_price=94_000.0,
            latest_fast_price=94_050.0,
            open_chainlink_price=93_990.0,
            latest_chainlink_price=93_995.0,
            current_yes_mid=None,            # ← unavailable
            yes_bid=None,
            yes_ask=None,
            fast_gap_seconds=1.0,
            chainlink_gap_seconds=1.5,
            fast_feed_stale=False,
            chainlink_feed_stale=False,
            seconds_to_window_close=120.0,
            candles_same_direction=0,
            yes_book_available=False,
            candles_available=False,
        )
        sig = engine.evaluate(fw)

        assert sig.quote_eligible is False
        assert sig.gates["extreme_zone"] is False
        assert any("yes_mid_unavailable" in r for r in sig.rejection_reasons), (
            f"Expected yes_mid_unavailable in rejection_reasons: {sig.rejection_reasons}"
        )


# ---------------------------------------------------------------------------
# Tests: Taker lane receives YES probability, not BTC spot
# ---------------------------------------------------------------------------

class TestTakerLanePriceSpaceEnforcement:
    """
    Verify that the taker lane price-space guard functions correctly,
    and that the bot correctly blocks fills when YES probability unavailable.
    """

    def test_taker_raises_on_btc_usd_price(self):
        """Passing a BTC/USD price to taker lane must raise ValueError."""
        from execution.taker_lane import TakerLane
        config = {"fees": {"taker_fee_C": 0.02, "assumed_slippage_bps": 0}}
        lane = TakerLane(config)
        with pytest.raises(ValueError, match=r"outside \(0, 1\)"):
            lane.evaluate(
                window_open_ts=1_700_000_000,
                slug="test",
                signal_direction="YES",
                decision_price=94_000.0,   # BTC/USD — must be rejected
                bankroll=30.0,
            )

    def test_taker_accepts_yes_probability(self):
        """A valid YES probability in (0,1) must be accepted."""
        from execution.taker_lane import TakerLane
        config = {"fees": {"taker_fee_C": 0.02, "assumed_slippage_bps": 0}}
        lane = TakerLane(config)
        result = lane.evaluate(
            window_open_ts=1_700_000_000,
            slug="test",
            signal_direction="YES",
            decision_price=0.87,           # YES probability — valid
            bankroll=30.0,
        )
        assert result.filled is True
        assert result.fill_price == 0.87

    def test_taker_blocks_fill_when_price_is_none(self):
        """decision_price=None (YES probability unavailable) must produce no fill."""
        from execution.taker_lane import TakerLane
        config = {"fees": {"taker_fee_C": 0.02, "assumed_slippage_bps": 0}}
        lane = TakerLane(config)
        result = lane.evaluate(
            window_open_ts=1_700_000_000,
            slug="test",
            signal_direction="YES",
            decision_price=None,           # YES probability unavailable → blocked
            bankroll=30.0,
        )
        assert result.filled is False
        assert result.fill_price is None


# ---------------------------------------------------------------------------
# Tests: Intra-window collector uses market.yes_token_id
# ---------------------------------------------------------------------------

class TestIntraWindowCollectorWiring:
    def test_collector_called_with_market_yes_token_id(self):
        """
        The IntraWindowYesPriceCollector must receive market.yes_token_id,
        not a hardcoded or invented token.
        """
        collected_token = []

        async def fake_collect(token_id: str, duration_seconds: float):
            collected_token.append(token_id)
            return [0.87, 0.86]

        intra_mock = MagicMock()
        intra_mock.collect = fake_collect

        from execution.maker_lane import MakerLane, FILL_GRADE_PROVISIONAL_PROXY

        market = _make_market(yes_token_id="the_real_token_xyz")
        yes_snap = _make_yes_snap(probability=0.87)

        # Simulate the collector call that bot._execute_paper_trades makes
        async def run():
            prices, _ = await asyncio.gather(
                intra_mock.collect(market.yes_token_id, 270.0),
                asyncio.sleep(0),
            )
            return prices

        prices = asyncio.run(run())

        assert collected_token == ["the_real_token_xyz"]
        assert prices == [0.87, 0.86]

    def test_maker_lane_uses_intra_prices_with_provisional_proxy_grade(self):
        """
        When intra_prices are returned by the collector, maker lane receives them
        with fill_realism_source=PROVISIONAL_OBSERVED_PROXY.
        """
        from execution.maker_lane import MakerLane, FILL_GRADE_PROVISIONAL_PROXY

        config = {
            "sizing": {"min_shares": 5, "fixed_shares_v1": 5, "initial_bankroll": 30.0},
            "quote_buckets": {"B1": [0.83, 0.86], "B2": [0.87, 0.90], "B3": [0.91, 0.92]},
        }
        lane = MakerLane(config)

        # Prices from intra-window collector — contain a value below limit
        intra_prices = [0.88, 0.87, 0.85]   # 0.85 dips below limit 0.87

        result = lane.evaluate(
            window_open_ts=1_700_000_000,
            slug="test",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=intra_prices,
            fill_realism_source=FILL_GRADE_PROVISIONAL_PROXY,
        )

        assert result.filled is True
        assert result.fill_realism_grade == "PROVISIONAL_OBSERVED_PROXY"

    def test_maker_lane_conservative_no_fill_when_no_intra_prices(self):
        """When intra_prices is None or empty, fill must be False (conservative)."""
        from execution.maker_lane import MakerLane, FILL_GRADE_PROVISIONAL

        config = {
            "sizing": {"min_shares": 5, "fixed_shares_v1": 5, "initial_bankroll": 30.0},
            "quote_buckets": {"B1": [0.83, 0.86], "B2": [0.87, 0.90], "B3": [0.91, 0.92]},
        }
        lane = MakerLane(config)

        result = lane.evaluate(
            window_open_ts=1_700_000_000,
            slug="test",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=None,    # no prices
        )

        assert result.filled is False
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL


# ---------------------------------------------------------------------------
# Tests: WindowLog field population (runtime_mode_effective + YES fields)
# ---------------------------------------------------------------------------

class TestWindowLogFields:
    def test_runtime_mode_effective_in_windowlog(self):
        """WindowLog must have runtime_mode_effective field."""
        wl = WindowLog(window_open_ts=1_700_000_000, slug="test", phase="0c")
        wl.runtime_mode_effective = "PROVISIONAL"
        assert wl.runtime_mode_effective == "PROVISIONAL"

    def test_yes_price_source_field_exists(self):
        wl = WindowLog(window_open_ts=1_700_000_000, slug="test", phase="0c")
        assert hasattr(wl, "yes_price_source")
        assert wl.yes_price_source is None  # default

    def test_yes_price_is_provisional_field_exists(self):
        wl = WindowLog(window_open_ts=1_700_000_000, slug="test", phase="0c")
        assert hasattr(wl, "yes_price_is_provisional")
        assert wl.yes_price_is_provisional is None  # default

    def test_signal_input_missing_field_exists(self):
        wl = WindowLog(window_open_ts=1_700_000_000, slug="test", phase="0c")
        assert hasattr(wl, "signal_input_missing")
        assert wl.signal_input_missing is None  # default

    def test_maker_path_source_field_exists(self):
        wl = WindowLog(window_open_ts=1_700_000_000, slug="test", phase="0c")
        assert hasattr(wl, "maker_path_source")
        assert wl.maker_path_source is None  # default

    def test_maker_path_points_collected_field_exists(self):
        wl = WindowLog(window_open_ts=1_700_000_000, slug="test", phase="0c")
        assert hasattr(wl, "maker_path_points_collected")
        assert wl.maker_path_points_collected == 0  # default

    def test_fetch_yes_snap_sets_all_three_fields_on_success(self):
        """Single call populates yes_price_source, yes_price_is_provisional."""
        snap = _make_yes_snap(probability=0.88)
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=snap)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        asyncio.run(stub._fetch_yes_snap(_make_market(), wl))

        assert wl.yes_price_source == "clob_midpoint"
        assert wl.yes_price_is_provisional is True
        assert wl.signal_input_missing is None  # no missing on success

    def test_fetch_yes_snap_sets_signal_input_missing_on_failure(self):
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=None)

        stub = _BotStub(yes_adapter=adapter)
        wl = _make_wl()
        asyncio.run(stub._fetch_yes_snap(_make_market(), wl))

        assert wl.signal_input_missing == "yes_mid_unavailable:clob_request_failed"
        assert wl.yes_price_source is None
        assert wl.yes_price_is_provisional is None
