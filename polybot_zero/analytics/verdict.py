"""
verdict.py — Human-readable verdict generator for ChatGPT review.

Design:
  Produces a structured, honest verdict on what was observed.
  Explicitly separates: proven vs assumed, measured vs guessed, canonical vs auxiliary.
  Does NOT claim edge without evidence.
  Designed to be sent directly to ChatGPT for final judgment.
"""

from __future__ import annotations
import json
import time
from typing import Dict, List, Optional

from loggingx.schemas import BucketStats


class VerdictGenerator:
    """
    Generates final run verdict for human/ChatGPT review.

    Job: summarize what was observed, what is unproven, and what (if anything) is actionable.
    Input: run statistics, bucket stats, system event counts
    Output: structured dict + human-readable text
    """

    def generate(
        self,
        run_duration_secs: float,
        n_markets_discovered: int,
        n_windows_observed: int,
        n_windows_resolved: int,
        n_windows_unresolved: int,
        n_no_trade: int,
        n_hypothetical_entries: int,
        n_chainlink_stale: int,
        bucket_rows: List[dict],
        proven_buckets: set,
        paper_closed: Optional[List[dict]] = None,
    ) -> dict:
        verdict = {
            "generated_at": time.time(),
            "run_duration_secs": round(run_duration_secs, 1),
            "scope": "BTC 5m Polymarket Up/Down — measurement_only mode",
            "canonical_truth": "Chainlink BTC/USD via Polygon RPC",

            "discovery": {
                "markets_discovered": n_markets_discovered,
            },
            "measurement": {
                "windows_observed":    n_windows_observed,
                "windows_resolved":    n_windows_resolved,
                "windows_unresolved":  n_windows_unresolved,
                "no_trade_events":     n_no_trade,
                "hypothetical_entries": n_hypothetical_entries,
                "chainlink_stale_events": n_chainlink_stale,
            },
            "edge_analysis": {
                "total_buckets_measured": len(bucket_rows),
                "buckets_with_edge":      sum(1 for r in bucket_rows if r.get("has_edge")),
                "proven_buckets":         list(proven_buckets),
                "bucket_table":           bucket_rows,
            },
            "paper_summary": paper_closed or [],
            "caveats": self._caveats(n_windows_resolved, n_hypothetical_entries, proven_buckets),
            "verdict": self._verdict_text(n_windows_resolved, proven_buckets, paper_closed),
        }
        return verdict

    def _caveats(
        self,
        n_resolved: int,
        n_hypothetical: int,
        proven_buckets: set,
    ) -> List[str]:
        caveats = [
            "Fill model: taker at best ask — optimistic in thin books.",
            "Fee rate sourced from API. If missing, windows are no-trade.",
            "Resolution truth: Chainlink BTC/USD on Polygon. UNRESOLVED windows excluded from edge analysis.",
            "Binance is auxiliary only — never used for settlement.",
        ]
        if n_resolved < 20:
            caveats.append(
                f"INSUFFICIENT DATA: only {n_resolved} resolved windows. "
                "Edge claims require at least 20+ resolved windows per bucket."
            )
        if not proven_buckets:
            caveats.append(
                "No proven buckets identified. Do NOT proceed to paper mode. "
                "Continue measurement until sufficient data accumulates."
            )
        if n_hypothetical == 0:
            caveats.append(
                "Zero hypothetical entries recorded. "
                "Check: Chainlink connectivity, book feed, market discovery."
            )
        return caveats

    def _verdict_text(
        self,
        n_resolved: int,
        proven_buckets: set,
        paper_closed: Optional[List[dict]],
    ) -> str:
        if n_resolved < 20:
            return (
                "MEASUREMENT INCOMPLETE. "
                "Insufficient resolved windows to make any edge claim. "
                "Continue running measurement mode."
            )
        if not proven_buckets:
            return (
                "NO PROVEN EDGE DETECTED after sufficient measurement. "
                "No buckets meet the minimum evidence threshold. "
                "Do not proceed to selective paper mode."
            )
        if paper_closed:
            net = sum(t.get("net_pnl_usdc", 0) or 0 for t in paper_closed)
            return (
                f"PAPER MODE RESULTS: {len(paper_closed)} trades closed. "
                f"Total net PnL: {net:.4f} USDC. "
                f"Proven buckets: {list(proven_buckets)}. "
                "Send full verdict to ChatGPT for final judgment."
            )
        return (
            f"PROVEN BUCKETS IDENTIFIED: {list(proven_buckets)}. "
            "Ready for selective paper mode. "
            "Send this verdict to ChatGPT before proceeding."
        )

    def to_text(self, verdict: dict) -> str:
        return json.dumps(verdict, indent=2, default=str)
