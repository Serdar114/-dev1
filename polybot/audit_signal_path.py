"""
audit_signal_path.py -- Signal path forensic audit for btc_open_delta single-side lane.

Question:
  Where does the current btc_open_delta signal fail most often:
  before trigger, at PM gate filtering, or after entry?

Reads only: logs/polybot-*.jsonl, logs/truth-*.jsonl
Does NOT: run the bot, open trades, touch resolution_truth.py,
          touch paper_trader.py, touch thresholds/fees/maker logic.

Event taxonomy (from signal_engine.evaluate_single + main.py log filter):
  - main.py SUPPRESSES "outside_entry_window" and "no_orderbook" from log
    -> every signal event in the log is inside or approaching the entry window
  - "no_signal(open=X,mid=Y,delta_bps=Z,min=M)" -> delta did NOT cross threshold
  - "price_too_high(...)"  -> delta CROSSED, PM price gate blocked entry
  - "depth_low(...)"       -> delta CROSSED, PM depth gate blocked entry
  - "spread_too_wide(...)" -> delta CROSSED, PM spread gate blocked entry
  - "ok(open=X,...)"       -> delta CROSSED, all gates passed, entry signal fired

Window reconstruction:
  window_ts = floor((event_unix + secs_to_res - 300) / 300) * 300
  (because secs_to_res = window_ts + 300 - event_unix, rearranged)
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

LOGS_DIR = Path(__file__).parent / "logs"
INTERVAL_SECS = 300
MIN_MOVE_BPS_DEFAULT = 10  # from config_5m_single_side_delta_diag.json


# ---------------------------------------------------------------------------
# Log loading
# ---------------------------------------------------------------------------

def _load_jsonl(pattern: str) -> list[dict]:
    events = []
    for f in sorted(LOGS_DIR.glob(pattern)):
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
    return events


def _ts_unix(ts_str: str) -> float:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _window_ts_from_signal(ts_unix: float, secs_to_res: int) -> int:
    approx = ts_unix + secs_to_res - INTERVAL_SECS
    return (int(approx) // INTERVAL_SECS) * INTERVAL_SECS


# ---------------------------------------------------------------------------
# Reason classification
# ---------------------------------------------------------------------------

def _classify(reason: str) -> str:
    if reason.startswith("no_signal("):
        return "no_signal"
    if reason.startswith("price_too_high"):
        return "price_too_high"
    if reason.startswith("depth_low"):
        return "depth_low"
    if reason.startswith("spread_too_wide"):
        return "spread_too_wide"
    if reason.startswith("ok(") or reason == "ok":
        return "ok"
    if reason == "no_btc_prices":
        return "no_btc_prices"
    if reason == "outside_entry_window":
        return "outside_entry_window"   # shouldn't appear (suppressed by main.py)
    if reason == "no_orderbook":
        return "no_orderbook"           # shouldn't appear (suppressed by main.py)
    return f"other:{reason[:50]}"


def _is_gate_block(cls: str) -> bool:
    return cls in ("price_too_high", "depth_low", "spread_too_wide")


def _parse_bps(reason: str) -> float | None:
    m = re.search(r"delta_bps=(-?\d+\.?\d*)", reason)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    sep = "=" * 108
    print(sep)
    print("SIGNAL FORENSIC AUDIT -- btc_open_delta single-side signal path")
    print(sep)

    # ---- Load ---------------------------------------------------------------
    polybot_evts = _load_jsonl("polybot-*.jsonl")
    truth_evts   = _load_jsonl("truth-*.jsonl")
    all_evts     = polybot_evts + truth_evts

    polybot_files = list(LOGS_DIR.glob("polybot-*.jsonl"))
    truth_files   = list(LOGS_DIR.glob("truth-*.jsonl"))

    print(f"  logs dir       : {LOGS_DIR}")
    print(f"  polybot files  : {len(polybot_files)}  ({[f.name for f in polybot_files]})")
    print(f"  truth files    : {len(truth_files)}  ({[f.name for f in truth_files]})")
    print(f"  total events   : {len(all_evts)}")
    print()

    if not all_evts:
        _no_logs_report()
        return

    # ---- Separate event types -----------------------------------------------
    signal_evts      = [e for e in all_evts if e.get("event") == "signal"]
    trade_open_evts  = [e for e in all_evts if e.get("event") == "trade_opened"]
    trade_res_evts   = [e for e in all_evts if e.get("event") == "trade_resolved"]
    res_truth_evts   = [e for e in all_evts if e.get("event") == "resolution_truth"]

    print(f"  signal events             : {len(signal_evts)}")
    print(f"  trade_opened events       : {len(trade_open_evts)}")
    print(f"  trade_resolved events     : {len(trade_res_evts)}")
    print(f"  resolution_truth events   : {len(res_truth_evts)}")
    print()

    # ---- Filter to btc_open_delta signal events only -------------------------
    # Identifier: reason contains "delta_bps=" (present in no_signal and ok reasons)
    # OR reason is a gate-block class (price_too_high / depth_low / spread_too_wide)
    #   -> these only appear AFTER threshold crossed in evaluate_single()
    # Exclude dual_entry signals (action == "dual_entry")
    delta_sig_evts = [
        e for e in signal_evts
        if e.get("action") != "dual_entry"
        and (
            "delta_bps=" in e.get("reason", "")
            or _classify(e.get("reason", "")) in (
                "price_too_high", "depth_low", "spread_too_wide",
            )
            or e.get("action", "").startswith("single_entry_")
        )
    ]

    # Single-side trades (to add windows even if signal log not available)
    ss_trade_opens = [
        e for e in trade_open_evts
        if e.get("strategy") == "single_side_taker"
    ]

    # Attribute resolved events via trade_id join (primary), then window match
    # (fallback).  paper_trader.resolved_data carries neither "strategy" nor
    # "side", so filtering on those fields always returns zero rows.
    ss_trade_ids = {t["trade_id"] for t in ss_trade_opens if t.get("trade_id")}
    ss_open_wts  = {int(t["window_ts"]) for t in ss_trade_opens
                    if t.get("window_ts") is not None}

    def _is_ss_resolved(e: dict) -> bool:
        tid = e.get("trade_id", "")
        if tid and tid in ss_trade_ids:
            return True
        wts = e.get("window") or e.get("window_ts")
        return bool(wts and int(wts) in ss_open_wts)

    ss_trade_res = [e for e in trade_res_evts if _is_ss_resolved(e)]

    print(f"  btc_open_delta signal events : {len(delta_sig_evts)}")
    print(f"  single_side_taker trade_opened: {len(ss_trade_opens)}")
    print(f"  single_side_taker trade_resolved: {len(ss_trade_res)}")
    print()

    if not delta_sig_evts and not ss_trade_opens:
        print("  No btc_open_delta events found in logs.")
        print("  Logs exist but no btc_open_delta sessions recorded.")
        print()
        _no_delta_sessions_report()
        return

    # ---- Group signal events by window_ts ------------------------------------
    window_sigs: dict[int, list[dict]] = defaultdict(list)
    for e in delta_sig_evts:
        ts = _ts_unix(e.get("ts", ""))
        sts = e.get("secs_to_res", 0)
        if ts == 0:
            continue
        wts = _window_ts_from_signal(ts, sts)
        window_sigs[wts].append(e)

    # Ensure windows with trades are present even if no signal events captured
    for t in ss_trade_opens:
        wts = t.get("window_ts")
        if wts and int(wts) not in window_sigs:
            window_sigs[int(wts)] = []

    # ---- Build lookups -------------------------------------------------------
    # trade_opened: window_ts -> event (polybot and truth logs both have window_ts)
    trade_open_by_wts: dict[int, dict] = {}
    for t in ss_trade_opens:
        wts = t.get("window_ts")
        if wts:
            trade_open_by_wts[int(wts)] = t

    # trade_resolved: paper_trader uses key "window" (not "window_ts")
    trade_res_by_wts: dict[int, dict] = {}
    for r in ss_trade_res:
        wts = r.get("window") or r.get("window_ts")
        if wts:
            trade_res_by_wts[int(wts)] = r

    # resolution_truth: window_ts -> event
    res_truth_by_wts: dict[int, dict] = {}
    for r in res_truth_evts:
        wts = r.get("window_ts")
        if wts:
            res_truth_by_wts[int(wts)] = r

    # ---- Per-window analysis -------------------------------------------------
    window_rows = []
    for wts in sorted(window_sigs.keys()):
        sigs = sorted(window_sigs[wts], key=lambda e: -e.get("secs_to_res", 0))
        classified = [(e, _classify(e.get("reason", ""))) for e in sigs]

        # Count no_signal ticks (delta below threshold)
        no_sig_ticks = sum(1 for _, c in classified if c == "no_signal")

        # Max absolute delta seen in no_signal ticks
        max_abs_bps = None
        for e, c in classified:
            if c == "no_signal":
                d = _parse_bps(e.get("reason", ""))
                if d is not None and (max_abs_bps is None or abs(d) > max_abs_bps):
                    max_abs_bps = abs(d)

        # First threshold cross event:
        # First event where class is NOT no_signal and NOT no_btc_prices
        first_cross = None
        first_cross_cls = None
        for e, c in classified:
            if c not in ("no_signal", "no_btc_prices"):
                first_cross = e
                first_cross_cls = c
                break

        # Gate block counts (any tick, any cross)
        n_price  = sum(1 for _, c in classified if c == "price_too_high")
        n_depth  = sum(1 for _, c in classified if c == "depth_low")
        n_spread = sum(1 for _, c in classified if c == "spread_too_wide")

        # First-cross fields
        fc_secs  = first_cross.get("secs_to_res") if first_cross else None
        fc_side  = first_cross.get("side", "") if first_cross else ""
        fc_ask   = first_cross.get("entry_price", 0.0) if first_cross else 0.0
        fc_bps   = _parse_bps(first_cross.get("reason", "")) if first_cross else None

        # Trade outcome
        trade    = trade_open_by_wts.get(wts)
        resolved = trade_res_by_wts.get(wts)
        rt       = res_truth_by_wts.get(wts)

        trade_opened   = trade is not None
        trade_side     = (trade or {}).get("side", "")
        trade_ask      = (trade or {}).get("entry_price", 0.0)
        trade_secs     = (trade or {}).get("secs_to_res", 0)

        # Winner from resolved trade or resolution_truth fallback
        if resolved:
            winner_bnb = resolved.get("winner_binance", "unknown")
            winner_cl  = resolved.get("winner_chainlink", "unknown")
            rt_status  = resolved.get("resolution_truth_status", "unknown")
            net_pnl    = resolved.get("net_pnl")
        elif rt:
            winner_bnb = rt.get("winner_binance", "unknown")
            winner_cl  = rt.get("winner_chainlink", "unknown")
            rt_status  = rt.get("resolution_truth_status", "unknown")
            net_pnl    = None
        else:
            winner_bnb = "unknown"
            winner_cl  = "unknown"
            rt_status  = "unknown"
            net_pnl    = None

        # Window outcome classification
        if first_cross is None and no_sig_ticks > 0:
            outcome = "no_threshold_cross"
        elif first_cross is None and no_sig_ticks == 0:
            outcome = "no_data"
        elif first_cross_cls == "ok" and trade_opened:
            if winner_bnb in ("up", "down"):
                outcome = "trade_win" if winner_bnb == trade_side else "trade_loss"
            else:
                outcome = "trade_unresolved"
        elif first_cross_cls == "ok" and not trade_opened:
            outcome = "signal_ok_risk_blocked"
        elif _is_gate_block(first_cross_cls):
            outcome = "gate_blocked"
        else:
            outcome = f"other:{first_cross_cls}"

        window_rows.append({
            "wts"          : wts,
            "no_sig_ticks" : no_sig_ticks,
            "max_bps"      : max_abs_bps,
            "fc_secs"      : fc_secs,
            "fc_side"      : fc_side,
            "fc_ask"       : fc_ask,
            "fc_bps"       : fc_bps,
            "n_price"      : n_price,
            "n_depth"      : n_depth,
            "n_spread"     : n_spread,
            "trade_opened" : trade_opened,
            "trade_side"   : trade_side,
            "trade_ask"    : trade_ask,
            "trade_secs"   : trade_secs,
            "winner_bnb"   : winner_bnb,
            "winner_cl"    : winner_cl,
            "rt_status"    : rt_status,
            "net_pnl"      : net_pnl,
            "outcome"      : outcome,
        })

    # ---- Summary counts ------------------------------------------------------
    total       = len(window_rows)
    n_cross     = sum(1 for w in window_rows if w["fc_secs"] is not None)
    n_no_cross  = sum(1 for w in window_rows if w["outcome"] == "no_threshold_cross")
    n_gate      = sum(1 for w in window_rows if w["outcome"] == "gate_blocked")
    n_opened    = sum(1 for w in window_rows if w["trade_opened"])
    n_p_high    = sum(1 for w in window_rows if w["n_price"] > 0)
    n_d_low     = sum(1 for w in window_rows if w["n_depth"] > 0)
    n_s_wide    = sum(1 for w in window_rows if w["n_spread"] > 0)
    n_risk_blk  = sum(1 for w in window_rows if w["outcome"] == "signal_ok_risk_blocked")

    # Hit rates -- require valid winner
    cross_w_winner = [w for w in window_rows
                      if w["fc_side"] and w["winner_bnb"] in ("up", "down")]
    fc_hit = (sum(1 for w in cross_w_winner if w["winner_bnb"] == w["fc_side"])
              / len(cross_w_winner)) if cross_w_winner else None

    trade_w_winner = [w for w in window_rows
                      if w["trade_opened"] and w["winner_bnb"] in ("up", "down")]
    tr_hit = (sum(1 for w in trade_w_winner if w["winner_bnb"] == w["trade_side"])
              / len(trade_w_winner)) if trade_w_winner else None

    dv_trades = [w for w in window_rows
                 if w["trade_opened"] and w["rt_status"] == "dual_verified"
                 and w["winner_cl"] in ("up", "down")]
    dv_hit = (sum(1 for w in dv_trades if w["winner_cl"] == w["trade_side"])
              / len(dv_trades)) if len(dv_trades) >= 3 else None

    fmthr = lambda x, n: f"{x:.1%}  (n={n})" if x is not None else f"N/A  (n={n})"

    print(sep)
    print("SUMMARY COUNTS")
    print(f"  total_windows_seen             = {total}")
    print(f"  threshold_cross_count          = {n_cross}")
    print(f"  no_threshold_cross_count       = {n_no_cross}")
    print(f"  gate_blocked_count             = {n_gate}")
    print(f"  signal_ok_risk_blocked         = {n_risk_blk}")
    print(f"  opened_trade_count             = {n_opened}")
    print(f"  blocked_price_count            = {n_p_high}  (windows with >=1 price_too_high tick)")
    print(f"  blocked_depth_count            = {n_d_low}  (windows with >=1 depth_low tick)")
    print(f"  blocked_spread_count           = {n_s_wide}  (windows with >=1 spread_too_wide tick)")
    print()
    print(f"  first_cross_binance_hit_rate   = {fmthr(fc_hit, len(cross_w_winner))}")
    print(f"  opened_trade_binance_hit_rate  = {fmthr(tr_hit, len(trade_w_winner))}")
    print(f"  dual_verified_only_hit_rate    = {fmthr(dv_hit, len(dv_trades))}")
    print()

    # ---- Per-window table ----------------------------------------------------
    if window_rows:
        hdr = (f"{'wts':>12}  {'ns_tk':>5}  {'max_bps':>7}  "
               f"{'fc_s':>5}  {'side':>4}  {'fc_ask':>6}  {'fc_dbps':>7}  "
               f"{'p>hi':>4}  {'d<lo':>4}  {'spr':>3}  "
               f"{'trd':>3}  {'w_bnb':>5}  {'w_cl':>5}  {'outcome':>26}")
        print("-" * 108)
        print(hdr)
        print("-" * 108)
        for w in window_rows:
            def _fmt(v, fmt):
                return format(v, fmt) if v is not None else "-"
            print(
                f"{w['wts']:>12}  "
                f"{w['no_sig_ticks']:>5}  "
                f"{_fmt(w['max_bps'], '7.1f'):>7}  "
                f"{_fmt(w['fc_secs'], '5d'):>5}  "
                f"{w['fc_side'] or '-':>4}  "
                f"{w['fc_ask']:>6.3f}  "
                f"{_fmt(w['fc_bps'], '7.1f'):>7}  "
                f"{w['n_price']:>4}  "
                f"{w['n_depth']:>4}  "
                f"{w['n_spread']:>3}  "
                f"{'Y' if w['trade_opened'] else 'N':>3}  "
                f"{w['winner_bnb']:>5}  "
                f"{w['winner_cl']:>5}  "
                f"{w['outcome']:>26}"
            )
        print("-" * 108)
        print()

    # ---- Five example windows ------------------------------------------------
    print("FIVE EXAMPLE WINDOWS")
    print()
    good     = [w for w in window_rows if w["outcome"] == "trade_win"]
    bad      = [w for w in window_rows if w["outcome"] == "trade_loss"]
    gate_ok  = [w for w in window_rows                          # blocked but would-have-won
                if w["outcome"] == "gate_blocked"
                and w["fc_side"] and w["winner_bnb"] == w["fc_side"]]
    gate_any = [w for w in window_rows if w["outcome"] == "gate_blocked"]

    examples: list[tuple[str, dict]] = []
    for w in good[:2]:
        examples.append(("GOOD -- trade opened, winner matched", w))
    for w in bad[:2]:
        examples.append(("BAD -- trade opened, wrong side", w))
    for w in (gate_ok or gate_any)[:1]:
        label = ("BLOCKED-CORRECT -- gate blocked after correct-side cross"
                 if gate_ok else "BLOCKED -- gate blocked after threshold cross")
        examples.append((label, w))

    # Pad with any remaining windows if categories were sparse
    used = {id(w) for _, w in examples}
    for w in window_rows:
        if len(examples) >= 5:
            break
        if id(w) not in used:
            examples.append(("EXAMPLE", w))
            used.add(id(w))

    if not examples:
        print("  No example windows available -- insufficient log data.")
    else:
        for i, (label, w) in enumerate(examples[:5], 1):
            pnl_str = f"  net_pnl={w['net_pnl']:.4f}" if w["net_pnl"] is not None else ""
            print(f"  [{i}] {label}")
            print(f"       window_ts={w['wts']}  outcome={w['outcome']}{pnl_str}")
            print(f"       no_signal_ticks={w['no_sig_ticks']}  "
                  f"max_abs_delta_bps={w['max_bps']}  "
                  f"fc_secs_to_res={w['fc_secs']}")
            print(f"       first_cross: side={w['fc_side']}  "
                  f"ask={w['fc_ask']:.3f}  "
                  f"delta_bps={w['fc_bps']}")
            print(f"       gates: price_too_high={w['n_price']}  "
                  f"depth_low={w['n_depth']}  "
                  f"spread_too_wide={w['n_spread']}")
            print(f"       trade_opened={w['trade_opened']}  "
                  f"trade_side={w['trade_side']}  "
                  f"trade_ask={w['trade_ask']:.3f}  "
                  f"trade_secs_to_res={w['trade_secs']}")
            print(f"       winner_binance={w['winner_bnb']}  "
                  f"winner_chainlink={w['winner_cl']}  "
                  f"resolution_truth_status={w['rt_status']}")
            print()

    # ---- Verdict -------------------------------------------------------------
    print(sep)
    print("VERDICT")
    print()
    if total == 0:
        verdict = "insufficient_evidence"
        text = ("No windows found in logs -- "
                "cannot determine dominant failure mode.")
    elif n_cross == 0:
        verdict = "threshold_weakness"
        text = (f"Dominant failure: THRESHOLD WEAKNESS -- "
                f"0/{total} windows crossed min_move_bps threshold; "
                f"signal never triggered.")
    elif n_gate > n_opened and n_gate > n_no_cross:
        verdict = "PM_gating_delay"
        text = (f"Dominant failure: PM GATING DELAY / REPRICING -- "
                f"{n_gate} windows crossed threshold but were gate-blocked "
                f"(price_too_high:{n_p_high} depth_low:{n_d_low} spread_wide:{n_s_wide}) "
                f"vs {n_opened} trades opened; PM ask had already moved past gates "
                f"by the time the signal fired.")
    elif n_no_cross > n_cross:
        verdict = "threshold_weakness"
        text = (f"Dominant failure: THRESHOLD WEAKNESS -- "
                f"{n_no_cross}/{total} windows never crossed threshold "
                f"vs {n_cross} that did; signal too sparse to be a real lane.")
    elif n_opened > 0 and tr_hit is not None and tr_hit < 0.45:
        verdict = "post_entry_reversal"
        text = (f"Dominant failure: POST-ENTRY REVERSAL -- "
                f"{n_opened} trades opened, Binance hit rate = {tr_hit:.1%}; "
                f"threshold crossing is not predictive of direction.")
    else:
        verdict = "insufficient_evidence"
        text = (f"INSUFFICIENT EVIDENCE -- {total} windows, "
                f"{n_cross} crossed threshold, {n_opened} trades opened; "
                f"sample too small to isolate the dominant failure mode.")

    print(f"  {text}")
    print()
    print("  WHAT IS STILL NOT PROVEN:")
    print("  - Lag between first threshold cross and first gate-block tick "
          "(how many seconds does PM take to reprice?)")
    print("  - Whether gate blocks are sticky within a window or oscillate "
          "(delta stays above threshold but price stays above cap)")
    print("  - Post-entry reversal rate at filled price vs at-signal price "
          "(delta crosses -> PM matches -> BTC reverses before resolution)")
    print("  - Whether Chainlink winner diverges from Binance winner specifically "
          "on losing trades (settlement contamination of signal verdict)")
    print()
    print(sep)
    print("SEND BACK: patch diff + exact command run + full terminal output + key logs")


# ---------------------------------------------------------------------------
# No-logs helpers
# ---------------------------------------------------------------------------

def _no_logs_report():
    sep = "=" * 108
    print()
    print("NO LOGS FOUND.")
    print(f"  Expected : {LOGS_DIR}/polybot-*.jsonl  or  truth-*.jsonl")
    print(f"  Logs dir : {LOGS_DIR}  exists={LOGS_DIR.exists()}")
    contents = list(LOGS_DIR.iterdir()) if LOGS_DIR.exists() else []
    print(f"  Contents : {contents}")
    print()
    print("  Signal events needed for this audit:")
    print("    polybot-*.jsonl: event='signal' (from signal_engine.log_signal)")
    print("    polybot-*.jsonl: event='trade_opened' (from main.py)")
    print("    polybot-*.jsonl: event='trade_resolved' (from paper_trader)")
    print("    truth-*.jsonl  : event='trade_opened', 'trade_resolved', 'quote_snapshot'")
    print()
    print("  To generate logs, run:")
    print("    cd polybot")
    print("    python main.py --config config_5m_single_side_delta_diag.json --once")
    print()
    print("  Note: --once runs a single window then exits.")
    print("  Run multiple times (one per 5m window) to accumulate 10+ windows.")
    print()
    _print_verdict("insufficient_evidence",
                   "No local log data -- run at least 10 btc_open_delta sessions "
                   "to produce a statistically meaningful signal path verdict.")
    print(sep)
    print("SEND BACK: patch diff + exact command run + full terminal output + key logs")


def _no_delta_sessions_report():
    sep = "=" * 108
    _print_verdict("insufficient_evidence",
                   "Logs exist but no btc_open_delta (single_side_taker) sessions recorded -- "
                   "re-run with config_5m_single_side_delta_diag.json.")
    print(sep)
    print("SEND BACK: patch diff + exact command run + full terminal output + key logs")


def _print_verdict(kind: str, text: str):
    print("VERDICT")
    print()
    print(f"  [{kind}] {text}")
    print()
    print("  WHAT IS STILL NOT PROVEN:")
    print("  - Whether the threshold (min_move_bps=10) is ever crossed in live 5m windows")
    print("  - Whether PM ask reprices away from entry cap before signal can fire")
    print("  - Whether post-entry BTC reversal is the dominant loss cause")
    print("  - Whether Chainlink and Binance agree on winner in the specific windows "
          "where a btc_open_delta signal fires")
    print()


if __name__ == "__main__":
    main()
