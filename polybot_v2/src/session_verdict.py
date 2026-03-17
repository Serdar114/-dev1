"""
Session verdict builder for polybot_v2 Phase 3.

Reads runtime artifacts (signals.jsonl, paper_trades.jsonl, shadow_quotes.jsonl,
bankroll.jsonl) from a session log directory and produces:
  - session_summary.json  -- structured machine-readable summary
  - session_summary.txt   -- human-readable text summary

The provisional verdict section is explicitly rules-based and conservative.
It does NOT claim edge unless runtime economics support it per config thresholds.
"""

from __future__ import annotations

import json
import statistics
import textwrap
from pathlib import Path
from typing import Any, Optional

from settings import Settings


# ------------------------------------------------------------------ #
# JSONL reader
# ------------------------------------------------------------------ #

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


# ------------------------------------------------------------------ #
# Taker summary
# ------------------------------------------------------------------ #

def _build_taker_summary(
    signals: list[dict],
    trades: list[dict],
    execution_enabled: bool = True,
) -> dict:
    taker_sigs = [r for r in signals if r.get("lane") == "selective_taker"]
    signal_count = len(taker_sigs)

    # Candidates = all PAPER_TRADE decisions logged to signals.jsonl.
    # These exist regardless of whether paper execution is enabled.
    # When execution is disabled, candidates are the only taker evidence.
    candidate_sigs = [r for r in taker_sigs if r.get("action") == "PAPER_TRADE"]
    no_trade_sigs = [r for r in taker_sigs if r.get("action") == "NO_TRADE"]
    no_trade_count = len(no_trade_sigs)

    # Candidate counts by side
    candidate_count = len(candidate_sigs)
    candidate_yes_count = sum(1 for r in candidate_sigs if r.get("chosen_side") == "yes")
    candidate_no_count = sum(1 for r in candidate_sigs if r.get("chosen_side") == "no")

    # Candidate edge  - from PAPER_TRADE signals directly
    cand_afe_list: list[float] = []
    for r in candidate_sigs:
        side = r.get("chosen_side")
        if side == "yes":
            v = r.get("after_fee_edge_yes")
        elif side == "no":
            v = r.get("after_fee_edge_no")
        else:
            v = None
        if v is not None:
            cand_afe_list.append(float(v))
    candidate_avg_after_fee_edge_pct = (
        round(statistics.mean(cand_afe_list) * 100.0, 4) if cand_afe_list else None
    )

    # Candidate avg seconds elapsed from window start
    cand_elapsed = [float(r.get("elapsed_from_window_start", 0)) for r in candidate_sigs]
    candidate_avg_entry_sec = round(statistics.mean(cand_elapsed), 1) if cand_elapsed else None

    # Candidate reason breakdown: counts by chosen_side
    candidate_reason_breakdown: dict[str, int] = {}
    for r in candidate_sigs:
        side = r.get("chosen_side") or "unknown"
        key = f"chosen_side:{side}"
        candidate_reason_breakdown[key] = candidate_reason_breakdown.get(key, 0) + 1

    # Execution mode tag
    execution_mode = "active" if execution_enabled else "candidate_only"

    # Trade counts from paper_trades.jsonl (actual executed trades)
    opened = [t for t in trades if t.get("event") == "open"]
    resolved = [t for t in trades if t.get("event") == "resolve"]
    trade_open_count = len(opened)
    trade_resolve_count = len(resolved)

    wins = [t for t in resolved if (t.get("pnl") or 0.0) > 0]
    losses = [t for t in resolved if (t.get("pnl") or 0.0) < 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = win_count / max(trade_resolve_count, 1) if trade_resolve_count > 0 else None

    pnl_list = [t.get("pnl", 0.0) for t in resolved]
    total_pnl = sum(pnl_list)
    avg_pnl = statistics.mean(pnl_list) if pnl_list else None
    median_pnl = statistics.median(pnl_list) if pnl_list else None
    # Expectancy = avg_pnl_per_resolved_trade (USDC)
    expectancy_usdc = avg_pnl

    # avg_entry_edge: prefer trade_thesis from paper_trades.jsonl, fall back to candidates
    thesis_sigs = [t for t in trades if t.get("event") == "trade_thesis"]
    afe_list: list[float] = []
    for t in thesis_sigs:
        side = t.get("side")
        if side == "yes":
            v = t.get("after_fee_edge_yes")
        elif side == "no":
            v = t.get("after_fee_edge_no")
        else:
            v = None
        if v is not None:
            afe_list.append(float(v))
    if not afe_list:
        afe_list = cand_afe_list[:]
    avg_entry_edge_pct = (
        round(statistics.mean(afe_list) * 100.0, 4) if afe_list else None
    )

    # avg_entry_elapsed_sec: from candidates (same data source, kept for compatibility)
    avg_entry_elapsed = candidate_avg_entry_sec

    # No-trade reason breakdown
    reason_counts: dict[str, int] = {}
    for r in no_trade_sigs:
        reason = r.get("reason") or "unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # Entry blocker breakdown  - canonical keys
    blocker_keys = [
        "position_policy:max_open_trades(1)",
        "outside_entry_window",
        "implied_prob_out_of_zone",
        "degenerate_book_reject",
        "delta_too_small",
        "wide_spread_reject",
        "negative_raw_edge",
    ]
    entry_blocker_breakdown = {}
    for k in blocker_keys:
        total = sum(v for rk, v in reason_counts.items() if k in rk or rk.startswith(k))
        if total > 0:
            entry_blocker_breakdown[k] = total
    matched = set()
    for k in blocker_keys:
        for rk in reason_counts:
            if k in rk or rk.startswith(k):
                matched.add(rk)
    other_blockers = {rk: rv for rk, rv in reason_counts.items() if rk not in matched}

    return {
        # Execution context
        "execution_mode": execution_mode,
        "signal_count": signal_count,
        "no_trade_count": no_trade_count,
        # Candidate fields  - from signals.jsonl; populated regardless of execution state.
        # When execution_mode="candidate_only", these are the primary taker evidence.
        "paper_trade_candidate_count": candidate_count,
        "paper_trade_candidate_yes_count": candidate_yes_count,
        "paper_trade_candidate_no_count": candidate_no_count,
        "candidate_reason_breakdown": candidate_reason_breakdown,
        "candidate_avg_after_fee_edge_pct": candidate_avg_after_fee_edge_pct,
        "candidate_avg_entry_second_into_window": candidate_avg_entry_sec,
        # Execution fields  - from paper_trades.jsonl; meaningful only when execution active.
        "trade_open_count": trade_open_count,
        "trade_resolve_count": trade_resolve_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "total_pnl_usdc": round(total_pnl, 5),
        "avg_pnl_per_resolved_trade_usdc": round(avg_pnl, 6) if avg_pnl is not None else None,
        "median_pnl_per_resolved_trade_usdc": round(median_pnl, 6) if median_pnl is not None else None,
        "expectancy_per_resolved_trade_usdc": round(expectancy_usdc, 6) if expectancy_usdc is not None else None,
        "avg_entry_after_fee_edge_pct": avg_entry_edge_pct,
        "avg_entry_elapsed_sec": avg_entry_elapsed,
        "no_trade_reason_breakdown": dict(sorted(reason_counts.items(), key=lambda x: -x[1])),
        "entry_blocker_breakdown": entry_blocker_breakdown,
        "entry_blocker_other": other_blockers,
    }


# ------------------------------------------------------------------ #
# Maker summary
# ------------------------------------------------------------------ #

def _build_maker_summary(quotes: list[dict]) -> dict:
    # Only count non-crossed shadow quote events (first appearance = "lifecycle" or no event)
    # Use quote_id uniqueness for unique_quote_count
    seen_ids: set = set()
    all_quotes = []
    for q in quotes:
        qid = q.get("quote_id") or ""
        if qid and qid not in seen_ids:
            seen_ids.add(qid)
            all_quotes.append(q)
        elif not qid:
            all_quotes.append(q)

    unique_quote_count = len(all_quotes)

    # Use latest record per quote_id to capture final fill_status
    latest: dict[str, dict] = {}
    for q in quotes:
        qid = q.get("quote_id") or ""
        if qid:
            latest[qid] = q
        else:
            # no quote_id; use the record as-is
            all_quotes.append(q)
    resolved_quotes = list(latest.values()) if latest else all_quotes

    # Reject breakdown (from crossed_rejected records that have reject_reason)
    reject_counts: dict[str, int] = {}
    for q in resolved_quotes:
        if q.get("fill_status") == "crossed_rejected":
            reason = q.get("reject_reason") or "unknown"
            reject_counts[reason] = reject_counts.get(reason, 0) + 1

    canonical_rejects = {
        "degenerate_book_reject": 0,
        "spread_too_wide": 0,
        "below_min_passive_edge": 0,
        "ste_out_of_range": 0,
        "other": 0,
    }
    for reason, cnt in reject_counts.items():
        if "degenerate_book" in reason:
            canonical_rejects["degenerate_book_reject"] += cnt
        elif "spread_too_wide" in reason:
            canonical_rejects["spread_too_wide"] += cnt
        elif "min_passive_edge" in reason or "passive_edge" in reason:
            canonical_rejects["below_min_passive_edge"] += cnt
        elif "ste_too_low" in reason or "ste_out_of_range" in reason:
            canonical_rejects["ste_out_of_range"] += cnt
        else:
            canonical_rejects["other"] += cnt

    # Status counts
    pending_qs = [q for q in resolved_quotes if q.get("fill_status") == "pending"]
    filled_qs = [q for q in resolved_quotes
                 if q.get("fill_status") in ("filled", "filled_adverse", "filled_favorable")]
    expired_qs = [q for q in resolved_quotes if q.get("fill_status") == "expired_unfilled"]
    adverse_qs = [q for q in resolved_quotes if q.get("fill_status") == "filled_adverse"]
    favorable_qs = [q for q in resolved_quotes if q.get("fill_status") == "filled_favorable"]
    boundary_resolved_qs = [q for q in resolved_quotes
                             if q.get("fill_status") == "boundary_resolved"]

    # Boundary outcomes from any resolved quote that has boundary_outcome_for_side set
    boundary_qs = [q for q in resolved_quotes if q.get("boundary_outcome_for_side") is not None]
    wins = [q for q in boundary_qs if float(q["boundary_outcome_for_side"]) >= 1.0]
    losses = [q for q in boundary_qs if float(q["boundary_outcome_for_side"]) <= 0.0]

    fill_count = len(filled_qs)
    expiry_count = len(expired_qs)
    adverse_fill_count = len(adverse_qs)
    favorable_fill_count = len(favorable_qs)
    resolved_filled_count = len(boundary_qs)
    boundary_win_count = len(wins)
    boundary_loss_count = len(losses)

    pending_placed = len([q for q in resolved_quotes if q.get("fill_status") != "crossed_rejected"])
    fill_rate = fill_count / max(pending_placed, 1) if pending_placed else None
    expiry_rate = expiry_count / max(pending_placed, 1) if pending_placed else None
    adverse_ratio = adverse_fill_count / max(fill_count, 1) if fill_count else None
    favorable_ratio = favorable_fill_count / max(fill_count, 1) if fill_count else None
    boundary_win_rate = (
        boundary_win_count / max(len(boundary_qs), 1)
        if boundary_qs else None
    )

    pnl_list = [
        float(q["maker_pnl_if_held"])
        for q in resolved_quotes
        if q.get("maker_pnl_if_held") is not None
    ]
    pnl_total = sum(pnl_list)
    pnl_mean = statistics.mean(pnl_list) if pnl_list else None
    pnl_median = statistics.median(pnl_list) if pnl_list else None

    # ---- Grouped breakdowns ----
    by_side = _group_breakdown(resolved_quotes, "side")
    by_regime = _group_breakdown(resolved_quotes, "regime")

    # STE bucket breakdown
    def _ste_bucket(ste: Optional[float]) -> str:
        if ste is None:
            return "unknown"
        if ste < 30:
            return "0-30s"
        if ste < 60:
            return "30-60s"
        if ste < 120:
            return "60-120s"
        if ste < 180:
            return "120-180s"
        return "180s+"

    by_ste: dict[str, dict] = {}
    for q in resolved_quotes:
        bucket = _ste_bucket(q.get("seconds_to_expiry"))
        if bucket not in by_ste:
            by_ste[bucket] = _empty_group()
        _update_group(by_ste[bucket], q)
    for g in by_ste.values():
        plist = g.pop("pnl_if_held_list", [])
        g["pnl_if_held_mean"] = round(statistics.mean(plist), 6) if plist else None

    # Passive edge bucket breakdown
    def _edge_bucket(edge: Optional[float]) -> str:
        if edge is None:
            return "unknown"
        if edge < 0.02:
            return "<0.02"
        if edge < 0.05:
            return "0.02-0.05"
        if edge < 0.10:
            return "0.05-0.10"
        return ">=0.10"

    by_passive_edge: dict[str, dict] = {}
    for q in resolved_quotes:
        bucket = _edge_bucket(q.get("intended_passive_edge"))
        if bucket not in by_passive_edge:
            by_passive_edge[bucket] = _empty_group()
        _update_group(by_passive_edge[bucket], q)
    for g in by_passive_edge.values():
        plist = g.pop("pnl_if_held_list", [])
        g["pnl_if_held_mean"] = round(statistics.mean(plist), 6) if plist else None

    return {
        "unique_quote_count": unique_quote_count,
        "fill_count": fill_count,
        "expiry_count": expiry_count,
        "fill_rate": round(fill_rate, 4) if fill_rate is not None else None,
        "expiry_rate": round(expiry_rate, 4) if expiry_rate is not None else None,
        "adverse_fill_count": adverse_fill_count,
        "favorable_fill_count": favorable_fill_count,
        "adverse_fill_ratio": round(adverse_ratio, 4) if adverse_ratio is not None else None,
        "favorable_fill_ratio": round(favorable_ratio, 4) if favorable_ratio is not None else None,
        "resolved_filled_count": resolved_filled_count,
        "boundary_win_count": boundary_win_count,
        "boundary_loss_count": boundary_loss_count,
        "boundary_win_rate": round(boundary_win_rate, 4) if boundary_win_rate is not None else None,
        "maker_pnl_if_held_total": round(pnl_total, 6),
        "maker_pnl_if_held_mean": round(pnl_mean, 6) if pnl_mean is not None else None,
        "maker_pnl_if_held_median": round(pnl_median, 6) if pnl_median is not None else None,
        "reject_breakdown": canonical_rejects,
        "reject_breakdown_raw": dict(sorted(reject_counts.items(), key=lambda x: -x[1])),
        "by_side": by_side,
        "by_regime": by_regime,
        "by_ste_bucket": by_ste,
        "by_passive_edge_bucket": by_passive_edge,
    }


def _empty_group() -> dict:
    return {
        "count": 0,
        "fill_count": 0,
        "boundary_win_count": 0,
        "boundary_loss_count": 0,
        "pnl_if_held_total": 0.0,
        "pnl_if_held_list": [],
    }


def _update_group(g: dict, q: dict) -> None:
    g["count"] += 1
    status = q.get("fill_status", "")
    if status in ("filled", "filled_adverse", "filled_favorable"):
        g["fill_count"] += 1
    outcome = q.get("boundary_outcome_for_side")
    if outcome is not None:
        if outcome >= 1.0:
            g["boundary_win_count"] += 1
        elif outcome <= 0.0:
            g["boundary_loss_count"] += 1
    pnl = q.get("maker_pnl_if_held")
    if pnl is not None:
        g["pnl_if_held_list"].append(float(pnl))
        g["pnl_if_held_total"] = round(g["pnl_if_held_total"] + float(pnl), 6)


def _group_breakdown(quotes: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for q in quotes:
        k = q.get(key) or "UNKNOWN"
        if k not in groups:
            groups[k] = _empty_group()
        _update_group(groups[k], q)
    # Finalize: compute mean pnl, remove raw list
    for g in groups.values():
        plist = g.pop("pnl_if_held_list", [])
        g["pnl_if_held_mean"] = round(statistics.mean(plist), 6) if plist else None
    return groups


# ------------------------------------------------------------------ #
# Regime / sample bias assessment
# ------------------------------------------------------------------ #

def _build_regime_bias_note(taker: dict, maker: dict) -> dict:
    """
    Assess whether observed side outperformance is likely structural or
    a sample/regime artifact.

    This is NOT a trading signal. It is a diagnostic that flags when the
    current sample appears directionally biased (e.g., BTC was bullish during
    the observation window), which would make YES-side outperformance unreliable
    as a generalizable conclusion.
    """
    notes: list[str] = []

    # 1. Taker candidate side distribution
    cand_yes = taker.get("paper_trade_candidate_yes_count", 0)
    cand_no = taker.get("paper_trade_candidate_no_count", 0)
    cand_total = cand_yes + cand_no
    signal_side_bias: Optional[str] = None
    yes_signal_frac: Optional[float] = None
    if cand_total > 0:
        yes_signal_frac = cand_yes / cand_total
        if yes_signal_frac > 0.65:
            signal_side_bias = f"YES-biased ({yes_signal_frac:.0%} of {cand_total} candidates)"
            notes.append(
                f"Signal distribution is YES-biased: {cand_yes}/{cand_total} "
                f"candidates chose YES  - possible bullish BTC regime during sample"
            )
        elif yes_signal_frac < 0.35:
            signal_side_bias = f"NO-biased ({1 - yes_signal_frac:.0%} of {cand_total} candidates)"
            notes.append(
                f"Signal distribution is NO-biased: {cand_no}/{cand_total} "
                f"candidates chose NO  - possible bearish BTC regime during sample"
            )
        else:
            signal_side_bias = f"balanced ({yes_signal_frac:.0%} YES / {1-yes_signal_frac:.0%} NO)"

    # 2. Maker fill side distribution
    by_side = maker.get("by_side", {})
    yes_fills = by_side.get("yes", {}).get("fill_count", 0)
    no_fills = by_side.get("no", {}).get("fill_count", 0)
    total_fills = yes_fills + no_fills
    fill_side_bias: Optional[str] = None
    yes_fill_frac: Optional[float] = None
    if total_fills > 0:
        yes_fill_frac = yes_fills / total_fills
        if yes_fill_frac > 0.65:
            fill_side_bias = f"YES-biased ({yes_fill_frac:.0%} of fills)"
            notes.append(
                f"Maker fill distribution is YES-biased: {yes_fills}/{total_fills} fills on YES side"
            )
        elif yes_fill_frac < 0.35:
            fill_side_bias = f"NO-biased ({1 - yes_fill_frac:.0%} of fills)"
            notes.append(
                f"Maker fill distribution is NO-biased: {no_fills}/{total_fills} fills on NO side"
            )
        else:
            fill_side_bias = f"balanced ({yes_fill_frac:.0%} YES)"

    # 3. YES vs NO pnl comparison and structural assessment
    yes_data = by_side.get("yes", {})
    no_data_m = by_side.get("no", {})
    yes_pnl = yes_data.get("pnl_if_held_mean")
    no_pnl = no_data_m.get("pnl_if_held_mean")
    side_asymmetry_assessment: Optional[str] = None

    if yes_pnl is not None and no_pnl is not None:
        if yes_pnl > no_pnl:
            if yes_fill_frac is not None and yes_fill_frac > 0.65:
                side_asymmetry_assessment = "likely_regime_artifact"
                notes.append(
                    f"YES maker pnl_mean={yes_pnl:+.4f} > NO pnl_mean={no_pnl:+.4f}, "
                    "AND fills are YES-biased  - side asymmetry is likely a sample/regime "
                    "artifact. Do NOT interpret as structural YES edge."
                )
            else:
                side_asymmetry_assessment = "possible_structural_requires_more_data"
                notes.append(
                    f"YES maker pnl_mean={yes_pnl:+.4f} > NO pnl_mean={no_pnl:+.4f} "
                    "with balanced fill distribution  - may have structural component; "
                    "requires balanced-regime sample to confirm."
                )
        elif no_pnl > yes_pnl:
            side_asymmetry_assessment = "no_side_appears_better_in_sample"
            notes.append(
                f"NO maker pnl_mean={no_pnl:+.4f} > YES pnl_mean={yes_pnl:+.4f} in this sample."
            )
        else:
            side_asymmetry_assessment = "sides_roughly_equal"

    # 4. Double-bias flag: both signal AND fill distribution biased same direction
    both_biased_yes = (
        yes_signal_frac is not None and yes_signal_frac > 0.65
        and yes_fill_frac is not None and yes_fill_frac > 0.65
    )
    both_biased_no = (
        yes_signal_frac is not None and yes_signal_frac < 0.35
        and yes_fill_frac is not None and yes_fill_frac < 0.35
    )
    if both_biased_yes:
        notes.append(
            "CRITICAL BIAS FLAG: Both signal generation AND maker fills are YES-biased. "
            "Current BTC regime appears bullish/YES-favoring. "
            "Observed YES outperformance MUST NOT be interpreted as structural trading edge "
            "until confirmed with a balanced or NO-biased regime sample."
        )
    if both_biased_no:
        notes.append(
            "CRITICAL BIAS FLAG: Both signal generation AND maker fills are NO-biased. "
            "Current BTC regime appears bearish/NO-favoring. "
            "Observed NO outperformance MUST NOT be interpreted as structural trading edge "
            "until confirmed with a balanced or YES-biased regime sample."
        )

    summary = (
        " | ".join(notes) if notes
        else "No strong directional bias detected in current sample."
    )

    return {
        "yes_signal_fraction": round(yes_signal_frac, 4) if yes_signal_frac is not None else None,
        "signal_side_bias": signal_side_bias,
        "yes_fill_fraction": round(yes_fill_frac, 4) if yes_fill_frac is not None else None,
        "fill_side_bias": fill_side_bias,
        "side_asymmetry_assessment": side_asymmetry_assessment,
        "both_signal_and_fill_biased_yes": both_biased_yes,
        "both_signal_and_fill_biased_no": both_biased_no,
        "notes": notes,
        "summary": summary,
    }


# ------------------------------------------------------------------ #
# Provisional verdict
# ------------------------------------------------------------------ #

def _build_verdict(
    taker: dict,
    maker: dict,
    cfg: Settings,
) -> dict:
    """
    Conservative rules-based verdict. Explicitly provisional.
    Does NOT claim live-readiness unless hard thresholds are met.
    """
    reasons: list[str] = []

    # --- Taker status ---
    execution_mode = taker.get("execution_mode", "active")
    exp = taker.get("expectancy_per_resolved_trade_usdc")
    n_resolved = taker.get("trade_resolve_count", 0)
    candidate_count = taker.get("paper_trade_candidate_count", 0)
    cand_yes = taker.get("paper_trade_candidate_yes_count", 0)
    cand_no = taker.get("paper_trade_candidate_no_count", 0)

    if execution_mode == "candidate_only":
        if candidate_count == 0:
            taker_status = "no_data"
            reasons.append("taker: execution disabled; zero candidates generated")
        else:
            taker_status = "candidate_only"
            reasons.append(
                f"taker: execution disabled; {candidate_count} candidates generated "
                f"(yes={cand_yes}, no={cand_no}); "
                "no economic verdict possible without execution  - "
                "candidate data shows signal engine activity only"
            )
    elif n_resolved == 0:
        taker_status = "no_data"
        reasons.append("taker: zero resolved trades")
    elif exp is None or exp <= cfg.verdict_taker_min_expectancy_usdc:
        taker_status = "baseline_only"
        reasons.append(
            f"taker: expectancy={exp} <= threshold={cfg.verdict_taker_min_expectancy_usdc} USDC"
        )
    else:
        taker_status = "baseline_positive_expectancy"
        reasons.append(
            f"taker: expectancy={exp:.5f} USDC > threshold={cfg.verdict_taker_min_expectancy_usdc}"
        )

    # --- Maker status ---
    resolved_fills = maker.get("resolved_filled_count", 0)
    bwr = maker.get("boundary_win_rate")
    mean_pnl = maker.get("maker_pnl_if_held_mean")
    adverse_ratio = maker.get("adverse_fill_ratio")

    min_fills = cfg.verdict_maker_min_resolved_fills
    min_bwr = cfg.verdict_maker_min_boundary_win_rate
    min_pnl = cfg.verdict_maker_min_mean_pnl_if_held
    max_adv = cfg.verdict_maker_max_adverse_fill_ratio

    if resolved_fills == 0:
        maker_status = "no_fill_data"
        reasons.append("maker: zero resolved filled quotes")
    elif resolved_fills < min_fills:
        maker_status = "insufficient_sample"
        reasons.append(
            f"maker: resolved_fills={resolved_fills} < min_required={min_fills}"
        )
    elif bwr is None or bwr < min_bwr:
        maker_status = "evaluation_only"
        reasons.append(
            f"maker: boundary_win_rate={bwr} < threshold={min_bwr}"
        )
    elif mean_pnl is None or mean_pnl < min_pnl:
        maker_status = "evaluation_only"
        reasons.append(
            f"maker: mean_pnl_if_held={mean_pnl} < threshold={min_pnl}"
        )
    elif adverse_ratio is not None and adverse_ratio > max_adv:
        maker_status = "evaluation_only"
        reasons.append(
            f"maker: adverse_fill_ratio={adverse_ratio:.3f} > max={max_adv}"
        )
    else:
        maker_status = "conditionally_researchable"
        reasons.append(
            f"maker: win_rate={bwr:.3f} mean_pnl={mean_pnl:.4f} adverse={adverse_ratio:.3f}  - thresholds met"
        )

    # --- Live candidate ---
    live_min_fills = cfg.verdict_live_candidate_min_maker_fills
    if (
        taker_status in ("baseline_positive_expectancy",)
        and maker_status == "conditionally_researchable"
        and resolved_fills >= live_min_fills
    ):
        live_candidate_status = "not_live_ready_but_researchable"
        reasons.append(
            f"live: both lanes positive but not_live_ready until manual review"
        )
    else:
        live_candidate_status = "not_live_ready"

    # --- Strongest false hope / real opportunity ---
    strongest_false_hope: Optional[str] = None
    strongest_real_opportunity: Optional[str] = None

    fill_count = maker.get("fill_count", 0)
    bwc = maker.get("boundary_win_count", 0)

    if fill_count > 0 and (bwr is None or bwr < 0.5):
        strongest_false_hope = (
            "maker fills are occurring but boundary_win_rate < 0.5  - "
            "adverse selection dominates any fill activity"
        )
    if mean_pnl is not None and mean_pnl < 0:
        strongest_false_hope = (
            "maker shows fills but mean_pnl_if_held is negative  - "
            "quoting at these prices destroys value on average"
        )

    if fill_count > 0 and adverse_ratio is not None and adverse_ratio < max_adv:
        strongest_real_opportunity = (
            f"maker adverse_fill_ratio={adverse_ratio:.3f} is within acceptable range  - "
            "structure allows further evaluation with more data"
        )
    if (
        resolved_fills >= min_fills
        and bwr is not None
        and bwr >= min_bwr
    ):
        strongest_real_opportunity = (
            f"maker has {resolved_fills} resolved fills with "
            f"boundary_win_rate={bwr:.3f}  - warrants controlled expansion of evaluation sample"
        )

    return {
        "_note": "PROVISIONAL. Rules-based. Not a trading recommendation.",
        "taker_status": taker_status,
        "maker_status": maker_status,
        "live_candidate_status": live_candidate_status,
        "strongest_false_hope": strongest_false_hope,
        "strongest_real_opportunity": strongest_real_opportunity,
        "verdict_basis": reasons,
    }


# ------------------------------------------------------------------ #
# Text summary renderer
# ------------------------------------------------------------------ #

def _render_txt(
    taker: dict,
    maker: dict,
    verdict: dict,
    session_ts: str,
    regime_bias: Optional[dict] = None,
) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append(f"SESSION SUMMARY  [{session_ts}]")
    lines.append("=" * 70)

    exec_mode = taker.get("execution_mode", "active")
    lines.append("\n-- TAKER (benchmark lane) --")
    lines.append(f"  Execution mode       : {exec_mode}")
    lines.append(f"  Signals evaluated    : {taker['signal_count']}")
    lines.append(f"  No-trade decisions   : {taker['no_trade_count']}")

    # Candidate block  - always shown; primary taker evidence when execution disabled
    cand_count = taker.get("paper_trade_candidate_count", 0)
    cand_yes = taker.get("paper_trade_candidate_yes_count", 0)
    cand_no = taker.get("paper_trade_candidate_no_count", 0)
    cand_edge = taker.get("candidate_avg_after_fee_edge_pct")
    cand_elapsed = taker.get("candidate_avg_entry_second_into_window")
    lines.append(f"  Candidates generated : {cand_count}  (yes={cand_yes}, no={cand_no})")
    lines.append(
        f"  Candidate avg edge   : {cand_edge:.4f}%" if cand_edge is not None
        else "  Candidate avg edge   : n/a"
    )
    lines.append(
        f"  Candidate avg elapsed: {cand_elapsed:.1f}s" if cand_elapsed is not None
        else "  Candidate avg elapsed: n/a"
    )
    if exec_mode == "candidate_only":
        lines.append(
            "  NOTE: Execution disabled. Candidates are signal evidence only."
        )
        lines.append(
            "        No economic verdict possible from taker until execution is re-enabled."
        )

    # Executed trade block  - meaningful only when execution active
    lines.append(f"  Trades opened        : {taker['trade_open_count']}")
    lines.append(f"  Trades resolved      : {taker['trade_resolve_count']}")
    lines.append(f"  Win / Loss           : {taker['win_count']} / {taker['loss_count']}")
    wr = taker.get("win_rate")
    lines.append(f"  Win rate             : {wr:.4f}" if wr is not None else "  Win rate             : n/a")
    lines.append(f"  Total PnL (USDC)     : {taker['total_pnl_usdc']:.5f}")
    exp = taker.get("expectancy_per_resolved_trade_usdc")
    lines.append(f"  Expectancy/trade     : {exp:.6f} USDC" if exp is not None else "  Expectancy/trade     : n/a")
    edge = taker.get("avg_entry_after_fee_edge_pct")
    lines.append(f"  Avg entry edge       : {edge:.4f}%" if edge is not None else "  Avg entry edge       : n/a")
    elapsed = taker.get("avg_entry_elapsed_sec")
    lines.append(f"  Avg entry elapsed    : {elapsed:.1f}s" if elapsed is not None else "  Avg entry elapsed    : n/a")

    top_blockers = list(taker.get("entry_blocker_breakdown", {}).items())[:5]
    if top_blockers:
        lines.append("  Top entry blockers:")
        for k, v in top_blockers:
            lines.append(f"    {k}: {v}")

    lines.append("\n-- MAKER (primary evaluation lane) --")
    lines.append(f"  Unique quotes placed : {maker['unique_quote_count']}")
    lines.append(f"  Fills                : {maker['fill_count']}")
    lines.append(f"  Expiries             : {maker['expiry_count']}")
    fr = maker.get("fill_rate")
    lines.append(f"  Fill rate            : {fr:.4f}" if fr is not None else "  Fill rate            : n/a")
    lines.append(f"  Adverse fills        : {maker['adverse_fill_count']}")
    lines.append(f"  Favorable fills      : {maker['favorable_fill_count']}")
    af = maker.get("adverse_fill_ratio")
    lines.append(f"  Adverse fill ratio   : {af:.4f}" if af is not None else "  Adverse fill ratio   : n/a")
    lines.append(f"  Resolved filled      : {maker['resolved_filled_count']}")
    lines.append(f"  Boundary wins        : {maker['boundary_win_count']}")
    lines.append(f"  Boundary losses      : {maker['boundary_loss_count']}")
    bwr = maker.get("boundary_win_rate")
    lines.append(f"  Boundary win rate    : {bwr:.4f}" if bwr is not None else "  Boundary win rate    : n/a")
    lines.append(f"  PnL-if-held total    : {maker['maker_pnl_if_held_total']:.6f}")
    mp = maker.get("maker_pnl_if_held_mean")
    lines.append(f"  PnL-if-held mean     : {mp:.6f}" if mp is not None else "  PnL-if-held mean     : n/a")
    mpmed = maker.get("maker_pnl_if_held_median")
    lines.append(f"  PnL-if-held median   : {mpmed:.6f}" if mpmed is not None else "  PnL-if-held median   : n/a")

    rejects = maker.get("reject_breakdown", {})
    if any(v > 0 for v in rejects.values()):
        lines.append("  Guard rejections:")
        for k, v in rejects.items():
            if v > 0:
                lines.append(f"    {k}: {v}")

    # Maker strategic interpretation
    lines.append("\n-- MAKER STRATEGIC INTERPRETATION --")
    by_side = maker.get("by_side", {})
    yes_m = by_side.get("yes", {})
    no_m = by_side.get("no", {})
    yes_pnl = yes_m.get("pnl_if_held_mean")
    no_pnl = no_m.get("pnl_if_held_mean")
    yes_bwins = yes_m.get("boundary_win_count", 0)
    no_bwins = no_m.get("boundary_win_count", 0)

    if yes_pnl is not None or no_pnl is not None:
        lines.append(
            f"  YES-side: fills={yes_m.get('fill_count',0)}  "
            f"bwins={yes_bwins}  "
            f"pnl_mean={yes_pnl:+.4f}" if yes_pnl is not None else
            f"  YES-side: fills={yes_m.get('fill_count',0)}  bwins={yes_bwins}  pnl_mean=n/a"
        )
        lines.append(
            f"  NO-side : fills={no_m.get('fill_count',0)}  "
            f"bwins={no_bwins}  "
            f"pnl_mean={no_pnl:+.4f}" if no_pnl is not None else
            f"  NO-side : fills={no_m.get('fill_count',0)}  bwins={no_bwins}  pnl_mean=n/a"
        )
        if yes_pnl is not None and no_pnl is not None:
            if yes_pnl > no_pnl:
                lines.append(
                    "  -> In this sample YES-side appears better than NO-side."
                )
            elif no_pnl > yes_pnl:
                lines.append(
                    "  -> In this sample NO-side appears better than YES-side."
                )
            else:
                lines.append("  -> YES/NO sides roughly equal in this sample.")
    else:
        lines.append("  YES/NO side breakdown: insufficient fill data")

    # Passive edge bucket ranking
    by_edge = maker.get("by_passive_edge_bucket", {})
    if by_edge:
        ranked = sorted(
            by_edge.items(),
            key=lambda x: x[1].get("pnl_if_held_mean") if x[1].get("pnl_if_held_mean") is not None else float("-inf"),
            reverse=True,
        )
        lines.append("  Passive edge buckets (ranked by pnl_mean):")
        for bucket, g in ranked:
            pm = g.get("pnl_if_held_mean")
            fc = g.get("fill_count", 0)
            pm_str = f"{pm:+.4f}" if pm is not None else "n/a"
            lines.append(f"    {bucket:12s}: pnl_mean={pm_str}  fills={fc}")

    lines.append(
        "  CAUTION: Side/edge comparisons above may reflect sample regime, not structural edge."
    )
    lines.append(
        "           See REGIME BIAS ASSESSMENT below before drawing strategic conclusions."
    )

    # Regime bias assessment
    if regime_bias:
        lines.append("\n-- REGIME / SAMPLE BIAS ASSESSMENT --")
        lines.append(f"  Signal side bias     : {regime_bias.get('signal_side_bias', 'n/a')}")
        lines.append(f"  Fill side bias       : {regime_bias.get('fill_side_bias', 'n/a')}")
        lines.append(f"  Side asymmetry       : {regime_bias.get('side_asymmetry_assessment', 'n/a')}")
        if regime_bias.get("both_signal_and_fill_biased_yes") or regime_bias.get("both_signal_and_fill_biased_no"):
            lines.append("  *** DOUBLE BIAS FLAG: Both signal and fill distributions are directionally biased ***")
        bias_notes = regime_bias.get("notes", [])
        if bias_notes:
            lines.append("  Findings:")
            for note in bias_notes:
                for ln in textwrap.wrap(note, width=64):
                    lines.append(f"    {ln}")

    lines.append("\n-- PROVISIONAL VERDICT --")
    lines.append(f"  NOTE: {verdict['_note']}")
    lines.append(f"  Taker status         : {verdict['taker_status']}")
    lines.append(f"  Maker status         : {verdict['maker_status']}")
    lines.append(f"  Live candidate       : {verdict['live_candidate_status']}")
    fh = verdict.get("strongest_false_hope")
    if fh:
        lines.append(f"  False hope warning   : {fh}")
    ro = verdict.get("strongest_real_opportunity")
    if ro:
        lines.append(f"  Real opportunity     : {ro}")
    lines.append("  Basis:")
    for b in verdict.get("verdict_basis", []):
        for ln in textwrap.wrap(b, width=64):
            lines.append(f"    {ln}")

    lines.append("\n" + "=" * 70)
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Public entry point
# ------------------------------------------------------------------ #

def build_session_summary(
    log_dir: Path,
    cfg: Settings,
    session_ts: str = "",
) -> dict:
    """
    Read artifacts from log_dir, build summary dict, write JSON + TXT files.
    Returns the summary dict.
    """
    signals = _read_jsonl(log_dir / "signals.jsonl")
    trades = _read_jsonl(log_dir / "paper_trades.jsonl")
    quotes = _read_jsonl(log_dir / "shadow_quotes.jsonl")
    bankroll_events = _read_jsonl(log_dir / "bankroll.jsonl")

    execution_enabled = cfg.taker_paper_execution_enabled
    taker = _build_taker_summary(signals, trades, execution_enabled=execution_enabled)
    maker = _build_maker_summary(quotes)
    regime_bias = _build_regime_bias_note(taker, maker)
    verdict = _build_verdict(taker, maker, cfg)

    # Session duration from bankroll events if available
    boundary_events = [e for e in bankroll_events if e.get("event") == "window_boundary"]
    windows_completed = len(boundary_events)

    summary = {
        "session_ts": session_ts,
        "windows_completed": windows_completed,
        "taker": taker,
        "maker": maker,
        "regime_bias": regime_bias,
        "verdict": verdict,
    }

    # Write JSON
    json_path = log_dir / "session_summary.json"
    with open(json_path, "w") as fh:
        json.dump(summary, fh, indent=2, default=str)

    # Write TXT  - explicit utf-8 encoding for Windows cp125x compatibility
    txt_path = log_dir / "session_summary.txt"
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(_render_txt(taker, maker, verdict, session_ts, regime_bias=regime_bias))

    return summary
