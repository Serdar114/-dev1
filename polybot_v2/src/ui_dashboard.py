"""
Rich terminal dashboard for polybot_v2 Phase 1.

Design: SNIPER_RED theme — dark background, red/yellow accents,
cyan informational, green positive, red negative.

Layout:
  ┌── HEADER BAR ─────────────────────────────────────────────────────────┐
  │  POLYBOT V2 PHASE-1  │  BTC  │  bankroll  │  pnl  │  W/L  │ window  │
  ├── ACTIVE WINDOW / MICROSTRUCTURE ─┬── STATUS / KASA ─────────────────┤
  │  BTC, delta, vol, book, spread,   │  bankroll / peak / DD / trades   │
  │  skew, sanity status              │  stake tier / cooldown            │
  ├── LAST DECISION ──────────────────┼── SHADOW PROBE ──────────────────┤
  │  fair / implied / edge / fee      │  pending / filled / expired       │
  │  action / reason / confidence     │  fill rate / adverse ratio        │
  ├── LIVE REJECT / DECISION TAPE ────┴──────────────────────────────────┤
  │  last 20 lines colour-coded by type                                   │
  └───────────────────────────────────────────────────────────────────────┘

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
# Theme: sniper_red
# ------------------------------------------------------------------ #

_T = {
    "header_brand":  "bold bright_white on red",
    "header_key":    "bold yellow",
    "header_val":    "bold cyan",
    "border_red":    "red",
    "border_yellow": "yellow",
    "border_cyan":   "cyan",
    "label":         "bold cyan",
    "val_pos":       "bold green",
    "val_neg":       "bold red",
    "val_neutral":   "bold white",
    "val_info":      "cyan",
    "val_dim":       "dim white",
    "val_warn":      "bold yellow",
    "regime_trending": "bold green",
    "regime_quiet":    "dim white",
    "regime_chop":     "yellow",
    "regime_extreme":  "bold red",
    "pat_sustained":   "bold green",
    "pat_burst":       "cyan",
    "pat_fade":        "yellow",
    "pat_noise":       "dim white",
    # Log tape
    "log_reject":    "bold red",
    "log_trade":     "bold green",
    "log_resolve":   "bold magenta",
    "log_shadow":    "bold cyan",
    "log_warn":      "bold yellow",
    "log_error":     "bold red on white",
    "log_info":      "white",
    "log_ts":        "dim white",
}


# ------------------------------------------------------------------ #
# Formatting helpers
# ------------------------------------------------------------------ #

def _ts(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")


def _sign(v: float) -> str:
    return _T["val_pos"] if v > 0 else (_T["val_neg"] if v < 0 else _T["val_neutral"])


def _pct(v: float, decimals: int = 2) -> str:
    fmt = f"{{:+.{decimals}f}}%"
    return fmt.format(v * 100)


def _na(v: float, decimals: int = 4, prefix: str = "") -> str:
    if v == 0.0:
        return "—"
    return f"{prefix}{v:.{decimals}f}"


def _delta_display(raw: float, pct_display: float) -> str:
    """Format delta showing both raw fraction and pct for disambiguation."""
    if raw == 0.0:
        return "—"
    s = "+" if raw > 0 else ""
    return f"{s}{pct_display:.4f}%  ({s}{raw:.6f})"


def _status_msg(snap: dict) -> str:
    """Return a human-readable system status string for placeholder display."""
    if snap.get("btc_mid", 0.0) == 0.0:
        return "WAITING FOR BINANCE FEED…"
    if not snap.get("market_slug"):
        return "WAITING FOR MARKET DISCOVERY…"
    if snap.get("best_bid_yes", 0.0) == 0.0:
        return "WAITING FOR ORDER BOOK…"
    if snap.get("sanity_reject"):
        return f"SANITY REJECT: {snap['sanity_reject']}"
    elapsed = snap.get("elapsed_from_window_start", 0.0)
    e_start = snap.get("entry_start_sec", 30)
    e_end = snap.get("entry_end_sec", 240)
    if elapsed < e_start:
        return f"WINDOW OPENING — {e_start - elapsed:.0f}s UNTIL ENTRY"
    if elapsed > e_end:
        return f"WINDOW CLOSING — {max(0, snap.get('window_end', 0) - time.time()):.0f}s LEFT"
    return "ENTRY WINDOW ACTIVE"


# ------------------------------------------------------------------ #
# Panel 1: Header bar
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
    stale_feed = binance_age > 3000
    slug = snap.get("market_slug", "") or "—"
    # short slug: last 12 chars
    slug_short = slug[-16:] if len(slug) > 16 else slug

    t = Text()
    t.append("  POLYBOT V2 PHASE-1  ", style=_T["header_brand"])
    t.append("  ")
    t.append(f"MODE:{mode}", style=_T["header_key"])
    t.append("  BTC:", style=_T["val_dim"])
    btc_s = "bold green" if btc > 0 else "bold red"
    t.append(f"${btc:,.2f}", style=btc_s)
    t.append("  BANKROLL:", style=_T["val_dim"])
    t.append(f"${bankroll:.2f}", style="bold green" if bankroll >= 30.0 else "bold red")
    t.append("  PnL:", style=_T["val_dim"])
    t.append(f"{pnl:+.4f}U", style=_sign(pnl))
    t.append("  W/L:", style=_T["val_dim"])
    t.append(f"{win}/{loss}", style="bold white")
    if win + loss > 0:
        wr = win / (win + loss)
        t.append(f"({wr*100:.0f}%)", style="bold green" if wr >= 0.5 else "bold red")
    t.append("  MARKET:", style=_T["val_dim"])
    t.append(slug_short, style="bold yellow")
    if ws > 0:
        t.append(f"  {_ts(ws)}→{_ts(we)}", style="bold yellow")
    t.append(f"  T+{elapsed:.0f}s/{remaining:.0f}s", style="dim white")
    if cooldown > 0:
        t.append(f"  ⚠COOLDOWN:{cooldown}w", style="bold red")
    if consec > 0:
        t.append(f"  LOSSES:{consec}", style="bold red")
    if stale_feed:
        t.append(f"  ⚡STALE-FEED({binance_age:.0f}ms)", style="bold red on white")
    t.append("  ")

    return Panel(t, style="on grey3", padding=(0, 0), border_style="red")


# ------------------------------------------------------------------ #
# Panel 2: Active Window / Market Microstructure
# ------------------------------------------------------------------ #

def _build_market_panel(snap: dict) -> Panel:
    btc = snap.get("btc_mid", 0.0)
    wo = snap.get("window_open_price", 0.0)
    bid_y = snap.get("best_bid_yes", 0.0)
    ask_y = snap.get("best_ask_yes", 0.0)
    bid_n = snap.get("best_bid_no", 0.0)
    ask_n = snap.get("best_ask_no", 0.0)
    yes_mid = snap.get("yes_mid", snap.get("implied_yes_prob", 0.5))
    no_mid = snap.get("no_mid", 0.5)
    implied = snap.get("implied_yes_prob", 0.5)
    spread_y = snap.get("spread_yes", ask_y - bid_y)
    spread_n = snap.get("spread_no", ask_n - bid_n)
    skew = snap.get("complement_skew", 0.0)
    mid_sum = snap.get("midpoint_sum", yes_mid + no_mid)
    sanity = snap.get("sanity_reject")
    we = snap.get("window_end", 0.0)
    elapsed = snap.get("elapsed_from_window_start", 0.0)
    remaining = max(0.0, we - time.time())
    binance_age = snap.get("binance_age_ms", 0.0)
    delta_raw = snap.get("delta_raw_fraction", 0.0)
    delta_pct = snap.get("delta_pct_display", 0.0)
    realized_vol = snap.get("realized_vol_60s", 0.0)
    e_start = snap.get("entry_start_sec", 30)
    e_end = snap.get("entry_end_sec", 240)

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan",
                expand=True, padding=(0, 1))
    tbl.add_column("FIELD", style="bold cyan", min_width=18)
    tbl.add_column("VALUE", style="white")

    # Status / placeholder
    if btc == 0.0:
        tbl.add_row("[bold red]STATUS[/bold red]", "[bold yellow]WAITING FOR BINANCE FEED[/bold yellow]")
        return Panel(tbl, title="[bold red]MARKET MICROSTRUCTURE[/bold red]",
                     border_style="red", padding=(0, 1))
    if bid_y == 0.0:
        tbl.add_row("[bold red]STATUS[/bold red]", "[bold yellow]WAITING FOR ORDER BOOK[/bold yellow]")
        tbl.add_row("BTC Now", f"[bold white]${btc:,.2f}[/bold white]")
        return Panel(tbl, title="[bold red]MARKET MICROSTRUCTURE[/bold red]",
                     border_style="red", padding=(0, 1))

    # BTC
    btc_up = btc > wo
    btc_s = "bold green" if btc_up else "bold red"
    tbl.add_row("BTC Now", f"[{btc_s}]${btc:,.2f}[/{btc_s}]")
    tbl.add_row("Window Open", f"[dim white]${wo:,.2f}[/dim white]")

    # Delta — explicit dual representation
    if delta_raw != 0.0:
        d_s = "bold green" if delta_raw > 0 else "bold red"
        d_sign = "+" if delta_raw > 0 else ""
        tbl.add_row(
            "Δ BTC (raw|%)",
            f"[{d_s}]{d_sign}{delta_raw:.6f}  |  {d_sign}{delta_pct:.4f}%[/{d_s}]",
        )
    else:
        tbl.add_row("Δ BTC", "[dim white]0.000000  |  0.0000%[/dim white]")

    tbl.add_row("Realized Vol", f"[dim white]{realized_vol:.6f}[/dim white]")
    tbl.add_row("T+ / Left", f"[yellow]{elapsed:.0f}s[/yellow] / [yellow]{remaining:.0f}s[/yellow]")

    # Entry window indicator
    in_entry = e_start <= elapsed <= e_end
    ew_s = "bold green" if in_entry else "dim white"
    tbl.add_row("Entry Window", f"[{ew_s}]{e_start}–{e_end}s  {'✔ ACTIVE' if in_entry else '○ inactive'}[/{ew_s}]")

    tbl.add_row("", "")
    tbl.add_row("[bold cyan]─── ORDER BOOK ───[/bold cyan]", "")
    tbl.add_row("YES Bid / Ask",
                f"[green]{bid_y:.4f}[/green] / [red]{ask_y:.4f}[/red]"
                f"  mid=[bold white]{yes_mid:.4f}[/bold white]")
    tbl.add_row("NO  Bid / Ask",
                f"[green]{bid_n:.4f}[/green] / [red]{ask_n:.4f}[/red]"
                f"  mid=[bold white]{no_mid:.4f}[/bold white]")
    tbl.add_row("Implied YES", f"[bold white]{implied:.4f}[/bold white]")

    spread_thr = snap.get("sanity_reject", "")
    sy_s = "bold red" if spread_y > 0.05 else "white"
    sn_s = "bold red" if spread_n > 0.05 else "white"
    tbl.add_row("Spread YES",
                f"[{sy_s}]{spread_y:.4f}[/{sy_s}]  "
                f"[dim white]thr={snap.get('spread_threshold', 0.05):.3f}[/dim white]")
    tbl.add_row("Spread NO",  f"[{sn_s}]{spread_n:.4f}[/{sn_s}]")
    tbl.add_row("Midpoint Sum", f"[bold white]{mid_sum:.4f}[/bold white]  "
                f"[dim white](ideal=1.0000)[/dim white]")

    skew_s = "bold red" if skew > snap.get("skew_threshold", 0.05) else "white"
    tbl.add_row("Complement Skew",
                f"[{skew_s}]{skew:.4f}[/{skew_s}]  "
                f"[dim white]thr={snap.get('skew_threshold', 0.05):.3f}[/dim white]")

    # Sanity status
    tbl.add_row("", "")
    if sanity:
        tbl.add_row("[bold red]SANITY[/bold red]", f"[bold red on white]REJECT: {sanity[:40]}[/bold red on white]")
    else:
        tbl.add_row("Sanity",  "[bold green]PASS[/bold green]")

    # Data age
    age_s = "bold red" if binance_age > 3000 else ("yellow" if binance_age > 1500 else "dim white")
    tbl.add_row("Data Age", f"[{age_s}]{binance_age:.0f}ms[/{age_s}]")

    return Panel(tbl, title="[bold red]MARKET MICROSTRUCTURE[/bold red]",
                 border_style="red", padding=(0, 1))


# ------------------------------------------------------------------ #
# Panel 3: Last Decision
# ------------------------------------------------------------------ #

def _build_decision_panel(snap: dict) -> Panel:
    d = snap.get("last_decision")
    metrics = snap.get("metrics") or {}

    tbl = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("K", style="bold cyan", min_width=20)
    tbl.add_column("V", style="white")

    if d is None or not hasattr(d, "action"):
        tbl.add_row("Status", "[dim white]no decision yet…[/dim white]")
    else:
        # Action + side
        act = d.action
        act_s = "bold green" if act == "PAPER_TRADE" else "bold red"
        tbl.add_row("ACTION", f"[{act_s}]{act}[/{act_s}]")
        if d.chosen_side:
            tbl.add_row("Side", f"[bold white]{d.chosen_side.upper()}[/bold white]")
        reason = (d.reason or "—")[:48]
        # colour reason by type
        if any(k in reason for k in ("reject", "too_small", "band", "noise", "cooldown", "stale")):
            r_s = "bold red"
        elif "edge_and_risk_ok" in reason:
            r_s = "bold green"
        else:
            r_s = "yellow"
        tbl.add_row("Reason", f"[{r_s}]{reason}[/{r_s}]")
        tbl.add_row("T+/STE",
                    f"[dim white]{d.elapsed_from_window_start:.0f}s[/dim white] / "
                    f"[dim white]{d.seconds_to_expiry:.0f}s[/dim white]")
        tbl.add_row("", "")

        # Fair vs implied
        fair = d.fair_yes_prob
        impl = d.implied_yes_prob
        diff = fair - impl
        if fair > 0:
            tbl.add_row("Fair YES",    f"[bold white]{fair:.4f}[/bold white]")
            tbl.add_row("Implied YES", f"[white]{impl:.4f}[/white]")
            tbl.add_row("Fair−Impl",   f"[{_sign(diff)}]{diff:+.4f}[/]")
            tbl.add_row("", "")

        # Edge
        raw_y = d.raw_edge_yes
        raw_n = d.raw_edge_no
        fee_y = d.after_fee_edge_yes
        fee_n = d.after_fee_edge_no
        if raw_y != 0 or raw_n != 0:
            tbl.add_row("Raw Edge YES/NO",
                        f"[{_sign(raw_y)}]{raw_y:+.5f}[/] / [{_sign(raw_n)}]{raw_n:+.5f}[/]")
            tbl.add_row("AfterFee YES/NO",
                        f"[{_sign(fee_y)}]{fee_y:+.5f}[/] / [{_sign(fee_n)}]{fee_n:+.5f}[/]")
            if d.fee_per_share > 0:
                tbl.add_row("Fee/share",
                            f"[dim white]{d.fee_per_share:.7f}[/dim white]"
                            f"  [dim white]({d.effective_rate*100:.3f}%)[/dim white]")
            tbl.add_row("", "")

        # Confidence / regime / pattern
        conf = d.confidence_score
        conf_s = "bold green" if conf >= 0.5 else ("yellow" if conf >= 0.2 else "bold red")
        tbl.add_row("Confidence", f"[{conf_s}]{conf:.3f}[/{conf_s}]")

        reg = getattr(d, "regime", "UNKNOWN")
        pat = getattr(d, "pattern", "UNKNOWN")
        reg_styles = {
            "TRENDING": _T["regime_trending"], "QUIET": _T["regime_quiet"],
            "CHOP": _T["regime_chop"], "EXTREME_ZONE": _T["regime_extreme"],
        }
        pat_styles = {
            "SUSTAINED_MOVE": _T["pat_sustained"], "BURST": _T["pat_burst"],
            "FADE": _T["pat_fade"], "NOISE": _T["pat_noise"],
        }
        tbl.add_row("Regime",  f"[{reg_styles.get(reg,'white')}]{reg}[/]")
        tbl.add_row("Pattern", f"[{pat_styles.get(pat,'white')}]{pat}[/]")

    # No-trade reason summary
    tbl.add_row("", "")
    tbl.add_row("[bold cyan]NO-TRADE REASONS[/bold cyan]", "[dim white](session)[/dim white]")
    reasons = metrics.get("no_trade_reasons_top5") or {}
    if reasons:
        for r, cnt in list(reasons.items())[:5]:
            short = r[:32]
            r_s = "bold red" if any(k in r for k in ("reject", "stale", "spread", "skew")) else "yellow"
            tbl.add_row(f"  {short}", f"[{r_s}]{cnt}[/{r_s}]")
    else:
        tbl.add_row("  —", "[dim white]none yet[/dim white]")

    return Panel(tbl, title="[bold red]DECISION[/bold red]",
                 border_style="red", padding=(0, 1))


# ------------------------------------------------------------------ #
# Panel 4: Status / Kasa
# ------------------------------------------------------------------ #

def _build_status_panel(snap: dict) -> Panel:
    bankroll = snap.get("bankroll", 0.0)
    peak = snap.get("peak_bankroll", 0.0)
    pnl = snap.get("paper_pnl", 0.0)
    dd = snap.get("drawdown", 0.0)
    open_trades = snap.get("open_trades") or []
    metrics = snap.get("metrics") or {}
    cooldown = snap.get("cooldown_remaining", 0)
    consec = snap.get("consecutive_losses", 0)
    win = snap.get("win_count", 0)
    loss = snap.get("loss_count", 0)

    tbl = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("K", style="bold cyan", min_width=16)
    tbl.add_column("V", style="white")

    # Bankroll block
    tbl.add_row("[bold cyan]─── KASA ───[/bold cyan]", "")
    tbl.add_row("Bankroll",  f"[bold green]${bankroll:.4f}[/bold green]")
    tbl.add_row("Peak",      f"[dim white]${peak:.4f}[/dim white]")
    pnl_s = "bold green" if pnl >= 0 else "bold red"
    tbl.add_row("Paper PnL", f"[{pnl_s}]{pnl:+.4f} USDC[/{pnl_s}]")
    dd_s = "bold red" if dd > 0.05 else ("yellow" if dd > 0.02 else "dim white")
    tbl.add_row("Drawdown",  f"[{dd_s}]{dd*100:.2f}%[/{dd_s}]")

    # Win/loss
    total_trades = win + loss
    if total_trades > 0:
        wr = win / total_trades
        wr_s = "bold green" if wr >= 0.5 else "bold red"
        tbl.add_row("W/L / Rate",
                    f"[bold white]{win}W/{loss}L[/bold white]  "
                    f"[{wr_s}]{wr*100:.0f}%[/{wr_s}]")
    else:
        tbl.add_row("W/L / Rate", "[dim white]—[/dim white]")

    tbl.add_row("Edge Mean",     f"[white]{metrics.get('edge_mean', 0.0):.4f}[/white]")
    tbl.add_row("Conf Mean",     f"[white]{metrics.get('confidence_mean', 0.0):.3f}[/white]")

    # Cooldown / consecutive losses
    tbl.add_row("", "")
    if cooldown > 0:
        tbl.add_row("[bold red]COOLDOWN[/bold red]", f"[bold red]{cooldown} windows remaining[/bold red]")
    if consec > 0:
        tbl.add_row("Consec Losses", f"[bold red]{consec}[/bold red]")
    if cooldown == 0 and consec == 0:
        tbl.add_row("Risk State", "[bold green]OK[/bold green]")

    # Open trades
    tbl.add_row("", "")
    tbl.add_row("[bold cyan]─── OPEN TRADE ───[/bold cyan]", "")
    if open_trades:
        t0 = open_trades[0]
        tbl.add_row("Side",     f"[bold white]{t0.side.upper()}[/bold white]")
        tbl.add_row("Entry",    f"[bold yellow]{t0.entry_price:.4f}[/bold yellow]")
        tbl.add_row("Shares",   f"[white]{t0.shares:.2f}[/white]")
        tbl.add_row("Notional", f"[white]{t0.notional:.4f} USDC[/white]")
        age = time.time() - t0.ts_open
        tbl.add_row("Age",      f"[dim white]{age:.0f}s[/dim white]")
    else:
        tbl.add_row("—", "[dim white]no open trades[/dim white]")

    # Stake progression hint
    tbl.add_row("", "")
    tbl.add_row("[bold cyan]─── STAKE SIZING ───[/bold cyan]", "")
    tbl.add_row("Bankroll",   f"[bold white]${bankroll:.2f}[/bold white]")
    tbl.add_row("Trades Total", f"[white]{total_trades}[/white]")
    tbl.add_row("Stale Rejects",  f"[{'bold red' if metrics.get('stale_reject_count',0) > 0 else 'dim white'}]{metrics.get('stale_reject_count',0)}[/]")
    tbl.add_row("Spread Rejects", f"[{'bold red' if metrics.get('spread_reject_count',0) > 0 else 'dim white'}]{metrics.get('spread_reject_count',0)}[/]")
    tbl.add_row("Skew Rejects",   f"[{'bold red' if metrics.get('skew_reject_count',0) > 0 else 'dim white'}]{metrics.get('skew_reject_count',0)}[/]")

    return Panel(tbl, title="[bold yellow]STATUS / KASA[/bold yellow]",
                 border_style="yellow", padding=(0, 1))


# ------------------------------------------------------------------ #
# Panel 5: Shadow Probe
# ------------------------------------------------------------------ #

def _build_shadow_panel(metrics: dict) -> Panel:
    tbl = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("K", style="bold cyan", min_width=18)
    tbl.add_column("V", style="white")

    total = metrics.get("shadow_quote_count", 0)
    filled = metrics.get("shadow_filled_count", 0)
    pending = metrics.get("shadow_pending_count", 0)
    expired = metrics.get("shadow_expired_count", 0)
    crossed = metrics.get("shadow_crossed_count", 0)
    adverse = metrics.get("shadow_adverse_fill_count", 0)

    fill_rate = filled / max(total, 1)
    adv_rate = adverse / max(filled, 1)

    tbl.add_row("Quotes Sent",    f"[white]{total}[/white]")
    tbl.add_row("Pending",        f"[yellow]{pending}[/yellow]")
    tbl.add_row("Filled",         f"[bold green]{filled}[/bold green]")
    tbl.add_row("Expired",        f"[dim white]{expired}[/dim white]")
    tbl.add_row("Crossed",        f"[red]{crossed}[/red]")
    tbl.add_row("Adverse Fill",   f"[bold red]{adverse}[/bold red]")
    tbl.add_row("", "")

    fr_s = "bold green" if fill_rate >= 0.4 else ("yellow" if fill_rate >= 0.2 else "bold red")
    tbl.add_row("Fill Rate",      f"[{fr_s}]{fill_rate*100:.1f}%[/{fr_s}]  [dim white]({filled}/{total})[/dim white]")
    adv_s = "bold red" if adv_rate > 0.3 else "dim white"
    tbl.add_row("Adverse Ratio",  f"[{adv_s}]{adv_rate*100:.1f}%[/{adv_s}]  [dim white]({adverse}/{filled if filled else 0})[/dim white]")

    return Panel(tbl, title="[bold cyan]SHADOW PROBE[/bold cyan]",
                 border_style="cyan", padding=(0, 1))


# ------------------------------------------------------------------ #
# Panel 6: Live Reject / Decision Tape
# ------------------------------------------------------------------ #

_LOG_KEYWORDS = {
    "PAPER_TRADE":     ("bold green",   "TRADE"),
    "paper_trade":     ("bold green",   "TRADE"),
    "SHADOW_QUOTE":    ("bold cyan",    "SHADOW"),
    "shadow_quote":    ("bold cyan",    "SHADOW"),
    "RESOLVE":         ("bold magenta", "RESOLVE"),
    "resolve_all":     ("bold magenta", "RESOLVE"),
    "wide_spread_reject":    ("bold red", "REJECT"),
    "complement_skew_reject": ("bold red", "REJECT"),
    "delta_too_small": ("bold red",    "REJECT"),
    "low_confidence":  ("bold red",    "REJECT"),
    "neutral_band":    ("bold red",    "REJECT"),
    "quiet_noise":     ("bold red",    "REJECT"),
    "stale_polymarket": ("bold red",   "REJECT"),
    "stale_binance":   ("bold red",    "REJECT"),
    "NO_TRADE":        ("red",         "NOTRADE"),
    "outside_entry_window": ("dim white", "WINDOW"),
    "Window initialised": ("yellow",   "WINDOW"),
    "Window boundary":    ("yellow",   "WINDOW"),
    "Window open snapshot": ("yellow", "WINDOW"),
    "New window open":     ("yellow",  "WINDOW"),
    "Sanity REJECT":   ("bold red",    "REJECT"),
    "sanity REJECT":   ("bold red",    "REJECT"),
}


def _classify_log(level: str, msg: str) -> tuple[str, str]:
    """Return (rich_style, tag) for a log line."""
    lu = level.upper()
    mu = msg.upper()
    if lu == "ERROR":
        return "bold red on white", "ERROR"
    if lu in ("WARNING", "WARN"):
        return "bold yellow", "WARN"
    # Check message content for typed classification
    for kw, (style, tag) in _LOG_KEYWORDS.items():
        if kw in msg or kw in mu:
            return style, tag
    return "white", "INFO"


def _build_log_panel(lines: list[tuple[str, str, str]], n: int = 20) -> Panel:
    text = Text(overflow="fold")
    shown = lines[-n:] if len(lines) > n else lines
    for ts_s, level, msg in shown:
        style, tag = _classify_log(level, msg)
        text.append(f"[{ts_s}] ", style=_T["log_ts"])
        text.append(f"{tag:<8}", style=style)
        # Truncate long lines
        display_msg = msg[:120]
        text.append(f"{display_msg}\n", style="white")

    return Panel(text, title="[bold cyan]LIVE REJECT/DECISION TAPE[/bold cyan]",
                 border_style="cyan", padding=(0, 1))


# ------------------------------------------------------------------ #
# Dashboard class
# ------------------------------------------------------------------ #

class UIDashboard:
    """
    Rich Live dashboard — 6-panel trader terminal.

    Runs in a daemon thread. Crash-isolated from main bot loop.
    """

    def __init__(self, settings: "Settings", ui_state: "UIState") -> None:
        if not _RICH_AVAILABLE:
            raise ImportError("rich is not installed — pip install rich")
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
                refresh_per_second=max(1, int(1.0 / max(refresh, 0.1))),
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

        header   = _build_header(snap)
        market   = _build_market_panel(snap)
        decision = _build_decision_panel(snap)
        status   = _build_status_panel(snap)
        shadow   = _build_shadow_panel(snap.get("metrics") or {})
        log_p    = _build_log_panel(log_lines, n=self._cfg.ui_show_log_lines)

        # Vertical split: header | mid | log
        layout.split_column(
            Layout(header,  name="header",  size=3),
            Layout(name="mid"),
            Layout(log_p,   name="log",     size=14),
        )

        # Mid: left (market+decision) | right (status+shadow) — ratio 3:2
        layout["mid"].split_row(
            Layout(name="left",  ratio=3),
            Layout(name="right", ratio=2),
        )
        layout["left"].split_column(
            Layout(market,   name="market"),
            Layout(decision, name="decision"),
        )
        layout["right"].split_column(
            Layout(status, name="status"),
            Layout(shadow, name="shadow"),
        )

        return layout
