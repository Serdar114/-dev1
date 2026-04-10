"""
runner.py — Main asyncio orchestration loop.

Design:
  Orchestrates all layers in the correct build order.
  Phase 1: discovery
  Phase 2: metadata (market object + /fee-rate per token)
  Phase 3: feeds (RTDS for Chainlink + Binance, CLOB WS for order books)
  Phase 4: window truth tracking (ChainlinkState wrapper)
  Phase 5: measurement-only mode (feature build + no-trade eval + hypotheticals)
  Phase 6: bucket analysis (in-memory, continuous)
  Phase 7: selective paper (only if mode=selective_paper and proven buckets exist)

  Canonical price source: RTDSClient.chainlink_latest() via ChainlinkState.
  Binance auxiliary: RTDSClient.binance_latest() — same connection, different topic.
  Order books: CLOBWSClient (separate service, different WebSocket endpoint).

  The main tick loop runs every 1 second.
  For each live market, per tick:
    - Build FeatureVector from live state
    - Evaluate no-trade rules
    - Record hypothetical entries (both UP and DOWN) regardless of no-trade
    - If paper mode AND proven bucket AND rules pass: open paper trade
    - Log all events

  Resolution handling:
    - At window close, capture Chainlink close price via ChainlinkState
    - Compute outcome
    - Resolve all hypothetical entries for that window
    - Close all paper positions for that window
    - Record bucket stats

  Error handling:
    - All exceptions in tick loop are caught and logged
    - System continues running unless keyboard interrupt
    - No exception propagates silently
"""

from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Set

import yaml

from app.modes import validate_mode, is_paper_active, MEASUREMENT_ONLY
from discovery.market_discovery import MarketDiscovery
from discovery.market_registry import MarketRegistry
from metadata.market_metadata import MetadataFetcher
from feeds.rtds_client import RTDSClient
from feeds.clob_ws_client import CLOBWSClient
from truth.chainlink_state import ChainlinkState
from truth.window_clock import WindowClock
from truth.resolution_truth import ResolutionTruthTracker
from truth.freshness import check_freshness
from book.orderbook_state import OrderBookState
from book.price_views import book_state_summary
from signals.feature_builder import FeatureBuilder
from signals.no_trade_rules import evaluate_all, format_verdict
from signals.bucket_probe import assign_bucket
from paper.hypothetical_entry import HypotheticalEntryEngine
from paper.paper_executor import PaperExecutor
from analytics.bucket_stats import BucketAccumulator
from analytics.edge_report import EdgeReport
from analytics.verdict import VerdictGenerator
from loggingx.event_logger import EventLogger
from loggingx.schemas import (
    MarketStatus, NoTradeEvent, ResolutionOutcome,
    FreshnessState, EventType,
)

logger = logging.getLogger("polybot.runner")


