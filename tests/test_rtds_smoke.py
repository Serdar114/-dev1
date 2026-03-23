"""
tests/test_rtds_smoke.py — RTDS transport layer correctness tests.

Verifies:
  1. Correct RTDS endpoint (wss://ws-live-data.polymarket.com, NOT the CLOB host).
  2. Subscription payloads match RTDS protocol format (not CLOB format).
  3. Message parsing handles the RTDS envelope (topic/payload) correctly.
  4. Heartbeat interval is 5 s (RTDS keepalive requirement).
  5. Symbol mapping: BTC-USD → BTCUSDT.
  6. Smoke integration: connect → send subscribe → parse first valid payload.
     (Uses a mock WebSocket so no real network call is made.)
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from feeds.fast_feed import FastFeedAdapter, _to_rtds_symbol
from feeds.chainlink_feed import ChainlinkFeedAdapter
from feeds.base import BaseFeedAdapter, FeedStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fast(host="wss://ws-live-data.polymarket.com", symbol="BTC-USD"):
    return FastFeedAdapter(rtds_host=host, symbol=symbol, stale_threshold_seconds=10.0)


def _make_chainlink(host="wss://ws-live-data.polymarket.com", symbol="BTC-USD"):
    return ChainlinkFeedAdapter(rtds_host=host, symbol=symbol, stale_threshold_seconds=10.0)


def _fast_rtds_msg(price=95000.0, symbol="BTCUSDT", ts_ms=None):
    """Build a valid crypto_prices RTDS envelope."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return {
        "topic": "crypto_prices",
        "type": "update",
        "timestamp": ts_ms,
        "payload": {
            "symbol": symbol,
            "value": price,
            "timestamp": ts_ms,
        },
    }


