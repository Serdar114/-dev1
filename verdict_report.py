"""
verdict_report.py - Post-session verdict report.

Aggregates all data collected during one session and produces:
  1. A human-readable console summary
  2. A machine-readable JSON report
  3. A one-line CSV row appended to a multi-session history file

Report covers:
  ─ windows observed (count, timing coverage)
  ─ candidate taker setups found (total evaluated, gates passed)
  ─ candidate maker setups found (quotes placed, fills, adverse selection rate)
  ─ spread distribution in last 60/10/5 seconds per market
  ─ min_order_size findings per market
  ─ runtime failure summary (WS disconnects, RTT stats, rate limits, errors)
  ─ single most load-bearing bottleneck (the one thing most likely to kill live trading)
  ─ whether anything moved closer to live candidacy

Output:
  data/reports/report_<session_id>.json
  data/reports/report_<session_id>.txt
  data/reports/session_history.csv    (appended, one row per session)
"""

import csv
import json
import logging
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_mean(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _safe_percentile(vals: List[float], pct: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    idx = max(0, min(len(s) - 1, int(len(s) * pct)))
    return round(s[idx], 4)


def _spread_stats(snaps: List[Dict], within_seconds: float) -> Dict:
    """Compute spread statistics for snapshots within N seconds of close."""
    relevant = [
        s for s in snaps
        if s.get("fetch_ok") and s.get("seconds_to_close") is not None
        and 0 <= s["seconds_to_close"] <= within_seconds
        and s.get("spread_pct") is not None
    ]
    spreads = [s["spread_pct"] for s in relevant]
    return {
        "n_snapshots":  len(relevant),
        "mean_pct":     _safe_mean(spreads),
        "median_pct":   _safe_percentile(spreads, 0.5),
        "p95_pct":      _safe_percentile(spreads, 0.95),
        "min_pct":      round(min(spreads), 4) if spreads else None,
        "max_pct":      round(max(spreads), 4) if spreads else None,
    }


# ── bottleneck analysis ───────────────────────────────────────────────────────

def _identify_bottleneck(
    runtime_summary: Dict,
    spread_data: Dict,
    taker_candidates: List[Dict],
    maker_summaries: List[Dict],
    markets: List[Dict],
) -> Tuple[str, str]:
    """
    Identify the single most load-bearing bottleneck for live candidacy.
    Returns (bottleneck_label, explanation).
    """
    issues: List[Tuple[int, str, str]] = []   # (severity, label, explanation)

    # WS reliability
    ws_disc = runtime_summary.get("ws_disconnects", 0)
    hb_gaps = runtime_summary.get("heartbeat_gaps", 0)
    if ws_disc > 2:
        issues.append((10, "ws_instability",
                       f"WS disconnected {ws_disc}x – cannot reliably track book in last seconds"))
    elif hb_gaps > 5:
        issues.append((8, "ws_heartbeat_gaps",
                       f"{hb_gaps} heartbeat gaps – WS delivery unreliable"))

    # Rate limits
    rate_lims = runtime_summary.get("rate_limits", 0)
    if rate_lims > 0:
        issues.append((9, "rate_limiting_425",
                       f"Hit 425 rate limit {rate_lims}x – REST polling frequency too high"))

    # Spread too wide in last seconds
    for mkt_id, spread_info in spread_data.items():
        last5 = spread_info.get("last_5s", {})
        median_spread = last5.get("median_pct")
        if median_spread is not None and median_spread > config.TAKER_MAX_SPREAD_PCT * 100:
            issues.append((7, f"spread_too_wide_{mkt_id[:8]}",
                           f"market {mkt_id[:8]} median spread in last 5s = {median_spread:.1f}% "
                           f"> {config.TAKER_MAX_SPREAD_PCT*100:.0f}% threshold"))

    # No markets found
    if not markets:
        issues.append((10, "no_btc_5m_markets",
                       "Zero BTC 5-minute markets discovered – check keyword filters or market availability"))

    # Min order size too large for bankroll
    for mkt in markets:
        min_sz = float(mkt.get("min_order_size") or config.DEFAULT_MIN_SHARES)
        best_ask_guess = 0.60   # conservative guess
        cost = min_sz * best_ask_guess * (1 + config.TAKER_FEE_RATE)
        if cost > config.MAX_POSITION_USDC:
            issues.append((6, f"min_size_too_large_{mkt.get('market_id','')[:8]}",
                           f"min_order_size={min_sz} shares costs ≈ {cost:.2f} USDC > max_pos={config.MAX_POSITION_USDC}"))

    # High adverse selection
    for s in maker_summaries:
        asr = s.get("adverse_selection_rate")
        if asr is not None and asr > 0.7:
            issues.append((6, f"adverse_selection_{s.get('market_id','')[:8]}",
                           f"maker adverse selection = {asr:.1%} – market making structurally toxic"))

    # REST latency
    worst_rtt = runtime_summary.get("worst_rtt_ms")
    if worst_rtt and worst_rtt > 500:
        issues.append((5, "high_rest_latency",
                       f"Worst REST RTT = {worst_rtt:.0f}ms – too slow for late-window taker timing"))

    # No taker candidates passed execution tier
    exec_count = sum(1 for c in taker_candidates if c.get("execution_candidate"))
    if not taker_candidates:
        issues.append((4, "no_taker_evaluated",
                       "No taker candidates evaluated – likely no markets or book data missing"))
    elif exec_count == 0:
        issues.append((3, "no_taker_passed_gates",
                       f"{len(taker_candidates)} taker candidates evaluated, 0 reached execution tier"))

    # Runtime errors
    rest_errors = runtime_summary.get("rest_errors", 0)
    if rest_errors > 10:
        issues.append((5, "rest_errors",
                       f"{rest_errors} REST errors – network or API instability"))

    if not issues:
        return ("none_identified", "No critical bottleneck detected in this session")

    # Sort by severity descending, take worst
    issues.sort(key=lambda x: -x[0])
    _, label, explanation = issues[0]
    return (label, explanation)


# ── candidacy assessment ──────────────────────────────────────────────────────

def _assess_candidacy(
    markets: List[Dict],
    taker_candidates: List[Dict],
    maker_summaries: List[Dict],
    runtime_summary: Dict,
    bottleneck_label: str,
) -> Tuple[bool, List[str]]:
    """
    Return (any_progress, list_of_findings).
    We do NOT claim edge. We only report what moved closer.
    """
    findings: List[str] = []
    any_progress = False

    if markets:
        findings.append(f"[+] {len(markets)} BTC 5m market(s) discovered and tracked")
        any_progress = True

    # Taker tier breakdown
    signal_c   = sum(1 for c in taker_candidates if c.get("signal_candidate"))
    pricing_c  = sum(1 for c in taker_candidates if c.get("pricing_candidate"))
    exec_c     = sum(1 for c in taker_candidates if c.get("execution_candidate"))
    ev_true    = sum(1 for c in taker_candidates if c.get("ev_candidate") is True)
    ev_unknown = sum(1 for c in taker_candidates if c.get("ev_candidate") is None and c.get("execution_candidate"))

    findings.append(
        f"[~] Taker tiers: signal={signal_c}  pricing={pricing_c}  "
        f"execution={exec_c}  ev=True:{ev_true}/Unknown:{ev_unknown}"
    )
    if exec_c > 0:
        any_progress = True
        findings.append(
            f"[+] {exec_c} execution candidate(s) – timing/spread/depth/size gates passed. "
            f"EV unknown ({ev_unknown}) because no external true_prob was supplied. "
            f"This is the expected state at this measurement stage."
        )
        wins   = sum(1 for c in taker_candidates if c.get("execution_candidate") and c.get("would_have_won") is True)
        losses = sum(1 for c in taker_candidates if c.get("execution_candidate") and c.get("would_have_won") is False)
        unres  = sum(1 for c in taker_candidates if c.get("execution_candidate") and c.get("would_have_won") is None)
        findings.append(f"  → Settlement: win={wins} loss={losses} unresolved={unres}")
    else:
        findings.append(f"[-] 0 execution candidates – check spread/depth/min_size gates")

    fill_count = sum(s.get("total_fills", 0) for s in maker_summaries)
    if fill_count > 0:
        rates = [s["adverse_selection_rate"] for s in maker_summaries
                 if s.get("adverse_selection_rate") is not None]
        avg_asr = _safe_mean(rates)
        findings.append(
            f"[~] {fill_count} maker shadow fill(s). "
            f"Mean adverse selection rate = {avg_asr*100:.1f}% "
            f"({'toxic' if avg_asr and avg_asr > 0.6 else 'borderline' if avg_asr and avg_asr > 0.4 else 'acceptable'})"
        )
    else:
        findings.append("[-] 0 maker shadow fills – spread never crossed our quotes or no book data")

    ws_disc = runtime_summary.get("ws_disconnects", 0)
    if ws_disc == 0:
        findings.append("[+] WebSocket stable: 0 disconnects")
    else:
        findings.append(f"[-] {ws_disc} WS disconnects – reliability concern for live use")

    critical = ["ws_instability", "no_btc_5m_markets", "rate_limiting_425", "no_taker_evaluated"]
    if bottleneck_label in critical:
        findings.append(f"[!] Bottleneck '{bottleneck_label}' is critical – NOT candidate for live")
    else:
        findings.append(f"[~] Bottleneck '{bottleneck_label}' is addressable in next session")

    return any_progress, findings


# ── spread distribution builder ───────────────────────────────────────────────

def _build_spread_data(book_recorders: Dict[str, Dict]) -> Dict:
    """Build spread distribution stats per market from book recorder ring buffers."""
    spread_data: Dict[str, Dict] = {}

    for mid, recs in book_recorders.items():
        mkt_spreads: Dict = {}
        for side_label, rec_key in [("YES", "yes"), ("NO", "no")]:
            rec = recs.get(rec_key)
            if rec is None:
                continue
            snaps = list(rec.recent)
            mkt_spreads[side_label] = {
                "last_60s":  _spread_stats(snaps, 60),
                "last_10s":  _spread_stats(snaps, 10),
                "last_5s":   _spread_stats(snaps, 5),
                "all_time":  _spread_stats(snaps, float("inf")),
            }
        spread_data[mid] = mkt_spreads

    return spread_data


# ── main report generator ─────────────────────────────────────────────────────

def generate_report(
    session_id: str,
    markets: List[Dict],
    book_recorders: Dict[str, Dict],
    taker_evaluators: Dict,    # market_id -> TakerShadowEvaluator
    maker_evaluators: Dict,    # market_id -> List[MakerShadowEvaluator]
    runtime_logger,
    session_start_ts: float,
) -> Dict:
    """
    Generate the full verdict report. Saves to JSON, TXT, and appends CSV.
    Returns the report dict.
    """
    session_end_ts  = time.time()
    session_duration_s = round(session_end_ts - session_start_ts, 1)

    runtime_summary = runtime_logger.summary()

    # ── collect taker data ────────────────────────────────────────────────────
    all_taker_candidates: List[Dict] = []
    taker_by_market: Dict[str, Dict] = {}
    for mid, ev in taker_evaluators.items():
        all_taker_candidates.extend(ev.candidates)
        taker_by_market[mid] = {
            "total_evaluated": len(ev.candidates),
            "passed":          ev.passed_count,
        }

    # ── collect maker data ────────────────────────────────────────────────────
    all_maker_summaries: List[Dict] = []
    for mid, evlist in maker_evaluators.items():
        for ev in evlist:
            all_maker_summaries.append(ev.summary())

    # ── spread data ───────────────────────────────────────────────────────────
    spread_data = _build_spread_data(book_recorders)

    # ── open reference truth summary ─────────────────────────────────────────
    open_ref_summary: List[Dict] = []
    for market in markets:
        mid = market.get("market_id") or market.get("condition_id", "?")
        # Pull from ref_recorders if passed (they're in taker_evaluators)
        ev = taker_evaluators.get(mid)
        ref_rec = getattr(ev, "_ref_recorder", None) if ev else None
        if ref_rec is not None:
            open_ref_summary.append({
                "market_id":              mid[:12],
                "open_reference_is_true": ref_rec.open_reference_is_true,
                "open_reference_lag_s":   ref_rec.open_reference_lag_s,
                "market_window_open_utc": ref_rec.market_window_open_time.isoformat()
                                          if ref_rec.market_window_open_time else None,
                "recorder_start_utc":     ref_rec.recorder_start_time.isoformat()
                                          if ref_rec.recorder_start_time else None,
                "snap_count":             len(ref_rec.snapshots),
            })
        else:
            open_ref_summary.append({
                "market_id":              mid[:12],
                "open_reference_is_true": None,
                "open_reference_lag_s":   None,
                "note":                   "no ref recorder available",
            })

    # ── token mapping summary ─────────────────────────────────────────────────
    token_mapping_summary: List[Dict] = []
    for mkt in markets:
        token_mapping_summary.append({
            "market_id":               (mkt.get("market_id") or "")[:12],
            "token_mapping_confidence": mkt.get("token_mapping_confidence", "unknown"),
            "outcome_labels":          mkt.get("outcome_labels", []),
            "yes_token_id":            (mkt.get("yes_token_id") or "MISSING")[:16],
            "no_token_id":             (mkt.get("no_token_id") or "MISSING")[:16],
        })

    # ── min_order_size findings ───────────────────────────────────────────────
    min_size_findings: List[Dict] = []
    for mkt in markets:
        min_sz  = mkt.get("min_order_size")
        tick_sz = mkt.get("tick_size")
        source  = mkt.get("min_order_size_source", "unknown")
        ask_guess = 0.60
        if min_sz is not None:
            cost_at_guess = float(min_sz) * ask_guess * (1 + config.TAKER_FEE_RATE)
            fits = cost_at_guess <= config.MAX_POSITION_USDC
        else:
            cost_at_guess = None
            fits = None
        min_size_findings.append({
            "market_id":              mkt.get("market_id", "")[:12],
            "min_order_size":         min_sz,
            "min_order_size_source":  source,
            "tick_size":              tick_sz,
            "cost_usdc_at_0.60_ask":  round(cost_at_guess, 4) if cost_at_guess else None,
            "fits_max_pos":           fits,
        })

    # ── bottleneck ────────────────────────────────────────────────────────────
    bottleneck_label, bottleneck_explanation = _identify_bottleneck(
        runtime_summary, spread_data, all_taker_candidates, all_maker_summaries, markets
    )

    # ── candidacy ─────────────────────────────────────────────────────────────
    any_progress, candidacy_findings = _assess_candidacy(
        markets, all_taker_candidates, all_maker_summaries, runtime_summary, bottleneck_label
    )

    # ── assemble report ───────────────────────────────────────────────────────
    report = {
        "session_id":             session_id,
        "session_start_utc":      datetime.fromtimestamp(session_start_ts, tz=timezone.utc).isoformat(),
        "session_end_utc":        datetime.fromtimestamp(session_end_ts, tz=timezone.utc).isoformat(),
        "session_duration_s":     session_duration_s,
        "bankroll_usdc":          config.BANKROLL_USDC,
        "max_position_usdc":      config.MAX_POSITION_USDC,

        "markets_observed":       len(markets),
        "markets":                [
            {
                "market_id":      m.get("market_id", "")[:16],
                "question":       m.get("question", "")[:80],
                "status":         m.get("status"),
                "window_seconds": m.get("window_seconds"),
                "yes_token_id":   (m.get("yes_token_id") or "")[:16],
                "no_token_id":    (m.get("no_token_id") or "")[:16],
            }
            for m in markets
        ],

        "taker_summary": {
            "total_evaluated":   len(all_taker_candidates),
            "signal_candidates": sum(1 for c in all_taker_candidates if c.get("signal_candidate")),
            "pricing_candidates": sum(1 for c in all_taker_candidates if c.get("pricing_candidate")),
            "execution_candidates": sum(1 for c in all_taker_candidates if c.get("execution_candidate")),
            "ev_true":           sum(1 for c in all_taker_candidates if c.get("ev_candidate") is True),
            "ev_unknown":        sum(1 for c in all_taker_candidates if c.get("ev_candidate") is None and c.get("execution_candidate")),
            "by_market":         taker_by_market,
            "settled_wins":      sum(1 for c in all_taker_candidates if c.get("would_have_won") is True),
            "settled_losses":    sum(1 for c in all_taker_candidates if c.get("would_have_won") is False),
        },

        "maker_summary": {
            "total_quotes":      sum(s.get("total_quotes", 0) for s in all_maker_summaries),
            "total_fills":       sum(s.get("total_fills", 0) for s in all_maker_summaries),
            "mean_adverse_rate": _safe_mean([
                s["adverse_selection_rate"] for s in all_maker_summaries
                if s.get("adverse_selection_rate") is not None
            ]),
            "by_side":           all_maker_summaries,
        },

        "open_ref_summary":        open_ref_summary,
        "token_mapping_summary":   token_mapping_summary,
        "spread_distribution":     spread_data,
        "min_order_size_findings": min_size_findings,
        "runtime_summary":         runtime_summary,

        "bottleneck": {
            "label":       bottleneck_label,
            "explanation": bottleneck_explanation,
        },

        "candidacy_assessment": {
            "any_progress":   any_progress,
            "findings":       candidacy_findings,
        },
    }

    # ── persist JSON ──────────────────────────────────────────────────────────
    json_path = config.REPORTS_DIR / f"report_{session_id}.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Report saved: %s", json_path)

    # ── persist TXT (human readable) ─────────────────────────────────────────
    txt_path = config.REPORTS_DIR / f"report_{session_id}.txt"
    lines = _format_txt(report)
    with open(txt_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log.info("Text report saved: %s", txt_path)

    # ── append to session history CSV ─────────────────────────────────────────
    hist_path = config.REPORTS_DIR / "session_history.csv"
    hist_fields = [
        "session_id", "session_start_utc", "session_duration_s",
        "markets_observed", "taker_evaluated", "taker_passed",
        "maker_fills", "mean_adverse_rate",
        "ws_disconnects", "rest_errors", "rate_limits",
        "bottleneck_label", "any_progress",
    ]
    write_header = not hist_path.exists()
    with open(hist_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=hist_fields)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "session_id":          session_id,
            "session_start_utc":   report["session_start_utc"],
            "session_duration_s":  session_duration_s,
            "markets_observed":    len(markets),
            "taker_evaluated":     report["taker_summary"]["total_evaluated"],
            "taker_passed":        report["taker_summary"].get("execution_candidates", 0),
            "maker_fills":         report["maker_summary"]["total_fills"],
            "mean_adverse_rate":   report["maker_summary"]["mean_adverse_rate"] or "",
            "ws_disconnects":      runtime_summary.get("ws_disconnects", 0),
            "rest_errors":         runtime_summary.get("rest_errors", 0),
            "rate_limits":         runtime_summary.get("rate_limits", 0),
            "bottleneck_label":    bottleneck_label,
            "any_progress":        any_progress,
        })

    return report


