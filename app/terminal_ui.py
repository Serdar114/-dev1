"""
app/terminal_ui.py — Curses terminal cockpit.

Renders a snapshot of SystemState every ~200ms.
Never holds state._lock during rendering.
Falls back to simple stdout if curses is unavailable.

Layout:
┌─ POLYBOT MEASUREMENT ──────────────────────────────────────────────────────┐
│ MARKET: btc-up-down-5m-1234567890                           MODE: MEAS     │
├────────────────────────────────────────────────────────────────────────────┤
│ WINDOW: 16:35:00→16:40:00 UTC    EXPIRY: 2m 34s    ID: 1234567890         │
├──────────────────────────────┬─────────────────────────────────────────────┤
│ CHAINLINK BTC/USD            │ BINANCE BTC/USDT (aux)                      │
│   $65,234.50  FRESH(3s)      │   Bid: $65,238.00  Ask: $65,240.00         │
│                              │   Mid: $65,239.00  FRESH(0s)               │
│                              │   Basis: +$4.50 (+0.007%)                  │
├──────────────────────────────┴─────────────────────────────────────────────┤
│ METADATA:  READY  tick=0.001  min=5.00 USDC  fee=2.00%(canonical)         │
├─────────────────────────────┬──────────────────────────────────────────────┤
│  UP TOKEN                   │  DOWN TOKEN                                  │
│  Bid: 0.4900 × 200          │  Bid: 0.5050 × 150                          │
│  Ask: 0.5100 × 300          │  Ask: 0.5150 × 250                          │
│  Spread: 0.0200             │  Spread: 0.0100                             │
├─────────────────────────────┴──────────────────────────────────────────────┤
│ Pair Sum (ask+ask): 1.025    Pair Sum (bid+bid): 0.995                     │
├────────────────────────────────────────────────────────────────────────────┤
│ NO-TRADE: none                                                             │
│ HYPO UP  @ 0.5100  win=+0.4700  lose=-0.5100  fee=2.00%(canonical)        │
│ HYPO DN  @ 0.5150  win=+0.4650  lose=-0.5150  fee=2.00%(canonical)        │
├────────────────────────────────────────────────────────────────────────────┤
│ EVENTS:                                                                    │
│  16:37:45 market_discovered slug=btc-up-down-5m-1234567890                │
└────────────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import curses
import time
from typing import Optional

from book.price_views import format_price, format_usd
from metadata.fee_schedule import compute_economics
from paper.fill_model import taker_economics
from signals.feature_builder import build as build_features
from signals.no_trade_rules import evaluate as eval_no_trade
from state import SystemState


# ---------------------------------------------------------------------------
# Color pair IDs
# ---------------------------------------------------------------------------
_C_NORMAL = 0
_C_GREEN = 1
_C_RED = 2
_C_YELLOW = 3
_C_CYAN = 4
_C_WHITE = 5


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(_C_RED, curses.COLOR_RED, -1)
    curses.init_pair(_C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(_C_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(_C_WHITE, curses.COLOR_WHITE, -1)


def _color(pair_id: int) -> int:
    return curses.color_pair(pair_id)


def _freshness_color(label: str) -> int:
    if "FRESH" in label:
        return _color(_C_GREEN)
    if "STALE" in label or "MISSING" in label:
        return _color(_C_RED)
    return _color(_C_YELLOW)


# ---------------------------------------------------------------------------
# Snapshot helper
# ---------------------------------------------------------------------------

def _take_snapshot(state: SystemState) -> dict:
    """Take a thread-safe snapshot of all display-relevant fields."""
    with state._lock:
        market = state.market
        meta = state.metadata
        window = state.window
        cl = state.chainlink
        bn = state.binance
        up = state.up_book
        dn = state.down_book
        no_trade = list(state.no_trade_reasons)
        events = list(state.lifecycle_events[-8:])
        mode = state.mode

    # System status computed under lock
    cl_max_age = float(config.get("chainlink", {}).get("max_age_seconds", 120))
    buf_depth = getattr(state, "_buffer_depth_snapshot", 0)
    status_label, status_blocking = state.system_status_label(cl_max_age, buf_depth)

    return {
        "market": market,
        "meta": meta,
        "window": window,
        "cl": cl,
        "bn": bn,
        "up": up,
        "dn": dn,
        "no_trade": no_trade,
        "events": events,
        "mode": mode,
        "now": time.time(),
        "status_label": status_label,
        "status_blocking": status_blocking,
    }


# ---------------------------------------------------------------------------
# Screen rendering
# ---------------------------------------------------------------------------

def _render(stdscr, snap: dict, config: dict) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    row = 0

    def safe_add(y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            if y < max_y - 1:
                stdscr.addstr(y, x, text[:max_x - x - 1], attr)
        except curses.error:
            pass

    market = snap["market"]
    meta = snap["meta"]
    window = snap["window"]
    cl = snap["cl"]
    bn = snap["bn"]
    up = snap["up"]
    dn = snap["dn"]
    no_trade = snap["no_trade"]
    events = snap["events"]
    mode = snap["mode"].upper()
    status_label = snap.get("status_label", "MEASUREMENT SCAFFOLD")
    status_blocking = snap.get("status_blocking", [])

    # ── Row 0: System status header ─────────────────────────────────────────
    # Shows TRUTH-TIGHT / DEGRADED / MEASUREMENT SCAFFOLD based on live truth quality
    if status_label == "TRUTH-TIGHT":
        status_color = _color(_C_GREEN) | curses.A_BOLD
    elif status_label == "DEGRADED":
        status_color = _color(_C_YELLOW) | curses.A_BOLD
    else:
        status_color = _color(_C_RED) | curses.A_BOLD

    slug = market.slug if market else "no market"
    status_line = f" [{status_label}] {mode} | {slug[:45]}"
    safe_add(row, 0, status_line.ljust(max_x - 1), status_color)
    row += 1

    # Show blocking reasons if not TRUTH-TIGHT
    if status_blocking:
        blocking_str = " BLOCKING: " + "  ".join(status_blocking[:4])
        safe_add(row, 0, blocking_str[:max_x - 1], _color(_C_YELLOW))
    row += 1

    # ── Row 1: Window ──────────────────────────────────────────────────────
    if window.start > 0:
        secs = window.secs_to_expiry()
        m, s = divmod(secs, 60)
        win_label = window.label()
        expiry_str = f"{m:02d}m{s:02d}s"
        wline = f" WIN: {win_label}  EXPIRY: {expiry_str}  ID:{window.start}"
    else:
        wline = " WIN: --  EXPIRY: --"
    safe_add(row, 0, wline.ljust(max_x - 1))
    row += 1

    # ── Row 2: Separator ───────────────────────────────────────────────────
    safe_add(row, 0, "─" * (max_x - 1), _color(_C_CYAN))
    row += 1

    # ── Row 3-4: Feeds ─────────────────────────────────────────────────────
    # Chainlink
    cl_price_str = format_usd(cl.price) if cl.price else "MISSING"
    cl_age = cl.age_seconds()
    cl_fresh = "MISSING" if cl_age is None else (
        f"FRESH({cl_age:.0f}s)" if cl_age <= float(config["chainlink"]["max_age_seconds"])
        else f"STALE({cl_age:.0f}s)"
    )
    cl_color = _freshness_color(cl_fresh)
    safe_add(row, 0, f" CHAINLINK: {cl_price_str}")
    safe_add(row, 30, cl_fresh, cl_color)

    # Binance
    bn_mid = bn.mid()
    bn_mid_str = format_usd(bn_mid)
    bn_age = bn.age_seconds()
    bn_fresh = "MISSING" if bn_age is None else (
        f"FRESH({bn_age:.0f}s)" if bn_age <= float(config["binance"]["max_age_seconds"])
        else f"STALE({bn_age:.0f}s)"
    )
    bn_color = _freshness_color(bn_fresh)
    safe_add(row, 50, f"| BINANCE mid: {bn_mid_str}")
    safe_add(row, 78, bn_fresh, bn_color)
    row += 1

    # Basis
    if cl.price and bn_mid:
        basis = bn_mid - cl.price
        basis_pct = basis / cl.price * 100.0
        basis_str = f" Basis: {basis:+.2f} ({basis_pct:+.4f}%)"
        b_color = _color(_C_GREEN) if abs(basis_pct) < 0.5 else _color(_C_YELLOW)
        safe_add(row, 0, basis_str, b_color)
    else:
        safe_add(row, 0, " Basis: --", _color(_C_RED))
    row += 1

    # ── Row 5: Separator ───────────────────────────────────────────────────
    safe_add(row, 0, "─" * (max_x - 1), _color(_C_CYAN))
    row += 1

    # ── Row 6: Metadata ────────────────────────────────────────────────────
    if meta:
        r = meta.readiness_label()
        tick_s = f"{meta.tick_size:.4f}" if meta.tick_size else "--"
        min_s = f"{meta.min_order_size:.2f}" if meta.min_order_size else "--"
        fee_s = f"{meta.taker_fee_rate*100:.2f}%({meta.fee_provenance})" if meta.taker_fee_rate else "--"
        meta_line = f" META: {r}  tick={tick_s}  min={min_s} USDC  fee={fee_s}"
        m_color = _color(_C_GREEN) if r == "READY" else _color(_C_YELLOW)
        safe_add(row, 0, meta_line, m_color)
    else:
        safe_add(row, 0, " META: MISSING", _color(_C_RED))
    row += 1

    # ── Row 7: Separator ───────────────────────────────────────────────────
    safe_add(row, 0, "─" * (max_x - 1), _color(_C_CYAN))
    row += 1

    # ── Row 8-10: Orderbook ────────────────────────────────────────────────
    safe_add(row, 0, f" {'UP TOKEN':<38}| {'DOWN TOKEN'}", _color(_C_WHITE) | curses.A_BOLD)
    row += 1

    up_ba = up.best_ask()
    up_bb = up.best_bid()
    dn_ba = dn.best_ask()
    dn_bb = dn.best_bid()

    up_ask_s = f"Ask: {up_ba[0]:.4f} × {up_ba[1]:.1f}" if up_ba else "Ask: --"
    up_bid_s = f"Bid: {up_bb[0]:.4f} × {up_bb[1]:.1f}" if up_bb else "Bid: --"
    dn_ask_s = f"Ask: {dn_ba[0]:.4f} × {dn_ba[1]:.1f}" if dn_ba else "Ask: --"
    dn_bid_s = f"Bid: {dn_bb[0]:.4f} × {dn_bb[1]:.1f}" if dn_bb else "Bid: --"

    safe_add(row, 0, f"  {up_ask_s:<38}|  {dn_ask_s}")
    row += 1
    safe_add(row, 0, f"  {up_bid_s:<38}|  {dn_bid_s}")
    row += 1

    up_sp = up.spread()
    dn_sp = dn.spread()
    sp_line = (
        f"  Spread UP: {up_sp:.4f}" if up_sp else "  Spread UP: --"
    ) + (
        f"   DOWN: {dn_sp:.4f}" if dn_sp else "   DOWN: --"
    )
    safe_add(row, 0, sp_line)
    row += 1

    # ── Row 11: Pair sum ───────────────────────────────────────────────────
    from state import SystemState as _SS
    ps_ask = None
    ps_bid = None
    if up_ba and dn_ba:
        ps_ask = up_ba[0] + dn_ba[0]
    if up_bb and dn_bb:
        ps_bid = up_bb[0] + dn_bb[0]
    ps_min = float(config["measurement"]["pair_sum_min"])
    ps_max = float(config["measurement"]["pair_sum_max"])
    ps_str = f"{ps_ask:.4f}" if ps_ask else "--"
    ps_color = _color(_C_GREEN)
    if ps_ask is None or ps_ask < ps_min or ps_ask > ps_max:
        ps_color = _color(_C_RED)
    safe_add(row, 0, f" Pair Sum ask: {ps_str}", ps_color)
    row += 1

    # ── Row 12: Separator ──────────────────────────────────────────────────
    safe_add(row, 0, "─" * (max_x - 1), _color(_C_CYAN))
    row += 1

    # ── Row 13: No-trade ───────────────────────────────────────────────────
    if no_trade:
        nt_str = " NO-TRADE: " + " | ".join(no_trade[:5])
        safe_add(row, 0, nt_str[:max_x - 1], _color(_C_RED))
    else:
        safe_add(row, 0, " NO-TRADE: none (all conditions pass)", _color(_C_GREEN))
    row += 1

    # ── Row 14-15: Hypothetical entries ────────────────────────────────────
    if not no_trade and meta and meta.taker_fee_rate:
        fee_rate = meta.taker_fee_rate
        fprov = meta.fee_provenance
        if up_ba:
            p = up_ba[0]
            fee_pu = fee_rate * p * (1.0 - p)   # official formula: rate * p * (1-p)
            net_win = 1.0 - p - fee_pu
            net_lose = -(p + fee_pu)
            safe_add(row, 0,
                f" HYPO UP  @ {p:.4f}  win={net_win:+.4f}  lose={net_lose:+.4f}"
                f"  fee_pu={fee_pu:.5f}  rate={fee_rate*100:.2f}%({fprov})",
                _color(_C_WHITE))
        row += 1
        if dn_ba:
            p = dn_ba[0]
            fee_pu = fee_rate * p * (1.0 - p)
            net_win = 1.0 - p - fee_pu
            net_lose = -(p + fee_pu)
            safe_add(row, 0,
                f" HYPO DN  @ {p:.4f}  win={net_win:+.4f}  lose={net_lose:+.4f}"
                f"  fee_pu={fee_pu:.5f}  rate={fee_rate*100:.2f}%({fprov})",
                _color(_C_WHITE))
        row += 1
    else:
        row += 2

    # ── Row 16: Separator ──────────────────────────────────────────────────
    safe_add(row, 0, "─" * (max_x - 1), _color(_C_CYAN))
    row += 1

    # ── Rows 17+: Lifecycle events ─────────────────────────────────────────
    safe_add(row, 0, " EVENTS:", _color(_C_CYAN))
    row += 1
    for ev_line in events[-min(6, max_y - row - 1):]:
        safe_add(row, 0, f"  {ev_line[:max_x - 3]}")
        row += 1

    stdscr.refresh()


# ---------------------------------------------------------------------------
# Main UI loop
# ---------------------------------------------------------------------------

class TerminalUI:
    """
    Curses-based terminal cockpit.
    Call .run(state, config) to start.
    Call .stop() from another thread to exit.
    """

    def __init__(self) -> None:
        self._running = False

    def run(self, state: SystemState, config: dict) -> None:
        """Blocking call. Runs until KeyboardInterrupt or .stop()."""
        self._running = True
        try:
            curses.wrapper(self._main_loop, state, config)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    def _main_loop(self, stdscr, state: SystemState, config: dict) -> None:
        _init_colors()
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

        while self._running:
            try:
                key = stdscr.getch()
                if key in (ord("q"), ord("Q"), 27):  # q or ESC
                    break
            except curses.error:
                pass

            snap = _take_snapshot(state)
            try:
                _render(stdscr, snap, config)
            except curses.error:
                pass

            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Fallback: plain stdout display (no curses)
# ---------------------------------------------------------------------------

def stdout_render(state: SystemState, config: dict) -> None:
    """Simple non-curses render for debugging or non-TTY environments."""
    snap = _take_snapshot(state)
    market = snap["market"]
    cl = snap["cl"]
    bn = snap["bn"]
    window = snap["window"]
    no_trade = snap["no_trade"]

    print("=" * 70)
    print(f"MODE: {snap['mode']}  MARKET: {market.slug if market else 'NONE'}")
    print(f"WINDOW: {window.label()}  EXPIRY: {window.secs_to_expiry()}s")
    cl_age = cl.age_seconds()
    print(f"CHAINLINK: {format_usd(cl.price)}  age={cl_age:.0f}s" if cl_age else "CHAINLINK: MISSING")
    print(f"BINANCE mid: {format_usd(bn.mid())}  age={bn.age_seconds():.0f}s" if bn.age_seconds() else "BINANCE: MISSING")
    if no_trade:
        print(f"NO-TRADE: {' | '.join(no_trade)}")
    else:
        print("NO-TRADE: none")
    print("=" * 70)
