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
from typing import Optional

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

def _setup_market(now_ts: int):
    """
    Discover and return the selected market plus fee context.
    Returns (market, fee_context) or (None, None).
    """
    market = gamma_api.select_front_short_horizon_btc_market(now_ts)
    if market is None:
        log.log_system_event(
            "no_market_available",
            detail="no accepting short-horizon BTC market found; will retry",
            level="warning",
        )
        return None, None

    # Fetch tick size and fee rate from CLOB REST for each token
    if market.tokens:
        primary_token = market.tokens[0].token_id
        meta = clob_rest.enrich_book_with_meta(primary_token, market.condition_id)
        log.log_system_event(
            "clob_meta_fetched",
            detail=f"tick={meta['tick_size']} fee={meta['fee_rate']} ({meta['fee_status']})",
            extra=meta,
        )
        # Merge CLOB-discovered values back if market object lacked them
        if market.minimum_tick_size is None and meta["tick_size"] is not None:
            market.minimum_tick_size = meta["tick_size"]
        if market.fee_rate_bps is None and meta["fee_rate"] is not None:
            market.fee_rate_bps = meta["fee_rate"]
        if market.fees_enabled is None and meta["fees_enabled"] is not None:
            market.fees_enabled = meta["fees_enabled"]

    fee_context = FeeContext(
        fees_enabled=market.fees_enabled,
        fee_schedule_source="gamma_market_object_plus_clob_rest",
        fee_rate_lookup_value=market.fee_rate_bps,
        fee_rate_lookup_units="bps",
        fee_lookup_ts=_now_ms(),
        fee_lookup_status="ok" if market.fee_rate_bps is not None else "unknown",
        fee_formula_version=None,  # not yet discoverable; will populate if found
    )

    # Log full market snapshot with fee context
    log.log_market_snapshot(market, fee_context)

    # Write future execution schema stub (no order data in Day 1-2)
    stub = ExecutionRecord(ts_local=_now_ms())
    log.log_future_execution_schema_stub(stub)

    return market, fee_context


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
) -> JoinedObservation:
    ts_local = _now_ms()

    # External price
    ext_snap = rtds.latest()
    ext_price = ext_snap.price if ext_snap else None
    ext_age = ext_snap.data_age_ms if ext_snap else None
    stale_external = _is_stale(ext_snap.ts_local if ext_snap else None, STALE_EXTERNAL_MS)

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
        "log_dir": os.environ.get("LOG_DIR", "logs"),
    })

    # ---- State ----
    market: Optional[MarketRecord] = None
    fee_context = None
    ws_client: Optional[ClobWsClient] = None
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None

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
    # Use a separate thread with a tight loop to ensure 2s cadence is not
    # distorted by market refresh or other blocking operations.
    obs_lock = threading.Lock()
    _obs_state = {
        "market": None,
        "ws_client": None,
        "fee_context": None,
        "up_token_id": None,
        "down_token_id": None,
    }

    def _observation_loop():
        next_tick = time.time() + OBSERVATION_INTERVAL_S
        while not shutdown_flag.is_set():
            now = time.time()
            sleep_s = next_tick - now
            if sleep_s > 0:
                time.sleep(sleep_s)
            next_tick += OBSERVATION_INTERVAL_S

            with obs_lock:
                m = _obs_state["market"]
                wsc = _obs_state["ws_client"]
                fc = _obs_state["fee_context"]
                up_t = _obs_state["up_token_id"]
                dn_t = _obs_state["down_token_id"]

            try:
                obs = _build_observation(m, wsc, rtds, up_t, dn_t, fc)
                log.log_joined_observation(obs)
            except Exception as exc:
                log.log_exception("observation_loop", exc)

    obs_thread = threading.Thread(target=_observation_loop, daemon=True, name="obs_loop")
    obs_thread.start()

    # ---- Main coordination loop ----
    try:
        while not shutdown_flag.is_set() and time.time() < end_time:
            now = time.time()

            # Market refresh
            if now - last_market_refresh >= MARKET_REFRESH_INTERVAL_S:
                last_market_refresh = now
                new_market, new_fee_ctx = _setup_market(_now_ms())

                if new_market is not None:
                    new_up, new_dn, side_labels = _token_side_labels(new_market)
                    token_ids = [t.token_id for t in new_market.tokens]

                    # If token ids changed (new market window or first run), restart WS
                    old_token_ids = sorted([t.token_id for t in market.tokens] if market else [])
                    if sorted(token_ids) != old_token_ids:
                        if ws_client is not None:
                            ws_client.stop()
                        ws_client = ClobWsClient(
                            token_ids=token_ids,
                            side_labels=side_labels,
                            market_slug=new_market.market_slug,
                        )
                        ws_client.start()
                        log.log_system_event(
                            "clob_ws_restarted",
                            detail=f"new token_ids={token_ids}",
                        )

                    with obs_lock:
                        _obs_state["market"] = new_market
                        _obs_state["ws_client"] = ws_client
                        _obs_state["fee_context"] = new_fee_ctx
                        _obs_state["up_token_id"] = new_up
                        _obs_state["down_token_id"] = new_dn

                    market = new_market
                    fee_context = new_fee_ctx
                    up_token_id = new_up
                    down_token_id = new_dn

            # Liveness checks
            if now - last_liveness_check >= LIVENESS_CHECK_INTERVAL_S:
                last_liveness_check = now
                rtds.check_liveness()
                if ws_client:
                    ws_client.check_liveness()

            # Periodic book snapshots to books.jsonl
            if now - last_book_log >= BOOK_LOG_INTERVAL_S and ws_client:
                last_book_log = now
                for snap in ws_client.get_all_snapshots().values():
                    log.log_book_snapshot(snap)

            # Periodic full market metadata re-log
            if now - last_market_snapshot_log >= MARKET_SNAPSHOT_LOG_INTERVAL_S and market:
                last_market_snapshot_log = now
                log.log_market_snapshot(market, fee_context)

            time.sleep(0.1)  # short sleep so main loop isn't a busy-wait

    except Exception as exc:
        log.log_exception("main_loop", exc)
    finally:
        shutdown_flag.set()
        if ws_client:
            ws_client.stop()
        rtds.stop()
        log.log_shutdown(reason="run_complete" if time.time() >= end_time else "shutdown_signal")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
