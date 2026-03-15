"""
polybot_v2 Phase 1 – main entry point.

Two-lane paper trading system:
  Lane 1: Selective Taker Paper Engine
  Lane 2: Maker Shadow Probe

No real orders are placed.
"""

from __future__ import annotations

import logging
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

_LOOP_INTERVAL = 5.0       # seconds between main loop iterations
_MARKET_REFRESH = 15.0     # seconds between Polymarket market data refreshes
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

        # Logging
        setup_console_logger(self._cfg.log_level)
        self._structured = StructuredLogger(
            self._cfg.log_dir,
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
            taker_fee_rate=self._cfg.taker_fee_rate,
            maker_rebate_rate=self._cfg.maker_rebate_rate,
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

        # Runtime state
        self._last_window_ts: float = self._store.last_window_ts
        self._last_market_refresh: float = 0.0
        self._last_metrics_log: float = 0.0
        self._current_market: Optional[MarketSnapshot] = None
        self._current_market_age: float = 0.0
        self._window_open_price: Optional[float] = None
        self._running = False

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

        # Refresh market data periodically
        if now - self._last_market_refresh > _MARKET_REFRESH:
            self._refresh_market(now)

        if self._current_market is None:
            log.debug("No active market, waiting…")
            return

        # Window boundary detection
        market = self._current_market
        if market.window_end_ts != self._last_window_ts:
            self._on_new_window(market.window_end_ts)

        # Get Binance snapshot
        btc_mid, btc_ts, realized_vol = self._binance.get_snapshot()
        if btc_mid is None:
            log.warning("No Binance price, skipping tick")
            return

        binance_age_ms = (now - btc_ts) * 1000
        polymarket_age_ms = (now - market.fetched_at) * 1000

        # Set / maintain window open price
        if self._window_open_price is None:
            self._window_open_price = btc_mid
            self._binance.set_window_open(btc_mid)
            log.info("Window open price set: %.2f", btc_mid)

        price_snap = PriceSnapshot(
            btc_mid=btc_mid,
            timestamp=btc_ts,
            realized_vol_60s=realized_vol,
        )

        # Lane 1: Selective Taker
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

            # Check adverse move from previous fill
            adverse = self._shadow_probe.check_adverse_move(market)
            if adverse:
                self._log_shadow_quote(adverse)

        # Periodic metrics log
        if now - self._last_metrics_log > _METRICS_LOG_INTERVAL:
            snap = self._metrics.snapshot()
            log.info("METRICS: %s", snap)
            self._last_metrics_log = now

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

    def _on_new_window(self, new_window_ts: float) -> None:
        log.info("New window detected: %.0f → %.0f", self._last_window_ts, new_window_ts)

        # Resolve any open paper trades from previous window
        if self._last_window_ts > 0 and self._paper_exec.get_open_trades():
            # Determine outcome: YES wins if BTC ended above window open
            btc_mid, _, _ = self._binance.get_snapshot()
            if btc_mid is not None and self._window_open_price is not None:
                outcome_yes = 1.0 if btc_mid > self._window_open_price else 0.0
            else:
                outcome_yes = 0.5  # fallback: neutral

            resolved = self._paper_exec.resolve_all_pending(outcome_yes, self._bankroll)
            for trade in resolved:
                self._metrics.on_trade_resolved(trade, self._bankroll.bankroll)
                if trade.pnl > 0:
                    self._risk_mgr.on_win(self._risk_state)
                elif trade.pnl < 0:
                    self._risk_mgr.on_loss(self._risk_state)
                self._log_paper_trade(trade, "resolve")

        # Reset per-window state
        self._risk_mgr.on_window_reset(self._risk_state)
        self._shadow_probe.reset()
        self._window_open_price = None
        self._discovery.invalidate()

        # Persist state
        self._store.update({
            "bankroll": self._bankroll.bankroll,
            "peak_bankroll": self._bankroll.peak_bankroll,
            "total_pnl": self._bankroll.total_pnl,
            "drawdown": self._bankroll.drawdown,
            "cooldown_windows_remaining": self._risk_state.cooldown_windows_remaining,
            "consecutive_losses": self._risk_state.consecutive_losses,
            "last_window_ts": new_window_ts,
        })
        self._store.save()

        self._last_window_ts = new_window_ts

        self._structured.log("bankroll", {
            "event": "window_end",
            "window_ts": new_window_ts,
            "bankroll": self._bankroll.bankroll,
            "peak_bankroll": self._bankroll.peak_bankroll,
            "total_pnl": self._bankroll.total_pnl,
            "drawdown": self._bankroll.drawdown,
        })

    # ------------------------------------------------------------------ #
    # Logging helpers
    # ------------------------------------------------------------------ #

    def _log_signal(self, d) -> None:
        self._structured.log("signals", {
            "ts": d.ts,
            "window_ts": d.window_ts,
            "lane": d.lane,
            "seconds_to_expiry": d.seconds_to_expiry,
            "btc_mid": d.btc_mid,
            "window_open": d.window_open,
            "delta_pct": d.delta_pct,
            "realized_vol_60s": d.realized_vol_60s,
            "fair_yes_prob": d.fair_yes_prob,
            "implied_yes_prob": d.implied_yes_prob,
            "raw_edge_yes": d.raw_edge_yes,
            "raw_edge_no": d.raw_edge_no,
            "after_fee_edge_yes": d.after_fee_edge_yes,
            "after_fee_edge_no": d.after_fee_edge_no,
            "action": d.action,
            "chosen_side": d.chosen_side,
            "reason": d.reason,
            "bankroll": d.bankroll,
            "data_age_ms": d.data_age_ms,
            "regime": d.regime,
            "pattern": d.pattern,
        })

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
