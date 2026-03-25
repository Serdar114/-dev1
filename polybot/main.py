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

import sys
import asyncio

# Windows: ProactorEventLoop (DNS + WebSocket uyumluluğu için zorunlu)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import time
import argparse
from pathlib import Path


def p(msg: str) -> None:
    """Anında stdout'a yaz (Windows'ta flush önemli)."""
    print(msg, flush=True)

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
        p("[window] market discovery başlıyor...")
        market = await discover_market(
            interval=self.interval,
            gamma_base=self.config.get("gamma_api_base", "https://gamma-api.polymarket.com"),
            clob_base=self.config.get("clob_api_base", "https://clob.polymarket.com"),
        )

        if not market:
            p("[window] SKIP: market bulunamadı")
            log_module.log_sync("window_skip", {"reason": "no_market_found"})
            return

        p(f"[window] market bulundu: {market['slug']} secs_to_res={market['secs_to_resolution']}")

        token_up = market["token_up"]
        token_down = market["token_down"]
        clob_base = self.config.get("clob_api_base", "https://clob.polymarket.com")

        self.feed.mark_window_open()
        p("[window] open_price capture başladı (ilk 5s Binance mid)")

        # Feed bağlıysa ama open_price capture penceresi kaçtıysa: mevcut mid'i fallback yap
        await asyncio.sleep(0.2)  # tick'in işlenmesi için kısa fırsat
        if self.feed.open_price is None and self.feed.mid is not None:
            self.feed._open_price = self.feed.mid
            p(f"[window] open_price fallback: btc_open={self.feed.mid:.2f} (WS geç bağlandı)")
            await log_module.log("open_price_fallback", {"btc_open": self.feed.mid})

        signal_sent = False
        poll_count = 0

        while self._running:
            secs_to_res = market["window_ts"] + (300 if self.interval == "5m" else 900) - int(time.time())

            if secs_to_res <= 0:
                p("[window] pencere kapandı (secs_to_res=0)")
                break

            can_trade, trade_reason = self.risk_manager.can_trade()

            if not can_trade:
                p(f"[window] trade_blocked: {trade_reason}")
                log_module.log_sync("trade_blocked", {
                    "reason": trade_reason,
                    "secs_to_res": secs_to_res,
                })
                if self.risk_manager.is_killed:
                    self._running = False
                    return
                break

            open_price = self.feed.open_price
            current_price = self.feed.mid
            poll_count += 1

            if poll_count % 3 == 1:  # her ~15s bir durum satırı
                p(f"[window] secs_to_res={secs_to_res} btc_open={open_price} btc_mid={current_price} signal_sent={signal_sent}")

            if not signal_sent and open_price and current_price:
                p(f"[window] midpoint sorgulanıyor... (secs_to_res={secs_to_res})")
                p_up = await get_orderbook_midpoint(token_up, clob_base)
                p_down = await get_orderbook_midpoint(token_down, clob_base)
                p(f"[window] midpoint: up={p_up} down={p_down}")

                if p_up and p_down:
                    signal = self.signal_engine.evaluate(
                        open_price=open_price,
                        current_price=current_price,
                        p_entry_up=p_up,
                        p_entry_down=p_down,
                        secs_to_res=secs_to_res,
                    )
                    await self.signal_engine.log_signal(signal)
                    p(f"[signal] action={signal.action} direction={signal.direction} delta={signal.delta_pct:.3f}% edge={signal.edge_pct:.2f}% reason={signal.reason}")

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
                        p(f"[TRADE] {pos.trade_id} dir={direction} shares={shares} p={p_entry:.4f} edge={signal.edge_pct:.2f}%")
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
        open_pos = self.paper_trader.open_positions()
        if open_pos:
            wait = self.config.get("resolve_confirm_secs", 130)
            p(f"[resolve] {len(open_pos)} pozisyon bekliyor, {wait}s Chainlink konfirmasyonu...")
            await self.paper_trader.resolve_pending(wait_secs=wait)
            await self.risk_manager.log_state()
            p(f"[resolve] tamamlandı — total_pnl={self.paper_trader.total_pnl():.4f} USDC")
        else:
            p("[window] bu pencerede açık pozisyon yok, resolve atlandı")

    async def run(self, run_once: bool = False) -> None:
        self._running = True
        p(f"[run] bot_start mode={self.mode} interval={self.interval}")
        log_module.log_sync("bot_start", {
            "mode": self.mode,
            "market_type": self.interval,
            "bankroll": self.config.get("bankroll", 30.0),
        })

        # Binance feed başlat
        p("[run] Binance WebSocket feed başlatılıyor...")
        self.feed.subscribe(self._on_new_price)
        feed_task = self.feed.start()
        p("[run] feed task oluşturuldu, ilk tick bekleniyor (max 8s)...")
        first_mid = await self.feed.wait_for_mid(timeout=8.0)
        if first_mid:
            p(f"[run] Binance bağlandı — BTC mid={first_mid:.2f}")
        else:
            p("[run] UYARI: 8s içinde Binance tick gelmedi, devam ediliyor")

        try:
            while self._running:
                secs_left = secs_to_next_window(self.interval)
                p(f"[run] sonraki pencereye {secs_left:.1f}s ({self.interval})")

                await log_module.log("window_wait", {
                    "secs_to_next": round(secs_left, 1),
                    "interval": self.interval,
                })

                if run_once:
                    # --once: mevcut pencereyi hemen işle, bekletme
                    p("[run] --once modu: mevcut pencereyi hemen işliyorum")
                else:
                    await asyncio.sleep(max(0, secs_left - 1))

                p("[run] _run_window() başlıyor...")
                await self._run_window()
                p("[run] _run_window() bitti")

                if run_once:
                    break

                if self.risk_manager.is_killed:
                    p(f"[run] KILL: {self.risk_manager.kill_reason}")
                    log_module.log_sync("bot_killed", self.risk_manager.summary())
                    break

        except asyncio.CancelledError:
            pass
        finally:
            await self.feed.stop()
            summary = self.risk_manager.summary()
            total_pnl = round(self.paper_trader.total_pnl(), 4)
            p(f"[run] bot_stop total_pnl={total_pnl} bankroll={summary.get('bankroll')}")
            log_module.log_sync("bot_stop", {
                "total_pnl": total_pnl,
                **summary,
            })


async def main() -> None:
    p("[1/5] argparse...")
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down Paper Bot")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--once", action="store_true", help="Sadece bir pencereyi işle")
    args = parser.parse_args()
    p(f"[2/5] config yükleniyor: {args.config}")

    config = load_config(args.config)
    p(f"[3/5] config OK — mode={config.get('mode')} market={config.get('market_type')} bankroll={config.get('bankroll')}")

    p("[4/5] PolyBot oluşturuluyor...")
    bot = PolyBot(config)
    p(f"[5/5] run başlıyor (once={args.once}) ...")
    await bot.run(run_once=args.once)


if __name__ == "__main__":
    p("=== PolyBot başlatılıyor ===")
    if sys.platform == "win32":
        p("Windows modu: ProactorEventLoop aktif")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        p("\nDurduruldu.")
        sys.exit(0)
