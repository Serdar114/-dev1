"""
Analyzer — truth JSONL observation stream'ini oku ve özetle.

Kullanım:
  python analyzer.py                      # bugünün dosyası
  python analyzer.py logs/truth-2026-03-26.jsonl   # belirli dosya
  python analyzer.py logs/truth-*.jsonl   # glob ile birden fazla

Çıktı:
  - Toplam event sayısı
  - Event type dağılımı
  - resolution_truth_status dağılımı
  - fee_source dağılımı
  - pair_sum min/max/avg (görüldüyse)
  - unresolved / blocked count
  - execution_lane dağılımı
"""

import json
import sys
import glob
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone


def load_events(paths: list[str]) -> list[dict]:
    """JSONL dosyalarını oku, her satır bir dict."""
    events = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        print(f"  WARN: {p}:{line_no} parse error: {e}")
        except FileNotFoundError:
            print(f"  WARN: file not found: {p}")
    return events


def analyze(events: list[dict]) -> dict:
    """Event listesinden özet istatistikler üret."""
    total = len(events)
    if total == 0:
        return {"total_events": 0, "note": "No events found."}

    # Event type dağılımı
    event_types = Counter(e.get("event", "unknown") for e in events)

    # resolution_truth_status dağılımı (sadece doluysa)
    rts_values = [e["resolution_truth_status"] for e in events
                  if e.get("resolution_truth_status")]
    rts_dist = Counter(rts_values)

    # fee_source dağılımı (sadece doluysa)
    fee_sources = [e["fee_source"] for e in events if e.get("fee_source")]
    fee_dist = Counter(fee_sources)

    # fee_status dağılımı
    fee_statuses = [e["fee_status"] for e in events if e.get("fee_status")]
    fee_status_dist = Counter(fee_statuses)

    # execution_lane dağılımı
    lanes = [e["execution_lane"] for e in events if e.get("execution_lane")]
    lane_dist = Counter(lanes)

    # pair_sum istatistikleri
    pair_sums = [e["pair_sum"] for e in events
                 if "pair_sum" in e and e["pair_sum"] is not None
                 and isinstance(e["pair_sum"], (int, float))]
    pair_sum_stats = {}
    if pair_sums:
        pair_sum_stats = {
            "count": len(pair_sums),
            "min": round(min(pair_sums), 4),
            "max": round(max(pair_sums), 4),
            "avg": round(sum(pair_sums) / len(pair_sums), 4),
        }

    # Unresolved / blocked counts
    blocked_count = sum(1 for e in events if e.get("event") == "trade_resolution_blocked")
    resolved_count = sum(1 for e in events if e.get("event") == "trade_resolved")
    opened_count = sum(1 for e in events if e.get("event") == "trade_opened")

    # Time range
    timestamps = [e["ts"] for e in events if "ts" in e]
    time_range = {}
    if timestamps:
        time_range = {"first": min(timestamps), "last": max(timestamps)}

    # Context coverage — trade events only
    trade_events = [e for e in events if e.get("event") in
                    ("trade_opened", "trade_resolved", "trade_resolution_blocked")]
    trade_count = len(trade_events)
    context_fields = [
        "market_slug", "btc_mid_binance",
        "up_ask", "down_ask", "up_bid", "down_bid",
        "spread_up_pct", "spread_down_pct", "secs_to_res",
    ]
    context_coverage = {}
    if trade_count > 0:
        for field in context_fields:
            present = sum(1 for e in trade_events
                         if e.get(field) and e[field] != 0 and e[field] != 0.0)
            context_coverage[field] = {
                "present": present,
                "total": trade_count,
                "pct": round(present / trade_count * 100, 1),
            }

    return {
        "total_events": total,
        "time_range": time_range,
        "event_type_distribution": dict(event_types.most_common()),
        "trade_opened_count": opened_count,
        "trade_resolved_count": resolved_count,
        "trade_resolution_blocked_count": blocked_count,
        "resolution_truth_status_distribution": dict(rts_dist.most_common()),
        "fee_source_distribution": dict(fee_dist.most_common()),
        "fee_status_distribution": dict(fee_status_dist.most_common()),
        "execution_lane_distribution": dict(lane_dist.most_common()),
        "pair_sum_stats": pair_sum_stats if pair_sum_stats else "no pair_sum observations",
        "context_coverage": context_coverage if context_coverage else "no trade events",
    }


def print_report(summary: dict) -> None:
    """Özet raporu stdout'a yaz."""
    print("=" * 60)
    print("TRUTH OBSERVATION ANALYZER")
    print("=" * 60)

    print(f"\nTotal events: {summary['total_events']}")

    if summary["total_events"] == 0:
        print("No events to analyze.")
        return

    tr = summary.get("time_range", {})
    if tr:
        print(f"Time range: {tr.get('first', '?')} → {tr.get('last', '?')}")

    print(f"\n--- Event Type Distribution ---")
    for k, v in summary["event_type_distribution"].items():
        print(f"  {k}: {v}")

    print(f"\n--- Trade Counts ---")
    print(f"  opened:   {summary['trade_opened_count']}")
    print(f"  resolved: {summary['trade_resolved_count']}")
    print(f"  blocked:  {summary['trade_resolution_blocked_count']}")

    print(f"\n--- Resolution Truth Status ---")
    rts = summary["resolution_truth_status_distribution"]
    if rts:
        for k, v in rts.items():
            print(f"  {k}: {v}")
    else:
        print("  (no resolution_truth_status observations)")

    print(f"\n--- Fee Source Distribution ---")
    fs = summary["fee_source_distribution"]
    if fs:
        for k, v in fs.items():
            print(f"  {k}: {v}")
    else:
        print("  (no fee_source observations)")

    print(f"\n--- Fee Status Distribution ---")
    fst = summary["fee_status_distribution"]
    if fst:
        for k, v in fst.items():
            print(f"  {k}: {v}")
    else:
        print("  (no fee_status observations)")

    print(f"\n--- Execution Lane Distribution ---")
    ld = summary["execution_lane_distribution"]
    if ld:
        for k, v in ld.items():
            print(f"  {k}: {v}")
    else:
        print("  (no execution_lane observations)")

    print(f"\n--- Pair Sum Stats ---")
    ps = summary["pair_sum_stats"]
    if isinstance(ps, dict):
        print(f"  count: {ps['count']}")
        print(f"  min:   {ps['min']}")
        print(f"  max:   {ps['max']}")
        print(f"  avg:   {ps['avg']}")
    else:
        print(f"  {ps}")

    print(f"\n--- Trade Context Coverage ---")
    cc = summary.get("context_coverage", {})
    if isinstance(cc, dict) and cc:
        for field, info in cc.items():
            print(f"  {field}: {info['present']}/{info['total']} ({info['pct']}%)")
    else:
        print(f"  {cc}")

    print("\n" + "=" * 60)


def main():
    if len(sys.argv) > 1:
        # Explicit paths or glob patterns
        paths = []
        for arg in sys.argv[1:]:
            expanded = glob.glob(arg)
            paths.extend(expanded if expanded else [arg])
    else:
        # Default: today's truth file
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        default_path = str(Path(__file__).parent / "logs" / f"truth-{date_str}.jsonl")
        paths = [default_path]

    print(f"Reading: {paths}")
    events = load_events(paths)
    summary = analyze(events)
    print_report(summary)
    return summary


if __name__ == "__main__":
    main()