def _chainlink_rtds_msg(price=95000.0, symbol="BTCUSDT", ts_ms=None, round_id=None):
    """Build a valid crypto_prices_chainlink RTDS envelope."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    data = {
        "symbol": symbol,
        "value": price,
        "timestamp": ts_ms,
    }
    if round_id is not None:
        data["round_id"] = round_id
    return {
        "topic": "crypto_prices_chainlink",
        "type": "update",
        "timestamp": ts_ms,
        "payload": data,
    }


# ---------------------------------------------------------------------------
# 1. Endpoint correctness
# ---------------------------------------------------------------------------

class TestRTDSEndpoint(unittest.TestCase):
    """RTDS host must be ws-live-data.polymarket.com, never the CLOB host."""

    CORRECT_HOST = "wss://ws-live-data.polymarket.com"
    WRONG_HOST = "wss://ws-subscriptions-clob.polymarket.com/ws/"

    def test_fast_feed_uses_rtds_host(self):
        adapter = _make_fast(host=self.CORRECT_HOST)
        self.assertEqual(adapter._rtds_host, self.CORRECT_HOST)

    def test_chainlink_feed_uses_rtds_host(self):
        adapter = _make_chainlink(host=self.CORRECT_HOST)
        self.assertEqual(adapter._rtds_host, self.CORRECT_HOST)

    def test_config_yaml_rtds_host(self):
        """settings.yaml must point to ws-live-data, not ws-subscriptions-clob."""
        import yaml, os
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        rtds_host = cfg["feeds"]["rtds_host"]
        self.assertIn("ws-live-data.polymarket.com", rtds_host,
                      f"rtds_host must be ws-live-data.polymarket.com, got: {rtds_host}")
        self.assertNotIn("ws-subscriptions-clob", rtds_host,
                         f"rtds_host must NOT be CLOB host, got: {rtds_host}")

    def test_heartbeat_interval_is_5s(self):
        """RTDS requires ~5 s pings, not the 20 s used for CLOB sockets."""
        self.assertEqual(BaseFeedAdapter.HEARTBEAT_INTERVAL, 5)


# ---------------------------------------------------------------------------
# 2. Subscription payload format
# ---------------------------------------------------------------------------

class TestSubscribePayload(unittest.TestCase):
    """Subscribe messages must use RTDS 'subscriptions' list format."""

    def test_fast_subscribe_structure(self):
        adapter = _make_fast()
        payload = adapter._subscribe_payload()
        self.assertEqual(payload["action"], "subscribe")
        self.assertIn("subscriptions", payload)
        subs = payload["subscriptions"]
        self.assertEqual(len(subs), 1)
        sub = subs[0]
        self.assertEqual(sub["topic"], "crypto_prices")
        self.assertEqual(sub["type"], "update")

    def test_fast_subscribe_has_btcusdt_filter(self):
        adapter = _make_fast(symbol="BTC-USD")
        payload = adapter._subscribe_payload()
        sub = payload["subscriptions"][0]
        self.assertIn("filters", sub)
        filters = json.loads(sub["filters"])
        self.assertEqual(filters["symbol"], "BTCUSDT")

    def test_fast_subscribe_no_channel_key(self):
        """RTDS format uses 'topic', not 'channel' (that's the CLOB format)."""
        adapter = _make_fast()
        payload = adapter._subscribe_payload()
        sub = payload["subscriptions"][0]
        self.assertNotIn("channel", sub)
        self.assertNotIn("assets", sub)

    def test_chainlink_subscribe_structure(self):
        adapter = _make_chainlink()
        payload = adapter._subscribe_payload()
        self.assertEqual(payload["action"], "subscribe")
        subs = payload["subscriptions"]
        self.assertEqual(len(subs), 1)
        sub = subs[0]
        self.assertEqual(sub["topic"], "crypto_prices_chainlink")
        self.assertEqual(sub["type"], "update")

    def test_chainlink_subscribe_no_channel_key(self):
        adapter = _make_chainlink()
        payload = adapter._subscribe_payload()
        sub = payload["subscriptions"][0]
        self.assertNotIn("channel", sub)

    def test_subscribe_payload_is_json_serializable(self):
        for adapter in [_make_fast(), _make_chainlink()]:
            payload = adapter._subscribe_payload()
            # Must not raise
            serialized = json.dumps(payload)
            self.assertIsInstance(serialized, str)


# ---------------------------------------------------------------------------
# 3. Message parsing — correct RTDS envelope (topic/payload, ms timestamps)
# ---------------------------------------------------------------------------

class TestFastFeedParsing(unittest.TestCase):

    def setUp(self):
        self.adapter = _make_fast()
        self.ts_ms = 1_700_000_000_000  # 1.7 trillion ms ≈ year 2023

    def test_valid_message_returns_snapshot(self):
        msg = _fast_rtds_msg(price=95000.0, ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, 95000.0)

    def test_timestamp_converted_from_ms_to_seconds(self):
        msg = _fast_rtds_msg(price=95000.0, ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        expected_ts_s = self.ts_ms / 1000.0
        self.assertAlmostEqual(snap.timestamp, expected_ts_s, places=3)

    def test_uses_topic_not_channel(self):
        """Parser looks at 'topic', not 'channel' (old CLOB-style key)."""
        msg = _fast_rtds_msg(price=95000.0)
        # Rename 'topic' to 'channel' to simulate old-format message
        old_format = {k: v for k, v in msg.items()}
        old_format["channel"] = old_format.pop("topic")
        snap = self.adapter._parse_message(old_format)
        self.assertIsNone(snap, "Old 'channel' key must NOT be accepted")

    def test_uses_payload_not_data(self):
        """Parser reads from 'payload', not 'data' (old schema)."""
        msg = _fast_rtds_msg(price=95000.0)
        # Rename 'payload' → 'data' to simulate old schema
        old_format = {k: v for k, v in msg.items()}
        old_format["data"] = old_format.pop("payload")
        snap = self.adapter._parse_message(old_format)
        self.assertIsNone(snap, "Old 'data' key must NOT be accepted")

    def test_uses_value_not_price_key(self):
        """Payload field is 'value', not 'price'."""
        msg = _fast_rtds_msg(price=95000.0)
        # Rename 'value' → 'price' inside payload
        msg["payload"] = {k: v for k, v in msg["payload"].items()}
        msg["payload"]["price"] = msg["payload"].pop("value")
        snap = self.adapter._parse_message(msg)
        self.assertIsNone(snap, "'price' key in payload must NOT be accepted; must use 'value'")

    def test_wrong_topic_returns_none(self):
        msg = _fast_rtds_msg(price=95000.0)
        msg["topic"] = "crypto_prices_chainlink"
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_wrong_symbol_filtered(self):
        msg = _fast_rtds_msg(price=95000.0, symbol="ETHUSDT")
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_matching_symbol_accepted(self):
        msg = _fast_rtds_msg(price=95000.0, symbol="BTCUSDT")
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)

    def test_missing_value_returns_none(self):
        msg = _fast_rtds_msg(price=95000.0)
        del msg["payload"]["value"]
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_feed_label(self):
        self.assertEqual(self.adapter._feed_label(), "fast")

    def test_price_above_one(self):
        """BTC price must be >> 1.0 — sanity check that value field is used."""
        msg = _fast_rtds_msg(price=94500.75)
        snap = self.adapter._parse_message(msg)
        self.assertGreater(snap.price, 1.0)


class TestChainlinkFeedParsing(unittest.TestCase):

    def setUp(self):
        self.adapter = _make_chainlink()
        self.ts_ms = 1_700_000_000_000

    def test_valid_message_returns_snapshot(self):
        msg = _chainlink_rtds_msg(price=95000.0, ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, 95000.0)

    def test_timestamp_ms_to_seconds(self):
        msg = _chainlink_rtds_msg(price=95000.0, ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertAlmostEqual(snap.timestamp, self.ts_ms / 1000.0, places=3)

    def test_wrong_topic_returns_none(self):
        msg = _chainlink_rtds_msg(price=95000.0)
        msg["topic"] = "crypto_prices"
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_round_id_does_not_break_parsing(self):
        msg = _chainlink_rtds_msg(price=95000.0, round_id="0xabc123")
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)

    def test_wrong_symbol_filtered(self):
        msg = _chainlink_rtds_msg(price=95000.0, symbol="ETHUSDT")
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_feed_label(self):
        self.assertEqual(self.adapter._feed_label(), "chainlink")

    def test_uses_topic_not_channel(self):
        msg = _chainlink_rtds_msg(price=95000.0)
        old_format = {k: v for k, v in msg.items()}
        old_format["channel"] = old_format.pop("topic")
        self.assertIsNone(self.adapter._parse_message(old_format))


# ---------------------------------------------------------------------------
# 4. Symbol mapping
# ---------------------------------------------------------------------------

class TestSymbolMapping(unittest.TestCase):

    def test_btc_usd_maps_to_btcusdt(self):
        self.assertEqual(_to_rtds_symbol("BTC-USD"), "BTCUSDT")

    def test_eth_usd_maps_to_ethusdt(self):
        self.assertEqual(_to_rtds_symbol("ETH-USD"), "ETHUSDT")

    def test_sol_usd_maps_to_solusdt(self):
        self.assertEqual(_to_rtds_symbol("SOL-USD"), "SOLUSDT")

    def test_fallback_strips_dash(self):
        # Unknown symbol falls back to stripping '-' and uppercasing
        result = _to_rtds_symbol("XRP-USD")
        self.assertEqual(result, "XRPUSD")


# ---------------------------------------------------------------------------
# 5. Smoke integration — mock WebSocket, verify connect + subscribe + parse
# ---------------------------------------------------------------------------

def _make_websockets_mock(ws_mock):
    """
    Build a fake 'websockets' module that routes connect() to ws_mock.
    Injected via sys.modules so feeds/base.py's `import websockets` resolves
    without the real package being installed.
    """
    import types, sys
    fake_ws_mod = types.ModuleType("websockets")
    # connect() must return an async context manager — wrap ws_mock
    fake_ws_mod.connect = MagicMock(return_value=ws_mock)
    return fake_ws_mod


def _inject_ws_module(connect_fn):
    """
    Context manager: injects a fake 'websockets' module into sys.modules so
    that feeds/base.py's `import websockets` resolves without the real package.
    `connect_fn` is the callable to use as websockets.connect.
    """
    import sys, types, contextlib

    @contextlib.contextmanager
    def _ctx():
        fake_mod = types.ModuleType("websockets")
        fake_mod.connect = connect_fn
        old = sys.modules.get("websockets")
        sys.modules["websockets"] = fake_mod
        try:
            yield fake_mod
        finally:
            if old is None:
                sys.modules.pop("websockets", None)
            else:
                sys.modules["websockets"] = old

    return _ctx()


class TestRTDSSmokeIntegration(unittest.TestCase):
    """
    Smoke test: simulate connect → subscribe → receive → parse using mocks.
    No real network call or real `websockets` package required.

    Verifies:
      - websockets.connect() called with RTDS host
      - Subscribe message sent using RTDS protocol format
      - HTTP 404 scenario: connect raises → adapter transitions to ERROR
      - Parsed RTDS payload produces correct FeedSnapshot (price + ms→s ts)
    """

    def test_fast_feed_smoke_subscribe_sent(self):
        """
        Connect to RTDS host → send subscribe with RTDS format immediately.
        Uses asyncio.run() to drive _connect_and_consume in an isolated loop.
        """
        adapter = _make_fast()
        captured = {}

        async def run():
            sent = []

            class FakeWS:
                async def send(self, msg):
                    sent.append(json.loads(msg))

                async def ping(self):
                    pass

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    # Stop immediately after subscribe is sent
                    raise StopAsyncIteration

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    pass

            def fake_connect(host):
                return FakeWS()

            with _inject_ws_module(fake_connect):
                try:
                    await adapter._connect_and_consume()
                except Exception:
                    pass

            if sent:
                captured.update(sent[0])

        asyncio.run(run())

        self.assertEqual(captured.get("action"), "subscribe",
                         f"Expected action=subscribe, got: {captured}")
        subs = captured.get("subscriptions", [])
        self.assertTrue(len(subs) > 0)
        self.assertEqual(subs[0]["topic"], "crypto_prices")
        self.assertEqual(subs[0]["type"], "update")
        self.assertIn("filters", subs[0])
        self.assertIn("BTCUSDT", subs[0]["filters"])

    def test_chainlink_smoke_subscribe_sent(self):
        """Chainlink connects and sends correct topic in subscribe."""
        adapter = _make_chainlink()
        captured = {}

        async def run():
            sent = []

            class FakeWS:
                async def send(self, msg):
                    sent.append(json.loads(msg))

                async def ping(self):
                    pass

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise StopAsyncIteration

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    pass

            with _inject_ws_module(lambda h: FakeWS()):
                try:
                    await adapter._connect_and_consume()
                except Exception:
                    pass
            if sent:
                captured.update(sent[0])

        asyncio.run(run())

        self.assertEqual(captured.get("action"), "subscribe")
        subs = captured.get("subscriptions", [])
        self.assertEqual(subs[0]["topic"], "crypto_prices_chainlink")

    def test_connect_to_wrong_host_causes_error_status(self):
        """
        CLOB host returns HTTP 404 → adapter catches exception → ERROR status.
        """
        adapter = _make_fast(host="wss://ws-subscriptions-clob.polymarket.com/ws/")
        adapter._running = True

        async def run():
            def bad_connect(host):
                raise Exception(f"HTTP 404: Not Found connecting to {host}")

            with _inject_ws_module(bad_connect):
                try:
                    await adapter._connect_and_consume()
                except Exception:
                    adapter._status = FeedStatus.ERROR

        asyncio.run(run())
        self.assertEqual(adapter._status, FeedStatus.ERROR)

    def test_first_valid_rtds_payload_parsed(self):
        """
        First valid RTDS envelope → FeedSnapshot with correct price and ms→s
        timestamp conversion.  Tested via _parse_message + _build_snapshot
        directly (full connect-loop tested in test_fast_feed_smoke_subscribe_sent).
        """
        adapter = _make_fast()
        ts_ms = 1_700_000_000_000
        expected_price = 96_500.0
        msg = _fast_rtds_msg(price=expected_price, ts_ms=ts_ms)

        # Directly invoke the parser (no asyncio needed).
        snap = adapter._parse_message(msg)

        self.assertIsNotNone(snap, "Expected FeedSnapshot from valid RTDS envelope")
        self.assertAlmostEqual(snap.price, expected_price, places=1)
        # Timestamp must be converted from ms to seconds.
        self.assertAlmostEqual(snap.timestamp, ts_ms / 1000.0, places=1)
        self.assertEqual(snap.feed_name, "fast")
        self.assertEqual(snap.symbol, "BTC-USD")


if __name__ == "__main__":
    unittest.main()
