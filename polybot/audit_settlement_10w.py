"""
audit_settlement_10w.py — Settlement audit for the last 10 closed 5m BTC windows.

Question: Do Binance and Chainlink produce the same winner across the last
10 closed 5-minute BTC windows?

Does NOT:
  - open trades
  - use PM websocket
  - depend on paper_trader
  - modify strategy behavior

Uses:
  - Binance historical kline API for per-window open/close
  - resolution_truth._fetch_btc_close_chainlink for Chainlink historical walkback
  - resolution_truth._determine_winner for consistent winner logic
"""

import asyncio
import time
import socket
import aiohttp
import aiohttp.resolver

from resolution_truth import (
    _fetch_btc_close_chainlink,
    _determine_winner,
)

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
WINDOW_COUNT = 10
INTERVAL_SECS = 300  # 5m


async def _fetch_binance_kline(window_ts: int) -> tuple[float, float, bool]:
    """
    Fetch the historical 5m Binance kline that starts exactly at window_ts.
    Returns: (btc_open, btc_close, success)
    On any failure or alignment mismatch returns (0.0, 0.0, False).
    """
    start_ms = window_ts * 1000
    end_ms = (window_ts + INTERVAL_SECS) * 1000
    url = (
        f"{BINANCE_KLINE_URL}?symbol=BTCUSDT&interval=5m"
        f"&startTime={start_ms}&endTime={end_ms}&limit=1"
    )
    try:
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            resolver=aiohttp.resolver.ThreadedResolver(),
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status != 200:
                    return 0.0, 0.0, False
                data = await r.json()
                if not data or not isinstance(data, list) or len(data[0]) < 5:
                    return 0.0, 0.0, False
                kline = data[0]
                kline_open_time_ms = int(kline[0])
                if kline_open_time_ms != start_ms:
                    # Kline doesn't align to window — reject
                    return 0.0, 0.0, False
                btc_open = round(float(kline[1]), 2)
                btc_close = round(float(kline[4]), 2)
                if btc_open <= 0 or btc_close <= 0:
                    return 0.0, 0.0, False
                return btc_open, btc_close, True
    except Exception:
        return 0.0, 0.0, False


async def _audit_window(window_ts: int) -> dict:
    """Audit a single fully closed 5m window."""
    round_end_ts = window_ts + INTERVAL_SECS

    # --- Binance historical kline ---
    btc_open_binance, btc_close_binance, binance_ok = await _fetch_binance_kline(window_ts)

    if binance_ok:
        winner_binance = _determine_winner(btc_open_binance, btc_close_binance)
    else:
        winner_binance = "unknown"

    # --- Chainlink historical round (only if Binance open is known) ---
    if binance_ok:
        btc_close_chainlink, winner_chainlink, chainlink_status = (
            await _fetch_btc_close_chainlink(window_ts, "5m", btc_open_binance)
        )
    else:
        btc_close_chainlink = 0.0
        winner_chainlink = "unknown"
        chainlink_status = "skipped_no_binance_open"

    # --- Resolution comparison ---
    if not binance_ok:
        resolution_match = "unknown"
        resolution_truth_status = "unresolved_fetch_error"
    elif chainlink_status in ("fetched", "fetched_stale") and winner_chainlink != "unknown":
        if winner_binance == winner_chainlink:
            resolution_match = "match"
            resolution_truth_status = "dual_verified"
        else:
            resolution_match = "mismatch"
            resolution_truth_status = "dual_mismatch"
    else:
        resolution_match = "unknown"
        resolution_truth_status = "binance_only"

    return {
        "window_ts": window_ts,
        "round_end_ts": round_end_ts,
        "btc_open_binance": btc_open_binance,
        "btc_close_binance": btc_close_binance,
        "winner_binance": winner_binance,
        "btc_close_chainlink": round(btc_close_chainlink, 2),
        "winner_chainlink": winner_chainlink,
        "chainlink_status": chainlink_status,
        "resolution_match": resolution_match,
        "resolution_truth_status": resolution_truth_status,
    }


async def main():
    now_ts = int(time.time())
    # Last fully closed 5m window (current window is still open)
    last_closed = (now_ts // INTERVAL_SECS) * INTERVAL_SECS - INTERVAL_SECS
    windows = sorted(
        [last_closed - i * INTERVAL_SECS for i in range(WINDOW_COUNT)]
    )  # oldest → newest

    print("=" * 110)
    print("SETTLEMENT AUDIT — last 10 closed 5m BTC windows")
    print(f"  audit_ts={now_ts}  last_closed_window={last_closed}")
    print("=" * 110)
    header = (
        f"{'window_ts':>12}  {'open_bnb':>10}  {'close_bnb':>10}  "
        f"{'w_bnb':>6}  {'close_cl':>10}  {'w_cl':>6}  "
        f"{'chainlink_status':>30}  {'match':>9}  {'resolution_truth_status':>26}"
    )
    print(header)
    print("-" * 110)

    results = []
    for w in windows:
        row = await _audit_window(w)
        results.append(row)
        print(
            f"{row['window_ts']:>12}  "
            f"{row['btc_open_binance']:>10.2f}  "
            f"{row['btc_close_binance']:>10.2f}  "
            f"{row['winner_binance']:>6}  "
            f"{row['btc_close_chainlink']:>10.2f}  "
            f"{row['winner_chainlink']:>6}  "
            f"{row['chainlink_status']:>30}  "
            f"{row['resolution_match']:>9}  "
            f"{row['resolution_truth_status']:>26}"
        )

    # --- Summary ---
    total = len(results)
    dual_verified = sum(1 for r in results if r["resolution_truth_status"] == "dual_verified")
    dual_mismatch = sum(1 for r in results if r["resolution_truth_status"] == "dual_mismatch")
    binance_only = sum(1 for r in results if r["resolution_truth_status"] == "binance_only")
    unresolved = sum(1 for r in results if r["resolution_truth_status"] == "unresolved_fetch_error")

    print("=" * 110)
    print("SUMMARY")
    print(f"  total_windows                = {total}")
    print(f"  dual_verified_count          = {dual_verified}")
    print(f"  dual_mismatch_count          = {dual_mismatch}")
    print(f"  binance_only_count           = {binance_only}")
    print(f"  unresolved_fetch_error_count = {unresolved}")
    print("=" * 110)


if __name__ == "__main__":
    asyncio.run(main())
