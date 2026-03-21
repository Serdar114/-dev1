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

Phases
------
  0a : Infrastructure validation  — feeds + discovery only, no signals
  0b : Signal validation           — signal engine on, no fills
  0c : Paper trading validation    — full dual-lane paper execution
  1  : Live-candidate readiness    — paper with tighter kill thresholds

Kill conditions are evaluated after every window via validator module.
The bot will stop (raise SessionKillError) if a KILL action is returned.

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
from execution.maker_lane import MakerLane
from execution.taker_lane import TakerLane
from feeds.chainlink_feed import ChainlinkFeedAdapter
from feeds.fast_feed import FastFeedAdapter
from logger.summary import SummaryReporter, WindowLog
from risk.sizing import PositionSizer
from signal.engine import FeedWindow, SignalEngine, SignalDirection
from validator.kill_conditions import KillAction, KillConditionValidator, SessionStats

logger = logging.getLogger(__name__)


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
        self._reporter = SummaryReporter(config, self._phase)
        self._validator = KillConditionValidator(config["kill_conditions_cfg"])

    async def run(self) -> None:
        """
        Main loop: start feeds, then iterate over 5m windows until killed.
        """
        logger.info("[bot] Starting research bot — Phase %s", self._phase)
        logger.info("[bot] PAPER TRADING ONLY — no live orders will be placed")

        await self._fast_feed.start()
        await self._chainlink_feed.start()

        # Allow feeds to warm up for a few seconds.
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
        """
        Phase 0a: Validate feeds and discovery for N windows without
        computing signals or executing paper trades.
        """
        logger.info("[bot/0a] Infrastructure validation — running 3 windows")
        for i in range(3):
            boundary = current_window_boundary()
            wl = WindowLog(window_open_ts=boundary, slug="", phase=self._phase)

            fast = self._fast_feed.latest
            cl = self._chainlink_feed.latest
            wl.fast_price = fast.price if fast else None
            wl.chainlink_price = cl.price if cl else None
            wl.fast_feed_gap_seconds = self._fast_feed.gap_seconds_now()
            wl.chainlink_gap_seconds = self._chainlink_feed.gap_seconds_now()
            wl.fast_feed_stale = fast.is_stale if fast else True
            wl.chainlink_feed_stale = cl.is_stale if cl else True

            start = time.time()
            market = await self._discovery.get_market_for_window(boundary)
            latency_ms = (time.time() - start) * 1000
            wl.discovery_ok = market is not None
            wl.discovery_latency_ms = latency_ms
            wl.slug = market.slug if market else f"btc-updown-5m-{boundary}"

            self._log_feed_pair(wl, boundary)
            self._reporter.record_window(wl)
            await asyncio.sleep(300)   # wait one window

        self._reporter.print_final_summary()

    # ------------------------------------------------------------------
    # Window loop
    # ------------------------------------------------------------------

    async def _wait_for_next_window(self) -> None:
        """Sleep until the start of the next 5m window boundary."""
        now = time.time()
        nxt = next_window_boundary(now)
        wait = nxt - now
        if wait > 0:
            logger.debug("[bot] Sleeping %.1fs until next window boundary", wait)
            await asyncio.sleep(wait)

    async def _run_window(self) -> None:
        """Execute one full window: feeds → discovery → signal → execution → settle."""
        window_open_ts = current_window_boundary()
        window_close_ts = window_open_ts + 300

        logger.info("[bot] === Window open ts=%d ===", window_open_ts)

        wl = WindowLog(window_open_ts=window_open_ts, slug="", phase=self._phase)

        # ---- Step 1: Capture feed snapshots ----
        open_capture_start = time.time()
        fast_snap = self._fast_feed.latest
        cl_snap = self._chainlink_feed.latest
        open_capture_delay = time.time() - open_capture_start

        wl.fast_price = fast_snap.price if fast_snap else None
        wl.chainlink_price = cl_snap.price if cl_snap else None
        wl.fast_feed_gap_seconds = self._fast_feed.gap_seconds_now()
        wl.chainlink_gap_seconds = self._chainlink_feed.gap_seconds_now()
        wl.fast_feed_stale = fast_snap.is_stale if fast_snap else True
        wl.chainlink_feed_stale = cl_snap.is_stale if cl_snap else True
        wl.open_price_capture_delay_seconds = open_capture_delay

        # ---- Step 2: Market discovery ----
        disc_start = time.time()
        market = await self._discovery.get_market_for_window(window_open_ts)
        wl.discovery_ok = market is not None
        wl.discovery_latency_ms = (time.time() - disc_start) * 1000
        wl.slug = market.slug if market else f"btc-updown-5m-{window_open_ts}"

        # ---- Step 3: Log both feeds (mandatory) ----
        self._log_feed_pair(wl, window_open_ts)

        # ---- Phase 0a exits early ----
        if self._phase == "0a":
            self._reporter.record_window(wl)
            return

        # ---- Step 4: Signal engine ----
        seconds_remaining = window_close_ts - time.time()
        fw = FeedWindow(
            window_open_ts=window_open_ts,
            open_fast_price=fast_snap.price if fast_snap else None,
            latest_fast_price=fast_snap.price if fast_snap else None,
            open_chainlink_price=cl_snap.price if cl_snap else None,
            latest_chainlink_price=cl_snap.price if cl_snap else None,
            fast_gap_seconds=wl.fast_feed_gap_seconds,
            chainlink_gap_seconds=wl.chainlink_gap_seconds,
            yes_bid=None,   # TODO: populate from CLOB order book feed
            yes_ask=None,   # TODO: populate from CLOB order book feed
            seconds_to_window_close=seconds_remaining,
            candles_same_direction=0,  # TODO: populate from 1m candle tracker
            fast_feed_stale=wl.fast_feed_stale,
            chainlink_feed_stale=wl.chainlink_feed_stale,
            slug=wl.slug,
        )

        sig = self._signal_engine.evaluate(fw)

        # Compute basis mismatch fields
        wl.basis_bps = sig.basis_bps
        wl.basis_mismatch_bps = sig.basis_mismatch_bps
        wl.basis_mismatch = sig.basis_mismatch
        wl.signal_direction = sig.direction.value
        wl.signal_eligible = sig.quote_eligible
        wl.gate_summary = sig.gate_summary()

        # ---- Phase 0b exits before fill simulation ----
        if self._phase == "0b":
            self._reporter.record_window(wl)
            self._check_kill(wl)
            return

        # ---- Step 5: Paper execution (phases 0c and 1) ----
        if market is not None:
            await self._execute_paper_trades(wl, sig, market, window_open_ts)

        self._reporter.record_window(wl)

        # ---- Step 6: Kill condition check ----
        self._check_kill(wl)

    async def _execute_paper_trades(
        self,
        wl: WindowLog,
        sig,
        market: WindowMarket,
        window_open_ts: int,
    ) -> None:
        """
        Simulate maker and taker paper fills for the current window.
        Settlement happens at end of window (after sleeping to close).
        """
        open_price = wl.fast_price

        # --- Maker lane ---
        maker_result = self._maker_lane.evaluate(
            window_open_ts=window_open_ts,
            slug=wl.slug,
            signal_direction=sig.direction.value,
            intended_price=sig.intended_price,
            bankroll=self._sizer.bankroll,
            low_price_in_window=open_price,    # TODO: use intra-window min price
            high_price_in_window=open_price,   # TODO: use intra-window max price
        )
        wl.maker_quote_bucket = maker_result.quote_bucket
        wl.maker_intended_price = maker_result.intended_price
        wl.maker_bankroll_fraction = maker_result.bankroll_fraction
        wl.maker_break_even_wr = maker_result.break_even_wr_estimate
        wl.maker_win_if_correct = maker_result.win_if_correct
        wl.maker_loss_if_wrong = maker_result.loss_if_wrong

        # --- Taker lane ---
        taker_result = self._taker_lane.evaluate(
            window_open_ts=window_open_ts,
            slug=wl.slug,
            signal_direction=sig.direction.value,
            open_price=open_price,
            bankroll=self._sizer.bankroll,
        )
        wl.taker_intended_price = taker_result.intended_price
        wl.taker_filled = taker_result.filled
        wl.taker_fill_price = taker_result.fill_price
        wl.taker_fee_per_share = taker_result.fee_per_share
        wl.taker_total_fee = taker_result.total_fee
        wl.taker_bankroll_fraction = taker_result.bankroll_fraction
        wl.taker_break_even_wr = taker_result.break_even_wr_estimate
        wl.taker_win_if_correct = taker_result.win_if_correct
        wl.taker_loss_if_wrong = taker_result.loss_if_wrong

        # --- Wait for window close to settle ---
        now = time.time()
        close_ts = window_open_ts + 300
        sleep_time = close_ts - now + 1   # +1s buffer for settlement
        if sleep_time > 0:
            logger.debug("[bot] Waiting %.1fs for window close (settle)", sleep_time)
            await asyncio.sleep(sleep_time)

        # --- Determine outcome ---
        close_fast = self._fast_feed.latest
        actual_outcome = self._determine_outcome(
            open_price=open_price,
            close_price=close_fast.price if close_fast else None,
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

        # Update bankroll with maker P&L (primary lane).
        if maker_result.net_pnl is not None:
            self._sizer.record_trade(maker_result.net_pnl)

    def _determine_outcome(
        self, open_price: Optional[float], close_price: Optional[float]
    ) -> Optional[str]:
        """
        Determine YES/NO outcome from open and close prices.
        Returns None if prices are unavailable.
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
            f"{wl.fast_price:.6f}" if wl.fast_price else "N/A",
            wl.fast_feed_gap_seconds or 0.0,
            wl.fast_feed_stale,
            f"{wl.chainlink_price:.6f}" if wl.chainlink_price else "N/A",
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
