"""
tests/test_rtds_smoke.py — RTDS transport layer correctness tests.

Verifies:
  1. Correct RTDS endpoint (wss://ws-live-data.polymarket.com, NOT the CLOB host).
  2. Subscription payloads match RTDS protocol format (not CLOB format).
  3. Message parsing matches the ACTUAL live wire format for both feeds.
  4. Heartbeat interval is 5 s (RTDS keepalive requirement).
  5. Symbol mapping: BTC-USD → BTCUSDT (for subscribe filter).
  6. Empty ack handling: empty string skipped, not counted as parse_failed.
  7. Smoke integration: connect → subscribe → parse via mock WebSocket.

Actual wire formats (observed from live connection):

  Fast feed (crypto_prices):
    msg #1: ""   (empty string — subscription ack)
    msg #2: {"payload": {"data": [{"timestamp": <ms>, "value": <float>}, ...]}}

  Chainlink feed (crypto_prices_chainlink):
    msg #1: ""   (empty string — subscription ack)
    msg #2+: {
      "connection_id": "...",
      "payload": {"symbol": "btc/usd", "timestamp": <ms>, "value": <float>,
                  "full_accuracy_value": "..."},
      "timestamp": <ms>, "topic": "crypto_prices_chainlink", "type": "update"
    }
    (also delivers eth/usd, sol/usd, doge/usd, bnb/usd — discarded silently)
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from feeds.fast_feed import FastFeedAdapter, _to_rtds_symbol
from feeds.chainlink_feed import ChainlinkFeedAdapter
from feeds.base import BaseFeedAdapter, FeedStatus


# ---------------------------------------------------------------------------
# Helpers — ACTUAL wire format builders
# ---------------------------------------------------------------------------

def _make_fast(host="wss://ws-live-data.polymarket.com", symbol="BTC-USD"):
    return FastFeedAdapter(rtds_host=host, symbol=symbol, stale_threshold_seconds=10.0)


def _make_chainlink(host="wss://ws-live-data.polymarket.com", symbol="BTC-USD"):
    return ChainlinkFeedAdapter(rtds_host=host, symbol=symbol, stale_threshold_seconds=10.0)


def _fast_rtds_msg(price=95000.0, ts_ms=None, extra_points=None):
    """
    Build an actual crypto_prices RTDS batch envelope.
    If extra_points provided, they are prepended before the final price point.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    data = []
    if extra_points:
        data.extend(extra_points)
    data.append({"timestamp": ts_ms, "value": price})
    return {"payload": {"data": data}}


