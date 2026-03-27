"""
core/ofi_engine.py — V21 OFI / TFI hesaplayıcı.

OFI (Order Flow Imbalance):
    Her bar: sum(bid_delta - ask_delta) ilk N depth seviyesinde.
    Z-skoru geçmiş lookback_bars üzerinden hesaplanır.

TFI (Trade Flow Imbalance):
    Her bar: sum(buy_qty - sell_qty) — buy=isBuyerMaker=False.

Bağımsız test modu:
    python -m core.ofi_engine [saniye]
    Binance'e bağlanır, 30s (veya verilen süre) veri toplar, özet basar.
"""

import statistics
import time
import utils.logger as logger_mod
from collections import deque

log = logger_mod.get("ofi_engine")


class OFIEngine:
    def __init__(self, cfg: dict, state):
        ofi = cfg.get("ofi", {})

        self._state = state
        self._lookback: int = ofi.get("lookback_bars", 30)
        self._z_thr: float = ofi.get("z_threshold", 1.5)
        self._ratio_thr: float = ofi.get("ratio_threshold", 1.5)
        self._tfi_confirm: bool = ofi.get("tfi_confirmation", True)
        self._depth_levels: int = ofi.get("depth_levels", 10)
        self._bar_dur: float = ofi.get("snapshot_interval_s", 1.0)

        # Rolling bar history
        self._ofi_bars: deque = deque(maxlen=self._lookback)
        self._tfi_bars: deque = deque(maxlen=self._lookback)

        # Current bar accumulators
        self._ofi_acc: float = 0.0
        self._tfi_acc: float = 0.0
        self._bar_start: float = time.time()

        # Previous depth snapshot  (price → size)
        self._prev_bids: dict = {}
        self._prev_asks: dict = {}

        log.info(
            "OFIEngine hazır: lookback=%d z_thr=%.2f tfi_confirm=%s",
            self._lookback, self._z_thr, self._tfi_confirm,
        )

    # ── Depth callback (BinanceStream tarafından çağrılır) ──────────────────

    def on_depth(self, bids: list, asks: list) -> None:
        """
        bids/asks: [["price_str", "size_str"], ...]  (Binance depth20 formatı)
        """
        cur_bids = {
            float(b[0]): float(b[1])
            for b in bids[: self._depth_levels]
            if float(b[1]) > 0
        }
        cur_asks = {
            float(a[0]): float(a[1])
            for a in asks[: self._depth_levels]
            if float(a[1]) > 0
        }

        if self._prev_bids:
            bid_delta = sum(
                cur_bids.get(p, 0.0) - self._prev_bids.get(p, 0.0)
                for p in set(cur_bids) | set(self._prev_bids)
            )
            ask_delta = sum(
                cur_asks.get(p, 0.0) - self._prev_asks.get(p, 0.0)
                for p in set(cur_asks) | set(self._prev_asks)
            )
            self._ofi_acc += bid_delta - ask_delta

        self._prev_bids = cur_bids
        self._prev_asks = cur_asks

        # Bar tamamlandı mı?
        now = time.time()
        if now - self._bar_start >= self._bar_dur:
            self._close_bar()
            self._update_state()

    # ── Trade callback (BinanceStream tarafından çağrılır) ──────────────────

    def on_trade(self, price: float, qty: float, is_buyer_maker: bool) -> None:
        """
        is_buyer_maker=True  → satış baskısı (aggressive seller)
        is_buyer_maker=False → alış baskısı (aggressive buyer)
        """
        if is_buyer_maker:
            self._tfi_acc -= qty
        else:
            self._tfi_acc += qty

    # ── Bar yönetimi ─────────────────────────────────────────────────────────

    def _close_bar(self) -> None:
        self._ofi_bars.append(self._ofi_acc)
        self._tfi_bars.append(self._tfi_acc)
        self._ofi_acc = 0.0
        self._tfi_acc = 0.0
        self._bar_start = time.time()

    def _update_state(self) -> None:
        n = len(self._ofi_bars)
        self._state.ofi_n = n
        self._state.tfi_n = len(self._tfi_bars)

        if n < 3:
            return

        bars = list(self._ofi_bars)
        tfi_bars = list(self._tfi_bars)

        # OFI z-skoru
        mean_ofi = statistics.mean(bars)
        std_ofi = statistics.stdev(bars) if n >= 2 else 1.0
        if std_ofi < 1e-10:
            std_ofi = 1e-10

        latest_ofi = bars[-1]
        z = (latest_ofi - mean_ofi) / std_ofi

        # TFI son pencere (5 bar)
        tfi_recent = sum(tfi_bars[-5:]) if len(tfi_bars) >= 5 else sum(tfi_bars)

        # OFI ratio (son 5 vs geçmiş)
        if n >= 10:
            rec5 = sum(abs(x) for x in bars[-5:]) / 5.0
            old_n = n - 5
            older = sum(abs(x) for x in bars[:-5]) / old_n if old_n > 0 else 0.0
            ratio = rec5 / (older + 1e-10)
        else:
            ratio = 0.0

        # State güncelle
        self._state.ofi_value = latest_ofi
        self._state.ofi_z = z
        self._state.ofi_ratio = ratio
        self._state.tfi_value = tfi_recent

        # Sinyal kararı
        direction = ""
        if abs(z) >= self._z_thr:
            candidate = "UP" if z > 0 else "DOWN"
            tfi_agree = (tfi_recent > 0) if candidate == "UP" else (tfi_recent < 0)

            if not self._tfi_confirm or tfi_agree:
                direction = candidate

        self._state.signal_valid = bool(direction)
        self._state.signal_direction = direction
        self._state.signal_conviction = abs(z) / max(self._z_thr, 1e-10)

        log.debug(
            "Bar#%d ofi=%.1f z=%.3f tfi=%.2f ratio=%.2f dir=%s",
            n, latest_ofi, z, tfi_recent, ratio, direction or "—",
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Bağımsız test modu:  python -m core.ofi_engine [saniye]
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    import sys
    import json
    import os

    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    # Minimal config
    cfg_path = "config_v21.json"
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as _f:
            _cfg = json.load(_f)
    else:
        _cfg = {"ofi": {}}

    import utils.logger as _logger_mod
    _logger_mod.setup(_cfg, level=10)  # DEBUG level

    from core.state import SharedState
    from core.binance_ws import BinanceStream

    _state = SharedState(_cfg.get("risk", {}).get("bankroll_usd", 30.0))
    _engine = OFIEngine(_cfg, _state)
    _binance = BinanceStream(_state, _engine)

    async def _run():
        print(f"\n[OFI Test] {duration}s boyunca Binance'e bağlanılıyor...\n")
        task = asyncio.create_task(_binance.start())
        t0 = time.time()

        try:
            while time.time() - t0 < duration:
                await asyncio.sleep(2)
                elapsed = time.time() - t0
                print(
                    f"  t={elapsed:5.1f}s | "
                    f"BTC={_state.btc_price:,.2f} | "
                    f"OFI_N={_state.ofi_n} TFI_N={_state.tfi_n} | "
                    f"z={_state.ofi_z:+.3f} tfi={_state.tfi_value:+.2f} | "
                    f"valid={_state.signal_valid} dir={_state.signal_direction or '—'}"
                )
        finally:
            await _binance.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        print("\n── Test özeti ──────────────────────────────────────────")
        print(f"  OFI bar sayısı : {_state.ofi_n}")
        print(f"  TFI bar sayısı : {_state.tfi_n}")
        print(f"  Son OFI z      : {_state.ofi_z:+.4f}")
        print(f"  Son TFI        : {_state.tfi_value:+.4f}")
        print(f"  Son sinyal     : {_state.signal_direction or 'YOK'}")
        print(f"  BTC fiyatı     : {_state.btc_price:,.2f}")
        print("────────────────────────────────────────────────────────\n")

    asyncio.run(_run())
