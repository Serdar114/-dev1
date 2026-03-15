"""
Rich terminal dashboard for polybot_v2 Phase 1.

Design: SNIPER_RED theme — dark background feel, red/yellow accents,
cyan informational, green positive, red negative.

Layout:
  ┌── HEADER BAR ─────────────────────────────────────────────────────┐
  │  POLYBOT V2 PHASE-1  │  BTC  │  bankroll  │  pnl  │  W/L  │ win  │
  ├── ACTIVE WINDOW ──────────┬── STATUS / KASA ─────────────────────┤
  │  market table             │  bankroll / regime / confidence       │
  ├── DECISION ───────────────┤  open trade info                     │
  │  fair / implied / edge    ├── SHADOW PROBE ──────────────────────┤
  │  action / reason          │  pending / filled / expired / adverse │
  ├── LIVE LOG ───────────────┴──────────────────────────────────────┤
  │  last 20 lines colour-coded                                       │
  └───────────────────────────────────────────────────────────────────┘

UI runs in a separate daemon thread.
If it crashes, the main bot is unaffected.
"""

from __future__ import annotations

import datetime
import math
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

try:
    from rich.columns import Columns
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

if TYPE_CHECKING:
    from settings import Settings
    from ui_state import UIState


# ------------------------------------------------------------------ #
# Theme
# ------------------------------------------------------------------ #

_THEME = {
    "header_bg":     "bold white on red",
    "header_title":  "bold bright_white",
    "panel_border":  "red",
    "panel_border2": "yellow",
    "panel_border3": "cyan",
    "label":         "bold cyan",
    "value_pos":     "bold green",
    "value_neg":     "bold red",
    "value_neutral": "bold white",
    "value_info":    "cyan",
    "warn":          "bold yellow",
    "trade_open":    "bold green",
    "trade_resolve": "bold magenta",
    "log_info":      "cyan",
    "log_warn":      "yellow",
    "log_error":     "bold red",
    "log_trade":     "bold green",
    "log_resolve":   "bold magenta",
    "dim":           "dim white",
}


