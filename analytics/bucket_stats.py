"""
analytics/bucket_stats.py — Per-bucket statistics accumulator.

Reads from the JSONL log to compute per-bucket hypothetical performance.
Not run in real-time. Run as a post-processing analysis step.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class BucketRecord:
    bucket: str
    hypothetical_entries: int = 0
    resolved_count: int = 0
    up_wins: int = 0
    down_wins: int = 0
    net_payoff_up_total: float = 0.0
    net_payoff_down_total: float = 0.0
    windows: List[int] = field(default_factory=list)

    def win_rate(self) -> Optional[float]:
        if self.resolved_count == 0:
            return None
        return self.up_wins / self.resolved_count

    def avg_net_payoff_up(self) -> Optional[float]:
        if self.resolved_count == 0:
            return None
        return self.net_payoff_up_total / self.resolved_count


def compute_bucket_stats(log_path: str) -> Dict[str, BucketRecord]:
    """
    Parse the JSONL log and return per-bucket statistics.
    """
    buckets: Dict[str, BucketRecord] = defaultdict(lambda: BucketRecord(bucket=""))

    hypotheticals: Dict[int, list] = defaultdict(list)   # window_id -> entries
    resolutions: Dict[int, str] = {}                     # window_id -> outcome

    path = Path(log_path)
    if not path.exists():
        return {}

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = ev.get("event_type", "")
            if etype == "hypothetical_entry":
                wid = ev.get("window_id", 0)
                hypotheticals[wid].append(ev)

            elif etype == "resolution":
                wid = ev.get("resolved_window_start", 0)
                status = ev.get("status", "")
                if status == "resolved_canonical":
                    resolutions[wid] = ev.get("outcome", "")

    # Accumulate
    for wid, entries in hypotheticals.items():
        outcome = resolutions.get(wid)
        for entry in entries:
            bucket_label = entry.get("bucket", "UNKNOWN")
            if bucket_label not in buckets:
                buckets[bucket_label] = BucketRecord(bucket=bucket_label)
            rec = buckets[bucket_label]
            rec.hypothetical_entries += 1
            if wid not in rec.windows:
                rec.windows.append(wid)
            if outcome in ("Up", "Down"):
                rec.resolved_count += 1
                if entry.get("side") == "Up":
                    if outcome == "Up":
                        rec.up_wins += 1
                    net = entry.get("net_payoff_if_win", 0) if outcome == "Up" else entry.get("net_payoff_if_lose", 0)
                    rec.net_payoff_up_total += net

    return dict(buckets)
