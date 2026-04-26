#!/usr/bin/env python3
"""
Analyze signals and observations from JSONL logs.

Answers the V1 success criteria questions:
  1. How many active daily temperature markets found?
  2. Parse success rate + target date coverage
  3. Source type distribution
  4. Paper/live eligible counts
  5. Ensemble usability (n_members, blocked reasons)
  6. Which cities appeared most?
  7. Which markets had usable orderbook depth?
  8. Which buckets showed model-vs-market edge?
  9. Which signals survived filters?
  10. Which cities had best settlement safety?
  11. Which signal type dominated?
  12. Ghost trade lifecycle counts
  13. Top reject reasons
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weatherbot.ghost_logger import (
    GHOST_TRADES_FILE,
    OBSERVATIONS_FILE,
    SIGNALS_FILE,
    GHOST_STATUS_FILLED,
    GHOST_STATUS_ORDER_PLACED,
    GHOST_STATUS_EXPIRED,
    GHOST_STATUS_CANCELLED,
    read_jsonl,
)


def _pct(n, total):
    if not total:
        return "0.0%"
    return f"{100*n/total:.1f}%"


def analyze():
    observations = read_jsonl(OBSERVATIONS_FILE)
    signals = read_jsonl(SIGNALS_FILE)
    ghost_trades = read_jsonl(GHOST_TRADES_FILE)

    print(f"\n{'='*60}")
    print("POLYMARKET WEATHER SCANNER — SIGNAL ANALYSIS")
    print(f"{'='*60}")
    print(f"Observations logged:  {len(observations)}")
    print(f"Signals logged:       {len(signals)}")
    print(f"Ghost trades logged:  {len(ghost_trades)}")

    if not observations:
        print("\nNo observations yet. Run: python tools/run_scan.py --once")
        return

    total = len(observations)

    # Q1: Market types
    types = Counter(o.get("market_type", "unknown") for o in observations)
    print(f"\n--- Market Types ---")
    for t, c in types.most_common():
        print(f"  {t}: {c}")

    # Q2: Parse success + target date coverage
    parse_ok = sum(1 for o in observations if "parse_failed" not in str(o.get("reject_reason", "")))
    parse_fail = total - parse_ok
    has_target_date = sum(1 for o in observations if o.get("target_date"))
    missing_target_date = total - has_target_date
    print(f"\n--- Parse Results ---")
    print(f"  Parsed OK:              {parse_ok} / {total} ({_pct(parse_ok, total)})")
    print(f"  Parse failed:           {parse_fail} ({_pct(parse_fail, total)})")
    print(f"  Has target date:        {has_target_date} / {total} ({_pct(has_target_date, total)})")
    print(f"  Missing target date:    {missing_target_date} ({_pct(missing_target_date, total)})")

    # Q3: Source type distribution
    source_types = Counter(o.get("source_type", "Unknown") for o in observations)
    print(f"\n--- Source Type Distribution ---")
    for st, c in source_types.most_common():
        print(f"  {st}: {c} ({_pct(c, total)})")

    # Q4: Paper/live eligible counts
    paper_elig = sum(1 for o in observations if o.get("paper_eligible", False))
    live_elig = sum(1 for o in observations if o.get("live_eligible", False))
    hard_bl = sum(1 for o in observations if o.get("hard_blacklist", False))
    print(f"\n--- Eligibility ---")
    print(f"  Hard blacklist:         {hard_bl} ({_pct(hard_bl, total)})")
    print(f"  Paper eligible:         {paper_elig} ({_pct(paper_elig, total)})")
    print(f"  Live eligible:          {live_elig} ({_pct(live_elig, total)})")

    # Q4b: Event coverage
    event_ids = set(o.get("event_id") for o in observations if o.get("event_id"))
    print(f"\n--- Event Coverage ---")
    print(f"  Unique events tracked:  {len(event_ids)}")

    # Q5: Ensemble usability
    ensemble_usable = sum(1 for o in observations if (o.get("n_members") or 0) > 0 and not o.get("deterministic_fallback_used"))
    n_members_list = [o.get("n_members") for o in observations if o.get("n_members") is not None]
    blocked_reasons = Counter(o.get("forecast_blocked_reason") for o in observations if o.get("forecast_blocked_reason"))
    outside_range = sum(1 for o in observations if o.get("model_distribution_outside_bucket_range"))
    print(f"\n--- Ensemble Validation ---")
    print(f"  Ensemble usable (n>0, no fallback): {ensemble_usable} / {total} ({_pct(ensemble_usable, total)})")
    if n_members_list:
        avg_members = sum(n_members_list) / len(n_members_list)
        print(f"  Avg n_members (where set): {avg_members:.1f}")
        zero_members = sum(1 for n in n_members_list if n == 0)
        print(f"  n_members=0:               {zero_members}")
    print(f"  Model dist outside bucket: {outside_range} ({_pct(outside_range, total)})")
    if blocked_reasons:
        print(f"  Blocked reasons:")
        for reason, c in blocked_reasons.most_common():
            print(f"    {reason}: {c}")

    # Q6: Top cities
    cities = Counter(o.get("city") for o in observations if o.get("city"))
    print(f"\n--- Top Cities (top 15) ---")
    for city, count in cities.most_common(15):
        print(f"  {city}: {count}")

    # Q7: Orderbook depth — use side-specific fields
    with_book = [o for o in observations if o.get("best_ask") is not None]
    ask_deep = [o for o in with_book if (o.get("ask_depth_top_n") or o.get("top_book_depth", 0) / 2) >= 4.0]
    bid_deep = [o for o in with_book if (o.get("bid_depth_top_n") or o.get("top_book_depth", 0) / 2) >= 4.0]
    print(f"\n--- Orderbook Coverage ---")
    print(f"  Has orderbook:          {len(with_book)} / {total}")
    print(f"  Ask depth >= 4 USDC:    {len(ask_deep)}")
    print(f"  Bid depth >= 4 USDC:    {len(bid_deep)}")

    book_states = Counter(o.get("book_state", o.get("display_price_mode", "unknown")) for o in observations)
    for state, c in book_states.most_common():
        print(f"  book_state={state}: {c}")

    # Q8: Edge distribution
    obs_with_edge = [o for o in observations if o.get("edge_net_maker") is not None]
    if obs_with_edge:
        pos_maker = [o for o in obs_with_edge if (o.get("edge_net_maker") or 0) >= 0.08]
        pos_taker = [o for o in obs_with_edge if (o.get("edge_net_taker") or 0) >= 0.12]
        avg_edge = sum(o.get("edge_net_maker", 0) for o in obs_with_edge) / len(obs_with_edge)
        print(f"\n--- Edge Distribution ---")
        print(f"  Obs with edge data:     {len(obs_with_edge)}")
        print(f"  Maker edge >= 0.08:     {len(pos_maker)}")
        print(f"  Taker edge >= 0.12:     {len(pos_taker)}")
        print(f"  Avg net maker edge:     {avg_edge:.4f}")

        top_edge = sorted(obs_with_edge, key=lambda x: x.get("edge_net_maker", 0), reverse=True)[:5]
        print(f"  Top 5 maker edge markets:")
        for o in top_edge:
            print(f"    {o.get('city','?')} | {o.get('bucket_label','?')} | "
                  f"model={o.get('model_probability',0):.2f} "
                  f"ask={o.get('best_ask','?')} "
                  f"edge_net_maker={o.get('edge_net_maker',0):.4f}")

    # Q8b: Top model bucket vs best ask (sorted by model_probability, descending)
    top_model_obs = sorted(
        [o for o in observations if o.get("model_probability") is not None and o.get("best_ask") is not None],
        key=lambda x: x.get("model_probability", 0),
        reverse=True,
    )[:10]
    if top_model_obs:
        print(f"\n--- Top 10 by Model Probability (with orderbook) ---")
        for o in top_model_obs:
            outside = " [OUTSIDE_RANGE]" if o.get("model_distribution_outside_bucket_range") else ""
            print(f"  {o.get('city','?')} | {o.get('bucket_label','?')} | "
                  f"model={o.get('model_probability',0):.3f} "
                  f"ask={o.get('best_ask','?'):.3f} "
                  f"n={o.get('n_members',0)}{outside}")

    # Q9: Surviving signals
    print(f"\n--- Surviving Signals ---")
    action_counts = Counter(s.get("action", "?") for s in signals)
    for action, count in action_counts.most_common():
        print(f"  {action}: {count}")

    # Q10: Settlement safety by city
    print(f"\n--- Settlement Safety by City (top 10) ---")
    city_safety: dict[str, list[float]] = defaultdict(list)
    for o in observations:
        city = o.get("city")
        score = o.get("settlement_safety_score")
        if city and score is not None:
            city_safety[city].append(score)

    city_avg = {city: sum(scores)/len(scores) for city, scores in city_safety.items()}
    for city, avg in sorted(city_avg.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {city}: {avg:.3f} (n={len(city_safety[city])})")

    # Q11: Signal types
    if signals:
        sig_types = Counter(s.get("signal_type", "?") for s in signals)
        print(f"\n--- Signal Types ---")
        for st, count in sig_types.most_common():
            print(f"  {st}: {count}")

    # Q12: Ghost trade lifecycle
    print(f"\n--- Ghost Trades ---")
    if ghost_trades:
        # Use only initial records (status-update records have "update_type" key)
        initial_trades = [t for t in ghost_trades if "update_type" not in t]
        update_records = [t for t in ghost_trades if "update_type" in t]

        placed = sum(1 for t in initial_trades if t.get("status") == GHOST_STATUS_ORDER_PLACED)
        filled = sum(1 for t in initial_trades if t.get("status") == GHOST_STATUS_FILLED)
        expired = sum(1 for t in update_records if t.get("new_status") == GHOST_STATUS_EXPIRED)
        cancelled = sum(1 for t in update_records if t.get("new_status") == GHOST_STATUS_CANCELLED)

        by_city = Counter(t.get("city") for t in initial_trades if t.get("city"))
        by_type = Counter(t.get("ghost_entry_type") for t in initial_trades)
        total_usdc = sum(t.get("ghost_size_usdc", 0) for t in initial_trades)

        print(f"  Total initial records:  {len(initial_trades)}")
        print(f"  Update records:         {len(update_records)}")
        print(f"  Placed (maker pending): {placed}")
        print(f"  Filled (taker immed.):  {filled}")
        print(f"  Expired unfilled:       {expired}")
        print(f"  Cancelled/repriced:     {cancelled}")
        print(f"  Total notional USDC:    {total_usdc:.2f}")
        print(f"  By entry type:          {dict(by_type)}")
        print(f"  Top cities:             {dict(by_city.most_common(5))}")
    else:
        print("  No ghost trades yet.")

    # Q13: Top reject reasons
    reject_reasons = Counter(
        o.get("action_reason") or o.get("reject_reason") or o.get("ev_reason")
        for o in observations
        if (o.get("action_reason") or o.get("reject_reason") or o.get("ev_reason"))
    )
    print(f"\n--- Top Reject/Action Reasons (top 10) ---")
    for reason, count in reject_reasons.most_common(10):
        print(f"  {reason}: {count}")

    print(f"\n{'='*60}")
    print("Log files:")
    print(f"  Observations: {OBSERVATIONS_FILE}")
    print(f"  Signals:      {SIGNALS_FILE}")
    print(f"  Ghost trades: {GHOST_TRADES_FILE}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    analyze()