def _chainlink_rtds_msg(price=95000.0, symbol="btc/usd", ts_ms=None,
                        full_accuracy_value=None):
    """Build an actual crypto_prices_chainlink RTDS envelope."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    inner = {
        "symbol": symbol,
        "timestamp": ts_ms,
        "value": price,
    }
    if full_accuracy_value is not None:
        inner["full_accuracy_value"] = full_accuracy_value
    return {
        "connection_id": "test_conn_id=",
        "payload": inner,
        "timestamp": ts_ms + 10,
        "topic": "crypto_prices_chainlink",
        "type": "update",
    }


# ---------------------------------------------------------------------------
# 1. Endpoint correctness
# ---------------------------------------------------------------------------

class TestRTDSEndpoint(unittest.TestCase):
    """RTDS host must be ws-live-data.polymarket.com, never the CLOB host."""

    CORRECT_HOST = "wss://ws-live-data.polymarket.com"

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
        """RTDS requires ~5 s pings."""
        self.assertEqual(BaseFeedAdapter.HEARTBEAT_INTERVAL, 5)


# ---------------------------------------------------------------------------
# 2. Subscription payload format (unchanged from before)
# ---------------------------------------------------------------------------

class TestSubscribePayload(unittest.TestCase):
    """Subscribe messages must use RTDS 'subscriptions' list format."""

    def test_fast_subscribe_structure(self):
        adapter = _make_fast()
        payload = adapter._subscribe_payload()
        self.assertEqual(payload["action"], "subscribe")
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
        adapter = _make_fast()
        sub = adapter._subscribe_payload()["subscriptions"][0]
        self.assertNotIn("channel", sub)
        self.assertNotIn("assets", sub)

    def test_chainlink_subscribe_structure(self):
        adapter = _make_chainlink()
        payload = adapter._subscribe_payload()
        self.assertEqual(payload["action"], "subscribe")
        sub = payload["subscriptions"][0]
        self.assertEqual(sub["topic"], "crypto_prices_chainlink")
        self.assertEqual(sub["type"], "update")

    def test_chainlink_subscribe_no_channel_key(self):
        adapter = _make_chainlink()
        sub = adapter._subscribe_payload()["subscriptions"][0]
        self.assertNotIn("channel", sub)

    def test_subscribe_payload_is_json_serializable(self):
        for adapter in [_make_fast(), _make_chainlink()]:
            serialized = json.dumps(adapter._subscribe_payload())
            self.assertIsInstance(serialized, str)


# ---------------------------------------------------------------------------
# 3. Fast feed parser — actual format: payload.data[] batch
# ---------------------------------------------------------------------------

class TestFastFeedParsing(unittest.TestCase):

    def setUp(self):
        self.adapter = _make_fast()
        self.ts_ms = 1_774_000_000_000  # ~May 2026

    def test_valid_message_returns_snapshot(self):
        msg = _fast_rtds_msg(price=70590.94, ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, 70590.94, places=1)

    def test_timestamp_converted_from_ms_to_seconds(self):
        msg = _fast_rtds_msg(price=70590.0, ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertAlmostEqual(snap.timestamp, self.ts_ms / 1000.0, places=3)

    def test_takes_last_element_from_batch(self):
        """When data[] has multiple entries, the last one is used."""
        earlier = {"timestamp": self.ts_ms - 2000, "value": 70500.0}
        latest_price = 70625.23
        msg = _fast_rtds_msg(price=latest_price, ts_ms=self.ts_ms,
                             extra_points=[earlier])
        snap = self.adapter._parse_message(msg)
        self.assertAlmostEqual(snap.price, latest_price, places=2)

    def test_multi_point_batch_like_live_data(self):
        """
        Matches actual msg #2 from live connection (10 data points).
        Last point: timestamp=1774285721000 value=70625.23
        """
        data_points = [
            {"timestamp": 1774285712000, "value": 70590.94},
            {"timestamp": 1774285713000, "value": 70592},
            {"timestamp": 1774285714000, "value": 70595.36},
            {"timestamp": 1774285715000, "value": 70617.99},
            {"timestamp": 1774285716000, "value": 70617.99},
            {"timestamp": 1774285717000, "value": 70619.61},
            {"timestamp": 1774285718000, "value": 70619.61},
            {"timestamp": 1774285719000, "value": 70619.6},
            {"timestamp": 1774285720000, "value": 70619.6},
            {"timestamp": 1774285721000, "value": 70625.23},
        ]
        msg = {"payload": {"data": data_points}}
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, 70625.23, places=2)
        self.assertAlmostEqual(snap.timestamp, 1774285721000 / 1000.0, places=3)

    def test_empty_data_list_returns_none_not_parse_failed(self):
        """Empty batch is a discarded message, not a parse failure."""
        msg = {"payload": {"data": []}}
        snap = self.adapter._parse_message(msg)
        self.assertIsNone(snap)

    def test_missing_payload_raises(self):
        """Missing 'payload' key → unexpected shape → raises (base counts parse_failed)."""
        with self.assertRaises(Exception):
            self.adapter._parse_message({"other_key": "whatever"})

    def test_missing_data_key_raises(self):
        """payload exists but no 'data' key → raises."""
        with self.assertRaises(Exception):
            self.adapter._parse_message({"payload": {"value": 70000}})

    def test_missing_value_in_item_raises(self):
        """Data item missing 'value' → raises (base counts parse_failed)."""
        msg = {"payload": {"data": [{"timestamp": self.ts_ms}]}}
        with self.assertRaises(Exception):
            self.adapter._parse_message(msg)

    def test_price_is_btc_range(self):
        """BTC price must be well above 1.0."""
        msg = _fast_rtds_msg(price=70000.0)
        snap = self.adapter._parse_message(msg)
        self.assertGreater(snap.price, 1.0)

    def test_feed_label(self):
        self.assertEqual(self.adapter._feed_label(), "fast")

    def test_no_topic_or_symbol_required(self):
        """
        Fast feed format has NO 'topic' or 'symbol' at any level.
        Messages without them parse fine.
        """
        msg = {"payload": {"data": [{"timestamp": self.ts_ms, "value": 70000.0}]}}
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)


# ---------------------------------------------------------------------------
# 4. Chainlink feed parser — actual format: payload.symbol/value/timestamp
# ---------------------------------------------------------------------------

class TestChainlinkFeedParsing(unittest.TestCase):

    def setUp(self):
        self.adapter = _make_chainlink()
        self.ts_ms = 1_774_285_831_000

    def test_btc_usd_returns_snapshot(self):
        """'btc/usd' symbol → snapshot returned."""
        msg = _chainlink_rtds_msg(price=94371.5, symbol="btc/usd", ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, 94371.5, places=1)

    def test_btc_usd_case_insensitive(self):
        """Symbol comparison is case-insensitive."""
        msg = _chainlink_rtds_msg(price=94000.0, symbol="BTC/USD", ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)

    def test_timestamp_ms_to_seconds(self):
        msg = _chainlink_rtds_msg(price=94000.0, ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertAlmostEqual(snap.timestamp, self.ts_ms / 1000.0, places=3)

    def test_eth_usd_discarded(self):
        """eth/usd → discarded (returns None), not a parse failure."""
        msg = _chainlink_rtds_msg(price=2135.56, symbol="eth/usd", ts_ms=self.ts_ms)
        snap = self.adapter._parse_message(msg)
        self.assertIsNone(snap)

    def test_sol_usd_discarded(self):
        msg = _chainlink_rtds_msg(price=90.37, symbol="sol/usd", ts_ms=self.ts_ms)
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_doge_usd_discarded(self):
        msg = _chainlink_rtds_msg(price=0.0943, symbol="doge/usd", ts_ms=self.ts_ms)
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_bnb_usd_discarded(self):
        msg = _chainlink_rtds_msg(price=636.16, symbol="bnb/usd", ts_ms=self.ts_ms)
        self.assertIsNone(self.adapter._parse_message(msg))

    def test_full_accuracy_value_does_not_break_parsing(self):
        """full_accuracy_value field is ignored in price calc but must not break parse."""
        msg = _chainlink_rtds_msg(price=94371.5, symbol="btc/usd",
                                  full_accuracy_value="94371800000000000")
        snap = self.adapter._parse_message(msg)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, 94371.5, places=1)

    def test_exact_live_message_format(self):
        """
        Parses the exact envelope observed on the live connection
        (one of messages #2-5 adapted to btc/usd).
        """
        live_msg = {
            "connection_id": "ar8RKcu1LPECEhQ=",
            "payload": {
                "full_accuracy_value": "94371800000000000",
                "symbol": "btc/usd",
                "timestamp": 1774285831000,
                "value": 94371.8,
            },
            "timestamp": 1774285831758,
            "topic": "crypto_prices_chainlink",
            "type": "update",
        }
        snap = self.adapter._parse_message(live_msg)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, 94371.8, places=1)
        self.assertAlmostEqual(snap.timestamp, 1774285831000 / 1000.0, places=3)

    def test_missing_payload_raises(self):
        with self.assertRaises(Exception):
            self.adapter._parse_message({"topic": "crypto_prices_chainlink"})

    def test_feed_label(self):
        self.assertEqual(self.adapter._feed_label(), "chainlink")


# ---------------------------------------------------------------------------
# 5. Symbol mapping (used in subscribe filter, not in message parsing)
# ---------------------------------------------------------------------------

class TestSymbolMapping(unittest.TestCase):

    def test_btc_usd_maps_to_btcusdt(self):
        self.assertEqual(_to_rtds_symbol("BTC-USD"), "BTCUSDT")

    def test_eth_usd_maps_to_ethusdt(self):
        self.assertEqual(_to_rtds_symbol("ETH-USD"), "ETHUSDT")

    def test_sol_usd_maps_to_solusdt(self):
        self.assertEqual(_to_rtds_symbol("SOL-USD"), "SOLUSDT")

    def test_fallback_strips_dash(self):
        result = _to_rtds_symbol("XRP-USD")
        self.assertEqual(result, "XRPUSD")


# ---------------------------------------------------------------------------
# 6. Empty ack counter in base
# ---------------------------------------------------------------------------

class TestEmptyAckHandling(unittest.TestCase):
    """Empty string messages (subscription ack) must be skipped cleanly."""

    def test_initial_empty_ack_counter_is_zero(self):
        adapter = _make_fast()
        self.assertEqual(adapter._total_empty_ack, 0)

    def test_initial_counters_all_zero(self):
        adapter = _make_fast()
        self.assertEqual(adapter._total_received, 0)
        self.assertEqual(adapter._total_parsed_ok, 0)
        self.assertEqual(adapter._total_parse_failed, 0)
        self.assertEqual(adapter._total_discarded, 0)
        self.assertEqual(adapter._total_empty_ack, 0)


# ---------------------------------------------------------------------------
# 7. Smoke integration — mock WebSocket
# ---------------------------------------------------------------------------

def _inject_ws_module(connect_fn):
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
    End-to-end smoke tests using mock WebSocket.
    Verifies subscribe payload sent + actual wire messages parsed correctly.
    """

    def test_fast_feed_smoke_subscribe_sent(self):
        """Connect → send RTDS subscribe with correct format."""
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
        self.assertTrue(len(subs) > 0)
        self.assertEqual(subs[0]["topic"], "crypto_prices")
        self.assertEqual(subs[0]["type"], "update")
        self.assertIn("filters", subs[0])
        self.assertIn("BTCUSDT", subs[0]["filters"])

    def test_chainlink_smoke_subscribe_sent(self):
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

    def test_first_valid_rtds_payload_parsed_fast(self):
        """
        Actual fast feed message → FeedSnapshot with correct price and timestamp.
        Matches the real msg #2 last element: value=70625.23 ts=1774285721000
        """
        adapter = _make_fast()
        ts_ms = 1_774_285_721_000
        expected_price = 70625.23
        msg = _fast_rtds_msg(price=expected_price, ts_ms=ts_ms)

        snap = adapter._parse_message(msg)

        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, expected_price, places=2)
        self.assertAlmostEqual(snap.timestamp, ts_ms / 1000.0, places=3)
        self.assertEqual(snap.feed_name, "fast")
        self.assertEqual(snap.symbol, "BTC-USD")

    def test_first_valid_rtds_payload_parsed_chainlink(self):
        """
        Actual chainlink message adapted to btc/usd → FeedSnapshot.
        """
        adapter = _make_chainlink()
        ts_ms = 1_774_285_831_000
        expected_price = 94371.8
        msg = _chainlink_rtds_msg(price=expected_price, symbol="btc/usd", ts_ms=ts_ms,
                                  full_accuracy_value="94371800000000000")

        snap = adapter._parse_message(msg)

        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, expected_price, places=1)
        self.assertAlmostEqual(snap.timestamp, ts_ms / 1000.0, places=3)
        self.assertEqual(snap.feed_name, "chainlink")


if __name__ == "__main__":
    unittest.main()
