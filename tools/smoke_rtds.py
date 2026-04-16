"""
Standalone smoke test for the Polymarket RTDS WebSocket connection.

Connects for 30 seconds and prints Binance + Chainlink BTC price ticks.
If no tick arrives within 10 seconds of connection, prints SUBSCRIBE FAILED
and exits with code 1.

Usage:
  python tools/smoke_rtds.py

Expected output:
  [00:01] connected
  [00:01] subscribed binance + chainlink
  [00:02] binance btcusdt = 84231.50
  [00:04] chainlink btc/usd = 84228.00
  ...
  [00:30] FINAL: binance ticks=120, chainlink ticks=18
"""
import json
import os
import sys
import threading
import time
from typing import Optional

# Allow import from parent directory when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import websocket
except ImportError:
    print("ERROR: websocket-client not installed.")
    print("Run: pip install websocket-client")
    sys.exit(1)

RTDS_ENDPOINT = "wss://ws-live-data.polymarket.com"
PING_INTERVAL_S = 5
TEST_DURATION_S = 30
NO_TICK_TIMEOUT_S = 10

# Verified subscribe payloads (JSON-encoded filters required by server).
SUBSCRIBE_BINANCE = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices",
            "type": "update",
            "filters": json.dumps({"symbol": "btcusdt"}),
        }
    ],
})

SUBSCRIBE_CHAINLINK = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": json.dumps({"symbol": "btc/usd"}),
        }
    ],
})


class _SmokeTest:
    def __init__(self) -> None:
        self._connect_ts: float = 0.0
        self._binance_ticks: int = 0
        self._chainlink_ticks: int = 0
        self._first_tick_ts: Optional[float] = None  # type: ignore[name-defined]
        self._subscribe_rejected: bool = False
        self._done = threading.Event()
        self._ws: Optional[websocket.WebSocketApp] = None  # type: ignore[name-defined]
        self._exit_code: int = 0

    def _elapsed(self) -> str:
        elapsed = int(time.time() - self._connect_ts) if self._connect_ts else 0
        return f"{elapsed // 60:02d}:{elapsed % 60:02d}"

    def on_open(self, ws: "websocket.WebSocketApp") -> None:
        self._connect_ts = time.time()
        print(f"[{self._elapsed()}] connected")
        ws.send(SUBSCRIBE_BINANCE)
        ws.send(SUBSCRIBE_CHAINLINK)
        print(f"[{self._elapsed()}] subscribed binance + chainlink")

        # Ping thread
        def _ping() -> None:
            while not self._done.is_set():
                try:
                    ws.send("PING")
                except Exception:
                    break
                self._done.wait(PING_INTERVAL_S)

        threading.Thread(target=_ping, daemon=True, name="smoke-ping").start()

        # No-tick timeout
        def _timeout_check() -> None:
            self._done.wait(NO_TICK_TIMEOUT_S)
            if self._first_tick_ts is None and not self._subscribe_rejected:
                print(
                    f"[{self._elapsed()}] SUBSCRIBE FAILED — "
                    f"no ticks received in {NO_TICK_TIMEOUT_S}s"
                )
                self._exit_code = 1
                self._done.set()
                try:
                    ws.close()
                except Exception:
                    pass

        threading.Thread(
            target=_timeout_check, daemon=True, name="smoke-timeout"
        ).start()

        # Duration timer
        def _duration() -> None:
            self._done.wait(TEST_DURATION_S)
            try:
                ws.close()
            except Exception:
                pass

        threading.Thread(
            target=_duration, daemon=True, name="smoke-duration"
        ).start()

    def on_message(self, ws: "websocket.WebSocketApp", message: str) -> None:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return

        # Detect subscribe rejection
        body = data.get("body")
        if isinstance(body, dict):
            msg = body.get("message", "")
            if "invalid Subscription" in msg or "does not match regex" in msg:
                self._subscribe_rejected = True
                print(f"[{self._elapsed()}] SUBSCRIBE REJECTED: {msg[:200]}")
                return

        topic = data.get("topic")
        payload = data.get("payload")
        if not topic or not payload:
            return

        if self._first_tick_ts is None:
            self._first_tick_ts = time.time()

        if topic == "crypto_prices":
            symbol = payload.get("symbol")
            value = payload.get("value")
            if symbol == "btcusdt" and value is not None:
                self._binance_ticks += 1
                print(f"[{self._elapsed()}] binance btcusdt = {float(value):.2f}")

        elif topic == "crypto_prices_chainlink":
            symbol = payload.get("symbol")
            value = payload.get("value")
            if symbol == "btc/usd" and value is not None:
                self._chainlink_ticks += 1
                print(f"[{self._elapsed()}] chainlink btc/usd = {float(value):.2f}")

    def on_error(self, ws: "websocket.WebSocketApp", error: Exception) -> None:
        print(f"[{self._elapsed()}] ERROR: {error}")

    def on_close(
        self,
        ws: "websocket.WebSocketApp",
        close_status_code,
        close_msg,
    ) -> None:
        print(
            f"\n[{self._elapsed()}] FINAL: "
            f"binance ticks={self._binance_ticks}, "
            f"chainlink ticks={self._chainlink_ticks}"
        )
        self._done.set()

    def run(self) -> int:
        print(f"Connecting to {RTDS_ENDPOINT}  (test duration: {TEST_DURATION_S}s) ...")
        self._ws = websocket.WebSocketApp(
            RTDS_ENDPOINT,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        self._ws.run_forever()
        return self._exit_code


def main() -> None:
    test = _SmokeTest()
    sys.exit(test.run())


if __name__ == "__main__":
    main()
