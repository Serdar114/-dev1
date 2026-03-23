"""
bot.py — Main research bot orchestrator.

Architecture
------------
Dual-lane paper trading:
  - MAKER lane: primary evaluation (passive limit orders, zero fee)
  - TAKER lane: benchmark / control (market orders, taker fee applied)

Both lanes use the SAME signal, enabling apples-to-apples fee comparison.

Feed pair (mandatory, both logged every active window):
  - FAST  : Polymarket RTDS `crypto_prices`       (Binance-like)
  - CHAINLINK: Polymarket RTDS `crypto_prices_chainlink` (reference/settlement)

Settlement truth
----------------
Settlement uses CHAINLINK_PROXY by default (Chainlink feed close price).
Fast feed is used as FAST_PROXY fallback if Chainlink is unavailable at close.
Settlement source is logged in every window record.

TODO: Replace proxy settlement with the official Polymarket settlement endpoint
      when confirmed.  Until then, all outcomes are estimates.

Daily caps (enforced in code)
------------------------------
  max_candidates_per_day : 50   (stop evaluating after this count)
  max_entries_per_day    : 5    (hard cap on paper fills)
  one_position_at_a_time : True (no overlapping fills)

Phases
------
  0a : Infrastructure validation  — feeds + discovery only, no signals
  0b : Signal validation           — signal engine on, no fills
  0c : Paper trading validation    — full dual-lane paper execution
  1  : Live-candidate readiness    — paper with tighter kill thresholds

NOTE: This codebase contains NO live trading code.
      See README.md for paper-to-live gap warnings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import yaml

from discovery.market import MarketDiscovery, WindowMarket, current_window_boundary, next_window_boundary
from execution.maker_lane import MakerLane, FILL_GRADE_OBSERVED
from execution.taker_lane import TakerLane, SRC_CLOB_ASK_AT_SIGNAL, SRC_UNAVAILABLE
from feeds.chainlink_feed import ChainlinkFeedAdapter
from feeds.fast_feed import FastFeedAdapter
from feeds.intra_window_collector import IntraWindowYesPriceCollector, CollectionResult
from feeds.price_types import YesPriceSnapshot
from feeds.yes_price_adapter import CLOBYesPriceAdapter
from logger.summary import SummaryReporter, WindowLog
from risk.sizing import PositionSizer
from risk.caps import DailyCaps
from sigeng.engine import FeedWindow, SignalEngine, SignalDirection
from validator.kill_conditions import KillAction, KillConditionValidator, SessionStats

logger = logging.getLogger(__name__)

# Settlement source labels
SETTLE_CHAINLINK_PROXY = "CHAINLINK_PROXY"
SETTLE_FAST_PROXY = "FAST_PROXY"
SETTLE_UNAVAILABLE = "UNAVAILABLE"


class SessionKillError(Exception):
    """Raised when a KILL condition triggers session termination."""


def load_config(config_dir: str = "config") -> dict:
    """Load and merge settings.yaml + kill_conditions.yaml."""
    with open(os.path.join(config_dir, "settings.yaml")) as f:
        config = yaml.safe_load(f)
    with open(os.path.join(config_dir, "kill_conditions.yaml")) as f:
        kill_cfg = yaml.safe_load(f)
    config["kill_conditions_cfg"] = kill_cfg
    return config


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_dir = log_cfg.get("log_dir", "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = int(time.time())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(log_dir, f"bot_{ts}.log")),
        ],
    )


class ResearchBot:
    """
    Async research bot.  Runs one 5-minute window iteration at a time.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._phase = config.get("phase", "0c")
        # STRICT: missing data = hard gate fail. PROVISIONAL: soft-pass with label.
        self._bot_mode = config.get("bot_mode", "STRICT")

        feed_cfg = config["feeds"]
        self._fast_feed = FastFeedAdapter(
            rtds_host=feed_cfg["rtds_host"],
            symbol="BTC-USD",
            stale_threshold_seconds=feed_cfg["stale_threshold_seconds"],
        )
        self._chainlink_feed = ChainlinkFeedAdapter(
            rtds_host=feed_cfg["rtds_host"],
            symbol="BTC-USD",
            stale_threshold_seconds=feed_cfg["stale_threshold_seconds"],
        )

        self._discovery = MarketDiscovery(config["discovery"])
        self._signal_engine = SignalEngine(config)
        self._maker_lane = MakerLane(config)
        self._taker_lane = TakerLane(config)
        self._sizer = PositionSizer(config["sizing"]["initial_bankroll"])
        self._caps = DailyCaps(config.get("daily_caps", {}))
        self._reporter = SummaryReporter(config, self._phase)
        self._validator = KillConditionValidator(config["kill_conditions_cfg"])

        # YES probability source (provisional REST polling until WebSocket live)
        clob_url = config.get("discovery", {}).get("clob_base_url", "https://clob.polymarket.com")
        self._yes_adapter = CLOBYesPriceAdapter(clob_base_url=clob_url)

        # Poll interval: config-driven so it can be tuned relative to decision window.
        # Default 15 s gives ~2–3 points in the ~40 s post-decision collection window,
        # which satisfies the PROVISIONAL_MULTI_POINT (evaluable) threshold.
        # A 60 s interval would yield 0–1 points → non-evaluable in every window.
        maker_cfg = config.get("maker", {})
        self._maker_poll_interval = float(
            maker_cfg.get("provisional_yes_poll_interval_seconds", 15.0)
        )
        self._intra_collector = IntraWindowYesPriceCollector(
            self._yes_adapter, poll_interval_s=self._maker_poll_interval
        )

        # Decision window parameters (mirrors SignalEngine for bot-level timing).
        sig_cfg = config.get("signal", {})
        self._dw_start = sig_cfg.get(
            "decision_window_start_seconds_to_close",
            sig_cfg.get("endcycle_entry_cutoff_seconds", 45),
        )
        self._dw_end = sig_cfg.get("decision_window_end_seconds_to_close", 10)

    async def run(self) -> None:
        """Main loop: start feeds, then iterate over 5m windows until killed."""
        logger.info("[bot] Starting research bot — Phase %s", self._phase)
        logger.info("[bot] PAPER TRADING ONLY — no live orders will be placed")

        await self._fast_feed.start()
        await self._chainlink_feed.start()

        logger.info("[bot] Waiting 5s for feed warm-up...")
        await asyncio.sleep(5)

        if self._phase == "0a":
            await self._run_infra_validation()
            return

        try:
            while True:
                await self._wait_for_next_window()
                await self._run_window()
        except SessionKillError as e:
            logger.critical("[bot] SESSION KILLED: %s", e)
            self._reporter.print_final_summary()
        except KeyboardInterrupt:
            logger.info("[bot] Interrupted by user")
            self._reporter.print_final_summary()
        finally:
            await self._fast_feed.stop()
            await self._chainlink_feed.stop()
            logger.info("[bot] Feeds stopped")

    # ------------------------------------------------------------------
    # Phase 0a: Infrastructure validation
    # ------------------------------------------------------------------

    async def _run_infra_validation(self) -> None:
        logger.info("[bot/0a] Infrastructure validation — running 3 windows")
        for _ in range(3):
            boundary = current_window_boundary()
            wl = WindowLog(window_open_ts=boundary, slug="", phase=self._phase)
            self._capture_feeds(wl)

            start = time.time()
            market = await self._discovery.get_market_for_window(boundary)
            wl.discovery_ok = market is not None
            wl.discovery_latency_ms = (time.time() - start) * 1000
            wl.slug = market.slug if market else f"btc-updown-5m-{boundary}"

            self._log_feed_pair(wl, boundary)
            self._reporter.record_window(wl)
            await asyncio.sleep(300)

        self._reporter.print_final_summary()

    # ------------------------------------------------------------------
    # Window loop
    # ------------------------------------------------------------------

    async def _wait_for_next_window(self) -> None:
        now = time.time()
        nxt = next_window_boundary(now)
        wait = nxt - now
        if wait > 0:
            logger.debug("[bot] Sleeping %.1fs until next window boundary", wait)
            await asyncio.sleep(wait)

    async def _run_window(self) -> None:
        """
        Execute one full 5-minute window.

        Two-phase flow
        --------------
        PHASE A — Window open (T+0):
            1. Capture open-time feed snapshots (BTC/USD reference prices).
            2. Run market discovery concurrently.
            3. Log mandatory feed pair.
            4. Check daily candidate cap.
            → Wait until decision window opens (T - dw_start seconds before close).

        PHASE B — Decision time (T - dw_start):
            5. Capture fresh decision-time feed snapshots.
            6. Fetch YES probability (CLOB REST, at decision time).
            7. Build FeedWindow with both open and decision data.
            8. Evaluate signal engine.
            9. Paper execute (phases 0c+) and settle.

        The endcycle_timing gate validates that evaluation actually happened
        inside the decision window [dw_end, dw_start]. This is a HARD gate.
        Direction uses decision_fast_price vs open_chainlink_price to measure
        within-window BTC movement, not open-time basis.
        """
        window_open_ts = current_window_boundary()
        window_close_ts = window_open_ts + 300

        logger.info(
            "[bot] === Window open ts=%d mode=%s dw=[T-%d, T-%d] ===",
            window_open_ts, self._bot_mode, self._dw_start, self._dw_end,
        )

        # ---- Daily cap reset ----
        self._caps.reset_if_new_day()

        wl = WindowLog(window_open_ts=window_open_ts, slug="", phase=self._phase)
        wl.runtime_mode_effective = self._bot_mode

        # ================================================================
        # PHASE A: Window open (T+0) — open-price snapshot + discovery
        # ================================================================

        # ---- Step 1: Capture open-time feed snapshots ----
        open_capture_start = time.time()
        self._capture_feeds(wl)   # sets wl.fast_price, wl.chainlink_price (open-time)
        wl.open_price_capture_delay_seconds = time.time() - open_capture_start

        # ---- Step 2: Market discovery ----
        disc_start = time.time()
        market = await self._discovery.get_market_for_window(window_open_ts)
        wl.discovery_ok = market is not None
        wl.discovery_latency_ms = (time.time() - disc_start) * 1000
        wl.slug = market.slug if market else f"btc-updown-5m-{window_open_ts}"

        # ---- Step 3: Log both feeds (mandatory per spec §1) ----
        self._log_feed_pair(wl, window_open_ts)

        if self._phase == "0a":
            self._reporter.record_window(wl)
            return

        # ---- Step 4: Daily candidate cap check ----
        can_observe, obs_reason = self._caps.can_observe_candidate()
        if not can_observe:
            logger.info("[bot] Candidate cap blocked: %s", obs_reason)
            wl.signal_direction = "NONE"
            wl.signal_rejection_reasons = f"cap:{obs_reason}"
            caps_state = self._caps.snapshot()
            wl.caps_candidates_today = caps_state.candidates_today
            wl.caps_entries_today = caps_state.entries_today
            wl.caps_position_open = caps_state.position_open
            self._reporter.record_window(wl)
            return

        # ================================================================
        # WAIT: Sleep until decision window opens
        # ================================================================
        decision_wake_ts = window_close_ts - self._dw_start
        wait_secs = decision_wake_ts - time.time()
        if wait_secs > 0.5:
            logger.debug(
                "[bot] Waiting %.1fs for decision window (T-%ds before close)",
                wait_secs, self._dw_start,
            )
            await asyncio.sleep(wait_secs)

        # ================================================================
        # PHASE B: Decision time (T - dw_start) — fresh snapshot + signal
        # ================================================================

        # ---- Step 5: Capture decision-time feed snapshots ----
        decision_ts = time.time()
        decision_seconds_to_close = window_close_ts - decision_ts
        wl.decision_ts = decision_ts
        wl.decision_seconds_to_close = decision_seconds_to_close

        # Fresh BTC/USD prices at decision time
        decision_fast_snap = self._fast_feed.latest
        decision_cl_snap = self._chainlink_feed.latest
        wl.decision_fast_price = decision_fast_snap.price if decision_fast_snap else None
        wl.decision_chainlink_price = decision_cl_snap.price if decision_cl_snap else None

        # Decision-time feed freshness (used in FeedWindow, NOT the open-time values)
        decision_fast_gap = self._fast_feed.gap_seconds_now()
        decision_cl_gap = self._chainlink_feed.gap_seconds_now()
        decision_fast_stale = decision_fast_snap.is_stale if decision_fast_snap else True
        decision_cl_stale = decision_cl_snap.is_stale if decision_cl_snap else True

        # ---- Step 6: Fetch YES probability at decision time ----
        # MUST be fetched at decision time, not window open.
        # Uses market.yes_token_id from discovery (never hardcoded).
        decision_yes_snap = await self._fetch_yes_snap(market, wl)
        wl.decision_yes_mid = decision_yes_snap.probability if decision_yes_snap else None

        # ---- Step 7: Build FeedWindow with open and decision data ----
        fw = FeedWindow(
            window_open_ts=window_open_ts,
            slug=wl.slug,
            # Open-time BTC/USD (reference anchor; used in open_price_integrity gate)
            open_fast_price=wl.fast_price,
            open_chainlink_price=wl.chainlink_price,
            # Latest BTC/USD (decision-time; used in basis mismatch calculation)
            latest_fast_price=wl.decision_fast_price,
            latest_chainlink_price=wl.decision_chainlink_price,
            # Explicit decision-time prices (used for direction: was price movement bullish?)
            decision_fast_price=wl.decision_fast_price,
            decision_chainlink_price=wl.decision_chainlink_price,
            # YES probability at decision time (CLOB REST, provisional)
            # MUST NOT be BTC/USD price — guard is in YesPriceSnapshot + TakerLane
            current_yes_mid=wl.decision_yes_mid,
            yes_bid=None,           # REST midpoint has no bid; book not yet live
            yes_ask=None,           # REST midpoint has no ask; book not yet live
            # Feed freshness at DECISION time (not open time)
            fast_gap_seconds=decision_fast_gap,
            chainlink_gap_seconds=decision_cl_gap,
            fast_feed_stale=decision_fast_stale,
            chainlink_feed_stale=decision_cl_stale,
            # seconds_to_window_close at decision time — endcycle gate validates this
            seconds_to_window_close=decision_seconds_to_close,
            candles_same_direction=0,       # TODO: connect 1m candle tracker
            yes_book_available=False,       # REST midpoint does not constitute a live book
            candles_available=False,        # candle tracker not yet connected
        )

        # ---- Step 8: Signal engine ----
        sig = self._signal_engine.evaluate(fw)

        wl.basis_bps = sig.basis_bps
        wl.basis_mismatch_bps = sig.basis_mismatch_bps
        wl.basis_mismatch = sig.basis_mismatch
        wl.signal_direction = sig.direction.value
        wl.signal_eligible = sig.quote_eligible
        wl.gate_summary = sig.gate_summary()
        wl.signal_rejection_reasons = sig.rejection_summary()
        wl.current_yes_mid = sig.current_yes_mid

        # Record caps state
        caps_state = self._caps.snapshot()
        wl.caps_candidates_today = caps_state.candidates_today
        wl.caps_entries_today = caps_state.entries_today
        wl.caps_position_open = caps_state.position_open

        # Record candidate if signal was emitted (any direction)
        if sig.direction != SignalDirection.NONE:
            self._caps.record_candidate()

        if self._phase == "0b":
            self._reporter.record_window(wl)
            self._check_kill(wl)
            return

        # ---- Step 9: Paper execution (phases 0c and 1) ----
        if market is not None and sig.direction != SignalDirection.NONE:
            await self._execute_paper_trades(
                wl, sig, market, decision_yes_snap, window_open_ts, window_close_ts
            )

        self._reporter.record_window(wl)
        self._check_kill(wl)

    async def _fetch_yes_snap(
        self,
        market: Optional[WindowMarket],
        wl: WindowLog,
    ) -> Optional[YesPriceSnapshot]:
        """
        Fetch YES probability from CLOB REST API using market.yes_token_id.

        Sets wl.yes_price_source, wl.yes_price_is_provisional, and
        wl.signal_input_missing as side-effects for audit logging.

        Returns
        -------
        YesPriceSnapshot with is_provisional=True, or None if unavailable.
        None is a valid result — callers must treat it as YES mid unavailable.
        """
        if market is None:
            wl.signal_input_missing = "yes_mid_unavailable:market_not_discovered"
            logger.warning("[bot] YES mid unavailable: market not discovered (window=%d)", wl.window_open_ts)
            return None

        snap = await self._yes_adapter.get_yes_mid(
            token_id=market.yes_token_id,
            timestamp=time.time(),
        )

        if snap is not None:
            wl.yes_price_source = snap.source
            wl.yes_price_is_provisional = snap.is_provisional
            logger.debug(
                "[bot] YES mid fetched: token=%s prob=%.4f provisional=%s window=%d",
                market.yes_token_id, snap.probability, snap.is_provisional, wl.window_open_ts,
            )
        else:
            wl.signal_input_missing = "yes_mid_unavailable:clob_request_failed"
            logger.warning(
                "[bot] YES mid unavailable for window=%d token=%s (CLOB request failed)",
                wl.window_open_ts, market.yes_token_id,
            )

        return snap

    async def _execute_paper_trades(
        self,
        wl: WindowLog,
        sig,
        market: WindowMarket,
        yes_snap: Optional[YesPriceSnapshot],
        window_open_ts: int,
        window_close_ts: int,
    ) -> None:
        """
        Simulate maker and taker paper fills, then settle at window close.

        YES probability flow
        --------------------
        yes_snap is the pre-signal CLOB midpoint snapshot fetched in _run_window.
        It is the ONLY valid source for decision_price in the taker lane.
        BTC/USD prices (wl.fast_price / wl.chainlink_price) MUST NOT reach
        the taker lane (TakerLane.evaluate() has a hard ValueError guard).

        Intra-window collection
        -----------------------
        IntraWindowYesPriceCollector polls market.yes_token_id every ~60s using
        asyncio.gather alongside the settlement sleep.  Collected prices feed
        the maker fill simulation with grade PROVISIONAL_OBSERVED_PROXY.
        """
        decision_ts = time.time()

        # YES probability for both lanes — from pre-signal CLOB fetch.
        yes_probability: Optional[float] = yes_snap.probability if yes_snap is not None else None
        yes_src = SRC_CLOB_ASK_AT_SIGNAL if yes_probability is not None else SRC_UNAVAILABLE

        if yes_probability is None:
            logger.warning(
                "[bot] YES probability unavailable for window=%d token=%s — "
                "taker fill blocked, maker fill blocked",
                window_open_ts, market.yes_token_id,
            )

        # --- Daily entry cap check ---
        can_enter, entry_reason = self._caps.can_enter()

        # --- Concurrent: intra-window YES price collection + settlement wait ---
        # IntraWindowYesPriceCollector uses market.yes_token_id directly.
        # Both tasks run concurrently via asyncio.gather.
        now = time.time()
        sleep_time = max(window_close_ts - now + 1, 0.0)
        collect_duration = max(sleep_time - 5.0, 0.0)  # stop polling 5s before close

        if collect_duration > 0:
            collect_result, _ = await asyncio.gather(
                self._intra_collector.collect(market.yes_token_id, collect_duration),
                asyncio.sleep(sleep_time),
            )
        else:
            collect_result = CollectionResult(poll_interval_s=self._maker_poll_interval)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        intra_prices = collect_result.prices

        # Log path statistics for audit / realism tracking.
        wl.maker_poll_interval_seconds = collect_result.poll_interval_s
        wl.maker_path_points_collected = collect_result.point_count
        wl.maker_first_path_ts = collect_result.first_ts
        wl.maker_last_path_ts = collect_result.last_ts

        if collect_result.point_count == 0:
            wl.maker_path_source = "PROVISIONAL_NO_PATH"
            logger.debug(
                "[bot] maker_path: 0 points for window=%d token=%s — fill blocked",
                window_open_ts, market.yes_token_id,
            )
        elif collect_result.point_count == 1:
            wl.maker_path_source = "PROVISIONAL_SINGLE_POINT"
            logger.debug(
                "[bot] maker_path: 1 point for window=%d token=%s — "
                "fill blocked (single-point not evaluable)",
                window_open_ts, market.yes_token_id,
            )
        else:
            wl.maker_path_source = "PROVISIONAL_MULTI_POINT"
            logger.debug(
                "[bot] maker_path: %d points for window=%d token=%s — evaluable",
                collect_result.point_count, window_open_ts, market.yes_token_id,
            )

        # --- Maker lane ---
        # fill_realism_source is intentionally omitted: grade is auto-determined
        # from path density (None/single-point/multi-point).  Pass FILL_GRADE_OBSERVED
        # only when a real WebSocket book feed is connected (future upgrade).
        maker_result = self._maker_lane.evaluate(
            window_open_ts=window_open_ts,
            slug=wl.slug,
            signal_direction=sig.direction.value,
            intended_price=yes_probability,   # YES probability (0,1) from CLOB
            bankroll=self._sizer.bankroll,
            intra_window_prices=intra_prices if intra_prices else None,
        )
        wl.maker_quote_bucket = maker_result.quote_bucket
        wl.maker_intended_price = maker_result.intended_price
        wl.maker_fill_realism_grade = maker_result.fill_realism_grade
        wl.maker_fill_evaluable = maker_result.fill_evaluable
        wl.maker_fill_realism_mode = maker_result.fill_realism_grade
        wl.maker_fill_confirmed_by_later_point = maker_result.fill_confirmed_by_later_point
        wl.maker_fill_confirmation_index = maker_result.fill_confirmation_index
        wl.maker_trade_zone_eligible = (maker_result.quote_bucket != "INELIGIBLE")
        wl.maker_bankroll_fraction = maker_result.bankroll_fraction
        wl.maker_break_even_wr = maker_result.break_even_wr_estimate
        wl.maker_win_if_correct = maker_result.win_if_correct
        wl.maker_loss_if_wrong = maker_result.loss_if_wrong

        # --- Taker lane ---
        # decision_price MUST be a YES probability in (0,1).
        # BTC/USD prices must NEVER reach here — TakerLane has a hard ValueError guard.
        # If YES probability is unavailable (yes_snap was None), pass None → no fill.
        # execution_blocked:yes_probability_unavailable is logged via rejection_reason.
        taker_decision_price = yes_probability  # None → fill blocked by TakerLane
        taker_result = self._taker_lane.evaluate(
            window_open_ts=window_open_ts,
            slug=wl.slug,
            signal_direction=sig.direction.value,
            decision_price=taker_decision_price,
            bankroll=self._sizer.bankroll,
            decision_ts=decision_ts,
            execution_source=yes_src,
        )
        if taker_decision_price is None:
            taker_result.rejection_reason = "execution_blocked:yes_probability_unavailable"
        wl.taker_intended_price = taker_result.intended_price
        wl.taker_decision_ts = taker_result.decision_ts
        wl.taker_execution_source = taker_result.execution_source
        wl.taker_assumed_slippage_bps = taker_result.assumed_slippage_bps
        wl.taker_trade_zone_eligible = taker_result.zone_eligible

        # --- Entry cap application (both lanes together) ---
        # Policy (either_lane_fill): a window where either lane produces a fill
        # consumes exactly ONE entry budget slot.  Both lanes are blocked/unblocked
        # together so the cap is honest regardless of which lane fills.
        if not can_enter:
            logger.info("[bot] Entry cap blocked both lanes: %s", entry_reason)
            maker_result.filled = False
            maker_result.rejection_reason = f"cap:{entry_reason}"
            taker_result.filled = False
            taker_result.rejection_reason = f"cap:{entry_reason}"
        else:
            wl.taker_filled = taker_result.filled
            wl.taker_fill_price = taker_result.fill_price
            wl.taker_fee_per_share = taker_result.fee_per_share
            wl.taker_total_fee = taker_result.total_fee
            wl.taker_bankroll_fraction = taker_result.bankroll_fraction
            wl.taker_break_even_wr = taker_result.break_even_wr_estimate
            wl.taker_win_if_correct = taker_result.win_if_correct
            wl.taker_loss_if_wrong = taker_result.loss_if_wrong
            # Record entry if EITHER lane filled (one budget slot per window)
            if maker_result.filled or taker_result.filled:
                self._caps.record_entry()
                logger.info(
                    "[bot] Entry recorded (either_lane_fill policy): "
                    "maker_filled=%s taker_filled=%s window=%d",
                    maker_result.filled, taker_result.filled, window_open_ts,
                )

        # --- Settlement: use Chainlink as proxy truth ---
        close_cl = self._chainlink_feed.latest
        close_fast = self._fast_feed.latest

        if close_cl is not None and wl.chainlink_price is not None:
            # Chainlink proxy settlement
            actual_outcome = self._determine_outcome(
                open_price=wl.chainlink_price,
                close_price=close_cl.price,
            )
            settlement_source = SETTLE_CHAINLINK_PROXY
        elif close_fast is not None and wl.fast_price is not None:
            # Fallback: fast feed proxy settlement
            actual_outcome = self._determine_outcome(
                open_price=wl.fast_price,
                close_price=close_fast.price,
            )
            settlement_source = SETTLE_FAST_PROXY
            logger.warning(
                "[bot] Settlement fallback to FAST_PROXY for window=%d "
                "(Chainlink unavailable)",
                window_open_ts,
            )
        else:
            actual_outcome = None
            settlement_source = SETTLE_UNAVAILABLE
            logger.warning(
                "[bot] Settlement UNAVAILABLE for window=%d (both feeds missing)",
                window_open_ts,
            )

        wl.settlement_source = settlement_source
        logger.info(
            "[bot] Settlement window=%d source=%s outcome=%s",
            window_open_ts, settlement_source, actual_outcome
        )

        # --- Settle both lanes ---
        if actual_outcome is not None:
            maker_result = self._maker_lane.settle(maker_result, actual_outcome)
            taker_result = self._taker_lane.settle(taker_result, actual_outcome)

        wl.maker_filled = maker_result.filled
        wl.maker_fill_price = maker_result.fill_price
        wl.maker_net_pnl = maker_result.net_pnl
        wl.maker_outcome_correct = maker_result.outcome_correct

        wl.taker_net_pnl = taker_result.net_pnl
        wl.taker_outcome_correct = taker_result.outcome_correct

        # Record cap exit for any settled position
        if maker_result.filled or taker_result.filled:
            self._caps.record_exit()

        # Update bankroll with maker P&L (primary lane).
        if maker_result.net_pnl is not None:
            self._sizer.record_trade(maker_result.net_pnl)

    def _capture_feeds(self, wl: WindowLog) -> None:
        """Snapshot both feeds into the window log."""
        fast_snap = self._fast_feed.latest
        cl_snap = self._chainlink_feed.latest
        wl.fast_price = fast_snap.price if fast_snap else None
        wl.chainlink_price = cl_snap.price if cl_snap else None
        wl.fast_feed_gap_seconds = self._fast_feed.gap_seconds_now()
        wl.chainlink_gap_seconds = self._chainlink_feed.gap_seconds_now()
        wl.fast_feed_stale = fast_snap.is_stale if fast_snap else True
        wl.chainlink_feed_stale = cl_snap.is_stale if cl_snap else True

    def _determine_outcome(
        self, open_price: Optional[float], close_price: Optional[float]
    ) -> Optional[str]:
        """
        Determine YES/NO outcome from open and close prices.
        Returns None if prices are unavailable.

        NOTE: This is a proxy for the actual Polymarket settlement.
        TODO: Replace with official settlement endpoint when available.
        """
        if open_price is None or close_price is None:
            return None
        return "YES" if close_price > open_price else "NO"

    def _log_feed_pair(self, wl: WindowLog, window_open_ts: int) -> None:
        """Log both feeds every active window (mandatory per spec §1)."""
        logger.info(
            "[feeds] window=%d "
            "fast_price=%s fast_gap=%.2fs fast_stale=%s | "
            "chainlink_price=%s chainlink_gap=%.2fs chainlink_stale=%s | "
            "basis_bps=%s basis_flag=%s",
            window_open_ts,
            f"{wl.fast_price:.2f}" if wl.fast_price else "N/A",
            wl.fast_feed_gap_seconds or 0.0,
            wl.fast_feed_stale,
            f"{wl.chainlink_price:.2f}" if wl.chainlink_price else "N/A",
            wl.chainlink_gap_seconds or 0.0,
            wl.chainlink_feed_stale,
            f"{wl.basis_bps:.2f}" if wl.basis_bps is not None else "N/A",
            wl.basis_mismatch,
        )

    def _check_kill(self, wl: WindowLog) -> None:
        """Evaluate kill conditions and raise SessionKillError if KILL triggered."""
        stats = self._reporter.build_session_stats()
        result = self._validator.check(stats)

        if result.recommended_action == KillAction.TIGHTEN:
            self._sizer.tightened = True
            logger.warning("[bot] TIGHTEN triggered: %s", result.verdict)

        if result.recommended_action == KillAction.KILL:
            self._reporter.print_final_summary(kill_result=result)
            raise SessionKillError(result.verdict)
