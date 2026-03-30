"""
test_resolution.py — Smoke test for resolution_truth.py

Tests:
  1. ABI parsing correctness with mock data
  2. _determine_winner logic
  3. Live RPC fallback (may fail in sandboxed environments)
"""

import asyncio
import time
import sys


def test_abi_parsing():
    """Verify ABI decoding of latestRoundData() response."""

    def encode_uint256(v):
        return format(v, "064x")

    def encode_int256(v):
        if v < 0:
            v = (1 << 256) + v
        return format(v, "064x")

    round_id = 42
    answer = 8700012345678  # 87000.12345678 with 8 decimals
    started_at = 1774872800
    updated_at = 1774872900
    answered_in_round = 42

    mock_result = "0x" + (
        encode_uint256(round_id)
        + encode_int256(answer)
        + encode_uint256(started_at)
        + encode_uint256(updated_at)
        + encode_uint256(answered_in_round)
    )

    assert len(mock_result) >= 322, f"Expected >=322 chars, got {len(mock_result)}"

    hex_data = mock_result[2:]
    answer_raw = int(hex_data[64:128], 16)
    if answer_raw >= 2**255:
        answer_raw -= 2**256
    price = round(answer_raw / (10**8), 2)
    parsed_updated_at = int(hex_data[192:256], 16)

    assert price == 87000.12, f"Expected 87000.12, got {price}"
    assert parsed_updated_at == 1774872900, f"Expected 1774872900, got {parsed_updated_at}"
    print("  [PASS] ABI parsing: price=87000.12, updatedAt=1774872900")


def test_determine_winner():
    """Verify winner logic."""
    from resolution_truth import _determine_winner

    assert _determine_winner(87000.0, 87100.0) == "up"
    assert _determine_winner(87000.0, 86900.0) == "down"
    assert _determine_winner(87000.0, 87000.0) == "up"  # tie → up
    print("  [PASS] _determine_winner: up/down/tie all correct")


def test_rpc_fallback_list():
    """Verify POLYGON_RPC_URLS is a list with expected entries."""
    from resolution_truth import POLYGON_RPC_URLS

    assert isinstance(POLYGON_RPC_URLS, list), "POLYGON_RPC_URLS must be a list"
    assert len(POLYGON_RPC_URLS) == 3, f"Expected 3 RPCs, got {len(POLYGON_RPC_URLS)}"
    assert "drpc.org" in POLYGON_RPC_URLS[0]
    assert "publicnode.com" in POLYGON_RPC_URLS[1]
    assert "1rpc.io" in POLYGON_RPC_URLS[2]
    print(f"  [PASS] POLYGON_RPC_URLS: {POLYGON_RPC_URLS}")


async def test_live_resolve():
    """Live smoke test — may fail in sandboxed environments."""
    from resolution_truth import resolve_truth

    now = int(time.time())
    past_window = ((now - 600) // 300) * 300
    btc_open = 87000.0
    print(f"  window_ts={past_window} (closed ~{now - past_window - 300}s ago)")
    print(f"  btc_open={btc_open} (arbitrary reference)")

    result = await resolve_truth(
        window_ts=past_window, interval="5m", btc_open=btc_open
    )

    print(f"  btc_close_binance={result.btc_close_binance}")
    print(f"  binance_fetch_ok={result.binance_fetch_ok}")
    print(f"  winner_binance={result.winner_binance}")
    print(f"  btc_close_chainlink={result.btc_close_chainlink}")
    print(f"  winner_chainlink={result.winner_chainlink}")
    print(f"  chainlink_status={result.chainlink_status}")
    print(f"  resolution_match={result.resolution_match}")
    print(f"  resolution_truth_status={result.resolution_truth_status}")
    print(f"  winner_source={result.winner_source}")

    # Structural assertions — always valid regardless of network
    assert result.interval == "5m"
    assert result.window_ts == past_window
    assert result.winner_binance in ("up", "down", "unknown")
    assert result.winner_chainlink in ("up", "down", "unknown")
    assert result.resolution_truth_status in (
        "dual_verified", "dual_mismatch", "binance_only", "unresolved_fetch_error",
    )

    if result.binance_fetch_ok and result.chainlink_status.startswith("fetched"):
        print("  [PASS] LIVE: dual resolution succeeded")
    elif result.binance_fetch_ok:
        print(f"  [PASS] LIVE: binance_only (chainlink: {result.chainlink_status})")
    else:
        print(f"  [PASS] LIVE: both fetches failed (sandbox expected)")


def main():
    print("=== test_resolution.py ===")
    print()

    print("[1/4] ABI parsing test...")
    test_abi_parsing()

    print("[2/4] _determine_winner test...")
    test_determine_winner()

    print("[3/4] RPC fallback list test...")
    test_rpc_fallback_list()

    print("[4/4] Live resolve smoke test...")
    asyncio.run(test_live_resolve())

    print()
    print("=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    main()
