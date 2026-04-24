#!/usr/bin/env python3
"""
Run settlement audit against resolved markets.

Checks open ghost trades for resolution and computes paper PnL.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weatherbot.settlement_audit import run_settlement_audit
from weatherbot.ghost_logger import read_jsonl, SETTLEMENT_AUDIT_FILE


def main():
    print("\n=== SETTLEMENT AUDIT ===")
    summary = run_settlement_audit()
    print(f"Open trades checked:      {summary.get('total_open_trades', 0)}")
    print(f"Markets checked:          {summary.get('markets_checked', 0)}")
    print(f"Trades resolved:          {summary.get('trades_resolved', 0)}")
    print(f"Fill-adjusted paper PnL:  {summary.get('total_fill_adjusted_pnl', 0.0):.4f} USDC")

    audit_records = read_jsonl(SETTLEMENT_AUDIT_FILE)
    resolved = [r for r in audit_records if r.get("status") == "resolved"]
    if resolved:
        wins = [r for r in resolved if r.get("ghost_won")]
        losses = [r for r in resolved if not r.get("ghost_won")]
        print(f"\nWins: {len(wins)}  |  Losses: {len(losses)}")
        for r in resolved[-10:]:
            won_str = "WIN" if r.get("ghost_won") else "LOSS"
            print(f"  [{won_str}] {r.get('city','?')} {r.get('bucket_label','?')} "
                  f"entry={r.get('ghost_price','?')} "
                  f"pnl={r.get('fill_adjusted_pnl','?')}")

    print(f"\nAudit log: {SETTLEMENT_AUDIT_FILE}")


if __name__ == "__main__":
    main()
