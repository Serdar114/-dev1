"""
app/runner.py — Main measurement loop coordinator.

Owns all threads. Coordinates discovery, feeds, analysis, and logging.
Writes the no_trade_reasons and lifecycle_events into SystemState each tick.

Measurement-only flow:
  1. Start Chainlink polling thread.
  2. Start Binance WebSocket thread.
  3. Start discovery loop thread.
  4. Main tick loop: evaluate no-trade rules, record hypotheticals, log.

Window rollover:
  - Detected in discovery loop.
  - Previous window is resolved via Chainlink.
  - New market is discovered.
  - Market WebSocket is resubscribed.
  - State is reset for new window.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from book.orderbook_state import fetch_and_seed
from discovery.market_discovery import MarketDiscovery
from discovery.market_registry import MarketRegistry
from feeds.binance_client import BinanceClient
from feeds.chainlink_client import ChainlinkClient
from feeds.market_ws_client import MarketWsClient
from feeds.rtds_chainlink_client import RtdsChainlinkClient
from loggingx.event_logger import EventLogger
from loggingx.schemas import (
    NoTradeEvent,
    ResolutionEvent,
    SystemStartEvent,
    WindowEndEvent,
    WindowStartEvent,
)
from metadata.market_metadata import MarketMetadataFetcher
from paper.hypothetical_entry import record_both_sides
from paper.paper_executor import PaperExecutor
from signals.feature_builder import build as build_features
from signals.no_trade_rules import evaluate as eval_no_trade
from state import MarketRecord, SystemState, WindowState
from truth.chainlink_buffer import ChainlinkBuffer
from truth.resolution_truth import resolve_window
from truth.window_clock import current_window_start, window_bounds

log = logging.getLogger(__name__)


class Runner:
    """
    Coordinates all runtime components.
    Call .start() to launch background threads and begin the main tick loop.
    Returns when UI closes or KeyboardInterrupt is raised.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._state = SystemState(mode=config.get("modes", {}).get("default", "measurement"))
        self._event_logger = EventLogger(config["measurement"]["log_file"])

        # Close-capture buffer: receives every Chainlink observation; used for resolution
        self._buffer = ChainlinkBuffer(max_entries=180)

        # Feeds
        # RTDS = primary Chainlink source; polygon_rpc = audit / fallback
        self._rtds = RtdsChainlinkClient(config, self._buffer, self._event_logger)
        self._chainlink = ChainlinkClient(config, self._buffer, self._event_logger)
        self._binance = BinanceClient(config, self._event_logger)
        self._market_ws = MarketWsClient(config, self._event_logger)

        self._discovery = MarketDiscovery(config, self._event_logger)
        self._registry = MarketRegistry()
        self._metadata_fetcher = MarketMetadataFetcher(config, self._event_logger)
        self._paper = PaperExecutor(config)

        self._stop_event = threading.Event()

        # Tracks Chainlink price at start of current window for resolution
        self._window_entry_price: Optional[float] = None
        self._last_hypo_window: int = 0
        # Tracks last window for which readiness summary was logged (once per window)
        self._last_readiness_window: int = 0

    @property
    def state(self) -> SystemState:
        return self._state

    # -----------------------------------------------------------------------
    # Startup
    # -----------------------------------------------------------------------

    def start(self, run_ui: bool = True) -> None:
        """Start all threads. Blocks until UI closed or Ctrl-C."""
        log.info("Runner starting in mode=%s", self._state.mode)
        self._event_logger.log(SystemStartEvent(
            mode=self._state.mode,
            config_summary=f"chainlink_max_age={self._config['chainlink']['max_age_seconds']}s",
        ))

        # Log startup readiness requirements.
        # System stays DEGRADED until BOTH conditions are true:
        #   1. RTDS connected and delivering oracle-timestamped observations
        #   2. Gamma feeSchedule.rate parsed (fee_provenance == "canonical_market_object")
        # This is enforced by system_status_label() on every tick.
        rtds_url = self._config.get("rtds", {}).get("ws_url", "wss://ws-live-data.polymarket.com")
        fallback_fee = float(self._config.get("paper", {}).get("default_taker_fee_rate", 0.072))
        log.info(
            "Startup readiness: DEGRADED until RTDS(%s) connected AND canonical feeSchedule parsed. "
            "Fallback fee=%.4f (fallback_config). TRUTH-TIGHT requires fee_provenance=canonical_market_object.",
            rtds_url, fallback_fee,
        )
        with self._state._lock:
            self._state.push_lifecycle(
                f"startup: DEGRADED until rtds_connected + fee_provenance=canonical_market_object"
            )

        # Launch background threads
        # RTDS starts first — it is the primary Chainlink source
        self._rtds.start()
        # Polygon RPC runs as audit/fallback; also populates buffer
        self._chainlink.start()
        self._binance.start()
        t_disc = threading.Thread(target=self._discovery_loop, daemon=True, name="discovery")
        t_disc.start()

        # Bootstrap Binance via REST while WS connects
        self._binance.rest_fetch_once()

        if run_ui:
            self._run_ui()
        else:
            self._tick_loop()

    def stop(self) -> None:
        self._stop_event.set()
        self._rtds.stop()
        self._chainlink.stop()
        self._binance.stop()
        self._market_ws.stop()
        self._event_logger.flush_and_stop()

    # -----------------------------------------------------------------------
    # Discovery loop (background thread)
    # -----------------------------------------------------------------------

    def _discovery_loop(self) -> None:
        """Runs continuously. Triggers rediscovery on window rollover."""
        while not self._stop_event.is_set():
            try:
                self._check_and_rediscover()
            except Exception as exc:
                log.error("Discovery loop error: %s", exc)
            self._stop_event.wait(timeout=15)

    def _check_and_rediscover(self) -> None:
        if not self._registry.needs_rediscovery():
            return

        log.info("Rediscovery triggered")

        # Resolve previous window before switching
        prev_market = self._registry.get()
        if prev_market and self._window_entry_price is not None:
            self._resolve_previous_window(prev_market)

        # Discover new market
        new_market = self._discovery.discover()
        if new_market is None:
            with self._state._lock:
                self._state.market = None
                self._state.metadata = None
                self._state.push_lifecycle("discovery_failed: no BTC 5m market found")
            self._registry.clear()
            return

        # Fetch metadata — pass raw Gamma response so fee can be sourced from feeSchedule
        fallback_fee = float(self._config.get("paper", {}).get("default_taker_fee_rate", 0.072))
        metadata = self._metadata_fetcher.fetch(
            new_market.condition_id,
            fallback_fee,
            gamma_data=new_market.raw_gamma_response,
        )

        # Seed initial orderbook from REST before WS connects
        fetch_and_seed(
            self._config["polymarket"]["clob_base"],
            new_market.up_token_id,
            new_market.down_token_id,
            self._state,
        )

        # Subscribe market WebSocket
        self._market_ws.subscribe(new_market.up_token_id, new_market.down_token_id)

        # Record window entry price: prefer RTDS snapshot, fall back to Polygon RPC
        rtds_snap = self._rtds.snapshot()
        cl_snap = self._chainlink.snapshot()
        if rtds_snap.price is not None:
            self._window_entry_price = rtds_snap.price
        elif cl_snap.price is not None:
            self._window_entry_price = cl_snap.price
        else:
            self._window_entry_price = None  # explicitly None — tracked in resolution

        # Update window clock
        ws, we = window_bounds()

        # Update state
        with self._state._lock:
            self._state.market = new_market
            self._state.metadata = metadata
            self._state.window = WindowState(start=ws, end=we)
            self._state.push_lifecycle(
                f"market_found slug={new_market.slug} src={new_market.discovery_source}"
            )
            if metadata.is_complete():
                self._state.push_lifecycle(
                    f"metadata_ready tick={metadata.tick_size} fee={metadata.taker_fee_rate:.4f}({metadata.fee_provenance})"
                )
            else:
                self._state.push_lifecycle(
                    f"metadata_incomplete readiness={metadata.readiness_label()}"
                )

        self._registry.set(new_market)

        self._event_logger.log(WindowStartEvent(
            window_id=ws,
            window_start=ws,
            window_end=we,
        ))
        log.info("New window started: %d  market=%s", ws, new_market.slug)

    def _resolve_previous_window(self, market: MarketRecord) -> None:
        """Attempt Chainlink-based resolution of the just-completed window."""
        max_age = float(self._config["chainlink"]["max_age_seconds"])
        resolution = resolve_window(
            window_start=market.window_start,
            price_at_start=self._window_entry_price,
            chainlink=self._chainlink,
            max_oracle_age=max_age,
            buffer=self._buffer,
        )
        self._event_logger.log(ResolutionEvent(
            window_id=market.window_start,
            resolved_window_start=market.window_start,
            price_at_start=resolution.price_at_start,
            price_at_end=resolution.price_at_end,
            chainlink_age_at_resolution=resolution.oracle_age_at_resolution,
            outcome=resolution.outcome,
            status=resolution.status,
            close_capture_timestamp=resolution.close_capture_timestamp,
            close_capture_source=resolution.close_capture_source,
            seconds_before_window_end=resolution.seconds_before_window_end,
        ))
        log.info(
            "Resolution window=%d status=%s outcome=%s",
            market.window_start, resolution.status, resolution.outcome,
        )
        with self._state._lock:
            self._state.push_lifecycle(
                f"resolution window={market.window_start} status={resolution.status} outcome={resolution.outcome}"
            )

        # If paper mode, resolve paper trades
        if resolution.status == "resolved_canonical" and resolution.outcome:
            self._paper.resolve(
                market.window_start,
                resolution.outcome,
                resolution.price_at_end or 0.0,
            )
        else:
            self._paper.mark_unresolved(market.window_start)

    # -----------------------------------------------------------------------
    # Tick loop (runs in main thread when no UI)
    # -----------------------------------------------------------------------

    def _tick_loop(self) -> None:
        """Simple tick loop for non-UI mode."""
        while not self._stop_event.is_set():
            self._tick()
            time.sleep(1.0)

    def _tick(self) -> None:
        """
        Single tick: inject feed state, evaluate no-trade rules,
        record hypotheticals, update state for display.
        """
        # Inject feed snapshots into state.
        # RTDS is primary Chainlink source; Polygon RPC only writes if RTDS has no fresh data.
        rtds_max_age = float(self._config["chainlink"].get("rtds_fallback_age_seconds", 30))
        if self._rtds.is_healthy(rtds_max_age):
            self._rtds.inject_state(self._state)
        else:
            # RTDS unhealthy or not yet connected — use Polygon RPC as fallback
            self._chainlink.inject_state(self._state)
        self._binance.inject_state(self._state)
        self._market_ws.inject_state(self._state)

        # Update window clock
        ws, we = window_bounds()
        with self._state._lock:
            if self._state.window.start != ws:
                self._state.window = WindowState(start=ws, end=we)

        # Evaluate no-trade rules under lock
        with self._state._lock:
            reasons = eval_no_trade(self._state, self._config)
            self._state.no_trade_reasons = reasons
            features = build_features(self._state)
            meta = self._state.metadata
            window_id = self._state.window.start

        # One readiness summary per window at INFO — shows what's blocking hypo entries
        if window_id != self._last_readiness_window and window_id != 0:
            self._last_readiness_window = window_id
            self._log_window_readiness(window_id, reasons, features, meta)

        # Log no-trade tick (throttled: only log when reasons change)
        if reasons:
            self._event_logger.log(NoTradeEvent(
                window_id=window_id,
                reasons=reasons,
            ))

        # Record hypothetical entries when no-trade rules pass
        fallback_fee = float(self._config.get("paper", {}).get("default_taker_fee_rate", 0.072))
        if not reasons and window_id != self._last_hypo_window:
            hypo_events = record_both_sides(features, meta, fallback_fee, window_id)
            for ev in hypo_events:
                self._event_logger.log(ev)
            if hypo_events:
                self._last_hypo_window = window_id
                with self._state._lock:
                    side = hypo_events[0].side
                    price = hypo_events[0].entry_price
                    self._state.push_lifecycle(
                        f"hypothetical_entry recorded side={side} @ {price:.4f}"
                    )

    # -----------------------------------------------------------------------
    # UI integration
    # -----------------------------------------------------------------------

    def _run_ui(self) -> None:
        """Launch tick loop in background, run UI in main thread."""
        t_tick = threading.Thread(target=self._tick_loop_wrapper, daemon=True, name="tick")
        t_tick.start()

        from app.terminal_ui import TerminalUI
        ui = TerminalUI()
        try:
            ui.run(self._state, self._config)
        except Exception as exc:
            log.error("UI error: %s", exc)
        finally:
            self.stop()

    def _tick_loop_wrapper(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.error("Tick error: %s", exc)
            time.sleep(1.0)

    def _log_window_readiness(self, window_id: int, reasons: list, features, meta) -> None:
        """Emit one INFO line per window summarising measurement readiness."""
        cl_s = "ok" if features.chainlink_price is not None else "MISSING"
        meta_s = meta.readiness_label() if meta is not None else "MISSING"
        up_s = "ok" if features.up_best_ask is not None else "MISSING"
        dn_s = "ok" if features.dn_best_ask is not None else "MISSING"
        ps = features.pair_sum_ask
        ps_s = f"{ps:.4f}" if ps is not None else "unavail"
        if reasons:
            status_s = "BLOCKED:" + ",".join(reasons[:2])
            if len(reasons) > 2:
                status_s += f"(+{len(reasons)-2})"
        else:
            status_s = "ELIGIBLE"
        log.info(
            "window=%d cl=%s meta=%s up=%s dn=%s pair_sum=%s | hypo=%s",
            window_id, cl_s, meta_s, up_s, dn_s, ps_s, status_s,
        )