def _format_txt(report: Dict) -> List[str]:
    """Format the report as a human-readable text summary."""
    sep = "=" * 68
    thin = "-" * 68

    lines = [
        sep,
        f"  POLYMARKET BTC 5-MINUTE HARNESS – SESSION REPORT",
        f"  session_id : {report['session_id']}",
        f"  start      : {report['session_start_utc']}",
        f"  duration   : {report['session_duration_s']}s",
        f"  bankroll   : ${report['bankroll_usdc']:.2f} USDC  max_pos=${report['max_position_usdc']:.2f}",
        sep,
        "",
        "MARKETS OBSERVED",
        thin,
    ]

    if not report["markets"]:
        lines.append("  (none)")
    for m in report["markets"]:
        lines.append(
            f"  {m['market_id'][:14]:<14s}  status={m['status']:<8s}  "
            f"window={m['window_seconds']}s  {m['question'][:50]}"
        )

    lines += ["", "OPEN REFERENCE TRUTH", thin]
    for r in (report.get("open_ref_summary") or []):
        lag = r.get("open_reference_lag_s")
        lag_str = f"{lag:.1f}s lag" if lag is not None else "lag=?"
        true_flag = r.get("open_reference_is_true")
        lines.append(
            f"  {r['market_id']:<14s}  open_ref_is_true={true_flag}  {lag_str}"
            + (f"  snaps={r['snap_count']}" if "snap_count" in r else "")
            + (f"  [{r['note']}]" if r.get("note") else "")
        )
    if not report.get("open_ref_summary"):
        lines.append("  (no ref recorder data)")

    lines += ["", "TOKEN MAPPING", thin]
    for t in (report.get("token_mapping_summary") or []):
        conf = t.get("token_mapping_confidence", "unknown")
        labels = t.get("outcome_labels", [])
        lines.append(
            f"  {t['market_id']:<14s}  confidence={conf:<12s}  "
            f"labels={labels}  yes={t['yes_token_id']}  no={t['no_token_id']}"
        )
    if not report.get("token_mapping_summary"):
        lines.append("  (no markets)")

    lines += ["", "TAKER SHADOW", thin]
    ts = report["taker_summary"]
    lines.append(f"  Evaluated         : {ts['total_evaluated']}")
    lines.append(f"  signal_candidates : {ts.get('signal_candidates', '?')}")
    lines.append(f"  pricing_candidates: {ts.get('pricing_candidates', '?')}")
    lines.append(f"  execution_cands   : {ts.get('execution_candidates', '?')}")
    lines.append(f"  ev=True           : {ts.get('ev_true', '?')}")
    lines.append(f"  ev=Unknown        : {ts.get('ev_unknown', '?')}  (no external true_prob)")
    lines.append(f"  Settled W         : {ts['settled_wins']}")
    lines.append(f"  Settled L         : {ts['settled_losses']}")

    lines += ["", "MAKER SHADOW", thin]
    ms = report["maker_summary"]
    lines.append(f"  Quotes    : {ms['total_quotes']}")
    lines.append(f"  Fills     : {ms['total_fills']}")
    asr = ms.get("mean_adverse_rate")
    lines.append(f"  Adverse % : {f'{asr*100:.1f}%' if asr is not None else 'N/A'}")

    lines += ["", "SPREAD DISTRIBUTION (last 60/10/5 seconds)", thin]
    for mid, sides in (report.get("spread_distribution") or {}).items():
        lines.append(f"  market={mid[:12]}")
        for side, windows in (sides or {}).items():
            for window_label, stats in windows.items():
                if stats.get("n_snapshots", 0) == 0:
                    continue
                lines.append(
                    f"    {side} {window_label:8s}  n={stats['n_snapshots']:4d}  "
                    f"median={stats['median_pct']}%  p95={stats['p95_pct']}%"
                )

    lines += ["", "MIN ORDER SIZE FINDINGS", thin]
    for f in (report.get("min_order_size_findings") or []):
        fits = "OK" if f.get("fits_max_pos") else ("TOO LARGE" if f.get("fits_max_pos") is False else "?")
        src = f.get("min_order_size_source", "unknown")
        lines.append(
            f"  {f['market_id']:<14s}  min_size={f['min_order_size']} ({src})  "
            f"tick={f['tick_size']}  cost@0.60=${f['cost_usdc_at_0.60_ask']}  [{fits}]"
        )

    lines += ["", "RUNTIME FAILURES", thin]
    rt = report["runtime_summary"]
    lines.append(f"  WS disconnects    : {rt.get('ws_disconnects', 0)}")
    lines.append(f"  WS errors         : {rt.get('ws_errors', 0)}")
    lines.append(f"  Heartbeat gaps    : {rt.get('heartbeat_gaps', 0)}")
    lines.append(f"  Rate limits (425) : {rt.get('rate_limits', 0)}")
    lines.append(f"  REST errors       : {rt.get('rest_errors', 0)}")
    lines.append(f"  Order rejects     : {rt.get('order_rejects', 0)}")
    rtt = rt.get("rtt_stats", {})
    for ep, stats in rtt.items():
        lines.append(
            f"  RTT {ep:<20s}: mean={stats['mean_ms']:.0f}ms  p95={stats['p95_ms']:.0f}ms  max={stats['max_ms']:.0f}ms"
        )

    lines += ["", "BOTTLENECK", thin]
    bn = report["bottleneck"]
    lines.append(f"  [{bn['label']}]")
    lines.append(f"  {bn['explanation']}")

    lines += ["", "CANDIDACY ASSESSMENT", thin]
    ca = report["candidacy_assessment"]
    for finding in ca["findings"]:
        lines.append(f"  {finding}")

    lines += ["", sep]
    return lines


def print_report(report: Dict):
    """Print the report to stdout."""
    for line in _format_txt(report):
        print(line)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    """Load a saved report JSON and re-print it."""
    import sys
    if len(sys.argv) < 2:
        print("Usage: python verdict_report.py <session_id>")
        raise SystemExit(1)

    session_id = sys.argv[1]
    json_path = config.REPORTS_DIR / f"report_{session_id}.json"
    if not json_path.exists():
        print(f"Report not found: {json_path}")
        raise SystemExit(1)

    with open(json_path) as f:
        report = json.load(f)
    print_report(report)
