"""
polybot_v2 Phase 2 - main entry point.

Two-lane paper evaluation system:
  Lane 1 (baseline):    Selective Taker Paper Engine
  Lane 2 (primary):     Maker Shadow Evaluation Lane

No real orders are placed. Phase 2 measures whether maker has edge.
"""

from __future__ import annotations

import logging
import math
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Add src to path when running directly
sys.path.insert(0, str(Path(__file__).parent))

from binance_feed import BinanceFeed
from session_verdict import build_session_summary
from edge_engine import EdgeEngine
from fair_prob_engine import FairProbEngine
from fee_engine import FeeEngine
from logger import StructuredLogger, setup_console_logger
from maker_shadow_probe import MakerShadowProbe
from market_discovery import MarketDiscovery
from metrics import MetricsCollector
from models import BankrollState, MarketSnapshot, PriceSnapshot
from paper_executor import PaperExecutor
from polymarket_client import PolymarketClient
from risk_manager import RiskManager, RiskState
from settings import Settings
from signal_engine import SignalEngine
from stake_policy import StakePolicy
from state_store import StateStore

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 2.0          # seconds between main loop iterations
_METRICS_LOG_INTERVAL = 60.0  # seconds between metrics summaries


def build_market_snapshot(
    market_info,
    client: PolymarketClient,
) -> Optional[MarketSnapshot]:
    """Fetch current order book and build MarketSnapshot."""
    book_yes = client.get_order_book(market_info.token_id_yes)
    book_no = client.get_order_book(market_info.token_id_no)
    if book_yes is None or book_no is None:
        return None

    bid_yes, ask_yes = client.parse_best_bid_ask(book_yes)
    bid_no, ask_no = client.parse_best_bid_ask(book_no)

    last_price = client.get_last_trade_price(market_info.token_id_yes)

    return MarketSnapshot(
        condition_id=market_info.condition_id,
        token_id_yes=market_info.token_id_yes,
        token_id_no=market_info.token_id_no,
        best_bid_yes=bid_yes,
        best_ask_yes=ask_yes,
        best_bid_no=bid_no,
        best_ask_no=ask_no,
        last_trade_price_yes=last_price,
        window_end_ts=market_info.window_end_ts,
        slug=market_info.slug,
    )


