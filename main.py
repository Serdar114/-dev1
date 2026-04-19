"""
main.py — 48-hour BTC short-horizon feasibility observation run.

Zero-order run. No strategy. No execution. No PnL.

What this does:
  1. Discover the front short-horizon BTC market via Gamma API
  2. Look up tick size and fee rate from CLOB REST endpoints
  3. Subscribe to CLOB public WS for live book data
  4. Subscribe to Binance WS for external BTC reference price
  5. Emit joined_observation.jsonl every 2 seconds (strictly)
  6. Refresh market selection every MARKET_REFRESH_INTERVAL_S
  7. Log everything to logs/ directory as JSONL

Run:
  python main.py

Environment variables (all optional):
  LOG_DIR          — override logs directory (default: logs)
  RUN_DURATION_S   — override run duration in seconds (default: 172800 = 48h)

Dependencies: see requirements.txt
  pip install -r requirements.txt
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Dict, Optional

import logger as log
from schemas import ExecutionRecord, FeeContext, JoinedObservation, MarketRecord
import gamma_api
import clob_rest
from clob_ws import ClobWsClient
from rtds_client import RtdsClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RUN_DURATION_S = int(os.environ.get("RUN_DURATION_S", 172800))  # 48 hours
OBSERVATION_INTERVAL_S = 2.0          # joined_observation cadence — do not change
MARKET_REFRESH_INTERVAL_S = 120       # re-select market every 2 minutes
LIVENESS_CHECK_INTERVAL_S = 5        # heartbeat/stale checks
BOOK_LOG_INTERVAL_S = 10             # log individual book snapshots every 10s
MARKET_SNAPSHOT_LOG_INTERVAL_S = 300 # re-log full market metadata every 5 min

# Stale thresholds for gate flags (observational only — no blocking in Day 1-2)
STALE_BOOK_MS = 15_000
STALE_EXTERNAL_MS = 5_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_stale(last_ms: Optional[int], threshold_ms: int) -> bool:
    if last_ms is None:
        return True
    return (_now_ms() - last_ms) > threshold_ms


# ---------------------------------------------------------------------------
# Market setup
# ---------------------------------------------------------------------------

def _enrich_and_build_fee_context(market: MarketRecord) -> FeeContext:
    """Fetch CLOB tick/fee metadata and merge into the market record in-place."""
    if market.tokens:
        primary_token = market.tokens[0].token_id
        meta = clob_rest.enrich_book_with_meta(primary_token, market.condition_id)
        log.log_system_event(
            "clob_meta_fetched",
            detail=(
                f"family={market.family_label} tick={meta['tick_size']} "
                f"fee={meta['fee_rate']} ({meta['fee_status']})"
            ),
            extra={**meta, "family": market.family_label},
        )
        if market.minimum_tick_size is None and meta["tick_size"] is not None:
            market.minimum_tick_size = meta["tick_size"]
        if market.fee_rate_bps is None and meta["fee_rate"] is not None:
            market.fee_rate_bps = meta["fee_rate"]
        if market.fees_enabled is None and meta["fees_enabled"] is not None:
            market.fees_enabled = meta["fees_enabled"]

    return FeeContext(
        fees_enabled=market.fees_enabled,
        fee_schedule_source="gamma_market_object_plus_clob_rest",
        fee_rate_lookup_value=market.fee_rate_bps,
        fee_rate_lookup_units="bps",
        fee_lookup_ts=_now_ms(),
        fee_lookup_status="ok" if market.fee_rate_bps is not None else "unknown",
        fee_formula_version=None,
    )


def _setup_all_markets(now_ts: int) -> Dict[str, tuple]:
    """
    Discover and enrich both market families.

    Returns:
      {
        "15m": (MarketRecord | None, FeeContext | None),
        "5m":  (MarketRecord | None, FeeContext | None),
      }

    15m = primary execution family (btc-updown-15m-*)
    5m  = observer / regime / gate family (btc-updown-5m-*)
    """
    markets_by_family = gamma_api.select_markets_by_family(now_ts)
    result: Dict[str, tuple] = {}

    for family in ("15m", "5m"):
        market = markets_by_family.get(family)
        if market is None:
            log.log_system_event(
                "no_market_available",
                detail=f"family={family}: no accepting market found; will retry",
                level="warning",
                extra={"family": family},
            )
            result[family] = (None, None)
            continue

        fee_ctx = _enrich_and_build_fee_context(market)
        log.log_market_snapshot(market, fee_ctx)
        result[family] = (market, fee_ctx)

    # Write future execution schema stub once per refresh (no order data Day 1-2)
    stub = ExecutionRecord(ts_local=_now_ms())
    log.log_future_execution_schema_stub(stub)

    return result


def _token_side_labels(market: MarketRecord):
    """
    Return (up_token_id, down_token_id, side_labels_dict) from a market record.
    Handles Yes/No and Up/Down outcome naming.
    """
    up_token: Optional[str] = None
    down_token: Optional[str] = None
    side_labels = {}

    for t in market.tokens:
        outcome_lower = t.outcome.lower()
        if outcome_lower in ("up", "yes"):
            up_token = t.token_id
            side_labels[t.token_id] = "up"
        elif outcome_lower in ("down", "no"):
            down_token = t.token_id
            side_labels[t.token_id] = "down"
        else:
            # Fallback: assign by position
            if up_token is None:
                up_token = t.token_id
                side_labels[t.token_id] = "up"
            elif down_token is None:
                down_token = t.token_id
                side_labels[t.token_id] = "down"

    return up_token, down_token, side_labels


# ---------------------------------------------------------------------------
# Observation builder
# ---------------------------------------------------------------------------

def _build_observation(
    market: Optional[MarketRecord],
    ws_client: Optional[ClobWsClient],
    rtds: RtdsClient,
    up_token_id: Optional[str],
    down_token_id: Optional[str],
    fee_context: Optional[FeeContext],
    family_label: Optional[str] = None,
) -> JoinedObservation:
    ts_local = _now_ms()

    # Dual external price (Binance + Chainlink via Polymarket RTDS)
    dual = rtds.latest_dual()

    # external_btc_price / external_data_age_ms / stale_external remain Binance-based
    # for backward compat with existing gate-flag semantics
    ext_price  = dual.binance_price if dual else None
    ext_age    = dual.binance_data_age_ms if dual else None
    stale_external = dual.binance_stale if dual else True

    # Book state
    up_snap = ws_client.get_snapshot(up_token_id) if (ws_client and up_token_id) else None
    dn_snap = ws_client.get_snapshot(down_token_id) if (ws_client and down_token_id) else None

    def _stale_book(snap) -> bool:
        if snap is None:
            return True
        return "stale" in snap.book_state_flags or snap.best_ask is None

    stale_up = _stale_book(up_snap)
    stale_dn = _stale_book(dn_snap)

    empty_up_ask = (up_snap is None or up_snap.best_ask is None)
    empty_dn_ask = (dn_snap is None or dn_snap.best_ask is None)

    crossed_up = up_snap is not None and "crossed" in up_snap.book_state_flags
    crossed_dn = dn_snap is not None and "crossed" in dn_snap.book_state_flags

    market_not_ready = (
        market is None
        or not market.accepting_orders
        or market.closed
    )

    fee_unknown = (
        fee_context is None
        or fee_context.fee_lookup_status != "ok"
        or fee_context.fee_rate_lookup_value is None
    )
    tick_unknown = market is None or market.minimum_tick_size is None
    min_size_unknown = market is None or market.min_order_size is None

    # Pair sums
    up_ask = up_snap.best_ask if up_snap else None
    dn_ask = dn_snap.best_ask if dn_snap else None
    up_bid = up_snap.best_bid if up_snap else None
    dn_bid = dn_snap.best_bid if dn_snap else None

    pair_ask_sum: Optional[float] = None
    pair_bid_sum: Optional[float] = None
    if up_ask is not None and dn_ask is not None:
        pair_ask_sum = round(up_ask + dn_ask, 6)
    if up_bid is not None and dn_bid is not None:
        pair_bid_sum = round(up_bid + dn_bid, 6)

    return JoinedObservation(
        ts_local=ts_local,
        market_id=market.market_id if market else None,
        market_slug=market.market_slug if market else None,
        window_start=market.start_time if market else None,
        window_end=market.end_time if market else None,
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        up_best_bid=up_bid,
        up_best_ask=up_ask,
        up_bid_size=up_snap.bid_size if up_snap else None,
        up_ask_size=up_snap.ask_size if up_snap else None,
        up_spread_pct=up_snap.spread_pct if up_snap else None,
        down_best_bid=dn_bid,
        down_best_ask=dn_ask,
        down_bid_size=dn_snap.bid_size if dn_snap else None,
        down_ask_size=dn_snap.ask_size if dn_snap else None,
        down_spread_pct=dn_snap.spread_pct if dn_snap else None,
        pair_best_ask_sum=pair_ask_sum,
        pair_best_bid_sum=pair_bid_sum,
        external_btc_price=ext_price,
        external_data_age_ms=ext_age,
        stale_external=stale_external,
        stale_book_up=stale_up,
        stale_book_down=stale_dn,
        empty_up_ask=empty_up_ask,
        empty_down_ask=empty_dn_ask,
        crossed_up=crossed_up,
        crossed_down=crossed_dn,
        market_not_ready=market_not_ready,
        fee_unknown=fee_unknown,
        tick_unknown=tick_unknown,
        min_size_unknown=min_size_unknown,
        family_label=family_label or (market.family_label if market else None),
        # Dual-source price fields from Polymarket RTDS
        external_btc_price_binance=dual.binance_price if dual else None,
        external_btc_price_chainlink=dual.chainlink_price if dual else None,
        external_basis_bps=dual.basis_bps if dual else None,
        external_lag_ms=dual.lag_ms if dual else None,
        stale_binance=dual.binance_stale if dual else True,
        stale_chainlink=dual.chainlink_stale if dual else True,
    )


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

def run() -> None:
    start_time = time.time()
    end_time = start_time + RUN_DURATION_S

    log.log_startup({
        "run_duration_s": RUN_DURATION_S,
        "observation_interval_s": OBSERVATION_INTERVAL_S,
        "market_refresh_interval_s": MARKET_REFRESH_INTERVAL_S,
        "families": ["15m (primary execution)", "5m (observer/regime/gate)"],
        "log_dir": os.environ.get("LOG_DIR", "logs"),
    })

    # ---- Dual-lane state: one slot per family ----
    # "15m" = primary execution family (btc-updown-15m-*)
    # "5m"  = observer / regime / gate family (btc-updown-5m-*)
    obs_lock = threading.Lock()
    _obs_state: Dict[str, dict] = {
        "15m": {
            "market": None, "ws_client": None, "fee_context": None,
            "up_token_id": None, "down_token_id": None,
        },
        "5m": {
            "market": None, "ws_client": None, "fee_context": None,
            "up_token_id": None, "down_token_id": None,
        },
    }
    # Local copies for coordination loop (no lock needed there, updated atomically)
    _live_markets: Dict[str, Optional[MarketRecord]] = {"15m": None, "5m": None}
    _live_ws: Dict[str, Optional[ClobWsClient]] = {"15m": None, "5m": None}
    _live_fee: Dict[str, Optional[FeeContext]] = {"15m": None, "5m": None}

    last_market_refresh = 0.0
    last_liveness_check = 0.0
    last_book_log = 0.0
    last_market_snapshot_log = 0.0

    # ---- Start RTDS ----
    rtds = RtdsClient()
    rtds.start()

    # ---- Signal handling for clean shutdown ----
    shutdown_flag = threading.Event()

    def _on_signal(signum, frame):
        log.log_system_event("signal_received", detail=f"signal={signum}; initiating shutdown")
        shutdown_flag.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # ---- Strict 2-second observation timer ----
    # Dedicated thread with next_tick arithmetic prevents cadence drift.
    # Emits TWO records per tick: one for 15m lane, one for 5m lane.
    # Both go to joined_observation.jsonl with family_label set.

    def _observation_loop():
        next_tick = time.time() + OBSERVATION_INTERVAL_S
        while not shutdown_flag.is_set():
            now = time.time()
            sleep_s = next_tick - now
            if sleep_s > 0:
                time.sleep(sleep_s)
            next_tick += OBSERVATION_INTERVAL_S

            with obs_lock:
                snap_15m = dict(_obs_state["15m"])
                snap_5m  = dict(_obs_state["5m"])

            for lane, snap in (("15m", snap_15m), ("5m", snap_5m)):
                try:
                    obs = _build_observation(
                        market=snap["market"],
                        ws_client=snap["ws_client"],
                        rtds=rtds,
                        up_token_id=snap["up_token_id"],
                        down_token_id=snap["down_token_id"],
                        fee_context=snap["fee_context"],
                        family_label=lane,
                    )
                    log.log_joined_observation(obs)
                except Exception as exc:
                    log.log_exception(f"observation_loop.{lane}", exc)

    obs_thread = threading.Thread(target=_observation_loop, daemon=True, name="obs_loop")
    obs_thread.start()

    # ---- Main coordination loop ----
    try:
        while not shutdown_flag.is_set() and time.time() < end_time:
            now = time.time()

            # Market refresh — discover both families each cycle
            if now - last_market_refresh >= MARKET_REFRESH_INTERVAL_S:
                last_market_refresh = now
                setup = _setup_all_markets(_now_ms())

                for family in ("15m", "5m"):
                    new_market, new_fee_ctx = setup[family]

                    if new_market is not None:
                        new_up, new_dn, side_labels = _token_side_labels(new_market)
                        token_ids = [t.token_id for t in new_market.tokens]

                        # Restart WS only if token ids changed for this lane
                        old_market = _live_markets.get(family)
                        old_ids = sorted([t.token_id for t in old_market.tokens] if old_market else [])
                        if sorted(token_ids) != old_ids:
                            old_ws = _live_ws.get(family)
                            if old_ws is not None:
                                old_ws.stop()
                            new_ws = ClobWsClient(
                                token_ids=token_ids,
                                side_labels=side_labels,
                                market_slug=new_market.market_slug,
                            )
                            new_ws.start()
                            _live_ws[family] = new_ws
                            log.log_system_event(
                                "clob_ws_restarted",
                                detail=f"family={family} new token_ids={token_ids}",
                                extra={"family": family},
                            )
                        else:
                            new_ws = _live_ws.get(family)

                        _live_markets[family] = new_market
                        _live_fee[family] = new_fee_ctx

                        with obs_lock:
                            _obs_state[family]["market"] = new_market
                            _obs_state[family]["ws_client"] = new_ws
                            _obs_state[family]["fee_context"] = new_fee_ctx
                            _obs_state[family]["up_token_id"] = new_up
                            _obs_state[family]["down_token_id"] = new_dn

            # Liveness checks for both lanes
            if now - last_liveness_check >= LIVENESS_CHECK_INTERVAL_S:
                last_liveness_check = now
                rtds.check_liveness()
                for family in ("15m", "5m"):
                    ws = _live_ws.get(family)
                    if ws:
                        ws.check_liveness()

            # Periodic book snapshots to books.jsonl (both lanes)
            if now - last_book_log >= BOOK_LOG_INTERVAL_S:
                last_book_log = now
                for family in ("15m", "5m"):
                    ws = _live_ws.get(family)
                    if ws:
                        for snap in ws.get_all_snapshots().values():
                            log.log_book_snapshot(snap)

            # Periodic full market metadata re-log (both lanes)
            if now - last_market_snapshot_log >= MARKET_SNAPSHOT_LOG_INTERVAL_S:
                last_market_snapshot_log = now
                for family in ("15m", "5m"):
                    m = _live_markets.get(family)
                    if m:
                        log.log_market_snapshot(m, _live_fee.get(family))

            time.sleep(0.1)

    except Exception as exc:
        log.log_exception("main_loop", exc)
    finally:
        shutdown_flag.set()
        for family in ("15m", "5m"):
            ws = _live_ws.get(family)
            if ws:
                ws.stop()
        rtds.stop()
        log.log_shutdown(reason="run_complete" if time.time() >= end_time else "shutdown_signal")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
