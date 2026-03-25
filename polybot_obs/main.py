#!/usr/bin/env python3
# main.py — asyncio entry point for the Polybot Observation Bot

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiohttp

# Ensure the package directory is on the path when run directly
sys.path.insert(0, os.path.dirname(__file__))

from binance import BinanceFeed
from config import (
    MIN_ORDER_SIZE_WARN,
    SNAPSHOT_POLL_INTERVAL,
    WINDOW_SECONDS,
)
from logger import (
    compute_and_write_daily_summary,
    load_today_records,
    log_startup,
    read_last_window_id,
    write_observation,
)
from paper import evaluate_snipe, resolve_snipe
from polymarket import get_best_ask, get_market, get_min_order_size, poll_resolution
from window import (
    WindowState,
    btc_delta_pct_at_T10,
    current_window_ts,
    snapshot_loop,
    time_remaining,
    window_close_ts,
    OFFSET_KEY,
)

# ── Logging setup ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("main")


# ── Startup check ──────────────────────────────────────────────────────────

async def startup_check(session: aiohttp.ClientSession, feed: BinanceFeed):
    """Fetch orderbook for current window's YES token. Log min_order_size."""
    log_startup("Bot starting up.")

    # Wait up to 10 s for Binance price
    for _ in range(20):
        if feed.btc_price is not None:
            break
        await asyncio.sleep(0.5)
    if feed.btc_price is None:
        log_startup("WARNING: Binance price not yet available at startup.", "WARNING")
    else:
        log_startup(f"Binance BTC price at startup: {feed.btc_price}")

    ts   = current_window_ts()
    slug = f"btc-updown-5m-{ts}"
    log_startup(f"Current window slug: {slug}")

    market = await get_market(session, slug)
    if not market or not market.get("yes_token_id"):
        log_startup(f"Could not fetch market for slug={slug}", "WARNING")
        return

    yes_token = market["yes_token_id"]
    mos = await get_min_order_size(session, yes_token)
    if mos is None:
        # Fall back to market-level value
        mos = market.get("min_order_size")

    log_startup(f"min_order_size for current window: {mos}")
    if mos is not None and float(mos) > MIN_ORDER_SIZE_WARN:
        log_startup(
            f"CRITICAL: min_order_size={mos} exceeds threshold {MIN_ORDER_SIZE_WARN}. "
            "Market liquidity may be low. Bot continues.",
            "CRITICAL",
        )

    # Log restart-safety info
    last_wid = read_last_window_id()
    log_startup(f"Last logged window_id: {last_wid}")


# ── Single window observation ──────────────────────────────────────────────

