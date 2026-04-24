#!/usr/bin/env python3
"""
Analyze signals and observations from JSONL logs.

Answers the V1 success criteria questions:
  1. How many active daily temperature markets found?
  2. How many parsed successfully?
  3. Which cities appeared most?
  4. Which markets had usable orderbook depth?
  5. Which buckets showed model-vs-market edge?
  6. Which signals survived filters?
  7. Which cities had best settlement safety?
  8. Which signal type dominated?
  9. How many ghost trades?
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
    read_jsonl,
)


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

    # Q1: Market types
    types = Counter(o.get("market_type", "unknown") for o in observations)
    print(f"\n--- Market Types ---")
    for t, c in types.most_common():
        print(f"  {t}: {c}")

    # Q2: Parse success
    parse_ok = sum(1 for o in observations if o.get("reject_reason") is None or "parse_failed" not in str(o.get("reject_reason", "")))
    parse_fail = sum(1 for o in observations if "parse_failed" in str(o.get("reject_reason", "")))
    print(f"\n--- Parse Results ---")
    print(f"  Parsed OK:     {parse_ok}")
    print(f"  Parse failed:  {parse_fail}")

    # Q3: Top cities
    cities = Counter(o.get("city") for o in observations if o.get("city"))
    print(f"\n--- Top Cities (top 15) ---")
    for city, count in cities.most_common(15):
        print(f"  {city}: {count}")

    # Q4: Orderbook depth
    with_book = [o for o in observations if o.get("best_ask") is not None]
    deep_book = [o for o in with_book if (o.get("top_book_depth") or 0) >= 4.0]
    print(f"\n--- Orderbook Coverage ---")
    print(f"  Has orderbook:     {len(with_book)} / {len(observations)}")
    print(f"  Usable depth (>=4): {len(deep_book)}")

    modes = Counter(o.get("display_price_mode", "unknown") for o in observations)
    for mode, c in modes.most_common():
        print(f"  mode={mode}: {c}")

    # Q5: Edge distribution
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

        # Top edge observations
        top_edge = sorted(obs_with_edge, key=lambda x: x.get("edge_net_maker", 0), reverse=True)[:5]
        print(f"  Top 5 maker edge markets:")
        for o in top_edge:
            print(f"    {o.get('city','?')} | {o.get('bucket_label','?')} | "
                  f"model={o.get('model_probability',0):.2f} "
                  f"ask={o.get('best_ask','?')} "
                  f"edge_net_maker={o.get('edge_net_maker',0):.4f}")

    # Q6: Signals that survived filters
    print(f"\n--- Surviving Signals ---")
    action_counts = Counter(s.get("action", "?") for s in signals)
    for action, count in action_counts.most_common():
        print(f"  {action}: {count}")

    # Q7: Settlement safety by city
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

    # Q8: Signal types
    if signals:
        sig_types = Counter(s.get("signal_type", "?") for s in signals)
        print(f"\n--- Signal Types ---")
        for st, count in sig_types.most_common():
            print(f"  {st}: {count}")

    # Q9: Ghost trades
    print(f"\n--- Ghost Trades ---")
    if ghost_trades:
        open_gt = [t for t in ghost_trades if t.get("status") == "open"]
        by_city = Counter(t.get("city") for t in ghost_trades if t.get("city"))
        by_type = Counter(t.get("ghost_entry_type") for t in ghost_trades)
        total_usdc = sum(t.get("ghost_size_usdc", 0) for t in ghost_trades)
        print(f"  Total ghost trades:  {len(ghost_trades)}")
        print(f"  Open:                {len(open_gt)}")
        print(f"  Total notional USDC: {total_usdc:.2f}")
        print(f"  By entry type:       {dict(by_type)}")
        print(f"  Top cities:          {dict(by_city.most_common(5))}")
    else:
        print("  No ghost trades yet.")

    print(f"\n{'='*60}")
    print("Log files:")
    print(f"  Observations: {OBSERVATIONS_FILE}")
    print(f"  Signals:      {SIGNALS_FILE}")
    print(f"  Ghost trades: {GHOST_TRADES_FILE}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    analyze()