class PolybotV2:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self._cfg = Settings(config_path)

        # Session timestamp for log rotation
        self._session_ts = time.time()

        # Logging — per-session subdirectory keeps runs separated
        session_log_dir = self._cfg.log_dir_for_session(self._session_ts)
        session_log_dir.mkdir(parents=True, exist_ok=True)
        setup_console_logger(self._cfg.log_level)
        self._structured = StructuredLogger(
            session_log_dir,
            enabled=self._cfg.jsonl_enabled,
        )

        # State
        state_path = self._cfg.log_dir / "state.json"
        self._store = StateStore(state_path)

        initial_bankroll = (
            self._store.bankroll
            if self._store.bankroll is not None
            else self._cfg.bankroll
        )
        peak = max(initial_bankroll, self._cfg.bankroll)
        self._bankroll = BankrollState(
            bankroll=initial_bankroll,
            peak_bankroll=peak,
            total_pnl=self._store.get("total_pnl", 0.0),
            drawdown=self._store.get("drawdown", 0.0),
        )

        self._risk_state = RiskState(
            consecutive_losses=self._store.consecutive_losses,
            cooldown_windows_remaining=self._store.cooldown_windows_remaining,
            max_notional_per_trade=self._bankroll.bankroll * self._cfg.max_risk_fraction,
        )

        # Components
        self._binance = BinanceFeed(self._cfg.stale_binance_ms)

        self._poly_client = PolymarketClient()
        self._discovery = MarketDiscovery(self._poly_client, self._cfg.window_sec)

        self._fee_engine = FeeEngine(
            fee_rate_base=self._cfg.fee_rate_base,
            fee_exponent=self._cfg.fee_exponent,
            maker_rebate_share=self._cfg.maker_rebate_share,
        )
        self._fair_engine = FairProbEngine(
            sigma_floor=self._cfg.sigma_floor,
            tau_floor=self._cfg.tau_floor,
            zscore_clip=self._cfg.zscore_clip,
            prob_clip_min=self._cfg.prob_clip_min,
            prob_clip_max=self._cfg.prob_clip_max,
        )
        self._edge_engine = EdgeEngine(self._fee_engine, self._cfg.min_after_fee_edge)
        self._stake_policy = StakePolicy(
            min_notional=self._cfg.min_notional,
            max_risk_fraction=self._cfg.max_risk_fraction,
            drawdown_reduce_factor=self._cfg.drawdown_reduce_factor,
            scale_tiers=self._cfg.scale_tiers,
        )
        self._risk_mgr = RiskManager(
            max_actions_per_window=self._cfg.max_actions_per_window,
            max_consecutive_losses=self._cfg.max_consecutive_losses,
            cooldown_windows=self._cfg.cooldown_windows,
            max_shadow_quotes_per_window=self._cfg.max_shadow_quotes_per_window,
            max_notional_per_trade=self._bankroll.bankroll * self._cfg.max_risk_fraction,
            stale_binance_ms=self._cfg.stale_binance_ms,
            stale_polymarket_ms=self._cfg.stale_polymarket_ms,
            max_open_paper_trades_per_window=self._cfg.max_open_paper_trades_per_window,
        )
        self._signal_engine = SignalEngine(
            settings=self._cfg,
            fair_prob_engine=self._fair_engine,
            edge_engine=self._edge_engine,
            stake_policy=self._stake_policy,
            risk_manager=self._risk_mgr,
        )
        self._paper_exec = PaperExecutor(self._fee_engine, self._stake_policy)
        self._shadow_probe = MakerShadowProbe(self._cfg)
        self._metrics = MetricsCollector()

        # UI dashboard (optional)
        self._ui = None
        self._ui_state = None
        if self._cfg.ui_enabled:
            try:
                from ui_dashboard import UIDashboard
                from ui_state import UIState, UILogHandler
                self._ui_state = UIState()
                # Seed with known-at-startup values so UI never shows $0.00 on fields
                # where config already provides the correct starting value
                self._ui_state.update(
                    mode=self._cfg.mode,
                    bankroll=self._bankroll.bankroll,
                    peak_bankroll=self._bankroll.peak_bankroll,
                    market_slug="discovering…",
                    entry_start_sec=self._cfg.entry_start_sec,
                    entry_end_sec=self._cfg.entry_end_sec,
                    status_msg="INITIALIZING…",
                )
                self._ui = UIDashboard(self._cfg, self._ui_state)
                # Route all log records to the live log panel
                _ui_handler = UILogHandler(self._ui_state)
                _ui_handler.setLevel(logging.DEBUG)
                logging.getLogger().addHandler(_ui_handler)
            except Exception as exc:
                log.warning("UI dashboard unavailable: %s", exc)
                self._ui = None
                self._ui_state = None

        # Runtime state
        self._last_window_ts: float = self._store.last_window_ts
        self._last_market_refresh: float = 0.0
        self._last_metrics_log: float = 0.0
        self._current_market: Optional[MarketSnapshot] = None
        self._current_market_age: float = 0.0
        self._window_open_price: Optional[float] = None
        self._running = False
        # Last sanity check details for UI diagnostics
        self._last_sanity_details: dict = {}

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        log.info(
            "polybot_v2 Phase 2 starting | mode=%s bankroll=%.2f USDC "
            "| taker=baseline maker=primary_eval",
            self._cfg.mode,
            self._bankroll.bankroll,
        )

        self._binance.start()
        self._running = True

        # Wait for first Binance price
        log.info("Waiting for Binance feed…")
        for _ in range(30):
            mid, _, _ = self._binance.get_snapshot()
            if mid is not None:
                break
            time.sleep(1)
        else:
            log.error("Binance feed did not connect within 30s, exiting")
            return

        log.info("Binance feed online")

        # Start UI if configured
        if self._ui is not None:
            try:
                self._ui.start()
            except Exception as exc:
                log.warning("UI start failed: %s — continuing without UI", exc)
                self._ui = None

        try:
            while self._running:
                self._tick()
                time.sleep(_LOOP_INTERVAL)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            self._shutdown()

    def _tick(self) -> None:
        now = time.time()

        # ------------------------------------------------------------------ #
        # Window boundary detection via LOCAL CLOCK (not market metadata).
        # current_window_start = floor(now / window_sec) * window_sec
        # current_window_end   = current_window_start + window_sec
        # This ensures boundary detection is deterministic and independent of
        # Polymarket market refresh timing.
        # ------------------------------------------------------------------ #
        window_sec = self._cfg.window_sec
        current_window_start = math.floor(now / window_sec) * window_sec
        current_window_end = current_window_start + window_sec

        if self._last_window_ts == 0:
            # First tick: initialise without triggering a resolve
            self._last_window_ts = current_window_end
            log.info(
                "Window initialised: start=%.0f end=%.0f (in %.0fs)",
                current_window_start, current_window_end,
                current_window_end - now,
            )
        elif current_window_end != self._last_window_ts:
            # Boundary crossed — resolve previous window at its exact close ts
            self._on_new_window(
                new_window_end=current_window_end,
                boundary_ts=current_window_start,  # = previous window_end
            )

        # Adaptive Polymarket refresh: fast during active window, slow outside
        elapsed_in_window = now - current_window_start
        in_active = (
            (self._cfg.entry_start_sec <= elapsed_in_window <= self._cfg.entry_end_sec)
            or (self._cfg.shadow_start_sec <= elapsed_in_window <= self._cfg.shadow_end_sec)
        )
        refresh_interval = (
            self._cfg.refresh_active_sec if in_active else self._cfg.refresh_inactive_sec
        )
        if now - self._last_market_refresh >= refresh_interval:
            self._refresh_market(now)

        # ── Binance snapshot ─────────────────────────────────────────────
        # Compute age with a FRESH time.time() AFTER get_snapshot() to avoid
        # the race where the WS callback fires between 'now' capture and the
        # snapshot read, making btc_ts > now → negative age.
        btc_mid, btc_ts, realized_vol = self._binance.get_snapshot()
        _snap_read_ts = time.time()
        if btc_mid is None:
            if self._ui_state is not None:
                self._ui_state.update(status_msg="WAITING FOR BINANCE FEED…")
            log.warning("No Binance price, skipping tick")
            return

        # Root-cause fix kept; max(0.0) is a defensive last-resort guard
        binance_age_ms = max(0.0, (_snap_read_ts - btc_ts) * 1000)

        # ── EARLY UI UPDATE ──────────────────────────────────────────────
        # Push BTC price BEFORE any market/sanity checks so the header
        # always shows the live price even on early-return paths.
        if self._ui_state is not None:
            self._ui_state.update(
                btc_mid=btc_mid,
                binance_age_ms=binance_age_ms,
                realized_vol_60s=realized_vol,
                ui_update_ts=_snap_read_ts,
            )
            if btc_mid <= 0.0:
                log.debug(
                    "UI_DEBUG btc_header_unset has_snapshot=True"
                    " snapshot_mid=%.2f snapshot_ts=%.3f",
                    btc_mid, btc_ts,
                )

        if self._current_market is None:
            if self._ui_state is not None:
                self._ui_state.update(status_msg="WAITING FOR MARKET DISCOVERY…")
            log.debug("No active market, waiting…")
            return

        market = self._current_market
        polymarket_age_ms = max(0.0, (_snap_read_ts - market.fetched_at) * 1000)

        # Set / maintain window open price — frozen at first tick of each window
        if self._window_open_price is None:
            self._window_open_price = btc_mid
            self._binance.set_window_open(btc_mid)
            log.info(
                "Window open snapshot: btc=%.2f window_start=%.0f",
                btc_mid, current_window_start,
            )

        # Implied price sanity check — hard reject if spread or skew too wide
        sanity_reject = self._sanity_check_market(market)

        price_snap = PriceSnapshot(
            btc_mid=btc_mid,
            timestamp=btc_ts,
            realized_vol_60s=realized_vol,
        )

        # Lane 1: Selective Taker
        # taker_signal_eval_enabled controls whether fair/edge logic runs and logs.
        # taker_paper_execution_enabled controls whether PAPER_TRADE opens real trades.
        wo = self._window_open_price or 0.0
        raw_d = (btc_mid - wo) / wo if wo > 0 else 0.0
        if not self._cfg.taker_signal_eval_enabled:
            # Taker eval disabled: emit a synthetic NO_TRADE so metrics/logs stay consistent
            from models import SignalDecision as _SD
            taker_decision = _SD(
                ts=now, window_ts=current_window_end,
                lane="selective_taker", action="NO_TRADE",
                chosen_side=None, reason="taker_signal_eval_disabled",
                seconds_to_expiry=market.seconds_to_expiry,
                elapsed_from_window_start=elapsed_in_window,
                btc_mid=btc_mid, window_open=wo,
                delta_pct=raw_d, delta_raw_fraction=raw_d,
                delta_pct_display=raw_d * 100.0,
                realized_vol_60s=realized_vol,
                implied_yes_prob=market.implied_yes_prob,
                bankroll=self._bankroll.bankroll,
                data_age_ms=binance_age_ms,
            )
        elif sanity_reject:
            # Hard sanity gate — synthetic NO_TRADE, carry diagnostic context
            from models import SignalDecision as _SD
            lf = self._signal_engine._last_fair_result
            lc = self._signal_engine._last_decision_context
            taker_decision = _SD(
                ts=now, window_ts=current_window_end,
                lane="selective_taker", action="NO_TRADE",
                chosen_side=None, reason=sanity_reject,
                seconds_to_expiry=market.seconds_to_expiry,
                elapsed_from_window_start=elapsed_in_window,
                btc_mid=btc_mid,
                window_open=wo,
                delta_pct=raw_d,
                delta_raw_fraction=raw_d,
                delta_pct_display=raw_d * 100.0,
                realized_vol_60s=realized_vol,
                implied_yes_prob=market.implied_yes_prob,
                fair_yes_prob=lf.fair_yes_prob if lf else 0.0,
                raw_edge_yes=lc["raw_edge_yes"] if lc else 0.0,
                raw_edge_no=lc["raw_edge_no"] if lc else 0.0,
                after_fee_edge_yes=lc["after_fee_edge_yes"] if lc else 0.0,
                after_fee_edge_no=lc["after_fee_edge_no"] if lc else 0.0,
                confidence_score=lc["confidence_score"] if lc else 0.0,
                confidence_components=lc["confidence_components"] if lc else "",
                fee_per_share_yes=lc["fee_per_share_yes"] if lc else 0.0,
                fee_per_share_no=lc["fee_per_share_no"] if lc else 0.0,
                effective_fee_rate_yes=lc["effective_fee_rate_yes"] if lc else 0.0,
                effective_fee_rate_no=lc["effective_fee_rate_no"] if lc else 0.0,
                regime=self._signal_engine._last_regime,
                pattern=self._signal_engine._last_pattern,
                bankroll=self._bankroll.bankroll,
                data_age_ms=binance_age_ms,
                fair_computed=(lf is not None),
                fair_computed_fresh=False,
                context_from_cache=(lf is not None),
            )
        else:
            taker_decision = self._signal_engine.evaluate_taker(
                price_snap=price_snap,
                market_snap=market,
                window_open=self._window_open_price,
                bankroll_state=self._bankroll,
                risk_state=self._risk_state,
                binance_age_ms=binance_age_ms,
                polymarket_age_ms=polymarket_age_ms,
            )
        self._metrics.on_signal(taker_decision)
        self._log_signal(taker_decision)

        if taker_decision.action == "PAPER_TRADE" and self._cfg.taker_paper_execution_enabled:
            entry_price = (
                market.best_ask_yes
                if taker_decision.chosen_side == "yes"
                else market.best_ask_no
            )
            trade = self._paper_exec.open_trade(taker_decision, entry_price, self._bankroll)
            if trade:
                self._risk_state.actions_this_window += 1
                self._risk_state.open_paper_trades_this_window += 1
                self._log_paper_trade(trade, "open")
                # Trade thesis — why we opened; logged separately for easy filtering
                d = taker_decision
                log.info(
                    "PAPER_TRADE OPEN thesis: side=%s fair=%.4f implied=%.4f "
                    "afe_yes=%+.5f afe_no=%+.5f conf=%.3f "
                    "delta_raw=%.5f delta_pct=%.4f%% regime=%s pattern=%s",
                    d.chosen_side, d.fair_yes_prob, d.implied_yes_prob,
                    d.after_fee_edge_yes, d.after_fee_edge_no, d.confidence_score,
                    d.delta_raw_fraction, d.delta_pct_display,
                    d.regime, d.pattern,
                )
                self._structured.log("paper_trades", {
                    "event": "trade_thesis",
                    "trade_id": trade.trade_id,
                    "side": d.chosen_side,
                    "entry_price": entry_price,
                    "fair_yes_prob": d.fair_yes_prob,
                    "implied_yes_prob": d.implied_yes_prob,
                    "raw_edge_yes": d.raw_edge_yes,
                    "raw_edge_no": d.raw_edge_no,
                    "after_fee_edge_yes": d.after_fee_edge_yes,
                    "after_fee_edge_no": d.after_fee_edge_no,
                    "fee_per_share": d.fee_per_share,
                    "effective_rate": d.effective_rate,
                    "confidence_score": d.confidence_score,
                    "confidence_components": d.confidence_components,
                    "delta_raw_fraction": d.delta_raw_fraction,
                    "delta_pct_display": d.delta_pct_display,
                    "regime": d.regime,
                    "pattern": d.pattern,
                    "elapsed_from_window_start": d.elapsed_from_window_start,
                    "seconds_to_expiry": d.seconds_to_expiry,
                    "sanity_status": "pass",
                })

        # Lane 2: Shadow Probe
        if self._cfg.mode == "paper_with_shadow_probe":
            shadow_decision = self._signal_engine.evaluate_shadow(
                price_snap=price_snap,
                market_snap=market,
                window_open=self._window_open_price,
                bankroll_state=self._bankroll,
                risk_state=self._risk_state,
                binance_age_ms=binance_age_ms,
                polymarket_age_ms=polymarket_age_ms,
            )
            self._metrics.on_signal(shadow_decision)

            if shadow_decision.action == "SHADOW_QUOTE":
                shadow_quote = self._shadow_probe.build_quote(shadow_decision, market)
                if shadow_quote:
                    self._risk_state.shadow_quotes_this_window += 1
                    self._metrics.on_shadow_fill(shadow_quote)
                    self._log_shadow_quote(shadow_quote)

            # Advance TTL-based fill simulation for all pending shadow quotes
            completed = self._shadow_probe.process_pending(market)
            for sq in completed:
                self._metrics.on_shadow_state_change(sq)
                self._log_shadow_quote(sq)

        # Update UI state snapshot
        if self._ui is not None and self._ui_state is not None:
            try:
                self._update_ui_state(
                    now=now,
                    btc_mid=btc_mid,
                    market=market,
                    current_window_start=current_window_start,
                    current_window_end=current_window_end,
                    elapsed=elapsed_in_window,
                    taker_decision=taker_decision,
                    binance_age_ms=binance_age_ms,
                    realized_vol=realized_vol,
                )
            except Exception as exc:
                log.debug("UI state update failed: %s", exc)

        # Periodic metrics log
        if now - self._last_metrics_log > _METRICS_LOG_INTERVAL:
            snap = self._metrics.snapshot()
            log.info("METRICS: %s", snap)
            self._last_metrics_log = now

    def _sanity_check_market(self, market: MarketSnapshot) -> Optional[str]:
        """
        Hard sanity gate for market data quality.
        Returns a reject reason string if market should be skipped, else None.
        Stores numerical diagnostics in self._last_sanity_details for UI display.
        """
        # ── Degenerate book guard ─────────────────────────────────────────
        # Must run BEFORE mid computations to avoid logging internally
        # inconsistent fields (raw bid/ask contradicting the fallback mid).
        bid_yes = market.best_bid_yes
        ask_yes = market.best_ask_yes
        bid_no  = market.best_bid_no
        ask_no  = market.best_ask_no
        if bid_yes <= 0 or ask_yes <= 0 or bid_yes >= ask_yes:
            reason = (
                f"degenerate_book_reject(yes_bid={bid_yes:.4f}"
                f" yes_ask={ask_yes:.4f})"
            )
            self._last_sanity_details = {
                "yes_bid": bid_yes, "yes_ask": ask_yes,
                "no_bid": bid_no, "no_ask": ask_no,
                "yes_mid": None, "no_mid": None,
                "midpoint_sum": None, "spread_yes": None, "spread_no": None,
                "complement_skew": None,
                "spread_threshold": self._cfg.max_spread_warn,
                "skew_threshold": self._cfg.max_complement_skew,
                "reject": reason,
            }
            log.warning("Market sanity REJECT: %s", reason)
            return reason
        if bid_no <= 0 or ask_no <= 0 or bid_no >= ask_no:
            reason = (
                f"degenerate_book_reject(no_bid={bid_no:.4f}"
                f" no_ask={ask_no:.4f})"
            )
            self._last_sanity_details = {
                "yes_bid": bid_yes, "yes_ask": ask_yes,
                "no_bid": bid_no, "no_ask": ask_no,
                "yes_mid": None, "no_mid": None,
                "midpoint_sum": None, "spread_yes": None, "spread_no": None,
                "complement_skew": None,
                "spread_threshold": self._cfg.max_spread_warn,
                "skew_threshold": self._cfg.max_complement_skew,
                "reject": reason,
            }
            log.warning("Market sanity REJECT: %s", reason)
            return reason

        # ── Mid computations — derived directly from raw quotes ───────────
        # Never use implied_yes_prob (which has a silent 0.5 fallback) so that
        # yes_mid in diagnostics is always consistent with yes_bid/yes_ask.
        yes_mid = (bid_yes + ask_yes) / 2.0
        no_mid  = (bid_no  + ask_no)  / 2.0
        spread_yes = ask_yes - bid_yes
        spread_no  = ask_no  - bid_no
        midpoint_sum = yes_mid + no_mid
        skew = abs(midpoint_sum - 1.0)

        # Always update diagnostics so UI shows current numbers
        self._last_sanity_details = {
            "yes_bid": bid_yes,
            "yes_ask": ask_yes,
            "no_bid": bid_no,
            "no_ask": ask_no,
            "yes_mid": yes_mid,
            "no_mid": no_mid,
            "midpoint_sum": midpoint_sum,
            "spread_yes": spread_yes,
            "spread_no": spread_no,
            "complement_skew": skew,
            "spread_threshold": self._cfg.max_spread_warn,
            "skew_threshold": self._cfg.max_complement_skew,
            "reject": None,
        }

        if spread_yes > self._cfg.max_spread_warn:
            reason = f"wide_spread_reject(spread_yes={spread_yes:.4f} > thr={self._cfg.max_spread_warn:.4f})"
            self._last_sanity_details["reject"] = reason
            log.warning(
                "Market sanity REJECT: %s bid=%.4f ask=%.4f spread=%.4f",
                reason, market.best_bid_yes, market.best_ask_yes, spread_yes,
            )
            return reason

        if skew > self._cfg.max_complement_skew:
            reason = f"complement_skew_reject(skew={skew:.4f} > thr={self._cfg.max_complement_skew:.4f})"
            self._last_sanity_details["reject"] = reason
            log.warning(
                "Market sanity REJECT: %s yes_mid=%.4f no_mid=%.4f sum=%.4f skew=%.4f",
                reason, yes_mid, no_mid, midpoint_sum, skew,
            )
            return reason

        return None

    def _refresh_market(self, now: float) -> None:
        market_info = self._discovery.get_current_market()
        if market_info is None:
            self._current_market = None
            self._last_market_refresh = now
            return

        snap = build_market_snapshot(market_info, self._poly_client)
        if snap:
            self._current_market = snap
            self._current_market_age = now
            log.debug(
                "Market refreshed: bid_yes=%.4f ask_yes=%.4f ste=%.0fs",
                snap.best_bid_yes, snap.best_ask_yes, snap.seconds_to_expiry,
            )
        self._last_market_refresh = now

    def _on_new_window(self, new_window_end: float, boundary_ts: float) -> None:
        """
        Called when the local clock crosses into a new 5-minute window.

        boundary_ts: unix ts of the boundary (= previous window_end = new window_start).
                     Used to look up the exact Binance snapshot for resolution.
        new_window_end: end timestamp of the new (current) window.
        """
        log.info(
            "Window boundary crossed: %.0f -> %.0f (boundary_ts=%.0f)",
            self._last_window_ts, new_window_end, boundary_ts,
        )

        # Resolve boundary outcome — needed for both paper trades and shadow quote attribution.
        # Use Binance snapshot nearest to boundary_ts for accurate resolution.
        resolve_price, snapshot_ts = self._binance.get_snapshot_near(boundary_ts)
        if resolve_price is not None and self._window_open_price is not None:
            outcome_yes = 1.0 if resolve_price > self._window_open_price else 0.0
            log.info(
                "Resolve: window_open=%.2f resolve_price=%.2f "
                "snapshot_ts=%.3f boundary_ts=%.3f outcome=%s",
                self._window_open_price, resolve_price,
                snapshot_ts, boundary_ts,
                "YES" if outcome_yes == 1.0 else "NO",
            )
        else:
            outcome_yes = 0.5  # fallback: neutral
            log.warning(
                "Resolve fallback: no Binance snapshot near boundary_ts=%.3f",
                boundary_ts,
            )

        if self._paper_exec.get_open_trades():
            resolved = self._paper_exec.resolve_all_pending(outcome_yes, self._bankroll)
            for trade in resolved:
                self._metrics.on_trade_resolved(trade, self._bankroll.bankroll)
                if trade.pnl > 0:
                    self._risk_mgr.on_win(self._risk_state)
                elif trade.pnl < 0:
                    self._risk_mgr.on_loss(self._risk_state)
                self._log_paper_trade(trade, "resolve")

        # Freeze current Binance mid as the OPEN snapshot for the new window
        btc_now, _, _ = self._binance.get_snapshot()
        if btc_now is not None:
            self._window_open_price = btc_now
            self._binance.set_window_open(btc_now)
            log.info(
                "New window open snapshot frozen: btc=%.2f window_start=%.0f",
                btc_now, boundary_ts,
            )
        else:
            self._window_open_price = None

        # Phase 2: resolve shadow quotes with boundary outcome before resetting probe
        if self._cfg.mode == "paper_with_shadow_probe":
            boundary_quotes = self._shadow_probe.resolve_boundary(outcome_yes)
            for bq in boundary_quotes:
                self._metrics.on_boundary_resolved(bq)
                self._log_shadow_quote(bq, event="boundary_resolved")

        # Reset per-window state
        self._risk_mgr.on_window_reset(self._risk_state)
        self._shadow_probe.reset()
        self._discovery.invalidate()

        # Persist state
        self._store.update({
            "bankroll": self._bankroll.bankroll,
            "peak_bankroll": self._bankroll.peak_bankroll,
            "total_pnl": self._bankroll.total_pnl,
            "drawdown": self._bankroll.drawdown,
            "cooldown_windows_remaining": self._risk_state.cooldown_windows_remaining,
            "consecutive_losses": self._risk_state.consecutive_losses,
            "last_window_ts": new_window_end,
        })
        self._store.save()

        self._last_window_ts = new_window_end

        self._structured.log("bankroll", {
            "event": "window_boundary",
            "boundary_ts": boundary_ts,
            "new_window_end": new_window_end,
            "bankroll": self._bankroll.bankroll,
            "peak_bankroll": self._bankroll.peak_bankroll,
            "total_pnl": self._bankroll.total_pnl,
            "drawdown": self._bankroll.drawdown,
        })

    # ------------------------------------------------------------------ #
    # Logging helpers
    # ------------------------------------------------------------------ #

    def _log_signal(self, d) -> None:
        """
        Write signal decision to JSONL with full diagnostic schema.

        Fields that require fair_prob computation are written as null when
        d.fair_computed is False (early-reject path before engine ran).
        Fields always available (btc_mid, delta, implied_yes, etc.) are
        always written.
        """
        # Analytical fields are available when engine ran this tick OR values come from cache
        fc = d.fair_computed_fresh or d.context_from_cache
        sd = self._last_sanity_details  # market sanity numerics from current tick

        # Helper: return value or null depending on whether analytical fields are available
        def _or_null(v, precision: int = 5):
            return round(v, precision) if fc else None

        # Helper: round only if value is not None — sd fields may legitimately be None
        # (e.g. degenerate book path sets spread/mid/skew to None).
        # sd.get(key, 0.0) does NOT protect against this: if key exists with None,
        # .get() returns None, not the default.
        def _rn(v, precision: int = 5):
            return round(v, precision) if v is not None else None

        self._structured.log("signals", {
            # ── Identity ──────────────────────────────────────────────
            "ts": d.ts,
            "window_ts": d.window_ts,
            "market_slug": sd.get("slug", "") or getattr(self._current_market, "slug", ""),
            "lane": d.lane,
            # ── Window position ───────────────────────────────────────
            "elapsed_from_window_start": round(d.elapsed_from_window_start, 1),
            "seconds_to_expiry": round(d.seconds_to_expiry, 1),
            # ── BTC price (dual representation) ──────────────────────
            "btc_mid": round(d.btc_mid, 2),
            "window_open": round(d.window_open, 2),
            "delta_raw_fraction": round(d.delta_raw_fraction, 6),
            "delta_pct_display": round(d.delta_pct_display, 4),
            "realized_vol_60s": round(d.realized_vol_60s, 8),
            # ── Order book snapshot ───────────────────────────────────
            "yes_bid": sd.get("yes_bid"),
            "yes_ask": sd.get("yes_ask"),
            "no_bid":  sd.get("no_bid"),
            "no_ask":  sd.get("no_ask"),
            "yes_mid": _rn(sd.get("yes_mid"), 4),
            "no_mid":  _rn(sd.get("no_mid"),  4),
            "spread_yes":      _rn(sd.get("spread_yes"),      5),
            "spread_no":       _rn(sd.get("spread_no"),       5),
            "complement_skew": _rn(sd.get("complement_skew"), 5),
            # ── Market sanity ─────────────────────────────────────────
            "sanity_status": "reject" if sd.get("reject") else "pass",
            "sanity_reason": sd.get("reject"),
            # ── Fair vs implied ───────────────────────────────────────
            # fair_yes_prob: null when engine hasn't run and no cache available
            "fair_yes_prob": _or_null(d.fair_yes_prob, 4),
            # implied_yes_prob: always from live order book, never null
            "implied_yes_prob": round(d.implied_yes_prob, 4) if d.implied_yes_prob > 0 else None,
            "fair_computed": fc,
            "fair_computed_fresh": d.fair_computed_fresh,
            "context_from_cache": d.context_from_cache,
            # ── Edge (null when not computed) ─────────────────────────
            "raw_edge_yes":       _or_null(d.raw_edge_yes),
            "raw_edge_no":        _or_null(d.raw_edge_no),
            "after_fee_edge_yes": _or_null(d.after_fee_edge_yes),
            "after_fee_edge_no":  _or_null(d.after_fee_edge_no),
            # Generic fee = fee for the executed side; null for NO_TRADE (chosen_side is None)
            "fee_per_share":  round(d.fee_per_share, 8) if (fc and d.chosen_side is not None) else None,
            "effective_rate": round(d.effective_rate, 6) if (fc and d.chosen_side is not None) else None,
            # Side-specific fees at actual market ask prices (null when not computed)
            "fee_per_share_yes":      _or_null(d.fee_per_share_yes, 8),
            "fee_per_share_no":       _or_null(d.fee_per_share_no, 8),
            "effective_fee_rate_yes": _or_null(d.effective_fee_rate_yes, 6),
            "effective_fee_rate_no":  _or_null(d.effective_fee_rate_no, 6),
            # ── Confidence (null when not computed) ───────────────────
            "confidence_score":      _or_null(d.confidence_score, 4),
            "confidence_components": d.confidence_components if fc else None,
            # ── Regime / pattern (UNKNOWN when not computed) ──────────
            "regime":  d.regime,
            "pattern": d.pattern,
            # ── Decision ─────────────────────────────────────────────
            "action":       d.action,
            "chosen_side":  d.chosen_side,
            "reason":       d.reason,
            # ── Context ──────────────────────────────────────────────
            "bankroll":    round(d.bankroll, 4),
            "data_age_ms": round(d.data_age_ms, 1),
        })

    def _update_ui_state(
        self, now, btc_mid, market, current_window_start,
        current_window_end, elapsed, taker_decision, binance_age_ms,
        realized_vol: float = 0.0,
    ) -> None:
        """Push a snapshot to the UI state object for rendering."""
        state = self._ui_state
        sd = self._last_sanity_details
        d = taker_decision
        state.update(
            ts=now,
            mode=self._cfg.mode,
            btc_mid=btc_mid,
            window_start=current_window_start,
            window_end=current_window_end,
            elapsed_from_window_start=elapsed,
            bankroll=self._bankroll.bankroll,
            paper_pnl=self._bankroll.total_pnl,
            peak_bankroll=self._bankroll.peak_bankroll,
            drawdown=self._bankroll.drawdown,
            best_bid_yes=market.best_bid_yes,
            best_ask_yes=market.best_ask_yes,
            best_bid_no=market.best_bid_no,
            best_ask_no=market.best_ask_no,
            implied_yes_prob=market.implied_yes_prob,
            window_open_price=self._window_open_price or 0.0,
            last_decision=taker_decision,
            metrics=self._metrics.snapshot(),
            open_trades=list(self._paper_exec.get_open_trades().values()),
            win_count=self._metrics._data.taker_win_count,
            loss_count=self._metrics._data.taker_loss_count,
            binance_age_ms=binance_age_ms,
            cooldown_remaining=self._risk_state.cooldown_windows_remaining,
            consecutive_losses=self._risk_state.consecutive_losses,
            # Market microstructure diagnostics
            yes_mid=sd.get("yes_mid", market.implied_yes_prob),
            no_mid=sd.get("no_mid", 0.5),
            spread_yes=sd.get("spread_yes", market.best_ask_yes - market.best_bid_yes),
            spread_no=sd.get("spread_no", market.best_ask_no - market.best_bid_no),
            midpoint_sum=sd.get("midpoint_sum", 0.0),
            complement_skew=sd.get("complement_skew", 0.0),
            sanity_reject=sd.get("reject"),
            # BTC delta (explicit units)
            delta_raw_fraction=getattr(d, "delta_raw_fraction", 0.0),
            delta_pct_display=getattr(d, "delta_pct_display", 0.0),
            realized_vol_60s=realized_vol,
            # Market slug
            market_slug=getattr(market, "slug", ""),
            # Window config
            entry_start_sec=self._cfg.entry_start_sec,
            entry_end_sec=self._cfg.entry_end_sec,
        )

    def _log_paper_trade(self, trade, event: str) -> None:
        from dataclasses import asdict
        record = asdict(trade)
        record["event"] = event
        self._structured.log("paper_trades", record)

    def _log_shadow_quote(self, quote, event: str = "lifecycle") -> None:
        """
        Write shadow quote event to JSONL.
        Phase 2: enriched record with full maker evaluation context.
        All fields serialized; None values are preserved as null (not 0.0).
        """
        self._structured.log("shadow_quotes", {
            # Identity
            "event": event,
            "quote_id": quote.quote_id,
            "ts": quote.ts,
            "window_ts": quote.window_ts,
            "market_slug": quote.market_slug,
            # Quote placement
            "side": quote.side,
            "quote_price": quote.quote_price,
            "best_bid": quote.best_bid,
            "best_ask": quote.best_ask,
            "tick_size": quote.tick_size,
            "crossed": quote.crossed,
            "fill_status": quote.fill_status,
            "reject_reason": quote.reject_reason,
            # Window context at quote time
            "seconds_to_expiry": quote.seconds_to_expiry,
            "elapsed_from_window_start": quote.elapsed_from_window_start,
            # Market and model state at quote time
            "implied_yes_prob": quote.implied_yes_prob,
            "fair_yes_prob": quote.fair_yes_prob,
            "delta_raw_fraction": quote.delta_raw_fraction,
            "delta_pct_display": round(quote.delta_pct_display, 4),
            "confidence_score": quote.confidence_score,
            "confidence_components": quote.confidence_components,
            "regime": quote.regime,
            "pattern": quote.pattern,
            # Maker economics at quote time
            "intended_passive_edge": quote.intended_passive_edge,
            "intended_notional": quote.intended_notional,
            # Fill lifecycle
            "fill_ts": quote.fill_ts,
            "fill_mid": quote.fill_mid,
            "next_mid_after_fill": quote.next_mid_after_fill,
            "adverse_move_after_fill": quote.adverse_move_after_fill,
            "favorable_move_after_fill": quote.favorable_move_after_fill,
            # Boundary resolution
            "boundary_outcome_yes": quote.boundary_outcome_yes,
            "boundary_outcome_for_side": quote.boundary_outcome_for_side,
            "maker_pnl_if_held": quote.maker_pnl_if_held,
            "maker_edge_realized_vs_expected": quote.maker_edge_realized_vs_expected,
        })

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    def _shutdown(self) -> None:
        log.info("Shutting down…")
        if self._ui is not None:
            try:
                self._ui.stop()
            except Exception:
                pass
        self._binance.stop()
        snap = self._metrics.snapshot()
        log.info("Final metrics: %s", snap)
        self._store.update({
            "bankroll": self._bankroll.bankroll,
            "peak_bankroll": self._bankroll.peak_bankroll,
            "total_pnl": self._bankroll.total_pnl,
            "drawdown": self._bankroll.drawdown,
            "cooldown_windows_remaining": self._risk_state.cooldown_windows_remaining,
            "consecutive_losses": self._risk_state.consecutive_losses,
        })
        self._store.save()
        self._structured.close()

        # Phase 3: write session_summary.json + session_summary.txt
        try:
            import datetime as _dt
            session_log_dir = self._cfg.log_dir_for_session(self._session_ts)
            ts_str = _dt.datetime.utcfromtimestamp(self._session_ts).strftime("%Y%m%d_%H%M%S")
            summary = build_session_summary(session_log_dir, self._cfg, session_ts=ts_str)
            verdict = summary.get("verdict", {})
            log.info(
                "Session summary written | taker=%s maker=%s live=%s",
                verdict.get("taker_status"),
                verdict.get("maker_status"),
                verdict.get("live_candidate_status"),
            )
        except Exception as exc:
            log.warning("Session summary generation failed: %s", exc)

        log.info("Shutdown complete. Bankroll: %.4f USDC", self._bankroll.bankroll)


def main() -> None:
    config_path = os.environ.get("POLYBOT_CONFIG")
    bot = PolybotV2(config_path)

    def _sigterm(sig, frame):
        log.info("SIGTERM received")
        bot._running = False

    signal.signal(signal.SIGTERM, _sigterm)
    bot.run()


if __name__ == "__main__":
    main()
