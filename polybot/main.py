"""
main.py — Polymarket BTC Up/Down Paper Bot

Entry point. Async event loop:
  1. Config yükle
  2. Binance WebSocket başlat (open price capture)
  3. Her 5m pencere başında:
     a. Market discovery (Gamma API + clobTokenIds fix)
     b. CLOB midpoint fiyatlarını çek
     c. Entry window'da signal_engine'i çalıştır
     d. Risk kontrolü → pozisyon aç
  4. Pencere kapanınca resolve_pending()
  5. Risk state log → kill kontrolü

Kullanım:
  python main.py
  python main.py --config path/to/config.json
  python main.py --once   # sadece mevcut pencereyi işle, çık
"""

import asyncio
import json
import sys
import time
import argparse
from pathlib import Path

import logger as log_module
from binance_feed import BinanceFeed
from market_discovery import discover_market, get_orderbook_midpoint, get_market_fee_rate
from signal_engine import SignalEngine
from risk_manager import RiskManager
from paper_trader import PaperTrader


CONFIG_PATH = Path(__file__).parent / "config.json"
POLL_INTERVAL = 5  # saniye


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def secs_to_next_window(interval: str = "5m") -> float:
    divisor = 300 if interval == "5m" else 900
    now = time.time()
    return divisor - (now % divisor)


class PolyBot:
    def __init__(self, config: dict):
        self.config = config
        self.interval: str = config.get("market_type", "5m")
        self.mode: str = config.get("mode", "paper")

        self.feed = BinanceFeed(
            ws_url=config.get("binance_ws", "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"),
            open_price_capture_secs=config.get("open_price_capture_secs", 5),
        )
        self.signal_engine = SignalEngine(config)
        self.risk_manager = RiskManager(config)
        self.paper_trader = PaperTrader(config, risk_manager=self.risk_manager)

        self._running = False

    async def _on_new_price(self, mid: float) -> None:
        """Binance subscriber callback — her tick'te çağrılır."""
        pass  # main loop poll ediyor

    async def _run_window(self) -> None:
        """Tek bir 5m pencereyi işle."""
        # Market discover
        market = await discover_market(
            interval=self.interval,
            gamma_base=self.config.get("gamma_api_base", "https://gamma-api.polymarket.com"),
            clob_base=self.config.get("clob_api_base", "https://clob.polymarket.com"),
        )

        if not market:
            log_module.log_sync("window_skip", {"reason": "no_market_found"})
            return

        token_up = market["token_up"]
        token_down = market["token_down"]
        clob_base = self.config.get("clob_api_base", "https://clob.polymarket.com")

        # Open price için pencere başını işaretle
        self.feed.mark_window_open()

        # Entry window girene kadar bekle ve sinyal ara
        signal_sent = False

        while self._running:
            secs_to_res = market["window_ts"] + (300 if self.interval == "5m" else 900) - int(time.time())

            if secs_to_res <= 0:
                break  # pencere kapandı

            can_trade, trade_reason = self.risk_manager.can_trade()

            if not can_trade:
                log_module.log_sync("trade_blocked", {
                    "reason": trade_reason,
                    "secs_to_res": secs_to_res,
                })
                if self.risk_manager.is_killed:
                    self._running = False
                    return
                break

            if not signal_sent:
                open_price = self.feed.open_price
                current_price = self.feed.mid

                if open_price and current_price:
                    # Midpoint fiyatları çek
                    p_up = await get_orderbook_midpoint(token_up, clob_base)
                    p_down = await get_orderbook_midpoint(token_down, clob_base)

                    if p_up and p_down:
                        signal = self.signal_engine.evaluate(
                            open_price=open_price,
                            current_price=current_price,
                            p_entry_up=p_up,
                            p_entry_down=p_down,
                            secs_to_res=secs_to_res,
                        )
                        await self.signal_engine.log_signal(signal)

                        if signal.action != "skip":
                            shares = self.risk_manager.get_shares()
                            direction = signal.direction
                            p_entry = p_up if direction == "up" else p_down

                            pos = self.paper_trader.open_position(
                                direction=direction,
                                shares=shares,
                                p_entry=p_entry,
                                open_price=open_price,
                                window_ts=market["window_ts"],
                            )
                            await log_module.log("trade_opened", {
                                "trade_id": pos.trade_id,
                                "mode": self.mode,
                                "direction": direction,
                                "shares": shares,
                                "p_entry": p_entry,
                                "signal_p": signal.p_signal,
                                "edge_pct": round(signal.edge_pct, 2),
                                "delta_pct": round(signal.delta_pct, 4),
                                "btc_open": open_price,
                                "btc_current": current_price,
                                "secs_to_res": secs_to_res,
                                **self.risk_manager.summary(),
                            })
                            signal_sent = True

            await asyncio.sleep(POLL_INTERVAL)

        # Pencere kapandı → resolve
        if self.paper_trader.open_positions():
            await self.paper_trader.resolve_pending(
                wait_secs=self.config.get("resolve_confirm_secs", 130)
            )
            await self.risk_manager.log_state()

    async def run(self, run_once: bool = False) -> None:
        self._running = True
        log_module.log_sync("bot_start", {
            "mode": self.mode,
            "market_type": self.interval,
            "bankroll": self.config.get("bankroll", 30.0),
        })

        # Binance feed başlat
        self.feed.subscribe(self._on_new_price)
        feed_task = self.feed.start()

        try:
            while self._running:
                # Pencere başında aç
                secs_left = secs_to_next_window(self.interval)
                next_open = time.time() + secs_left

                await log_module.log("window_wait", {
                    "secs_to_next": round(secs_left, 1),
                    "interval": self.interval,
                })
                await asyncio.sleep(max(0, secs_left - 1))  # 1s erken uyan

                # Pencere işle
                await self._run_window()

                if run_once:
                    break

                if self.risk_manager.is_killed:
                    log_module.log_sync("bot_killed", self.risk_manager.summary())
                    break

        except asyncio.CancelledError:
            pass
        finally:
            await self.feed.stop()
            log_module.log_sync("bot_stop", {
                "total_pnl": round(self.paper_trader.total_pnl(), 4),
                **self.risk_manager.summary(),
            })


async def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down Paper Bot")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--once", action="store_true", help="Sadece bir pencereyi işle")
    args = parser.parse_args()

    config = load_config(args.config)
    bot = PolyBot(config)
    await bot.run(run_once=args.once)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDurduruldu.")
        sys.exit(0)