async def observe_window(
    session: aiohttp.ClientSession,
    feed: BinanceFeed,
    open_ts: int,
    skip_ids: set[str],
) -> dict | None:
    """
    Observe a single 5-minute window.
    Returns the complete JSONL record, or None if skipped.
    """
    state = WindowState(open_ts)

    if state.slug in skip_ids:
        log.info("Skipping already-logged window: %s", state.slug)
        return None

    log.info("=== New window: %s (open_ts=%d) ===", state.slug, open_ts)

    # ── Record open price ────────────────────────────────────────────────
    state.open_btc = feed.btc_price
    if state.open_btc is None:
        log.warning("No Binance price at window open. Continuing with null.")

    # ── Fetch market metadata ────────────────────────────────────────────
    market = await get_market(session, state.slug)
    if market:
        state.yes_token_id   = market.get("yes_token_id")
        state.no_token_id    = market.get("no_token_id")
        state.min_order_size = market.get("min_order_size")
    else:
        log.warning("Could not fetch market for %s. Snapshots will be null.", state.slug)

    # ── Snapshot + snipe evaluation loop ────────────────────────────────
    # ask_fn always fetches YES token — used by snapshot_loop for yes_ask column
    async def ask_fn() -> float | None:
        if not state.yes_token_id:
            return None
        return await get_best_ask(session, state.yes_token_id)

    close_ts = window_close_ts(open_ts)

    # Run snapshot loop concurrently with snipe evaluation
    snap_task = asyncio.create_task(
        snapshot_loop(state, lambda: feed.btc_price, ask_fn)
    )

    # Snipe evaluation: check every poll interval during T-15 to T-2 window
    while not snap_task.done():
        tr = time_remaining(open_ts)
        if not state.snipe_done and 2 < tr < 15:
            btc = feed.btc_price
            if btc is not None and state.open_btc is not None:
                btc_delta_pct = (btc - state.open_btc) / state.open_btc * 100
                # Pick token side based on current BTC direction
                if btc_delta_pct > 0:
                    token_id   = state.yes_token_id
                    token_side = "YES"
                else:
                    token_id   = state.no_token_id
                    token_side = "NO"
                if token_id:
                    ask = await get_best_ask(session, token_id)
                    if ask is not None:
                        result = evaluate_snipe(btc, state.open_btc, ask, tr)
                        if result["triggered"]:
                            result["token_side"] = token_side
                            state.paper_snipe = result
                            state.snipe_done  = True
                            log.info(
                                "Paper snipe triggered: dir=%s side=%s entry=%.4f tr=%.1fs",
                                result["direction"], token_side, result["entry_price"], tr,
                            )
        await asyncio.sleep(SNAPSHOT_POLL_INTERVAL)

    await snap_task  # ensure it's finished and snapshots are filled

    # ── Wait for window close ────────────────────────────────────────────
    now = time.time()
    if now < close_ts:
        await asyncio.sleep(close_ts - now + 0.1)

    log.info("Window closed: %s — polling for resolution.", state.slug)

    # ── Resolution polling ───────────────────────────────────────────────
    resolution = await poll_resolution(
        session,
        state.slug,
        close_ts,
        btc_price_fn=lambda: feed.btc_price,
    )

    log.info(
        "Resolution for %s: outcome=%s chainlink=%s",
        state.slug,
        resolution.get("outcome"),
        resolution.get("chainlink_price"),
    )

    # ── Enrich snipe with resolution outcome ─────────────────────────────
    if state.paper_snipe.get("triggered") and resolution.get("outcome") not in (
        None, "UNRESOLVED"
    ):
        state.paper_snipe = resolve_snipe(state.paper_snipe, resolution["outcome"])

    # ── Build JSONL record ────────────────────────────────────────────────
    record = {
        "window_id":          state.slug,
        "open_time_unix":     open_ts,
        "open_btc_price":     state.open_btc,
        "yes_token_id":       state.yes_token_id,
        "min_order_size":     state.min_order_size,
        "snapshots":          state.snapshots,
        "btc_delta_pct_at_T10": btc_delta_pct_at_T10(state),
        "paper_snipe":        state.paper_snipe,
        "resolution":         resolution,
    }

    write_observation(record)
    return record


# ── Daily summary trigger ──────────────────────────────────────────────────

def maybe_write_daily_summary(records_today: list[dict], prev_date: str):
    """Write daily summary when UTC date rolls over or at startup for yesterday."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != prev_date and prev_date:
        day_records = load_today_records(prev_date)
        if day_records:
            compute_and_write_daily_summary(prev_date, day_records)
    return today


# ── Main loop ──────────────────────────────────────────────────────────────

async def main():
    feed = BinanceFeed()

    # Start Binance feed in background
    feed_task = asyncio.create_task(feed.run())

    async with aiohttp.ClientSession() as session:
        await startup_check(session, feed)

        # Restart safety: collect already-logged window IDs
        skip_ids: set[str] = set()
        last_wid = read_last_window_id()
        if last_wid:
            skip_ids.add(last_wid)
            log.info("Will skip already-logged window: %s", last_wid)

        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        records_today: list[dict] = load_today_records(current_date)

        while True:
            open_ts = current_window_ts()
            close_ts = window_close_ts(open_ts)

            # ── Daily summary roll-over ──────────────────────────────────
            new_date = maybe_write_daily_summary(records_today, current_date)
            if new_date != current_date:
                current_date  = new_date
                records_today = load_today_records(current_date)

            # ── Observe this window ──────────────────────────────────────
            record = await observe_window(session, feed, open_ts, skip_ids)

            if record:
                records_today.append(record)
                # Don't accumulate skip_ids indefinitely — just last window
                skip_ids = {record["window_id"]}
            else:
                skip_ids = set()

            # ── Wait for next window to start ────────────────────────────
            next_open = close_ts
            sleep_for = next_open - time.time()
            if sleep_for > 0:
                log.info("Waiting %.1fs for next window (open_ts=%d).", sleep_for, next_open)
                await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Observation bot stopped by user.")
