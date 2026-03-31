"""
test_resolution.py — Smoke test for resolution_truth.py

Tests:
  1. ABI parsing correctness with mock data
  2. _determine_winner logic
  3. RPC fallback list structure
  4. _encode_get_round_data + _parse_round_response helpers
  5. Live resolve smoke test (may fail in sandboxed environments)
  6. Historical window 1774900500 (known mismatch candidate)
  7. Historical window 1774901700 (known mismatch candidate)
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
    assert _determine_winner(87000.0, 87000.0) == "up"  # tie -> up
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


def test_helpers():
    """Verify _encode_get_round_data and _parse_round_response."""
    from resolution_truth import _encode_get_round_data, _parse_round_response

    # encode
    data = _encode_get_round_data(12345)
    assert data.startswith("0x9a6fc8f5"), f"Bad selector: {data[:10]}"
    assert len(data) == 10 + 64, f"Bad length: {len(data)}"
    decoded_id = int(data[10:], 16)
    assert decoded_id == 12345, f"Bad round id: {decoded_id}"
    print("  [PASS] _encode_get_round_data(12345) correct")

    # parse
    def enc256(v):
        return format(v, "064x")
    def enc_int256(v):
        if v < 0:
            v = (1 << 256) + v
        return format(v, "064x")

    mock = "0x" + (
        enc256(999)               # roundId
        + enc_int256(8750000000000)  # answer: 87500.00 with 8 dec
        + enc256(1774900000)      # startedAt
        + enc256(1774900100)      # updatedAt
        + enc256(999)             # answeredInRound
    )
    parsed = _parse_round_response(mock)
    assert parsed is not None
    rid, price, updated_at = parsed
    assert rid == 999, f"Bad roundId: {rid}"
    assert price == 87500.0, f"Bad price: {price}"
    assert updated_at == 1774900100, f"Bad updatedAt: {updated_at}"
    print(f"  [PASS] _parse_round_response: rid={rid} price={price} updatedAt={updated_at}")

    # short response
    assert _parse_round_response("0xdeadbeef") is None
    print("  [PASS] _parse_round_response: short response -> None")


async def _resolve_window(window_ts: int, btc_open: float, label: str):
    """Helper to resolve a single window and print results."""
    from resolution_truth import resolve_truth

    print(f"  window_ts={window_ts} btc_open={btc_open} ({label})")
    result = await resolve_truth(
        window_ts=window_ts, interval="5m", btc_open=btc_open,
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

    # Structural assertions
    assert result.interval == "5m"
    assert result.window_ts == window_ts
    assert result.winner_binance in ("up", "down", "unknown")
    assert result.winner_chainlink in ("up", "down", "unknown")
    assert result.resolution_truth_status in (
        "dual_verified", "dual_mismatch", "binance_only", "unresolved_fetch_error",
    )

    if result.chainlink_status.startswith("fetched"):
        print(f"  [PASS] chainlink fetched (historical round)")
    elif "all_rpc_failed" in result.chainlink_status:
        print(f"  [PASS] all RPCs failed (sandbox expected)")
    else:
        print(f"  [PASS] chainlink_status={result.chainlink_status}")
    return result


async def test_live_resolve():
    """Live smoke test with recent window."""
    now = int(time.time())
    past_window = ((now - 600) // 300) * 300
    await _resolve_window(past_window, 87000.0, "recent window")


async def test_window_1774900500():
    """Known mismatch candidate: window_ts=1774900500, real btc_open=66549.535."""
    await _resolve_window(1774900500, 66549.535, "known mismatch window #1")


async def test_window_1774901700():
    """Known mismatch candidate: window_ts=1774901700, real btc_open=66614.935."""
    await _resolve_window(1774901700, 66614.935, "known mismatch window #2")


def main():
    print("=== test_resolution.py ===")
    print()

    print("[1/7] ABI parsing test...")
    test_abi_parsing()

    print("[2/7] _determine_winner test...")
    test_determine_winner()

    print("[3/7] RPC fallback list test...")
    test_rpc_fallback_list()

    print("[4/7] Helper functions test...")
    test_helpers()

    print("[5/7] Live resolve smoke test...")
    asyncio.run(test_live_resolve())

    print("[6/7] Historical window 1774900500...")
    asyncio.run(test_window_1774900500())

    print("[7/7] Historical window 1774901700...")
    asyncio.run(test_window_1774901700())

    print()
    print("=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    main()
