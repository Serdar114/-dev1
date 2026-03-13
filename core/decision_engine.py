"""
core/decision_engine.py — V21 karar motoru.

Kurallar:
  • Maker-only giriş (paper: bid fiyatından simüle)
  • 5m piyasalar only (15m kapalı)
  • Sabit $5 stake
  • Normal bant: 0.55–0.72 | Yüksek conviction: 0.65–0.80
  • Maker penceresi: 45s → 10s kalan süre
  • Min edge: %8
  • Taker girişi: KAPALI
  • Exit: TP1 (+0.06), TP2 (+0.10), Hard stop (-0.04), Signal flip
"""

import asyncio
import json
import time
import uuid
import utils.logger as logger_mod
from core.state import Position
from core.bankroll import Bankroll

log = logger_mod.get("decision_engine")


class DecisionEngine:
    def __init__(self, cfg: dict, state, bankroll: Bankroll):
        self._cfg = cfg
        self._state = state
        self._bankroll = bankroll
        self._running = False

        entry = cfg.get("entry", {})
        exit_cfg = cfg.get("exit", {})
        risk = cfg.get("risk", {})

        # Entry parametreleri
        self._normal_band: tuple = (
            entry.get("normal_band_min", 0.55),
            entry.get("normal_band_max", 0.72),
        )
        self._high_conv_band: tuple = (
            entry.get("high_conv_band_min", 0.65),
            entry.get("high_conv_band_max", 0.80),
        )
        self._maker_max_s: float = entry.get("maker_window_max_s", 45.0)
        self._maker_min_s: float = entry.get("maker_window_min_s", 10.0)
        self._min_edge_pct: float = entry.get("min_edge_pct", 8.0)
        self._min_liquidity: float = entry.get("min_liquidity_usd", 50.0)
        self._cooldown_s: float = entry.get("cooldown_s", 60.0)
        self._scan_s: float = entry.get("scan_interval_s", 10.0)
        self._stake: float = risk.get("stake_usd", 5.0)

        # Exit parametreleri
        self._tp1: float = exit_cfg.get("tp1_delta", 0.06)
        self._tp2: float = exit_cfg.get("tp2_delta", 0.10)
        self._hstop: float = exit_cfg.get("hard_stop_delta", 0.04)
        self._flip_exit: bool = exit_cfg.get("signal_flip_exit", True)

        # Son giriş zamanı (slug → timestamp)
        self._last_entry: dict = {}

        log.info(
            "DecisionEngine hazır: band=%.2f-%.2f stake=$%.2f "
            "window=%.0f-%.0fs edge>=%.0f%%",
            self._normal_band[0], self._normal_band[1],
            self._stake, self._maker_min_s, self._maker_max_s,
            self._min_edge_pct,
        )

    # ── Ana döngü ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Tick hatası: %s", exc, exc_info=True)
            await asyncio.sleep(self._scan_s)

    async def _tick(self) -> None:
        state = self._state

        # Önce açık pozisyonları yönet
        await self._manage_exits()

        # Market yok → bekle
        if not state.market.slug:
            return

        # Sinyal yok → giriş yok
        if not state.signal_valid:
            return

        # Maker penceresi kontrolü
        secs = state.seconds_to_market_end()
        if secs < self._maker_min_s or secs > self._maker_max_s:
            return

        # Cooldown kontrolü
        slug = state.market.slug
        if time.time() - self._last_entry.get(slug, 0) < self._cooldown_s:
            return

        # Bankroll / risk kontrolü
        can, reason = self._bankroll.can_open()
        if not can:
            log.debug("Giriş engellendi: %s", reason)
            return

        # Book verisi taze mi?
        if not state.is_book_fresh(self._cfg.get("stale_data_limit_ms", 5000)):
            return

        # Giriş fiyatı ve token
        direction = state.signal_direction
        entry_price, token_id = self._maker_entry_price(direction)
        if entry_price is None:
            return

        # Bant kontrolü
        band = self._high_conv_band if state.signal_conviction >= 1.5 else self._normal_band
        if not (band[0] <= entry_price <= band[1]):
            log.debug(
                "Giriş fiyatı %.3f bant dışında [%.2f–%.2f]",
                entry_price, band[0], band[1],
            )
            return

        # Edge hesabı
        edge_pct = self._calc_edge(entry_price, state.signal_conviction)
        if edge_pct < self._min_edge_pct:
            log.debug("Edge %.1f%% < min %.1f%%", edge_pct, self._min_edge_pct)
            return

        # Pozisyon aç
        await self._open(direction, entry_price, token_id, edge_pct)

    # ── Giriş fiyatı ─────────────────────────────────────────────────────────

    def _maker_entry_price(self, direction: str):
        """
        Maker order simülasyonu: bid fiyatından al.
        Returns: (entry_price, token_id) ya da (None, None)
        """
        book = self._state.book
        market = self._state.market

        if direction == "UP":
            price = book.up_bid
            token_id = market.up_token_id
        elif direction == "DOWN":
            price = book.down_bid
            token_id = market.down_token_id
        else:
            return None, None

        if price <= 0 or not token_id:
            return None, None

        return price, token_id

    # ── Edge hesabı ──────────────────────────────────────────────────────────

    def _calc_edge(self, entry_price: float, conviction: float) -> float:
        """
        Edge % = (tahmini_kazanma_olasiligi - giriş_fiyatı) / giriş_fiyatı * 100

        Tahmini kazanma olasılığı:
          OFI conviction (z/z_thr) + entry_price ile orantılı basit model.
          conviction=1.0 → +%10 premium; conviction=2.0 → +%20 premium.
        """
        premium = min(0.25, (conviction - 1.0) * 0.10 + 0.10)
        est_win_prob = min(0.92, entry_price + premium)
        edge_pct = (est_win_prob - entry_price) / entry_price * 100.0
        return edge_pct

    # ── Pozisyon aç ──────────────────────────────────────────────────────────

    async def _open(
        self, direction: str, entry_price: float, token_id: str, edge_pct: float
    ) -> None:
        state = self._state
        shares = self._stake / entry_price

        pos = Position(
            id=uuid.uuid4().hex[:8],
            side=direction,
            token_id=token_id,
            entry_price=entry_price,
            stake_usd=self._stake,
            shares=shares,
            open_time=time.time(),
            market_slug=state.market.slug,
        )

        state.open_positions.append(pos)
        state.bankroll -= self._stake
        self._last_entry[state.market.slug] = time.time()
        self._bankroll.update_peak()

        msg = (
            f"OPEN {direction} @ {entry_price:.3f} | "
            f"stake=${self._stake:.2f} edge={edge_pct:.1f}% "
            f"shares={shares:.2f}"
        )
        log.info(msg)
        state.log_event(msg)
        self._write_trade(pos, "OPEN", entry_price, 0.0)

    # ── Exit yönetimi ────────────────────────────────────────────────────────

    async def _manage_exits(self) -> None:
        state = self._state
        book = state.book

        to_close = []
        for pos in list(state.open_positions):
            if pos.status != "open":
                continue

            # Güncel fiyat
            if pos.side == "UP":
                cur_bid, cur_ask = book.up_bid, book.up_ask
            else:
                cur_bid, cur_ask = book.down_bid, book.down_ask

            if cur_bid <= 0:
                continue

            cur_mid = (cur_bid + cur_ask) / 2.0 if cur_ask > cur_bid > 0 else cur_bid

            # TP2 (tam çıkış)
            if cur_mid >= pos.entry_price + self._tp2:
                pnl = (cur_bid - pos.entry_price) * pos.shares
                pos.status = "closed_tp2"
                pos.pnl = pnl
                to_close.append((pos, "TP2", cur_bid, pnl))
                continue

            # TP1 (tam çıkış — küçük kasada kısmi yönetimi karmaşıklaştırma)
            if not pos.tp1_hit and cur_mid >= pos.entry_price + self._tp1:
                pos.tp1_hit = True
                pnl = (cur_bid - pos.entry_price) * pos.shares
                pos.status = "closed_tp1"
                pos.pnl = pnl
                to_close.append((pos, "TP1", cur_bid, pnl))
                continue

            # Hard stop
            if cur_mid <= pos.entry_price - self._hstop:
                pnl = (cur_bid - pos.entry_price) * pos.shares
                pos.status = "closed_stop"
                pos.pnl = pnl
                to_close.append((pos, "STOP", cur_bid, pnl))
                continue

            # Signal flip exit
            if (
                self._flip_exit
                and state.signal_valid
                and state.signal_direction
                and state.signal_direction != pos.side
            ):
                pnl = (cur_bid - pos.entry_price) * pos.shares
                pos.status = "closed_flip"
                pos.pnl = pnl
                to_close.append((pos, "FLIP", cur_bid, pnl))
                continue

            # Market sona erdi → resolve at 0.5 (paper unknown)
            if state.seconds_to_market_end() <= 2.0:
                # Paper: sabit 0.5'ten resolve (gerçek resolution yok)
                resolve_price = 0.5
                pnl = (resolve_price - pos.entry_price) * pos.shares
                pos.status = "resolved"
                pos.pnl = pnl
                to_close.append((pos, "EXPIRE", resolve_price, pnl))

        for pos, reason, close_price, pnl in to_close:
            state.open_positions.remove(pos)
            state.closed_positions.append(pos)

            # Bakiyeyi güncelle: stake iade + pnl
            state.bankroll += self._stake + pnl
            state.total_pnl += pnl

            if pnl >= 0:
                state.win_count += 1
            else:
                state.loss_count += 1
                self._bankroll.record_loss()

            self._bankroll.update_peak()

            msg = (
                f"EXIT {pos.side} [{reason}] @ {close_price:.3f} | "
                f"pnl=${pnl:+.2f} | bank=${state.bankroll:.2f}"
            )
            log.info(msg)
            state.log_event(msg)
            self._write_trade(pos, f"CLOSE_{reason}", close_price, pnl)

    # ── Trade log ────────────────────────────────────────────────────────────

    def _write_trade(
        self, pos: Position, action: str, price: float, pnl: float
    ) -> None:
        memory_file = self._cfg.get("memory_file", "trades_v21.jsonl")
        record = {
            "ts":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "id":     pos.id,
            "action": action,
            "side":   pos.side,
            "price":  round(price, 4),
            "stake":  pos.stake_usd,
            "pnl":    round(pnl, 4),
            "slug":   pos.market_slug,
        }
        try:
            with open(memory_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("Trade log yazılamadı: %s", exc)

    # ── Durdur ───────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        self._running = False
        log.info("DecisionEngine durduruldu")
