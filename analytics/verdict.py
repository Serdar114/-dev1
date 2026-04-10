"""
analytics/verdict.py — Overall verdict on measurement run quality.

Summarises what was measured and whether the data is trustworthy enough
to proceed to selective paper mode.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def verdict(log_path: str) -> dict:
    """
    Analyse a completed measurement run log.
    Returns a verdict dict with key stats.
    """
    path = Path(log_path)
    if not path.exists():
        return {"error": "log file not found", "ready_for_paper": False}

    total_events = 0
    windows_seen = set()
    hypotheticals = 0
    resolved_canonical = 0
    no_trades = 0
    chainlink_stale = 0
    chainlink_missing = 0
    market_not_found = 0

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_events += 1
            etype = ev.get("event_type", "")
            wid = ev.get("window_id", 0)
            if wid:
                windows_seen.add(wid)
            if etype == "hypothetical_entry":
                hypotheticals += 1
            elif etype == "resolution":
                if ev.get("status") == "resolved_canonical":
                    resolved_canonical += 1
            elif etype == "no_trade":
                no_trades += 1
                for r in ev.get("reasons", []):
                    if "chainlink_stale" in r:
                        chainlink_stale += 1
                    if "chainlink_missing" in r:
                        chainlink_missing += 1
                    if "market_not_found" in r:
                        market_not_found += 1

    resolution_rate = resolved_canonical / len(windows_seen) if windows_seen else 0.0
    ready = (
        len(windows_seen) >= 20
        and resolution_rate >= 0.8
        and hypotheticals >= 20
        and chainlink_missing == 0
    )

    return {
        "total_events": total_events,
        "windows_seen": len(windows_seen),
        "hypotheticals_recorded": hypotheticals,
        "resolved_canonical": resolved_canonical,
        "resolution_rate": f"{resolution_rate:.1%}",
        "no_trade_ticks": no_trades,
        "chainlink_stale_blocks": chainlink_stale,
        "chainlink_missing_blocks": chainlink_missing,
        "market_not_found_blocks": market_not_found,
        "ready_for_paper": ready,
        "verdict": "READY_FOR_PAPER" if ready else "NEED_MORE_DATA",
    }


def print_verdict(log_path: str) -> None:
    v = verdict(log_path)
    print("\n=== MEASUREMENT VERDICT ===")
    for k, val in v.items():
        print(f"  {k}: {val}")
