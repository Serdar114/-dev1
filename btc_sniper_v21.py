#!/usr/bin/env python3
"""
btc_sniper_v21.py — Polymarket BTC 5m Paper Sniper · V21

Mimari:
  BinanceStream ─┐
                 ├─→ OFIEngine → SharedState ←┐
  PolyPoller ────┘                             │
  MarketDiscovery ─────────────────────────────┤
  DecisionEngine (+ Bankroll) ─────────────────┘
  Dashboard (Rich Live)

Başlatma:
  python btc_sniper_v21.py [config_v21.json]

Mod: PAPER ONLY · maker-only · $5 sabit stake · 5m piyasalar
"""

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime

# ── Rich dashboard ──────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
    RICH_OK = True
except ImportError:
    RICH_OK = False

# ── Kendi modüllerimiz ──────────────────────────────────────────────────────
import utils.logger as logger_mod
from core.state import SharedState
from core.ofi_engine import OFIEngine
from core.binance_ws import BinanceStream
from core.market_discovery import MarketDiscovery
from core.polymarket_ws import PolymarketPoller
from core.bankroll import Bankroll
from core.decision_engine import DecisionEngine

log = logger_mod.get("main")

# ═══════════════════════════════════════════════════════════════════════════
#  Config yükleme
# ═══════════════════════════════════════════════════════════════════════════

