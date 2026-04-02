"""
main.py — Polymarket BTC Up/Down Paper Bot

Entry point. Async event loop:
  1. Config yükle
  2. Binance WebSocket başlat (open price capture)
  3. Her 5m pencere başında:
     a. Market discovery
     b. CLOB orderbook
     c. Entry window'da signal_engine'i çalıştır (dual veya single)
     d. Risk kontrolü → pozisyon aç
  4. Pencere kapanınca resolve_pending()
  5. Risk state log → kill kontrolü

Kullanım:
  python main.py
  python main.py --config path/to/config.json
  python main.py --once
"""

import sys
import asyncio

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        pass

import json
import os
import time
import uuid
import argparse
from pathlib import Path


def p(msg: str) -> None:
    print(msg, flush=True)

import logger as log_module
import truth_logger
from binance_feed import BinanceFeed
from polymarket_feed import PolymarketFeed
from market_discovery import discover_market
from signal_engine import SignalEngine
from risk_manager import RiskManager
from paper_trader import PaperTrader


CONFIG_PATH = Path(__file__).parent / "config.json"


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
        self.strategy: str = config.get("strategy", "dual_side_capture")

        # Session identity — stamped in every log entry via set_session_ctx()
        self.run_id: str = str(uuid.uuid4())
        self._pid: int = os.getpid()

        self.feed = BinanceFeed(
            ws_url=config.get("binance_ws", "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"),
            open_price_capture_secs=config.get("open_price_capture_secs", 5),
        )
        self.pm_feed = PolymarketFeed()
        self.signal_engine = SignalEngine(config)
        self.risk_manager = RiskManager(config)
        self.paper_trader = PaperTrader(config, risk_manager=self.risk_manager)

        self._running = False

        # Tick-driven state (reset at start of each _run_window)
        self._window_active = False
        self._window_token_up: str = ""
        self._window_token_down: str = ""
        self._window_ts: int = 0
        self._window_slug: str = ""
        self._signal_sent = False
        self._last_eval_sec: int = 0

        # Once-per-window event guards — prevent duplicate lifecycle log entries.
        # All reset at the start of each _run_window().
        self._first_cross_emitted: bool = False      # first_cross_detected
        self._window_decision_emitted: bool = False  # window_decision
        self._window_gate_reason_emitted: bool = False  # gate_reason
        self._last_gate_reason: str = ""             # tracks most recent substantive skip

    def _reset_window_state(self) -> None:
        """Reset all per-window state. Called at the top of _run_window()."""
        self._signal_sent = False
        self._last_eval_sec = 0
        self._first_cross_emitted = False
        self._window_decision_emitted = False
        self._window_gate_reason_emitted = False
        self._last_gate_reason = ""

    async def _on_btc_tick(self, btc_mid: float) -> None:
        """
        Binance bookTicker tick callback.
        Routes to dual_side_capture or single_side_taker based on config.
        """
        if not self._window_active or self._signal_sent:
            return

        # Tick throttle — max 1 evaluation per second
        current_sec = int(time.time())
        if current_sec == self._last_eval_sec:
            return
        self._last_eval_sec = current_sec

        divisor = 300 if self.interval == "5m" else 900
        secs_to_res = self._window_ts + divisor - int(time.time())

        book_up = self.pm_feed.get_book(self._window_token_up)
        book_down = self.pm_feed.get_book(self._window_token_down)

        # ── Strategy dispatch ──────────────────────────────────────────────────
        if self.strategy == "single_side_taker":
            btc_open_val = self.feed.open_price or btc_mid
            signal = self.signal_engine.evaluate_single(
                book_up=book_up,
                book_down=book_down,
                secs_to_res=secs_to_res,
                btc_mid=btc_mid,
                btc_open=btc_open_val,
            )
        else:
            signal = self.signal_engine.evaluate(
                book_up=book_up,
                book_down=book_down,
                secs_to_res=secs_to_res,
            )

        # ── Signal logging (non-trivial events only) ───────────────────────────
        if signal.action != "skip" or signal.reason not in ("outside_entry_window", "no_orderbook"):
            p(f"[tick] secs={secs_to_res} action={signal.action} side={signal.side!r} "
              f"entry={signal.entry_price:.4f} pair_sum={signal.pair_sum:.4f} "
              f"delta_bps={signal.delta_bps:.1f} reason={signal.reason}")
            await self.signal_engine.log_signal(signal)

        # ── Once-per-window: first_cross_detected ─────────────────────────────
        # Emitted the first time we are inside the entry evaluation zone
        # (any reason other than "outside_entry_window").
        if signal.reason != "outside_entry_window" and not self._first_cross_emitted:
            await log_module.log("first_cross_detected", {
                "window_ts": self._window_ts,
                "secs_to_res": secs_to_res,
                "strategy": self.strategy,
                "action": signal.action,
                "reason": signal.reason,
            })
            self._first_cross_emitted = True

        # ── Once-per-window: gate_reason ──────────────────────────────────────
        # Emitted the first time a substantive gate rejects the signal
        # (inside entry window, not just "no_orderbook").
        if (signal.action == "skip"
                and signal.reason not in ("outside_entry_window", "no_orderbook")
                and not self._window_gate_reason_emitted):
            await log_module.log("gate_reason", {
                "window_ts": self._window_ts,
                "reason": signal.reason,
                "secs_to_res": secs_to_res,
                "strategy": self.strategy,
                "up_ask": signal.up_ask,
                "down_ask": signal.down_ask,
                "spread_up": signal.spread_up,
                "spread_down": signal.spread_down,
                "delta_bps": signal.delta_bps,
            })
            self._window_gate_reason_emitted = True

        # Track most recent substantive skip for end-of-window window_decision
        if signal.action == "skip" and signal.reason not in ("outside_entry_window", "no_orderbook"):
            self._last_gate_reason = signal.reason

        if signal.action == "skip":
            return

        # ── Risk check — lock window if blocked ───────────────────────────────
        # Setting _signal_sent=True prevents contradictory blocked→opened outcome.
        can_trade, trade_reason = self.risk_manager.can_trade()
        if not can_trade:
            p(f"[tick] trade_blocked: {trade_reason}")
            self._signal_sent = True  # window locked — no further open attempt
            if not self._window_decision_emitted:
                await log_module.log("window_decision", {
                    "window_ts": self._window_ts,
                    "decision": "blocked",
                    "reason": trade_reason,
                    "strategy": self.strategy,
                    "side": signal.side,
                    "secs_to_res": secs_to_res,
                })
                self._window_decision_emitted = True
            return

        # ── Open position ──────────────────────────────────────────────────────
        shares = self.config.get("shares_per_side", 5)
        btc_open_val = self.feed.open_price or btc_mid

        market_ctx = {
            "market_slug": self._window_slug,
            "btc_mid_binance": btc_mid,
            "secs_to_res": secs_to_res,
            "up_ask": signal.up_ask,
            "down_ask": signal.down_ask,
        }
        if book_up:
            market_ctx["up_bid"] = book_up.bid
            market_ctx["spread_up_pct"] = round(book_up.spread_pct, 2)
        if book_down:
            market_ctx["down_bid"] = book_down.bid
            market_ctx["spread_down_pct"] = round(book_down.spread_pct, 2)

        if signal.action == "single_entry":
            pos = self.paper_trader.open_single_position(
                side=signal.side,
                entry_price=signal.entry_price,
                shares=shares,
                btc_open=btc_open_val,
                window_ts=self._window_ts,
                interval=self.interval,
                market_context=market_ctx,
            )
        else:  # dual_entry
            pos = self.paper_trader.open_position(
                up_ask=signal.up_ask,
                down_ask=signal.down_ask,
                shares=shares,
                btc_open=btc_open_val,
                window_ts=self._window_ts,
                interval=self.interval,
                market_context=market_ctx,
            )

        self._signal_sent = True

        # ── Once-per-window: window_decision (opened) ─────────────────────────
        if not self._window_decision_emitted:
            await log_module.log("window_decision", {
                "window_ts": self._window_ts,
                "decision": "opened",
                "trade_id": pos.trade_id,
                "strategy": self.strategy,
                "side": signal.side,
                "entry_price": signal.entry_price,
                "delta_bps": signal.delta_bps,
                "secs_to_res": secs_to_res,
            })
            self._window_decision_emitted = True

        # ── Canonical trade_opened write (single writer) ───────────────────────
        if signal.action == "single_entry":
            p(f"[SINGLE] {pos.trade_id} side={signal.side} "
              f"entry={signal.entry_price:.4f} delta={signal.delta_bps:.1f}bps shares={shares}")
            await log_module.log("trade_opened", {
                "trade_id": pos.trade_id,
                "window_ts": self._window_ts,
                "mode": self.mode,
                "strategy": "single_side_taker",
                "side": signal.side,
                "entry_price": signal.entry_price,
                "delta_bps": signal.delta_bps,
                "up_ask": signal.up_ask,
                "down_ask": signal.down_ask,
                "spread_up": signal.spread_up,
                "spread_down": signal.spread_down,
                "shares": shares,
                "btc_open": btc_open_val,
                "btc_mid": btc_mid,
                "secs_to_res": secs_to_res,
                "fee_source": self.paper_trader.fee_source,
                "fee_status": self.paper_trader.fee_status,
                "fee_rate": self.paper_trader.fee_rate,
                "fee_exponent": self.paper_trader.fee_exponent,
                "fee_exponent_source": self.paper_trader.fee_exponent_source,
                "tick_size": self.paper_trader.tick_size,
                "tick_size_source": self.paper_trader.tick_size_source,
                "min_order_size": self.paper_trader.min_order_size,
                "min_order_size_source": self.paper_trader.min_order_size_source,
                "execution_lane": self.paper_trader.execution_lane,
                **self.risk_manager.summary(),
            })
        else:
            p(f"[DUAL] {pos.trade_id} up_ask={signal.up_ask:.4f} down_ask={signal.down_ask:.4f} "
              f"pair_sum={signal.pair_sum:.4f} net_edge={signal.net_edge:.4f} shares={shares}")
            await log_module.log("trade_opened", {
                "trade_id": pos.trade_id,
                "window_ts": self._window_ts,
                "mode": self.mode,
                "strategy": "dual_side_capture",
                "up_ask": signal.up_ask,
                "down_ask": signal.down_ask,
                "pair_sum": signal.pair_sum,
                "net_edge": signal.net_edge,
                "shares": shares,
                "btc_open": btc_open_val,
                "secs_to_res": secs_to_res,
                "fee_source": self.paper_trader.fee_source,
                "fee_status": self.paper_trader.fee_status,
                "fee_rate": self.paper_trader.fee_rate,
                "fee_exponent": self.paper_trader.fee_exponent,
                "fee_exponent_source": self.paper_trader.fee_exponent_source,
                "tick_size": self.paper_trader.tick_size,
                "tick_size_source": self.paper_trader.tick_size_source,
                "min_order_size": self.paper_trader.min_order_size,
                "min_order_size_source": self.paper_trader.min_order_size_source,
                "execution_lane": self.paper_trader.execution_lane,
                **self.risk_manager.summary(),
            })

    async def _run_window(self) -> None:
        """Process one 5m window — tick-driven, no polling."""
        p("[window] market discovery başlıyor...")
        market = await discover_market(
            interval=self.interval,
            gamma_base=self.config.get("gamma_api_base", "https://gamma-api.polymarket.com"),
            clob_base=self.config.get("clob_api_base", "https://clob.polymarket.com"),
        )

        if not market:
            p("[window] SKIP: market bulunamadı veya <60s kaldı")
            log_module.log_sync("window_skip", {"reason": "no_market_found"})
            return

        p(f"[window] market bulundu: {market['slug']} secs_to_res={market['secs_to_resolution']}")

        # Market discovery fee attempt (once per window)
        if not self.paper_trader._market_fee_attempted:
            fee_result = await self.paper_trader.try_update_fee_from_market(market["token_up"])
            p(f"[window] market_fee_attempt: verified={fee_result['verified_remote']} "
              f"effective_source={self.paper_trader.fee_source} "
              f"fee_rate={self.paper_trader.fee_rate}")

        # Set window state for tick callbacks
        self._window_token_up = market["token_up"]
        self._window_token_down = market["token_down"]
        self._window_ts = market["window_ts"]
        self._window_slug = market.get("slug", "")

        # Reset all per-window guards
        self._reset_window_state()

        # Subscribe Polymarket feed
        p(f"[window] PM subscribe: up={market['token_up'][:16]}... down={market['token_down'][:16]}...")
        await self.pm_feed.resubscribe([market["token_up"], market["token_down"]])

        # Wait for first orderbook snapshot
        book_up = await self.pm_feed.wait_for_book(market["token_up"], timeout=12.0)
        if book_up:
            p(f"[window] PM orderbook hazır: up bid={book_up.bid} ask={book_up.ask} spread={book_up.spread_pct:.1f}%")
        else:
            p("[window] UYARI: 12s içinde PM orderbook gelmedi, devam ediliyor")

        # Open price capture
        self.feed.mark_window_open()
        await asyncio.sleep(0.2)
        if self.feed.open_price is None and self.feed.mid is not None:
            self.feed._open_price = self.feed.mid
            p(f"[window] open_price fallback: btc_open={self.feed.mid:.2f}")
            await log_module.log("open_price_fallback", {"btc_open": self.feed.mid})

        # Window active — tick callbacks now evaluate signals
        self._window_active = True
        p(f"[window] pencere aktif strategy={self.strategy} window_ts={self._window_ts}")

        divisor = 300 if self.interval == "5m" else 900
        last_status_secs = 999

        while self._running:
            secs_to_res = self._window_ts + divisor - int(time.time())

            if secs_to_res <= 0:
                p("[window] pencere kapandı")
                break

            if self.risk_manager.is_killed:
                self._running = False
                break

            if last_status_secs - secs_to_res >= 30:
                bu = self.pm_feed.get_book(market["token_up"])
                bd = self.pm_feed.get_book(market["token_down"])
                if bu and bd:
                    pair_sum = round(bu.ask + bd.ask, 4)
                    p(f"[window] secs={secs_to_res} pair_sum={pair_sum:.4f} "
                      f"spread_up={bu.spread_pct:.1f}% spread_down={bd.spread_pct:.1f}% "
                      f"signal_sent={self._signal_sent}")
                    await truth_logger.observe("quote_snapshot", {
                        "market_slug": market.get("slug", ""),
                        "interval": self.interval,
                        "execution_lane": self.paper_trader.execution_lane,
                        "secs_to_res": secs_to_res,
                        "pair_sum": pair_sum,
                        "up_bid": bu.bid,
                        "up_ask": bu.ask,
                        "down_bid": bd.bid,
                        "down_ask": bd.ask,
                        "spread_up_pct": round(bu.spread_pct, 2),
                        "spread_down_pct": round(bd.spread_pct, 2),
                        "btc_mid_binance": self.feed.mid,
                        "fee_source": self.paper_trader.fee_source,
                        "fee_status": self.paper_trader.fee_status,
                    })
                else:
                    p(f"[window] secs={secs_to_res} orderbook=? signal_sent={self._signal_sent}")
                last_status_secs = secs_to_res

            await asyncio.sleep(1)

        # Window closed
        self._window_active = False

        # ── Once-per-window: window_decision (no_signal / gate_fail) ──────────
        # Emitted only if no trade was opened and no block was logged.
        if not self._window_decision_emitted:
            decision = "gate_fail" if self._last_gate_reason else "no_signal"
            await log_module.log("window_decision", {
                "window_ts": self._window_ts,
                "decision": decision,
                "reason": self._last_gate_reason or "no_entry_cross",
                "strategy": self.strategy,
            })
            self._window_decision_emitted = True

        # Resolve
        open_pos = self.paper_trader.open_positions()
        if open_pos:
            wait = self.config.get("resolve_confirm_secs", 130)
            p(f"[resolve] {len(open_pos)} pozisyon, {wait}s bekleniyor...")
            await self.paper_trader.resolve_pending(wait_secs=wait)
            await self.risk_manager.log_state()
            unresolved = self.paper_trader.unresolved_positions()
            p(f"[resolve] tamamlandı — net_pnl={self.paper_trader.total_pnl():.4f} USDC "
              f"unresolved={len(unresolved)}")
        else:
            p("[window] bu pencerede açık pozisyon yok")

    async def run(self, run_once: bool = False) -> None:
        self._running = True

        # Inject run_id + pid into every log entry for this session
        log_module.set_session_ctx({"run_id": self.run_id, "pid": self._pid})

        p(f"[run] bot_start mode={self.mode} strategy={self.strategy} "
          f"interval={self.interval} run_id={self.run_id} pid={self._pid}")
        log_module.log_sync("bot_start", {
            "mode": self.mode,
            "strategy": self.strategy,
            "market_type": self.interval,
            "bankroll": self.config.get("bankroll", 30.0),
            "fee_rate": self.paper_trader.fee_rate,
            "fee_source": self.paper_trader.fee_source,
            "fee_status": self.paper_trader.fee_status,
            "fee_exponent": self.paper_trader.fee_exponent,
            "fee_exponent_source": self.paper_trader.fee_exponent_source,
            "tick_size": self.paper_trader.tick_size,
            "tick_size_source": self.paper_trader.tick_size_source,
            "min_order_size": self.paper_trader.min_order_size,
            "min_order_size_source": self.paper_trader.min_order_size_source,
            "execution_lane": self.paper_trader.execution_lane,
            # single-side config snapshot
            "signal_mode": self.config.get("signal_mode", ""),
            "forced_side": self.config.get("forced_side", ""),
            "min_move_bps": self.config.get("min_move_bps", ""),
            "max_entry_price": self.config.get("max_entry_price", ""),
        })

        p("[run] Binance WebSocket feed başlatılıyor...")
        self.feed.subscribe(self._on_btc_tick)
        feed_task = self.feed.start()
        p("[run] feed task oluşturuldu, ilk tick bekleniyor (max 8s)...")
        first_mid = await self.feed.wait_for_mid(timeout=8.0)
        if first_mid:
            p(f"[run] Binance bağlandı — BTC mid={first_mid:.2f}")
        else:
            p("[run] UYARI: 8s içinde Binance tick gelmedi, devam ediliyor")

        p("[run] Polymarket CLOB WebSocket feed başlatılıyor...")
        pm_task = self.pm_feed.start()

        try:
            while self._running:
                secs_left = secs_to_next_window(self.interval)
                p(f"[run] sonraki pencereye {secs_left:.1f}s ({self.interval})")

                await log_module.log("window_wait", {
                    "secs_to_next": round(secs_left, 1),
                    "interval": self.interval,
                })

                if run_once:
                    p("[run] --once modu: mevcut pencereyi hemen işliyorum")
                else:
                    sleep_secs = max(0, secs_left + 2)
                    p(f"[run] {sleep_secs:.1f}s bekleniyor (yeni pencere +2s)")
                    await asyncio.sleep(sleep_secs)

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
            self._window_active = False
            await self.feed.stop()
            await self.pm_feed.stop()
            summary = self.risk_manager.summary()
            net_pnl = round(self.paper_trader.total_pnl(), 4)
            stats = self.paper_trader.summary_stats()
            p(f"[run] bot_stop net_pnl={net_pnl} bankroll={summary.get('bankroll')} "
              f"unresolved={stats.get('unresolved_count', 0)}")
            p(f"[RAPOR] trades={stats['trades']} "
              f"gross={stats.get('total_gross_pnl', 0.0)} "
              f"fee={stats.get('total_fee_paid', 0.0)} "
              f"net={stats.get('total_net_pnl', 0.0)} "
              f"avg_edge={stats['avg_net_edge']} "
              f"win_up={stats['win_up']} win_down={stats['win_down']} "
              f"unresolved={stats.get('unresolved_count', 0)} "
              f"fee_source={stats.get('fee_source', 'unknown')} "
              f"fee_status={stats.get('fee_status', 'unknown')} "
              f"lane={stats.get('execution_lane', 'unknown')}")
            log_module.log_sync("bot_stop", {
                "net_pnl": net_pnl,
                **summary,
                **stats,
            })


async def main() -> None:
    p("[1/5] argparse...")
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down Paper Bot")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--once", action="store_true", help="Sadece bir pencereyi işle")
    args = parser.parse_args()
    p(f"[2/5] config yükleniyor: {args.config}")

    config = load_config(args.config)
    p(f"[3/5] config OK — mode={config.get('mode')} strategy={config.get('strategy')} "
      f"market={config.get('market_type')} bankroll={config.get('bankroll')}")

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
