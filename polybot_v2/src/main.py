"""
polybot_v2 Phase 1 – main entry point.

Two-lane paper trading system:
  Lane 1: Selective Taker Paper Engine
  Lane 2: Maker Shadow Probe

No real orders are placed.
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
            "polybot_v2 Phase 1 starting | mode=%s bankroll=%.2f USDC",
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

        if self._current_market is None:
            log.debug("No active market, waiting…")
            return

        market = self._current_market

        # Get Binance snapshot — compute age with a FRESH time.time() taken after
        # the snapshot call.  Using the stale 'now' from tick-start risks negative
        # data_age_ms if the WS callback fires between 'now = time.time()' and
        # 'get_snapshot()' and updates _last_ts to a value > now.
        btc_mid, btc_ts, realized_vol = self._binance.get_snapshot()
        _snap_read_ts = time.time()
        if btc_mid is None:
            log.warning("No Binance price, skipping tick")
            return

        binance_age_ms = (_snap_read_ts - btc_ts) * 1000   # always >= 0
        polymarket_age_ms = (_snap_read_ts - market.fetched_at) * 1000

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

        # Lane 1: Selective Taker — hard sanity gate first
        if sanity_reject:
            # Synthetic NO_TRADE with spread/skew reject reason
            from models import SignalDecision as _SD
            taker_decision = _SD(
                ts=now, window_ts=current_window_end,
                lane="selective_taker", action="NO_TRADE",
                chosen_side=None, reason=sanity_reject,
                seconds_to_expiry=market.seconds_to_expiry,
                elapsed_from_window_start=elapsed_in_window,
                btc_mid=btc_mid,
                window_open=self._window_open_price or 0.0,
                bankroll=self._bankroll.bankroll,
                data_age_ms=binance_age_ms,
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

        if taker_decision.action == "PAPER_TRADE":
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
        yes_mid = market.implied_yes_prob
        no_mid = (market.best_bid_no + market.best_ask_no) / 2.0 if (
            market.best_bid_no > 0 and market.best_ask_no > 0
        ) else 0.5
        spread_yes = market.best_ask_yes - market.best_bid_yes
        spread_no = market.best_ask_no - market.best_bid_no
        midpoint_sum = yes_mid + no_mid
        skew = abs(midpoint_sum - 1.0)

        # Always update diagnostics so UI shows current numbers
        self._last_sanity_details = {
            "yes_bid": market.best_bid_yes,
            "yes_ask": market.best_ask_yes,
            "no_bid": market.best_bid_no,
            "no_ask": market.best_ask_no,
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
            "Window boundary crossed: %.0f → %.0f (boundary_ts=%.0f)",
            self._last_window_ts, new_window_end, boundary_ts,
        )

        # Resolve any open paper trades from previous window.
        # Use Binance snapshot nearest to boundary_ts for accurate resolution.
        if self._paper_exec.get_open_trades():
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
        """Write signal decision to JSONL with unified schema (no legacy mixing)."""
        self._structured.log("signals", {
            # Identity
            "ts": d.ts,
            "window_ts": d.window_ts,
            "lane": d.lane,
            # Window position
            "elapsed_from_window_start": round(d.elapsed_from_window_start, 1),
            "seconds_to_expiry": round(d.seconds_to_expiry, 1),
            # BTC price — dual representation to eliminate unit ambiguity
            "btc_mid": d.btc_mid,
            "window_open": d.window_open,
            "delta_raw_fraction": round(d.delta_raw_fraction, 6),   # e.g. 0.001000
            "delta_pct_display": round(d.delta_pct_display, 4),     # e.g. 0.1000 (%)
            "delta_pct": round(d.delta_pct, 6),                     # legacy alias
            "realized_vol_60s": round(d.realized_vol_60s, 8),
            # Fair vs implied
            "fair_yes_prob": round(d.fair_yes_prob, 4),
            "implied_yes_prob": round(d.implied_yes_prob, 4),
            # Edge
            "raw_edge_yes": round(d.raw_edge_yes, 5),
            "raw_edge_no": round(d.raw_edge_no, 5),
            "after_fee_edge_yes": round(d.after_fee_edge_yes, 5),
            "after_fee_edge_no": round(d.after_fee_edge_no, 5),
            # Fee (pre-computed in SignalDecision)
            "fee_per_share": d.fee_per_share,
            "effective_rate": d.effective_rate,
            # Signal quality
            "confidence_score": d.confidence_score,
            "regime": d.regime,
            "pattern": d.pattern,
            # Decision
            "action": d.action,
            "chosen_side": d.chosen_side,
            "reason": d.reason,
            # Context
            "bankroll": round(d.bankroll, 4),
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

    def _log_shadow_quote(self, quote) -> None:
        from dataclasses import asdict
        record = asdict(quote)
        self._structured.log("shadow_quotes", record)

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