class Runner:
    """
    Main orchestration runner.

    Job: wire all layers together, drive the main event loop.
    """

    def __init__(self, config: dict):
        self.config = config
        self.mode = validate_mode(config.get("mode", MEASUREMENT_ONLY))
        self._start_time = time.time()

        # Event logger
        log_cfg = config.get("logging", {})
        self._elog = EventLogger(
            log_dir=log_cfg.get("log_dir", "logs"),
            events_file=log_cfg.get("events_file", "events.jsonl"),
        )

        # Discovery + registry
        disc_cfg = config.get("discovery", {})
        self._discovery = MarketDiscovery(
            clob_api_url=disc_cfg.get("clob_api_url", "https://clob.polymarket.com"),
            gamma_api_url=disc_cfg.get("gamma_api_url", "https://gamma-api.polymarket.com"),
            title_keywords=disc_cfg.get("title_keywords", ["btc", "bitcoin"]),
            min_window_secs=disc_cfg.get("min_window_secs", 240),
            max_window_secs=disc_cfg.get("max_window_secs", 360),
        )
        self._registry = MarketRegistry()

        # Metadata
        meta_cfg = config.get("metadata", {})
        self._meta_fetcher = MetadataFetcher(
            clob_api_url=meta_cfg.get("clob_api_url", "https://clob.polymarket.com"),
            fee_rate_endpoint=meta_cfg.get("fee_rate_endpoint", "https://clob.polymarket.com/fee-rate"),
        )
        self._require_fee = meta_cfg.get("require_confirmed_fee", True)

        # RTDS feed (Chainlink canonical + Binance auxiliary on same WS)
        feeds_cfg = config.get("feeds", {})
        rtds_cfg = feeds_cfg.get("rtds", {})
        self._rtds = RTDSClient(
            ws_url=rtds_cfg.get("url", "wss://ws-live-data.polymarket.com"),
            chainlink_topic=rtds_cfg.get("chainlink_topic", "crypto_prices_chainlink"),
            chainlink_symbol=rtds_cfg.get("chainlink_symbol", "btc/usd"),
            binance_topic=rtds_cfg.get("binance_topic", "crypto_prices"),
            binance_symbol=rtds_cfg.get("binance_symbol", "btcusdt"),
            chainlink_staleness_secs=rtds_cfg.get("chainlink_staleness_secs", 30),
            binance_staleness_secs=rtds_cfg.get("binance_staleness_secs", 20),
            reconnect_delay_secs=rtds_cfg.get("reconnect_delay_secs", 3),
            ping_interval_secs=rtds_cfg.get("ping_interval_secs", 5),
        )

        # ChainlinkState wrapper with gap detection
        self._chainlink_state = ChainlinkState(
            rtds_client=self._rtds,
            gap_threshold_secs=rtds_cfg.get("chainlink_gap_threshold_secs", 15),
        )

        # CLOB WebSocket (order books only — separate service)
        clob_ws_cfg = feeds_cfg.get("clob_ws", {})
        self._clob_ws = CLOBWSClient(
            ws_url=clob_ws_cfg.get("url", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            reconnect_delay_secs=clob_ws_cfg.get("reconnect_delay_secs", 5),
            on_book_update=self._on_book_update,
        )

        # Per-token order book state
        self._book_states: Dict[str, OrderBookState] = {}

        # Per-market window trackers
        self._window_clocks:  Dict[str, WindowClock] = {}
        self._truth_trackers: Dict[str, ResolutionTruthTracker] = {}

        # Feature builder
        self._feat_builder = FeatureBuilder()

        # No-trade config
        nt_cfg = config.get("no_trade", {})
        self._nt_max_staleness = nt_cfg.get("chainlink_max_staleness_secs", 30)
        self._nt_min_secs      = nt_cfg.get("min_secs_to_expiry", 30)
        self._nt_max_spread    = nt_cfg.get("max_spread", 0.10)
        self._nt_sum_min       = nt_cfg.get("pair_sum_min", 0.90)
        self._nt_sum_max       = nt_cfg.get("pair_sum_max", 1.20)

        # Paper layer
        paper_cfg = config.get("paper", {})
        stake = paper_cfg.get("max_stake_usdc", 5.0)
        self._hyp_engine = HypotheticalEntryEngine(stake_usdc=stake)
        self._paper_exec = PaperExecutor(
            max_open_positions=paper_cfg.get("max_open_positions", 1),
            max_stake_usdc=stake,
        )
        self._min_bucket_obs  = paper_cfg.get("min_bucket_observations", 20)
        self._min_bucket_edge = paper_cfg.get("min_bucket_net_edge", 0.02)

        # Analytics
        an_cfg = config.get("analytics", {})
        self._bucket_acc = BucketAccumulator(
            min_observations=an_cfg.get("min_observations_for_report", 5)
        )
        self._edge_report = EdgeReport(
            min_obs_for_report=an_cfg.get("min_observations_for_report", 5)
        )

        # Counters
        self._n_markets_discovered    = 0
        self._n_windows_observed      = 0
        self._n_windows_resolved      = 0
        self._n_windows_unresolved    = 0
        self._n_no_trade              = 0
        self._n_hypothetical_entries  = 0
        self._n_chainlink_stale       = 0

        # Chainlink freshness transition tracking (log on state change only)
        self._chainlink_was_fresh: Optional[bool] = None
        self._last_freshness_log_ts: float = 0.0

        # Per-window hypothetical tracking
        self._window_hypotheticals: Dict[str, list] = {}

    async def run(self) -> None:
        self._elog.log_system_start(self.config)
        logger.info("Runner starting — mode=%s", self.mode)

        tasks = [
            asyncio.create_task(self._rtds.start(),      name="rtds"),
            asyncio.create_task(self._clob_ws.start(),   name="clob_ws"),
            asyncio.create_task(self._discovery_loop(),  name="discovery"),
            asyncio.create_task(self._main_tick_loop(),  name="tick"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Runner cancelled — shutting down")
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down")
        finally:
            for t in tasks:
                t.cancel()
            await self._shutdown()

    async def _shutdown(self) -> None:
        await self._rtds.stop()
        await self._clob_ws.stop()
        self._print_final_verdict()
        self._elog.close()
        logger.info("Runner stopped")

    async def _discovery_loop(self) -> None:
        """Periodically discover new BTC 5m markets."""
        disc_cfg = self.config.get("discovery", {})
        interval = disc_cfg.get("poll_interval_secs", 60)

        while True:
            try:
                markets = await self._discovery.discover()
                for identity in markets:
                    if self._registry.register(identity):
                        self._n_markets_discovered += 1
                        self._elog.log_market_discovered(
                            condition_id=identity.condition_id,
                            question=identity.question,
                            up_token_id=identity.up_token_id,
                            down_token_id=identity.down_token_id,
                            window_start_ts=identity.window_start_ts,
                            window_end_ts=identity.window_end_ts,
                        )
                        await self._fetch_metadata(identity)

                active_ids = self._registry.active_token_ids()
                if active_ids:
                    await self._clob_ws.subscribe(active_ids)

            except Exception as exc:
                logger.error("Discovery loop error: %s", exc)
                self._elog.log_error("discovery_loop", str(exc))

            await asyncio.sleep(interval)

    async def _fetch_metadata(self, identity) -> None:
        """Fetch and store metadata for one market, including per-token fee rates."""
        meta = await self._meta_fetcher.fetch(
            condition_id=identity.condition_id,
            up_token_id=identity.up_token_id,
            down_token_id=identity.down_token_id,
            gamma_fee_rate=getattr(identity, "gamma_fee_rate", None),
            gamma_fee_source=getattr(identity, "gamma_fee_source", None),
        )
        self._registry.set_metadata(identity.condition_id, meta)

        if meta.fetch_error:
            self._elog.log("METADATA_FAILED", {
                "condition_id": identity.condition_id,
                "error": meta.fetch_error,
            })
        else:
            self._elog.log("METADATA_FETCHED", {
                "condition_id":   identity.condition_id,
                "fee_rate":       meta.fee_rate,
                "fee_source":     meta.fee_source,
                "fees_enabled":   meta.fees_enabled,
                "tick_size":      meta.tick_size,
                "min_order_size": meta.min_order_size,
                "active":         meta.active,
            })

    async def _main_tick_loop(self) -> None:
        """Main per-market tick loop. Runs every second."""
        # Wait briefly for feeds to connect and subscribe
        await asyncio.sleep(5)

        while True:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Tick loop error: %s", exc)
                self._elog.log_error("tick_loop", str(exc))
            await asyncio.sleep(1.0)

    async def _tick(self) -> None:
        """Process one tick for all tracked markets."""
        chainlink = self._chainlink_state.latest()
        binance   = self._rtds.binance_latest()
        now       = time.time()

        # ── Chainlink freshness — log on transition only, plus 60s heartbeat ─
        chainlink_is_fresh = (
            chainlink is not None and chainlink.freshness == FreshnessState.FRESH
        )
        if not chainlink_is_fresh:
            self._n_chainlink_stale += 1

        if self._chainlink_was_fresh is None:
            # First tick: initialise silently; emit one INFO if already fresh
            if chainlink_is_fresh:
                logger.info(
                    "Chainlink ready: price=%.2f freshness=FRESH",
                    chainlink.price_usd,
                )
            self._chainlink_was_fresh = chainlink_is_fresh
        elif chainlink_is_fresh != self._chainlink_was_fresh:
            if chainlink_is_fresh:
                logger.info(
                    "Chainlink STALE → FRESH price=%.2f",
                    chainlink.price_usd,
                )
            else:
                state_str = chainlink.freshness if chainlink else FreshnessState.MISSING
                age = chainlink.age_secs() if chainlink else None
                logger.warning("Chainlink FRESH → %s age=%.1fs", state_str, age or -1)
                self._elog.log_chainlink_stale(
                    last_updated_at=chainlink.updated_at if chainlink else None,
                    age_secs=age,
                )
            self._chainlink_was_fresh = chainlink_is_fresh

        # Periodic heartbeat (every 60s)
        if now - self._last_freshness_log_ts >= 60.0:
            self._last_freshness_log_ts = now
            if chainlink:
                logger.info(
                    "Chainlink heartbeat: price=%.2f age=%.1fs freshness=%s gap=%s",
                    chainlink.price_usd, chainlink.age_secs(),
                    chainlink.freshness, self._chainlink_state.gap_flag,
                )
            else:
                logger.info("Chainlink heartbeat: no data yet")

        for entry in self._registry.all_markets():
            cid = entry.condition_id
            identity = entry.identity

            # Initialize window clock if not done
            if cid not in self._window_clocks:
                try:
                    clock = WindowClock(
                        condition_id=cid,
                        window_start_ts=identity.window_start_ts,
                        window_end_ts=identity.window_end_ts,
                    )
                    self._window_clocks[cid] = clock
                    self._truth_trackers[cid] = ResolutionTruthTracker(
                        condition_id=cid,
                        window_start_ts=identity.window_start_ts,
                        window_end_ts=identity.window_end_ts,
                    )
                except ValueError as exc:
                    logger.warning("[%s] Invalid window: %s", cid, exc)
                    continue

            clock   = self._window_clocks[cid]
            tracker = self._truth_trackers[cid]

            # ── Expired guard: window over before we ever observed it ────────
            # Must come first — never allow PENDING→LIVE on a closed window.
            if clock.is_expired_unobserved():
                if entry.status not in (MarketStatus.RESOLVED, MarketStatus.CLOSED):
                    logger.info(
                        "[%s] MARKET_EXPIRED_UNOBSERVED window_end=%.0f now=%.0f — skipping",
                        cid, identity.window_end_ts, time.time(),
                    )
                    self._registry.mark_resolved(cid)
                continue

            # ── PENDING ──────────────────────────────────────────────────────
            if clock.is_before_open():
                if entry.status != MarketStatus.PENDING:
                    self._registry.set_status(cid, MarketStatus.PENDING)
                continue

            if clock.should_fire_open():
                self._registry.mark_live(cid)
                self._n_windows_observed += 1
                tracker.capture_open(chainlink)
                self._elog.log_window_open(
                    condition_id=cid,
                    window_start_ts=identity.window_start_ts,
                    window_end_ts=identity.window_end_ts,
                    chainlink_open=tracker.truth.chainlink_open,
                    chainlink_ok=tracker.truth.chainlink_open_ok,
                    error=tracker.truth.open_capture_error,
                )
                await self._clob_ws.subscribe({identity.up_token_id, identity.down_token_id})

            if clock.should_fire_close():
                tracker.capture_close(chainlink)
                outcome = tracker.truth.outcome

                self._elog.log_window_close(
                    condition_id=cid,
                    window_start_ts=identity.window_start_ts,
                    chainlink_close=tracker.truth.chainlink_close,
                    chainlink_ok=tracker.truth.chainlink_close_ok,
                    outcome=outcome,
                    error=tracker.truth.close_capture_error,
                )
                self._elog.log_resolution(
                    condition_id=cid,
                    window_start_ts=identity.window_start_ts,
                    outcome=outcome,
                    chainlink_open=tracker.truth.chainlink_open,
                    chainlink_close=tracker.truth.chainlink_close,
                )

                self._resolve_window(cid, outcome)

                if is_paper_active(self.mode):
                    closed = self._paper_exec.close_all_for_market(cid, outcome)
                    for t in closed:
                        self._elog.log_paper_trade_close(asdict(t))

                if outcome == ResolutionOutcome.UNRESOLVED:
                    self._n_windows_unresolved += 1
                else:
                    self._n_windows_resolved += 1

                self._registry.mark_resolved(cid)
                continue

            # Skip if market is closed/resolved
            if entry.status not in (MarketStatus.LIVE, MarketStatus.EXPIRING):
                continue

            # Expiring flag
            if clock.secs_to_expiry() < self._nt_min_secs:
                self._registry.mark_expiring(cid)

            # Build features
            up_book   = self._clob_ws.get_book(identity.up_token_id)
            down_book = self._clob_ws.get_book(identity.down_token_id)

            fv = self._feat_builder.build(
                condition_id=cid,
                up_token_id=identity.up_token_id,
                down_token_id=identity.down_token_id,
                window_start_ts=identity.window_start_ts,
                window_end_ts=identity.window_end_ts,
                chainlink=chainlink,
                chainlink_open=tracker.truth.chainlink_open,
                binance=binance,
                up_book=up_book,
                down_book=down_book,
                metadata=entry.metadata,
            )

            # Evaluate no-trade rules
            verdict = evaluate_all(
                fv,
                min_secs_to_expiry=self._nt_min_secs,
                max_spread=self._nt_max_spread,
                pair_sum_min=self._nt_sum_min,
                pair_sum_max=self._nt_sum_max,
            )
            should_trade, reason_code, details = verdict

            if not should_trade:
                self._n_no_trade += 1
                no_trade_evt = NoTradeEvent(
                    condition_id=cid,
                    window_start_ts=identity.window_start_ts,
                    reason_code=reason_code,
                    is_canonical=details.get("_canonical", True),
                    details={k: v for k, v in (details or {}).items() if k != "_canonical"},
                )
                self._elog.log_no_trade(no_trade_evt)
                up_entry, down_entry = self._hyp_engine.compute(fv, no_trade_reason=reason_code)
            else:
                up_entry, down_entry = self._hyp_engine.compute(fv, no_trade_reason=None)

            self._window_hypotheticals[cid] = [up_entry, down_entry]
            self._n_hypothetical_entries += 2

            self._elog.log_hypothetical(asdict(up_entry))
            self._elog.log_hypothetical(asdict(down_entry))

            # Paper mode
            if is_paper_active(self.mode) and should_trade:
                proven = self._bucket_acc.proven_buckets(
                    min_obs=self._min_bucket_obs,
                    net_edge_threshold=self._min_bucket_edge,
                )
                bucket_id = assign_bucket(fv)
                if bucket_id in proven:
                    side = self._pick_paper_side(bucket_id, fv)
                    if side and not self._paper_exec.has_open_position_for(cid):
                        trade = self._paper_exec.open_trade(fv, side, bucket_id, proven)
                        if trade:
                            self._elog.log_paper_trade_open(asdict(trade))

    def _resolve_window(self, condition_id: str, outcome: str) -> None:
        hyps = self._window_hypotheticals.pop(condition_id, [])
        for entry in hyps:
            resolved = self._hyp_engine.resolve(entry, outcome)
            self._bucket_acc.record(resolved)

    def _pick_paper_side(self, bucket_id: str, fv) -> Optional[str]:
        return None  # Conservative default; requires proven bucket analysis

    async def _on_book_update(self, token_id: str, book) -> None:
        if token_id not in self._book_states:
            self._book_states[token_id] = OrderBookState(token_id=token_id)
        self._book_states[token_id].update(book)

    def _print_final_verdict(self) -> None:
        all_stats = self._bucket_acc.all_stats(self._min_bucket_edge)
        rows = self._edge_report.generate(all_stats)
        proven = self._bucket_acc.proven_buckets(self._min_bucket_obs, self._min_bucket_edge)

        paper_closed = [asdict(t) for t in self._paper_exec.closed_trades()]

        verdict = VerdictGenerator().generate(
            run_duration_secs=time.time() - self._start_time,
            n_markets_discovered=self._n_markets_discovered,
            n_windows_observed=self._n_windows_observed,
            n_windows_resolved=self._n_windows_resolved,
            n_windows_unresolved=self._n_windows_unresolved,
            n_no_trade=self._n_no_trade,
            n_hypothetical_entries=self._n_hypothetical_entries,
            n_chainlink_stale=self._n_chainlink_stale,
            bucket_rows=rows,
            proven_buckets=proven,
            paper_closed=paper_closed or None,
        )

        print("\n" + "=" * 80)
        print("POLYBOT_ZERO FINAL VERDICT")
        print("=" * 80)
        print(VerdictGenerator().to_text(verdict))
        print("=" * 80)

        self._elog.log("VERDICT", verdict)
        logger.info("Edge table:\n%s", self._edge_report.print_table(rows))