def load_config(path: str = "config_v21.json") -> dict:
    if not os.path.exists(path):
        sys.exit(f"[HATA] Config bulunamadı: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════
#  Rich Dashboard
# ═══════════════════════════════════════════════════════════════════════════

def _color_z(z: float) -> str:
    if z > 1.5:   return "bold green"
    if z > 0.5:   return "green"
    if z < -1.5:  return "bold red"
    if z < -0.5:  return "red"
    return "yellow"

def _color_pnl(pnl: float) -> str:
    if pnl > 0:  return "bold green"
    if pnl < 0:  return "bold red"
    return "white"

def _color_signal(valid: bool, direction: str) -> tuple:
    if not valid:     return "grey50", "─"
    if direction == "UP":   return "bold green",  "▲ UP"
    if direction == "DOWN": return "bold red",    "▼ DOWN"
    return "white", direction


def build_dashboard(state: SharedState) -> Layout:
    """Her refresh döngüsünde yeni layout üretir."""

    now_str = datetime.now().strftime("%H:%M:%S")
    market = state.market
    book   = state.book
    secs   = state.seconds_to_market_end()

    sig_color, sig_text = _color_signal(state.signal_valid, state.signal_direction)
    z_color = _color_z(state.ofi_z)

    # ── Panel 1: Sistem Durumu ──────────────────────────────────────────────
    status_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    status_tbl.add_column("k", style="bold cyan", width=18)
    status_tbl.add_column("v", width=30)

    status_tbl.add_row("Saat",       now_str)
    status_tbl.add_row("Çalışma",    state.uptime_str())
    status_tbl.add_row("Durum",      Text(state.bot_status, style="bold yellow"))
    status_tbl.add_row("BTC Fiyatı",
        Text(f"${state.btc_price:,.2f}", style="bold white")
        if state.is_btc_fresh() else Text("─ (stale)", style="dim red")
    )

    status_panel = Panel(
        status_tbl,
        title="[bold blue]■ SİSTEM[/bold blue]",
        border_style="blue",
    )

    # ── Panel 2: Aktif Market ───────────────────────────────────────────────
    mkt_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    mkt_tbl.add_column("k", style="bold cyan", width=18)
    mkt_tbl.add_column("v", width=30)

    slug_disp = (market.slug[:35] + "…") if len(market.slug) > 36 else (market.slug or "─")
    mkt_tbl.add_row("Market",    slug_disp)
    mkt_tbl.add_row("Kalan Süre",
        Text(f"{secs:.0f}s", style="bold green" if secs > 30 else "bold red")
    )
    mkt_tbl.add_row("UP  bid/ask",
        f"{book.up_bid:.3f} / {book.up_ask:.3f}"
        if book.up_bid > 0 else "─"
    )
    mkt_tbl.add_row("DOWN bid/ask",
        f"{book.down_bid:.3f} / {book.down_ask:.3f}"
        if book.down_bid > 0 else "─"
    )
    mkt_tbl.add_row("Book tazeli",
        Text("TAZE", style="green") if state.is_book_fresh()
        else Text("ESKİ", style="dim red")
    )

    market_panel = Panel(
        mkt_tbl,
        title="[bold magenta]■ PİYASA[/bold magenta]",
        border_style="magenta",
    )

    # ── Panel 3: OFI / Sinyal ──────────────────────────────────────────────
    ofi_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    ofi_tbl.add_column("k", style="bold cyan", width=18)
    ofi_tbl.add_column("v", width=30)

    ofi_tbl.add_row("OFI_N / TFI_N", f"{state.ofi_n} / {state.tfi_n}")
    ofi_tbl.add_row("OFI z-skor",    Text(f"{state.ofi_z:+.3f}", style=z_color))
    ofi_tbl.add_row("TFI",           f"{state.tfi_value:+.2f}")
    ofi_tbl.add_row("OFI Ratio",     f"{state.ofi_ratio:.2f}x")
    ofi_tbl.add_row("Conviction",    f"{state.signal_conviction:.2f}x")
    ofi_tbl.add_row("SİNYAL",        Text(sig_text, style=sig_color))

    ofi_panel = Panel(
        ofi_tbl,
        title="[bold green]■ OFI / SİNYAL[/bold green]",
        border_style="green",
    )

    # ── Panel 4: Bankroll ──────────────────────────────────────────────────
    bank_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    bank_tbl.add_column("k", style="bold cyan", width=18)
    bank_tbl.add_column("v", width=30)

    dd = state.drawdown_pct()
    bank_tbl.add_row("Bankroll",    Text(f"${state.bankroll:.2f}", style="bold white"))
    bank_tbl.add_row("Başlangıç",   f"${state.initial_bankroll:.2f}")
    bank_tbl.add_row("Toplam PnL",  Text(f"${state.total_pnl:+.2f}", style=_color_pnl(state.total_pnl)))
    bank_tbl.add_row("Drawdown",    Text(f"{dd:.1f}%", style="bold red" if dd > 20 else "yellow"))
    bank_tbl.add_row("W / L",       f"{state.win_count} / {state.loss_count}")
    bank_tbl.add_row("Win Rate",    f"{state.win_rate()*100:.0f}%")
    bank_tbl.add_row("Açık Poz.",   str(len(state.open_positions)))

    bank_panel = Panel(
        bank_tbl,
        title="[bold yellow]■ BANKROLL[/bold yellow]",
        border_style="yellow",
    )

    # ── Panel 5: Açık Pozisyonlar ─────────────────────────────────────────
    pos_tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    pos_tbl.add_column("ID",    style="dim", width=9)
    pos_tbl.add_column("Yön",   width=6)
    pos_tbl.add_column("Giriş", width=7)
    pos_tbl.add_column("Şimdi", width=7)
    pos_tbl.add_column("PnL$",  width=8)
    pos_tbl.add_column("Durum", width=8)

    for pos in state.open_positions[:4]:
        if pos.side == "UP":
            cur = book.up_bid
        else:
            cur = book.down_bid
        unreal_pnl = (cur - pos.entry_price) * pos.shares if cur > 0 else 0.0
        pnl_style = "green" if unreal_pnl >= 0 else "red"
        pos_tbl.add_row(
            pos.id,
            Text(pos.side, style="green" if pos.side == "UP" else "red"),
            f"{pos.entry_price:.3f}",
            f"{cur:.3f}" if cur > 0 else "─",
            Text(f"{unreal_pnl:+.2f}", style=pnl_style),
            pos.status,
        )

    if not state.open_positions:
        pos_tbl.add_row("─", "─", "─", "─", "─", "─")

    pos_panel = Panel(
        pos_tbl,
        title="[bold cyan]■ AÇIK POZİSYONLAR[/bold cyan]",
        border_style="cyan",
    )

    # ── Panel 6: Event Log ────────────────────────────────────────────────
    log_lines = list(state.events)[:12]
    log_text = "\n".join(log_lines) if log_lines else "─"
    log_panel = Panel(
        log_text,
        title="[bold white]■ EVENT LOG[/bold white]",
        border_style="white",
    )

    # ── Layout birleştir ─────────────────────────────────────────────────
    layout = Layout()
    layout.split_column(
        Layout(name="top",    ratio=3),
        Layout(name="mid",    ratio=3),
        Layout(name="bottom", ratio=4),
    )
    layout["top"].split_row(
        Layout(status_panel,  name="status"),
        Layout(market_panel,  name="market"),
        Layout(ofi_panel,     name="ofi"),
    )
    layout["mid"].split_row(
        Layout(bank_panel,    name="bank"),
        Layout(pos_panel,     name="positions"),
    )
    layout["bottom"].update(log_panel)

    return layout


# ═══════════════════════════════════════════════════════════════════════════
#  Market Discovery döngüsü
# ═══════════════════════════════════════════════════════════════════════════

async def market_loop(discovery: MarketDiscovery, state: SharedState, cfg: dict) -> None:
    """
    Aktif market yok veya market süresi dolduysa yeni market arar.
    Her 10s kontrol eder.
    """
    scan_s = cfg.get("entry", {}).get("scan_interval_s", 10)
    while True:
        try:
            market = state.market
            needs_discovery = (
                not market.slug
                or state.seconds_to_market_end() < 5
            )
            if needs_discovery:
                state.bot_status = "MARKET ARIYOR"
                found = await discovery.find_active_market()
                if found:
                    state.bot_status = "ÇALIŞIYOR"
                else:
                    state.bot_status = "MARKET YOK"
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("Market discovery döngü hatası: %s", exc)

        await asyncio.sleep(scan_s)


# ═══════════════════════════════════════════════════════════════════════════
#  Ana giriş noktası
# ═══════════════════════════════════════════════════════════════════════════

async def main(cfg_path: str) -> None:
    cfg = load_config(cfg_path)
    logger_mod.setup(cfg)
    log.info("=== BTC Sniper V21 başlatılıyor ===")

    # Paper mode zorunlu kontrolü
    if not cfg.get("mode", {}).get("paper_only", True):
        sys.exit("[HATA] paper_only=false! Live mod bu sürümde kapalı.")
    if cfg.get("mode", {}).get("live_enabled", False):
        sys.exit("[HATA] live_enabled=true! Bu sürümde live kapalı.")

    # ── Bileşenleri başlat ─────────────────────────────────────────────────
    bankroll_start = cfg.get("risk", {}).get("bankroll_usd", 30.0)
    state     = SharedState(bankroll_start)
    ofi       = OFIEngine(cfg, state)
    binance   = BinanceStream(state, ofi)
    discovery = MarketDiscovery(cfg, state)
    poller    = PolymarketPoller(cfg, state)
    bankroll  = Bankroll(cfg, state)
    engine    = DecisionEngine(cfg, state, bankroll)

    state.bot_status = "BAŞLANGIC"
    state.log_event("V21 Paper Bot başlatıldı")
    log.info("Başlangıç bankroll: $%.2f", bankroll_start)

    # ── Graceful shutdown ─────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(*_):
        log.info("Kapatma sinyali alındı")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            pass  # Windows

    # ── Dashboard ve görevler ──────────────────────────────────────────────
    tasks = [
        asyncio.create_task(binance.start(),             name="binance"),
        asyncio.create_task(poller.start(),              name="poller"),
        asyncio.create_task(market_loop(discovery, state, cfg), name="market_loop"),
        asyncio.create_task(engine.run(),                name="engine"),
        asyncio.create_task(stop_event.wait(),           name="stop_waiter"),
    ]

    if RICH_OK:
        console = Console()
        with Live(
            build_dashboard(state),
            console=console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            while not stop_event.is_set():
                live.update(build_dashboard(state))
                await asyncio.sleep(0.5)
    else:
        # Fallback: terminal output yok (loglar dosyaya gidiyor)
        log.warning("Rich kurulu değil — dashboard devre dışı")
        await stop_event.wait()

    # ── Temiz kapanış ─────────────────────────────────────────────────────
    log.info("Kapatılıyor…")
    await binance.stop()
    await poller.stop()
    await engine.stop()

    for task in tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ── Son özet ─────────────────────────────────────────────────────────
    print("\n" + "─" * 55)
    print(f"  BTC Sniper V21 — Son Özet")
    print(f"  Çalışma süresi : {state.uptime_str()}")
    print(f"  Bankroll       : ${state.bankroll:.2f}  (başlangıç: ${state.initial_bankroll:.2f})")
    print(f"  Toplam PnL     : ${state.total_pnl:+.2f}")
    print(f"  W / L          : {state.win_count} / {state.loss_count}")
    print(f"  Drawdown       : {state.drawdown_pct():.1f}%")
    print("─" * 55 + "\n")
    log.info(
        "Bot kapandı | bank=$%.2f pnl=$%+.2f W=%d L=%d",
        state.bankroll, state.total_pnl, state.win_count, state.loss_count,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config_v21.json"
    try:
        asyncio.run(main(cfg_path))
    except KeyboardInterrupt:
        print("\n[Ctrl+C] Bot durduruldu.")