def _ts_fmt(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")


def _sign_color(v: float) -> str:
    if v > 0:
        return _THEME["value_pos"]
    if v < 0:
        return _THEME["value_neg"]
    return _THEME["value_neutral"]


def _pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def _fmt_btc_delta(current: float, window_open: float) -> str:
    if window_open <= 0:
        return "—"
    d = (current - window_open) / window_open
    return f"{d*100:+.3f}%"


# ------------------------------------------------------------------ #
# Panel builders
# ------------------------------------------------------------------ #

def _build_header(snap: dict) -> Panel:
    btc = snap.get("btc_mid", 0.0)
    bankroll = snap.get("bankroll", 0.0)
    pnl = snap.get("paper_pnl", 0.0)
    win = snap.get("win_count", 0)
    loss = snap.get("loss_count", 0)
    mode = snap.get("mode", "paper").upper().replace("_", "+")
    ws = snap.get("window_start", 0.0)
    we = snap.get("window_end", 0.0)
    elapsed = snap.get("elapsed_from_window_start", 0.0)
    remaining = max(0.0, we - time.time())
    cooldown = snap.get("cooldown_remaining", 0)
    consec = snap.get("consecutive_losses", 0)
    binance_age = snap.get("binance_age_ms", 0.0)
    stale = binance_age > 3000

    t = Text()
    t.append("  POLYBOT V2 PHASE-1  ", style="bold bright_white on red")
    t.append("  ")
    t.append(f"MODE: {mode}", style="bold yellow")
    t.append("   BTC: ")
    t.append(f"${btc:,.2f}", style="bold cyan")
    t.append("   BANKROLL: ")
    t.append(f"${bankroll:.2f}", style="bold green" if bankroll >= 30.0 else "bold red")
    t.append("   PnL: ")
    t.append(f"{pnl:+.4f} USDC", style=_sign_color(pnl))
    t.append("   W/L: ")
    t.append(f"{win}/{loss}", style="bold white")
    if win + loss > 0:
        wr = win / (win + loss)
        t.append(f" ({wr*100:.0f}%)", style="bold green" if wr >= 0.5 else "bold red")
    t.append("   WINDOW: ")
    t.append(f"{_ts_fmt(ws)}→{_ts_fmt(we)}", style="bold yellow")
    t.append(f"  T+{elapsed:.0f}s / {remaining:.0f}s left", style="dim white")
    if cooldown > 0:
        t.append(f"   ⚠ COOLDOWN:{cooldown}w", style="bold red")
    if consec > 0:
        t.append(f"  LOSSES:{consec}", style="bold red")
    if stale:
        t.append("  ⚡ STALE", style="bold red on white")
    t.append("  ")

    return Panel(t, style="on grey3", padding=(0, 0), border_style="red")


def _build_market_panel(snap: dict) -> Panel:
    btc = snap.get("btc_mid", 0.0)
    wo = snap.get("window_open_price", 0.0)
    bid_y = snap.get("best_bid_yes", 0.0)
    ask_y = snap.get("best_ask_yes", 0.0)
    bid_n = snap.get("best_bid_no", 0.0)
    ask_n = snap.get("best_ask_no", 0.0)
    implied = snap.get("implied_yes_prob", 0.5)
    remaining = max(0.0, snap.get("window_end", 0.0) - time.time())

    d = snap.get("last_decision")

    tbl = Table(
        box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan",
        expand=True, padding=(0, 1),
    )
    tbl.add_column("FIELD", style="bold cyan", width=18)
    tbl.add_column("VALUE", style="white")

    delta_str = _fmt_btc_delta(btc, wo)
    delta_style = "bold green" if btc > wo else "bold red"

    tbl.add_row("BTC Now", f"[bold white]${btc:,.2f}[/bold white]")
    tbl.add_row("Window Open", f"[dim white]${wo:,.2f}[/dim white]")
    tbl.add_row("Δ BTC", f"[{delta_style}]{delta_str}[/{delta_style}]")
    tbl.add_row("Remaining", f"[yellow]{remaining:.0f}s[/yellow]")
    tbl.add_row("YES Bid/Ask", f"[green]{bid_y:.4f}[/green] / [red]{ask_y:.4f}[/red]")
    tbl.add_row("NO  Bid/Ask", f"[green]{bid_n:.4f}[/green] / [red]{ask_n:.4f}[/red]")
    tbl.add_row("Implied YES", f"[bold white]{implied:.4f}[/bold white]")
    spread = ask_y - bid_y
    spread_style = "bold red" if spread > 0.05 else "white"
    tbl.add_row("Spread YES", f"[{spread_style}]{spread:.4f}[/{spread_style}]")

    if d and hasattr(d, "fair_yes_prob") and d.fair_yes_prob > 0:
        fair = d.fair_yes_prob
        diff = fair - implied
        diff_style = "green" if diff > 0 else "red"
        tbl.add_row("Fair YES", f"[bold white]{fair:.4f}[/bold white]  "
                    f"[{diff_style}]Δ{diff:+.4f}[/{diff_style}]")

    return Panel(tbl, title="[bold red]ACTIVE WINDOW[/bold red]",
                 border_style="red", padding=(0, 1))


def _build_status_panel(snap: dict) -> Panel:
    bankroll = snap.get("bankroll", 0.0)
    peak = snap.get("peak_bankroll", 0.0)
    pnl = snap.get("paper_pnl", 0.0)
    dd = snap.get("drawdown", 0.0)
    d = snap.get("last_decision")
    metrics = snap.get("metrics", {})
    open_trades = snap.get("open_trades", [])

    tbl = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("K", style="bold cyan", width=16)
    tbl.add_column("V", style="white")

    tbl.add_row("Bankroll", f"[bold green]${bankroll:.4f}[/bold green]")
    tbl.add_row("Peak",     f"[dim white]${peak:.4f}[/dim white]")
    pnl_s = "bold green" if pnl >= 0 else "bold red"
    tbl.add_row("Paper PnL", f"[{pnl_s}]{pnl:+.4f} USDC[/{pnl_s}]")
    dd_s = "bold red" if dd > 0.05 else "dim white"
    tbl.add_row("Drawdown",  f"[{dd_s}]{dd*100:.1f}%[/{dd_s}]")
    tbl.add_row("", "")

    if d and hasattr(d, "regime") and d.regime != "UNKNOWN":
        reg = d.regime
        reg_s = {"TRENDING": "bold green", "QUIET": "dim white",
                 "CHOP": "yellow", "EXTREME_ZONE": "bold red"}.get(reg, "white")
        tbl.add_row("Regime",  f"[{reg_s}]{reg}[/{reg_s}]")
        pat = d.pattern
        pat_s = {"SUSTAINED_MOVE": "bold green", "BURST": "cyan",
                 "FADE": "yellow", "NOISE": "dim white"}.get(pat, "white")
        tbl.add_row("Pattern", f"[{pat_s}]{pat}[/{pat_s}]")
        conf = d.confidence_score
        conf_s = "bold green" if conf >= 0.5 else ("yellow" if conf >= 0.2 else "bold red")
        tbl.add_row("Confidence", f"[{conf_s}]{conf:.3f}[/{conf_s}]")
        if d.fee_per_share > 0:
            tbl.add_row("Fee/share", f"[dim white]{d.fee_per_share:.6f}[/dim white]")
        tbl.add_row("", "")

    # Open trades
    if open_trades:
        t = open_trades[0]
        tbl.add_row("OPEN TRADE", "")
        tbl.add_row("  Side",   f"[bold white]{t.side.upper()}[/bold white]")
        tbl.add_row("  Entry",  f"[bold yellow]{t.entry_price:.4f}[/bold yellow]")
        tbl.add_row("  Shares", f"[white]{t.shares:.2f}[/white]")
        tbl.add_row("  Notional", f"[white]{t.notional:.4f} USDC[/white]")
    else:
        tbl.add_row("Open Trade", "[dim white]none[/dim white]")

    tbl.add_row("", "")
    wr = metrics.get("win_rate", 0.0)
    wr_s = "bold green" if wr >= 0.5 else "bold red"
    tbl.add_row("Win Rate", f"[{wr_s}]{wr*100:.0f}%[/{wr_s}] "
                f"({metrics.get('taker_win_count',0)}W/"
                f"{metrics.get('taker_loss_count',0)}L)")
    tbl.add_row("Edge Mean", f"[white]{metrics.get('edge_mean',0.0):.4f}[/white]")
    tbl.add_row("Conf Mean", f"[white]{metrics.get('confidence_mean',0.0):.3f}[/white]")

    return Panel(tbl, title="[bold yellow]STATUS / KASA[/bold yellow]",
                 border_style="yellow", padding=(0, 1))


def _build_decision_panel(snap: dict) -> Panel:
    d = snap.get("last_decision")
    metrics = snap.get("metrics", {})

    tbl = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("K", style="bold cyan", width=20)
    tbl.add_column("V", style="white")

    if d and hasattr(d, "fair_yes_prob"):
        tbl.add_row("Fair YES", f"[bold white]{d.fair_yes_prob:.4f}[/bold white]")
        tbl.add_row("Implied YES", f"[white]{d.implied_yes_prob:.4f}[/white]")
        diff = d.fair_yes_prob - d.implied_yes_prob
        tbl.add_row("Fair-Impl Δ",
                    f"[{'green' if diff>0 else 'red'}]{diff:+.4f}[/]")
        tbl.add_row("Raw Edge YES",
                    f"[{_sign_color(d.raw_edge_yes)}]{d.raw_edge_yes:+.5f}[/]")
        tbl.add_row("Raw Edge NO",
                    f"[{_sign_color(d.raw_edge_no)}]{d.raw_edge_no:+.5f}[/]")
        tbl.add_row("AfterFee YES",
                    f"[{_sign_color(d.after_fee_edge_yes)}]{d.after_fee_edge_yes:+.5f}[/]")
        tbl.add_row("AfterFee NO",
                    f"[{_sign_color(d.after_fee_edge_no)}]{d.after_fee_edge_no:+.5f}[/]")
        tbl.add_row("", "")
        act = d.action
        act_s = "bold green" if act == "PAPER_TRADE" else "bold red"
        tbl.add_row("ACTION", f"[{act_s}]{act}[/{act_s}]")
        if d.chosen_side:
            tbl.add_row("Side", f"[bold white]{d.chosen_side.upper()}[/bold white]")
        reason = d.reason or "—"
        tbl.add_row("Reason", f"[yellow]{reason}[/yellow]")
        elapsed = d.elapsed_from_window_start
        ste = d.seconds_to_expiry
        tbl.add_row("T+ / STE", f"[dim white]{elapsed:.0f}s[/dim white] / "
                    f"[dim white]{ste:.0f}s[/dim white]")
    else:
        tbl.add_row("Status", "[dim white]waiting…[/dim white]")

    tbl.add_row("", "")
    # Top 3 no-trade reasons
    reasons = metrics.get("no_trade_reasons_top5", {})
    if reasons:
        tbl.add_row("[bold cyan]NO-TRADE REASONS[/bold cyan]", "")
        for r, cnt in list(reasons.items())[:3]:
            short = r[:28]
            tbl.add_row(f"  {short}", f"[yellow]{cnt}[/yellow]")

    stale = metrics.get("stale_reject_count", 0)
    spread = metrics.get("spread_reject_count", 0)
    skew = metrics.get("skew_reject_count", 0)
    if stale or spread or skew:
        tbl.add_row("", "")
        tbl.add_row("Stale Rejects",  f"[bold red]{stale}[/bold red]")
        tbl.add_row("Spread Rejects", f"[bold red]{spread}[/bold red]")
        tbl.add_row("Skew Rejects",   f"[bold red]{skew}[/bold red]")

    return Panel(tbl, title="[bold red]DECISION[/bold red]",
                 border_style="red", padding=(0, 1))


def _build_shadow_panel(metrics: dict) -> Panel:
    tbl = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("K", style="bold cyan", width=20)
    tbl.add_column("V", style="white")

    tbl.add_row("Quotes Sent",    f"[white]{metrics.get('shadow_quote_count',0)}[/white]")
    tbl.add_row("Pending",        f"[yellow]{metrics.get('shadow_pending_count',0)}[/yellow]")
    tbl.add_row("Filled",         f"[bold green]{metrics.get('shadow_filled_count',0)}[/bold green]")
    tbl.add_row("Expired",        f"[dim white]{metrics.get('shadow_expired_count',0)}[/dim white]")
    tbl.add_row("Crossed",        f"[red]{metrics.get('shadow_crossed_count',0)}[/red]")
    tbl.add_row("Adverse Fill",   f"[bold red]{metrics.get('shadow_adverse_fill_count',0)}[/bold red]")

    return Panel(tbl, title="[bold cyan]SHADOW PROBE[/bold cyan]",
                 border_style="cyan", padding=(0, 1))


def _build_log_panel(lines: list[tuple[str, str, str]]) -> Panel:
    text = Text()
    for ts_s, level, msg in lines:
        level_u = level.upper()
        if "TRADE" in level_u and "OPEN" in msg.upper():
            style = _THEME["log_trade"]
        elif "RESOLVE" in msg.upper() or "RESOLVE" in level_u:
            style = _THEME["log_resolve"]
        elif level_u in ("WARNING", "WARN"):
            style = _THEME["log_warn"]
        elif level_u == "ERROR":
            style = _THEME["log_error"]
        else:
            style = _THEME["log_info"]
        text.append(f"[{ts_s}] ", style="dim white")
        text.append(f"{level_u:<8}", style=style)
        text.append(f"{msg}\n", style="white")

    return Panel(text, title="[bold cyan]LIVE LOG[/bold cyan]",
                 border_style="cyan", padding=(0, 1))


# ------------------------------------------------------------------ #
# Dashboard main class
# ------------------------------------------------------------------ #

class UIDashboard:
    """
    Rich Live dashboard.

    Runs in a daemon thread. The main loop calls nothing on it after start().
    It reads from UIState.snapshot() on its own refresh cadence.
    """

    def __init__(self, settings: "Settings", ui_state: "UIState") -> None:
        if not _RICH_AVAILABLE:
            raise ImportError("rich is not installed — install with: pip install rich")
        self._cfg = settings
        self._state = ui_state
        self._console = Console()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="UIDashboard"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        refresh = self._cfg.ui_refresh_sec
        log_lines = self._cfg.ui_show_log_lines

        try:
            with Live(
                self._render(self._state.snapshot(), self._state.get_log_lines(log_lines)),
                console=self._console,
                refresh_per_second=max(1, int(1.0 / refresh)),
                screen=True,
            ) as live:
                while self._running:
                    try:
                        snap = self._state.snapshot()
                        lines = self._state.get_log_lines(log_lines)
                        live.update(self._render(snap, lines))
                    except Exception:
                        pass
                    time.sleep(refresh)
        except Exception:
            pass  # UI died — bot continues unaffected

    def _render(self, snap: dict, log_lines: list) -> Layout:
        layout = Layout()

        header = _build_header(snap)
        market = _build_market_panel(snap)
        status = _build_status_panel(snap)
        decision = _build_decision_panel(snap)
        shadow = _build_shadow_panel(snap.get("metrics", {}))
        log_panel = _build_log_panel(log_lines)

        # Top row: full-width header
        layout.split_column(
            Layout(header, name="header", size=3),
            Layout(name="mid"),
            Layout(log_panel, name="log", size=12),
        )

        # Mid row: left (market + decision) and right (status + shadow)
        layout["mid"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=2),
        )
        layout["left"].split_column(
            Layout(market, name="market"),
            Layout(decision, name="decision"),
        )
        layout["right"].split_column(
            Layout(status, name="status"),
            Layout(shadow, name="shadow"),
        )

        return layout
